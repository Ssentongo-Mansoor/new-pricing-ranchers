"""Sales Manager: set monthly sales targets for reps and track attainment."""
from datetime import date
from functools import wraps

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import User, Customer, Product
from services.permissions import has_perm
from services import targets as tsvc
from services.audit import log

bp = Blueprint("targets", __name__, url_prefix="/targets")


def _guard(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **k):
        if not (current_user.is_admin or has_perm(current_user, "manage_targets")):
            abort(403)
        return fn(*a, **k)
    return wrapper


def _ym(default=None):
    raw = request.args.get("ym") or request.form.get("ym") or ""
    try:
        y, m = raw.split("-")
        return int(y), int(m)
    except ValueError:
        t = default or date.today()
        return t.year, t.month


def _to_int(v):
    """M13: parse an id from a parallel-array form field; skip blanks/bad values
    instead of raising a 500."""
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _reps():
    # A sales manager handles only the reps allocated to them; admins, the
    # sales director and other approvers see every rep.
    if getattr(current_user, "is_sales_manager", False):
        return sorted(current_user.managed_reps, key=lambda r: r.full_name or "")
    return db.session.scalars(
        db.select(User).filter_by(role="rep").order_by(User.full_name)).all()


@bp.route("/")
@_guard
def index():
    year, month = _ym()
    rows = []
    for rep in _reps():
        tg = tsvc.targets_for(rep.id, year, month)
        act = tsvc.rep_actuals(rep, year, month)
        tot = tg["total"]
        pct = (act["total"] / tot * 100.0) if tot else None
        rows.append({"rep": rep, "target": tot, "actual": act["total"], "pct": pct,
                     "n_cust": len(tg["customer"]), "n_prod": len(tg["product"])})
    label = date(year, month, 1).strftime("%B %Y")
    return render_template("targets/index.html", rows=rows, year=year, month=month,
                           ym=f"{year}-{month:02d}", label=label)


@bp.route("/rep/<int:rep_id>", methods=["GET", "POST"])
@_guard
def rep(rep_id):
    rep = db.session.get(User, rep_id)
    if not rep or rep.role != "rep":
        abort(404)
    if getattr(current_user, "is_sales_manager", False) and rep.manager_id != current_user.id:
        abort(403)
    year, month = _ym()

    if request.method == "POST":
        # overall total
        tsvc.upsert_target(rep_id, year, month, "total", _money(request.form.get("total")))
        # customer lines (parallel arrays)
        cids = request.form.getlist("cust_id")
        camts = request.form.getlist("cust_amt")
        for cid, amt in zip(cids, camts):
            iid = _to_int(cid)
            if iid is not None:
                tsvc.upsert_target(rep_id, year, month, "customer", _money(amt),
                                   customer_id=iid)
        # product lines
        pids = request.form.getlist("prod_id")
        pamts = request.form.getlist("prod_amt")
        for pid, amt in zip(pids, pamts):
            iid = _to_int(pid)
            if iid is not None:
                tsvc.upsert_target(rep_id, year, month, "product", _money(amt),
                                   product_id=iid)
        db.session.commit()
        log("targets_set", "user", rep_id,
            detail=f"targets set for {rep.full_name} {year}-{month:02d}", commit=True)
        flash("Targets saved.", "success")
        return redirect(url_for("targets.rep", rep_id=rep_id, ym=f"{year}-{month:02d}"))

    tg = tsvc.targets_for(rep_id, year, month)
    act = tsvc.rep_actuals(rep, year, month)
    assigned = sorted(rep.assigned_customers, key=lambda c: c.name)
    products = db.session.scalars(
        db.select(Product).order_by(Product.description)).all()
    cust_by_id = {c.id: c for c in assigned}
    prod_by_id = {p.id: p for p in products}
    label = date(year, month, 1).strftime("%B %Y")
    return render_template("targets/rep.html", rep=rep, year=year, month=month,
                           ym=f"{year}-{month:02d}", label=label, tg=tg, act=act,
                           assigned=assigned, products=products,
                           cust_by_id=cust_by_id, prod_by_id=prod_by_id)


def _money(v):
    try:
        return round(float(str(v).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return 0.0
