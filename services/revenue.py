"""Single source of truth for the net (excl-VAT) UGX value of a live order.

Revenue aggregates mix two sources: uploaded invoice history (net, ``untaxed``)
and live sales orders. To keep them comparable, every live order must be valued
NET of VAT and in UGX. ``SalesOrder.total`` is VAT-inclusive, so it must never be
used for revenue; use ``SalesOrder.subtotal`` (the net sum of line totals).

For a foreign-currency order the net value is multiplied by the stamped
``exchange_rate_value``. When that is missing (older/portal orders that were not
stamped), fall back to the managed rate in force on the order's date. If no rate
is available at all, the order contributes 0 and a warning is logged.
"""
from services import currency as cx


def net_ugx(order):
    """Return the NET (excl-VAT) UGX value of a SalesOrder.

    Uses ``order.subtotal`` (net), never ``order.total`` (VAT-inclusive).
    Converts foreign currency to UGX via the stamped rate, falling back to the
    dated live rate, and returns 0.0 (with a warning) if no rate can be found.
    """
    net = float(order.subtotal or 0)
    ccy = order.currency or "UGX"
    if ccy == "UGX":
        return net

    rate = order.exchange_rate_value
    if rate:
        return net * float(rate)

    # Foreign order with no stamped rate: fall back to the dated live rate.
    try:
        r = cx.get_rate(ccy, on=getattr(order, "order_date", None))
        if r is not None:
            return net * float(r.rate)
    except Exception:
        pass

    try:
        from flask import current_app
        current_app.logger.warning(
            "net_ugx: no exchange rate for order %s (%s on %s); counted as 0",
            getattr(order, "number", getattr(order, "id", "?")),
            ccy, getattr(order, "order_date", None))
    except Exception:
        pass
    return 0.0
