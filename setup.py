"""One-time setup: create the database schema and the first admin account.

Usage:
    python setup.py

Set RF_ADMIN_USER / RF_ADMIN_PASS / RF_ADMIN_NAME in the environment to run
non-interactively (useful for automated provisioning).
"""
import getpass
import os
import sys
from datetime import date, timedelta
from decimal import Decimal

from app import create_app
from extensions import db
from services.security import hash_password
from services import settings as settings_svc


DEFAULT_CATEGORIES = [
    ("Beef", ["Budget", "Marbled", "Prime", "Choice"]),
    ("Pork", []),
    ("Lamb", []),
    ("Goat", []),
    ("Chicken", []),
    ("Sausages", ["Premium", "Breakfast", "BBQ"]),
    ("Hotdogs & Viennas", []),
    ("Bacon", []),
    ("Cold Meats", []),
    ("Burgers", []),
    ("Ready-to-Eat", []),
    ("Betar", []),
]


def seed_categories():
    from models import Category
    for parent_name, children in DEFAULT_CATEGORIES:
        parent = db.session.scalar(
            db.select(Category).filter_by(name=parent_name, parent_id=None))
        if parent is None:
            parent = Category(name=parent_name)
            db.session.add(parent)
            db.session.flush()
        for child in children:
            exists = db.session.scalar(
                db.select(Category).filter_by(name=child, parent_id=parent.id))
            if exists is None:
                db.session.add(Category(name=child, parent_id=parent.id))
    db.session.commit()


def seed_starter_rate(admin_id):
    """Create a starter UGX->USD rate so USD exports/offers work out of the box."""
    from models import ExchangeRate
    if db.session.scalar(db.select(ExchangeRate).filter_by(quote_ccy="USD")):
        return
    db.session.add(ExchangeRate(
        base_ccy="UGX", quote_ccy="USD", rate=Decimal("3800"),
        effective_date=date.today(), expiry_date=date.today() + timedelta(days=90),
        created_by=admin_id))
    db.session.commit()
    print("  + starter exchange rate UGX 3,800 / USD added (update it in the app).")


def create_admin():
    from models import User
    if db.session.scalar(db.select(User).filter_by(role="admin")):
        print("An admin account already exists; skipping admin creation.")
        return None

    username = os.environ.get("RF_ADMIN_USER")
    password = os.environ.get("RF_ADMIN_PASS")
    full_name = os.environ.get("RF_ADMIN_NAME")

    if not username:
        print("\n--- Create the first administrator ---")
        username = input("Admin username: ").strip()
        full_name = input("Full name: ").strip() or username
        while True:
            password = getpass.getpass("Password (min 8 chars): ")
            if len(password) < 8:
                print("  Too short, try again.")
                continue
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("  Passwords do not match, try again.")
                continue
            break
    else:
        full_name = full_name or username
        if not password or len(password) < 8:
            print("RF_ADMIN_PASS must be at least 8 characters.")
            sys.exit(1)

    admin = User(username=username, full_name=full_name, role="admin",
                 can_edit=True, is_active=True, password_hash=hash_password(password))
    db.session.add(admin)
    db.session.commit()
    print(f"  + admin '{username}' created (with edit/pricing rights).")
    return admin


def _clean_stale_empty_db():
    """Remove a leftover 0-byte SQLite DB (and its journal) so a fresh setup
    never inherits a half-created file."""
    from config import INSTANCE_DIR
    dbfile = os.path.join(INSTANCE_DIR, "pricing.db")
    try:
        if os.path.exists(dbfile) and os.path.getsize(dbfile) == 0:
            os.remove(dbfile)
            for ext in ("-journal", "-wal", "-shm"):
                j = dbfile + ext
                if os.path.exists(j):
                    os.remove(j)
            print("  (removed an empty leftover pricing.db)")
    except OSError:
        pass


def main():
    app = create_app()
    _clean_stale_empty_db()
    with app.app_context():
        print("Creating database schema...")
        db.create_all()
        settings_svc.ensure_defaults()
        seed_categories()
        from blueprints.customers import ensure_customer_categories
        ensure_customer_categories()
        admin = create_admin()
        admin_id = admin.id if admin else None
        if admin_id is None:
            from models import User
            a = db.session.scalar(db.select(User).filter_by(role="admin"))
            admin_id = a.id if a else None
        seed_starter_rate(admin_id)
        print("\nSetup complete. Next: run  python importer.py  to load the seed pricelists.")


if __name__ == "__main__":
    main()
