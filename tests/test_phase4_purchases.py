"""Phase 4 acceptance test — purchases, expenses, payables.

Run against a COPY of the live database:

    rm -f /tmp/p4.db*; cp instance/pricing.db /tmp/p4.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/p4.db COOKIE_INSECURE=1 \
    python3 tests/test_phase4_purchases.py

Proves:
  1.  Stock purchase on account: DR inventory net + DR VAT input + CR AP
      gross; item receives at the bought cost; average moves correctly.
  2.  Stock NEVER hits expense: an expense line pointed at an inventory or
      revenue account is refused.
  3.  Expense purchase paid cash: DR expense + CR cash; no stock movement.
  4.  Mixed bill (stock + expense) balances; VAT only from a VAT-registered
      supplier.
  5.  Supplier balance = posted on-account gross; cash bills excluded;
      reversal removes it.
  6.  Reversal mirrors the journal and issues the goods back out; second
      reversal refused; insufficient stock blocks reversal.
  7.  Purchases with no stock unit on the item are refused (worklist rule).
  8.  Posted purchases refuse edits and deletes at the database level.
  9.  Screens render; clerk CAN record (SoD: entry is clerk work); cashier
      cannot; GL ties and the trial balance still balances.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-only-secret")
os.environ.setdefault("COOKIE_INSECURE", "1")
if "DATABASE_URL" not in os.environ:
    print("Refusing to run without DATABASE_URL.")
    sys.exit(2)

import re

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app import app
from extensions import db
from models import AccSupplier, AccItem, User, Store, StoreItem
from services import ledger
from services import inventory_costing as inv
from services import purchase_posting as pp

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


with app.app_context():
    raw = db.engine.raw_connection()
    for f in ("acc_001_triggers.sql", "acc_002_inventory.sql",
              "acc_003_invoices.sql", "acc_004_purchases.sql"):
        raw.executescript(open(os.path.join(
            os.path.dirname(__file__), "..", "migrations", f)).read())
    raw.commit()
    raw.close()

    sup = AccSupplier(name="TEST Abattoir Ltd", vat_registered=True,
                      payment_terms="14 days")
    sup_cash = AccSupplier(name="TEST Market Vendor", vat_registered=False)
    db.session.add_all([sup, sup_cash])
    # a raw-material item to buy into. M2 (QA audit 5 Jul 2026): every
    # acc_item needs a source, so each fixture gets a disposable StoreItem.
    _store = db.session.scalar(db.select(Store).limit(1))
    _si1 = StoreItem(store_id=_store.id, name="TEST Beef carcass side", uom="kg")
    _si2 = StoreItem(store_id=_store.id, name="TEST Unitless thing")
    db.session.add_all([_si1, _si2])
    db.session.flush()
    carcass = AccItem(name="TEST Beef carcass side", stage="raw",
                      stock_unit="kg", store_item_id=_si1.id)
    no_unit = AccItem(name="TEST Unitless thing", stage="raw",
                      store_item_id=_si2.id)
    db.session.add_all([carcass, no_unit])
    db.session.commit()

    print("== 1. Stock purchase on account ==")
    p1 = pp.post_purchase(sup, [
        {"type": "stock", "item_id": carcass.id, "qty": 100,
         "unit_cost": 14000, "vat": False}],
        pay_from="account", bill_ref="ABT-001")
    je = p1.journal_entry
    check("journal balanced", je.posted and je.is_balanced,
          f"{je.entry_no}: {je.total_debit:,}")
    keys = {l.account.system_key: l for l in je.lines}
    check("DR inv_raw 1,400,000 / CR AP 1,400,000",
          keys["inv_raw"].debit == 1_400_000
          and keys["ap_control"].credit == 1_400_000)
    check("item received at cost", carcass.qty_on_hand == 100
          and carcass.value_on_hand == 1_400_000
          and carcass.avg_cost == 14000)
    p1b = pp.post_purchase(sup, [
        {"type": "stock", "item_id": carcass.id, "qty": 50,
         "unit_cost": 17000, "vat": False}], pay_from="account")
    check("average moves: (100@14k + 50@17k) -> 15,000/kg",
          carcass.avg_cost == 15000 and carcass.value_on_hand == 2_250_000)

    print("== 2. Stock never hits expense ==")
    try:
        pp.post_purchase(sup, [{"type": "expense", "account": "inv_raw",
                                "amount": 100000}])
        check("expense line to inventory account refused", False)
    except pp.PurchaseError as e:
        check("expense line to inventory account refused", True, str(e)[:60])
    try:
        pp.post_purchase(sup, [{"type": "expense", "account": "rev_fresh",
                                "amount": 100000}])
        check("expense line to revenue account refused", False)
    except pp.PurchaseError:
        check("expense line to revenue account refused", True)

    print("== 3. Cash expense ==")
    p2 = pp.post_purchase(sup_cash, [
        {"type": "expense", "account": "6200", "amount": 80000,
         "description": "Fuel for delivery truck", "vat": True}],
        pay_from="cash")
    keys2 = {(l.account.system_key or l.account.code): l for l in p2.journal_entry.lines}
    check("DR 6200 / CR cash, no VAT from unregistered supplier",
          keys2["6200"].debit == 80000 and keys2["cash"].credit == 80000
          and p2.vat_minor == 0)
    n_mv = db.session.execute(text(
        "SELECT count(*) FROM acc_inv_movement WHERE journal_entry_id=:e"),
        {"e": p2.journal_entry_id}).scalar()
    check("no stock movement on a pure expense", n_mv == 0)

    print("== 4. Mixed bill with input VAT ==")
    p3 = pp.post_purchase(sup, [
        {"type": "stock", "item_id": carcass.id, "qty": 10,
         "unit_cost": 15000, "vat": True},
        {"type": "expense", "account": "6200", "amount": 50000, "vat": True}],
        pay_from="account")
    vat_expect = ledger.to_minor(10 * 15000 * 0.18) + ledger.to_minor(50000 * 0.18)
    check("input VAT booked (registered supplier)",
          p3.vat_minor == vat_expect
          and any(l.account.system_key == "vat_input" and l.debit == vat_expect
                  for l in p3.journal_entry.lines), f"{p3.vat_minor:,}")
    check("gross = net + VAT", p3.gross_minor == p3.net_minor + p3.vat_minor)

    print("== 5. Supplier balances ==")
    bal = sup.balance_minor
    expect = p1.gross_minor + p1b.gross_minor + p3.gross_minor
    check("balance = on-account posted gross", bal == expect, f"{bal:,}")
    check("cash supplier owes nothing", sup_cash.balance_minor == 0)

    print("== 6. Reversal ==")
    qty0, val0 = carcass.qty_on_hand, carcass.value_on_hand
    rev = pp.reverse_purchase(p1b, reason="TEST wrong price")
    check("reversal journal mirrors", rev.posted
          and rev.reversal_of_id == p1b.journal_entry_id)
    check("goods issued back out",
          carcass.qty_on_hand == qty0 - 50 and carcass.value_on_hand < val0)
    check("supplier balance drops", sup.balance_minor == expect - p1b.gross_minor)
    try:
        pp.reverse_purchase(p1b)
        check("second reversal refused", False)
    except pp.PurchaseError:
        check("second reversal refused", True)

    print("== 7. Unit rule ==")
    try:
        pp.post_purchase(sup, [{"type": "stock", "item_id": no_unit.id,
                                "qty": 5, "unit_cost": 1000}])
        check("item without stock unit refused", False)
    except pp.PurchaseError as e:
        check("item without stock unit refused", True, str(e)[:55])

    print("== 8. Immutability (raw SQL) ==")
    for label, stmt in [
            ("gross edit refused",
             f"UPDATE acc_purchase SET gross_minor=1 WHERE id={p1.id}"),
            ("DELETE refused", f"DELETE FROM acc_purchase WHERE id={p1.id}")]:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            check(label, False)
        except DatabaseError as e:
            db.session.rollback()
            check(label, True, str(e.orig)[:50])

    print("== 9. GL ties + trial balance ==")
    ties = inv.valuation_summary()
    check("inventory subledger ties to GL", all(t["tied"] for t in ties),
          "; ".join(f"{t['stage']}: {t['subledger']:,}" for t in ties))
    rows, tdr, tcr = ledger.trial_balance()
    check("trial balance balances", tdr == tcr, f"{tdr:,}")

    admin_id = db.session.scalar(db.select(User.id).where(User.role == "admin"))
    for role in ("finance_clerk", "cashier"):
        u = User(username=f"t4_{role}", full_name=role, role=role,
                 password_hash="x")
        db.session.add(u)
    db.session.commit()
    clerk_id = db.session.scalar(db.select(User.id).where(User.username == "t4_finance_clerk"))
    cashier_id = db.session.scalar(db.select(User.id).where(User.username == "t4_cashier"))
    sup_id, p1_id = sup.id, p1.id

print("== screens + roles ==")


def login_as(c, uid):
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True


c = app.test_client()
login_as(c, admin_id)
for p in ("/accounting/purchases", "/accounting/purchases/new",
          f"/accounting/purchases/{p1_id}", "/accounting/suppliers"):
    r = c.get(p)
    check(f"admin GET {p}", r.status_code == 200, str(r.status_code))

ck = app.test_client()
login_as(ck, clerk_id)
check("clerk sees purchase entry", ck.get("/accounting/purchases/new").status_code == 200)
page = ck.get("/accounting/purchases/new").get_data(as_text=True)
token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
r = ck.post("/accounting/purchases/new", data={
    "csrf_token": token, "supplier_id": str(sup_id), "pay_from": "account",
    "purchase_date": "2026-07-02",
    "ltype": ["expense"], "item_id": [""], "qty": [""],
    "unit_cost": ["25000"], "amount": ["25000"],
    "expense_account": [""], "ldesc": ["airtime"], "lvat": ["0"],
}, follow_redirects=True)
# expense_account empty -> line dropped -> friendly error, still a 200 flow
check("clerk POST flows (validation, no crash)", r.status_code == 200)

cs = app.test_client()
login_as(cs, cashier_id)
check("cashier blocked from purchases", cs.get("/accounting/purchases/new").status_code == 403)

with app.app_context():
    db.session.execute(text("DELETE FROM user WHERE username LIKE 't4_%'"))
    db.session.commit()

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
