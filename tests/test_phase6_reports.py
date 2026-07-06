"""Phase 6 acceptance test — the six financial reports.

Run against a COPY of the live database:

    rm -f /tmp/p6.db*; cp instance/pricing.db /tmp/p6.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/p6.db COOKIE_INSECURE=1 \
    EFRIS_MODE=simulate python3 tests/test_phase6_reports.py

Builds a full month of activity through the REAL services (opening stock,
sales, purchases, receipts, payments, transfer), then proves every report
against independently computed expectations:

  1.  P&L: revenue = invoice nets, COGS = posted COGS, gross margin math,
      net profit = income - COGS - expenses.
  2.  Balance sheet balances: assets = liabilities + equity + result.
  3.  Cash flow: opening + in + out = closing = money GL.
  4.  Inventory valuation ties (standing check).
  5.  VAT: output = invoice VAT sum (credit notes negative), input =
      purchase VAT, net = output - input; every document lists its FDN.
  6.  Aged AR total = AR control; aged AP total = AP control; buckets sum.
  7.  Screens render for finance_viewer; rep blocked.
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

from app import app
from extensions import db
from models import (AccItem, AccSupplier, Customer, User, SalesOrder,
                    SalesOrderLine, Store, StoreItem)
from services import ledger, efris
from services import inventory_costing as inv
from services import sale_posting as sp
from services import purchase_posting as pp
from services import cash_posting as cash
from services import reports_finance as rf

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def make_order(customer, items, vat=True):
    o = SalesOrder(customer_id=customer.id, currency="UGX",
                   market=customer.market, vat_applicable=vat, vat_rate=18.0,
                   status="in_fulfillment", order_date=date.today(),
                   stock_deducted=True)
    db.session.add(o)
    db.session.flush()
    o.number = f"SO-T6-{o.id:05d}"
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
              "acc_005_cash.sql"):
        raw.executescript(open(os.path.join(
            os.path.dirname(__file__), "..", "migrations", f)).read())
    raw.commit()
    raw.close()

    print("== build a month of activity through the real services ==")
    entry, n_open, opening_value = inv.load_opening_stock()
    valued = db.session.scalar(db.select(AccItem).where(
        AccItem.value_on_hand > 0, AccItem.qty_on_hand >= 40))
    cust = db.session.scalar(db.select(Customer).where(
        Customer.market == "local", Customer.archived.is_(False)))
    sup = AccSupplier(name="T6 Abattoir", vat_registered=True)
    db.session.add(sup)
    db.session.flush()
    # M2 (QA audit 5 Jul 2026): acc_item needs a source — disposable StoreItem.
    _si = StoreItem(store_id=db.session.scalar(db.select(Store.id).limit(1)),
                    name="T6 carcass", uom="kg")
    db.session.add(_si)
    db.session.flush()
    carc = AccItem(name="T6 carcass", stage="raw", stock_unit="kg",
                   store_item_id=_si.id)
    db.session.add(carc)
    db.session.commit()

    # sale 1: vatable, valued -> revenue + VAT + COGS
    o1 = make_order(cust, [(valued, 10, 30000, True)])
    inv1 = sp.post_sale(o1)
    efris.try_fiscalize(inv1)
    # sale 2: non-vatable
    o2 = make_order(cust, [(valued, 5, 30000, False)])
    inv2 = sp.post_sale(o2)
    # credit note on sale 2
    cn = sp.post_credit_note(inv2, reason="T6 return", restock=True)
    efris.try_fiscalize(cn, action="credit_note")
    # purchases: stock on account + cash expense w/ VAT
    b1 = pp.post_purchase(sup, [{"type": "stock", "item_id": carc.id,
                                 "qty": 200, "unit_cost": 14000, "vat": False}],
                          pay_from="account")
    b2 = pp.post_purchase(sup, [{"type": "expense", "account": "6200",
                                 "amount": 120000, "vat": True}],
                          pay_from="cash")
    # receipt on sale 1 with WHT, partial payment of the stock bill
    r1 = cash.post_receipt(cust, [(inv1, inv1.gross_minor)], method="bank_ugx",
                           amount=(inv1.gross_minor * 94) // 100 / 1.0,
                           wht=inv1.gross_minor - (inv1.gross_minor * 94) // 100)
    pay1 = cash.post_supplier_payment(sup, [(b1, 1_000_000)], method="bank_ugx")
    cash.post_transfer("bank_ugx", "cash", 200_000, notes="t6 float")
    print(f"  fixtures: opening {opening_value:,}, 2 sales, 1 CN, 2 bills, "
          f"1 receipt, 1 payment, 1 transfer")

    today = date.today()
    dfrom, dto = today - timedelta(days=31), today

    print("== 1. Profit & loss ==")
    r = rf.profit_and_loss(dfrom, dto)
    exp_income = inv1.net_minor + inv2.net_minor + cn.net_minor  # CN negative
    check("income = sum of invoice nets (CN negative)",
          r["totals"]["income"] == exp_income,
          f"{r['totals']['income']:,} = {exp_income:,}")
    exp_cogs = inv1.cogs_minor + inv2.cogs_minor + cn.cogs_minor
    check("COGS = posted COGS (restock reversed)",
          r["totals"]["cogs"] == exp_cogs, f"{r['totals']['cogs']:,}")
    check("gross profit = income - COGS",
          r["gross_profit"] == r["totals"]["income"] - r["totals"]["cogs"])
    check("expenses include the fuel bill",
          r["totals"]["expense"] == 120000, f"{r['totals']['expense']:,}")
    check("net = gross - expenses",
          r["net_profit"] == r["gross_profit"] - r["totals"]["expense"])

    print("== 2. Balance sheet ==")
    b = rf.balance_sheet(dto)
    check("assets = liabilities + equity + result",
          b["check"] == 0,
          f"A {b['totals']['asset']:,} = L+E {b['liab_equity_total']:,}")
    check("result on the sheet equals the P&L net",
          b["result"] == r["net_profit"],
          f"{b['result']:,}")

    print("== 3. Cash flow ==")
    c = rf.cash_flow(dfrom, dto)
    check("closing = opening + in + out", c["closing"] ==
          c["opening"] + c["inflow"] + c["outflow"])
    check("closing ties to the money GL", c["tied"],
          f"{c['closing']:,} = {c['gl_closing']:,}")
    check("transfers excluded from flows",
          not any("transfer" in lbl.lower() for lbl, _v in c["flows"]))
    labels = dict(c["flows"])
    check("customer receipts and supplier payments named",
          "Receipts from customers" in labels
          and "Payments to suppliers" in labels)

    print("== 4. Inventory valuation ==")
    ties = inv.valuation_summary()
    check("subledger ties to GL per stage", all(t["tied"] for t in ties),
          "; ".join(f"{t['stage']}: {t['subledger']:,}" for t in ties))

    print("== 5. VAT summary ==")
    v = rf.vat_summary(dfrom, dto)
    exp_out = inv1.vat_minor + inv2.vat_minor + cn.vat_minor
    check("output VAT = invoice VAT (CN negative)",
          v["output_vat"] == exp_out, f"{v['output_vat']:,}")
    check("input VAT = purchase VAT", v["input_vat"] == b2.vat_minor,
          f"{v['input_vat']:,}")
    check("net = output - input", v["net_vat"] == exp_out - b2.vat_minor)
    check("every fiscalized document shows its FDN",
          all(i.efris_fdn for i, _ in v["invoices"]
              if i.efris_status == "fiscalized"))

    print("== 6. Aged AR / AP ==")
    ar = rf.aged_receivables()
    check("aged AR total = AR control account", ar["tied"],
          f"{ar['total']:,} = {ar['gl']:,}")
    check("AR buckets sum to total",
          sum(ar["bucket_totals"].values()) == ar["total"])
    ap = rf.aged_payables()
    check("aged AP total = AP control account", ap["tied"],
          f"{ap['total']:,} = {ap['gl']:,}")
    check("open bill sits in the 0-30 bucket",
          ap["bucket_totals"]["0–30"] == ap["total"])

    rows, tdr, tcr = ledger.trial_balance()
    check("trial balance still balances", tdr == tcr, f"{tdr:,}")

    admin_id = db.session.scalar(db.select(User.id).where(User.role == "admin"))
    viewer = User(username="t6_viewer", full_name="Viewer",
                  role="finance_viewer", password_hash="x")
    db.session.add(viewer)
    db.session.commit()
    viewer_id = viewer.id
    rep_id = db.session.scalar(db.select(User.id).where(User.role == "rep"))

print("== 7. Screens ==")


def login_as(c, uid):
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True


paths = ("/accounting/reports", "/accounting/reports/pl",
         "/accounting/reports/balance-sheet", "/accounting/reports/cash-flow",
         "/accounting/reports/vat", "/accounting/reports/aged-receivables",
         "/accounting/reports/aged-payables")
c = app.test_client()
login_as(c, viewer_id)
for p in paths:
    check(f"finance_viewer GET {p}", c.get(p).status_code == 200)
c2 = app.test_client()
login_as(c2, rep_id)
check("rep blocked from reports", c2.get("/accounting/reports").status_code == 403)

with app.app_context():
    db.session.execute(text("DELETE FROM user WHERE username='t6_viewer'"))
    db.session.commit()

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
