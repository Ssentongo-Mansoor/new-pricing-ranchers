"""Carcass cuts: yield / cut-out costing that feeds the ingredient master."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify)
from flask_login import login_required, current_user

from extensions import db
from services.costing_models import (Carcass, CarcassCost, Cut, Ingredient, PriceHistory,
                    Recipe, SPECIES)
from services.costing_auth import editor_required, log_action
from services.costing_engine import carcass_breakdown, recipes_using_ingredient

cuts_bp = Blueprint("cuts", __name__, url_prefix="/costing/cuts")

# Typical cuts per species, used to pre-fill a new carcass form (fully editable).
DEFAULT_CUTS = {
    "Pork": ["Pork Belly", "Pork Shoulder", "Pork Leg / Ham", "Pork Loin",
             "Pork Ribs", "Pork Trimmings", "Pork Back Fat", "Bone / Loss"],
    "Beef": ["Topside / Silverside", "Rump", "Sirloin", "Chuck", "Brisket",
             "Ribs", "Shin", "Beef Trimmings", "Beef Fat", "Bone / Loss"],
    "Chicken": ["Breast Fillet", "Thighs", "Drumsticks", "Wings", "Carcass / MDM",
                "Skin", "Loss"],
    "Lamb": ["Leg", "Shoulder", "Loin / Chops", "Rack", "Breast",
             "Lamb Trimmings", "Bone / Loss"],
    "Goat": ["Leg", "Shoulder", "Loin / Chops", "Ribs", "Goat Trimmings",
             "Bone / Loss"],
}


@cuts_bp.route("/")
@login_required
def index():
    carcasses = Carcass.query.order_by(Carcass.created_at.desc()).all()
    rows = [(c, carcass_breakdown(c)) for c in carcasses]
    return render_template("costing/cuts.html", rows=rows)


@cuts_bp.route("/<int:cid>")
@login_required
def view(cid):
    carcass = db.get_or_404(Carcass, cid)
    b = carcass_breakdown(carcass)
    return render_template("costing/cut_view.html", carcass=carcass, b=b)


@cuts_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    if request.method == "POST":
        return _save(None)
    return render_template("costing/cut_form.html", carcass=None, species=SPECIES,
                           default_cuts=DEFAULT_CUTS,
                           ingredients=Ingredient.query.order_by(Ingredient.name).all())


@cuts_bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(cid):
    carcass = db.get_or_404(Carcass, cid)
    if request.method == "POST":
        return _save(carcass)
    return render_template("costing/cut_form.html", carcass=carcass, species=SPECIES,
                           default_cuts=DEFAULT_CUTS,
                           ingredients=Ingredient.query.order_by(Ingredient.name).all())


def _save(carcass):
    form = request.form
    is_new = carcass is None
    if is_new:
        carcass = Carcass(label=form["label"].strip())
        db.session.add(carcass)
        db.session.flush()

    carcass.label = form["label"].strip()
    carcass.species = form.get("species", "Beef")
    carcass.carcass_weight_kg = float(form.get("carcass_weight_kg") or 0)
    carcass.purchase_cost = float(form.get("purchase_cost") or 0)
    carcass.processing_fee_per_kg = float(form.get("processing_fee_per_kg") or 0)
    carcass.injection_pct = float(form.get("injection_pct") or 0)
    carcass.allocation_method = form.get("allocation_method", "value")
    carcass.note = form.get("note") or None

    for x in list(carcass.costs):
        db.session.delete(x)
    for x in list(carcass.cuts):
        db.session.delete(x)
    db.session.flush()

    # Extra landed costs
    cnames = form.getlist("cost_name")
    camounts = form.getlist("cost_amount")
    for i, nm in enumerate(cnames):
        nm = (nm or "").strip()
        if not nm:
            continue
        db.session.add(CarcassCost(carcass_id=carcass.id, name=nm,
                                   amount=float(camounts[i] or 0)))

    # Cuts
    names = form.getlist("cut_name")
    weights = form.getlist("cut_weight")
    sells = form.getlist("cut_sell")
    exports = form.getlist("cut_export")
    injectables = form.getlist("cut_injectable")  # hidden "1"/"0" per row, kept aligned by JS
    ings = form.getlist("cut_ingredient")
    for i, nm in enumerate(names):
        nm = (nm or "").strip()
        if not nm:
            continue
        ing_id = ings[i] if i < len(ings) else ""
        db.session.add(Cut(
            carcass_id=carcass.id, position=i, name=nm,
            weight_kg=float(weights[i] or 0) if i < len(weights) else 0,
            selling_price=float(sells[i] or 0) if i < len(sells) else 0,
            export_price=float(exports[i] or 0) if i < len(exports) else 0,
            injectable=(injectables[i] == "1") if i < len(injectables) else False,
            ingredient_id=int(ing_id) if str(ing_id).strip() else None))

    log_action("create" if is_new else "update", "carcass", carcass.id, "label", None, carcass.label)
    db.session.commit()
    flash(f"Carcass '{carcass.label}' saved.", "success")
    return redirect(url_for("cuts.view", cid=carcass.id))


@cuts_bp.route("/<int:cid>/delete", methods=["POST"])
@login_required
@editor_required
def delete(cid):
    carcass = db.get_or_404(Carcass, cid)
    log_action("delete", "carcass", carcass.id, "label", carcass.label, None)
    db.session.delete(carcass)
    db.session.commit()
    flash(f"Carcass '{carcass.label}' deleted.", "info")
    return redirect(url_for("cuts.index"))


@cuts_bp.route("/<int:cid>/push", methods=["POST"])
@login_required
@editor_required
def push(cid):
    """Push selected cut costs to their linked ingredients (confirm step)."""
    carcass = db.get_or_404(Carcass, cid)
    b = carcass_breakdown(carcass)
    selected = set(request.form.getlist("cut_id"))
    updated = 0
    affected_recipes = set()
    for row in b["rows"]:
        cut = row["cut"]
        if str(cut.id) not in selected or cut.ingredient_id is None:
            continue
        ing = db.session.get(Ingredient, cut.ingredient_id)
        if ing is None:
            continue
        old_base = ing.base_cost
        old_total = ing.total_cost
        new_base = round(row["cost_per_kg"], 4)
        if abs(new_base - old_base) < 1e-6:
            continue
        ing.base_cost = new_base
        db.session.add(PriceHistory(
            ingredient_id=ing.id, old_cost=old_base, new_cost=new_base,
            old_total=old_total, new_total=ing.total_cost,
            changed_by=current_user.username))
        log_action("update", "ingredient", ing.id, "base_cost (from carcass)",
                   round(old_base), round(new_base))
        for r in recipes_using_ingredient(ing, Recipe.query.all()):
            affected_recipes.add(r.id)
        updated += 1
    db.session.commit()
    flash(f"Updated {updated} ingredient price(s) from carcass cuts. "
          f"{len(affected_recipes)} recipe(s) recalculated.", "success")
    return redirect(url_for("cuts.view", cid=carcass.id))


@cuts_bp.route("/calc.json", methods=["POST"])
@login_required
def calc_json():
    """Live breakdown preview for the builder. No DB writes."""
    data = request.get_json(force=True)
    method = data.get("method", "value")
    carcass_weight = float(data.get("carcass_weight") or 0)
    fee = float(data.get("processing_fee") or 0)
    inj = float(data.get("injection_pct") or 0)
    landed = float(data.get("purchase_cost") or 0)
    landed += sum(float(x or 0) for x in data.get("extra_costs", []))

    cuts = data.get("cuts", [])
    cut_weight = sum(float(c.get("weight") or 0) for c in cuts)
    total_cost = landed + fee * cut_weight
    total_value = sum(float(c.get("weight") or 0) * float(c.get("sell") or 0) for c in cuts)
    use_value = method == "value" and total_value > 0
    uniform = (total_cost / cut_weight) if cut_weight else 0.0

    rows = []
    revenue = 0.0
    export_revenue = 0.0
    injected_revenue = 0.0
    has_export = False
    for c in cuts:
        w = float(c.get("weight") or 0)
        sell = float(c.get("sell") or 0)
        exp = float(c.get("export") or 0)
        injectable = bool(c.get("injectable"))
        if exp > 0:
            has_export = True
        cpk = ((landed / total_value * sell) + fee) if use_value else uniform
        inj_w = w * (1 + inj) if injectable else w
        revenue += w * sell
        export_revenue += w * (exp if exp > 0 else sell)
        injected_revenue += inj_w * sell
        rows.append({
            "cpk": cpk, "cut_cost": cpk * w,
            "yield_pct": (w / carcass_weight * 100) if carcass_weight else 0,
            "margin": sell - cpk,
            "margin_pct": ((sell - cpk) / sell * 100) if sell else 0,
        })
    loss = carcass_weight - cut_weight
    return jsonify({
        "landed": landed, "total_cost": total_cost,
        "cut_weight": cut_weight, "loss": loss,
        "loss_pct": (loss / carcass_weight * 100) if carcass_weight else 0,
        "avg_cpk": uniform,
        "rows": rows, "revenue": revenue, "profit": revenue - total_cost,
        "profit_pct": ((revenue - total_cost) / revenue * 100) if revenue else 0,
        "has_export": has_export, "export_revenue": export_revenue,
        "export_profit": export_revenue - total_cost,
        "export_profit_pct": ((export_revenue - total_cost) / export_revenue * 100) if export_revenue else 0,
        "injected": inj > 0, "injected_revenue": injected_revenue,
        "injected_profit": injected_revenue - total_cost,
        "injected_profit_pct": ((injected_revenue - total_cost) / injected_revenue * 100) if injected_revenue else 0,
        "used_value": use_value,
    })


@cuts_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
