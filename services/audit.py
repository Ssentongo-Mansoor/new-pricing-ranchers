"""Audit logging helper. Every price/rate/rights/offer change flows through here."""
from flask_login import current_user

from extensions import db
from models import AuditLog


def log(action, entity_type=None, entity_id=None, field=None,
        old_value=None, new_value=None, detail=None, commit=False):
    uid = getattr(current_user, "id", None) if current_user else None
    uname = getattr(current_user, "username", None) if current_user else None
    entry = AuditLog(
        user_id=uid, username=uname, action=action,
        entity_type=entity_type, entity_id=entity_id, field=field,
        old_value=None if old_value is None else str(old_value),
        new_value=None if new_value is None else str(new_value),
        detail=detail,
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry
