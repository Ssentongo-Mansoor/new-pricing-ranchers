"""Production planning (Phase 1) — replenishment, finished-goods level.

Orders are served from stock. Production tops stock back up. For every product
the system compares demand from open orders against stock on hand and suggests a
quantity to produce when stock falls short:

    demand(product)    = sum of ordered quantity across OPEN order lines
    on_hand(product)   = product.stock_on_hand
    shortfall(product) = max(demand - on_hand, 0)   # the suggested quantity to make

There is no per-order allocation. Recording production adds goods to general
stock through the existing services.stock.apply_movement (kind 'production').
All quantities are in the product's own unit (catch weight respected, never
converted on an assumed fixed weight). See SCHEMA_MAP.md.
"""
from datetime import datetime

from extensions import db
from models import (SalesOrder, SalesOrderLine, Product, ProdProduction,
                    StockMovement)
from services import stock as stock_svc
from services.audit import log

# Orders that count as committed demand: accepted and awaiting delivery.
OPEN_STATUSES = ("placed", "in_fulfillment", "pending")

EPS = 1e-9
_FAR = datetime.max.date()


def open_orders():
    """Open orders, soonest delivery first."""
    rows = db.session.scalars(
        db.select(SalesOrder).filter(SalesOrder.status.in_(OPEN_STATUSES))).all()
    rows.sort(key=lambda o: (o.delivery_date is None, o.delivery_date or _FAR))
    return rows


def _open_lines():
    """Every order line on an open order that has a product, with its order."""
    rows = db.session.scalars(
        db.select(SalesOrderLine)
        .join(SalesOrder, SalesOrderLine.order_id == SalesOrder.id)
        .filter(SalesOrder.status.in_(OPEN_STATUSES),
                SalesOrderLine.product_id.isnot(None))).all()
    return rows


def demand_by_product():
    """Aggregate open-order demand per product.

    Returns {product_id: {"demand": float, "earliest": date|None,
                          "lines": [SalesOrderLine, ...]}}.
    """
    out = {}
    for line in _open_lines():
        d = out.setdefault(line.product_id,
                           {"demand": 0.0, "earliest": None, "lines": []})
        d["demand"] += float(line.quantity or 0)
        d["lines"].append(line)
        dd = line.order.delivery_date if line.order else None
        if dd and (d["earliest"] is None or dd < d["earliest"]):
            d["earliest"] = dd
    return out


def product_row(product, demand_info=None):
    """Demand / stock / shortfall for one product."""
    if demand_info is None:
        demand_info = demand_by_product().get(product.id,
                                               {"demand": 0.0, "earliest": None, "lines": []})
    demand = float(demand_info["demand"])
    on_hand = float(product.stock_on_hand or 0)
    shortfall = max(demand - on_hand, 0.0)
    return {
        "product": product,
        "uom": product.unit_of_measure or "",
        "pack_size": product.pack_size or "",
        "demand": demand,
        "on_hand": on_hand,
        "shortfall": shortfall,
        "earliest": demand_info["earliest"],
        "lines": demand_info["lines"],
    }


def to_produce_list():
    """Products that need production now: demand exceeds stock. Soonest delivery
    first, then largest shortfall. This is the Production Manager's main list."""
    dmap = demand_by_product()
    rows = []
    for pid, info in dmap.items():
        product = db.session.get(Product, pid)
        if product is None:
            continue
        r = product_row(product, info)
        if r["shortfall"] > EPS:
            rows.append(r)
    rows.sort(key=lambda r: (r["earliest"] is None, r["earliest"] or _FAR,
                             -r["shortfall"]))
    return rows


def order_coverage(order):
    """Can this order be served from stock now? Per line, shortfall against the
    product's stock (each line judged on its own ordered quantity vs on-hand)."""
    lines = []
    short_lines = 0
    for l in order.lines:
        if not l.product_id:
            continue
        req = float(l.quantity or 0)
        on_hand = float(l.product.stock_on_hand or 0) if l.product else 0.0
        short = max(req - on_hand, 0.0)
        if short > EPS:
            short_lines += 1
        lines.append({"line": l, "product": l.product,
                      "uom": (l.product.unit_of_measure if l.product else "") or "",
                      "required": req, "on_hand": on_hand, "short": short})
    status = "Coverable from stock" if short_lines == 0 else "Awaiting production"
    return {"lines": lines, "short_lines": short_lines, "status": status}


def open_orders_coverage():
    return [{"order": o, "coverage": order_coverage(o)} for o in open_orders()]


def stock_overview():
    """Per active product: on-hand, open-order demand, shortfall. Sorted with the
    short items first."""
    dmap = demand_by_product()
    products = db.session.scalars(
        db.select(Product).filter(Product.status == "active")
        .order_by(Product.description)).all()
    rows = [product_row(p, dmap.get(p.id)) for p in products]
    rows.sort(key=lambda r: (r["shortfall"] <= EPS, r["product"].description.lower()))
    return rows


def recent_production(product_id, limit=10):
    return db.session.scalars(
        db.select(ProdProduction).filter(ProdProduction.product_id == product_id)
        .order_by(ProdProduction.created_at.desc()).limit(limit)).all()


def record_production(product, qty, user, note=None, lot_number=None, expiry=None):
    """Record finished goods produced into stock. Increases the product's stock
    through the existing apply_movement (kind 'production'). Replenishment, so no
    order link. Returns (ok, message). The one approved write into existing
    stock tables.

    Batch/lot traceability (QA audit 5 Jul 2026): every production run carries
    a lot number. When the operator leaves the field blank one is generated as
    LYYYYMMDD-<article>, so output is always traceable for a recall."""
    if product is None:
        return False, "No product selected."
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return False, "Enter a valid quantity produced."
    if q <= EPS:
        return False, "Quantity produced must be greater than zero."

    lot = (lot_number or "").strip() or \
        f"L{datetime.utcnow():%Y%m%d}-{product.article_no}"

    mv = stock_svc.apply_movement(
        product, +q, "production", user_id=getattr(user, "id", None),
        note=note or "Production to stock", lot_number=lot, expiry=expiry)
    db.session.flush()   # get mv.id
    db.session.add(ProdProduction(
        product_id=product.id, qty=q, note=note,
        lot_number=lot, expiry=expiry,
        recorded_by_id=getattr(user, "id", None),
        stock_movement_id=mv.id if mv else None))
    log("prod_record", "product", product.id,
        detail=f"produced {q:g} {product.unit_of_measure or ''} of "
               f"{product.article_no} {product.description} (lot {lot}); stock now "
               f"{product.stock_on_hand:g}")
    db.session.commit()
    return True, f"Recorded {q:g} {product.unit_of_measure or ''} produced. Stock updated.".strip()
