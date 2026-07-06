"""Configuration for the Ranchers Finest pricing application.

All settings can be overridden with environment variables so the same code runs
in development (SQLite, ``flask run``) and production (``gunicorn`` behind Nginx).
The database is SQLite by default; point ``DATABASE_URL`` at PostgreSQL to migrate.
"""
import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)


class Config:
    # No fallback: create_app fails hard in production if this is unset or the
    # committed placeholder. Dev gets a generated random key (see app.create_app).
    SECRET_KEY = os.environ.get("SECRET_KEY")

    # SQLite file by default; set DATABASE_URL=postgresql+psycopg://... to switch.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(INSTANCE_DIR, "pricing.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # The costing module is native since 2 July 2026: ingredients, recipes,
    # carcass cuts, spice mixes, overhead and packaging all live in pricing.db
    # (tables categories/settings/ingredients/recipes/... ported from the
    # standalone meat-costing-app). No secondary bind, no external file.

    # Sessions: expire after 8 hours of inactivity; "remember me" lasts 30 days.
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    SESSION_REFRESH_EACH_REQUEST = True

    # Cookie hardening. Secure flags are gated so local HTTP dev still works:
    # set COOKIE_INSECURE=1 in development to serve cookies over plain HTTP.
    _COOKIE_SECURE = os.environ.get("COOKIE_INSECURE") != "1"
    SESSION_COOKIE_SECURE = _COOKIE_SECURE
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = _COOKIE_SECURE
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # CSRF: tokens must not expire mid-session (long forms, portal ordering).
    WTF_CSRF_TIME_LIMIT = None

    # Login security.
    MAX_LOGIN_ATTEMPTS = 5
    LOCKOUT_MINUTES = 15

    # Upload limits (pricelist imports, catalogue PDFs which are image-heavy).
    MAX_CONTENT_LENGTH = 80 * 1024 * 1024  # 80 MB
    UPLOAD_DIR = os.path.join(INSTANCE_DIR, "uploads")

    # Seed data location (the six source workbooks).
    SEED_DIR = os.environ.get(
        "SEED_DIR", os.path.join(os.path.dirname(BASE_DIR), "seed_data")
    )

    # Pricing defaults (also stored as editable Settings; these are fallbacks).
    DEFAULT_VAT_RATE = 18.0
    BASE_CURRENCY = "UGX"

    # Production UI (decided 6 Jul 2026): production moves to its own app on
    # the same database. This deployment hides the production screens; the
    # factory deployment sets PRODUCTION_UI=1. An ENVIRONMENT switch on
    # purpose — a database flag would flip both apps at once, since they
    # share the database. Models and data stay untouched either way.
    PRODUCTION_UI = os.environ.get("PRODUCTION_UI") == "1"
