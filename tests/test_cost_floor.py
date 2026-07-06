"""Cost floor acceptance test — QA audit 5 Jul 2026 feature gap.

Run against a COPY of the live database:

    rm -f /tmp/cf.db*; cp instance/pricing.db /tmp/cf.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/cf.db COOKIE_INSECURE=1 \
    python3 tests/test_cost_floor.py

Proves (cost convention: unit_cost is UGX PER KG, price basis follows the
line's pack size — per pack when the pack parses to a weight, per kg when the
pack is "KG"/blank, unguarded when the basis is unknown):
  1. below_cost_error: no cost -> no guard; below cost -> error; at cost ->
     allowed; discount counts; pack prices compare against cost/kg x weight;
     unknown pack basis stands aside; zero price is a placeholder, ignored.
  2. Inline pricelist price edit below cost returns 400 with the reason.
  3. Inline edit at/above cost still works.
  4. unit_cost column exists after boot (migration ladder).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-only-secret")
os.environ.setdefault("COOKIE_INSECURE", "1")
if "DATABASE_URL" not in os.environ:
    print("Refusing to run without DATABASE_URL.")
    sys.exit(2)

from sqlalchemy import text  # noqa: E402

from app import app  # noqa: E402
from extensions import db  # noqa: E402
from models import Product, Pricelist, PricelistLine, LinePrice, User  # noqa: E402
from services.cost_guard import below_cost_error  # noqa: E402

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
    cols = {r[1] for r in db.session.execute(text("PRAGMA table_info(product)"))}
    check("product.unit_cost column exists", "unit_cost" in cols)

    # Pick a UGX pricelist line with a price and a per-kg product, so the
    # route checks below compare per kg (pack "KG"/blank).
    line = db.session.scalar(
        db.select(PricelistLine).join(Pricelist).join(Product)
        .join(LinePrice, LinePrice.line_id == PricelistLine.id)
        .where(Pricelist.currency == "UGX", Pricelist.archived.is_(False),
               LinePrice.amount.isnot(None),
               db.func.lower(db.func.coalesce(PricelistLine.pack_size, "kg"))
               .in_(("kg", ""))).limit(1))
    if line is None:
        print("No priced per-kg UGX pricelist line found; cannot continue.")
        sys.exit(2)
    product = line.product
    lp = next(p for p in line.prices if p.amount is not None)
    tier_key = lp.tier.key

    # 1. Service-level behaviour (pack_size passed explicitly per line).
    product.unit_cost = None
    check("no cost -> no guard",
          below_cost_error(product, 100, "UGX", pack_size="kg") is None)
    product.unit_cost = 10000    # UGX per kg
    check("per kg below cost -> blocked",
          below_cost_error(product, 9999, "UGX", pack_size="kg") is not None)
    check("per kg at cost -> allowed",
          below_cost_error(product, 10000, "UGX", pack_size="kg") is None)
    check("per kg above cost -> allowed",
          below_cost_error(product, 15000, "UGX", pack_size="kg") is None)
    check("discount pushes below -> blocked",
          below_cost_error(product, 11000, "UGX", discount_pct=15,
                           pack_size="kg") is not None)
    check("discount stays above -> allowed",
          below_cost_error(product, 11000, "UGX", discount_pct=5,
                           pack_size="kg") is None)
    # Pack basis: floor for a 200g pack = 10,000 x 0.2 = 2,000.
    check("200g pack below pack floor -> blocked",
          below_cost_error(product, 1900, "UGX", pack_size="200g") is not None)
    check("200g pack above pack floor -> allowed",
          below_cost_error(product, 2100, "UGX", pack_size="200g") is None)
    check("unknown basis (4PCS) -> unguarded",
          below_cost_error(product, 1, "UGX", pack_size="4PCS") is None)
    check("zero price is a placeholder -> ignored",
          below_cost_error(product, 0, "UGX", pack_size="kg") is None)
    db.session.commit()

    admin_id = db.session.scalar(db.select(User.id).where(User.role == "admin"))
    line_id = line.id

c = app.test_client()
with c.session_transaction() as s:
    s.clear()
    s["_user_id"] = str(admin_id)
    s["_fresh"] = True

page = c.get("/pricelists/").get_data(as_text=True)
token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)

# 2. Route: below cost -> 400 with reason.
r = c.post(f"/pricelists/line/{line_id}/price",
           data={"tier": tier_key, "value": "9000", "csrf_token": token})
check("route: below-cost price refused (400)", r.status_code == 400,
      str(r.status_code))
check("route: reason names the cost", b"below cost" in r.data)

# 3. Route: above cost -> accepted.
r = c.post(f"/pricelists/line/{line_id}/price",
           data={"tier": tier_key, "value": "12000", "csrf_token": token})
check("route: above-cost price accepted", r.status_code == 200,
      str(r.status_code))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
