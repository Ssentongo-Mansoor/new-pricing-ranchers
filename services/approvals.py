"""Approval workflow for price changes, new products, and new pricelists.

A pricing officer's changes are staged and held until an approver (Admin, CEO,
or Sales Director) signs off. Other roles that may edit prices apply directly.

Staging model:
* Price change -> the new value sits in line_price.pending_amount; the live
  ``amount`` is untouched until approved. One PriceApproval(kind='price') per
  pricelist groups all its staged lines.
* New product -> product.status='pending' (not usable) + PriceApproval('product').
* New pricelist -> pricelist.approval_status='pending' (hidden from ordering)
  + PriceApproval('pricelist').
"""
from datetime import datetime

from extensions import db
from models import (PriceApproval, Pricelist, Product, PricelistLine, LinePrice)

APPROVER_ROLES = ("admin", "ceo", "sales_director", "cfo")


def is_approver(user):
    return getattr(user, "role", None) in APPROVER_ROLES


def needs_approval(user):
    """Only the pricing officer's pricing/catalogue changes are held."""
    return getattr(user, "is_pricing_officer", False)


def pending_count():
    return db.session.scalar(
        db.select(db.func.count(PriceApproval.id)).where(
            PriceApproval.status == "pending")) or 0


# ---- creating / refreshing requests ---------------------------------------
def _price_request_for(pl):
    return db.session.scalar(db.select(PriceApproval).where(
        PriceApproval.kind == "price", PriceApproval.pricelist_id == pl.id,
        PriceApproval.status == "pending"))


def stage_price_request(pl, user):
    """Ensure a pending price request exists for this pricelist; refresh summary."""
    req = _price_request_for(pl)
    n = len(pending_price_lines(pl))
    if req is None:
        req = PriceApproval(kind="price", pricelist_id=pl.id,
                            requested_by_id=getattr(user, "id", None))
        db.session.add(req)
    req.summary = f"{n} price change(s) on '{pl.name}'"
    req.requested_at = datetime.utcnow()
    return req


def request_product(product, user):
    req = PriceApproval(kind="product", product_id=product.id,
                        requested_by_id=getattr(user, "id", None),
                        summary=f"New product {product.article_no} — {product.description}")
    db.session.add(req)
    return req


def request_pricelist(pl, user):
    req = PriceApproval(kind="pricelist", pricelist_id=pl.id,
                        requested_by_id=getattr(user, "id", None),
                        summary=f"New pricelist '{pl.name}'")
    db.session.add(req)
    return req


def request_promo(promo, user):
    line = promo.line
    req = PriceApproval(kind="promo", promo_id=promo.id,
                        pricelist_id=line.pricelist_id if line else None,
                        requested_by_id=getattr(user, "id", None),
                        summary=(f"Promo {line.product.article_no} on "
                                 f"'{line.pricelist.name}'" if line else "Promo"))
    db.session.add(req)
    return req


# ---- reading staged detail -------------------------------------------------
def pending_price_lines(pl):
    """Return [(line, tier, old, new)] for staged price changes on a pricelist."""
    out = []
    for line in pl.lines:
        for lp in line.prices:
            if lp.pending_amount is not None:
                out.append((line, lp.tier, lp.amount, lp.pending_amount))
    return out


# ---- decisions -------------------------------------------------------------
def approve(req, user, note=None):
    if req.kind == "price" and req.pricelist:
        for line in req.pricelist.lines:
            for lp in line.prices:
                if lp.pending_amount is not None:
                    lp.amount = lp.pending_amount
                    lp.pending_amount = None
    elif req.kind == "product" and req.product:
        req.product.status = "active"
    elif req.kind == "pricelist" and req.pricelist:
        req.pricelist.approval_status = "approved"
    elif req.kind == "promo" and req.promo:
        req.promo.status = "active"
        req.promo.approved_by_id = getattr(user, "id", None)
        req.promo.approved_at = datetime.utcnow()
    req.status = "approved"
    req.decided_by_id = getattr(user, "id", None)
    req.decided_at = datetime.utcnow()
    req.decision_note = (note or "").strip() or None


def decline(req, user, note=None):
    if req.kind == "price" and req.pricelist:
        for line in req.pricelist.lines:
            for lp in line.prices:
                lp.pending_amount = None
    elif req.kind == "product" and req.product:
        req.product.status = "inactive"
    elif req.kind == "pricelist" and req.pricelist:
        req.pricelist.approval_status = "declined"
        req.pricelist.archived = True
    elif req.kind == "promo" and req.promo:
        req.promo.status = "declined"
    req.status = "declined"
    req.decided_by_id = getattr(user, "id", None)
    req.decided_at = datetime.utcnow()
    req.decision_note = (note or "").strip() or None
