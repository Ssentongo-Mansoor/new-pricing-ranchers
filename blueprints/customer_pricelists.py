"""Customer-specific pricelists: a separate tab, visibility by rep assignment."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import (Pricelist, PricelistTier, PricelistLine, LinePrice,
                    Customer, Product)
from services.security import (assert_can_see_pricelist, can_see_customer_pricelist,
                               assert_can_see_customer, manager_required, edit_required)
from services.audit import log
from blueprints.pricelists import _render_detail

bp = Blueprint("customer_pricelists", __name__, url_prefix="/customer-pricelists")


@bp.route("/")
@login_required
def index():
    show_archived = request.args.get("archived") == "1"
    lists = db.session.scalars(
        db.select(Pricelist).filter_by(is_customer=True, archived=show_archived)
        .order_by(Pricelist.name)).all()
    visible = [p for p in lists if can_see_customer_pricelist(current_user, p)]
    n_archived = sum(1 for p in db.session.scalars(
        db.select(Pricelist).filter_by(is_customer=True, archived=True))
        if can_see_customer_pricelist(current_user, p))
    return render_template("customer_pricelists/index.html",
                           lists=visible, today=date.today(),
                           show_archived=show_archived, n_archived=n_archived)


@bp.route("/<int:list_id>")
@login_required
def detail(list_id):
    return _render_detail(list_id, customer=True)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@manager_required
def new():
    """Create a customer pricelist from a generic base in two steps:
    1) choose customer + base + name; 2) finalise every price on one screen,
    then create in a single save."""
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    bases = db.session.scalars(
        db.select(Pricelist).filter_by(is_customer=False, archived=False).order_by(Pricelist.name)).all()

    if request.method == "POST":
        action = request.form.get("action", "build")
        base = db.session.get(Pricelist, int(request.form.get("base_id") or 0))
        customer = db.session.get(Customer, int(request.form.get("customer_id") or 0))
        if not base or not customer:
            abort(404)
        name = (request.form.get("name") or "").strip()
        valid_until_raw = request.form.get("valid_until") or ""
        notes = request.form.get("notes") or ""

        # Step 1 -> show the finalise grid (nothing saved yet)
        if action == "build":
            return render_template("customer_pricelists/finalise.html",
                                   customer=customer, base=base,
                                   name=name or f"{customer.name} — {base.name}",
                                   valid_until_raw=valid_until_raw, notes=notes)

        # Step 2 -> create with the finalised prices
        new_pl = Pricelist(
            name=name or f"{customer.name} — {base.name}",
            channel=base.channel, market=base.market, currency=base.currency,
            vat_applicable=base.vat_applicable, vat_rate=base.vat_rate,
            effective_date=date.today(), valid_until=_parse_date(valid_until_raw),
            notes=notes, is_customer=True, customer_id=customer.id,
            base_pricelist_id=base.id, source_file=f"derived from {base.name}")
        db.session.add(new_pl)
        db.session.flush()
        tier_map = {}
        for t in base.tiers:
            nt = PricelistTier(pricelist_id=new_pl.id, key=t.key, label=t.label,
                               sort_order=t.sort_order)
            db.session.add(nt)
            db.session.flush()
            tier_map[t.key] = nt
        for line in base.lines:
            nl = PricelistLine(
                pricelist_id=new_pl.id, product_id=line.product_id,
                section=line.section, pack_size=line.pack_size,
                units_per_pack=line.units_per_pack, box_small=line.box_small,
                box_medium=line.box_medium, box_large=line.box_large,
                sort_order=line.sort_order)
            db.session.add(nl)
            db.session.flush()
            for t in base.tiers:
                raw = (request.form.get(f"price_{line.id}_{t.key}") or "").replace(",", "").strip()
                amount = line.price_for(t.key)
                if raw != "":
                    try:
                        amount = Decimal(raw)
                    except InvalidOperation:
                        pass
                db.session.add(LinePrice(line_id=nl.id, tier_id=tier_map[t.key].id, amount=amount))
        log("customer_pricelist_create", "pricelist", new_pl.id,
            detail=f"'{new_pl.name}' from base '{base.name}' for {customer.name} (finalised)")
        db.session.commit()
        flash("Customer pricelist created with your prices.", "success")
        return redirect(url_for("customer_pricelists.detail", list_id=new_pl.id))

    return render_template("customer_pricelists/new.html",
                           customers=customers, bases=bases)


@bp.route("/<int:list_id>/settings", methods=["GET", "POST"])
@login_required
@manager_required
def settings(list_id):
    pl = db.session.get(Pricelist, list_id)
    if pl is None or not pl.is_customer:
        abort(404)
    assert_can_see_pricelist(current_user, pl)
    if request.method == "POST":
        pl.name = (request.form.get("name") or pl.name).strip()
        pl.effective_date = _parse_date(request.form.get("effective_date")) or pl.effective_date
        pl.valid_until = _parse_date(request.form.get("valid_until"))
        pl.notes = request.form.get("notes")
        log("customer_pricelist_edit", "pricelist", pl.id, detail="settings updated")
        db.session.commit()
        flash("Saved. Logged to history.", "success")
        return redirect(url_for("customer_pricelists.detail", list_id=pl.id))
    return render_template("customer_pricelists/settings.html", pl=pl)


@bp.route("/<int:list_id>/line/add", methods=["POST"])
@login_required
@edit_required
def add_line(list_id):
    pl = db.session.get(Pricelist, list_id)
    if pl is None or not pl.is_customer:
        abort(404)
    assert_can_see_pricelist(current_user, pl)
    art = (request.form.get("article_no") or "").strip()
    product = db.session.scalar(db.select(Product).filter_by(article_no=art))
    if product is None:
        flash(f"No product with article number '{art}'.", "danger")
        return redirect(url_for("customer_pricelists.detail", list_id=pl.id))
    max_sort = max([l.sort_order for l in pl.lines], default=0)
    line = PricelistLine(pricelist_id=pl.id, product_id=product.id,
                         pack_size=product.pack_size, sort_order=max_sort + 1,
                         section="ADDED LINES")
    db.session.add(line)
    db.session.flush()
    for t in pl.tiers:
        db.session.add(LinePrice(line_id=line.id, tier_id=t.id, amount=None))
    log("customer_pricelist_edit", "pricelist", pl.id,
        detail=f"added line {product.article_no}")
    db.session.commit()
    flash(f"Added {product.article_no}. Set its prices inline.", "success")
    return redirect(url_for("customer_pricelists.detail", list_id=pl.id))


@bp.route("/<int:list_id>/line/<int:line_id>/remove", methods=["POST"])
@login_required
@edit_required
def remove_line(list_id, line_id):
    pl = db.session.get(Pricelist, list_id)
    line = db.session.get(PricelistLine, line_id)
    if pl is None or line is None or line.pricelist_id != pl.id or not pl.is_customer:
        abort(404)
    assert_can_see_pricelist(current_user, pl)
    art = line.product.article_no
    db.session.delete(line)
    log("customer_pricelist_edit", "pricelist", pl.id, detail=f"removed line {art}")
    db.session.commit()
    flash(f"Removed {art}.", "success")
    return redirect(url_for("customer_pricelists.detail", list_id=pl.id))


@bp.route("/<int:list_id>/delete", methods=["POST"])
@login_required
@manager_required
def delete(list_id):
    """Delete a tailor-made (customer) pricelist. Pricing officer / manager / admin."""
    from models import customer_pricelist_alloc
    pl = db.session.get(Pricelist, list_id)
    if pl is None or not pl.is_customer:
        abort(404)
    name = pl.name
    # remove any allocations to customers, then delete (cascades tiers/lines/prices)
    db.session.execute(
        customer_pricelist_alloc.delete().where(
            customer_pricelist_alloc.c.pricelist_id == pl.id))
    # detach any list derived from this one as a base
    for child in db.session.scalars(db.select(Pricelist).filter_by(base_pricelist_id=pl.id)):
        child.base_pricelist_id = None
    db.session.delete(pl)
    log("pricelist_delete", "pricelist", list_id, detail=f"deleted customer list '{name}'")
    db.session.commit()
    flash(f"'{name}' permanently deleted.", "success")
    return redirect(url_for("customer_pricelists.index"))


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
