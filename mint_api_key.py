"""Mint an API key for the REST API.

Usage:
    python3 mint_api_key.py --label "QuickBooks" --scope read_write --user admin

Prints the raw key ONCE. Store it now; it is not recoverable (only a hash is kept).
"""
import argparse

from app import create_app
from extensions import db
from models import User
from services.api_auth import create_key


def main():
    ap = argparse.ArgumentParser(description="Mint an API key.")
    ap.add_argument("--label", required=True, help="Human label, e.g. 'QuickBooks integration'")
    ap.add_argument("--scope", default="read", choices=["read", "read_write"],
                    help="read (default) or read_write")
    ap.add_argument("--user", default=None,
                    help="Username the key acts as for audit/created_by (optional)")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        acts_as_id = None
        if args.user:
            u = db.session.scalar(db.select(User).filter_by(username=args.user))
            if not u:
                raise SystemExit(f"No user named {args.user!r}.")
            acts_as_id = u.id
        key, raw = create_key(args.label, scope=args.scope,
                              acts_as_user_id=acts_as_id, created_by_id=acts_as_id)
        print("\nAPI key created. Store this now — it will not be shown again:\n")
        print("  " + raw + "\n")
        print(f"  label:  {key.label}")
        print(f"  scope:  {key.scope}")
        print(f"  id:     {key.id}")
        print("\nUse it as:  Authorization: Bearer " + raw + "\n")


if __name__ == "__main__":
    main()
