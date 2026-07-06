"""Editable global settings stored in the database, with sensible defaults."""
from extensions import db
from models import Setting

DEFAULTS = {
    "company_name": "Ranchers Finest U Ltd",
    "app_name": "Ranchers Finest Sales Hub",
    "vat_rate": "18",
    "base_currency": "UGX",
    "usd_round": "2",     # decimals for USD display
    "ugx_round": "0",     # decimals for UGX display
    "offer_validity_days": "30",
    "sla_dispatch_hours": "24",   # target: placed -> dispatched
    "sla_delivery_hours": "48",   # target: placed -> delivered
}


def get(key, default=None):
    row = db.session.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    return DEFAULTS.get(key, default)


def get_int(key, default=0):
    try:
        return int(float(get(key, default)))
    except (TypeError, ValueError):
        return default


def get_float(key, default=0.0):
    try:
        return float(get(key, default))
    except (TypeError, ValueError):
        return default


def set_value(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    return row


def ensure_defaults():
    for k, v in DEFAULTS.items():
        if db.session.get(Setting, k) is None:
            db.session.add(Setting(key=k, value=v))
    db.session.commit()
