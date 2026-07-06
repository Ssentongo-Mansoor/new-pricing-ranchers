"""Production planning (Phase 1) — replenishment.

Orders are served from stock. Production tops stock back up. Screens:

  /production/            To produce now: products where open-order demand
                          exceeds stock, with a suggested quantity and a record
                          control (the Production Manager's main screen).
  /production/product/<id>  One product: demand breakdown by order, production
                          history, and the record control.
  /production/orders      Open orders with coverage: can each be served from
                          stock now, or is it waiting on production.
  /production/stock       Per product: on hand, open-order demand, shortfall.

Permissions are enforced on the server:
  view_production    open any production screen
  record_production  record produced goods, which adds stock (Production Manager)
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import SalesOrder, Product, ProdRecipe, ProdRecipeMap
from services.permissions import has_perm
from services import production as prod
from services import recipes as rec
from services import recipe_sync

bp = Blueprint("production", __name__, url_prefix="/production")


@bp.before_request
@login_required
def _guard():
    if not has_perm(current_user, "view_production"):
        abort(403)


def _require(cap):
    if not has_perm(current_user, cap):
        abort(403)


@bp.route("/")
def index():
    rows = prod.to_produce_list()
    can_record = has_perm(current_user, "record_production")
    return render_template("production/index.html", rows=rows,
                           can_record=can_record)


@bp.route("/product/<int:product_id>")
def product_detail(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)
    row = prod.product_row(product)
    history = prod.recent_production(product_id)
    can_record = has_perm(current_user, "record_production")
    # Phase 2: recipe + explosion for the current shortfall (if a recipe is linked).
    mapping = db.session.scalar(
        db.select(ProdRecipeMap).filter_by(product_id=product_id))
    explosion = None
    if mapping is not None:
        qty = row["shortfall"] if row["shortfall"] > 0 else row["demand"]
        explosion = rec.explode(product, qty, mapping.recipe_id)
    all_recipes = db.session.scalars(
        db.select(ProdRecipe).order_by(ProdRecipe.name)).all()
    suggested = None if mapping else rec.propose_for_product(product)
    return render_template("production/product_detail.html", row=row,
                           history=history, can_record=can_record,
                           mapping=mapping, explosion=explosion,
                           all_recipes=all_recipes, suggested=suggested,
                           can_map=has_perm(current_user, "record_production"))


@bp.route("/recipes")
def recipes():
    cmap = rec.confirmed_map()
    mapped = []
    for pid, m in cmap.items():
        p = db.session.get(Product, pid)
        if p is not None:
            mapped.append({"product": p, "map": m})
    mapped.sort(key=lambda r: r["product"].description.lower())
    props = rec.proposals()
    all_recipes = db.session.scalars(
        db.select(ProdRecipe).order_by(ProdRecipe.name)).all()
    can_map = has_perm(current_user, "record_production")
    return render_template("production/recipes.html", mapped=mapped,
                           proposals=props, all_recipes=all_recipes,
                           can_map=can_map, last_synced=recipe_sync.last_synced(),
                           recipe_count=recipe_sync.recipe_count())


@bp.route("/recipes/sync", methods=["POST"])
def recipe_sync_now():
    _require("record_production")
    try:
        counts = recipe_sync.sync_from_costing(current_user)
        flash("Recipes synced from costing: "
              + ", ".join(f"{k} {v}" for k, v in counts.items()) + ".", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Sync failed: {e}. Check the costing database is reachable.", "danger")
    return redirect(url_for("production.recipes"))


@bp.route("/recipes/map", methods=["POST"])
def recipe_map():
    _require("record_production")
    try:
        pid = int(request.form.get("product_id"))
        rid = int(request.form.get("recipe_id"))
    except (TypeError, ValueError):
        flash("Pick a product and a recipe.", "danger")
        return redirect(url_for("production.recipes"))
    ok, msg = rec.set_map(pid, rid, current_user, method="manual")
    flash(msg, "success" if ok else "danger")
    return redirect(request.form.get("next") or url_for("production.recipes"))


@bp.route("/recipes/unmap", methods=["POST"])
def recipe_unmap():
    _require("record_production")
    try:
        pid = int(request.form.get("product_id"))
    except (TypeError, ValueError):
        abort(400)
    ok, msg = rec.unmap(pid)
    flash(msg, "success" if ok else "danger")
    return redirect(request.form.get("next") or url_for("production.recipes"))


@bp.route("/recipes/confirm-all", methods=["POST"])
def recipe_confirm_all():
    _require("record_production")
    n = rec.confirm_all_proposals(current_user)
    flash(f"Confirmed {n} name-matched mapping(s).", "success")
    return redirect(url_for("production.recipes"))


@bp.route("/materials")
def materials():
    shortfalls = prod.to_produce_list()
    needs, unmapped = rec.materials_requirement(shortfalls)
    return render_template("production/materials.html", needs=needs,
                           unmapped=unmapped, n_products=len(shortfalls))


@bp.route("/product/<int:product_id>/produce", methods=["POST"])
def produce(product_id):
    _require("record_production")
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)
    # Batch/lot traceability (QA audit 5 Jul 2026): optional lot + expiry.
    expiry = None
    raw_exp = (request.form.get("expiry") or "").strip()
    if raw_exp:
        try:
            from datetime import date
            expiry = date.fromisoformat(raw_exp)
        except ValueError:
            flash("Expiry date not understood; recorded without it.", "warning")
    ok, msg = prod.record_production(
        product, request.form.get("qty"), current_user,
        note=(request.form.get("note") or "").strip() or None,
        lot_number=(request.form.get("lot_number") or "").strip() or None,
        expiry=expiry)
    flash(msg, "success" if ok else "danger")
    return redirect(request.form.get("next") or url_for("production.index"))


@bp.route("/orders")
def orders():
    rows = prod.open_orders_coverage()
    return render_template("production/orders.html", rows=rows)


@bp.route("/stock")
def stock():
    rows = prod.stock_overview()
    return render_template("production/stock.html", rows=rows)
