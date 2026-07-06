"""Auth and audit adapters for the ported costing module.

The standalone costing app had its own users, roles, and audit table. Inside
the pricing app those concerns already exist, so:

  * editor_required / admin_required map onto the pricing permission matrix
    (capability 'edit_costing'; admins pass everything as usual);
  * viewing is gated per-blueprint with require_costing_view() ('view_costing');
  * log_action writes the pricing AuditLog through the same shape the costing
    code expects.

The costing role model (admin/manager/viewer + hide_costs) is retired.
"""
from functools import wraps

from flask import abort
from flask_login import current_user, login_required

from extensions import db
from models import AuditLog


def require_costing_view():
    """Blueprint-level view gate; call from a before_request handler."""
    from services.permissions import has_perm
    if not has_perm(current_user, "view_costing"):
        abort(403)


def editor_required(fn):
    """Write access: capability 'edit_costing' (admins always pass)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from services.permissions import has_perm
        if not current_user.is_authenticated:
            abort(401)
        if not has_perm(current_user, "edit_costing"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


# The costing app used admin_required only for user management (dropped) and
# a couple of destructive settings actions; edit_costing governs those now.
admin_required = editor_required


def roles_required(*_roles):
    """Legacy decorator shape; maps to the edit gate."""
    def decorator(fn):
        return editor_required(fn)
    return decorator


def log_action(action, entity_type, entity_id=None, field=None, old=None, new=None):
    """Write an audit-log row (pricing AuditLog). Caller commits."""
    try:
        eid = int(entity_id) if entity_id is not None else None
    except (TypeError, ValueError):
        eid = None
    entry = AuditLog(
        user_id=(current_user.id if current_user.is_authenticated else None),
        username=(current_user.username if current_user.is_authenticated else "system"),
        action=f"costing_{action}",
        entity_type=entity_type,
        entity_id=eid,
        field=field,
        old_value=None if old is None else str(old)[:255],
        new_value=None if new is None else str(new)[:255],
    )
    db.session.add(entry)
    return entry


def fmt_money(value):
    try:
        return "{:,.0f}".format(float(value))
    except (TypeError, ValueError):
        return "0"


def fmt_money2(value):
    try:
        return "{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "0.00"
