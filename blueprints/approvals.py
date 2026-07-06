"""Approvals queue: approvers (Admin, CEO, Sales Director) sign off pricing-
officer changes; the pricing officer can view the status of their requests."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from extensions import db
from models import PriceApproval
from services import approvals as svc
from services.audit import log
from services.pricing import format_money

bp = Blueprint("approvals", __name__, url_prefix="/approvals")


def _can_view():
    return svc.is_approver(current_user) or getattr(current_user, "is_pricing_officer", False)


@bp.before_request
@login_required
def _guard():
    if not _can_view():
        abort(403)


def _detail(req):
    """Price line detail for a price request: [(label, old, new, currency)]."""
    if req.kind != "price" or not req.pricelist:
        return []
    cur = req.pricelist.currency
    rows = []
    for line, tier, old, new in svc.pending_price_lines(req.pricelist):
        rows.append({
            "product": f"{line.product.article_no} · {line.product.description}",
            "tier": tier.label,
            "old": format_money(old, cur) if old is not None else "—",
            "new": format_money(new, cur) if new is not None else "—",
        })
    return rows


@bp.route("/")
def index():
    is_appr = svc.is_approver(current_user)
    q = db.select(PriceApproval).where(PriceApproval.status == "pending")
    if not is_appr:
        q = q.where(PriceApproval.requested_by_id == current_user.id)
    pending = db.session.scalars(q.order_by(PriceApproval.requested_at.desc())).all()

    qd = db.select(PriceApproval).where(PriceApproval.status != "pending")
    if not is_appr:
        qd = qd.where(PriceApproval.requested_by_id == current_user.id)
    decided = db.session.scalars(
        qd.order_by(PriceApproval.decided_at.desc()).limit(30)).all()

    details = {r.id: _detail(r) for r in pending}
    return render_template("approvals/index.html", pending=pending, decided=decided,
                           details=details, is_approver=is_appr)


@bp.route("/<int:req_id>/<decision>", methods=["POST"])
def decide(req_id, decision):
    if not svc.is_approver(current_user):
        abort(403)
    req = db.session.get(PriceApproval, req_id)
    if req is None or req.status != "pending":
        abort(404)
    note = request.form.get("note")
    if decision == "approve":
        svc.approve(req, current_user, note)
        verb = "approved"
    elif decision == "decline":
        svc.decline(req, current_user, note)
        verb = "declined"
    else:
        abort(400)
    log(f"approval_{verb}", "price_approval", req.id,
        detail=f"{req.kind}: {req.summary}", commit=False)
    db.session.commit()
    flash(f"Request {verb}.", "success")
    return redirect(url_for("approvals.index"))
