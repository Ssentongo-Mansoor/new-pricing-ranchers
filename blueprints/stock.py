"""Stock module: levels, add stock, wastage / adjustments, movement history.
The store manager maintains it; sales deduct automatically at fulfilment."""
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, jsonify)
from flask_login import login_required, current_user

from extensions import db
from models import (Product, Category, StockMovement, StockCount, StockCountLine,
                    SalesOrder, SalesOrderLine, Store, StoreItem)
from services.permissions import has_perm
from services.audit import log
from services import stock as stock_svc

bp = Blueprint("stock", __name__, url_prefix="/stock")


@bp.before_request
@login_required
def _guard():
    if not getattr(current_user, "can_see_stock", False):
        abort(403)


def _can_manage():
    return has_perm(current_user, "manage_stock")


def _can_audit():
    return has_perm(current_user, "audit_stock")


def _f(name):
    v = (request.form.get(name) or "").strip()
    try:
        return float(v)
    except ValueError:
        return None


@bp.route("/")
def index():
    q = (request.args.get("q") or "").strip().lower()
    cat = request.args.get("category", "")
    view = request.args.get("view", "")   # '' | 'low' | 'out'
    products = db.session.scalars(
        db.select(Product).filter_by(status="active").order_by(Product.article_no)).all()
    cats = db.session.scalars(db.select(Category).order_by(Category.name)).all()

    def keep(p):
        if cat and (not p.category or p.category.full_name != cat):
            return False
        if view == "low" and not p.is_low_stock:
            return False
        if view == "out" and not p.is_out_of_stock:
            return False
        if q and q not in p.article_no.lower() and q not in (p.description or "").lower():
            return False
        return True

    rows = [p for p in products if keep(p)]
    n_low = sum(1 for p in products if p.is_low_stock)
    n_out = sum(1 for p in products if p.is_out_of_stock)
    return render_template("stock/index.html", products=rows,
                           categories=sorted({c.full_name for c in cats}, key=str.lower),
                           q=request.args.get("q", ""), cat=cat, view=view,
                           n_low=n_low, n_out=n_out, can_manage=_can_manage())


@bp.route("/<int:product_id>/move", methods=["POST"])
def move(product_id):
    if not _can_manage():
        abort(403)
    p = db.session.get(Product, product_id)
    if p is None:
        abort(404)
    kind = request.form.get("kind")
    qty = _f("qty")
    note = request.form.get("note")
    if qty is None or qty <= 0:
        flash("Enter a quantity greater than zero.", "warning")
        return redirect(request.referrer or url_for("stock.index"))
    if kind == "receipt":
        delta = qty
    elif kind in ("wastage", "remove"):
        delta = -qty
        kind = "wastage"
    elif kind == "set":
        # adjustment to an exact count
        delta = qty - (p.stock_on_hand or 0)
        kind = "adjustment"
    else:
        abort(400)
    # Batch/lot traceability (QA audit 5 Jul 2026): optional on any movement.
    expiry = None
    raw_exp = (request.form.get("expiry") or "").strip()
    if raw_exp:
        try:
            from datetime import date as _date
            expiry = _date.fromisoformat(raw_exp)
        except ValueError:
            expiry = None
    stock_svc.apply_movement(p, delta, kind, user_id=current_user.id, note=note,
                             lot_number=(request.form.get("lot_number") or "").strip() or None,
                             expiry=expiry)
    log("stock_move", "product", p.id,
        field=kind, new_value=p.stock_on_hand, detail=f"{p.article_no} {delta:+g}")
    db.session.commit()
    flash(f"{p.article_no}: stock now {p.stock_on_hand:g} {p.unit_of_measure or ''}.", "success")
    return redirect(request.referrer or url_for("stock.index"))


@bp.route("/<int:product_id>/reorder", methods=["POST"])
def set_reorder(product_id):
    if not _can_manage():
        abort(403)
    p = db.session.get(Product, product_id)
    if p is None:
        abort(404)
    p.low_stock_level = _f("low_stock_level") or 0
    db.session.commit()
    flash(f"Low-stock alert for {p.article_no} set at {p.low_stock_level:g}.", "success")
    return redirect(request.referrer or url_for("stock.index"))


@bp.route("/<int:product_id>/history")
def history(product_id):
    p = db.session.get(Product, product_id)
    if p is None:
        abort(404)
    moves = db.session.scalars(
        db.select(StockMovement).filter_by(product_id=p.id)
        .order_by(StockMovement.created_at.desc())).all()
    return render_template("stock/history.html", product=p, moves=moves,
                           can_manage=_can_manage())


@bp.route("/stores")
def stores():
    stock_svc.ensure_stores()
    rows = db.session.scalars(
        db.select(Store).filter_by(archived=False).order_by(Store.sort_order, Store.name)).all()
    return render_template("stock/stores.html", stores=rows, can_manage=_can_manage())


@bp.route("/store/<int:store_id>")
def store_detail(store_id):
    s = db.session.get(Store, store_id)
    if s is None:
        abort(404)
    if s.is_sellable:
        return redirect(url_for("stock.index"))
    q = (request.args.get("q") or "").strip().lower()
    items = [i for i in s.items if not q or q in i.name.lower()
             or q in (i.category or "").lower()]
    return render_template("stock/store_detail.html", store=s, items=items,
                           q=request.args.get("q", ""), can_manage=_can_manage())


@bp.route("/store/<int:store_id>/item/add", methods=["POST"])
def store_item_add(store_id):
    if not _can_manage():
        abort(403)
    s = db.session.get(Store, store_id)
    if s is None or s.is_sellable:
        abort(400)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Item name is required.", "warning")
        return redirect(url_for("stock.store_detail", store_id=s.id))
    db.session.add(StoreItem(
        store_id=s.id, name=name, category=request.form.get("category"),
        pack_size=request.form.get("pack_size"), uom=request.form.get("uom"),
        origin=request.form.get("origin"), quantity=_f("quantity") or 0,
        low_level=_f("low_level") or 0, note=request.form.get("note")))
    log("store_item_add", "store", s.id, detail=f"{s.name}: {name}")
    db.session.commit()
    flash("Item added.", "success")
    return redirect(url_for("stock.store_detail", store_id=s.id))


@bp.route("/store/item/<int:item_id>/qty", methods=["POST"])
def store_item_qty(item_id):
    if not _can_manage():
        abort(403)
    it = db.session.get(StoreItem, item_id)
    if it is None:
        abort(404)
    it.quantity = _f("quantity") if _f("quantity") is not None else it.quantity
    if "low_level" in request.form:
        it.low_level = _f("low_level") or 0
    log("store_item_qty", "store", it.store_id, detail=f"{it.name} -> {it.quantity}")
    db.session.commit()
    flash(f"{it.name} updated.", "success")
    return redirect(url_for("stock.store_detail", store_id=it.store_id))


@bp.route("/store/item/<int:item_id>/remove", methods=["POST"])
def store_item_remove(item_id):
    if not _can_manage():
        abort(403)
    it = db.session.get(StoreItem, item_id)
    if it is None:
        abort(404)
    sid = it.store_id
    db.session.delete(it)
    db.session.commit()
    flash("Item removed.", "success")
    return redirect(url_for("stock.store_detail", store_id=sid))


@bp.route("/search")
def search():
    """Product search for the stock-take entry box."""
    q = (request.args.get("q") or "").strip().lower()
    out = []
    if q:
        for p in db.session.scalars(db.select(Product).filter_by(status="active")
                                    .order_by(Product.article_no)):
            if q in p.article_no.lower() or q in (p.description or "").lower():
                out.append({"id": p.id, "article_no": p.article_no,
                            "description": p.description,
                            "on_hand": p.stock_on_hand or 0,
                            "uom": p.unit_of_measure or ""})
            if len(out) >= 25:
                break
    return jsonify(results=out)


# --------------------------------------------------------------------------- #
# Stock takes (audit)
# --------------------------------------------------------------------------- #
@bp.route("/counts")
def counts():
    if not (_can_audit() or _can_manage()):
        abort(403)
    rows = db.session.scalars(db.select(StockCount).order_by(StockCount.created_at.desc())).all()
    return render_template("stock/counts.html", counts=rows, can_audit=_can_audit())


@bp.route("/count/new", methods=["POST"])
def count_new():
    if not _can_audit():
        abort(403)
    kind = request.form.get("kind")
    if kind not in StockCount.KINDS:
        kind = "spot"
    c = StockCount(kind=kind, note=request.form.get("note"),
                   created_by_id=current_user.id)
    db.session.add(c)
    db.session.commit()
    return redirect(url_for("stock.count_detail", count_id=c.id))


@bp.route("/count/<int:count_id>")
def count_detail(count_id):
    if not (_can_audit() or _can_manage()):
        abort(403)
    c = db.session.get(StockCount, count_id)
    if c is None:
        abort(404)
    return render_template("stock/count_detail.html", count=c, can_audit=_can_audit())


@bp.route("/count/<int:count_id>/line", methods=["POST"])
def count_line(count_id):
    if not _can_audit():
        abort(403)
    c = db.session.get(StockCount, count_id)
    if c is None or c.status != "open":
        abort(400)
    p = db.session.get(Product, int(request.form.get("product_id") or 0))
    if p is None:
        flash("Pick a product from the search.", "warning")
        return redirect(url_for("stock.count_detail", count_id=c.id))
    try:
        counted = float(request.form.get("counted_qty"))
    except (TypeError, ValueError):
        flash("Enter the physical quantity.", "warning")
        return redirect(url_for("stock.count_detail", count_id=c.id))
    existing = next((l for l in c.lines if l.product_id == p.id), None)
    if existing:
        existing.counted_qty = counted
        existing.system_qty = p.stock_on_hand or 0
    else:
        db.session.add(StockCountLine(count_id=c.id, product_id=p.id,
                                      system_qty=p.stock_on_hand or 0, counted_qty=counted))
    db.session.commit()
    return redirect(url_for("stock.count_detail", count_id=c.id))


@bp.route("/count/<int:count_id>/line/<int:line_id>/remove", methods=["POST"])
def count_line_remove(count_id, line_id):
    if not _can_audit():
        abort(403)
    l = db.session.get(StockCountLine, line_id)
    if l is None or l.count_id != count_id:
        abort(404)
    if l.count.status != "open":
        abort(400)
    db.session.delete(l)
    db.session.commit()
    return redirect(url_for("stock.count_detail", count_id=count_id))


@bp.route("/count/<int:count_id>/post", methods=["POST"])
def count_post(count_id):
    if not _can_audit():
        abort(403)
    c = db.session.get(StockCount, count_id)
    if c is None or c.status != "open":
        abort(400)
    adjusted = 0
    for l in c.lines:
        if l.counted_qty is None:
            continue
        delta = round(l.counted_qty - (l.product.stock_on_hand or 0), 4)
        if delta:
            stock_svc.apply_movement(l.product, delta, "adjustment",
                                     user_id=current_user.id,
                                     note=f"Stock take #{c.id} ({c.kind_label})")
            adjusted += 1
    c.status = "posted"
    c.posted_at = datetime.utcnow()
    log("stock_count_post", "stock_count", c.id,
        detail=f"Stock take #{c.id}: {adjusted} adjustment(s)")
    db.session.commit()
    flash(f"Stock take posted. {adjusted} product(s) corrected.", "success")
    return redirect(url_for("stock.count_detail", count_id=c.id))


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
@bp.route("/reports/out-of-stock")
def report_oos():
    if not (_can_audit() or _can_manage() or has_perm(current_user, "view_reports")):
        abort(403)
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    since = datetime.utcnow() - timedelta(days=days)
    # order items flagged out of stock / not delivered
    events = []
    lines = db.session.scalars(
        db.select(SalesOrderLine).join(SalesOrder)
        .where(SalesOrderLine.availability.in_(("out_of_stock", "not_delivered")),
               SalesOrder.created_at >= since)
        .order_by(SalesOrder.created_at.desc())).all()
    for l in lines:
        o = l.order
        events.append({"date": o.created_at, "order": o, "article": l.article_no,
                       "desc": l.description, "qty": l.quantity,
                       "status": l.availability,
                       "expected": l.expected_restock,
                       "customer": o.customer.name if o.customer else "—"})
    # products at/below zero now
    zero = db.session.scalars(
        db.select(Product).filter_by(status="active")
        .where(Product.stock_on_hand <= 0).order_by(Product.article_no)).all()
    return render_template("stock/report_oos.html", events=events, zero=zero, days=days)
