"""Customer management. Reps see only their assigned customers."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from datetime import date
import unicodedata

from extensions import db
from models import (Customer, User, Pricelist, CustomerCategory, SalesOrder,
                    Offer, Message, Invoice)
from services.security import (manager_required, admin_required,
                               assert_can_see_customer, can_see_customer,
                               can_allocate_pricelists, hash_password)
from services.audit import log

def _temp_password():
    """Random 10-character temporary password, unambiguous alphabet (no 0/O,
    1/l/I). Shown once to the creator; the user replaces it at first sign-in."""
    import secrets
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def portal_username(name):
    """Derive the portal username from the account name: lowercase, dots for
    spaces and symbols, numeric suffix on collisions. 'Cafe Javas' becomes
    cafe.javas. Shared by auto-provisioning and the admin New-user form."""
    import re
    base = re.sub(r"[^a-z0-9]+", ".", (name or "").lower()).strip(".") or "customer"
    base = base[:56]
    username, n = base, 1
    while db.session.scalar(db.select(User).filter_by(username=username)):
        n += 1
        username = f"{base}{n}"
    return username


def _provision_portal_login(c):
    """Auto-create the portal login for a freshly created customer or
    distributor: username derived from the name, random temporary password,
    forced change at first sign-in. Returns (user, temp_password), or
    (None, None) when the customer already carries a login. The password is
    returned in clear exactly once so the creator sees it; only the hash is
    stored. Caller commits."""
    existing = db.session.scalar(
        db.select(User).filter_by(customer_id=c.id, role="customer"))
    if existing:
        return None, None
    username = portal_username(c.name)
    temp_pw = _temp_password()
    u = User(username=username,
             full_name=(c.contact_name or c.name or username).strip(),
             email=c.email,
             role="customer",
             can_edit=False,
             is_active=True,
             customer_id=c.id,
             must_change_password=True,
             password_hash=hash_password(temp_pw))
    db.session.add(u)
    log("user_create", "user", None,
        detail=f"portal login {username} auto-created for {c.name}")
    return u, temp_pw


def _portal_user_for(c):
    """The portal login linked to this customer, or None."""
    return db.session.scalar(
        db.select(User).filter_by(customer_id=c.id, role="customer"))


def _reset_portal_password(u):
    """Issue a fresh temporary password and re-arm the forced change.
    Returns the new password in clear (shown/printed once)."""
    temp_pw = _temp_password()
    u.password_hash = hash_password(temp_pw)
    u.must_change_password = True
    log("portal_pw_reset", "user", u.id,
        detail=f"temporary password reset for {u.username}")
    return temp_pw


def _send_welcome_email(c, u, temp_pw=None):
    """Best-effort welcome email: username plus a signed 72-hour activation
    link where the customer sets their own password. No password travels by
    mail — spam filters flag credential mails, and the link is safer anyway.
    (temp_pw is accepted for call-site compatibility; the printed welcome
    sheet is the channel that carries a temporary password.)
    Returns (ok, reason). Never raises; call AFTER commit so an SMTP failure
    cannot roll back the customer."""
    from services import comms
    from services import settings as settings_svc
    from services.security import make_activation_token
    if u is None:
        return (False, "no login created")
    if not (c.email or "").strip():
        return (False, "no email address on file")
    company = settings_svc.get("company_name") or "Ranchers Finest"
    root = request.url_root.rstrip("/")
    activate_url = root + url_for("auth.activate", token=make_activation_token(u))
    who = (c.contact_name or c.name or "").strip()
    body = (
        f"Dear {who},\n"
        f"\n"
        f"Welcome to {company}. Your customer portal account is ready.\n"
        f"\n"
        f"Username: {u.username}\n"
        f"\n"
        f"Activate your account and choose your own password here\n"
        f"(the link works for 72 hours):\n"
        f"{activate_url}\n"
        f"\n"
        f"For later sign-ins, the portal lives at {root}/login\n"
        f"\n"
        f"How the portal works\n"
        f"1. My Pricelist — your agreed prices, always current.\n"
        f"2. New Order — pick products and quantities, then submit. You\n"
        f"   receive an order number and we confirm it.\n"
        f"3. Orders — follow each order from confirmation to delivery and\n"
        f"   download the order PDF.\n"
        f"4. Messages — questions or changes on an order go here. We reply\n"
        f"   in the portal.\n"
        f"5. Account — change your password any time.\n"
        f"\n"
        f"The full guide is on the Help page inside the portal.\n"
        f"Need a hand? Reply to this email or contact your sales\n"
        f"representative.\n"
        f"\n"
        f"{company}\n"
    )
    from services.email_templates import portal_welcome_html
    import os
    from flask import current_app
    html = portal_welcome_html(company, f"{root}/login", activate_url,
                               u.username, who)
    logo = os.path.join(current_app.static_folder, "img", "ranchers-logo.png")
    ok, reason = comms.send_email(c.email,
                                  f"Welcome to the {company} Customer Portal",
                                  body, html=html,
                                  inline_images={"rflogo": logo})
    log("welcome_email", "user", u.id,
        detail=f"welcome email to {c.email}: {'sent' if ok else reason}",
        commit=True)
    return ok, reason


DEFAULT_CUSTOMER_CATEGORIES = [
    "Supermarket", "Hotel", "Restaurant", "Cafe", "Butchery", "Caterer",
    "Fast Food / QSR", "School / Institution", "Hospital", "Wholesaler",
    "Embassy / NGO", "Other",
]


def _ascii_safe(value):
    if not value:
        return value
    normalized = unicodedata.normalize("NFKD", str(value))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def ensure_customer_categories():
    if db.session.scalar(db.select(db.func.count(CustomerCategory.id))) == 0:
        for i, name in enumerate(DEFAULT_CUSTOMER_CATEGORIES):
            db.session.add(CustomerCategory(name=_ascii_safe(name), sort_order=i))
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
    from datetime import timedelta
    cat = request.args.get("category", type=int)
    show_archived = request.args.get("archived") == "1"
    # Filter on when the record became a customer: since=mtd (this month) or
    # since=<days>. The dashboard's "New customers" tile links here with it.
    since = request.args.get("since")
    since_date = since_label = None
    if since == "mtd":
        today = date.today()
        since_date = date(today.year, today.month, 1)
        since_label = "new this month"
    elif since:
        try:
            days = int(since)
            since_date = date.today() - timedelta(days=days)
            since_label = f"new in the last {days} days"
        except ValueError:
            pass
    # New customers usually have no purchases yet, so the default 'active'
    # pill would hide them — default to 'all' when the since filter is on.
    status = request.args.get("status", "all" if since_date else "active")
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
    rep_id = request.args.get("rep", type=int)
    if rep_id:
        customers = [c for c in customers
                     if any(r.id == rep_id for r in c.reps)]
    if since_date:
        customers = [c for c in customers
                     if c.created_at and c.created_at.date() >= since_date]
        customers.sort(key=lambda c: c.created_at, reverse=True)

    active_ids = _active_customer_ids(6)
    n_active = sum(1 for c in customers if c.id in active_ids)
    n_inactive = len(customers) - n_active
    if status == "active":
        customers = [c for c in customers if c.id in active_ids]
    elif status == "inactive":
        customers = [c for c in customers if c.id not in active_ids]

    rep_users = db.session.scalars(
        db.select(User).filter(User.is_active.is_(True),
                               User.role.in_(("rep", "sales_manager", "telesales")))
        .order_by(User.full_name)).all()
    return render_template("customers/index.html", customers=customers, cat=cat,
                           categories=_categories(), show_archived=show_archived,
                           n_archived=n_archived, status=status,
                           n_active=n_active, n_inactive=n_inactive,
                           active_ids=active_ids, since=since,
                           since_date=since_date, since_label=since_label,
                           rep_id=rep_id, rep_users=rep_users)


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
    if request.args.get("archived") != "1":
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
    from datetime import timedelta
    show_archived = request.args.get("archived") == "1"
    rows = db.session.scalars(
        db.select(Customer).filter_by(segment="distributor").order_by(Customer.name)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        rows = [c for c in rows if can_see_customer(current_user, c)]
    n_archived = sum(1 for c in rows if c.archived)
    rows = [c for c in rows if bool(c.archived) == show_archived]
    rep_id = request.args.get("rep", type=int)
    if rep_id:
        rows = [c for c in rows if any(r.id == rep_id for r in c.reps)]
    since = request.args.get("since")
    since_date = None
    if since == "mtd":
        today = date.today()
        since_date = date(today.year, today.month, 1)
    elif since:
        try:
            since_date = date.today() - timedelta(days=int(since))
        except ValueError:
            pass
    if since_date:
        rows = [c for c in rows
                if c.created_at and c.created_at.date() >= since_date]
        rows.sort(key=lambda c: c.created_at, reverse=True)
    rep_users = db.session.scalars(
        db.select(User).filter(User.is_active.is_(True),
                               User.role.in_(("rep", "sales_manager", "telesales")))
        .order_by(User.full_name)).all()
    return render_template("customers/distributors.html", distributors=rows,
                           show_archived=show_archived, n_archived=n_archived,
                           rep_id=rep_id, rep_users=rep_users, since=since)


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
        if float(i.total or 0) > 0
        and i.payment_status in ("Not Paid", "Partially Paid", "In Payment"))

    # Portal access: linked login plus the welcome-email / password-reset trail
    from models import AuditLog
    portal_user = _portal_user_for(c)
    portal_trail = []
    if portal_user:
        portal_trail = db.session.scalars(
            db.select(AuditLog)
            .where(AuditLog.entity_type == "user",
                   AuditLog.entity_id == portal_user.id,
                   AuditLog.action.in_(("welcome_email", "portal_pw_reset")))
            .order_by(AuditLog.ts.desc()).limit(5)).all()

    return render_template("customers/detail.html", customer=c,
                           visit_outcomes=VISIT_OUTCOMES, call_outcomes=CALL_OUTCOMES,
                           deal_stages=Deal.STAGES,
                           can_log=has_perm(current_user, "log_activity"),
                           can_allocate=can_allocate_pricelists(current_user),
                           hist_years=hist_years, hist_top=hist_top,
                           hist_returns=hist_returns, inv_recent=inv_recent,
                           inv_count=inv_count, inv_outstanding=inv_outstanding,
                           portal_user=portal_user, portal_trail=portal_trail)


@bp.route("/invoice/<int:inv_id>")
@login_required
def invoice_detail(inv_id):
    """One imported invoice or credit note, full header detail.

    The Odoo export carries headers only (no line items), so this shows
    everything the import has: dates, amounts, VAT, status, salesperson,
    EFRIS. Access follows the customer: whoever may see the customer may
    see their documents; unmatched documents need manage rights."""
    from models import Invoice
    inv = db.session.get(Invoice, inv_id)
    if inv is None:
        abort(404)
    if inv.customer is not None:
        assert_can_see_customer(current_user, inv.customer)
    elif not current_user.can_manage_all:
        abort(403)
    is_credit = (inv.number or "").upper().startswith("RINV") or \
        float(inv.total or 0) < 0
    vat = None
    if inv.total is not None and inv.untaxed is not None:
        vat = float(inv.total) - float(inv.untaxed)
    # Same customer, around the same date — quick context for the viewer.
    related = []
    if inv.customer_id:
        related = db.session.scalars(
            db.select(Invoice).where(Invoice.customer_id == inv.customer_id,
                                     Invoice.id != inv.id)
            .order_by(Invoice.invoice_date.desc()).limit(10)).all()
    return render_template("customers/invoice_detail.html", inv=inv,
                           is_credit=is_credit, vat=vat, related=related)


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
        db.session.flush()
        login, temp_pw = _provision_portal_login(c)
        log("customer_onboard", "customer", None,
            detail=f"{c.name} registered by {current_user.full_name} (pending allocation)")
        db.session.commit()
        extra = ""
        if login:
            sent, reason = _send_welcome_email(c, login, temp_pw)
            if sent:
                extra = (f" Portal login '{login.username}' created and the "
                         f"welcome email with the login details was sent to "
                         f"{c.email}.")
            else:
                extra = (f" Portal login: username '{login.username}', "
                         f"temporary password '{temp_pw}'. Shown once only — "
                         f"pass both to the customer yourself (email not "
                         f"sent: {reason}). They set their own password at "
                         "first sign-in.")
        flash("Customer registered. The Pricing Officer will allocate a pricelist and "
              f"approve the credit terms before ordering.{extra}", "success")
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


@bp.route("/<int:customer_id>/portal/resend-email", methods=["POST"])
@login_required
@manager_required
def portal_resend_email(customer_id):
    """Reset the temporary password and resend the welcome email."""
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    u = _portal_user_for(c)
    if u is None:
        flash("No portal login exists for this account yet.", "warning")
        return redirect(url_for("customers.detail", customer_id=c.id))
    temp_pw = _reset_portal_password(u)
    db.session.commit()
    sent, reason = _send_welcome_email(c, u)
    if sent:
        flash(f"Fresh welcome email sent to {c.email} with a new activation "
              "link. Earlier links no longer work.", "success")
    else:
        flash(f"Password was reset but the email was not sent ({reason}). "
              f"Temporary password for '{u.username}': '{temp_pw}'. "
              "Shown once only.", "warning")
    return redirect(url_for("customers.detail", customer_id=c.id))


@bp.route("/<int:customer_id>/portal/welcome.pdf", methods=["POST"])
@login_required
@manager_required
def portal_welcome_pdf(customer_id):
    """Reset the temporary password and download the welcome sheet PDF with
    the fresh credentials and the short portal guide."""
    from flask import Response
    from services import exports
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    u = _portal_user_for(c)
    if u is None:
        flash("No portal login exists for this account yet.", "warning")
        return redirect(url_for("customers.detail", customer_id=c.id))
    temp_pw = _reset_portal_password(u)
    db.session.commit()
    portal_url = request.url_root.rstrip("/") + "/login"
    data = exports.portal_welcome_pdf(c, u, temp_pw, portal_url)
    from werkzeug.utils import secure_filename
    fname = secure_filename(f"Portal_Access_{c.name}.pdf") or "Portal_Access.pdf"
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@bp.route("/<int:customer_id>/portal/create", methods=["POST"])
@login_required
@manager_required
def portal_create(customer_id):
    """Create the portal login for an account that predates auto-provisioning."""
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    if _portal_user_for(c):
        flash("This account already has a portal login.", "warning")
        return redirect(url_for("customers.detail", customer_id=c.id))
    login, temp_pw = _provision_portal_login(c)
    db.session.commit()
    sent, reason = _send_welcome_email(c, login, temp_pw)
    if sent:
        flash(f"Portal login '{login.username}' created and the welcome email "
              f"was sent to {c.email}.", "success")
    else:
        flash(f"Portal login created: username '{login.username}', temporary "
              f"password '{temp_pw}'. Shown once only (email not sent: "
              f"{reason}).", "warning")
    return redirect(url_for("customers.detail", customer_id=c.id))


@bp.route("/new", methods=["GET", "POST"])
@login_required
@manager_required
def new():
    if request.method == "POST":
        c = Customer()
        _save_fields(c, request.form)
        db.session.add(c)
        db.session.flush()
        login, temp_pw = _provision_portal_login(c)
        log("customer_create", "customer", None, detail=c.name)
        db.session.commit()
        if login:
            sent, reason = _send_welcome_email(c, login, temp_pw)
            if sent:
                flash(f"Customer created. Portal login '{login.username}' "
                      f"created and the welcome email with the login details "
                      f"was sent to {c.email}.", "success")
            else:
                flash(f"Customer created. Portal login: username "
                      f"'{login.username}', temporary password '{temp_pw}'. "
                      f"Shown once only — pass both to the customer yourself "
                      f"(email not sent: {reason}). They set their own "
                      "password at first sign-in.", "warning")
        else:
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
        db.session.flush()
        login, temp_pw = _provision_portal_login(c)
        log("customer_create", "customer", None, detail=f"distributor {c.name}")
        db.session.commit()
        if login:
            sent, reason = _send_welcome_email(c, login, temp_pw)
            if sent:
                flash(f"Distributor created. Portal login '{login.username}' "
                      f"created and the welcome email with the login details "
                      f"was sent to {c.email}.", "success")
            else:
                flash(f"Distributor created. Portal login: username "
                      f"'{login.username}', temporary password '{temp_pw}'. "
                      f"Shown once only — pass both to the distributor "
                      f"yourself (email not sent: {reason}). They set their "
                      "own password at first sign-in.", "warning")
        else:
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
