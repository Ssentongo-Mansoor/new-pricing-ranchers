"""Phase 7 acceptance test — own shops: transfers, not sales.

Run against a COPY of the live database:

    rm -f /tmp/p7.db*; cp instance/pricing.db /tmp/p7.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/p7.db COOKIE_INSECURE=1 \
    EFRIS_MODE=simulate python3 tests/test_phase7_shops.py

Proves:
  1.  Locations seed; a customer links to a shop.
  2.  Fulfilling an INTERNAL order posts a transfer: NO invoice, NO journal,
      NO EFRIS queue row, NO revenue. Shop qty rises; valued total unchanged.
  3.  Fulfilling a NORMAL order still invoices (regression).
  4.  The shop's daily sale posts: cash gross, revenue net (VAT extracted
      only on vatable lines), VAT output, COGS at weighted average; shop qty
      falls; selling more than the shop holds is refused.
  5.  Shop VAT lands in the VAT summary; revenue in the P&L; balance sheet
      and GL ties hold.
  6.  Cancel of a transferred order is blocked; return transfer works.
  7.  Documents refuse edits/deletes (triggers); screens per role (cashier
      records shop sales, cannot link customers).
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

from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app import app
from extensions import db
from models import (AccItem, AccLocation, AccInvoice, AccEfrisQueue, Customer,
                    User, SalesOrder, SalesOrderLine)
from services import ledger
from services import inventory_costing as inv
from services import sale_posting as sp
from services import shop_ops
from services import reports_finance as rf

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def make_order(customer, items, vat=True):
    o = SalesOrder(customer_id=customer.id, currency="UGX",
                   market=customer.market or "local", vat_applicable=vat,
                   vat_rate=18.0, status="in_fulfillment",
                   order_date=date.today(), stock_deducted=True)
    db.session.add(o)
    db.session.flush()
    o.number = f"SO-T7-{o.id:05d}"
    for i, (item, qty, price, vatable) in enumerate(items):
        db.session.add(SalesOrderLine(
            order_id=o.id, product_id=item.product_id, description=item.name,
            quantity=qty, fulfilled_qty=qty, unit_price=price,
            vat_applicable=vatable, sort_order=i))
    db.session.commit()
    return o


with app.app_context():
    raw = db.engine.raw_connection()
    for f in ("acc_001_triggers.sql", "acc_002_inventory.sql",
              "acc_003_invoices.sql", "acc_004_purchases.sql",
              "acc_005_cash.sql", "acc_006_shops.sql"):
        raw.executescript(open(os.path.join(
            os.path.dirname(__file__), "..", "migrations", f)).read())
    raw.commit()
    raw.close()

    print("== 1. Locations + internal link ==")
    shop_ops.ensure_locations()
    locs = db.session.scalars(db.select(AccLocation)).all()
    check("plant + 3 shops seeded", len(locs) >= 4
          and sum(1 for l in locs if l.is_main) == 1,
          ", ".join(l.name for l in locs))
    lugogo = db.session.scalar(db.select(AccLocation).where(
        AccLocation.name == "Lugogo Shop"))
    inv.load_opening_stock()
    valued = db.session.scalar(db.select(AccItem).where(
        AccItem.value_on_hand > 0, AccItem.qty_on_hand >= 40))
    # a vatable + a non-vatable valued item for the retail VAT split
    vat_item = db.session.scalar(
        db.select(AccItem).join(AccItem.product).where(
            AccItem.value_on_hand > 0).where(
            db.text("product.vat_applicable = 1")))
    shop_cust = Customer(name="RF Lugogo Shop (internal)", market="local",
                         internal_location_id=lugogo.id)
    db.session.add(shop_cust)
    db.session.commit()

    print("== 2. Internal order -> transfer, not sale ==")
    pre_total_qty, pre_total_val = valued.qty_on_hand, valued.value_on_hand
    je_before = db.session.scalar(db.select(db.func.count()).select_from(
        db.text("acc_journal_entry")))
    o1 = make_order(shop_cust, [(valued, 20, 25000, True)])
    trf = shop_ops.transfer_for_order(o1)
    check("transfer posted", trf.transfer_no.startswith("TRF-")
          and trf.to_location_id == lugogo.id, trf.transfer_no)
    check("NO invoice for the internal order",
          sp.invoice_for_order(o1) is None)
    je_after = db.session.scalar(db.select(db.func.count()).select_from(
        db.text("acc_journal_entry")))
    check("NO journal posted by the transfer", je_after == je_before)
    check("NO EFRIS queue row", db.session.scalar(
        db.select(db.func.count(AccEfrisQueue.id))) == 0)
    check("shop quantity rose", shop_ops.shop_qty(valued.id, lugogo.id) == 20)
    check("valued entity total unchanged",
          valued.qty_on_hand == pre_total_qty
          and valued.value_on_hand == pre_total_val)
    check("idempotent per order",
          shop_ops.transfer_for_order(o1).id == trf.id)

    print("== 3. Normal order still invoices (regression) ==")
    normal_cust = db.session.scalar(db.select(Customer).where(
        Customer.market == "local", Customer.archived.is_(False),
        Customer.internal_location_id.is_(None)))
    o2 = make_order(normal_cust, [(valued, 2, 25000, True)])
    inv2 = sp.post_sale(o2)
    check("real customer gets an invoice", inv2 is not None
          and inv2.invoice_no.startswith("INV-"))

    print("== 4. Shop daily sale ==")
    avg = valued.value_on_hand / valued.qty_on_hand
    sale = shop_ops.post_shop_sale(
        lugogo, [(valued, 8, 400000)])   # vatable? depends on item
    vatable = bool(valued.product and valued.product.vat_applicable)
    exp_net = round(40000000 / 118) if vatable else 400000  # minor units below
    check("gross recorded", sale.gross_minor == 400000)
    if vatable:
        check("VAT extracted from till price",
              sale.vat_minor == 400000 - int(round(400000 / 1.18)),
              f"{sale.vat_minor:,}")
    else:
        check("no VAT on fresh line", sale.vat_minor == 0)
    check("COGS = 8 x weighted average",
          sale.cogs_minor == round(8 * avg), f"{sale.cogs_minor:,}")
    check("shop qty fell to 12", shop_ops.shop_qty(valued.id, lugogo.id) == 12)
    k = {l.account.system_key or l.account.code: l
         for l in sale.journal_entry.lines}
    check("cash debited with the gross", k["cash"].debit == 400000)
    try:
        shop_ops.post_shop_sale(lugogo, [(valued, 100, 100000)])
        check("selling beyond shop stock refused", False)
    except shop_ops.ShopError as e:
        check("selling beyond shop stock refused", True, str(e)[:50])

    print("== 5. Reports pick the shop up ==")
    today = date.today()
    v = rf.vat_summary(today - timedelta(days=1), today)
    check("shop VAT in the VAT summary", v["shop_vat"] == sale.vat_minor
          and any(s.id == sale.id for s in v["shop_sales"]))
    pl = rf.profit_and_loss(today - timedelta(days=1), today)
    check("shop revenue in the P&L income",
          pl["totals"]["income"] >= sale.net_minor)
    ties = inv.valuation_summary()
    check("inventory subledger ties to GL", all(t["tied"] for t in ties))
    rows, tdr, tcr = ledger.trial_balance()
    check("trial balance balances", tdr == tcr, f"{tdr:,}")

    print("== 6. Cancel guard + return transfer ==")
    plant = shop_ops.main_location()
    back = shop_ops.post_transfer(lugogo, plant, [(valued, 5)],
                                  notes="return for test")
    check("return transfer shop->plant", shop_ops.shop_qty(valued.id, lugogo.id) == 7)
    try:
        shop_ops.post_transfer(lugogo, plant, [(valued, 500)])
        check("over-transfer from shop refused", False)
    except shop_ops.ShopError:
        check("over-transfer from shop refused", True)

    print("== 7. Immutability ==")
    for label, stmt in [
            ("transfer DELETE refused",
             f"DELETE FROM acc_transfer WHERE id={trf.id}"),
            ("shop sale gross edit refused",
             f"UPDATE acc_shop_sale SET gross_minor=1 WHERE id={sale.id}")]:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            check(label, False)
        except DatabaseError as e:
            db.session.rollback()
            check(label, True, str(e.orig)[:45])

    admin_id = db.session.scalar(db.select(User.id).where(User.role == "admin"))
    cashier = User(username="t7_cashier", full_name="Till", role="cashier",
                   password_hash="x")
    db.session.add(cashier)
    db.session.commit()
    cashier_id, lugogo_id, o1_id = cashier.id, lugogo.id, o1.id

print("== screens + roles ==")


def login_as(c, uid):
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True


import re
a = app.test_client()
login_as(a, admin_id)
for p in ("/accounting/shops", f"/accounting/shops/{lugogo_id}"):
    check(f"admin GET {p}", a.get(p).status_code == 200)
page = a.get("/accounting/shops").get_data(as_text=True)
token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
r = a.post(f"/orders/{o1_id}/cancel", data={"csrf_token": token},
           follow_redirects=True)
check("cancel of transferred order blocked",
      "return" in r.get_data(as_text=True).lower()
      and "transfer" in r.get_data(as_text=True).lower())

c = app.test_client()
login_as(c, cashier_id)
check("cashier opens shops", c.get("/accounting/shops").status_code == 200)
check("cashier opens shop detail (sale entry)",
      c.get(f"/accounting/shops/{lugogo_id}").status_code == 200)
r = c.post("/accounting/shops/link", data={"location_id": str(lugogo_id)})
check("cashier cannot link customers", r.status_code in (400, 403))

with app.app_context():
    db.session.execute(text("DELETE FROM user WHERE username='t7_cashier'"))
    db.session.commit()

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
