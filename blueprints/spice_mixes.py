"""Spice mixes / sub-recipes: list, view, create, edit, delete."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify)
from flask_login import login_required

from extensions import db
from services.costing_models import SpiceMix, SpiceMixLine, Ingredient, RecipeLine
from services.costing_auth import editor_required, log_action
from services.costing_engine import spice_mix_cost_per_kg, spice_mix_line_cost

spice_bp = Blueprint("spice_mixes", __name__, url_prefix="/costing/spice-mixes")


@spice_bp.route("/")
@login_required
def index():
    mixes = SpiceMix.query.order_by(SpiceMix.name).all()
    rows = [(m, spice_mix_cost_per_kg(m)) for m in mixes]
    return render_template("costing/spice_mixes.html", rows=rows)


@spice_bp.route("/<int:mid>")
@login_required
def view(mid):
    mix = db.get_or_404(SpiceMix, mid)
    lines = [(l, spice_mix_line_cost(l), (l.mass_kg or 0) * spice_mix_line_cost(l))
             for l in mix.lines]
    total_mass = sum(l.mass_kg or 0 for l in mix.lines)
    batch = sum(x[2] for x in lines)
    return render_template("costing/spice_mix_view.html", mix=mix, lines=lines,
                           total_mass=total_mass, batch=batch,
                           cost_per_kg=spice_mix_cost_per_kg(mix))


@spice_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    if request.method == "POST":
        return _save(None)
    return render_template("costing/spice_mix_form.html", mix=None,
                           ingredients=Ingredient.query.order_by(Ingredient.name).all())


@spice_bp.route("/<int:mid>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(mid):
    mix = db.get_or_404(SpiceMix, mid)
    if request.method == "POST":
        return _save(mix)
    return render_template("costing/spice_mix_form.html", mix=mix,
                           ingredients=Ingredient.query.order_by(Ingredient.name).all())


def _save(mix):
    name = request.form["name"].strip()
    note = request.form.get("note") or None
    is_new = mix is None
    if is_new:
        mix = SpiceMix(name=name, note=note)
        db.session.add(mix)
        db.session.flush()
    else:
        mix.name = name
        mix.note = note
        for ln in list(mix.lines):
            db.session.delete(ln)
        db.session.flush()

    names = request.form.getlist("line_name")
    masses = request.form.getlist("line_mass")
    ing_ids = request.form.getlist("line_ingredient")
    overrides = request.form.getlist("line_cost")
    for i, nm in enumerate(names):
        nm = nm.strip()
        if not nm:
            continue
        ing_id = ing_ids[i] if i < len(ing_ids) else ""
        ing_id = int(ing_id) if ing_id.strip() else None
        ovr = overrides[i] if i < len(overrides) else ""
        db.session.add(SpiceMixLine(
            spice_mix_id=mix.id, position=i, display_name=nm,
            mass_kg=float(masses[i] or 0) if i < len(masses) else 0,
            ingredient_id=ing_id,
            cost_override=(float(ovr) if (ovr.strip() and not ing_id) else None)))
    log_action("create" if is_new else "update", "spice_mix", mix.id, "name", None, name)
    db.session.commit()
    flash(f"Spice mix '{name}' saved.", "success")
    return redirect(url_for("spice_mixes.view", mid=mix.id))


@spice_bp.route("/<int:mid>/delete", methods=["POST"])
@login_required
@editor_required
def delete(mid):
    mix = db.get_or_404(SpiceMix, mid)
    used = RecipeLine.query.filter_by(spice_mix_id=mix.id).count()
    if used:
        flash(f"Cannot delete '{mix.name}': used in {used} recipe line(s).", "danger")
        return redirect(url_for("spice_mixes.view", mid=mid))
    log_action("delete", "spice_mix", mix.id, "name", mix.name, None)
    db.session.delete(mix)
    db.session.commit()
    flash(f"Spice mix '{mix.name}' deleted.", "info")
    return redirect(url_for("spice_mixes.index"))


@spice_bp.route("/search.json")
@login_required
def search_json():
    q = request.args.get("q", "").strip()
    query = SpiceMix.query
    if q:
        query = query.filter(SpiceMix.name.ilike(f"%{q}%"))
    return jsonify([{"id": m.id, "name": m.name,
                     "cost": round(spice_mix_cost_per_kg(m), 2), "type": "spice_mix"}
                    for m in query.order_by(SpiceMix.name).limit(25)])


@spice_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
