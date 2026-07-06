"""Product catalogue: a single PDF the company uploads and replaces over time.

Any logged-in user (staff or customer portal) opens it. Admins upload and
replace it. The file lives in instance/uploads/catalogue and the current
filename, label, and update date are stored as global settings, so a redeploy
of code never touches it.
"""
import os
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, current_app, send_file)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from extensions import db
from services import settings as settings_svc
from services.security import admin_required
from services.audit import log

bp = Blueprint("catalogue", __name__, url_prefix="/catalogue")

FN_KEY = "catalogue_filename"
LABEL_KEY = "catalogue_label"
UPDATED_KEY = "catalogue_updated"


def _folder():
    return os.path.join(current_app.config["UPLOAD_DIR"], "catalogue")


def _current():
    fn = settings_svc.get(FN_KEY, "")
    if not fn:
        return None
    path = os.path.join(_folder(), fn)
    if not os.path.exists(path):
        return None
    return path


@bp.route("/")
@login_required
def view():
    ctx = dict(
        has_file=_current() is not None,
        label=settings_svc.get(LABEL_KEY, "Product catalogue"),
        updated=settings_svc.get(UPDATED_KEY, ""),
    )
    if getattr(current_user, "is_customer_user", False):
        return render_template("portal/catalogue.html", **ctx)
    return render_template("catalogue.html", **ctx)


@bp.route("/file")
@login_required
def file():
    path = _current()
    if not path:
        abort(404)
    return send_file(path, mimetype="application/pdf")


@bp.route("/download")
@login_required
def download():
    path = _current()
    if not path:
        abort(404)
    label = settings_svc.get(LABEL_KEY, "catalogue")
    name = secure_filename(label if label.lower().endswith(".pdf") else label + ".pdf")
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                     download_name=name or "catalogue.pdf")


@bp.route("/manage", methods=["GET", "POST"])
@login_required
@admin_required
def manage():
    if request.method == "POST":
        if request.form.get("remove"):
            settings_svc.set_value(FN_KEY, "")
            settings_svc.set_value(LABEL_KEY, "")
            settings_svc.set_value(UPDATED_KEY, "")
            db.session.commit()
            log("catalogue_remove", "setting", None, detail="catalogue removed", commit=True)
            flash("Catalogue removed.", "success")
            return redirect(url_for("catalogue.manage"))

        file = request.files.get("file")
        if not file or not file.filename.lower().endswith(".pdf"):
            flash("Please choose a PDF file.", "danger")
            return redirect(url_for("catalogue.manage"))
        os.makedirs(_folder(), exist_ok=True)
        stored = f"catalogue_{datetime.utcnow():%Y%m%d%H%M%S}.pdf"
        file.save(os.path.join(_folder(), secure_filename(stored)))
        settings_svc.set_value(FN_KEY, stored)
        label = (request.form.get("label") or "").strip() or os.path.splitext(file.filename)[0]
        settings_svc.set_value(LABEL_KEY, label)
        settings_svc.set_value(UPDATED_KEY, datetime.utcnow().strftime("%d %b %Y"))
        db.session.commit()
        log("catalogue_upload", "setting", None, detail=f"catalogue '{label}' uploaded", commit=True)
        flash("Catalogue updated. Everyone now sees the new version.", "success")
        return redirect(url_for("catalogue.manage"))

    return render_template("catalogue_manage.html",
                           has_file=_current() is not None,
                           label=settings_svc.get(LABEL_KEY, ""),
                           updated=settings_svc.get(UPDATED_KEY, ""))
