"""Offer / quote builder. Prices pull from a chosen list+tier; VAT/zero-rating
applies by market; USD offers stamp the rate in force so they stay fixed."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, abort, Response)
from flask_login import login_required, current_user

from extensions import db
from models import (Offer, OfferLine, Pricelist, PricelistLine, LinePrice,
                    Product, Customer, PricelistTier, SalesOrder, SalesOrderLine)
from services.security import (assert_can_see_customer, can_see_customer,
                               can_see_customer_pricelist)
from services.audit import log
from services import currency as cx
from services.pricing import format_money, effective_line_price
from services import settings as settings_svc
from services import exports
from services import order_vat

bp = Blueprint("offers", __name__, url_prefix="/offers")


def _visible_offers():
    offers = db.session.scalars(db.select(Offer).order_by(Offer.created_at.desc())).all()
    if current_user.can_manage_all or getattr(current_user, "is_order_manager", False):
        return offers
    assigned = {c.id for c in current_user.assigned_customers}
    return [o for o in offers if o.customer_id in assigned]


def _get_offer(offer_id):
    o = db.session.get(Offer, offer_id)
    if o is None:
        abort(404)
    assert_can_see_customer(current_user, o.customer)
    return o


@bp.route("/")
@login_required
def index():
    view = request.args.get("view", "active")
    allo = _visible_offers()
    groups = {
        "active": [o for o in allo if o.status in ("draft", "issued")],
        "converted": [o for o in allo if o.status == "converted"],
        "not_ordered": [o for o in allo if o.status == "not_ordered"],
        "archived": [o for o in allo if o.status == "archived"],
    }
    counts = {k: len(v) for k, v in groups.items()}
    offers = groups.get(view, groups["active"])
    return render_template("offers/index.html", offers=offers, today=date.today(),
                           view=view, counts=counts)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    from services.permissions import has_perm
    if not has_perm(current_user, "create_offers_orders"):
        abort(403)
    from services.allocation import selectable_customers, build_allocation, is_allowed
    customers = selectable_customers(current_user)
    alloc_map, lists = build_allocation(current_user, customers)

    if request.method == "POST":
        customer_id = request.form.get("customer_id", type=int)
        source_id = request.form.get("source_id", type=int)
        if customer_id is None or source_id is None:
            flash("Choose a customer and a pricelist.", "danger")
            return render_template("offers/new.html", customers=customers,
                                   lists=lists, alloc_map=alloc_map), 400
        customer = db.session.get(Customer, customer_id)
        if customer is None:
            abort(404)
        assert_can_see_customer(current_user, customer)
        src = db.session.get(Pricelist, source_id)
        if src is None:
            abort(404)
        if not is_allowed(current_user, customer, src):
            flash("That pricelist is not allocated to this customer.", "danger")
            return render_template("offers/new.html", customers=customers,
                                   lists=lists, alloc_map=alloc_map)
        ccy = request.form.get("currency", "UGX")
        # H2/H3/M9: derive VAT and market server-side (never from the form) so an
        # offer prices the same as a staff order on the same list.
        vat_applicable, vat_rate = order_vat.derive_vat(src, customer)
        market = customer.market or src.market or "local"
        valid_days = settings_svc.get_int("offer_validity_days", 30)
        valid_until = _parse_date(request.form.get("valid_until")) or (date.today() + timedelta(days=valid_days))

        # Stamp the USD rate in force if any USD<->UGX conversion may be needed.
        rate_value, rate_id = None, None
        if ccy == "USD" or src.currency == "USD":
            rate = cx.get_rate("USD")
            if rate is None:
                flash("No valid UGX→USD exchange rate. Enter one before creating a USD offer.", "danger")
                return render_template("offers/new.html", customers=customers,
                                       lists=lists, alloc_map=alloc_map)
            rate_value, rate_id = rate.rate, rate.id

        offer = Offer(customer_id=customer.id, source_pricelist_id=src.id,
                      currency=ccy, market=market, vat_applicable=vat_applicable,
                      vat_rate=vat_rate,
                      exchange_rate_value=rate_value, exchange_rate_id=rate_id,
                      valid_from=date.today(), valid_until=valid_until,
                      notes=request.form.get("notes"), created_by=current_user.id)
        db.session.add(offer)
        order_vat.assign_number(offer, "RF")   # C3: safe id-derived number + commit
        log("offer_create", "offer", offer.id,
            detail=f"{offer.number} for {customer.name}", commit=True)
        return redirect(url_for("offers.detail", offer_id=offer.id))

    return render_template("offers/new.html", customers=customers, lists=lists,
                           alloc_map=alloc_map)


@bp.route("/<int:offer_id>")
@login_required
def detail(offer_id):
    offer = _get_offer(offer_id)
    return render_template("offers/detail.html", offer=offer, today=date.today())


@bp.route("/<int:offer_id>/search-products")
@login_required
def search_products(offer_id):
    offer = _get_offer(offer_id)
    q = (request.args.get("q") or "").strip().lower()
    src = offer.source_pricelist
    out = []
    seen = set()
    for line in src.lines:
        p = line.product
        if p.id in seen:
            continue
        if q and q not in p.article_no.lower() and q not in (p.description or "").lower():
            continue
        seen.add(p.id)
        tiers = [{"key": t.key, "label": t.label,
                  "price": (float(line.price_for(t.key)) if line.price_for(t.key) is not None else None)}
                 for t in src.tiers]
        out.append({"line_id": line.id, "article_no": p.article_no,
                    "description": p.description,
                    "pack_size": line.pack_size or p.pack_size or "",
                    "currency": src.currency, "tiers": tiers})
        if len(out) >= 40:
            break
    return jsonify(results=out)


@bp.route("/<int:offer_id>/line/add", methods=["POST"])
@login_required
def add_line(offer_id):
    offer = _get_offer(offer_id)
    if offer.status != "draft":
        flash("Only draft quotes can be edited. Issued and archived quotes are locked.", "warning")
        return redirect(url_for("offers.detail", offer_id=offer.id))

    line_id = request.form.get("line_id", type=int)
    src_line = db.session.get(PricelistLine, line_id) if line_id else None
    tier_key = request.form.get("tier")
    try:
        qty = float(request.form.get("quantity", "1") or 1)
        discount = float(request.form.get("discount_pct", "0") or 0)
    except ValueError:
        flash("Quantity and discount must be numbers.", "danger")
        return redirect(url_for("offers.detail", offer_id=offer.id))

    # H10: reject a non-positive quantity; clamp discount into 0..100.
    if qty <= 0:
        flash("Quantity must be greater than zero.", "danger")
        return redirect(url_for("offers.detail", offer_id=offer.id))
    discount = min(100.0, max(0.0, discount))

    use_fixed = request.form.get("use_fixed") == "1"
    if src_line is None or src_line.pricelist_id != offer.source_pricelist_id:
        abort(400)
    product = src_line.product
    tier = db.session.scalar(
        db.select(PricelistTier).filter_by(pricelist_id=offer.source_pricelist_id, key=tier_key))
    tier_label = tier.label if tier else ""
    vat_snap = bool(product.vat_applicable) if product else None

    if use_fixed:
        try:
            unit_price = Decimal(request.form.get("fixed_price", "0"))
        except InvalidOperation:
            flash("Invalid fixed price.", "danger")
            return redirect(url_for("offers.detail", offer_id=offer.id))
        # H10: reject a negative fixed price. A below-tier fixed price is allowed
        # here but does not route through the pricelist approval workflow — full
        # below-tier approval routing is out of scope for this fix.
        if unit_price < 0:
            flash("Fixed price cannot be negative.", "danger")
            return redirect(url_for("offers.detail", offer_id=offer.id))
        # Cost floor (QA audit 5 Jul 2026): the effective fixed price (after
        # the line discount) must not fall below the product cost.
        from services.cost_guard import below_cost_error
        err = below_cost_error(product, unit_price, offer.currency, discount)
        if err:
            flash(err, "danger")
            return redirect(url_for("offers.detail", offer_id=offer.id))
        is_fixed, fixed_note = True, request.form.get("fixed_note") or "Fixed-price deal"
    else:
        eff = effective_line_price(src_line, tier_key)
        if eff["amount"] is None:
            flash("That tier has no price. Choose another or set a fixed price.", "danger")
            return redirect(url_for("offers.detail", offer_id=offer.id))
        src_ccy = eff["currency"]
        try:
            if src_ccy == offer.currency:
                unit_price = Decimal(str(eff["amount"]))
            else:
                unit_price = cx.convert(eff["amount"], src_ccy, offer.currency,
                                        rate_value=offer.exchange_rate_value)
        except cx.NoValidRate:
            flash("No valid exchange rate for this currency. Ask the pricing person to set one.", "danger")
            return redirect(url_for("offers.detail", offer_id=offer.id))
        is_fixed = eff["is_fixed"]
        fixed_note = eff["note"] if eff["is_fixed"] else None

    max_sort = max([l.sort_order for l in offer.lines], default=0)

    # M10: cap the promo quantity within this offer. Only units still under the
    # promo cap get the promo price; the excess is charged the normal tier price.
    from services import promos as promo_svc
    promo = None if (use_fixed or is_fixed) else promo_svc.active_promo_for(src_line, tier_key)
    if promo is not None:
        allowed = promo_svc.promo_qty_allowed(promo, qty)
        if 0 < allowed < qty:
            base = src_line.price_for(tier_key)
            try:
                normal_up = (Decimal(str(base)) if src_ccy == offer.currency
                             else cx.convert(base, src_ccy, offer.currency,
                                             rate_value=offer.exchange_rate_value)) \
                    if base is not None else unit_price
            except cx.NoValidRate:
                normal_up = unit_price
            db.session.add(OfferLine(
                offer_id=offer.id, product_id=product.id, description=product.description,
                article_no=product.article_no, pack_size=src_line.pack_size or product.pack_size,
                tier_label=tier_label + " (promo)", quantity=allowed, unit_price=unit_price,
                discount_pct=discount, is_fixed=False, fixed_note=fixed_note,
                vat_applicable=vat_snap, sort_order=max_sort + 1))
            db.session.add(OfferLine(
                offer_id=offer.id, product_id=product.id, description=product.description,
                article_no=product.article_no, pack_size=src_line.pack_size or product.pack_size,
                tier_label=tier_label, quantity=qty - allowed, unit_price=normal_up,
                discount_pct=discount, is_fixed=False, fixed_note=None,
                vat_applicable=vat_snap, sort_order=max_sort + 2))
            log("offer_edit", "offer", offer.id, detail=f"added {product.article_no} x{qty} (promo capped)")
            db.session.commit()
            flash("Line added (promo cap applied to the excess quantity).", "success")
            return redirect(url_for("offers.detail", offer_id=offer.id))

    db.session.add(OfferLine(
        offer_id=offer.id, product_id=product.id, description=product.description,
        article_no=product.article_no,
        pack_size=src_line.pack_size or product.pack_size,
        tier_label=tier_label, quantity=qty, unit_price=unit_price,
        discount_pct=discount, is_fixed=is_fixed, fixed_note=fixed_note,
        vat_applicable=vat_snap, sort_order=max_sort + 1))
    log("offer_edit", "offer", offer.id, detail=f"added {product.article_no} x{qty}")
    db.session.commit()
    flash("Line added.", "success")
    return redirect(url_for("offers.detail", offer_id=offer.id))


@bp.route("/<int:offer_id>/line/<int:line_id>/remove", methods=["POST"])
@login_required
def remove_line(offer_id, line_id):
    offer = _get_offer(offer_id)
    if offer.status != "draft":
        flash("Only draft quotes can be edited.", "warning")
        return redirect(url_for("offers.detail", offer_id=offer.id))
    line = db.session.get(OfferLine, line_id)
    if line is None or line.offer_id != offer.id:
        abort(404)
    db.session.delete(line)
    log("offer_edit", "offer", offer.id, detail=f"removed {line.article_no}")
    db.session.commit()
    flash("Line removed.", "success")
    return redirect(url_for("offers.detail", offer_id=offer.id))


@bp.route("/<int:offer_id>/settings", methods=["POST"])
@login_required
def update_settings(offer_id):
    offer = _get_offer(offer_id)
    offer.valid_until = _parse_date(request.form.get("valid_until")) or offer.valid_until
    offer.notes = request.form.get("notes")
    db.session.commit()
    flash("Saved.", "success")
    return redirect(url_for("offers.detail", offer_id=offer.id))


@bp.route("/<int:offer_id>/issue", methods=["POST"])
@login_required
def issue(offer_id):
    offer = _get_offer(offer_id)
    # M11: only a draft may be issued (never re-issue a converted/archived quote).
    if offer.status != "draft":
        flash("Only a draft quote can be issued.", "warning")
        return redirect(url_for("offers.detail", offer_id=offer.id))
    offer.status = "issued"
    log("offer_issue", "offer", offer.id,
        detail=f"issued; rate stamped {offer.exchange_rate_value or 'n/a'}")
    db.session.commit()
    flash("Quote issued and locked. The exchange rate is now fixed on it.", "success")
    return redirect(url_for("offers.detail", offer_id=offer.id))


@bp.route("/<int:offer_id>/archive", methods=["POST"])
@login_required
def archive(offer_id):
    offer = _get_offer(offer_id)
    offer.status = "archived"
    log("offer_archive", "offer", offer.id, detail=f"{offer.number} archived")
    db.session.commit()
    flash(f"Offer {offer.number} archived. Its prices stay frozen as a record.", "success")
    return redirect(url_for("offers.detail", offer_id=offer.id))


# offers.unarchive removed 3 Jul 2026 (QA audit M2): no button or link called
# it. If restoring archived offers is wanted, re-add the handler with a button
# on the offer detail page (it was a working implementation, see backups).


@bp.route("/archive-superseded", methods=["POST"])
@login_required
def archive_superseded():
    """Archive every issued offer that was created before a given date — handy
    after a price change, to file the offers made at the old prices."""
    before = _parse_date(request.form.get("before")) or date.today()
    count = 0
    for o in _visible_offers():
        if o.status == "issued" and o.created_at and o.created_at.date() < before:
            o.status = "archived"
            log("offer_archive", "offer", o.id, detail=f"{o.number} bulk-archived (pre {before})")
            count += 1
    db.session.commit()
    flash(f"Archived {count} issued quote(s) created before {before}.", "success")
    return redirect(url_for("offers.index"))


@bp.route("/<int:offer_id>/convert", methods=["POST"])
@login_required
def convert_to_order(offer_id):
    """Customer confirmed the offer — turn it into a sales order (placed)."""
    offer = _get_offer(offer_id)
    if offer.converted_order_id:
        flash(f"This offer was already converted to order {offer.converted_order.number}.", "warning")
        return redirect(url_for("orders.detail", order_id=offer.converted_order_id))
    if not offer.lines:
        flash("This quote has no lines to convert.", "warning")
        return redirect(url_for("offers.detail", offer_id=offer.id))
    # M11: only an issued quote may convert (not a draft, not_ordered or archived).
    if offer.status not in ("issued",):
        flash("Only an issued quote can be converted to an order.", "warning")
        return redirect(url_for("offers.detail", offer_id=offer.id))
    # M11: block converting an expired quote — its prices may be stale (pre-increase).
    if offer.valid_until and offer.valid_until < date.today():
        flash(f"This quote expired on {offer.valid_until:%d %b %Y}. Re-issue it at "
              f"current prices before converting.", "warning")
        return redirect(url_for("offers.detail", offer_id=offer.id))

    order = SalesOrder(
        customer_id=offer.customer_id,
        source_pricelist_id=offer.source_pricelist_id, currency=offer.currency,
        market=offer.market, vat_applicable=offer.vat_applicable, vat_rate=offer.vat_rate,
        exchange_rate_value=offer.exchange_rate_value, exchange_rate_id=offer.exchange_rate_id,
        status="placed", order_date=date.today(),
        notes=f"Created from offer {offer.number}.", created_by=current_user.id)
    db.session.add(order)
    db.session.flush()
    order.number = order_vat.derive_number("SO", order.id)   # C3: id-derived number
    for ol in offer.lines:
        db.session.add(SalesOrderLine(
            order_id=order.id, product_id=ol.product_id, description=ol.description,
            article_no=ol.article_no, pack_size=ol.pack_size, tier_label=ol.tier_label,
            quantity=ol.quantity, unit_price=ol.unit_price, discount_pct=ol.discount_pct,
            is_fixed=ol.is_fixed, fixed_note=ol.fixed_note, availability="available",
            vat_applicable=ol.vat_applicable, sort_order=ol.sort_order))
    offer.status = "converted"
    offer.converted_order_id = order.id
    log("offer_convert", "offer", offer.id,
        detail=f"{offer.number} converted to order {order.number}")
    db.session.commit()
    flash(f"Offer {offer.number} converted to order {order.number}. It is now in the fulfilment inbox.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:offer_id>/not-ordered", methods=["POST"])
@login_required
def mark_not_ordered(offer_id):
    offer = _get_offer(offer_id)
    if offer.status == "converted":
        flash("A converted quote cannot be marked not ordered.", "warning")
        return redirect(url_for("offers.detail", offer_id=offer.id))
    offer.status = "not_ordered"
    log("offer_not_ordered", "offer", offer.id, detail=f"{offer.number} marked not ordered")
    db.session.commit()
    flash(f"Offer {offer.number} filed under past offers (not ordered).", "success")
    return redirect(url_for("offers.detail", offer_id=offer.id))


@bp.route("/<int:offer_id>/reopen", methods=["POST"])
@login_required
def reopen(offer_id):
    offer = _get_offer(offer_id)
    if offer.status in ("not_ordered", "archived"):
        offer.status = "issued"
        log("offer_reopen", "offer", offer.id, detail=f"{offer.number} reopened")
        db.session.commit()
        flash(f"Offer {offer.number} reopened.", "success")
    return redirect(url_for("offers.detail", offer_id=offer.id))


@bp.route("/<int:offer_id>/promote", methods=["POST"])
@login_required
def promote(offer_id):
    """Save an offer as a new customer pricelist."""
    offer = _get_offer(offer_id)
    name = (request.form.get("name") or f"{offer.customer.name} — from {offer.number}").strip()
    pl = Pricelist(name=name, channel=offer.source_pricelist.channel if offer.source_pricelist else "mixed",
                   market=offer.market, currency=offer.currency,
                   vat_applicable=offer.vat_applicable, vat_rate=offer.vat_rate,
                   effective_date=date.today(), valid_until=offer.valid_until,
                   notes=f"Promoted from offer {offer.number}",
                   is_customer=True, customer_id=offer.customer_id,
                   source_file=f"offer {offer.number}")
    db.session.add(pl)
    db.session.flush()
    tier = PricelistTier(pricelist_id=pl.id, key="price", label="Price", sort_order=0)
    db.session.add(tier)
    db.session.flush()
    for ol in offer.lines:
        nl = PricelistLine(pricelist_id=pl.id, product_id=ol.product_id,
                           section="FROM OFFER", pack_size=ol.pack_size)
        db.session.add(nl)
        db.session.flush()
        db.session.add(LinePrice(line_id=nl.id, tier_id=tier.id, amount=ol.unit_price))
    log("customer_pricelist_create", "pricelist", pl.id,
        detail=f"promoted from offer {offer.number}")
    db.session.commit()
    flash("Quote saved as a customer pricelist.", "success")
    return redirect(url_for("customer_pricelists.detail", list_id=pl.id))


@bp.route("/<int:offer_id>/export.<fmt>")
@login_required
def export(offer_id, fmt):
    offer = _get_offer(offer_id)
    safe = offer.number.replace("/", "_")
    if fmt == "xlsx":
        data = exports.offer_to_excel(offer)
        log("export", "offer", offer.id, detail="Excel export", commit=True)
        return Response(data,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=Quote_{safe}.xlsx"})
    if fmt == "pdf":
        data = exports.offer_to_pdf(offer)
        log("export", "offer", offer.id, detail="PDF export", commit=True)
        disp = "inline" if request.args.get("view") == "1" else "attachment"
        return Response(data, mimetype="application/pdf",
                        headers={"Content-Disposition": f"{disp}; filename=Quote_{safe}.pdf"})
    abort(404)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
