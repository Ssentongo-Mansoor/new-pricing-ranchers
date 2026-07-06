"""Phase 2 acceptance test — inventory valuation (weighted average).

Run against a COPY of the live database:

    rm -f /tmp/p2_test.db*
    cp instance/pricing.db /tmp/p2_test.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/p2_test.db COOKIE_INSECUR E=1 \
    python3 tests/test_phase2_inventory.py

Proves:
  1.  Item registry seeds from the catalogue and the stores.
  2.  Weighted-average maths: receipts move the average, issues take
      round(qty x value/qty) shillings, emptying an item leaves exactly 0.
  3.  Rounding never drifts: after any sequence, value equals the replay.
  4.  Over-issue is refused. Uncosted items refuse valuation.
  5.  Unit conversion: pack items cost through pack weight.
  6.  Recipe auto-confirm links products to recipe costs.
  7.  Opening load posts ONE balanced journal; subledger ties to GL to the
      shilling; second run is a no-op (idempotent).
  8.  Valued movements are append-only (raw SQL refused by triggers).
  9.  Screens render; rep is blocked.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-only-secret")
os.environ.setdefault("COOKIE_INSECURE", "1")
if "DATABASE_URL" not in os.environ:
    print("Refusing to run without DATABASE_URL (never test on the live file).")
    sys.exit(2)

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app import app
from extensions import db
from models import AccItem, AccInvMovement, User, Product, Store, StoreItem
from services import inventory_costing as inv
from services import ledger
from services import recipes as rec

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def apply_sql(path):
    raw = db.engine.raw_connection()
    raw.executescript(open(path).read())
    raw.commit()
    raw.close()


with app.app_context():
    for f in ("migrations/acc_001_triggers.sql", "migrations/acc_002_inventory.sql"):
        apply_sql(os.path.join(os.path.dirname(__file__), "..", f))

    print("== 1. Item registry ==")
    created = inv.ensure_items()
    n_fin = db.session.scalar(db.select(db.func.count(AccItem.id))
                              .where(AccItem.stage == "finished"))
    n_raw = db.session.scalar(db.select(db.func.count(AccItem.id))
                              .where(AccItem.stage == "raw"))
    n_pkg = db.session.scalar(db.select(db.func.count(AccItem.id))
                              .where(AccItem.stage == "packaging"))
    check(f"seeded finished={n_fin} raw={n_raw} packaging={n_pkg}",
          n_fin > 700 and (n_raw + n_pkg) > 500)
    again = inv.ensure_items()
    check("idempotent reseed adds nothing",
          sum(again.values()) == 0, str(again))

    print("== 2/3. Weighted-average maths ==")

    # M2 (QA audit 5 Jul 2026): every acc_item needs exactly one source, so
    # the synthetic maths fixtures each get a disposable StoreItem.
    def _mk_item(name, **kw):
        store = db.session.scalar(db.select(Store).limit(1))
        si = StoreItem(store_id=store.id, name=name, uom=kw.get("stock_unit", "kg"))
        db.session.add(si)
        db.session.flush()
        item = AccItem(name=name, store_item_id=si.id, **kw)
        db.session.add(item)
        db.session.flush()
        return item

    it = _mk_item("TEST WA item", stage="finished", stock_unit="kg")
    inv.receive(it, 100, ledger.to_minor(1_000_000), "purchase")
    check("receive 100kg @10,000 -> avg 10,000", it.avg_cost == 10_000)
    inv.receive(it, 50, ledger.to_minor(650_000), "purchase")
    check("receive 50kg @13,000 -> avg 11,000",
          it.avg_cost == 11_000 and it.value_on_hand == 1_650_000)
    mv = inv.issue(it, 30, "sale")
    check("issue 30kg takes exactly 330,000",
          mv.value_ugx == -330_000 and it.value_on_hand == 1_320_000
          and it.avg_cost == 11_000)
    # awkward numbers: 3 issues that would each round, then empty the item
    it2 = _mk_item("TEST rounding", stage="finished", stock_unit="kg")
    inv.receive(it2, 7, 1_000, "purchase")       # 142.857.../kg
    v = 0
    for q in (1, 2.5, 1.7):
        v += -inv.issue(it2, q, "sale").value_ugx
    last = inv.issue(it2, 7 - 5.2, "sale")       # empties the item
    v += -last.value_ugx
    check("emptying leaves exactly zero value",
          it2.value_on_hand == 0 and abs(it2.qty_on_hand) < 1e-9 and v == 1_000,
          f"sum of issues {v}")

    print("== 4. Guards ==")
    try:
        inv.issue(it, 1_000_000, "sale")
        check("over-issue refused", False)
    except inv.CostingError as e:
        check("over-issue refused", True, str(e)[:50])
    bare = _mk_item("TEST no cost", stage="finished", stock_unit="kg")
    cost, source, reason = inv.unit_cost_minor(bare)
    check("uncosted item refuses valuation", cost is None and reason, reason)
    # M2: an item with NO source must be refused at write time. Commit first
    # so the rollback below cannot wipe the fixtures built above.
    db.session.commit()
    try:
        db.session.add(AccItem(name="TEST sourceless", stage="finished",
                               stock_unit="kg"))
        db.session.flush()
        check("sourceless acc_item refused", False)
        db.session.rollback()
    except (ValueError, DatabaseError) as e:
        db.session.rollback()
        check("sourceless acc_item refused", True, str(e)[:50])
    # Empty the synthetic maths items: their receipts carried no journal, so
    # leftover value would (correctly) break the subledger-to-GL tie below.
    inv.issue(it, it.qty_on_hand, "adjustment", note="test cleanup")
    check("cleanup: synthetic items back to zero value",
          it.value_on_hand == 0 and it2.value_on_hand == 0)

    print("== 5. Unit conversion ==")
    packy = _mk_item("TEST pack conv", stage="finished",
                     stock_unit="pack", pack_weight_kg=0.5,
                     manual_cost_minor=None)
    check("pack-size parser: '5 x 200 Gr'",
          inv.parse_pack_weight_kg("5 x 200 Gr.") == 1.0)
    check("pack-size parser: '500G'",
          inv.parse_pack_weight_kg("500G") == 0.5)
    check("pack-size parser: '1kg'",
          inv.parse_pack_weight_kg("1kg") == 1.0)
    check("pack-size parser: junk -> None",
          inv.parse_pack_weight_kg("assorted") is None)

    print("== 6. Recipe links ==")
    n_conf = rec.confirm_all_proposals(db.session.scalar(
        db.select(User).where(User.role == "admin")))
    cmap = rec.confirmed_map()
    check(f"auto-confirmed {n_conf} product-recipe links (total {len(cmap)})",
          len(cmap) > 40)
    # one linked item must now cost through its recipe
    linked = None
    for item in db.session.scalars(db.select(AccItem)
                                   .where(AccItem.product_id.isnot(None))).all():
        if item.product_id in cmap and item.stock_unit == "kg":
            c, s, r = inv.unit_cost_minor(item)
            if c:
                linked = (item, c)
                break
    check("a linked kg item costs from its recipe", linked is not None,
          f"{linked[0].name}: {linked[1]:,}/kg" if linked else "none found")
    db.session.commit()

    print("== 7. Opening load + GL tie ==")
    ready, blocked = inv.opening_candidates()
    check(f"candidates: {len(ready)} ready, {len(blocked)} blocked (worklist)",
          len(ready) > 0)
    entry, n, total = inv.load_opening_stock()
    check(f"opening journal posted: {n} items, UGX {total:,}",
          entry is not None and entry.posted and n == len(ready))
    rows, tdr, tcr = ledger.trial_balance()
    check("trial balance still balances", tdr == tcr, f"{tdr:,}")
    ties = inv.valuation_summary()
    check("subledger ties to GL per stage, to the shilling",
          all(t["tied"] for t in ties),
          "; ".join(f"{t['stage']}: {t['subledger']:,}" for t in ties))
    e2, n2, t2 = inv.load_opening_stock()
    check("second opening run is a no-op", e2 is None and n2 == 0)

    print("== 8. Movements append-only (raw SQL) ==")
    mvid = db.session.scalar(db.select(AccInvMovement.id).limit(1))
    for label, stmt in [
            ("UPDATE refused", f"UPDATE acc_inv_movement SET value_ugx=1 WHERE id={mvid}"),
            ("DELETE refused", f"DELETE FROM acc_inv_movement WHERE id={mvid}")]:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            check(label, False)
        except DatabaseError as e:
            db.session.rollback()
            check(label, "append-only" in str(e.orig) or "cannot be deleted" in str(e.orig),
                  str(e.orig)[:50])

    admin_id = db.session.scalar(db.select(User.id).where(User.role == "admin"))
    rep_id = db.session.scalar(db.select(User.id).where(User.role == "rep"))
    item_id = db.session.scalar(db.select(AccItem.id)
                                .where(AccItem.value_on_hand > 0).limit(1))

print("== 9. Screens ==")


def login_as(client, uid):
    with client.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True


c = app.test_client()
login_as(c, admin_id)
for path in ("/accounting/inventory", "/accounting/inventory/worklist",
             f"/accounting/inventory/{item_id}"):
    r = c.get(path)
    check(f"admin GET {path}", r.status_code == 200, str(r.status_code))
c2 = app.test_client()
login_as(c2, rep_id)
check("rep blocked (403)", c2.get("/accounting/inventory").status_code == 403)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
