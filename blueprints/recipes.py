"""Product recipes: list, view, create/edit builder, activate/deactivate, live calc."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify)
from flask_login import login_required

from extensions import db
from services.costing_models import (Recipe, RecipeLine, RecipeExtra, PackSize, Category,
                    Ingredient, SpiceMix, Setting, utcnow)
from services.costing_auth import editor_required, log_action
from services.costing_engine import (recipe_cost_breakdown, recipe_cost_per_kg,
                     spice_mix_cost_per_kg)

recipes_bp = Blueprint("recipes", __name__, url_prefix="/costing/recipes")


@recipes_bp.route("/")
@login_required
def index():
    show_all = request.args.get("show") == "all"
    cat_id = request.args.get("category", type=int)
    query = Recipe.query
    if not show_all:
        query = query.filter_by(status="active")
    if cat_id:
        query = query.filter_by(category_id=cat_id)
    recipes = query.order_by(Recipe.name).all()
    rows = [(r, recipe_cost_per_kg(r)) for r in recipes]
    categories = Category.query.order_by(Category.display_order).all()
    return render_template("costing/recipes.html", rows=rows, show_all=show_all,
                           categories=categories, cat_id=cat_id)


@recipes_bp.route("/inactive")
@login_required
def inactive():
    recipes = Recipe.query.filter_by(status="inactive").order_by(Recipe.name).all()
    return render_template("costing/recipes_inactive.html", recipes=recipes)


@recipes_bp.route("/<int:rid>")
@login_required
def view(rid):
    recipe = db.get_or_404(Recipe, rid)
    breakdown = recipe_cost_breakdown(recipe)
    return render_template("costing/recipe_view.html", recipe=recipe, b=breakdown)


@recipes_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    if request.method == "POST":
        return _save(None)
    return render_template("costing/recipe_form.html", recipe=None,
                           categories=Category.query.order_by(Category.display_order).all(),
                           default_overhead=Setting.get_float("overhead_per_kg", 900),
                           default_packaging=Setting.get_float("default_packaging", 1405.69))


@recipes_bp.route("/<int:rid>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(rid):
    recipe = db.get_or_404(Recipe, rid)
    if request.method == "POST":
        return _save(recipe)
    return render_template("costing/recipe_form.html", recipe=recipe,
                           categories=Category.query.order_by(Category.display_order).all(),
                           default_overhead=Setting.get_float("overhead_per_kg", 900),
                           default_packaging=Setting.get_float("default_packaging", 1405.69))


def _resolve_category(form):
    """Return a Category id, creating a new one inline if requested."""
    new_cat = form.get("new_category", "").strip()
    if new_cat:
        existing = Category.query.filter(db.func.lower(Category.name) == new_cat.lower()).first()
        if existing:
            return existing.id
        order = (db.session.query(db.func.max(Category.display_order)).scalar() or 0) + 1
        cat = Category(name=new_cat, display_order=order)
        db.session.add(cat)
        db.session.flush()
        log_action("create", "category", cat.id, "name", None, cat.name)
        return cat.id
    cid = form.get("category_id")
    return int(cid) if cid else None


def _save(recipe):
    form = request.form
    is_new = recipe is None
    if is_new:
        recipe = Recipe(name=form["name"].strip(), status="active")
        db.session.add(recipe)
        db.session.flush()

    recipe.name = form["name"].strip()
    recipe.category_id = _resolve_category(form)
    recipe.batch_label = form.get("batch_label") or None
    recipe.casing_type = form.get("casing_type") or None
    recipe.casing_cpk = float(form.get("casing_cpk") or 0)
    recipe.casing_pct = float(form.get("casing_pct") or 0)
    oh = form.get("overhead_override", "").strip()
    recipe.overhead_override = float(oh) if oh else None
    recipe.packaging_cpk = float(form.get("packaging_cpk") or 0)
    recipe.note = form.get("note") or None

    # Rebuild lines.
    for ln in list(recipe.lines):
        db.session.delete(ln)
    for ex in list(recipe.extras):
        db.session.delete(ex)
    db.session.flush()

    names = form.getlist("line_name")
    masses = form.getlist("line_mass")
    kinds = form.getlist("line_kind")        # ingredient | spice_mix | manual
    refs = form.getlist("line_ref")          # id of ingredient/spice_mix
    overrides = form.getlist("line_cost")
    for i, nm in enumerate(names):
        nm = (nm or "").strip()
        if not nm:
            continue
        kind = kinds[i] if i < len(kinds) else "manual"
        ref = refs[i] if i < len(refs) else ""
        ovr = overrides[i] if i < len(overrides) else ""
        ing_id = mix_id = None
        cost_override = None
        if kind == "ingredient" and ref:
            ing_id = int(ref)
        elif kind == "spice_mix" and ref:
            mix_id = int(ref)
        else:
            cost_override = float(ovr) if str(ovr).strip() else 0.0
        db.session.add(RecipeLine(
            recipe_id=recipe.id, position=i, display_name=nm,
            mass_kg=float(masses[i] or 0) if i < len(masses) else 0,
            ingredient_id=ing_id, spice_mix_id=mix_id, cost_override=cost_override))

    # Extras.
    ex_names = form.getlist("extra_name")
    ex_vals = form.getlist("extra_value")
    for i, en in enumerate(ex_names):
        en = (en or "").strip()
        if not en:
            continue
        db.session.add(RecipeExtra(recipe_id=recipe.id, name=en,
                                   value_per_kg=float(ex_vals[i] or 0)))

    db.session.flush()
    db.session.expire(recipe)   # reload lines/extras so the snapshot is accurate
    recipe.last_cost_per_kg = recipe_cost_per_kg(recipe)
    log_action("create" if is_new else "update", "recipe", recipe.id,
               "cost_per_kg", None, round(recipe.last_cost_per_kg))
    db.session.commit()
    _resync_production()
    flash(f"Recipe '{recipe.name}' saved. Cost/kg: "
          f"{recipe.last_cost_per_kg:,.0f} {Setting.get('currency','UGX')}", "success")
    return redirect(url_for("recipes.view", rid=recipe.id))


def _resync_production():
    """Keep the prod_* copies (read by production planning and inventory
    valuation) in step with the native costing tables after every edit, so
    the two can never drift now that costing lives in the same database."""
    from services import recipe_sync
    try:
        recipe_sync.sync_from_costing()
    except Exception:
        from flask import current_app
        current_app.logger.exception("Recipe re-sync after save failed")


@recipes_bp.route("/<int:rid>/toggle", methods=["POST"])
@login_required
@editor_required
def toggle(rid):
    recipe = db.get_or_404(Recipe, rid)
    if recipe.status == "active":
        recipe.status = "inactive"
        recipe.deactivated_at = utcnow()
        recipe.deactivate_reason = request.form.get("reason") or None
        recipe.last_cost_per_kg = recipe_cost_per_kg(recipe)
        log_action("status", "recipe", recipe.id, "status", "active", "inactive")
        flash(f"'{recipe.name}' deactivated.", "info")
    else:
        recipe.status = "active"
        recipe.deactivated_at = None
        recipe.deactivate_reason = None
        recipe.last_cost_per_kg = recipe_cost_per_kg(recipe)  # recalc on reactivation
        log_action("status", "recipe", recipe.id, "status", "inactive", "active")
        flash(f"'{recipe.name}' reactivated. Cost/kg recalculated: "
              f"{recipe.last_cost_per_kg:,.0f}", "success")
    db.session.commit()
    _resync_production()
    nxt = request.form.get("next") or url_for("recipes.view", rid=recipe.id)
    return redirect(nxt)


@recipes_bp.route("/<int:rid>/copy", methods=["POST"])
@login_required
@editor_required
def copy(rid):
    """Duplicate a recipe (lines, extras, pack sizes) and open the copy for editing."""
    src = db.get_or_404(Recipe, rid)
    new = Recipe(
        name=f"{src.name} (copy)",
        category_id=src.category_id,
        status="active",
        batch_label=src.batch_label,
        casing_type=src.casing_type,
        casing_cpk=src.casing_cpk,
        casing_pct=src.casing_pct,
        overhead_override=src.overhead_override,
        packaging_cpk=src.packaging_cpk,
        note=src.note,
    )
    db.session.add(new)
    db.session.flush()
    for ln in src.lines:
        db.session.add(RecipeLine(
            recipe_id=new.id, position=ln.position, display_name=ln.display_name,
            mass_kg=ln.mass_kg, ingredient_id=ln.ingredient_id,
            spice_mix_id=ln.spice_mix_id, cost_override=ln.cost_override))
    for ex in src.extras:
        db.session.add(RecipeExtra(recipe_id=new.id, name=ex.name,
                                   value_per_kg=ex.value_per_kg))
    for ps in src.pack_sizes:
        db.session.add(PackSize(
            recipe_id=new.id, label=ps.label, pack_weight_kg=ps.pack_weight_kg,
            pieces=ps.pieces, packing_cost=ps.packing_cost))
    db.session.flush()
    db.session.expire(new)
    new.last_cost_per_kg = recipe_cost_per_kg(new)
    log_action("copy", "recipe", new.id, "name", src.name, new.name)
    db.session.commit()
    _resync_production()
    flash(f"Copied '{src.name}'. Editing the new copy now.", "success")
    return redirect(url_for("recipes.edit", rid=new.id))


@recipes_bp.route("/calc.json", methods=["POST"])
@login_required
def calc_json():
    """Real-time cost preview for the builder. No DB writes."""
    data = request.get_json(force=True)
    total_mass = 0.0
    batch = 0.0
    for line in data.get("lines", []):
        try:
            mass = float(line.get("mass") or 0)
        except ValueError:
            mass = 0.0
        kind = line.get("kind")
        ref = line.get("ref")
        cost = 0.0
        if kind == "ingredient" and ref:
            ing = db.session.get(Ingredient, int(ref))
            cost = ing.total_cost if ing else 0.0
        elif kind == "spice_mix" and ref:
            mix = db.session.get(SpiceMix, int(ref))
            cost = spice_mix_cost_per_kg(mix) if mix else 0.0
        else:
            try:
                cost = float(line.get("cost") or 0)
            except ValueError:
                cost = 0.0
        total_mass += mass
        batch += mass * cost

    mince = batch / total_mass if total_mass else 0.0
    casing = float(data.get("casing_cpk") or 0) * float(data.get("casing_pct") or 0)
    oh = data.get("overhead")
    overhead = float(oh) if str(oh).strip() else Setting.get_float("overhead_per_kg", 900)
    packaging = float(data.get("packaging") or 0)
    extras = sum(float(e or 0) for e in data.get("extras", []))
    final = mince + casing + overhead + packaging + extras
    return jsonify({
        "total_mass": total_mass, "batch": batch, "mince": mince,
        "casing": casing, "overhead": overhead, "packaging": packaging,
        "extras": extras, "final": final,
    })


@recipes_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
