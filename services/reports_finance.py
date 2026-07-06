"""The six monthly financial reports (accounting Phase 6).

Every figure comes from POSTED journal entries only — the same rows the
trial balance proves balanced. No report keeps its own numbers; each one is
a different cut of the one ledger, and the screens show the tie-outs
(TB balanced, subledger = control account) so trust is visible, not assumed.

Sign conventions for display: income and liabilities are credit-normal and
show positive when in credit; assets and costs show positive when in debit.
"""
from datetime import date

from extensions import db
from models import (AccAccount, AccJournalEntry, AccJournalLine, AccInvoice,
                    AccPurchase, AccSupplier, Customer)
from services import ledger


def _net_by_account(date_from=None, date_to=None):
    """{account_id: debit - credit} over posted entries in the window."""
    q = (db.select(AccJournalLine.account_id,
                   db.func.coalesce(db.func.sum(AccJournalLine.debit), 0),
                   db.func.coalesce(db.func.sum(AccJournalLine.credit), 0))
         .join(AccJournalEntry, AccJournalEntry.id == AccJournalLine.entry_id)
         .where(AccJournalEntry.posted.is_(True))
         .group_by(AccJournalLine.account_id))
    if date_from:
        q = q.where(AccJournalEntry.entry_date >= date_from)
    if date_to:
        q = q.where(AccJournalEntry.entry_date <= date_to)
    return {aid: dr - cr for aid, dr, cr in db.session.execute(q).all()}


def _accounts():
    return {a.id: a for a in db.session.scalars(db.select(AccAccount)).all()}


# ---------------------------------------------------------------------------
# 1. Profit & loss — with the gross margin the pricing app never had
# ---------------------------------------------------------------------------
def profit_and_loss(date_from, date_to):
    nets = _net_by_account(date_from, date_to)
    accounts = _accounts()
    sections = {"income": [], "cogs": [], "expense": []}
    totals = {"income": 0, "cogs": 0, "expense": 0}
    for aid, net in nets.items():
        a = accounts.get(aid)
        if not a or a.type not in sections:
            continue
        display = -net if a.type == "income" else net   # credit-normal flip
        if display == 0:
            continue
        sections[a.type].append((a, display))
        totals[a.type] += display
    for rows in sections.values():
        rows.sort(key=lambda t: t[0].code)
    gross_profit = totals["income"] - totals["cogs"]
    net_profit = gross_profit - totals["expense"]
    margin_pct = (gross_profit / totals["income"] * 100) if totals["income"] else None
    return {"sections": sections, "totals": totals,
            "gross_profit": gross_profit, "net_profit": net_profit,
            "margin_pct": margin_pct}


# ---------------------------------------------------------------------------
# 2. Balance sheet
# ---------------------------------------------------------------------------
def balance_sheet(as_of):
    nets = _net_by_account(None, as_of)
    accounts = _accounts()
    sections = {"asset": [], "liability": [], "equity": []}
    totals = {"asset": 0, "liability": 0, "equity": 0}
    result = 0   # cumulative P&L result to date -> shown inside equity
    for aid, net in nets.items():
        a = accounts.get(aid)
        if not a:
            continue
        if a.type == "income":
            result += -net          # credit-normal: credits increase profit
            continue
        if a.type in ("cogs", "expense"):
            result -= net           # debit-normal: debits reduce profit
            continue
        if a.type not in sections:
            continue
        display = net if a.type == "asset" else -net
        if display == 0:
            continue
        sections[a.type].append((a, display))
        totals[a.type] += display
    for rows in sections.values():
        rows.sort(key=lambda t: t[0].code)
    check = totals["asset"] - totals["liability"] - totals["equity"] - result
    return {"sections": sections, "totals": totals,
            "result": result,
            "liab_equity_total": totals["liability"] + totals["equity"] + result,
            "check": check}


# ---------------------------------------------------------------------------
# 3. Cash flow — direct method over the four money accounts
# ---------------------------------------------------------------------------
def cash_flow(date_from, date_to):
    from services.cash_posting import MONEY_KEYS
    from services.coa import account_for
    money_ids = {account_for(k).id: k for k in MONEY_KEYS}

    opening = 0
    nets_before = _net_by_account(None, None)
    # opening = balance of money accounts BEFORE date_from
    q = (db.select(db.func.coalesce(
            db.func.sum(AccJournalLine.debit - AccJournalLine.credit), 0))
         .join(AccJournalEntry, AccJournalEntry.id == AccJournalLine.entry_id)
         .where(AccJournalEntry.posted.is_(True),
                AccJournalLine.account_id.in_(money_ids)))
    if date_from:
        opening = db.session.scalar(
            q.where(AccJournalEntry.entry_date < date_from)) or 0

    lines = db.session.execute(
        db.select(AccJournalLine, AccJournalEntry)
        .join(AccJournalEntry, AccJournalEntry.id == AccJournalLine.entry_id)
        .where(AccJournalEntry.posted.is_(True),
               AccJournalLine.account_id.in_(money_ids),
               AccJournalEntry.entry_date >= date_from,
               AccJournalEntry.entry_date <= date_to)).all()

    LABELS = {("payment", 1): "Receipts from customers",
              ("payment", -1): "Payments to suppliers",
              ("purchase", -1): "Cash purchases & expenses",
              ("invoice", 1): "Cash sales",
              ("opening", 1): "Opening balances loaded",
              ("manual", 1): "Other money in",
              ("manual", -1): "Other money out",
              ("adjustment", 1): "Adjustments in",
              ("adjustment", -1): "Adjustments out"}
    flows = {}
    for l, e in lines:
        if e.source_type == "transfer":
            continue   # internal moves net to zero across the money accounts
        sign = 1 if l.signed_amount > 0 else -1
        label = LABELS.get((e.source_type, sign),
                           "Other money in" if sign > 0 else "Other money out")
        flows[label] = flows.get(label, 0) + l.signed_amount
    inflow = sum(v for v in flows.values() if v > 0)
    outflow = sum(v for v in flows.values() if v < 0)
    closing = opening + inflow + outflow
    # tie: closing must equal the GL balance of the money accounts at date_to
    gl_closing = db.session.scalar(
        q.where(AccJournalEntry.entry_date <= date_to)) or 0
    return {"opening": opening, "flows": sorted(flows.items()),
            "inflow": inflow, "outflow": outflow, "closing": closing,
            "gl_closing": gl_closing, "tied": closing == gl_closing}


# ---------------------------------------------------------------------------
# 5. VAT summary — reconciled to EFRIS, line by line
# ---------------------------------------------------------------------------
def vat_summary(date_from, date_to):
    invs = db.session.scalars(
        db.select(AccInvoice)
        .where(AccInvoice.invoice_date >= date_from,
               AccInvoice.invoice_date <= date_to)
        .order_by(AccInvoice.id)).all()

    def ugx(minor, ccy, rate):
        if ccy == "UGX" or not rate:
            return minor
        return int(round(minor / 100 * float(rate)))

    out_rows, output_vat, fiscalized, pending = [], 0, 0, 0
    for i in invs:
        vat_ugx = ugx(i.vat_minor, i.currency, i.fx_rate)
        output_vat += vat_ugx
        if i.efris_status == "fiscalized":
            fiscalized += 1
        else:
            pending += 1
        out_rows.append((i, vat_ugx))

    # Own-shop daily sales: fiscalized at the till's own EFRIS device, so no
    # FDN here — the VAT still belongs in this period's output.
    from models import AccShopSale
    shop_sales = db.session.scalars(
        db.select(AccShopSale)
        .where(AccShopSale.sale_date >= date_from,
               AccShopSale.sale_date <= date_to,
               AccShopSale.status == "posted")
        .order_by(AccShopSale.id)).all()
    shop_vat = sum(s.vat_minor for s in shop_sales)
    output_vat += shop_vat

    purchases = db.session.scalars(
        db.select(AccPurchase)
        .where(AccPurchase.purchase_date >= date_from,
               AccPurchase.purchase_date <= date_to,
               AccPurchase.status == "posted")
        .order_by(AccPurchase.id)).all()
    input_vat = sum(ugx(p.vat_minor, p.currency, p.fx_rate) for p in purchases)

    return {"invoices": out_rows, "purchases": purchases,
            "shop_sales": shop_sales, "shop_vat": shop_vat,
            "output_vat": output_vat, "input_vat": input_vat,
            "net_vat": output_vat - input_vat,
            "fiscalized": fiscalized, "pending": pending}


# ---------------------------------------------------------------------------
# 6. Aged receivables / payables — tied to the control accounts
# ---------------------------------------------------------------------------
BUCKETS = [(0, 30, "0–30"), (31, 60, "31–60"), (61, 90, "61–90"),
           (91, 100000, "90+")]


def _bucket(days):
    for lo, hi, label in BUCKETS:
        if lo <= days <= hi:
            return label
    return BUCKETS[-1][2]


def aged_receivables(as_of=None):
    as_of = as_of or date.today()
    open_invs = [i for i in db.session.scalars(
        db.select(AccInvoice).where(AccInvoice.kind == "invoice",
                                    AccInvoice.status == "posted")).all()
        if i.open_minor > 0]
    by_customer, bucket_totals = {}, {b[2]: 0 for b in BUCKETS}
    total = 0
    for i in open_invs:
        ugx_open = i.open_minor if i.currency == "UGX" or not i.fx_rate \
            else int(round(i.open_minor / 100 * float(i.fx_rate)))
        b = _bucket(max((as_of - i.invoice_date).days, 0))
        row = by_customer.setdefault(
            i.customer_id, {"name": i.buyer_name or "?", "total": 0,
                            **{bb[2]: 0 for bb in BUCKETS}, "invoices": []})
        row[b] += ugx_open
        row["total"] += ugx_open
        row["invoices"].append(i)
        bucket_totals[b] += ugx_open
        total += ugx_open
    from services.coa import account_for
    gl = ledger.account_balances().get(account_for("ar_control").id, 0)
    return {"rows": sorted(by_customer.values(), key=lambda r: -r["total"]),
            "bucket_totals": bucket_totals, "total": total,
            "gl": gl, "tied": total == gl}


def aged_payables(as_of=None):
    as_of = as_of or date.today()
    open_bills = [p for p in db.session.scalars(
        db.select(AccPurchase).where(AccPurchase.status == "posted")).all()
        if p.on_account and p.gross_minor > (p.paid_minor or 0)]
    by_supplier, bucket_totals = {}, {b[2]: 0 for b in BUCKETS}
    total = 0
    for p in open_bills:
        open_ugx = p.gross_ugx_minor - (p.paid_minor or 0)
        b = _bucket(max((as_of - p.purchase_date).days, 0))
        row = by_supplier.setdefault(
            p.supplier_id, {"name": p.supplier.name, "total": 0,
                            **{bb[2]: 0 for bb in BUCKETS}, "bills": []})
        row[b] += open_ugx
        row["total"] += open_ugx
        row["bills"].append(p)
        bucket_totals[b] += open_ugx
        total += open_ugx
    from services.coa import account_for
    gl = -ledger.account_balances().get(account_for("ap_control").id, 0)
    return {"rows": sorted(by_supplier.values(), key=lambda r: -r["total"]),
            "bucket_totals": bucket_totals, "total": total,
            "gl": gl, "tied": total == gl}
