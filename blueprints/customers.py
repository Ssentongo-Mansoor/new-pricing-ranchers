"""Customer management. Reps see only their assigned customers."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from datetime import date

from extensions import db
from models import (Customer, User, Pricelist, CustomerCategory, SalesOrder,
                    Offer, Message, Invoice)
from services.security import (manager_required, admin_required,
                               assert_can_see_customer, can_see_customer,
                               can_allocate_pricelists)
from services.audit import log

DEFAULT_CUSTOMER_CATEGORIES = [
    "Supermarket", "Hotel", "Restaurant", "Café", "Butchery", "Caterer",
    "Fast Food / QSR", "School / Institution", "Hospital", "Wholesaler",
    "Embassy / NGO", "Other",
]


def ensure_customer_categories():
    if db.session.scalar(db.select(db.func.count(CustomerCategory.id))) == 0:
        for i, name in enumerate(DEFAULT_CUSTOMER_CATEGORIES):
            db.session.add(CustomerCategory(name=name, sort_order=i))
        db.session.commit()


def _categories():
    ensure_customer_categories()
    return db.session.scalars(
        db.select(CustomerCategory).order_by(CustomerCategory.sort_order, CustomerCategory.name)).all()


def _generic_lists():
    return db.session.scalars(
        db.select(Pricelist).filter_by(is_customer=False, archived=False)
        .order_by(Pricelist.group_name, Pricelist.name)).all()


def _grouped_generic():
    """Generic pricelists grouped by their display group, for the customer form."""
    from blueprints.pricelists import effective_group, GROUP_ORDER
    groups = {}
    for p in _generic_lists():
        groups.setdefault(effective_group(p), []).append(p)
    extras = sorted(g for g in groups if g not in GROUP_ORDER)
    return [(g, groups[g]) for g in GROUP_ORDER if g in groups] + \
           [(g, groups[g]) for g in extras]


def _customer_lists(exclude_customer_id=None):
    """All tailor-made (customer) pricelists, for allocating to other customers."""
    q = db.select(Pricelist).filter_by(is_customer=True, archived=False)
    rows = db.session.scalars(q.order_by(Pricelist.name)).all()
    if exclude_customer_id is not None:
        rows = [p for p in rows if p.customer_id != exclude_customer_id]
    return rows


def _apply_allocation(customer, form):
    ids = form.getlist("pricelists")
    customer.allowed_pricelists = (
        db.session.scalars(db.select(Pricelist).filter(Pricelist.id.in_(ids))).all()
        if ids else [])

bp = Blueprint("customers", __name__, url_prefix="/customers")


def _reps():
    return db.session.scalars(
        db.select(User).filter_by(is_active=True).order_by(User.full_name)).all()


def _save_fields(c, form, force_segment=None):
    c.name = (form.get("name") or c.name or "").strip()
    c.contact_name = form.get("contact_name")
    c.email = form.get("email")
    c.phone = form.get("phone")
    c.market = form.get("market", c.market or "local")
    c.default_currency = form.get("default_currency", c.default_currency or "UGX")
    c.segment = force_segment or form.get("segment", c.segment or "customer")
    if "proposed_payment_terms" in form:
        c.proposed_payment_terms = form.get("proposed_payment_terms")
    # Approved credit terms and account status are set only by pricing officer / admin.
    if can_allocate_pricelists(current_user) and "payment_terms" in form:
        c.payment_terms = form.get("payment_terms")
    if can_allocate_pricelists(current_user) and "account_status" in form:
        st = form.get("account_status")
        c.account_status = st if st in ("ok", "on_hold", "blocked") else "ok"
        c.account_note = form.get("account_note")
    c.category_id = form.get("category_id", type=int) or None
    c.area = form.get("area")
    c.address = form.get("address")
    def _f(name):
        v = (form.get(name) or "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None
    c.latitude = _f("latitude")
    c.longitude = _f("longitude")
    c.notes = form.get("notes")
    # named contacts
    c.procurement_name = form.get("procurement_name")
    c.procurement_phone = form.get("procurement_phone")
    c.procurement_email = form.get("procurement_email")
    c.chef_name = form.get("chef_name")
    c.chef_phone = form.get("chef_phone")
    c.chef_email = form.get("chef_email")
    c.other_contact_name = form.get("other_contact_name")
    c.other_contact_phone = form.get("other_contact_phone")
    c.other_contact_email = form.get("other_contact_email")
    c.tax_id = form.get("tax_id")
    # delivery acceptance
    c.delivery_days = ",".join(form.getlist("delivery_days")) or None
    c.delivery_time_from = form.get("delivery_time_from") or None
    c.delivery_time_to = form.get("delivery_time_to") or None
    c.delivery_notes = form.get("delivery_notes") or None
    rep_ids = form.getlist("reps")
    c.reps = db.session.scalars(db.select(User).filter(User.id.in_(rep_ids))).all() if rep_ids else []
    # Only the pricing officer (and admin) may change pricelist allocation.
    if can_allocate_pricelists(current_user):
        _apply_allocation(c, form)


def _active_customer_ids(months=6):
    """Customer ids that bought within the last `months` (history + live)."""
    from datetime import date
    today = date.today()
    y, m = today.year, today.month - (months - 1)
    while m <= 0:
        m += 12
        y -= 1
    cutoff = date(y, m, 1)
    ids = set()
    for cid in db.session.scalars(db.select(Invoice.customer_id).where(
            Invoice.customer_id.isnot(None), Invoice.invoice_date >= cutoff,
            Invoice.payment_status != "Reversed", Invoice.untaxed > 0).distinct()):
        ids.add(cid)
    for cid in db.session.scalars(db.select(SalesOrder.customer_id).where(
            SalesOrder.customer_id.isnot(None), SalesOrder.order_date >= cutoff,
            SalesOrder.status.in_(("placed", "in_fulfillment", "pending",
                                   "ready_for_dispatch", "out_for_delivery",
                                   "dispatched", "delivered", "fulfilled"))).distinct()):
        ids.add(cid)
    return ids


@bp.route("/")
@login_required
def index():
    cat = request.args.get("category", type=int)
    show_archived = request.args.get("archived") == "1"
    status = request.args.get("status", "active")   # active | inactive | all
    if status not in ("active", "inactive", "all"):
        status = "active"
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        customers = [c for c in customers if can_see_customer(current_user, c)]
    customers = [c for c in customers if (c.segment or "customer") != "distributor"]
    n_archived = sum(1 for c in customers if c.archived)
    customers = [c for c in customers if bool(c.archived) == show_archived]
    if cat:
        customers = [c for c in customers if c.category_id == cat]

    active_ids = _active_customer_ids(6)
    n_active = sum(1 for c in customers if c.id in active_ids)
    n_inactive = len(customers) - n_active
    if status == "active":
        customers = [c for c in customers if c.id in active_ids]
    elif status == "inactive":
        customers = [c for c in customers if c.id not in active_ids]

    return render_template("customers/index.html", customers=customers, cat=cat,
                           categories=_categories(), show_archived=show_archived,
                           n_archived=n_archived, status=status,
                           n_active=n_active, n_inactive=n_inactive,
                           active_ids=active_ids)


def _filtered_for_export():
    """Apply the export filters (status/rep/category/segment) to the customers
    the current user may see. Returns (customers, active_ids, meta)."""
    status = request.args.get("status", "all")
    rep_id = request.args.get("rep", type=int)
    cat = request.args.get("category", type=int)
    segment = request.args.get("segment", "customer")
    months = request.args.get("months", default=6, type=int)

    rows = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        rows = [c for c in rows if can_see_customer(current_user, c)]
    rows = [c for c in rows if not c.archived]
    if segment in ("customer", "distributor"):
        rows = [c for c in rows if (c.segment or "customer") == segment]
    if cat:
        rows = [c for c in rows if c.category_id == cat]
    if rep_id:
        rows = [c for c in rows if any(r.id == rep_id for r in c.reps)]

    active_ids = _active_customer_ids(months)
    if status == "active":
        rows = [c for c in rows if c.id in active_ids]
    elif status == "inactive":
        rows = [c for c in rows if c.id not in active_ids]
    return rows, active_ids, {"status": status, "rep": rep_id, "category": cat,
                              "segment": segment, "months": months}


@bp.route("/export")
@login_required
def export_form():
    from services.customer_export import COLUMNS, DEFAULT_COLS
    reps = db.session.scalars(
        db.select(User).filter_by(role="rep").order_by(User.full_name)).all()
    return render_template("customers/export.html", reps=reps, categories=_categories(),
                           columns=COLUMNS, default_cols=DEFAULT_COLS)


@bp.route("/export.xlsx")
@login_required
def export_xlsx():
    from flask import send_file
    from services.customer_export import build_workbook, DEFAULT_COLS
    rows, active_ids, meta = _filtered_for_export()
    cols = request.args.getlist("col") or DEFAULT_COLS
    sort = request.args.get("sort", "name")
    if sort == "rep":
        rows.sort(key=lambda c: (", ".join(r.full_name for r in c.reps).lower(), c.name.lower()))
    elif sort == "status":
        rows.sort(key=lambda c: (c.id not in active_ids, c.name.lower()))
    bio = build_workbook(rows, cols, active_ids)
    label = meta["status"]
    fname = f"customers_{label}_{date.today():%Y%m%d}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/distributors")
@login_required
def distributors():
    show_archived = request.args.get("archived") == "1"
    rows = db.session.scalars(
        db.select(Customer).filter_by(segment="distributor").order_by(Customer.name)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        rows = [c for c in rows if can_see_customer(current_user, c)]
    n_archived = sum(1 for c in rows if c.archived)
    rows = [c for c in rows if bool(c.archived) == show_archived]
    return render_template("customers/distributors.html", distributors=rows,
                           show_archived=show_archived, n_archived=n_archived)


@bp.route("/<int:customer_id>/archive", methods=["POST"])
@login_required
@manager_required
def archive(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    c.archived = True
    log("customer_archive", "customer", c.id, detail=f"archived {c.name}")
    db.session.commit()
    flash(f"{c.name} archived. The record is kept and can be restored.", "success")
    return redirect(url_for("customers.distributors" if c.segment == "distributor" else "customers.index"))


@bp.route("/<int:customer_id>/unarchive", methods=["POST"])
@login_required
@manager_required
def unarchive(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    c.archived = False
    log("customer_unarchive", "customer", c.id, detail=f"restored {c.name}")
    db.session.commit()
    flash(f"{c.name} restored.", "success")
    return redirect(url_for("customers.detail", customer_id=c.id))


@bp.route("/<int:customer_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    noun = "distributor" if c.segment == "distributor" else "customer"
    back = url_for("customers.distributors" if c.segment == "distributor" else "customers.index")
    # Guard: never delete a record with trading history — archive instead.
    if db.session.scalar(db.select(db.func.count(SalesOrder.id)).filter_by(customer_id=c.id)) \
       or db.session.scalar(db.select(db.func.count(Offer.id)).filter_by(customer_id=c.id)):
        flash(f"This {noun} has orders or offers on record — archive it instead so the "
              f"history is kept.", "danger")
        return redirect(url_for("customers.detail", customer_id=c.id))
    if db.session.scalar(db.select(db.func.count(User.id)).filter_by(customer_id=c.id)):
        flash("Remove this customer's portal login first (Admin → Users), then delete.", "danger")
        return redirect(url_for("customers.detail", customer_id=c.id))
    name = c.name
    # clean up dependent records that have no history value
    db.session.query(Message).filter_by(customer_id=c.id).delete(synchronize_session=False)
    for pl in db.session.scalars(db.select(Pricelist).filter_by(customer_id=c.id, is_customer=True)).all():
        db.session.delete(pl)
    c.reps = []
    c.allowed_pricelists = []
    db.session.delete(c)
    log("customer_delete", "customer", customer_id, detail=f"deleted {noun} {name}")
    db.session.commit()
    flash(f"{name} permanently deleted.", "success")
    return redirect(back)


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    assert_can_see_customer(current_user, c)
    from blueprints.crm import VISIT_OUTCOMES, CALL_OUTCOMES
    from services.permissions import has_perm
    from models import Deal, SalesHistory, Invoice, Product

    # Historical invoiced sales (2024-2026), if this customer is matched
    hist = db.session.scalars(
        db.select(SalesHistory).filter_by(customer_id=c.id)).all()
    pmap = {p.id: p.description for p in db.session.scalars(db.select(Product))}
    hist_years, hist_top, hist_returns = {}, {}, 0.0
    for h in hist:
        y = hist_years.setdefault(h.year, {"rev": 0.0, "qty": 0.0})
        y["rev"] += float(h.revenue or 0)
        y["qty"] += float(h.quantity or 0)
        lbl = pmap.get(h.product_id)        # catalogue products only in the mix
        if lbl:
            hist_top[lbl] = hist_top.get(lbl, 0.0) + float(h.revenue or 0)
        if h.is_return:
            hist_returns += float(h.revenue or 0)
    hist_years = dict(sorted(hist_years.items()))
    hist_top = sorted(hist_top.items(), key=lambda kv: kv[1], reverse=True)[:8]

    # invoice history (dated) + outstanding for this customer
    invs = db.session.scalars(
        db.select(Invoice).filter_by(customer_id=c.id)
        .order_by(Invoice.invoice_date.desc())).all()
    inv_recent = invs[:15]
    inv_count = len(invs)
    inv_outstanding = sum(
        float(i.total or 0) for i in invs
        if i.currency == "UGX" and float(i.total or 0) > 0
        and i.payment_status in ("Not Paid", "Partially Paid", "In Payment"))

    return render_template("customers/detail.html", customer=c,
                           visit_outcomes=VISIT_OUTCOMES, call_outcomes=CALL_OUTCOMES,
                           deal_stages=Deal.STAGES,
                           can_log=has_perm(current_user, "log_activity"),
                           can_allocate=can_allocate_pricelists(current_user),
                           hist_years=hist_years, hist_top=hist_top,
                           hist_returns=hist_returns, inv_recent=inv_recent,
                           inv_count=inv_count, inv_outstanding=inv_outstanding)


ONBOARD_ROLES = ("rep", "telesales", "manager", "order_manager", "admin")


def _can_onboard(user):
    return getattr(user, "role", None) in ONBOARD_ROLES


@bp.route("/onboard", methods=["GET", "POST"])
@login_required
def onboard():
    """A rep registers a new customer. It lands as 'pending' for the pricing
    officer to allocate a pricelist and approve credit terms."""
    if not _can_onboard(current_user):
        abort(403)
    if request.method == "POST":
        c = Customer()
        seg = "distributor" if request.form.get("segment") == "distributor" else "customer"
        _save_fields(c, request.form, force_segment=seg)
        c.onboarding_status = "pending"
        c.credit_approved = False
        c.created_by_id = current_user.id
        # the creating rep covers it unless they ticked others
        if not c.reps:
            c.reps = [current_user]
        db.session.add(c)
        log("customer_onboard", "customer", None,
            detail=f"{c.name} registered by {current_user.full_name} (pending allocation)")
        db.session.commit()
        flash("Customer registered. The Pricing Officer will allocate a pricelist and "
              "approve the credit terms before ordering.", "success")
        return redirect(url_for("customers.detail", customer_id=c.id))
    return render_template("customers/edit.html", customer=None, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(), can_allocate=False,
                           onboarding=True, is_distributor=False)


@bp.route("/onboarding")
@login_required
def onboarding_queue():
    """Customers awaiting pricelist allocation / credit approval."""
    rows = db.session.scalars(
        db.select(Customer).filter_by(onboarding_status="pending", archived=False)
        .order_by(Customer.created_at.desc())).all()
    if not (current_user.can_manage_all or can_allocate_pricelists(current_user)):
        rows = [c for c in rows if can_see_customer(current_user, c)]
    return render_template("customers/onboarding.html", rows=rows)


@bp.route("/<int:customer_id>/approve", methods=["POST"])
@login_required
def approve_onboarding(customer_id):
    if not can_allocate_pricelists(current_user):
        abort(403)
    from services.allocation import allowed_pricelists_for
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    if not allowed_pricelists_for(c):
        flash("Allocate at least one pricelist before approving.", "warning")
        return redirect(url_for("customers.edit", customer_id=c.id))
    if not (c.payment_terms or "").strip():
        flash("Set the approved credit terms before approving.", "warning")
        return redirect(url_for("customers.edit", customer_id=c.id))
    c.onboarding_status = "approved"
    c.credit_approved = True
    log("customer_approve", "customer", c.id,
        detail=f"{c.name} approved (terms: {c.payment_terms})")
    db.session.commit()
    flash(f"{c.name} approved and ready to order.", "success")
    return redirect(url_for("customers.detail", customer_id=c.id))


@bp.route("/new", methods=["GET", "POST"])
@login_required
@manager_required
def new():
    if request.method == "POST":
        c = Customer()
        _save_fields(c, request.form)
        db.session.add(c)
        log("customer_create", "customer", None, detail=c.name)
        db.session.commit()
        flash("Customer created.", "success")
        return redirect(url_for("customers.detail", customer_id=c.id))
    return render_template("customers/edit.html", customer=None, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(),
                           can_allocate=can_allocate_pricelists(current_user),
                           is_distributor=False)


@bp.route("/distributors/new", methods=["GET", "POST"])
@login_required
@manager_required
def distributor_new():
    if request.method == "POST":
        c = Customer()
        _save_fields(c, request.form, force_segment="distributor")
        db.session.add(c)
        log("customer_create", "customer", None, detail=f"distributor {c.name}")
        db.session.commit()
        flash("Distributor created.", "success")
        return redirect(url_for("customers.detail", customer_id=c.id))
    return render_template("customers/edit.html", customer=None, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(),
                           can_allocate=can_allocate_pricelists(current_user),
                           is_distributor=True)


@bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
@manager_required
def edit(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    if request.method == "POST":
        _save_fields(c, request.form)
        log("customer_edit", "customer", c.id, detail=c.name)
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("customers.detail", customer_id=c.id))
    return render_template("customers/edit.html", customer=c, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(exclude_customer_id=c.id),
                           can_allocate=can_allocate_pricelists(current_user),
                           is_distributor=(c.segment == "distributor"))
