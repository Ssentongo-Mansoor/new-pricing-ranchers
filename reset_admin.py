"""Reset (or create) an admin login.

Usage:
    python reset_admin.py                 # interactive prompts
    python reset_admin.py USERNAME PASS   # non-interactive

If the username exists it is promoted to admin with edit rights and the new
password; otherwise a new admin is created. Passwords are stored hashed.
"""
import getpass
import sys

from app import create_app
from extensions import db
from models import User
from services.security import hash_password


def main():
    if len(sys.argv) >= 3:
        username, password = sys.argv[1], sys.argv[2]
    else:
        username = input("Admin username (e.g. admin): ").strip()
        password = getpass.getpass("New password (min 8 chars): ")
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)

    app = create_app()
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username=username))
        if user is None:
            user = User(username=username, full_name=username, role="admin",
                        can_edit=True, is_active=True,
                        password_hash=hash_password(password))
            db.session.add(user)
            action = "created"
        else:
            user.role = "admin"
            user.can_edit = True
            user.is_active = True
            user.failed_attempts = 0
            user.locked_until = None
            user.password_hash = hash_password(password)
            action = "reset"
        db.session.commit()
        print(f"Admin '{username}' {action}. You can now log in with the new password.")


if __name__ == "__main__":
    main()
