"""Money formatting and effective-price resolution helpers."""
from datetime import date
from decimal import Decimal

from services import currency as cx
from services import settings

CURRENCY_SYMBOL = {"UGX": "UGX", "USD": "$", "TZS": "TZS"}


def format_money(amount, ccy="UGX"):
    """Format with thousands separators and the correct currency symbol/decimals."""
    if amount is None:
        return ""
    decimals = cx.rounding_for(ccy)
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    body = f"{value:,.{decimals}f}"
    sym = CURRENCY_SYMBOL.get(ccy, ccy)
    if ccy == "USD":
        return f"{sym}{body}"
    return f"{sym} {body}"


def effective_line_price(line, tier_key, display_ccy=None, on=None):
    """Resolve the price to show for a line+tier in ``display_ccy``.

    Returns a dict: ``{amount, currency, is_fixed, implied_rate, note}``.
    Honours an active in-window fixed-price override; otherwise converts the
    stored amount to the requested display currency at the live rate.
    """
    on = on or date.today()
    src_ccy = line.pricelist.currency
    display_ccy = display_ccy or src_ccy
    result = {"amount": None, "currency": display_ccy, "is_fixed": False,
              "is_promo": False, "implied_rate": None, "note": None}

    # An active promotional price wins over the normal price and the override.
    from services import promos as _promos
    promo = _promos.active_promo_for(line, tier_key, on)
    if promo is not None:
        amt = Decimal(str(promo.promo_amount))
        result["is_promo"] = True
        bits = []
        if promo.end_date:
            bits.append(f"until {promo.end_date:%d %b %Y}")
        if promo.qty_cap is not None:
            bits.append(f"first {promo.qty_cap:g}")
        result["note"] = "Promo" + (" " + ", ".join(bits) if bits else "")
        result["amount"] = amt if display_ccy == src_ccy else cx.convert(amt, src_ccy, display_ccy, on=on)
        return result

    ov = line.override
    if ov is not None and ov.is_in_window(on):
        result["is_fixed"] = True
        result["currency"] = ov.currency
        result["amount"] = Decimal(str(ov.amount))
        result["note"] = ov.note
        base = line.price_for(tier_key)
        if base is not None and ov.currency != src_ccy and float(ov.amount):
            # implied rate against the current base, for reference
            try:
                if src_ccy == "UGX" and ov.currency == "USD":
                    result["implied_rate"] = float(base) / float(ov.amount)
            except ZeroDivisionError:
                pass
        return result

    base = line.price_for(tier_key)
    if base is None:
        return result
    if display_ccy == src_ccy:
        result["amount"] = Decimal(str(base))
    else:
        result["amount"] = cx.convert(base, src_ccy, display_ccy, on=on)
    return result
