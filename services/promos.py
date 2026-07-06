"""Temporary promotional prices on pricelist lines.

A promo ends on the earlier of its end date or its quantity cap, after which the
normal price returns. Quantity sold is counted live from confirmed orders on the
same pricelist for the same product since the promo started.
"""
from datetime import date

from extensions import db
from models import PromoPrice, SalesOrder, SalesOrderLine

CONFIRMED = ("placed", "in_fulfillment", "pending", "ready_for_dispatch",
             "out_for_delivery", "dispatched", "delivered", "fulfilled")


def promo_start(promo):
    """The date the promo window opened.

    Do not silently default a null start_date to *today* (that would make the
    window drift forward every day and never count past sales). Fall back to the
    creation date instead, so a promo with no explicit start counts from when it
    was created (M10).
    """
    if promo.start_date:
        return promo.start_date
    if getattr(promo, "created_at", None):
        return promo.created_at.date()
    return date.today()


def qty_sold(promo):
    """Quantity of this product sold at the promo price on this pricelist since
    the promo started. Counts only lines that were actually sold at (or below)
    the promo amount, so full-price sales through other tiers do not burn the
    cap (M10)."""
    line = promo.line
    if line is None:
        return 0.0
    promo_amt = float(promo.promo_amount or 0)
    total = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(SalesOrderLine.quantity), 0.0))
        .join(SalesOrder, SalesOrder.id == SalesOrderLine.order_id)
        .where(SalesOrderLine.product_id == line.product_id,
               SalesOrder.source_pricelist_id == line.pricelist_id,
               SalesOrder.status.in_(CONFIRMED),
               # only lines priced at the promo amount count toward the cap
               SalesOrderLine.unit_price <= promo_amt,
               SalesOrder.order_date >= promo_start(promo)))
    return float(total or 0.0)


def qty_remaining(promo):
    """Units still available at the promo price before the cap is hit, or None
    when the promo has no quantity cap (unlimited)."""
    if promo.qty_cap is None:
        return None
    return max(0.0, float(promo.qty_cap) - qty_sold(promo))


def promo_qty_allowed(promo, requested_qty):
    """How many of ``requested_qty`` units may be sold at the promo price.

    Caps the promo quantity WITHIN a single order (M10): only the units still
    under the cap get the promo price; the excess is left for the caller to
    price normally. Returns ``requested_qty`` unchanged when the promo has no
    cap. The orders agent calls this to split a line into promo + normal parts.
    """
    req = float(requested_qty or 0)
    if req <= 0:
        return 0.0
    rem = qty_remaining(promo)
    if rem is None:
        return req
    return min(req, rem)


def is_active(promo, on=None):
    on = on or date.today()
    if promo.status != "active":
        return False
    start = promo_start(promo)
    if start and on < start:
        return False
    if promo.end_date and on > promo.end_date:
        return False
    if promo.qty_cap is not None and qty_sold(promo) >= promo.qty_cap:
        return False
    return True


def active_promo_for(line, tier_key, on=None):
    """Return the active promo for this line+tier, or None."""
    for p in line.promos:
        if p.tier and p.tier.key == tier_key and is_active(p, on):
            return p
    return None


def status_label(promo, on=None):
    """Human status for the management list."""
    on = on or date.today()
    if promo.status == "pending":
        return "pending approval"
    if promo.status == "declined":
        return "declined"
    if promo.status == "ended":
        return "ended"
    # active record: check whether the window/cap has lapsed
    start = promo_start(promo)
    if start and on < start:
        return "scheduled"
    if promo.end_date and on > promo.end_date:
        return "expired"
    if promo.qty_cap is not None and qty_sold(promo) >= promo.qty_cap:
        return "cap reached"
    return "running"
