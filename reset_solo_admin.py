"""Reset to a single admin account.

Sets the 'admin' login to a password you supply (creating it if needed) and
removes every other user. The password is read from the ADMIN_PASS environment
variable, or prompted for interactively. Never hard-code a password here.
Run once from the app folder:

    ADMIN_PASS='...' python reset_solo_admin.py
    # or just: python reset_solo_admin.py   (prompts for the password)
"""
import getpass
import os
import sys

from app import create_app
from extensions import db
from models import User
from services.security import hash_password

ADMIN_USER = "admin"


def _get_password():
    pw = os.environ.get("ADMIN_PASS")
    if not pw:
        pw = getpass.getpass("New admin password (min 8 chars): ")
    if len(pw) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)
    return pw


def main():
    admin_pass = _get_password()
    app = create_app()
    with app.app_context():
        admin = db.session.scalar(db.select(User).filter_by(username=ADMIN_USER))
        if admin is None:
            admin = User(username=ADMIN_USER, full_name="Administrator", role="admin",
                         can_edit=True, is_active=True,
                         password_hash=hash_password(admin_pass))
            db.session.add(admin)
            db.session.flush()
            print(f"Created admin '{ADMIN_USER}'.")
        else:
            admin.role = "admin"
            admin.can_edit = True
            admin.is_active = True
            admin.failed_attempts = 0
            admin.locked_until = None
            admin.password_hash = hash_password(admin_pass)
            print(f"Reset admin '{ADMIN_USER}'.")

        removed = 0
        for u in db.session.scalars(db.select(User).where(User.id != admin.id)).all():
            u.assigned_customers = []   # clear rep→customer links first
            db.session.delete(u)
            removed += 1

        db.session.commit()
        print(f"Removed {removed} other user(s).")
        print(f"Login is now:  {ADMIN_USER} / (the password you just set)")


if __name__ == "__main__":
    main()
