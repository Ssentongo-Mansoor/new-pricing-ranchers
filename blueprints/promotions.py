"""Promotions / updates posted to the customer portal, targeted by audience."""
import os
from datetime import datetime, date

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, current_app, send_file)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from extensions import db
from models import Announcement
from services.security import manager_required
from services.audit import log

bp = Blueprint("promotions", __name__, url_prefix="/promotions")
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _save_image(file):
    if not file or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in _IMG_EXT:
        return None
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "promos")
    os.makedirs(folder, exist_ok=True)
    safe = f"{datetime.utcnow():%Y%m%d%H%M%S}_{secure_filename(file.filename)}"
    file.save(os.path.join(folder, safe))
    return safe


@bp.route("/")
@login_required
@manager_required
def index():
    items = db.session.scalars(
        db.select(Announcement).order_by(Announcement.created_at.desc())).all()
    return render_template("promotions/index.html", items=items, today=date.today())


@bp.route("/new", methods=["GET", "POST"])
@login_required
@manager_required
def new():
    if request.method == "POST":
        a = Announcement(
            title=(request.form.get("title") or "").strip(),
            body=request.form.get("body"),
            audience=request.form.get("audience", "all"),
            is_active=bool(request.form.get("is_active")),
            valid_from=_parse_date(request.form.get("valid_from")),
            valid_until=_parse_date(request.form.get("valid_until")),
            image_filename=_save_image(request.files.get("image")),
            created_by=current_user.id)
        if not a.title:
            flash("A title is required.", "danger")
            return render_template("promotions/edit.html", a=None, form=request.form)
        db.session.add(a)
        log("promo_create", "announcement", None, detail=a.title)
        db.session.commit()
        flash("Promotion posted.", "success")
        return redirect(url_for("promotions.index"))
    return render_template("promotions/edit.html", a=None, form={})


@bp.route("/<int:promo_id>/edit", methods=["GET", "POST"])
@login_required
@manager_required
def edit(promo_id):
    a = db.session.get(Announcement, promo_id)
    if a is None:
        abort(404)
    if request.method == "POST":
        a.title = (request.form.get("title") or a.title).strip()
        a.body = request.form.get("body")
        a.audience = request.form.get("audience", a.audience)
        a.is_active = bool(request.form.get("is_active"))
        a.valid_from = _parse_date(request.form.get("valid_from"))
        a.valid_until = _parse_date(request.form.get("valid_until"))
        new_img = _save_image(request.files.get("image"))
        if new_img:
            a.image_filename = new_img
        log("promo_edit", "announcement", a.id, detail=a.title)
        db.session.commit()
        flash("Promotion updated.", "success")
        return redirect(url_for("promotions.index"))
    return render_template("promotions/edit.html", a=a, form={})


@bp.route("/<int:promo_id>/delete", methods=["POST"])
@login_required
@manager_required
def delete(promo_id):
    a = db.session.get(Announcement, promo_id)
    if a is None:
        abort(404)
    db.session.delete(a)
    log("promo_delete", "announcement", promo_id, detail=a.title)
    db.session.commit()
    flash("Promotion deleted.", "success")
    return redirect(url_for("promotions.index"))


@bp.route("/image/<int:promo_id>")
@login_required
def image(promo_id):
    a = db.session.get(Announcement, promo_id)
    if a is None or not a.image_filename:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], "promos", a.image_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)
