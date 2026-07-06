"""Pricing calculator: wholesale / RRP (excl & incl VAT) per pack size."""
from flask import (Blueprint, render_template, request, redirect, url_for, flash)
from flask_login import login_required

from extensions import db
from services.costing_models import Recipe, PackSize, Setting
from services.costing_auth import editor_required, log_action
from services.costing_engine import recipe_cost_per_kg, pricing_for_cost

pricing_bp = Blueprint("pricing", __name__, url_prefix="/costing/pricing")


@pricing_bp.route("/")
@login_required
def index():
    recipes = Recipe.query.filter_by(status="active").order_by(Recipe.name).all()
    wm = Setting.get_float("wholesale_margin", 0.47)
    rm = Setting.get_float("rrp_margin", 0.15)
    vat = Setting.get_float("vat_rate", 0.18)
    rows = []
    for r in recipes:
        cost = recipe_cost_per_kg(r)
        rows.append((r, cost, pricing_for_cost(cost, wm, rm, vat)))
    return render_template("costing/pricing.html", rows=rows,
                           wm=wm * 100, rm=rm * 100, vat=vat * 100)


@pricing_bp.route("/<int:rid>", methods=["GET", "POST"])
@login_required
def detail(rid):
    recipe = db.get_or_404(Recipe, rid)
    cost = recipe_cost_per_kg(recipe)
    wm = Setting.get_float("wholesale_margin", 0.47)
    rm = Setting.get_float("rrp_margin", 0.15)
    vat = Setting.get_float("vat_rate", 0.18)
    pack_rows = []
    for ps in recipe.pack_sizes:
        per_kg_packing = (ps.packing_cost or 0) / ps.pack_weight_kg if ps.pack_weight_kg else 0
        pricing = pricing_for_cost(cost + per_kg_packing, wm, rm, vat)
        # Scale to the pack (per-pack prices).
        pack = {k: (v * ps.pack_weight_kg if k != "margin_pct" else v)
                for k, v in pricing.items()}
        pack_rows.append((ps, pricing, pack))
    return render_template("costing/pricing_detail.html", recipe=recipe, cost=cost,
                           pack_rows=pack_rows, wm=wm * 100, rm=rm * 100, vat=vat * 100)


@pricing_bp.route("/<int:rid>/packsize", methods=["POST"])
@login_required
@editor_required
def add_packsize(rid):
    recipe = db.get_or_404(Recipe, rid)
    ps = PackSize(
        recipe_id=recipe.id,
        label=request.form.get("label", "").strip() or "Pack",
        pack_weight_kg=float(request.form.get("pack_weight_kg") or 1),
        pieces=int(request.form["pieces"]) if request.form.get("pieces") else None,
        packing_cost=float(request.form.get("packing_cost") or 0),
    )
    db.session.add(ps)
    log_action("create", "pack_size", recipe.id, "label", None, ps.label)
    db.session.commit()
    flash(f"Pack size '{ps.label}' added.", "success")
    return redirect(url_for("pricing.detail", rid=recipe.id))


@pricing_bp.route("/<int:rid>/packsize/<int:psid>/delete", methods=["POST"])
@login_required
@editor_required
def delete_packsize(rid, psid):
    ps = db.get_or_404(PackSize, psid)
    db.session.delete(ps)
    log_action("delete", "pack_size", rid, "label", ps.label, None)
    db.session.commit()
    flash("Pack size removed.", "info")
    return redirect(url_for("pricing.detail", rid=rid))


@pricing_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
