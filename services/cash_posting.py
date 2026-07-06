"""Cash & bank postings (accounting Phase 5).

Customer receipt (each rule commented):
    DR 1000/1010/1020/1050  money received            } what arrived
    DR 1320 WHT Receivable  the 6% a designated agent withheld
    CR 1100 AR              invoices settled (money + WHT)
    DR/CR 7000 FX           difference when a USD invoice settles at a
                            different rate than it was booked

Supplier payment:
    DR 2000 AP  /  CR money account            (UGX only in this version)

Transfer between money accounts:
    DR target / CR source — a plain journal, source_type 'transfer'.

Allocation discipline: every receipt/payment allocates fully against open
documents — no floating on-account balances in this version (a deposit
without an invoice is a Phase 6+ decision, flagged in the notes). Partial
settlement of any document is fine; over-settlement refuses.
"""
from datetime import date

from extensions import db
from models import (AccReceipt, AccReceiptAllocation, AccSupplierPayment,
                    AccPaymentAllocation, AccInvoice, AccPurchase, AccSupplier,
                    AccReconciliation, AccReconLine, AccJournalLine,
                    AccJournalEntry)
from services import ledger

MONEY_KEYS = ("cash", "bank_ugx", "bank_usd", "momo")


class CashError(ValueError):
    pass


def post_receipt(customer, allocations, method="cash", amount=None,
                 wht=0, currency="UGX", fx_rate=None, receipt_date=None,
                 notes=None, user_id=None):
    """One customer receipt settling one or more invoices, one transaction.

    ``allocations``: [(invoice, doc_minor_to_settle), ...] — doc currency.
    ``amount``: money received, MAJOR units of ``currency``.
    ``wht``: withheld tax, MAJOR units (0 for non-agents).
    USD receipts need ``fx_rate`` (UGX per USD on the day the money landed).
    """
    if method not in AccReceipt.METHOD_ACCOUNTS:
        raise CashError("Unknown receipt method.")
    if currency != "UGX" and not fx_rate:
        raise CashError(f"A {currency} receipt needs the day's exchange rate.")
    if method == "bank_usd" and currency != "USD":
        raise CashError("The USD bank account takes USD receipts.")
    if not allocations:
        raise CashError("Allocate the receipt to at least one invoice.")

    amount_minor = ledger.to_minor(amount or 0, currency)
    wht_minor = ledger.to_minor(wht or 0, currency)
    if amount_minor <= 0:
        raise CashError("Amount received must be positive.")
    settled_minor = amount_minor + wht_minor

    total_alloc = 0
    for invoice, alloc_minor in allocations:
        alloc_minor = int(alloc_minor)
        if invoice.kind != "invoice" or invoice.status == "credited":
            raise CashError(f"{invoice.invoice_no} is not payable.")
        if invoice.customer_id != customer.id:
            raise CashError(f"{invoice.invoice_no} belongs to another customer.")
        if invoice.currency != currency:
            raise CashError(f"{invoice.invoice_no} is a {invoice.currency} "
                            f"document; settle it in {invoice.currency}.")
        if alloc_minor <= 0:
            raise CashError("Allocations must be positive.")
        if alloc_minor > invoice.open_minor:
            raise CashError(
                f"{invoice.invoice_no}: allocating {alloc_minor:,} exceeds the "
                f"open balance {invoice.open_minor:,}.")
        total_alloc += alloc_minor
    if total_alloc != settled_minor:
        raise CashError(
            f"Allocations ({total_alloc:,}) must equal money received plus "
            f"WHT ({settled_minor:,}) — no floating balances.")

    # ---- UGX legs ----
    if currency == "UGX":
        money_ugx, wht_ugx = amount_minor, wht_minor
        ar_ugx = settled_minor
        fx_ugx = 0
    else:
        # money lands at the RECEIPT-day rate; AR was booked at each
        # invoice's own rate — the difference is realized FX gain/loss.
        money_ugx = int(round(amount_minor / 100 * float(fx_rate)))
        wht_ugx = int(round(wht_minor / 100 * float(fx_rate)))
        ar_ugx = sum(int(round(a / 100 * float(inv_.fx_rate or fx_rate)))
                     for inv_, a in allocations)
        fx_ugx = ar_ugx - (money_ugx + wht_ugx)   # + = loss (DR), - = gain (CR)

    fx_doc = {"orig_currency": currency, "fx_rate": fx_rate} \
        if currency != "UGX" else {}
    jlines = [{"account": AccReceipt.METHOD_ACCOUNTS[method],
               "debit": money_ugx,
               "orig_amount_minor": amount_minor if currency != "UGX" else None,
               "line_memo": f"Receipt — {customer.name}", **fx_doc}]
    if wht_ugx:
        jlines.append({"account": "wht_recv", "debit": wht_ugx,
                       "line_memo": "6% withholding tax credit"})
    if fx_ugx > 0:
        jlines.append({"account": "fx", "debit": fx_ugx,
                       "line_memo": "Realized FX loss on settlement"})
    elif fx_ugx < 0:
        jlines.append({"account": "fx", "credit": -fx_ugx,
                       "line_memo": "Realized FX gain on settlement"})
    jlines.append({"account": "ar_control", "credit": ar_ugx,
                   "customer_id": customer.id,
                   "line_memo": "Invoices settled"})

    entry = ledger.post_entry(
        entry_date=(receipt_date or date.today()),
        memo=f"Receipt — {customer.name}",
        lines=jlines, source_type="payment", user_id=user_id)

    r = AccReceipt(customer_id=customer.id,
                   receipt_date=(receipt_date or date.today()),
                   method=method, currency=currency, fx_rate=fx_rate,
                   amount_minor=amount_minor, wht_minor=wht_minor,
                   journal_entry_id=entry.id, status="draft",
                   notes=notes, created_by_id=user_id)
    db.session.add(r)
    db.session.flush()
    from services.order_vat import derive_number
    r.receipt_no = derive_number("RCT", r.id)
    for invoice, alloc_minor in allocations:
        db.session.add(AccReceiptAllocation(
            receipt_id=r.id, invoice_id=invoice.id, amount_minor=int(alloc_minor)))
        invoice.paid_minor = (invoice.paid_minor or 0) + int(alloc_minor)
    db.session.flush()
    r.status = "posted"
    db.session.commit()
    return r


def reverse_receipt(receipt, user_id=None, reason=None):
    """Mirror the journal and roll the invoice paid balances back."""
    if receipt.status != "posted":
        raise CashError(f"{receipt.receipt_no} is already reversed.")
    rev = ledger.reverse_entry(receipt.journal_entry, user_id=user_id,
                               memo=f"Reversal of {receipt.receipt_no}"
                                    + (f" — {reason}" if reason else ""))
    for al in receipt.allocations:
        al.invoice.paid_minor = max((al.invoice.paid_minor or 0) - al.amount_minor, 0)
    receipt.status = "reversed"
    receipt.reversal_entry_id = rev.id
    db.session.commit()
    return rev


def post_supplier_payment(supplier, allocations, method="bank_ugx",
                          payment_date=None, notes=None, user_id=None):
    """Pay open purchases. UGX only in this version (USD bills are rare on the
    supply side; extend when one exists). One transaction.

    ``allocations``: [(purchase, ugx_minor), ...]"""
    if method not in ("cash", "bank_ugx", "momo"):
        raise CashError("Pay suppliers from cash, UGX bank, or mobile money.")
    if not allocations:
        raise CashError("Allocate the payment to at least one purchase.")
    total = 0
    for p, a in allocations:
        a = int(a)
        if p.supplier_id != supplier.id:
            raise CashError(f"{p.purchase_no} belongs to another supplier.")
        if p.status != "posted" or not p.on_account:
            raise CashError(f"{p.purchase_no} is not an open payable.")
        if p.currency != "UGX":
            raise CashError(f"{p.purchase_no} is a {p.currency} bill — "
                            "foreign-currency supplier payments arrive with "
                            "the first real USD bill.")
        open_ = p.gross_minor - (p.paid_minor or 0)
        if a <= 0 or a > open_:
            raise CashError(f"{p.purchase_no}: allocation exceeds the open "
                            f"balance {open_:,}.")
        total += a

    entry = ledger.post_entry(
        entry_date=(payment_date or date.today()),
        memo=f"Payment — {supplier.name}",
        lines=[{"account": "ap_control", "debit": total,
                "line_memo": f"{supplier.name} bills settled"},
               {"account": AccReceipt.METHOD_ACCOUNTS[method], "credit": total,
                "line_memo": f"Payment — {supplier.name}"}],
        source_type="payment", user_id=user_id)

    pay = AccSupplierPayment(supplier_id=supplier.id,
                             payment_date=(payment_date or date.today()),
                             method=method, amount_minor=total,
                             journal_entry_id=entry.id, status="draft",
                             notes=notes, created_by_id=user_id)
    db.session.add(pay)
    db.session.flush()
    from services.order_vat import derive_number
    pay.payment_no = derive_number("PAY", pay.id)
    for p, a in allocations:
        db.session.add(AccPaymentAllocation(payment_id=pay.id, purchase_id=p.id,
                                            amount_minor=int(a)))
        p.paid_minor = (p.paid_minor or 0) + int(a)
    db.session.flush()
    pay.status = "posted"
    db.session.commit()
    return pay


def post_transfer(from_key, to_key, amount, on=None, notes=None, user_id=None):
    """Move money between the four money accounts. Amount in UGX major units."""
    if from_key not in MONEY_KEYS or to_key not in MONEY_KEYS:
        raise CashError("Transfers run between the cash/bank/MoMo accounts.")
    if from_key == to_key:
        raise CashError("Pick two different accounts.")
    if from_key == "bank_usd" or to_key == "bank_usd":
        raise CashError("USD transfers arrive with the first real USD movement "
                        "— they need a rate and an FX leg.")
    minor = ledger.to_minor(amount or 0, "UGX")
    if minor <= 0:
        raise CashError("Transfer amount must be positive.")
    return ledger.post_entry(
        entry_date=(on or date.today()),
        memo=notes or "Transfer between money accounts",
        lines=[{"account": to_key, "debit": minor},
               {"account": from_key, "credit": minor}],
        source_type="transfer", user_id=user_id)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def money_balances():
    """{system_key: (account, balance_minor)} for the four money accounts."""
    from services.coa import account_for
    balances = ledger.account_balances()
    out = {}
    for key in MONEY_KEYS:
        acct = account_for(key)
        out[key] = (acct, balances.get(acct.id, 0))
    return out


def cleared_line_ids(account_id):
    """Journal-line ids already cleared by any CLOSED reconciliation."""
    return set(db.session.scalars(
        db.select(AccReconLine.journal_line_id)
        .join(AccReconciliation, AccReconciliation.id == AccReconLine.recon_id)
        .where(AccReconciliation.account_id == account_id,
               AccReconciliation.status == "closed")).all())


def uncleared_lines(account_id):
    """Posted journal lines on a money account not yet cleared."""
    done = cleared_line_ids(account_id)
    q = (db.select(AccJournalLine)
         .join(AccJournalEntry, AccJournalEntry.id == AccJournalLine.entry_id)
         .where(AccJournalLine.account_id == account_id,
                AccJournalEntry.posted.is_(True))
         .order_by(AccJournalLine.id))
    return [l for l in db.session.scalars(q).all() if l.id not in done]


def close_reconciliation(account, statement_date, statement_balance_minor,
                         line_ids, user_id=None):
    """Tick the given lines as on-statement; close only when cleared-total
    equals the statement balance. Lines themselves are never touched."""
    prior = cleared_line_ids(account.id)
    lines = db.session.scalars(
        db.select(AccJournalLine).where(AccJournalLine.id.in_(line_ids))).all() \
        if line_ids else []
    for l in lines:
        if l.account_id != account.id:
            raise CashError("A ticked line belongs to another account.")
        if l.id in prior:
            raise CashError("A ticked line is already cleared by an earlier "
                            "reconciliation.")
    prior_total = db.session.scalar(
        db.select(db.func.coalesce(
            db.func.sum(AccJournalLine.debit - AccJournalLine.credit), 0))
        .where(AccJournalLine.id.in_(prior))) if prior else 0
    new_total = sum(l.signed_amount for l in lines)
    cleared_balance = (prior_total or 0) + new_total
    diff = int(statement_balance_minor) - cleared_balance

    recon = AccReconciliation(
        account_id=account.id, statement_date=statement_date,
        statement_balance_minor=int(statement_balance_minor),
        status=("closed" if diff == 0 else "open"),
        created_by_id=user_id)
    if diff == 0:
        from datetime import datetime as _dt
        recon.closed_at = _dt.utcnow()
    db.session.add(recon)
    db.session.flush()
    for l in lines:
        db.session.add(AccReconLine(recon_id=recon.id, journal_line_id=l.id))
    db.session.commit()
    return recon, diff
