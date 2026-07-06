"""The double-entry ledger's single writer. Every journal entry — manual or
automatic — is created and posted here and nowhere else, so the balance rules,
money conversion, and append-only behaviour cannot drift between the web UI,
the API, and future posting rules (sales, purchases, production).

Money boundary
--------------
The app stores prices as Numeric/float; the ledger stores INTEGER minor units
(UGX whole shillings, USD cents). ``to_minor`` is the one conversion point,
using the same ROUND_HALF_UP rule as services/currency.quantize, so the ledger
and the printed documents always agree.

Posting protocol (two-phase, trigger-backed)
--------------------------------------------
1. Insert the entry with posted=0 and its lines (drafts are invisible to every
   report and the trial balance).
2. Flip posted to 1 inside the same transaction. The SQLite trigger
   ``acc_entry_post_check`` (migrations/acc_001_triggers.sql) aborts the flip
   unless debits equal credits, at least two lines exist, and every line is
   single-sided and non-negative. Application checks run first for friendly
   errors; the trigger is the physical guarantee.

Append-only: posted entries and their lines refuse UPDATE and DELETE at the
database level. Corrections go through ``reverse_entry``.
"""
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP

from extensions import db
from models import AccAccount, AccJournalEntry, AccJournalLine
from services import order_vat
from services.coa import account_for

# Minor-unit scale per currency: UGX has no minor unit, USD has cents.
_MINOR_SCALE = {"UGX": 0, "USD": 2}


class LedgerError(ValueError):
    """A posting violated a ledger rule (unbalanced, bad line, bad account)."""


def to_minor(amount, currency="UGX"):
    """Convert a Numeric/float/str amount to integer minor units.

    UGX 12,345.6 -> 12346 shillings; USD 10.505 -> 1051 cents (ROUND_HALF_UP,
    matching services/currency.quantize). None -> 0."""
    if amount is None:
        return 0
    scale = _MINOR_SCALE.get(currency, 2)
    q = Decimal(str(amount)).scaleb(scale).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(q)


def from_minor(minor, currency="UGX"):
    """Integer minor units back to a Decimal major amount (display only)."""
    scale = _MINOR_SCALE.get(currency, 2)
    return Decimal(minor or 0).scaleb(-scale)


def _resolve_account(ref):
    """Accept an AccAccount, an id, a code, or a system_key."""
    if isinstance(ref, AccAccount):
        return ref
    if isinstance(ref, int):
        acct = db.session.get(AccAccount, ref)
        if acct:
            return acct
        raise LedgerError(f"No account with id {ref}.")
    if isinstance(ref, str):
        acct = db.session.scalar(db.select(AccAccount).where(AccAccount.code == ref))
        if acct is None:
            acct = db.session.scalar(
                db.select(AccAccount).where(AccAccount.system_key == ref))
        if acct:
            return acct
        raise LedgerError(f"No account with code or key '{ref}'.")
    raise LedgerError(f"Cannot resolve account from {ref!r}.")


def post_entry(entry_date, memo, lines, source_type="manual", source_id=None,
               channel=None, user_id=None, reversal_of_id=None):
    """Create AND post one balanced journal entry in a single transaction.

    ``lines`` is a list of dicts:
        {"account": <AccAccount|id|code|system_key>,
         "debit": <int UGX shillings> or "credit": <int>,
         "orig_currency": ..., "orig_amount_minor": ..., "fx_rate": ...,
         "customer_id": ..., "line_memo": ...}
    Amounts must already be integer minor units — callers convert with
    ``to_minor`` so the rounding decision stays visible at the call site.

    Returns the posted entry. Raises LedgerError before any write when the
    lines are invalid; rolls back if the database trigger objects."""
    if not isinstance(entry_date, date):
        raise LedgerError("entry_date must be a date.")
    if len(lines) < 2:
        raise LedgerError("A journal entry needs at least two lines.")

    prepared, total_dr, total_cr = [], 0, 0
    for i, ln in enumerate(lines):
        acct = _resolve_account(ln.get("account"))
        if not acct.is_postable or not acct.active:
            raise LedgerError(f"Line {i + 1}: account {acct.code} {acct.name} "
                              "is not postable.")
        dr = int(ln.get("debit") or 0)
        cr = int(ln.get("credit") or 0)
        if dr < 0 or cr < 0:
            raise LedgerError(f"Line {i + 1}: negative amounts are not allowed; "
                              "swap the side instead.")
        if (dr > 0) == (cr > 0):   # both set or both zero
            raise LedgerError(f"Line {i + 1}: exactly one of debit/credit must "
                              "be greater than zero.")
        total_dr += dr
        total_cr += cr
        prepared.append((acct, dr, cr, ln))

    if total_dr != total_cr:
        raise LedgerError(
            f"Entry does not balance: debits {total_dr:,} != credits {total_cr:,} "
            "(UGX shillings).")

    entry = AccJournalEntry(
        entry_date=entry_date, memo=(memo or "").strip() or None,
        source_type=source_type, source_id=source_id, channel=channel,
        created_by_id=user_id, reversal_of_id=reversal_of_id, posted=False,
    )
    db.session.add(entry)
    try:
        # Number from the flushed id (same collision-safe scheme as SO/OF).
        db.session.flush()
        entry.entry_no = order_vat.derive_number("JE", entry.id, on=entry_date)
        for acct, dr, cr, ln in prepared:
            db.session.add(AccJournalLine(
                entry_id=entry.id, account_id=acct.id, debit=dr, credit=cr,
                orig_currency=ln.get("orig_currency"),
                orig_amount_minor=ln.get("orig_amount_minor"),
                fx_rate=ln.get("fx_rate"),
                customer_id=ln.get("customer_id"),
                line_memo=ln.get("line_memo"),
            ))
        db.session.flush()
        # Phase 2 of the two-phase post: the trigger checks balance here.
        entry.posted = True
        entry.posted_at = datetime.utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return entry


def reverse_entry(entry, user_id=None, memo=None, on=None):
    """Post the exact mirror of a posted entry (the only correction path).

    Every debit becomes a credit and vice versa. The original stays untouched.
    Refuses to reverse drafts, refuses to reverse twice."""
    if not entry.posted:
        raise LedgerError("Only posted entries reverse; delete the draft instead.")
    already = [r for r in entry.reversals if r.posted]
    if already:
        raise LedgerError(f"Entry {entry.entry_no} is already reversed by "
                          f"{already[0].entry_no}.")
    lines = [{
        "account": l.account_id, "debit": l.credit, "credit": l.debit,
        "orig_currency": l.orig_currency, "orig_amount_minor": l.orig_amount_minor,
        "fx_rate": l.fx_rate, "customer_id": l.customer_id,
        "line_memo": l.line_memo,
    } for l in entry.lines]
    return post_entry(
        entry_date=(on or date.today()),
        memo=memo or f"Reversal of {entry.entry_no}",
        lines=lines, source_type="adjustment", source_id=entry.id,
        channel=entry.channel, user_id=user_id, reversal_of_id=entry.id)


def account_balances(as_of=None, up_to=None):
    """{account_id: net signed balance (debit positive)} over posted entries.

    ``as_of`` limits by entry_date (inclusive) for the trial balance."""
    q = (db.select(AccJournalLine.account_id,
                   db.func.coalesce(db.func.sum(AccJournalLine.debit), 0),
                   db.func.coalesce(db.func.sum(AccJournalLine.credit), 0))
         .join(AccJournalEntry, AccJournalEntry.id == AccJournalLine.entry_id)
         .where(AccJournalEntry.posted.is_(True))
         .group_by(AccJournalLine.account_id))
    if as_of:
        q = q.where(AccJournalEntry.entry_date <= as_of)
    return {aid: (dr - cr) for aid, dr, cr in db.session.execute(q).all()}


def trial_balance(as_of=None):
    """Rows for the trial balance: (account, debit_balance, credit_balance),
    ordered by code, accounts with zero balance skipped. Also returns totals.

    Debit-normal accounts show a positive net as a debit balance; a negative
    net flips to the credit column (and vice versa), which is exactly how an
    accountant expects a TB to read."""
    balances = account_balances(as_of=as_of)
    accounts = db.session.scalars(
        db.select(AccAccount).order_by(AccAccount.code)).all()
    rows, total_dr, total_cr = [], 0, 0
    for a in accounts:
        net = balances.get(a.id, 0)
        if net == 0:
            continue
        dr = net if net > 0 else 0
        cr = -net if net < 0 else 0
        rows.append((a, dr, cr))
        total_dr += dr
        total_cr += cr
    return rows, total_dr, total_cr
