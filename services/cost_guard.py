"""Cost floor — blocks below-cost pricing (QA audit 5 Jul 2026, feature gap).

The product carries a ``unit_cost`` in UGX per product unit (the same unit
prices are quoted in). When a cost is set, no human-entered price — inline
pricelist edit, bulk adjustment, offer fixed price, or a new product's opening
price — may fall below the cost. Products without a cost are not guarded:
the floor only enforces data someone has entered, never guesses.

Rules:
  * Comparison happens in UGX. Foreign-currency prices convert at the dated
    rate; when no rate exists the guard stands aside instead of blocking
    unrelated work on a missing rate row.
  * A None/zero cost disables the guard for that product.
  * Discounts count: the guard checks the effective per-unit amount.
"""
from decimal import Decimal, InvalidOperation

from services import currency as cx
from services.pricing import format_money


def _to_ugx(amount, currency):
    if amount is None:
        return None
    if not currency or currency == "UGX":
        return Decimal(str(amount))
    try:
        return Decimal(str(cx.convert(amount, currency, "UGX")))
    except Exception:
        return None  # no usable rate — do not block on a lookup failure


def below_cost_error(product, amount, currency="UGX", discount_pct=0):
    """Return a human-readable error when the effective price is below cost.

    None means the price is acceptable (or the product has no cost set, or
    the currency cannot be converted)."""
    if product is None or amount is None:
        return None
    cost = getattr(product, "unit_cost", None)
    try:
        cost = Decimal(str(cost)) if cost is not None else None
    except InvalidOperation:
        return None
    if not cost or cost <= 0:
        return None
    try:
        eff = Decimal(str(amount)) * (Decimal(1) - Decimal(str(discount_pct or 0)) / Decimal(100))
    except InvalidOperation:
        return None
    eff_ugx = _to_ugx(eff, currency)
    if eff_ugx is None:
        return None
    if eff_ugx < cost:
        return (f"{product.article_no} {product.description}: price "
                f"{format_money(eff, currency or 'UGX')} is below cost "
                f"{format_money(cost, 'UGX')}. Below-cost prices are blocked; "
                f"correct the price or update the product cost.")
    return None
