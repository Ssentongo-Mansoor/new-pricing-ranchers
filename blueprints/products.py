"""Product catalogue: search, view, create/edit, activate/deactivate, delete."""
import re
from collections import Counter

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, jsonify)
from flask_login import login_required, current_user

from extensions import db
from models import Product, Category
from services.security import manager_required
from services.audit import log

bp = Blueprint("products", __name__, url_prefix="/products")

_CODE = re.compile(r"^([A-Za-z]+)(\d+)")


def suggest_article_no(category_id):
    """Propose the next article number from the codes already used in this
    sub-category: take the dominant letter prefix, then the next free number
    after the highest one used in that sub-category."""
    cat = db.session.get(Category, category_id) if category_id else None
    if cat is None:
        return ""
    arts = [p.article_no for p in cat.products if p.article_no]
    prefixes = Counter()
    width = 3
    for a in arts:
        m = _CODE.match(a)
        if m:
            prefixes[m.group(1).upper()] += 1
            width = max(width, len(m.group(2)))
    if not prefixes and cat.parent:                  # fall back to parent category
        for p in cat.parent.products:
            m = _CODE.match(p.article_no or "")
            if m:
                prefixes[m.group(1).upper()] += 1
    if not prefixes:
        return ""
    prefix = prefixes.most_common(1)[0][0]
    base = 0
    for a in arts:
        m = _CODE.match(a)
        if m and m.group(1).upper() == prefix:
            base = max(base, int(m.group(2)))
    existing = {p.article_no.upper() for p in db.session.scalars(db.select(Product))}
    n = base + 1
    code = f"{prefix}{n:0{width}d}"
    while code.upper() in existing:
        n += 1
        code = f"{prefix}{n:0{width}d}"
    return code


@bp.route("/suggest-code")
@login_required
def suggest_code():
    return jsonify(code=suggest_article_no(request.args.get("category_id", type=int)))


@bp.route("/")
@login_required
def index():
    q = (request.args.get("q") or "").strip().lower()
    cat = request.args.get("category", "")
    vat = request.args.get("vat", "")   # '' | 'vat' | 'exempt'
    show_inactive = request.args.get("inactive") == "1"
    products = db.session.scalars(db.select(Product).order_by(Product.article_no)).all()
    cats = db.session.scalars(db.select(Category).order_by(Category.name)).all()

    def keep(p):
        if not show_inactive and not p.is_active:
            return False
        if cat and (not p.category or p.category.full_name != cat):
            return False
        if vat == "vat" and not p.vat_applicable:
            return False
        if vat == "exempt" and p.vat_applicable:
            return False
        if q and q not in p.article_no.lower() and q not in (p.description or "").lower() \
           and q not in (p.barcode or "").lower():
            return False
        return True

    products = [p for p in products if keep(p)]
    categories = sorted({c.full_name for c in cats}, key=str.lower)
    return render_template("products/index.html", products=products,
                           categories=categories, q=request.args.get("q", ""),
                           cat=cat, vat=vat, show_inactive=show_inactive)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@manager_required
def new():
    from models import Pricelist
    cats = db.session.scalars(db.select(Category).order_by(Category.name)).all()
    _lists = db.session.scalars(db.select(Pricelist).where(
        Pricelist.archived.is_(False), Pricelist.is_customer.is_(False),
        Pricelist.approval_status == "approved").order_by(Pricelist.name)).all()
    standard = [pl for pl in _lists if not pl.is_distributor]
    distributor = [pl for pl in _lists if pl.is_distributor]

    def _render(form):
        return render_template("products/new.html", cats=cats, form=form,
                               standard_lists=standard, distributor_lists=distributor)

    if request.method == "POST":
        article_no = (request.form.get("article_no") or "").strip().upper()
        if not article_no:
            flash("Article number is required.", "danger")
            return _render(request.form)
        if db.session.scalar(db.select(Product).filter_by(article_no=article_no)):
            flash(f"Article number '{article_no}' already exists.", "danger")
            return _render(request.form)
        cid = request.form.get("category_id")
        from services import approvals
        pend = approvals.needs_approval(current_user)
        def _cost(v):
            try:
                c = float(str(v).replace(",", "").strip())
                return c if c > 0 else None
            except (TypeError, ValueError):
                return None

        p = Product(article_no=article_no,
                    description=(request.form.get("description") or article_no).strip(),
                    barcode=request.form.get("barcode") or None,
                    pack_size=request.form.get("pack_size") or None,
                    unit_of_measure=request.form.get("unit_of_measure") or None,
                    category_id=int(cid) if cid else None,
                    vat_applicable=bool(request.form.get("vat_applicable")),
                    unit_cost=_cost(request.form.get("unit_cost")),
                    status="pending" if pend else "active")
        db.session.add(p)
        db.session.flush()
        log("product_create", "product", p.id, detail=f"{article_no} {p.description}")

        # Post to the chosen pricelists with a price each.
        from models import Pricelist, PricelistLine, LinePrice

        def _money(v):
            try:
                return round(float(str(v).replace(",", "").strip()), 4)
            except (TypeError, ValueError):
                return None

        posted = 0
        from services.cost_guard import below_cost_error
        for plid in request.form.getlist("pricelist"):
            pl = db.session.get(Pricelist, int(plid)) if plid.isdigit() else None
            if pl is None:
                continue
            price = _money(request.form.get(f"price_{plid}"))
            if price is None or price <= 0:
                continue
            # Cost floor (QA audit 5 Jul 2026): opening prices obey it too.
            err = below_cost_error(p, price, pl.currency)
            if err:
                db.session.rollback()
                flash(err, "danger")
                return _render(request.form)
            max_sort = max([l.sort_order for l in pl.lines], default=0)
            line = PricelistLine(pricelist_id=pl.id, product_id=p.id,
                                 pack_size=p.pack_size, section="ADDED PRODUCTS",
                                 sort_order=max_sort + 1)
            db.session.add(line)
            db.session.flush()
            base = pl.primary_tier()
            for t in pl.tiers:
                val = None
                if base is not None and t.id == base.id:
                    val = price
                elif t.key == "incl_vat":
                    # VAT only applies when the PRODUCT carries VAT, not just the list.
                    if pl.vat_applicable and p.vat_applicable:
                        val = round(price * (1 + (pl.vat_rate or 0) / 100.0), 2)
                    else:
                        val = price
                lp = LinePrice(line_id=line.id, tier_id=t.id)
                if val is not None:
                    if pend:
                        lp.pending_amount = val
                    else:
                        lp.amount = val
                db.session.add(lp)
            if pend:
                approvals.stage_price_request(pl, current_user)
            posted += 1

        if pend:
            approvals.request_product(p, current_user)
            db.session.commit()
            flash(f"Product and {posted} pricelist price(s) submitted for approval.", "success")
        else:
            db.session.commit()
            flash(f"Product created and added to {posted} pricelist(s)." if posted
                  else "Product created. Add it to pricelists to set prices.", "success")
        return redirect(url_for("products.detail", product_id=p.id))

    return _render({})


@bp.route("/<int:product_id>")
@login_required
def detail(product_id):
    p = db.session.get(Product, product_id)
    if p is None:
        abort(404)
    # Pricelists this user may add the product to (not already on, not archived).
    on_ids = {l.pricelist_id for l in p.lines}
    addable = []
    if current_user.can_manage_all or current_user.may_edit_prices:
        from models import Pricelist
        from services.security import can_see_customer_pricelist
        for pl in db.session.scalars(db.select(Pricelist).filter_by(archived=False)
                                     .order_by(Pricelist.is_customer, Pricelist.name)):
            if pl.id in on_ids:
                continue
            if can_see_customer_pricelist(current_user, pl):
                addable.append(pl)
    return render_template("products/detail.html", product=p, addable=addable)


@bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
@manager_required
def edit(product_id):
    p = db.session.get(Product, product_id)
    if p is None:
        abort(404)
    cats = db.session.scalars(db.select(Category).order_by(Category.name)).all()
    if request.method == "POST":
        p.description = (request.form.get("description") or p.description).strip()
        p.barcode = request.form.get("barcode") or None
        p.pack_size = request.form.get("pack_size")
        p.unit_of_measure = request.form.get("unit_of_measure")
        cid = request.form.get("category_id")
        p.category_id = int(cid) if cid else None
        p.vat_applicable = bool(request.form.get("vat_applicable"))
        raw_cost = (request.form.get("unit_cost") or "").replace(",", "").strip()
        try:
            new_cost = float(raw_cost) if raw_cost else None
            old_cost = p.unit_cost
            p.unit_cost = new_cost if (new_cost or 0) > 0 else None
            if (old_cost or None) != (p.unit_cost or None):
                log("cost_change", "product", p.id, field="unit_cost",
                    old_value=old_cost, new_value=p.unit_cost, detail=p.article_no)
        except ValueError:
            flash("Unit cost must be a number; cost left unchanged.", "warning")
        p.status = request.form.get("status", p.status)
        log("product_edit", "product", p.id, detail=p.article_no)
        db.session.commit()
        flash("Product updated.", "success")
        return redirect(url_for("products.detail", product_id=p.id))
    return render_template("products/edit.html", product=p, cats=cats)


@bp.route("/<int:product_id>/delete", methods=["POST"])
@login_required
@manager_required
def delete(product_id):
    p = db.session.get(Product, product_id)
    if p is None:
        abort(404)
    from models import SalesOrderLine, OfferLine, SalesHistory
    used = db.session.scalar(
        db.select(db.func.count(SalesOrderLine.id)).where(SalesOrderLine.product_id == p.id)) or 0
    if used:
        flash(f"Cannot delete {p.article_no}: it is used on {used} existing order line(s). "
              f"Mark it inactive instead.", "warning")
        return redirect(url_for("products.detail", product_id=p.id))
    # detach soft references, remove pricelist lines, then delete
    for l in list(p.lines):
        db.session.delete(l)
    db.session.query(OfferLine).filter_by(product_id=p.id).update(
        {"product_id": None}, synchronize_session=False)
    db.session.query(SalesHistory).filter_by(product_id=p.id).update(
        {"product_id": None}, synchronize_session=False)
    art = p.article_no
    db.session.delete(p)
    log("product_delete", "product", None, detail=f"{art} {p.description}")
    db.session.commit()
    flash(f"Product {art} deleted.", "success")
    return redirect(url_for("products.index"))


@bp.route("/<int:product_id>/toggle", methods=["POST"])
@login_required
@manager_required
def toggle(product_id):
    p = db.session.get(Product, product_id)
    if p is None:
        abort(404)
    p.status = "inactive" if p.is_active else "active"
    log("product_status", "product", p.id, new_value=p.status, detail=p.article_no)
    db.session.commit()
    flash(f"{p.article_no} is now {p.status}.", "success")
    return redirect(request.referrer or url_for("products.index"))
