"""Public REST API (v1) for machine-to-machine integrations.

Auth: Authorization: Bearer <key>  (see services/api_auth.py). This blueprint is
CSRF-exempt (registered in app.py) because CSRF is a browser defence and does not
apply to token-authenticated server-to-server calls.

Design rule: writes reuse the exact same service functions as the web UI
(order_vat.derive_vat, order_vat.assign_number, pricing.effective_line_price,
promos, currency, stock) so the API can never drift from the pricing, VAT, and
numbering rules enforced everywhere else. Totals are computed model properties
and are never written.
"""
from datetime import date, datetime

from flask import Blueprint, request, jsonify, g, url_for

from extensions import db
from models import (Customer, Product, Pricelist, PricelistLine, SalesOrder,
                    SalesOrderLine, ExchangeRate)
from services.api_auth import api_key_required
from services import order_vat
from services import pricing as pricing_svc
from services import promos as promo_svc
from services import currency as cx

bp = Blueprint("api", __name__, url_prefix="/api/v1")

MAX_PAGE = 200
DEFAULT_PAGE = 50


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _page_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.args.get("page_size", DEFAULT_PAGE))
    except (TypeError, ValueError):
        size = DEFAULT_PAGE
    size = max(1, min(MAX_PAGE, size))
    return page, size


def _paginate(query, page, size):
    total = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    rows = db.session.scalars(query.limit(size).offset((page - 1) * size)).all()
    return rows, (total or 0)


def _meta(page, size, total):
    return {"page": page, "page_size": size, "total": total,
            "pages": (total + size - 1) // size if size else 0}


def _err(status, code, message):
    return jsonify(error=code, message=message), status


def _customer_json(c):
    return {
        "id": c.id, "name": c.name, "segment": c.segment, "market": c.market,
        "currency": c.default_currency, "account_status": c.account_status,
        "category_id": c.category_id, "email": c.email, "phone": c.phone,
        "payment_terms": c.payment_terms, "archived": bool(c.archived),
    }


def _product_json(p):
    return {
        "id": p.id, "article_no": p.article_no, "description": p.description,
        "vat_applicable": bool(p.vat_applicable), "stock_on_hand": p.stock_on_hand,
        "status": p.status, "barcode": getattr(p, "barcode", None),
    }


def _pricelist_json(pl, with_lines=False):
    d = {
        "id": pl.id, "name": pl.name, "currency": pl.currency,
        "market": pl.market, "vat_applicable": bool(pl.vat_applicable),
        "vat_rate": pl.vat_rate, "is_customer": bool(pl.is_customer),
        "customer_id": pl.customer_id,
        "tiers": [{"key": t.key, "label": t.label} for t in pl.tiers],
    }
    if with_lines:
        lines = []
        for ln in pl.lines:
            lines.append({
                "line_id": ln.id,
                "product_id": ln.product_id,
                "article_no": ln.product.article_no if ln.product else None,
                "description": ln.product.description if ln.product else None,
                "prices": ln.price_map(),
            })
        d["lines"] = lines
    return d


def _order_json(o, with_lines=True):
    d = {
        "id": o.id, "number": o.number, "status": o.status,
        "customer_id": o.customer_id,
        "customer_name": o.customer.name if o.customer else None,
        "source_pricelist_id": o.source_pricelist_id,
        "currency": o.currency, "market": o.market,
        "vat_applicable": bool(o.vat_applicable), "vat_rate": o.vat_rate,
        "exchange_rate_value": float(o.exchange_rate_value) if o.exchange_rate_value else None,
        "order_date": o.order_date.isoformat() if o.order_date else None,
        "delivery_date": o.delivery_date.isoformat() if o.delivery_date else None,
        "customer_po": o.customer_po,
        "subtotal": float(o.subtotal or 0),
        "vat_amount": float(o.vat_amount or 0),
        "total": float(o.total or 0),
    }
    if with_lines:
        d["lines"] = [{
            "id": l.id, "product_id": l.product_id, "article_no": l.article_no,
            "description": l.description, "quantity": l.quantity,
            "unit_price": float(l.unit_price or 0), "discount_pct": l.discount_pct,
            "vat_applicable": l.is_vatable,
            "line_total": float(l.line_total or 0),
        } for l in o.lines]
    return d


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------
@bp.get("/ping")
@api_key_required("read")
def ping():
    return jsonify(ok=True, service="ranchers-pricing-api", version="v1",
                   scope=g.api_key.scope,
                   acts_as=(g.api_user.username if g.api_user else None),
                   time=datetime.utcnow().isoformat() + "Z")


# --------------------------------------------------------------------------
# read endpoints
# --------------------------------------------------------------------------
@bp.get("/customers")
@api_key_required("read")
def list_customers():
    page, size = _page_args()
    q = db.select(Customer).where(Customer.archived.is_(False)).order_by(Customer.name)
    if request.args.get("segment"):
        q = q.where(Customer.segment == request.args["segment"])
    if request.args.get("q"):
        q = q.where(Customer.name.ilike(f"%{request.args['q']}%"))
    rows, total = _paginate(q, page, size)
    return jsonify(data=[_customer_json(c) for c in rows], meta=_meta(page, size, total))


@bp.get("/customers/<int:cid>")
@api_key_required("read")
def get_customer(cid):
    c = db.session.get(Customer, cid)
    if not c:
        return _err(404, "not_found", "Customer not found.")
    return jsonify(data=_customer_json(c))


@bp.get("/products")
@api_key_required("read")
def list_products():
    page, size = _page_args()
    q = db.select(Product).order_by(Product.description)
    if request.args.get("q"):
        term = f"%{request.args['q']}%"
        q = q.where(db.or_(Product.description.ilike(term), Product.article_no.ilike(term)))
    rows, total = _paginate(q, page, size)
    return jsonify(data=[_product_json(p) for p in rows], meta=_meta(page, size, total))


@bp.get("/products/<int:pid>")
@api_key_required("read")
def get_product(pid):
    p = db.session.get(Product, pid)
    if not p:
        return _err(404, "not_found", "Product not found.")
    return jsonify(data=_product_json(p))


@bp.get("/pricelists")
@api_key_required("read")
def list_pricelists():
    page, size = _page_args()
    q = db.select(Pricelist).order_by(Pricelist.name)
    if request.args.get("market"):
        q = q.where(Pricelist.market == request.args["market"])
    rows, total = _paginate(q, page, size)
    return jsonify(data=[_pricelist_json(pl) for pl in rows], meta=_meta(page, size, total))


@bp.get("/pricelists/<int:plid>")
@api_key_required("read")
def get_pricelist(plid):
    pl = db.session.get(Pricelist, plid)
    if not pl:
        return _err(404, "not_found", "Pricelist not found.")
    return jsonify(data=_pricelist_json(pl, with_lines=True))


@bp.get("/orders")
@api_key_required("read")
def list_orders():
    page, size = _page_args()
    q = db.select(SalesOrder).order_by(SalesOrder.id.desc())
    if request.args.get("status"):
        q = q.where(SalesOrder.status == request.args["status"])
    if request.args.get("customer_id"):
        try:
            q = q.where(SalesOrder.customer_id == int(request.args["customer_id"]))
        except ValueError:
            return _err(400, "bad_request", "customer_id must be an integer.")
    if request.args.get("since"):
        d = _parse_date(request.args["since"])
        if d:
            q = q.where(SalesOrder.order_date >= d)
    rows, total = _paginate(q, page, size)
    return jsonify(data=[_order_json(o, with_lines=False) for o in rows],
                   meta=_meta(page, size, total))


@bp.get("/orders/<int:oid>")
@api_key_required("read")
def get_order(oid):
    o = db.session.get(SalesOrder, oid)
    if not o:
        return _err(404, "not_found", "Order not found.")
    return jsonify(data=_order_json(o))


@bp.get("/exchange-rates")
@api_key_required("read")
def list_rates():
    rows = db.session.scalars(
        db.select(ExchangeRate).order_by(ExchangeRate.effective_date.desc()).limit(100)
    ).all()
    return jsonify(data=[{
        "id": r.id, "quote_currency": getattr(r, "quote_ccy", getattr(r, "currency", None)),
        "rate": float(r.rate), "effective_date": r.effective_date.isoformat() if r.effective_date else None,
        "expiry_date": r.expiry_date.isoformat() if getattr(r, "expiry_date", None) else None,
    } for r in rows])


# --------------------------------------------------------------------------
# write endpoints (require read_write scope)
# --------------------------------------------------------------------------
def _parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


@bp.post("/customers")
@api_key_required("read_write")
def create_customer():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return _err(400, "bad_request", "name is required.")
    market = (body.get("market") or "local").strip()
    if market not in ("local", "export"):
        return _err(400, "bad_request", "market must be 'local' or 'export'.")
    account_status = body.get("account_status", "ok")
    if account_status not in ("ok", "on_hold", "blocked"):
        account_status = "ok"
    c = Customer(
        name=name,
        market=market,
        default_currency=(body.get("currency") or ("USD" if market == "export" else "UGX")),
        segment=(body.get("segment") or "customer"),
        category_id=body.get("category_id"),
        contact_name=body.get("contact_name"),
        email=body.get("email"),
        phone=body.get("phone"),
        payment_terms=body.get("payment_terms"),
        account_status=account_status,
        created_by_id=(g.api_user.id if g.api_user else None),
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(data=_customer_json(c),
                   links={"self": url_for("api.get_customer", cid=c.id, _external=False)}), 201


@bp.post("/orders")
@api_key_required("read_write")
def create_order():
    """Create an order from a customer, a source pricelist, and lines.

    Body:
    {
      "customer_id": 12,
      "pricelist_id": 5,
      "currency": "UGX",            # optional; defaults to pricelist currency
      "delivery_date": "2026-07-10",# optional
      "customer_po": "PO-123",      # optional
      "notes": "...",               # optional
      "place": false,               # optional; if true, moves draft -> placed
      "lines": [
        {"product_id": 3, "tier": "excl_vat", "qty": 10, "discount_pct": 0}
        # or identify the product by "article_no" instead of product_id
      ]
    }
    Pricing, VAT, currency and numbering are computed server-side using the same
    services as the web UI. Returns the created order with computed totals.
    """
    body = request.get_json(silent=True) or {}

    # --- resolve customer + pricelist ---
    customer = db.session.get(Customer, body.get("customer_id") or 0)
    if not customer:
        return _err(400, "bad_request", "Valid customer_id is required.")
    if customer.account_status == "blocked":
        return _err(409, "customer_blocked", "Customer account is blocked.")
    src = db.session.get(Pricelist, body.get("pricelist_id") or 0)
    if not src:
        return _err(400, "bad_request", "Valid pricelist_id is required.")

    lines_in = body.get("lines") or []
    if not isinstance(lines_in, list) or not lines_in:
        return _err(400, "bad_request", "At least one line is required.")

    ccy = (body.get("currency") or src.currency or "UGX").upper()

    # --- VAT + market derived server-side (H2/H3/M9 rules) ---
    vat_applicable, vat_rate = order_vat.derive_vat(src, customer)
    market = customer.market or src.market or "local"

    # --- stamp exchange rate when USD is involved ---
    rate_value, rate_id = None, None
    if ccy == "USD" or (src.currency or "UGX") == "USD":
        rate = cx.get_rate("USD")
        if rate is None:
            return _err(409, "no_rate", "No valid USD exchange rate on file.")
        rate_value, rate_id = rate.rate, rate.id

    # --- build a quick index of the pricelist's lines by product ---
    pl_lines_by_pid = {}
    pl_lines_by_art = {}
    for pll in src.lines:
        pl_lines_by_pid[pll.product_id] = pll
        if pll.product and pll.product.article_no:
            pl_lines_by_art[pll.product.article_no.strip().lower()] = pll

    # --- validate every line up front; price server-side ---
    prepared = []
    for i, item in enumerate(lines_in):
        if not isinstance(item, dict):
            return _err(400, "bad_request", f"Line {i}: must be an object.")
        pll = None
        if item.get("product_id"):
            pll = pl_lines_by_pid.get(item["product_id"])
        elif item.get("article_no"):
            pll = pl_lines_by_art.get(str(item["article_no"]).strip().lower())
        if pll is None:
            return _err(422, "line_not_on_pricelist",
                        f"Line {i}: product not found on pricelist {src.id}.")
        # quantity + discount validation (H10)
        try:
            qty = float(item.get("qty", item.get("quantity")))
        except (TypeError, ValueError):
            return _err(400, "bad_request", f"Line {i}: qty must be a number.")
        if qty <= 0:
            return _err(400, "bad_request", f"Line {i}: qty must be greater than 0.")
        try:
            disc = float(item.get("discount_pct", 0) or 0)
        except (TypeError, ValueError):
            disc = 0.0
        disc = min(100.0, max(0.0, disc))
        tier_key = item.get("tier") or (src.primary_tier().key if src.primary_tier() else None)
        if not tier_key:
            return _err(422, "no_tier", f"Line {i}: no tier specified and pricelist has no default.")
        eff = pricing_svc.effective_line_price(pll, tier_key)
        if eff.get("amount") is None:
            return _err(422, "no_price",
                        f"Line {i}: no price for tier '{tier_key}' on this product.")
        prepared.append((pll, tier_key, qty, disc, eff))

    # --- create the order header (mirrors orders.new) ---
    order = SalesOrder(
        customer_id=customer.id, source_pricelist_id=src.id,
        currency=ccy, market=market,
        vat_applicable=vat_applicable, vat_rate=vat_rate,
        exchange_rate_value=rate_value, exchange_rate_id=rate_id,
        order_date=date.today(),
        delivery_date=_parse_date(body.get("delivery_date")),
        delivery_address=body.get("delivery_address"),
        customer_po=body.get("customer_po"),
        payment_terms=customer.payment_terms,
        notes=body.get("notes"),
        created_by=(g.api_user.id if g.api_user else None),
        status="draft",
    )
    db.session.add(order)

    try:
        order_vat.assign_number(order, "SO")   # flush + number + commit

        sort_i = 0
        for pll, tier_key, qty, disc, eff in prepared:
            product = pll.product
            vat_snap = bool(product.vat_applicable) if product else None
            src_ccy = eff.get("currency") or src.currency or "UGX"

            def to_order_ccy(amount):
                if src_ccy == order.currency:
                    return amount
                return cx.convert(amount, src_ccy, order.currency,
                                  rate_value=order.exchange_rate_value)

            # promo split (M10), unless a fixed price is in force
            promo = None if eff.get("is_fixed") else promo_svc.active_promo_for(pll, tier_key)
            segments = []   # (qty, unit_price, label_suffix)
            if promo:
                allowed = promo_svc.promo_qty_allowed(promo, qty)
                normal = to_order_ccy(pll.price_for(tier_key))
                promo_price = to_order_ccy(eff["amount"])
                if allowed >= qty:
                    segments.append((qty, promo_price, " (promo)"))
                elif allowed <= 0:
                    segments.append((qty, normal, ""))
                else:
                    segments.append((allowed, promo_price, " (promo)"))
                    segments.append((qty - allowed, normal, ""))
            else:
                segments.append((qty, to_order_ccy(eff["amount"]), ""))

            for seg_qty, unit_price, suffix in segments:
                sort_i += 1
                db.session.add(SalesOrderLine(
                    order_id=order.id,
                    product_id=product.id if product else None,
                    description=(product.description if product else None),
                    article_no=(product.article_no if product else None),
                    pack_size=(pll.pack_size or (product.pack_size if product else None)),
                    tier_label=((_tier_label(src, tier_key) or tier_key) + suffix),
                    quantity=seg_qty,
                    unit_price=unit_price,
                    discount_pct=disc,
                    is_fixed=bool(eff.get("is_fixed")),
                    vat_applicable=vat_snap,
                    sort_order=sort_i,
                ))

        if body.get("place"):
            order.status = "placed"
            order.placed_at = datetime.utcnow()

        db.session.commit()
    except cx.NoValidRate:
        db.session.rollback()
        return _err(409, "no_rate", "No valid exchange rate for one of the lines.")
    except Exception as e:
        db.session.rollback()
        return _err(500, "server_error", f"Could not create order: {e}")

    db.session.refresh(order)
    return jsonify(data=_order_json(order),
                   links={"self": url_for("api.get_order", oid=order.id, _external=False)}), 201


def _tier_label(pricelist, tier_key):
    for t in pricelist.tiers:
        if t.key == tier_key:
            return t.label
    return None
