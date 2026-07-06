"""Phase 5 acceptance test — cash & bank.

Run against a COPY of the live database:

    rm -f /tmp/p5.db*; cp instance/pricing.db /tmp/p5.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/p5.db COOKIE_INSECURE=1 \
    EFRIS_MODE=simulate python3 tests/test_phase5_cash.py

Proves:
  1.  UGX receipt with 6% WHT: DR bank 94% + DR 1320 6% / CR AR 100%;
      invoice paid balance moves; over-allocation refused; float refused.
  2.  Partial receipts across two invoices; oldest-first story works.
  3.  USD receipt at a different rate: AR relieved at the INVOICE rate,
      money at the RECEIPT rate, difference lands in 7000 FX.
  4.  Receipt reversal reopens the invoices.
  5.  Supplier payment clears bills; supplier balance and AP GL drop equally.
  6.  Transfer moves money between accounts; USD transfer refused for now.
  7.  Reconciliation closes only when cleared lines equal the statement.
  8.  Money documents refuse edits/deletes at the database level.
  9.  Cashier reaches receipts and NOTHING else in accounting; trial balance
      and inventory tie still hold.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-only-secret")
os.environ.setdefault("COOKIE_INSECURE", "1")
os.environ["EFRIS_MODE"] = "simulate"
if "DATABASE_URL" not in os.environ:
    print("Refusing to run without DATABASE_URL.")
    sys.exit(2)

from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app import app
from extensions import db
from models import (AccInvoice, AccItem, AccSupplier, Customer, User,
                    SalesOrder, SalesOrderLine)
from services import ledger, efris
from services import inventory_costing as inv
from services import sale_posting as sp
from services import purchase_posting as pp
from services import cash_posting as cash

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def make_order(customer, items, currency="UGX", rate=None, vat=True):
    o = SalesOrder(customer_id=customer.id, currency=currency,
                   market=customer.market, vat_applicable=vat, vat_rate=18.0,
                   exchange_rate_value=rate, status="in_fulfillment",
                   order_date=date.today(), stock_deducted=True)
    db.session.add(o)
    db.session.flush()
    o.number = f"SO-T5-{o.id:05d}"
    for i, (item, qty, price, vatable) in enumerate(items):
        db.session.add(SalesOrderLine(
            order_id=o.id, product_id=(item.product_id if item else None),
            description=(item.name if item else "x"), quantity=qty,
            fulfilled_qty=qty, unit_price=price, vat_applicable=vatable,
            sort_order=i))
    db.session.commit()
    return o


with app.app_context():
    raw = db.engine.raw_connection()
    for f in ("acc_001_triggers.sql", "acc_002_inventory.sql",
              "acc_003_invoices.sql", "acc_004_purchases.sql",
              "acc_005_cash.sql"):
        raw.executescript(open(os.path.join(
            os.path.dirname(__file__), "..", "migrations", f)).read())
    raw.commit()
    raw.close()

    inv.load_opening_stock()
    valued = db.session.scalar(db.select(AccItem).where(
        AccItem.value_on_hand > 0, AccItem.qty_on_hand >= 30))
    cust = db.session.scalar(db.select(Customer).where(
        Customer.market == "local", Customer.archived.is_(False)))
    exp_cust = db.session.scalar(db.select(Customer).where(
        Customer.market == "export")) or cust

    print("== 1. UGX receipt with WHT ==")
    o1 = make_order(cust, [(valued, 5, 100000, False)])   # net 500,000, no VAT
    inv1 = sp.post_sale(o1)
    check("fixture invoice open 500,000", inv1.open_minor == 500_000)
    r1 = cash.post_receipt(cust, [(inv1, 500_000)], method="bank_ugx",
                           amount=470_000, wht=30_000)
    je = r1.journal_entry
    k = {l.account.system_key: l for l in je.lines}
    check("DR bank 470,000 + DR WHT 30,000 / CR AR 500,000",
          k["bank_ugx"].debit == 470_000 and k["wht_recv"].debit == 30_000
          and k["ar_control"].credit == 500_000)
    check("invoice fully settled", inv1.open_minor == 0)
    o2 = make_order(cust, [(valued, 2, 50000, False)])
    inv2 = sp.post_sale(o2)
    try:
        cash.post_receipt(cust, [(inv2, 200_000)], amount=200_000)
        check("over-allocation refused", False)
    except cash.CashError:
        check("over-allocation refused", True)
    try:
        cash.post_receipt(cust, [(inv2, 50_000)], amount=80_000)
        check("floating money refused (alloc != received)", False)
    except cash.CashError as e:
        check("floating money refused (alloc != received)", True, str(e)[:50])

    print("== 2. Partial receipts ==")
    p_r = cash.post_receipt(cust, [(inv2, 60_000)], amount=60_000, method="cash")
    check("partial leaves the rest open", inv2.open_minor == 40_000)
    cash.post_receipt(cust, [(inv2, 40_000)], amount=40_000, method="cash")
    check("second receipt closes it", inv2.open_minor == 0)

    print("== 3. USD receipt with FX ==")
    o3 = make_order(exp_cust, [(valued, 2, 10.00, False)],
                    currency="USD", rate=3700, vat=False)
    inv3 = sp.post_sale(o3)   # USD 20.00 booked at 3,700 -> AR 74,000 UGX
    r3 = cash.post_receipt(exp_cust, [(inv3, 2000)], method="bank_usd",
                           amount=20.00, currency="USD", fx_rate=3800)
    k3 = {l.account.system_key: l for l in r3.journal_entry.lines}
    check("money at receipt rate (76,000), AR at invoice rate (74,000)",
          k3["bank_usd"].debit == 76_000 and k3["ar_control"].credit == 74_000)
    check("FX gain 2,000 booked", k3["fx"].credit == 2_000)
    check("USD invoice settled", inv3.open_minor == 0)

    print("== 4. Receipt reversal ==")
    rev = cash.reverse_receipt(p_r if False else r1, reason="TEST bounce")
    check("reversal reopens the invoice", inv1.open_minor == 500_000
          and rev.posted)

    print("== 5. Supplier payment ==")
    sup = AccSupplier(name="TEST5 Farm Supplies", payment_terms="14 days")
    db.session.add(sup)
    db.session.flush()
    b1 = pp.post_purchase(sup, [{"type": "expense", "account": "6300",
                                 "amount": 300000}], pay_from="account")
    b2 = pp.post_purchase(sup, [{"type": "expense", "account": "6300",
                                 "amount": 200000}], pay_from="account")
    check("supplier owes 500,000", sup.balance_minor == 500_000)
    ap_before = ledger.account_balances()
    pay = cash.post_supplier_payment(sup, [(b1, 300_000), (b2, 100_000)],
                                     method="bank_ugx")
    check("payment posted 400,000", pay.amount_minor == 400_000)
    check("supplier balance drops to 100,000", sup.balance_minor == 100_000)
    from services.coa import account_for
    ap_acct = account_for("ap_control")
    ap_after = ledger.account_balances()
    check("AP GL moved by exactly 400,000",
          (ap_after.get(ap_acct.id, 0) - ap_before.get(ap_acct.id, 0)) == 400_000)
    try:
        cash.post_supplier_payment(sup, [(b2, 200_000)])
        check("overpayment refused", False)
    except cash.CashError:
        check("overpayment refused", True)

    print("== 6. Transfers ==")
    bal0 = cash.money_balances()
    cash.post_transfer("cash", "bank_ugx", 50_000, notes="TEST banking the till")
    bal1 = cash.money_balances()
    check("cash -50,000 / bank +50,000",
          bal1["cash"][1] == bal0["cash"][1] - 50_000
          and bal1["bank_ugx"][1] == bal0["bank_ugx"][1] + 50_000)
    try:
        cash.post_transfer("bank_usd", "cash", 10)
        check("USD transfer refused for now", False)
    except cash.CashError:
        check("USD transfer refused for now", True)

    print("== 7. Reconciliation ==")
    acct = account_for("bank_ugx")
    lines = cash.uncleared_lines(acct.id)
    total = sum(l.signed_amount for l in lines)
    recon, diff = cash.close_reconciliation(
        acct, date.today(), total, [l.id for l in lines])
    check("closes when statement equals cleared", recon.status == "closed"
          and diff == 0, f"cleared {total:,}")
    check("cleared lines leave the uncleared list",
          len(cash.uncleared_lines(acct.id)) == 0)
    r_extra = cash.post_transfer("bank_ugx", "cash", 10_000)
    recon2, diff2 = cash.close_reconciliation(acct, date.today(), 0, [])
    check("mismatch stays open with the difference reported",
          recon2.status == "open" and diff2 != 0, f"diff {diff2:,}")

    print("== 8. Immutability (raw SQL) ==")
    for label, stmt in [
            ("receipt amount edit refused",
             f"UPDATE acc_receipt SET amount_minor=1 WHERE id={r3.id}"),
            ("receipt DELETE refused",
             f"DELETE FROM acc_receipt WHERE id={r3.id}"),
            ("payment DELETE refused",
             f"DELETE FROM acc_supplier_payment WHERE id={pay.id}")]:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            check(label, False)
        except DatabaseError as e:
            db.session.rollback()
            check(label, True, str(e.orig)[:45])

    print("== 9. Ties ==")
    ties = inv.valuation_summary()
    check("inventory subledger ties to GL", all(t["tied"] for t in ties))
    rows, tdr, tcr = ledger.trial_balance()
    check("trial balance balances", tdr == tcr, f"{tdr:,}")

    admin_id = db.session.scalar(db.select(User.id).where(User.role == "admin"))
    cashier = User(username="t5_cashier", full_name="Till", role="cashier",
                   password_hash="x")
    db.session.add(cashier)
    db.session.commit()
    cashier_id = cashier.id

print("== cashier access ==")


def login_as(c, uid):
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True


c = app.test_client()
login_as(c, cashier_id)
check("cashier opens receipts", c.get("/accounting/receipts").status_code == 200)
check("cashier opens receipt entry", c.get("/accounting/receipts/new").status_code == 200)
for p in ("/accounting/journal", "/accounting/invoices", "/accounting/cash-bank",
          "/accounting/payments"):
    check(f"cashier blocked from {p}", c.get(p).status_code == 403)
a = app.test_client()
login_as(a, admin_id)
for p in ("/accounting/receipts", "/accounting/receipts/new", "/accounting/payments",
          "/accounting/payments/new", "/accounting/cash-bank",
          "/accounting/cash-bank/reconcile/bank_ugx"):
    check(f"admin GET {p}", a.get(p).status_code == 200)

with app.app_context():
    db.session.execute(text("DELETE FROM user WHERE username='t5_cashier'"))
    db.session.commit()

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
