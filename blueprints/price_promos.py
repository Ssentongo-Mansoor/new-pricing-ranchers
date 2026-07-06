"""Temporary promotional prices: create, list, and end. A pricing officer's
promo waits for approval (Admin/CEO/Sales Director); approvers create live."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import Pricelist, PricelistLine, PricelistTier, Product, PromoPrice
from services.audit import log
from services import approvals
from services import promos as promo_svc

bp = Blueprint("price_promos", __name__, url_prefix="/price-promos")


def _can():
    return (getattr(current_user, "may_edit_prices", False)
            or current_user.is_admin or approvals.is_approver(current_user))


@bp.before_request
@login_required
def _guard():
    if not _can():
        abort(403)


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _num(s):
    try:
        return Decimal(str(s).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


@bp.route("/")
def index():
    promos = db.session.scalars(
        db.select(PromoPrice).order_by(PromoPrice.created_at.desc())).all()
    rows = []
    for p in promos:
        line = p.line
        rows.append({"p": p, "line": line,
                     "product": (f"{line.product.article_no} · {line.product.description}"
                                 if line else "—"),
                     "pricelist": line.pricelist.name if line else "—",
                     "currency": line.pricelist.currency if line else "UGX",
                     "status": promo_svc.status_label(p),
                     "sold": promo_svc.qty_sold(p) if p.qty_cap is not None else None})
    pricelists = db.session.scalars(db.select(Pricelist).where(
        Pricelist.archived.is_(False)).order_by(Pricelist.is_customer, Pricelist.name)).all()
    return render_template("price_promos/index.html", rows=rows, pricelists=pricelists,
                           today=date.today())


@bp.route("/create", methods=["POST"])
def create():
    pl = db.session.get(Pricelist, request.form.get("pricelist_id", type=int))
    art = (request.form.get("article_no") or "").strip().upper()
    if pl is None or not art:
        flash("Choose a pricelist and a product article number.", "danger")
        return redirect(url_for("price_promos.index"))
    line = next((l for l in pl.lines if l.product and l.product.article_no.upper() == art), None)
    if line is None:
        flash(f"{art} is not on '{pl.name}'. Add it to that pricelist first.", "danger")
        return redirect(url_for("price_promos.index"))
    tier_key = request.form.get("tier_key") or (pl.tiers[0].key if pl.tiers else None)
    tier = next((t for t in pl.tiers if t.key == tier_key), None)
    if tier is None:
        flash("Choose a valid price tier.", "danger")
        return redirect(url_for("price_promos.index"))
    amt = _num(request.form.get("promo_amount"))
    if amt is None or amt <= 0:
        flash("Enter a valid promo price.", "danger")
        return redirect(url_for("price_promos.index"))
    start = _parse_date(request.form.get("start_date")) or date.today()
    end = _parse_date(request.form.get("end_date"))
    qty_cap = None
    try:
        q = (request.form.get("qty_cap") or "").strip()
        qty_cap = float(q) if q else None
    except ValueError:
        qty_cap = None
    if end is None and qty_cap is None:
        flash("Set an end date, a quantity cap, or both.", "danger")
        return redirect(url_for("price_promos.index"))

    pend = approvals.needs_approval(current_user)
    promo = PromoPrice(line_id=line.id, tier_id=tier.id, promo_amount=amt,
                       start_date=start, end_date=end, qty_cap=qty_cap,
                       note=request.form.get("note") or None,
                       created_by_id=current_user.id,
                       status="pending" if pend else "active")
    db.session.add(promo)
    db.session.flush()
    log("promo_create", "promo_price", promo.id,
        detail=f"{art} on '{pl.name}' @ {amt}")
    if pend:
        approvals.request_promo(promo, current_user)
        db.session.commit()
        flash("Promotion submitted for approval. It goes live once approved.", "success")
    else:
        db.session.commit()
        flash("Promotion is live.", "success")
    return redirect(url_for("price_promos.index"))


@bp.route("/<int:promo_id>/end", methods=["POST"])
def end(promo_id):
    promo = db.session.get(PromoPrice, promo_id)
    if promo is None:
        abort(404)
    promo.status = "ended"
    log("promo_end", "promo_price", promo.id, detail="ended manually", commit=True)
    flash("Promotion ended. The normal price is back.", "success")
    return redirect(url_for("price_promos.index"))
