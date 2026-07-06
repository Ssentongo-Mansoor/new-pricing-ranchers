"""Import / re-sync recipe data from the costing app into pricing.db.

The costing app stays the editor for now, so this copies its recipe structure
into the native prod_recipe* tables. Ids are preserved so the product-to-recipe
map and any references stay stable across re-syncs. It is a full refresh: native
recipe tables are cleared and rebuilt from costing each run. It never writes to
costing.db (that bind is read-only). prod_recipe_map is left untouched.
"""
from datetime import datetime

from extensions import db
from models import (
    CostRecipe, CostRecipeLine, CostIngredient, CostSpiceMix, CostSpiceMixLine,
    CostPackSize, CostRecipeExtra,
    ProdRecipe, ProdRecipeLine, ProdIngredient, ProdSpiceMix, ProdSpiceMixLine,
    ProdPackSize, ProdRecipeExtra)
from services.audit import log


def _clear_native():
    # Children first; relationships also cascade, but explicit is safe and fast.
    for model in (ProdRecipeLine, ProdPackSize, ProdRecipeExtra, ProdRecipe,
                  ProdSpiceMixLine, ProdSpiceMix, ProdIngredient):
        db.session.query(model).delete()
    db.session.flush()


def sync_from_costing(user=None):
    """Full refresh of native recipe tables from the costing bind. Returns a
    dict of counts. Raises if the costing database is unreachable."""
    now = datetime.utcnow()
    _clear_native()

    counts = {}

    ings = db.session.scalars(db.select(CostIngredient)).all()
    for s in ings:
        db.session.add(ProdIngredient(id=s.id, name=s.name, alias1=s.alias1,
                                      alias2=s.alias2, uom=s.uom, base_cost=s.base_cost))
    counts["ingredients"] = len(ings)

    mixes = db.session.scalars(db.select(CostSpiceMix)).all()
    for s in mixes:
        db.session.add(ProdSpiceMix(id=s.id, name=s.name))
    counts["spice_mixes"] = len(mixes)

    sml = db.session.scalars(db.select(CostSpiceMixLine)).all()
    for s in sml:
        db.session.add(ProdSpiceMixLine(id=s.id, spice_mix_id=s.spice_mix_id,
                                        position=s.position, ingredient_id=s.ingredient_id,
                                        display_name=s.display_name, mass_kg=s.mass_kg))
    counts["spice_mix_lines"] = len(sml)

    recs = db.session.scalars(db.select(CostRecipe)).all()
    # Costing is native now: refresh every snapshot cost from the live engine
    # first, so ingredient/overhead/packaging changes made since the last
    # recipe save still land in the synced cost the production and inventory
    # layers read.
    from services.costing_engine import recipe_cost_per_kg as _live_cost
    for s in recs:
        try:
            s.last_cost_per_kg = _live_cost(s)
        except Exception:
            pass  # keep the stored snapshot if a recipe is malformed
    for s in recs:
        db.session.add(ProdRecipe(id=s.id, name=s.name, status=s.status,
                                  batch_label=s.batch_label, casing_type=s.casing_type,
                                  casing_cpk=s.casing_cpk, casing_pct=s.casing_pct,
                                  packaging_cpk=s.packaging_cpk,
                                  last_cost_per_kg=s.last_cost_per_kg, synced_at=now))
    counts["recipes"] = len(recs)

    rls = db.session.scalars(db.select(CostRecipeLine)).all()
    for s in rls:
        db.session.add(ProdRecipeLine(id=s.id, recipe_id=s.recipe_id, position=s.position,
                                      ingredient_id=s.ingredient_id, spice_mix_id=s.spice_mix_id,
                                      display_name=s.display_name, mass_kg=s.mass_kg))
    counts["recipe_lines"] = len(rls)

    pks = db.session.scalars(db.select(CostPackSize)).all()
    for s in pks:
        db.session.add(ProdPackSize(id=s.id, recipe_id=s.recipe_id, label=s.label,
                                    pack_weight_kg=s.pack_weight_kg, pieces=s.pieces,
                                    packing_cost=s.packing_cost))
    counts["pack_sizes"] = len(pks)

    extras = db.session.scalars(db.select(CostRecipeExtra)).all()
    for s in extras:
        db.session.add(ProdRecipeExtra(id=s.id, recipe_id=s.recipe_id, name=s.name,
                                       value_per_kg=s.value_per_kg))
    counts["recipe_extras"] = len(extras)

    log("recipe_sync", "prod_recipe", None,
        detail="imported from costing: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    db.session.commit()
    return counts


def last_synced():
    """The most recent recipe sync timestamp, or None if never synced."""
    return db.session.scalar(db.select(db.func.max(ProdRecipe.synced_at)))


def recipe_count():
    return db.session.scalar(db.select(db.func.count(ProdRecipe.id))) or 0
