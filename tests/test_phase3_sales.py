"""Phase 3 acceptance test — sales posting + EFRIS.

Run against a COPY of the live database:

    rm -f /tmp/p3s.db*; cp instance/pricing.db /tmp/p3s.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/p3s.db COOKIE_INSECURE=1 \
    EFRIS_MODE=simulate python3 tests/test_phase3_sales.py

Proves:
  1.  Completing a sale posts ONE balanced journal: AR gross, revenue net by
      class, VAT output, COGS at weighted average, inventory credit.
  2.  The invoice reconciles to the order's own total to the minor unit.
  3.  Lines without valued stock post revenue but skip COGS, flagged.
  4.  EFRIS simulate: FDN/verification/QR + full response stored, queue done.
  5.  EFRIS failure: ledger unaffected, invoice pending, queue row with
      backoff; retry drains the queue once URA "recovers".
  6.  Credit note: paired fiscal reversal — journal mirrored, goods restocked
      at the exact sale value, own EFRIS submission; second CN refused.
  7.  Cancel of an invoiced order is blocked (route).
  8.  Posted invoices refuse edits/deletes at the database level.
  9.  Screens render; clerk cannot raise credit notes; USD order books UGX
      at the stamped rate with original-currency legs.
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

import json
import re
from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app import app
from extensions import db
from models import (AccInvoice, AccItem, AccInvMovement, SalesOrder,
                    SalesOrderLine, Customer, User, ExchangeRate)
from services import ledger, efris
from services import inventory_costing as inv
from services import sale_posting as sp

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def apply_sql(*names):
    raw = db.engine.raw_connection()
    for n in names:
        raw.executescript(open(os.path.join(
            os.path.dirname(__file__), "..", "migrations", n)).read())
    raw.commit()
    raw.close()


def make_order(customer, items, currency="UGX", rate=None, vat=True):
    o = SalesOrder(customer_id=customer.id, currency=currency,
                   market=customer.market, vat_applicable=vat, vat_rate=18.0,
                   exchange_rate_value=rate, status="in_fulfillment",
                   order_date=date.today(), stock_deducted=True)
    db.session.add(o)
    db.session.flush()
    o.number = f"SO-TEST-{o.id:05d}"
    for i, (item, qty, price, vatable) in enumerate(items):
        db.session.add(SalesOrderLine(
            order_id=o.id, product_id=(item.product_id if item else None),
            description=(item.name if item else "UNVALUED TEST LINE"),
            quantity=qty, fulfilled_qty=qty, unit_price=price,
            vat_applicable=vatable, sort_order=i))
    db.session.commit()
    return o


with app.app_context():
    apply_sql("acc_001_triggers.sql", "acc_002_inventory.sql", "acc_003_invoices.sql")

    print("== setup: opening stock on the test copy ==")
    entry, n_open, total_open = inv.load_opening_stock()
    check(f"opening loaded ({n_open} items, {total_open:,})", n_open > 0)
    valued = db.session.scalar(db.select(AccItem).where(
        AccItem.value_on_hand > 0, AccItem.qty_on_hand >= 20))
    unvalued = db.session.scalar(db.select(AccItem).where(
        AccItem.value_on_hand == 0, AccItem.product_id.isnot(None)))
    cust = db.session.scalar(db.select(Customer).where(
        Customer.market == "local", Customer.archived.is_(False)))
    check("fixtures found", valued is not None and unvalued is not None
          and cust is not None, f"valued: {valued.name}")

    print("== 1/2/3. Sale posts: revenue + VAT + AR + COGS, reconciled ==")
    pre_qty, pre_val = valued.qty_on_hand, valued.value_on_hand
    order = make_order(cust, [(valued, 10, 20000, True),
                              (unvalued, 5, 15000, False)])
    invoice = sp.post_sale(order)
    je = invoice.journal_entry
    check("journal posted and balanced", je.posted and je.is_balanced,
          f"{je.entry_no}: {je.total_debit:,}")
    order_gross = ledger.to_minor(order.total)
    check("invoice gross == order total (reconciliation)",
          invoice.gross_minor == order_gross,
          f"{invoice.gross_minor:,} == {order_gross:,}")
    vat_expect = ledger.to_minor(10 * 20000 * 0.18)
    check("VAT = 18% of vatable net only", invoice.vat_minor == vat_expect,
          f"{invoice.vat_minor:,}")
    avg = pre_val / pre_qty
    cogs_expect = round(10 * avg)
    check("COGS = 10 x weighted average of the valued item",
          invoice.cogs_minor == cogs_expect,
          f"{invoice.cogs_minor:,} (avg {avg:,.2f})")
    check("valued item reduced", valued.qty_on_hand == pre_qty - 10
          and valued.value_on_hand == pre_val - cogs_expect)
    check("unvalued line flagged, not costed",
          invoice.cogs_skipped and "UNVALUED" not in (invoice.cogs_skipped or "")
          and unvalued.name in invoice.cogs_skipped, invoice.cogs_skipped)
    ties = inv.valuation_summary()
    check("inventory subledger still ties to GL", all(t["tied"] for t in ties))
    rows, tdr, tcr = ledger.trial_balance()
    check("trial balance balances", tdr == tcr, f"{tdr:,}")

    print("== 4. EFRIS success path (simulate) ==")
    ok = efris.try_fiscalize(invoice)
    check("fiscalized", ok and invoice.efris_status == "fiscalized")
    check("FDN + verification + QR + full response stored",
          bool(invoice.efris_fdn and invoice.efris_verification_code
               and invoice.efris_qr and invoice.efris_response),
          f"FDN {invoice.efris_fdn}")
    check("response is valid JSON",
          json.loads(invoice.efris_response).get("simulated") is True)
    q = db.session.execute(text(
        "SELECT status FROM acc_efris_queue WHERE invoice_id=:i"),
        {"i": invoice.id}).scalar()
    check("queue row done", q == "done")

    print("== 5. EFRIS failure + retry (simulate_fail -> simulate) ==")
    os.environ["EFRIS_MODE"] = "simulate_fail"
    order2 = make_order(cust, [(valued, 5, 20000, True)])
    inv2 = sp.post_sale(order2)
    check("sale posted despite URA down",
          inv2.journal_entry.posted and inv2.efris_status == "pending")
    ok = efris.try_fiscalize(inv2)
    row = db.session.execute(text(
        "SELECT status, attempts, last_error FROM acc_efris_queue "
        "WHERE invoice_id=:i"), {"i": inv2.id}).fetchone()
    check("failure recorded, queued with backoff",
          not ok and inv2.efris_status == "pending" and row[0] == "queued"
          and row[1] == 1, str(row[2]))
    os.environ["EFRIS_MODE"] = "simulate"
    db.session.execute(text(
        "UPDATE acc_efris_queue SET next_attempt_at=datetime('now','-1 minute') "
        "WHERE invoice_id=:i"), {"i": inv2.id})
    db.session.commit()
    got, bad = efris.process_queue()
    db.session.refresh(inv2)
    check("queue drained after URA recovery",
          got >= 1 and inv2.efris_status == "fiscalized", f"FDN {inv2.efris_fdn}")

    print("== 6. Credit note ==")
    qty_before, val_before = valued.qty_on_hand, valued.value_on_hand
    cn = sp.post_credit_note(invoice, reason="TEST return", restock=True)
    check("credit note posted + fiscal submission",
          cn.kind == "credit_note" and cn.reverses_invoice_id == invoice.id
          and cn.journal_entry.posted, cn.invoice_no)
    check("journal mirrored (pair nets zero)",
          cn.journal_entry.reversal_of_id == invoice.journal_entry_id
          and cn.gross_minor == -invoice.gross_minor)
    check("goods restocked at exact sale value",
          valued.qty_on_hand == qty_before + 10
          and valued.value_on_hand == val_before + invoice.cogs_minor)
    try:
        sp.post_credit_note(invoice)
        check("second credit note refused", False)
    except sp.SalePostingError:
        check("second credit note refused", True)
    efris.try_fiscalize(cn, action="credit_note")
    check("credit note fiscalized", cn.efris_status == "fiscalized")

    print("== 8. Invoice immutability (raw SQL) ==")
    for label, stmt in [
            ("gross edit refused",
             f"UPDATE acc_invoice SET gross_minor=1 WHERE id={invoice.id}"),
            ("DELETE refused", f"DELETE FROM acc_invoice WHERE id={invoice.id}")]:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            check(label, False)
        except DatabaseError as e:
            db.session.rollback()
            check(label, True, str(e.orig)[:55])

    print("== 9b. USD order books UGX at the stamped rate ==")
    usd_cust = db.session.scalar(db.select(Customer).where(
        Customer.market == "export")) or cust
    o3 = make_order(usd_cust, [(valued, 2, 10.00, False)],
                    currency="USD", rate=3700, vat=False)
    inv3 = sp.post_sale(o3)
    ar_line = next(l for l in inv3.journal_entry.lines
                   if l.account.system_key == "ar_control")
    check("USD gross -> UGX at rate",
          ar_line.debit == round(inv3.gross_minor / 100 * 3700)
          and ar_line.orig_currency == "USD"
          and ar_line.orig_amount_minor == inv3.gross_minor,
          f"USD {inv3.gross_minor/100:,.2f} -> UGX {ar_line.debit:,}")
    check("export revenue account used",
          any(l.account.system_key == "rev_export" and l.credit
              for l in inv3.journal_entry.lines))

    admin_id = db.session.scalar(db.select(User.id).where(User.role == "admin"))
    clerk = User(username="t3_clerk", full_name="Clerk", role="finance_clerk",
                 password_hash="x")
    db.session.add(clerk)
    db.session.commit()
    # order2 carries a live (non-credited) invoice — the cancel guard target.
    clerk_id, inv_id, order_id = clerk.id, invoice.id, order2.id

print("== 7/9. Routes ==")


def login_as(c, uid):
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True


c = app.test_client()
login_as(c, admin_id)
for p in ("/accounting/invoices", f"/accounting/invoices/{inv_id}"):
    r = c.get(p)
    check(f"admin GET {p}", r.status_code == 200, str(r.status_code))
page = c.get("/accounting/invoices").get_data(as_text=True)
token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
r = c.post(f"/orders/{order_id}/cancel", data={"csrf_token": token},
           follow_redirects=True)
body = r.get_data(as_text=True)
check("cancel of invoiced order blocked (route)",
      "Cancel is blocked" in body or "credit note" in body.lower())
c2 = app.test_client()
login_as(c2, clerk_id)
r = c2.post(f"/accounting/invoices/{inv_id}/credit-note",
            data={"reason": "x"})
check("clerk cannot raise credit notes", r.status_code in (400, 403),
      str(r.status_code))

with app.app_context():
    db.session.execute(text("DELETE FROM user WHERE username='t3_clerk'"))
    db.session.commit()

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
