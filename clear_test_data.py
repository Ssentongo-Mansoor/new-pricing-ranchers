"""Clear transactional test data — orders, offers and messages — while keeping
pricelists, products, customers, distributors, users, exchange rates and
promotions intact.

Usage:
    python clear_test_data.py          # asks for confirmation
    python clear_test_data.py --yes    # no prompt (for scripts)

This does NOT touch the audit log (your historical record). Order/offer numbers
restart from 0001 afterwards.
"""
import sys

from app import create_app
from extensions import db
from models import SalesOrder, SalesOrderLine, Offer, OfferLine, Message


def main():
    app = create_app()
    with app.app_context():
        n_orders = db.session.scalar(db.select(db.func.count(SalesOrder.id))) or 0
        n_offers = db.session.scalar(db.select(db.func.count(Offer.id))) or 0
        n_msgs = db.session.scalar(db.select(db.func.count(Message.id))) or 0

        print(f"This will permanently delete:\n"
              f"  - {n_orders} sales order(s) and their lines\n"
              f"  - {n_offers} offer(s) and their lines\n"
              f"  - {n_msgs} message(s)\n"
              f"Pricelists, products, customers, users, rates and promotions are kept.")
        if "--yes" not in sys.argv:
            if input("Type YES to proceed: ").strip() != "YES":
                print("Cancelled. Nothing was deleted.")
                return

        # Offers first (they reference orders), then messages, then orders.
        for o in db.session.scalars(db.select(Offer)).all():
            db.session.delete(o)          # cascades offer lines
        db.session.query(Message).delete(synchronize_session=False)
        for so in db.session.scalars(db.select(SalesOrder)).all():
            db.session.delete(so)         # cascades order lines
        db.session.commit()

        # Safety sweep for any orphaned lines (if FK cascade was off).
        db.session.query(SalesOrderLine).delete(synchronize_session=False)
        db.session.query(OfferLine).delete(synchronize_session=False)
        db.session.commit()

        print("Done — orders, offers and messages cleared. "
              "Pricelists and customers are untouched.")


if __name__ == "__main__":
    main()
