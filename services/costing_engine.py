"""Costing engine.

Every cost shown anywhere in the app is computed here from live data, so a single
ingredient price change cascades to every recipe and price instantly. Nothing is
read from a stored total.

Recipe final cost / kg
    mince   = sum(mass * unit_cost) / sum(mass)
    casing  = casing_cpk * casing_pct
    final   = mince + casing + overhead + packaging + sum(extras)

Pricing (matches the legacy workbook convention)
    total_cost     = recipe_cost + pack_packing_cost
    wholesale_excl = total_cost / (1 - wholesale_margin)
    wholesale_incl = wholesale_excl * (1 + vat)
    rrp_excl       = wholesale_excl / (1 - rrp_margin)
    rrp_incl       = rrp_excl * (1 + vat)
    profit_margin% = (wholesale_excl - total_cost) / wholesale_excl * 100
"""
from services.costing_models import Setting


# --------------------------------------------------------------------------- #
#  Unit costs
# --------------------------------------------------------------------------- #
def ingredient_unit_cost(ingredient):
    return ingredient.total_cost if ingredient else 0.0


def spice_mix_line_cost(line):
    if line.ingredient is not None:
        return line.ingredient.total_cost
    return line.cost_override or 0.0


def spice_mix_cost_per_kg(mix):
    total_mass = sum((l.mass_kg or 0) for l in mix.lines)
    if total_mass <= 0:
        return 0.0
    batch = sum((l.mass_kg or 0) * spice_mix_line_cost(l) for l in mix.lines)
    return batch / total_mass


def recipe_line_unit_cost(line):
    """Resolve the live cost / kg for a recipe line from its linked source."""
    if line.ingredient is not None:
        return line.ingredient.total_cost
    if line.spice_mix is not None:
        return spice_mix_cost_per_kg(line.spice_mix)
    return line.cost_override or 0.0


# --------------------------------------------------------------------------- #
#  Recipe costing
# --------------------------------------------------------------------------- #
def recipe_cost_breakdown(recipe):
    """Return a full breakdown dict for a recipe."""
    overhead = recipe.overhead_override
    if overhead is None:
        overhead = Setting.get_float("overhead_per_kg", 900)

    lines = []
    total_mass = 0.0
    batch_cost = 0.0
    for ln in recipe.lines:
        unit = recipe_line_unit_cost(ln)
        mass = ln.mass_kg or 0.0
        line_batch = mass * unit
        total_mass += mass
        batch_cost += line_batch
        lines.append({
            "line": ln,
            "name": ln.display_name,
            "mass": mass,
            "unit_cost": unit,
            "batch_cost": line_batch,
        })

    mince = batch_cost / total_mass if total_mass > 0 else 0.0
    casing = (recipe.casing_cpk or 0.0) * (recipe.casing_pct or 0.0)
    packaging = recipe.packaging_cpk or 0.0
    extras = [{"name": e.name, "value": e.value_per_kg or 0.0} for e in recipe.extras]
    extras_total = sum(e["value"] for e in extras)

    product_cost = mince + casing                      # cost/kg before overhead & packaging
    final = product_cost + overhead + packaging + extras_total

    # cost percentage per line (share of batch)
    for d in lines:
        d["cost_pct"] = (d["batch_cost"] / batch_cost * 100) if batch_cost else 0.0

    return {
        "lines": lines,
        "total_mass": total_mass,
        "batch_cost": batch_cost,
        "mince": mince,
        "casing": casing,
        "overhead": overhead,
        "packaging": packaging,
        "extras": extras,
        "extras_total": extras_total,
        "product_cost": product_cost,
        "final": final,
    }


def recipe_cost_per_kg(recipe):
    return recipe_cost_breakdown(recipe)["final"]


# --------------------------------------------------------------------------- #
#  Pricing
# --------------------------------------------------------------------------- #
def pricing_for_cost(total_cost, wholesale_margin=None, rrp_margin=None, vat=None):
    if wholesale_margin is None:
        wholesale_margin = Setting.get_float("wholesale_margin", 0.47)
    if rrp_margin is None:
        rrp_margin = Setting.get_float("rrp_margin", 0.15)
    if vat is None:
        vat = Setting.get_float("vat_rate", 0.18)

    wholesale_excl = total_cost / (1 - wholesale_margin) if (1 - wholesale_margin) > 0 else 0.0
    wholesale_incl = wholesale_excl * (1 + vat)
    rrp_excl = wholesale_excl / (1 - rrp_margin) if (1 - rrp_margin) > 0 else 0.0
    rrp_incl = rrp_excl * (1 + vat)
    profit = wholesale_excl - total_cost
    margin_pct = (profit / wholesale_excl * 100) if wholesale_excl else 0.0
    return {
        "total_cost": total_cost,
        "wholesale_excl": wholesale_excl,
        "wholesale_incl": wholesale_incl,
        "rrp_excl": rrp_excl,
        "rrp_incl": rrp_incl,
        "profit": profit,
        "margin_pct": margin_pct,
    }


def recipe_pricing(recipe, packing_cost=0.0, **kw):
    cost = recipe_cost_per_kg(recipe)
    return pricing_for_cost(cost + (packing_cost or 0.0), **kw)


# --------------------------------------------------------------------------- #
#  What-if impact analysis
# --------------------------------------------------------------------------- #
def recipes_using_ingredient(ingredient, recipes):
    """Return recipes whose cost depends on `ingredient` (directly or via a spice mix)."""
    affected = []
    for r in recipes:
        if _recipe_depends_on(r, ingredient.id):
            affected.append(r)
    return affected


def _recipe_depends_on(recipe, ingredient_id):
    for ln in recipe.lines:
        if ln.ingredient_id == ingredient_id:
            return True
        if ln.spice_mix is not None:
            for sl in ln.spice_mix.lines:
                if sl.ingredient_id == ingredient_id:
                    return True
    return False


def carcass_breakdown(carcass):
    """Cost out a carcass into its cuts.

    Loss (bone, trim, evaporation) is the carcass weight not recovered as cuts.
    The full landed cost is spread across the recovered cut weight, so loss
    correctly raises the cost per kg of the saleable cuts.

    Allocation:
      weight -> every cut costs the same per kg = landed_cost / cut_weight
      value  -> cost per kg proportional to each cut's market value
                (falls back to weight allocation if no selling prices)
    """
    landed = (carcass.purchase_cost or 0.0) + sum((c.amount or 0.0) for c in carcass.costs)
    fee = carcass.processing_fee_per_kg or 0.0
    cut_weight = sum((c.weight_kg or 0.0) for c in carcass.cuts)
    carcass_weight = carcass.carcass_weight_kg or 0.0
    loss = carcass_weight - cut_weight
    loss_pct = (loss / carcass_weight * 100) if carcass_weight else 0.0

    # Total cost = purchase + lump extras + a flat processing fee on every cut kg.
    total_cost = landed + fee * cut_weight

    total_value = sum((c.weight_kg or 0.0) * (c.selling_price or 0.0) for c in carcass.cuts)
    method = carcass.allocation_method or "value"
    use_value = (method == "value" and total_value > 0)
    uniform_cpk = (total_cost / cut_weight) if cut_weight else 0.0

    inj = carcass.injection_pct or 0.0   # water injection adds saleable weight on injectable cuts

    rows = []
    revenue = 0.0
    export_revenue = 0.0
    injected_revenue = 0.0
    injected_export_revenue = 0.0
    saleable_weight = 0.0
    has_export = False
    for c in carcass.cuts:
        w = c.weight_kg or 0.0
        sell = c.selling_price or 0.0
        exp = c.export_price or 0.0
        if exp > 0:
            has_export = True
        # Allocate the purchase/landed cost, then add the flat per-kg fee on top.
        if use_value:
            cpk = ((landed / total_value) * sell if total_value else 0.0) + fee
        else:
            cpk = uniform_cpk
        cut_cost = cpk * w
        margin = sell - cpk
        margin_pct = (margin / sell * 100) if sell else 0.0
        exp_margin = exp - cpk
        exp_margin_pct = (exp_margin / exp * 100) if exp else 0.0
        inj_w = w * (1 + inj) if c.injectable else w
        revenue += w * sell
        export_revenue += w * (exp if exp > 0 else sell)
        injected_revenue += inj_w * sell
        injected_export_revenue += inj_w * (exp if exp > 0 else sell)
        if sell > 0:
            saleable_weight += w
        rows.append({
            "cut": c, "name": c.name, "weight": w, "injectable": c.injectable,
            "injected_weight": inj_w,
            "yield_pct": (w / carcass_weight * 100) if carcass_weight else 0.0,
            "cost_per_kg": cpk, "cut_cost": cut_cost,
            "selling_price": sell, "margin": margin, "margin_pct": margin_pct,
            "export_price": exp, "export_margin": exp_margin, "export_margin_pct": exp_margin_pct,
        })

    profit = revenue - total_cost
    profit_pct = (profit / revenue * 100) if revenue else 0.0
    export_profit = export_revenue - total_cost
    export_profit_pct = (export_profit / export_revenue * 100) if export_revenue else 0.0
    avg_cpk = (total_cost / cut_weight) if cut_weight else 0.0            # over all cut kg
    avg_saleable_cpk = (total_cost / saleable_weight) if saleable_weight else 0.0

    injected = inj > 0
    injected_profit = injected_revenue - total_cost
    injected_profit_pct = (injected_profit / injected_revenue * 100) if injected_revenue else 0.0
    injected_export_profit = injected_export_revenue - total_cost
    injected_export_profit_pct = (injected_export_profit / injected_export_revenue * 100) if injected_export_revenue else 0.0
    return {
        "landed_cost": landed,
        "processing_fee_per_kg": fee,
        "total_cost": total_cost,
        "carcass_weight": carcass_weight,
        "cut_weight": cut_weight,
        "loss": loss, "loss_pct": loss_pct,
        "method": method, "used_value": use_value,
        "uniform_cpk": uniform_cpk,
        "avg_cpk": avg_cpk, "avg_saleable_cpk": avg_saleable_cpk,
        "rows": rows,
        "revenue": revenue, "profit": profit, "profit_pct": profit_pct,
        "has_export": has_export,
        "export_revenue": export_revenue, "export_profit": export_profit,
        "export_profit_pct": export_profit_pct,
        "injected": injected, "injection_pct": inj,
        "injected_revenue": injected_revenue, "injected_profit": injected_profit,
        "injected_profit_pct": injected_profit_pct,
        "injected_export_revenue": injected_export_revenue,
        "injected_export_profit": injected_export_profit,
        "injected_export_profit_pct": injected_export_profit_pct,
    }


def whatif_impact(ingredient, new_total_cost, recipes):
    """Compute each affected recipe's cost before/after a hypothetical price change.

    Temporarily overrides the ingredient's component costs to hit `new_total_cost`,
    recomputes, then restores. No database writes.
    """
    affected = recipes_using_ingredient(ingredient, recipes)

    old_base = ingredient.base_cost
    old_tax = ingredient.tax_value
    old_clear = ingredient.clearance
    old_freight = ingredient.freight
    old_total = ingredient.total_cost

    results = []
    for r in affected:
        before = recipe_cost_per_kg(r)
        results.append([r, before, None, None, None])

    # Apply hypothetical: keep add-ons, shift base so total == new_total_cost.
    addons = old_tax + old_clear + old_freight
    ingredient.base_cost = max(0.0, new_total_cost - addons)
    try:
        for row in results:
            r = row[0]
            after = recipe_cost_per_kg(r)
            row[2] = after
            row[3] = after - row[1]
            row[4] = (row[3] / row[1] * 100) if row[1] else 0.0
    finally:
        ingredient.base_cost = old_base
        ingredient.tax_value = old_tax
        ingredient.clearance = old_clear
        ingredient.freight = old_freight

    return {
        "ingredient": ingredient,
        "old_total": old_total,
        "new_total": new_total_cost,
        "rows": results,
        "count": len(results),
    }
