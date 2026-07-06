"""Token authentication for the machine-to-machine API (blueprints/api.py).

Keys are bearer tokens for server-to-server use (accounting/ERP, mobile app,
scripts). The raw key is shown once at creation; only a bcrypt hash is stored,
like a password. A key carries a scope ('read' or 'read_write') and acts as a
chosen user so audit trails stay meaningful.

Header on every call:  Authorization: Bearer rf_<prefix>_<secret>
"""
import secrets
from datetime import datetime
from functools import wraps

from flask import request, jsonify, g

from extensions import db
from models import ApiKey
from services.security import hash_password, verify_password

KEY_PREFIX = "rf"          # all keys look like rf_<prefix>_<secret>


def generate_key():
    """Return (raw_key, prefix, key_hash). Store prefix + hash; give raw once."""
    prefix = secrets.token_hex(4)          # 8 hex chars, used for fast lookup
    secret = secrets.token_urlsafe(32)     # the actual entropy
    raw = f"{KEY_PREFIX}_{prefix}_{secret}"
    return raw, prefix, hash_password(raw)


def create_key(label, scope="read", acts_as_user_id=None, created_by_id=None):
    """Mint and persist a key. Returns (ApiKey, raw_key). Raw is not stored."""
    if scope not in ("read", "read_write"):
        raise ValueError("scope must be 'read' or 'read_write'")
    raw, prefix, key_hash = generate_key()
    k = ApiKey(label=label, prefix=prefix, key_hash=key_hash, scope=scope,
               acts_as_user_id=acts_as_user_id, created_by_id=created_by_id, active=True)
    db.session.add(k)
    db.session.commit()
    return k, raw


def _extract_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    # allow X-API-Key as a convenience for tools that cannot set Authorization
    return request.headers.get("X-API-Key", "").strip() or None


def resolve_key(raw):
    """Return the matching active ApiKey for a raw token, or None."""
    if not raw or raw.count("_") < 2:
        return None
    try:
        _, prefix, _ = raw.split("_", 2)
    except ValueError:
        return None
    # Look up by prefix (indexed), then verify the hash. Prefix is not a secret.
    candidates = db.session.scalars(
        db.select(ApiKey).filter_by(prefix=prefix, active=True)
    ).all()
    # QA audit 5 Jul 2026 M4: verify EVERY candidate instead of returning on
    # the first match, so response time does not vary with match position and
    # leak which prefixes exist. bcrypt itself is constant-time per check.
    match = None
    for k in candidates:
        if verify_password(raw, k.key_hash) and match is None:
            match = k
    return match


def api_key_required(scope="read"):
    """Guard an API route. Sets g.api_key and g.api_user. 401 if no/invalid key,
    403 if the key lacks write scope on a write endpoint."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            raw = _extract_bearer()
            key = resolve_key(raw)
            if key is None:
                return jsonify(error="unauthorized",
                               message="Missing or invalid API key."), 401
            if scope == "read_write" and not key.can_write:
                return jsonify(error="forbidden",
                               message="This key is read-only."), 403
            # record usage (best effort)
            try:
                key.last_used_at = datetime.utcnow()
                key.request_count = (key.request_count or 0) + 1
                db.session.commit()
            except Exception:
                db.session.rollback()
            g.api_key = key
            g.api_user = key.acts_as_user
            return fn(*args, **kwargs)
        return wrapper
    return decorator
