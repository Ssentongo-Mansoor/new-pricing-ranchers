"""What-if impact analysis: change an ingredient price, see affected products."""
from flask import (Blueprint, render_template, request, jsonify)
from flask_login import login_required

from extensions import db
from services.costing_models import Ingredient, Recipe, Setting
from services.costing_engine import whatif_impact, pricing_for_cost

whatif_bp = Blueprint("whatif", __name__, url_prefix="/costing/what-if")


@whatif_bp.route("/")
@login_required
def index():
    ingredients = Ingredient.query.order_by(Ingredient.name).all()
    return render_template("costing/whatif.html", ingredients=ingredients)


@whatif_bp.route("/run", methods=["POST"])
@login_required
def run():
    data = request.get_json(force=True)
    ing = db.session.get(Ingredient, int(data["ingredient_id"]))
    if ing is None:
        return jsonify({"ok": False, "error": "Ingredient not found"}), 404
    try:
        new_total = float(data["new_cost"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid price"}), 400

    recipes = Recipe.query.filter_by(status="active").all()
    impact = whatif_impact(ing, new_total, recipes)

    wm = Setting.get_float("wholesale_margin", 0.47)
    rm = Setting.get_float("rrp_margin", 0.15)
    vat = Setting.get_float("vat_rate", 0.18)

    out_rows = []
    for recipe, before, after, delta, pct in impact["rows"]:
        m_before = pricing_for_cost(before, wm, rm, vat)["margin_pct"]
        m_after = pricing_for_cost(after, wm, rm, vat)["margin_pct"]
        out_rows.append({
            "name": recipe.name,
            "category": recipe.category.name if recipe.category else "—",
            "before": before, "after": after, "delta": delta, "pct": pct,
            "margin_before": m_before, "margin_after": m_after,
            "margin_delta": m_after - m_before,
        })
    out_rows.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return jsonify({
        "ok": True,
        "ingredient": ing.name,
        "old_total": impact["old_total"],
        "new_total": impact["new_total"],
        "count": impact["count"],
        "rows": out_rows,
    })


@whatif_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
