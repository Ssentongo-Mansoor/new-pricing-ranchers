"""Currency conversion against the managed exchange rate.

UGX is the base of value. A rate is ``UGX per 1 unit of the quote currency``
(for example UGX 3,800 per 1 USD). Conversions:

    ugx -> usd :  ugx / rate
    usd -> ugx :  usd * rate

The rate in force for a date is the most recent ``ExchangeRate`` whose window
covers that date. An expired rate with no replacement raises ``NoValidRate`` so
new USD exports/offers are blocked until a fresh rate is entered.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select

from extensions import db
from models import ExchangeRate
from services import settings


class NoValidRate(Exception):
    """Raised when no exchange rate covers the requested currency/date."""


def rounding_for(currency):
    if currency == "USD":
        return settings.get_int("usd_round", 2)
    if currency == "UGX":
        return settings.get_int("ugx_round", 0)
    return 2


def quantize(value, currency):
    if value is None:
        return None
    decimals = rounding_for(currency)
    q = Decimal(1).scaleb(-decimals)  # e.g. 0.01 for 2 decimals
    return Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)


def get_rate(quote_ccy, on=None, base_ccy="UGX"):
    """Return the ExchangeRate row in force for ``quote_ccy`` on ``on`` (or None)."""
    if quote_ccy == base_ccy:
        return None  # identity; no rate needed
    on = on or date.today()
    stmt = (
        select(ExchangeRate)
        .where(ExchangeRate.quote_ccy == quote_ccy, ExchangeRate.base_ccy == base_ccy)
        .where(ExchangeRate.effective_date <= on)
        .order_by(ExchangeRate.effective_date.desc(), ExchangeRate.id.desc())
    )
    # Only the most recent effective rate governs the date. If that newest rate
    # has expired for ``on`` we do NOT fall back to older still-open rows (L4):
    # an expired latest rate means no valid rate, so callers block until a fresh
    # one is entered rather than silently using a stale price.
    row = db.session.scalars(stmt).first()
    if row is None:
        return None
    if row.expiry_date is None or row.expiry_date >= on:
        return row
    return None


def require_rate(quote_ccy, on=None, base_ccy="UGX"):
    rate = get_rate(quote_ccy, on=on, base_ccy=base_ccy)
    if rate is None:
        raise NoValidRate(
            f"No valid {base_ccy}->{quote_ccy} exchange rate for {on or date.today()}."
        )
    return rate


def convert(amount, from_ccy, to_ccy, on=None, rate_value=None, rate_ccy=None):
    """Convert an amount between currencies through the UGX base.

    ``rate_value`` (UGX per 1 unit of the *non-UGX* currency) can be supplied to
    use a stamped rate (e.g. on an issued offer) instead of the live one.

    A stamped ``rate_value`` only describes ONE currency. ``rate_ccy`` names that
    currency; if omitted it defaults to whichever of ``from_ccy``/``to_ccy`` is
    not UGX. For a cross-currency conversion (e.g. USD->TZS) the stamped rate is
    applied only to its own leg (M15); the other leg falls through to the normal
    dated rate lookup, so a stamped USD rate is never reused for the TZS leg.
    """
    if amount is None:
        return None
    if from_ccy == to_ccy:
        return quantize(amount, to_ccy)
    amount = Decimal(str(amount))

    if rate_ccy is None:
        # Default: the single non-UGX side of a UGX<->X conversion.
        rate_ccy = to_ccy if from_ccy == "UGX" else from_ccy

    def rate_ugx_per(ccy):
        if rate_value is not None and ccy == rate_ccy:
            return Decimal(str(rate_value))
        return Decimal(str(require_rate(ccy, on=on).rate))

    # to UGX first
    if from_ccy == "UGX":
        ugx = amount
    else:
        ugx = amount * rate_ugx_per(from_ccy)
    # then to target
    if to_ccy == "UGX":
        result = ugx
    else:
        result = ugx / rate_ugx_per(to_ccy)
    return quantize(result, to_ccy)
