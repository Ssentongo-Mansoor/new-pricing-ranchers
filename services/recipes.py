"""Phase 2 — recipes, product-to-recipe mapping, and the raw-material explosion.

Recipes live in the separate costing app, attached read-only on the 'costing'
bind. The link from a sellable product to its recipe does not exist in either
app, so it is built here: proposed by name match, confirmed once by a manager,
and stored in prod_recipe_map (pricing.db).

Explosion scales a recipe to the quantity needed. A recipe is a batch (a list of
ingredient masses). Batch size = sum of the recipe's line masses in kg. To make
N kg, every ingredient is multiplied by N / batch_size. Spice-mix lines are
expanded into their own ingredients by the mix's internal proportions. Quantities
in packs or pieces are converted to kg with the recipe's pack weight.
"""
import re

from extensions import db
from models import (Product, StoreItem, ProdRecipeMap,
                    ProdRecipe as CostRecipe, ProdRecipeLine as CostRecipeLine,
                    ProdIngredient as CostIngredient, ProdSpiceMix as CostSpiceMix,
                    ProdSpiceMixLine as CostSpiceMixLine, ProdPackSize as CostPackSize)
from services.audit import log

EPS = 1e-9
_STOP = re.compile(r'\b(NEW|COSTING|RF|SELECTION)\b')


def norm(s):
    """Normalise a name for matching: upper, drop filler words and punctuation."""
    s = (s or "").upper()
    s = _STOP.sub('', s)
    s = re.sub(r'[^A-Z0-9]', '', s)
    return s


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def _recipe_index():
    idx = {}
    for r in db.session.scalars(db.select(CostRecipe)):
        for key in (norm(r.name), norm(r.batch_label)):
            if key:
                idx.setdefault(key, r)   # first wins
    return idx


def confirmed_map():
    """{product_id: ProdRecipeMap} for confirmed links."""
    return {m.product_id: m for m in
            db.session.scalars(db.select(ProdRecipeMap))}


def propose_for_product(product, idx=None):
    """Best recipe guess for one product by normalised name. Returns CostRecipe or None."""
    idx = idx or _recipe_index()
    return idx.get(norm(product.description))


def proposals():
    """Auto-match suggestions for active products that are not yet mapped.
    Returns list of {product, recipe}."""
    idx = _recipe_index()
    mapped = set(confirmed_map().keys())
    out = []
    for p in db.session.scalars(
            db.select(Product).filter(Product.status == "active")
            .order_by(Product.description)):
        if p.id in mapped:
            continue
        r = idx.get(norm(p.description))
        if r is not None:
            out.append({"product": p, "recipe": r})
    return out


def set_map(product_id, recipe_id, user, method="manual"):
    product = db.session.get(Product, product_id)
    recipe = db.session.get(CostRecipe, recipe_id)
    if product is None or recipe is None:
        return False, "Product or recipe not found."
    m = db.session.scalar(db.select(ProdRecipeMap).filter_by(product_id=product_id))
    if m is None:
        m = ProdRecipeMap(product_id=product_id)
        db.session.add(m)
    m.recipe_id = recipe.id
    m.recipe_name = recipe.name
    m.match_method = method
    m.confirmed = True
    m.confirmed_by_id = getattr(user, "id", None)
    log("prod_recipe_map", "product", product_id,
        detail=f"{product.article_no} {product.description} -> recipe {recipe.id} {recipe.name}")
    db.session.commit()
    return True, f"Linked {product.description} to recipe {recipe.name}."


def unmap(product_id):
    m = db.session.scalar(db.select(ProdRecipeMap).filter_by(product_id=product_id))
    if m:
        db.session.delete(m)
        db.session.commit()
    return True, "Mapping removed."


def confirm_all_proposals(user):
    """Confirm every current auto-proposal in one go."""
    n = 0
    for pr in proposals():
        ok, _ = set_map(pr["product"].id, pr["recipe"].id, user, method="auto")
        if ok:
            n += 1
    return n


# ---------------------------------------------------------------------------
# Explosion
# ---------------------------------------------------------------------------
def batch_total_kg(recipe_id):
    total = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(CostRecipeLine.mass_kg), 0.0))
        .filter(CostRecipeLine.recipe_id == recipe_id)) or 0.0
    return float(total)


def pack_weight_kg(recipe_id):
    """Representative pack weight for converting packs/pieces to kg. Uses the
    first pack size on the recipe. Returns None if none."""
    ps = db.session.scalar(
        db.select(CostPackSize).filter(CostPackSize.recipe_id == recipe_id,
                                       CostPackSize.pack_weight_kg.isnot(None))
        .order_by(CostPackSize.id))
    return float(ps.pack_weight_kg) if ps and ps.pack_weight_kg else None


def needed_kg(product, qty, recipe_id):
    """Convert a quantity in the product's unit to kilograms.

    Returns (kg, basis) where basis explains the conversion.
    """
    uom = (product.unit_of_measure or "").lower()
    q = float(qty or 0)
    if uom in ("kg", "kgs", "kilogram", "kilograms"):
        return q, "kg direct"
    pw = pack_weight_kg(recipe_id)
    if pw:
        return q * pw, f"{q:g} x {pw:g}kg pack"
    return q, "unit treated as kg (no pack weight)"


def explode(product, qty, recipe_id=None):
    """Raw-material requirements to make `qty` (product unit) of a product.

    Returns a dict: recipe, kg_needed, basis, batch_kg, factor, components
    (list of {ingredient_id, name, kg}), and any warning. Components aggregate
    direct recipe ingredients and spice-mix ingredients expanded by the mix's
    internal proportions.
    """
    if recipe_id is None:
        m = db.session.scalar(db.select(ProdRecipeMap).filter_by(product_id=product.id))
        if m is None:
            return {"recipe": None, "warning": "No recipe linked to this product."}
        recipe_id = m.recipe_id
    recipe = db.session.get(CostRecipe, recipe_id)
    if recipe is None:
        return {"recipe": None, "warning": "Linked recipe not found in costing."}

    batch = batch_total_kg(recipe_id)
    if batch <= EPS:
        return {"recipe": recipe, "warning": "Recipe has no ingredient masses."}
    kg, basis = needed_kg(product, qty, recipe_id)
    factor = kg / batch if batch else 0.0

    comp = {}   # ingredient_id (or name key) -> {name, kg}

    def add(ing_id, name, mass):
        key = ing_id if ing_id is not None else ("name:" + (name or ""))
        c = comp.setdefault(key, {"ingredient_id": ing_id, "name": name, "kg": 0.0})
        c["kg"] += mass

    for line in db.session.scalars(
            db.select(CostRecipeLine).filter_by(recipe_id=recipe_id)
            .order_by(CostRecipeLine.position)):
        line_kg = (line.mass_kg or 0) * factor
        if line.spice_mix_id:
            sm_lines = db.session.scalars(
                db.select(CostSpiceMixLine).filter_by(spice_mix_id=line.spice_mix_id)).all()
            sm_total = sum(sl.mass_kg or 0 for sl in sm_lines)
            if sm_total > EPS:
                for sl in sm_lines:
                    frac = (sl.mass_kg or 0) / sm_total
                    ing = db.session.get(CostIngredient, sl.ingredient_id) if sl.ingredient_id else None
                    name = (ing.name if ing else None) or sl.display_name or "spice component"
                    add(sl.ingredient_id, name, line_kg * frac)
            else:
                sm = db.session.get(CostSpiceMix, line.spice_mix_id)
                add(None, (sm.name if sm else None) or line.display_name or "spice mix", line_kg)
        else:
            ing = db.session.get(CostIngredient, line.ingredient_id) if line.ingredient_id else None
            name = (ing.name if ing else None) or line.display_name or "ingredient"
            add(line.ingredient_id, name, line_kg)

    components = sorted(comp.values(), key=lambda c: -c["kg"])
    return {"recipe": recipe, "kg_needed": kg, "basis": basis, "batch_kg": batch,
            "factor": factor, "components": components, "warning": None,
            "packaging_cpk": recipe.packaging_cpk, "casing_type": recipe.casing_type}


# ---------------------------------------------------------------------------
# Raw materials needs vs store stock
# ---------------------------------------------------------------------------
def _store_index():
    """{normalised name: StoreItem} for non-sellable store items (raw materials)."""
    idx = {}
    for it in db.session.scalars(db.select(StoreItem)):
        k = norm(it.name)
        if k and k not in idx:
            idx[k] = it
    return idx


def materials_requirement(shortfall_rows):
    """Aggregate raw-material needs across the given product shortfalls.

    shortfall_rows: list of dicts from production.to_produce_list (each has
    'product' and 'shortfall'). Returns (needs, unmapped_products):
      needs: list of {name, kg_needed, on_hand, uom, short, matched}
      unmapped_products: products with a shortfall but no recipe link.
    """
    cmap = confirmed_map()
    agg = {}        # normalised ingredient name -> {name, kg}
    unmapped = []
    for row in shortfall_rows:
        product = row["product"]
        if product.id not in cmap:
            unmapped.append(product)
            continue
        ex = explode(product, row["shortfall"], cmap[product.id].recipe_id)
        if ex.get("warning"):
            unmapped.append(product)
            continue
        for c in ex["components"]:
            k = norm(c["name"])
            a = agg.setdefault(k, {"name": c["name"], "kg": 0.0})
            a["kg"] += c["kg"]

    sidx = _store_index()
    needs = []
    for k, a in agg.items():
        it = sidx.get(k)
        on_hand = float(it.quantity or 0) if it else None
        uom = (it.uom if it else "kg") or "kg"
        short = (a["kg"] - on_hand) if on_hand is not None else None
        needs.append({"name": a["name"], "kg_needed": a["kg"],
                      "on_hand": on_hand, "uom": uom,
                      "short": (max(short, 0.0) if short is not None else None),
                      "matched": it is not None,
                      "store_item": it})
    needs.sort(key=lambda n: (n["matched"], -(n["short"] or 0), -n["kg_needed"]))
    return needs, unmapped
