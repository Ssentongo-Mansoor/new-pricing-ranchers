"""Order fulfilment timing and SLA (delay) checks.

Durations are measured from when the order was placed (placed_at), or its order
date as a fallback. SLA targets are admin-configurable settings (hours).
"""
from datetime import datetime

from services import settings as settings_svc

OPEN_STATUSES = ("placed", "in_fulfillment", "pending", "ready_for_dispatch",
                 "out_for_delivery", "dispatched")


def _start(order):
    # Use the earliest real timestamp we have, so the SLA clock starts when the
    # order actually entered the system — not midnight of the order date.
    for ts in (order.placed_at, getattr(order, "submitted_at", None), order.created_at):
        if ts:
            return ts
    if order.order_date:
        return datetime.combine(order.order_date, datetime.min.time())
    return None


def _hours(a, b):
    if not a or not b:
        return None
    return (b - a).total_seconds() / 3600.0


def humanize(hours):
    if hours is None:
        return "—"
    mins = int(round(hours * 60))
    d, rem = divmod(mins, 60 * 24)
    h, m = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m and not d:
        parts.append(f"{m}m")
    return " ".join(parts) or "0m"


def durations(order):
    start = _start(order)
    now = datetime.utcnow()
    deliver_at = order.delivered_at or order.fulfilled_at
    to_dispatch = _hours(start, order.dispatched_at)
    to_deliver = _hours(start, deliver_at)
    dispatch_to_deliver = _hours(order.dispatched_at, deliver_at)
    prep = _hours(order.fulfilment_started_at or start, order.dispatched_at)
    elapsed = _hours(start, now) if order.status in OPEN_STATUSES else None
    return {"to_dispatch": to_dispatch, "to_deliver": to_deliver,
            "dispatch_to_deliver": dispatch_to_deliver, "prep": prep,
            "elapsed": elapsed}


def delay_status(order):
    """Return (delayed: bool, reason: str) for an open order against the SLA."""
    if order.status not in OPEN_STATUSES:
        return False, ""
    start = _start(order)
    if not start:
        return False, ""
    hrs = (datetime.utcnow() - start).total_seconds() / 3600.0
    sla_dispatch = settings_svc.get_float("sla_dispatch_hours", 24)
    sla_delivery = settings_svc.get_float("sla_delivery_hours", 48)
    if order.dispatched_at is None and hrs > sla_dispatch:
        return True, f"not dispatched after {humanize(hrs)} (target {humanize(sla_dispatch)})"
    if order.dispatched_at is not None and order.delivered_at is None and hrs > sla_delivery:
        return True, f"not delivered after {humanize(hrs)} (target {humanize(sla_delivery)})"
    return False, ""
