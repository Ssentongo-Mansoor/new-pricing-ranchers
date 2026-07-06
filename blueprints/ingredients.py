"""Ingredient master: CRUD, inline price editing, price history, search."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify)
from flask_login import login_required, current_user

from extensions import db
from services.costing_models import Ingredient, PriceHistory, Recipe, utcnow
from services.costing_auth import editor_required, log_action
from services.costing_engine import recipes_using_ingredient

ingredients_bp = Blueprint("ingredients", __name__, url_prefix="/costing/ingredients")


@ingredients_bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    query = Ingredient.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            Ingredient.name.ilike(like),
            Ingredient.alias1.ilike(like),
            Ingredient.alias2.ilike(like),
        ))
    ingredients = query.order_by(Ingredient.name).all()
    return render_template("costing/ingredients.html", ingredients=ingredients, q=q)


@ingredients_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    if request.method == "POST":
        ing = Ingredient(
            name=request.form["name"].strip(),
            alias1=request.form.get("alias1") or None,
            alias2=request.form.get("alias2") or None,
            uom=request.form.get("uom") or "kg",
            base_cost=float(request.form.get("base_cost") or 0),
            tax_value=float(request.form.get("tax_value") or 0),
            clearance=float(request.form.get("clearance") or 0),
            freight=float(request.form.get("freight") or 0),
        )
        db.session.add(ing)
        db.session.flush()
        log_action("create", "ingredient", ing.id, "name", None, ing.name)
        db.session.commit()
        flash(f"Ingredient '{ing.name}' added.", "success")
        return redirect(url_for("ingredients.index"))
    return render_template("costing/ingredient_form.html", ingredient=None)


@ingredients_bp.route("/<int:iid>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(iid):
    ing = db.get_or_404(Ingredient, iid)
    if request.method == "POST":
        old_total = ing.total_cost
        old_base = ing.base_cost
        ing.name = request.form["name"].strip()
        ing.alias1 = request.form.get("alias1") or None
        ing.alias2 = request.form.get("alias2") or None
        ing.uom = request.form.get("uom") or "kg"
        ing.base_cost = float(request.form.get("base_cost") or 0)
        ing.tax_value = float(request.form.get("tax_value") or 0)
        ing.clearance = float(request.form.get("clearance") or 0)
        ing.freight = float(request.form.get("freight") or 0)
        if abs(ing.total_cost - old_total) > 1e-6:
            db.session.add(PriceHistory(
                ingredient_id=ing.id, old_cost=old_base, new_cost=ing.base_cost,
                old_total=old_total, new_total=ing.total_cost,
                changed_by=current_user.username))
            log_action("update", "ingredient", ing.id, "total_cost",
                       round(old_total), round(ing.total_cost))
        db.session.commit()
        flash(f"Ingredient '{ing.name}' updated.", "success")
        return redirect(url_for("ingredients.index"))
    return render_template("costing/ingredient_form.html", ingredient=ing)


@ingredients_bp.route("/<int:iid>/delete", methods=["POST"])
@login_required
@editor_required
def delete(iid):
    ing = db.get_or_404(Ingredient, iid)
    affected = recipes_using_ingredient(ing, Recipe.query.all())
    if affected:
        flash(f"Cannot delete '{ing.name}': used in {len(affected)} recipe(s). "
              "Remove it from those recipes first.", "danger")
        return redirect(url_for("ingredients.index"))
    log_action("delete", "ingredient", ing.id, "name", ing.name, None)
    db.session.delete(ing)
    db.session.commit()
    flash(f"Ingredient '{ing.name}' deleted.", "info")
    return redirect(url_for("ingredients.index"))


@ingredients_bp.route("/<int:iid>/history")
@login_required
def history(iid):
    ing = db.get_or_404(Ingredient, iid)
    return render_template("costing/ingredient_history.html", ingredient=ing)


@ingredients_bp.route("/<int:iid>/price", methods=["POST"])
@login_required
@editor_required
def update_price(iid):
    """Inline price edit. Returns JSON with the count of affected products."""
    ing = db.get_or_404(Ingredient, iid)
    try:
        new_base = float(request.json.get("base_cost"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid value"}), 400

    old_total = ing.total_cost
    old_base = ing.base_cost
    ing.base_cost = new_base
    if abs(ing.total_cost - old_total) > 1e-6:
        db.session.add(PriceHistory(
            ingredient_id=ing.id, old_cost=old_base, new_cost=new_base,
            old_total=old_total, new_total=ing.total_cost,
            changed_by=current_user.username))
        log_action("update", "ingredient", ing.id, "base_cost",
                   round(old_base), round(new_base))
    affected = recipes_using_ingredient(ing, Recipe.query.all())
    db.session.commit()
    return jsonify({
        "ok": True,
        "total_cost": ing.total_cost,
        "affected": len(affected),
        "ingredient_id": ing.id,
    })


@ingredients_bp.route("/search.json")
@login_required
def search_json():
    """Autocomplete endpoint used by the recipe builder."""
    q = request.args.get("q", "").strip()
    query = Ingredient.query.filter_by(active=True)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            Ingredient.name.ilike(like),
            Ingredient.alias1.ilike(like),
            Ingredient.alias2.ilike(like)))
    results = [{"id": i.id, "name": i.name, "cost": round(i.total_cost, 2),
                "type": "ingredient"}
               for i in query.order_by(Ingredient.name).limit(25)]
    return jsonify(results)


@ingredients_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
