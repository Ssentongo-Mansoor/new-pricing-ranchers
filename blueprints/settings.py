"""Global settings (admin) and category management (admin/manager)."""
from flask import (Blueprint, render_template, request, redirect, url_for, flash)
from flask_login import login_required

from extensions import db
from services.costing_models import Setting, Category, Recipe
from services.costing_auth import admin_required, editor_required, log_action

settings_bp = Blueprint("settings", __name__, url_prefix="/costing/settings")

# (key, label, is_percent) — percents stored as fractions, shown as %.
GLOBAL_FIELDS = [
    ("overhead_per_kg", "Overhead (UGX/kg)", False),
    ("default_packaging", "Default packaging (UGX/kg)", False),
    ("vat_rate", "VAT rate", True),
    ("wholesale_margin", "Wholesale margin", True),
    ("rrp_margin", "RRP margin (on wholesale)", True),
    ("margin_threshold_low", "Margin red threshold (%)", False),
    ("margin_threshold_high", "Margin green threshold (%)", False),
    ("currency", "Currency code", None),
]


@settings_bp.route("/")
@login_required
def index():
    values = {}
    for key, _label, is_pct in GLOBAL_FIELDS:
        v = Setting.get(key)
        if is_pct:
            try:
                v = round(float(v) * 100, 2)
            except (TypeError, ValueError):
                pass
        values[key] = v
    categories = Category.query.order_by(Category.display_order).all()
    return render_template("costing/settings.html", fields=GLOBAL_FIELDS, values=values,
                           categories=categories)


@settings_bp.route("/save", methods=["POST"])
@login_required
@admin_required
def save():
    for key, _label, is_pct in GLOBAL_FIELDS:
        if key not in request.form:
            continue
        raw = request.form[key].strip()
        old = Setting.get(key)
        if is_pct:
            try:
                raw = str(float(raw) / 100)
            except ValueError:
                continue
        Setting.set(key, raw)
        if str(old) != str(raw):
            log_action("update", "setting", key, key, old, raw)
    db.session.commit()
    flash("Settings saved. All recipes and prices updated.", "success")
    return redirect(url_for("settings.index"))


# ---- Categories ---------------------------------------------------------- #
@settings_bp.route("/categories/new", methods=["POST"])
@login_required
@editor_required
def category_new():
    name = request.form["name"].strip()
    if not name:
        flash("Category name required.", "danger")
        return redirect(url_for("settings.index"))
    if Category.query.filter(db.func.lower(Category.name) == name.lower()).first():
        flash("A category with that name already exists.", "warning")
        return redirect(url_for("settings.index"))
    order = (db.session.query(db.func.max(Category.display_order)).scalar() or 0) + 1
    cat = Category(name=name, display_order=order)
    db.session.add(cat)
    log_action("create", "category", None, "name", None, name)
    db.session.commit()
    flash(f"Category '{name}' added.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/categories/<int:cid>/rename", methods=["POST"])
@login_required
@editor_required
def category_rename(cid):
    cat = db.get_or_404(Category, cid)
    new_name = request.form["name"].strip()
    if new_name:
        old = cat.name
        cat.name = new_name
        log_action("update", "category", cat.id, "name", old, new_name)
        db.session.commit()
        flash(f"Category renamed to '{new_name}'. All products updated.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/categories/<int:cid>/move", methods=["POST"])
@login_required
@editor_required
def category_move(cid):
    cat = db.get_or_404(Category, cid)
    direction = request.form.get("dir")
    ordered = Category.query.order_by(Category.display_order).all()
    i = ordered.index(cat)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ordered):
        ordered[i].display_order, ordered[j].display_order = \
            ordered[j].display_order, ordered[i].display_order
        db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/categories/<int:cid>/merge", methods=["POST"])
@login_required
@editor_required
def category_merge(cid):
    src = db.get_or_404(Category, cid)
    target_id = request.form.get("target_id")
    if not target_id:
        flash("Choose a target category to merge into.", "warning")
        return redirect(url_for("settings.index"))
    target = db.get_or_404(Category, int(target_id))
    if target.id == src.id:
        flash("Cannot merge a category into itself.", "warning")
        return redirect(url_for("settings.index"))
    for r in Recipe.query.filter_by(category_id=src.id).all():
        r.category_id = target.id
    log_action("update", "category", src.id, "merge", src.name, target.name)
    db.session.delete(src)
    db.session.commit()
    flash(f"Merged '{src.name}' into '{target.name}'.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/categories/<int:cid>/delete", methods=["POST"])
@login_required
@editor_required
def category_delete(cid):
    cat = db.get_or_404(Category, cid)
    active = Recipe.query.filter_by(category_id=cat.id, status="active").count()
    if active:
        flash(f"Cannot delete '{cat.name}': {active} active product(s) assigned. "
              "Reassign or deactivate them first.", "danger")
        return redirect(url_for("settings.index"))
    # Detach any inactive recipes.
    for r in Recipe.query.filter_by(category_id=cat.id).all():
        r.category_id = None
    log_action("delete", "category", cat.id, "name", cat.name, None)
    db.session.delete(cat)
    db.session.commit()
    flash(f"Category '{cat.name}' deleted.", "info")
    return redirect(url_for("settings.index"))


@settings_bp.before_request
def _costing_gate():
    from flask_login import current_user
    from flask import abort
    if not current_user.is_authenticated:
        abort(401)
    from services.costing_auth import require_costing_view
    require_costing_view()
