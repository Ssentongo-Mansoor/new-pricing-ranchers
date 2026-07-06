"""Cost floor — blocks below-cost pricing (QA audit 5 Jul 2026, feature gap).

THE COSTING CONVENTION (Stephan, 6 Jul 2026): all costing is per kg. The
costing sheets produce a cost per kg; selling prices are then per kg for
catch-weight items (pack "KG" or blank) and PER PACK for packed items, derived
from the pack weight. product.unit_cost therefore stores UGX COST PER KG for
every product, and this guard converts per pricelist line:

  * line pack parses to a weight (200g, 720Gr, 5 x 200 Gr) ->
        floor = cost/kg x pack weight, compared against the pack price;
  * line pack is "KG" or blank -> price is per kg, compared directly;
  * line pack does not parse (e.g. "4PCS") -> the basis is unknown, the
    guard stands aside rather than misfire.

Other rules:
  * Comparison happens in UGX. Foreign-currency prices convert at the dated
    rate; when no rate exists the guard stands aside instead of blocking
    unrelated work on a missing rate row.
  * None/zero cost disables the guard for the product. None/zero price is a
    placeholder, not a below-cost price — ignored.
  * Discounts count: the guard checks the effective per-unit amount.
"""
from decimal import Decimal, InvalidOperation

from services import currency as cx
from services.inventory_costing import parse_pack_weight_kg
from services.pricing import format_money

_PER_KG_TOKENS = {"", "kg", "kg.", "per kg", "/kg", "1kg", "1 kg"}


def pack_factor(pack_size):
    """Multiplier from cost/kg to cost per selling unit for a line.

    1.0   -> price is per kg
    0.2   -> 200g pack, price is per pack
    None  -> basis unknown (e.g. piece counts without weight): do not guard."""
    t = (pack_size or "").strip().lower()
    if t in _PER_KG_TOKENS:
        return Decimal(1)
    w = parse_pack_weight_kg(pack_size or "")
    if w and w > 0:
        return Decimal(str(w))
    return None


def _to_ugx(amount, currency):
    if amount is None:
        return None
    if not currency or currency == "UGX":
        return Decimal(str(amount))
    try:
        return Decimal(str(cx.convert(amount, currency, "UGX")))
    except Exception:
        return None  # no usable rate — do not block on a lookup failure


def below_cost_error(product, amount, currency="UGX", discount_pct=0,
                     pack_size=None):
    """Return a human-readable error when the effective price is below cost.

    ``amount`` is the price as entered on the line (per kg or per pack —
    ``pack_size`` decides). ``product.unit_cost`` is always UGX per kg.
    None means acceptable, unguarded, or basis/rate unknown."""
    if product is None or amount is None:
        return None
    cost_kg = getattr(product, "unit_cost", None)
    try:
        cost_kg = Decimal(str(cost_kg)) if cost_kg is not None else None
    except InvalidOperation:
        return None
    if not cost_kg or cost_kg <= 0:
        return None
    try:
        eff = Decimal(str(amount)) * (Decimal(1) - Decimal(str(discount_pct or 0)) / Decimal(100))
    except InvalidOperation:
        return None
    if eff <= 0:
        return None  # zero/negative = placeholder, not a price
    factor = pack_factor(pack_size if pack_size is not None
                         else getattr(product, "pack_size", None))
    if factor is None:
        return None  # unknown unit basis — never misfire
    floor = cost_kg * factor
    eff_ugx = _to_ugx(eff, currency)
    if eff_ugx is None:
        return None
    if eff_ugx < floor:
        unit = "per kg" if factor == 1 else f"per {product.pack_size or pack_size} pack"
        return (f"{product.article_no} {product.description}: price "
                f"{format_money(eff, currency or 'UGX')} {unit} is below cost "
                f"{format_money(floor, 'UGX')} (cost {format_money(cost_kg, 'UGX')}/kg). "
                f"Below-cost prices are blocked; correct the price or the product cost.")
    return None
