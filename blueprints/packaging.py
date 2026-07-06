"""Packaging cost calculator: define packaging configs and per-kg totals."""
from flask import (Blueprint, render_template, request, redirect, url_for, flash)
from flask_login import login_required

from extensions import db
from services.costing_models import PackagingConfig, PackagingItem, Setting
from services.costing_auth import editor_required, admin_required, log_action

packaging_bp = Blueprint("packaging", __name__, url_prefix="/costing/packaging")


@packaging_bp.route("/")
@login_required
def index():
    configs = PackagingConfig.query.order_by(PackagingConfig.name).all()
    return render_template("costing/packaging.html", configs=configs,
                           default_packaging=Setting.get_float("default_packaging", 1405.69))


@packaging_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    if request.method == "POST":
        return _save(None)
    return render_template("costing/packaging_form.html", config=None)


@packaging_bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(cid):
    config = db.get_or_404(PackagingConfig, cid)
    if request.method == "POST":
        return _save(config)
    return render_template("costing/packaging_form.html", config=config)


def _save(config):
    name = request.form["name"].strip()
    note = request.form.get("note") or None
    is_new = config is None
    if is_new:
        config = PackagingConfig(name=name, note=note)
        db.session.add(config)
        db.session.flush()
    else:
        config.name = name
        config.note = note
        for it in list(config.items):
            db.session.delete(it)
        db.session.flush()

    mats = request.form.getlist("material")
    prices = request.form.getlist("unit_price")
    for i, m in enumerate(mats):
        m = (m or "").strip()
        if not m:
            continue
        db.session.add(PackagingItem(config_id=config.id, material=m,
                                     unit_price=float(prices[i] or 0)))
    log_action("create" if is_new else "update", "packaging", config.id, "name", None, name)
    db.session.commit()
    flash(f"Packaging config '{name}' saved ({config.total_per_kg:,.0f} UGX/kg).", "success")
    return redirect(url_for("packaging.index"))


@packaging_bp.route("/<int:cid>/delete", methods=["POST"])
@login_required
@editor_required
def delete(cid):
    config = db.get_or_404(PackagingConfig, cid)
    log_action("delete", "packaging", config.id, "name", config.name, None)
    db.session.delete(config)
    db.session.commit()
    flash("Packaging config deleted.", "info")
    return redirect(url_for("packaging.index"))


@packaging_bp.route("/<int:cid>/set-default", methods=["POST"])
@login_required
@admin_required
def set_default(cid):
    config = db.get_or_404(PackagingConfig, cid)
    Setting.set("default_packaging", round(config.total_per_kg, 2))
    log_action("update", "setting", "default_packaging", "default_packaging",
               None, round(config.total_per_kg))
    db.session.commit()
    flash(f"Default packaging set to {config.total_per_kg:,.0f} UGX/kg "
          f"from '{config.name}'.", "success")
    return redirect(url_for("packaging.index"))


@packaging_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
