"""Batch/lot traceability acceptance test — QA audit 5 Jul 2026 feature gap.

Run against a COPY of the live database:

    rm -f /tmp/lot.db*; cp instance/pricing.db /tmp/lot.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/lot.db COOKIE_INSECURE=1 \
    python3 tests/test_lot_traceability.py

Proves:
  1. lot_number/expiry columns exist on stock_movement and prod_production.
  2. Recording production with an explicit lot stores it on BOTH the
     production record and the stock movement (recall = one query by lot).
  3. Recording production without a lot auto-generates LYYYYMMDD-<article>.
  4. A stock receipt carries its lot and expiry.
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-only-secret")
os.environ.setdefault("COOKIE_INSECURE", "1")
if "DATABASE_URL" not in os.environ:
    print("Refusing to run without DATABASE_URL.")
    sys.exit(2)

from sqlalchemy import text  # noqa: E402

from app import app  # noqa: E402
from extensions import db  # noqa: E402
from models import Product, ProdProduction, StockMovement, User  # noqa: E402
from services import production as prod  # noqa: E402
from services import stock as stock_svc  # noqa: E402

PASS = FAIL = 0


def check(name, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {note}")


with app.app_context():
    for table in ("stock_movement", "prod_production"):
        cols = {r[1] for r in db.session.execute(text(f"PRAGMA table_info({table})"))}
        check(f"{table}.lot_number exists", "lot_number" in cols)
        check(f"{table}.expiry exists", "expiry" in cols)

    product = db.session.scalar(db.select(Product).where(Product.status == "active").limit(1))
    user = db.session.scalar(db.select(User).where(User.role == "admin").limit(1))

    # 2. Explicit lot + expiry.
    ok, _ = prod.record_production(product, 5, user, note="lot test",
                                   lot_number="LOT-TEST-001",
                                   expiry=date(2026, 8, 1))
    check("production recorded", ok)
    pp = db.session.scalar(db.select(ProdProduction)
                           .where(ProdProduction.lot_number == "LOT-TEST-001"))
    check("lot on production record", pp is not None)
    check("expiry on production record", pp and pp.expiry == date(2026, 8, 1))
    mv = db.session.scalar(db.select(StockMovement)
                           .where(StockMovement.lot_number == "LOT-TEST-001"))
    check("lot on stock movement (recall query works)", mv is not None)
    check("movement is the production movement",
          mv and pp and pp.stock_movement_id == mv.id)

    # 3. Auto-generated lot.
    ok, _ = prod.record_production(product, 3, user)
    check("production without lot recorded", ok)
    expected = f"L{datetime.utcnow():%Y%m%d}-{product.article_no}"
    pp2 = db.session.scalar(db.select(ProdProduction)
                            .where(ProdProduction.lot_number == expected)
                            .order_by(ProdProduction.id.desc()))
    check(f"auto lot generated ({expected})", pp2 is not None)

    # 4. Receipt with lot + expiry.
    stock_svc.apply_movement(product, 10, "receipt", user_id=user.id,
                             note="receipt lot test", lot_number="LOT-RCV-9",
                             expiry=date(2026, 9, 1))
    db.session.commit()
    mv2 = db.session.scalar(db.select(StockMovement)
                            .where(StockMovement.lot_number == "LOT-RCV-9"))
    check("receipt carries lot", mv2 is not None)
    check("receipt carries expiry", mv2 and mv2.expiry == date(2026, 9, 1))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
