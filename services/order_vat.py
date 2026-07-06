"""Shared order/offer helpers used by the staff order desk, the offer builder and
the customer portal so every door prices and numbers a document the same way.

Two concerns live here:

* ``derive_vat`` — the single source of truth for whether a document carries VAT
  and at what rate (H2/H3/M9). Market is taken from the customer or the pricelist,
  never from a request form, so a user cannot post ``market=export`` to strip VAT.

* ``next_number`` / ``derive_number`` — safe document numbering (C3). The old
  ``count()+1`` scheme collided under cPanel's multiple Passenger processes and
  produced duplicate numbers (a unique-constraint 500 and a lost order). The
  numbers are now derived from the flushed row id, which is unique per row.
"""
from datetime import date

from sqlalchemy.exc import IntegrityError

from extensions import db
from services import settings as settings_svc


# ---------------------------------------------------------------------------
# VAT derivation (H2 / H3 / M9)
# ---------------------------------------------------------------------------
def derive_vat(pricelist, customer, market=None):
    """Return ``(vat_applicable, vat_rate)`` for a document.

    Rules:
      * market comes from ``customer.market`` or ``pricelist.market`` (never a
        request form field) unless an explicit ``market`` is passed in;
      * VAT applies only when the pricelist allows it AND the market is local;
      * the rate is the pricelist's own rate, falling back to the global setting
        and finally 18.0.
    """
    if market is None:
        market = (getattr(customer, "market", None)
                  or getattr(pricelist, "market", None)
                  or "local")

    vat_applicable = bool(getattr(pricelist, "vat_applicable", False)) and market == "local"

    rate = getattr(pricelist, "vat_rate", None)
    if rate is None:
        rate = settings_svc.get_float("vat_rate", 18.0)
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = 18.0

    return vat_applicable, rate


# ---------------------------------------------------------------------------
# Document numbering (C3)
# ---------------------------------------------------------------------------
def derive_number(prefix, row_id, on=None):
    """Build a document number from a flushed row id, e.g. ``SO-2026-00042``.

    The id is unique per row, so two documents created concurrently in separate
    processes never derive the same number (unlike the old ``count()+1``)."""
    year = (on or date.today()).year
    return f"{prefix}-{year}-{row_id:05d}"


def assign_number(obj, prefix, on=None, retries=1):
    """Flush ``obj`` to get its id, derive its number from that id, then commit.

    Retries once on an :class:`IntegrityError` (e.g. a rare number collision with
    a legacy row) by re-deriving after a rollback+flush. The caller must have
    added ``obj`` to the session already."""
    db.session.flush()
    obj.number = derive_number(prefix, obj.id, on=on)
    for attempt in range(retries + 1):
        try:
            db.session.commit()
            return obj.number
        except IntegrityError:
            db.session.rollback()
            if attempt >= retries:
                raise
            db.session.add(obj)
            db.session.flush()
            obj.number = derive_number(prefix, obj.id, on=on)
    return obj.number
