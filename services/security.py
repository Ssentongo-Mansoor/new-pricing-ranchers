"""Server-side access control: password hashing and route guards.

Authorization is enforced here on every protected route, not only hidden in the
UI. A read-only user is blocked from any write even via a direct request.
"""
from functools import wraps

import bcrypt
from flask import abort
from flask_login import current_user


# ---- passwords ----
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


# ---- route guards ----
def roles_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def manager_required(fn):
    """Catalogue management — gated by the 'manage_catalogue' role permission."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from services.permissions import has_perm
        if not current_user.is_authenticated or not has_perm(current_user, "manage_catalogue"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def capability_required(cap):
    """Generic guard for a named role capability (see services.permissions)."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from services.permissions import has_perm
            if not current_user.is_authenticated or not has_perm(current_user, cap):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def edit_required(fn):
    """Block users without the independent edit/pricing right from any write."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.may_edit_prices:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


# ---- assignment-based visibility ----
def can_see_customer(user, customer):
    # Admins, managers, order managers and telesales see every customer; reps only assigned.
    if getattr(user, "sees_all_customers", False) or user.can_manage_all:
        return True
    # A sales manager sees the customers of the reps allocated to them.
    if getattr(user, "is_sales_manager", False):
        rep_ids = {r.id for r in getattr(user, "managed_reps", [])}
        if any(rp.id in rep_ids for rp in customer.reps):
            return True
    return any(c.id == customer.id for c in user.assigned_customers)


def can_see_customer_pricelist(user, pricelist):
    """Generic lists are visible to everyone. Tailor-made (customer) lists follow
    customer visibility: admins/managers, the pricing officer and other
    whole-base roles see all; reps see only their assigned customers' lists."""
    if not pricelist.is_customer:
        return True
    if user.can_manage_all or getattr(user, "sees_all_customers", False):
        return True
    if pricelist.customer is None:
        return False
    return can_see_customer(user, pricelist.customer)


def can_allocate_pricelists(user):
    """Only the pricing officer (and admin) may link pricelists to customers."""
    return bool(user and (user.is_admin or getattr(user, "is_pricing_officer", False)))


def assert_can_see_customer(user, customer):
    if not can_see_customer(user, customer):
        abort(403)


def assert_can_see_pricelist(user, pricelist):
    if not can_see_customer_pricelist(user, pricelist):
        abort(403)
