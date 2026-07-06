"""Overhead cost calculator. Computes overhead UGX/kg and feeds all recipes."""
from flask import (Blueprint, render_template, request, redirect, url_for, flash)
from flask_login import login_required

from extensions import db
from services.costing_models import Setting
from services.costing_auth import editor_required, admin_required, log_action

overhead_bp = Blueprint("overhead", __name__, url_prefix="/costing/overhead")

# Calculator inputs persist in the settings store so the screen is sticky.
OH_KEYS = {
    "oh_labour": "86556023",
    "oh_electricity": "41373935",
    "oh_water": "7200000",
    "oh_volume": "141143",
    "oh_elec_prod_split": "0.80",
    "oh_water_prod_split": "0.90",
}


def _f(key):
    return Setting.get_float(key, float(OH_KEYS[key]))


def compute():
    labour = _f("oh_labour")
    electricity = _f("oh_electricity")
    water = _f("oh_water")
    volume = _f("oh_volume") or 1
    elec_split = _f("oh_elec_prod_split")
    water_split = _f("oh_water_prod_split")

    labour_pk = labour / volume
    elec_pk = (electricity * elec_split) / volume
    water_pk = (water * water_split) / volume
    total = labour_pk + elec_pk + water_pk
    return {
        "labour": labour, "electricity": electricity, "water": water,
        "volume": volume, "elec_split": elec_split, "water_split": water_split,
        "labour_pk": labour_pk, "elec_pk": elec_pk, "water_pk": water_pk,
        "total": total,
        "current_overhead": Setting.get_float("overhead_per_kg", 900),
    }


@overhead_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        return _save()
    return render_template("costing/overhead.html", d=compute())


@overhead_bp.route("/save", methods=["POST"])
@login_required
@editor_required
def _save():
    for key in OH_KEYS:
        if key in request.form:
            Setting.set(key, request.form[key])
    db.session.commit()
    d = compute()
    flash(f"Overhead recalculated: {d['total']:,.0f} UGX/kg. "
          "Click 'Apply to all recipes' to make it the active rate.", "success")
    return redirect(url_for("overhead.index"))


@overhead_bp.route("/apply", methods=["POST"])
@login_required
@admin_required
def apply():
    d = compute()
    rounded = round(d["total"])
    old = Setting.get_float("overhead_per_kg", 900)
    Setting.set("overhead_per_kg", rounded)
    log_action("update", "setting", "overhead_per_kg", "overhead_per_kg",
               round(old), rounded)
    db.session.commit()
    flash(f"Global overhead set to {rounded:,.0f} UGX/kg. All recipes updated.", "success")
    return redirect(url_for("overhead.index"))


@overhead_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
