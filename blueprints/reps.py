"""Reps directory: see each sales rep and the customers they are responsible
for, and assign customers to them with a simple checkbox list."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import User, Customer
from services.security import manager_required
from services.audit import log

bp = Blueprint("reps", __name__, url_prefix="/reps")


@bp.route("/")
@login_required
@manager_required
def index():
    reps = db.session.scalars(
        db.select(User).filter_by(role="rep").order_by(User.full_name)).all()
    return render_template("reps/index.html", reps=reps)


@bp.route("/<int:user_id>", methods=["GET"])
@login_required
@manager_required
def detail(user_id):
    rep = db.session.get(User, user_id)
    if rep is None:
        abort(404)
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    assigned_ids = {c.id for c in rep.assigned_customers}
    return render_template("reps/detail.html", rep=rep, customers=customers,
                           assigned_ids=assigned_ids)


@bp.route("/<int:user_id>/assign", methods=["POST"])
@login_required
@manager_required
def assign(user_id):
    rep = db.session.get(User, user_id)
    if rep is None:
        abort(404)
    before = ", ".join(sorted(c.name for c in rep.assigned_customers)) or "none"
    ids = request.form.getlist("customers")
    rep.assigned_customers = (
        db.session.scalars(db.select(Customer).filter(Customer.id.in_(ids))).all()
        if ids else [])
    after = ", ".join(sorted(c.name for c in rep.assigned_customers)) or "none"
    log("rights_change", "user", rep.id, field="assigned_customers",
        old_value=before, new_value=after,
        detail=f"customer assignment for {rep.full_name}")
    db.session.commit()
    flash(f"Updated the customers assigned to {rep.full_name}.", "success")
    return redirect(url_for("reps.detail", user_id=rep.id))
