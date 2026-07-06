"""Weighted-average inventory valuation (accounting Phase 2).

The maths, in one place
-----------------------
Each AccItem carries qty_on_hand (float, in its stock_unit) and value_on_hand
(INTEGER UGX shillings). The average cost is value/qty — computed, never
stored, never rounded away.

  receive(qty, value):  qty_on_hand += qty;  value_on_hand += value
  issue(qty):           value_out = round(qty * value_on_hand / qty_on_hand)
                        qty_on_hand -= qty;  value_on_hand -= value_out
                        (issuing the LAST unit takes the LAST shilling, so an
                         emptied item is worth exactly 0 — no residue)

Because every movement moves an integer number of shillings, the sum of item
values always equals the GL inventory accounts to the shilling.

Cost sources for the opening load
---------------------------------
  * recipe — processed products link to a recipe (services/recipes.py) whose
    last_cost_per_kg comes from the costing app. Pack/pc items convert
    through pack_weight_kg.
  * manual — a unit cost entered on the worklist (fresh cuts, store items).
  * none   — no valuation; the item sits on the worklist and is refused by
    the opening load rather than booked at a fictional cost.
"""
import re
from datetime import date

from extensions import db
from models import (AccItem, AccInvMovement, Product, Store, StoreItem,
                    ProdRecipe)
from services import ledger
from services import recipes as rec

# Store-item names that mean packaging rather than raw material.
_PACKAGING_PAT = re.compile(
    r"box|bag|casing|label|vacuum|packag|sleeve|wrap|tray|carton|film|tape|"
    r"sticker|pouch|net\b", re.I)

# "1kg" / "500G" / "5 x 200 Gr" / "2.5 KG" -> kilograms
_PACK_ONE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(kg|g|gr|grams?)\b", re.I)
_PACK_MULTI = re.compile(
    r"^\s*(\d+)\s*[xX*]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|gr|grams?)\b", re.I)


class CostingError(ValueError):
    """A valuation rule was violated (no cost, unit unresolved, overdraw)."""


def parse_pack_weight_kg(text):
    """Best-effort parse of a pack-size string to kilograms. None if unclear."""
    if not text:
        return None
    t = str(text).strip()
    m = _PACK_MULTI.match(t)
    if m:
        n, w, unit = int(m.group(1)), float(m.group(2).replace(",", ".")), m.group(3)
        kg = w if unit.lower().startswith("k") else w / 1000.0
        return round(n * kg, 4)
    m = _PACK_ONE.match(t)
    if m:
        w, unit = float(m.group(1).replace(",", ".")), m.group(2)
        return round(w if unit.lower().startswith("k") else w / 1000.0, 4)
    return None


# ---------------------------------------------------------------------------
# Item registry
# ---------------------------------------------------------------------------
def ensure_items():
    """Create missing AccItem rows: one per active catalogue product (finished)
    and one per StoreItem (raw or packaging by name). Idempotent; never edits
    an existing item, so worklist decisions stick. Returns counts."""
    created = {"finished": 0, "raw": 0, "packaging": 0}

    have = {i.product_id for i in db.session.scalars(
        db.select(AccItem).where(AccItem.product_id.isnot(None))).all()}
    for p in db.session.scalars(
            db.select(Product).where(Product.status == "active")).all():
        if p.id in have:
            continue
        unit = (p.unit_of_measure or "").strip().lower() or None
        if unit in ("pcs", "piece", "pieces"):
            unit = "pc"
        db.session.add(AccItem(
            product_id=p.id, name=p.description, stage="finished",
            stock_unit=unit,
            pack_weight_kg=(1.0 if unit == "kg"
                            else parse_pack_weight_kg(p.pack_size)),
        ))
        created["finished"] += 1

    have_si = {i.store_item_id for i in db.session.scalars(
        db.select(AccItem).where(AccItem.store_item_id.isnot(None))).all()}
    for si in db.session.scalars(db.select(StoreItem)).all():
        if si.id in have_si:
            continue
        blob = f"{si.name} {si.category or ''}"
        stage = "packaging" if _PACKAGING_PAT.search(blob) else "raw"
        unit = (si.uom or "").strip().lower() or None
        db.session.add(AccItem(store_item_id=si.id, name=si.name, stage=stage,
                               stock_unit=unit))
        created[stage] += 1

    db.session.commit()
    return created


def recipe_cost_per_kg(item):
    """last_cost_per_kg of the CONFIRMED recipe for this item's product."""
    if not item.product_id:
        return None
    m = rec.confirmed_map().get(item.product_id)
    if not m:
        return None
    r = db.session.get(ProdRecipe, m.recipe_id)
    if r and r.last_cost_per_kg and r.last_cost_per_kg > 0:
        return float(r.last_cost_per_kg)
    return None


def unit_cost_minor(item):
    """Integer UGX cost per stock_unit, or None with the blocking reason.

    Returns (cost_minor, source, reason)."""
    if item.manual_cost_minor and item.manual_cost_minor > 0:
        return item.manual_cost_minor, "manual", None
    per_kg = recipe_cost_per_kg(item)
    if per_kg:
        if item.stock_unit == "kg":
            return ledger.to_minor(per_kg), "recipe", None
        # pack/pc items need a pack weight to travel from cost-per-kg
        pw = item.pack_weight_kg
        if not pw and item.product_id:
            m = rec.confirmed_map().get(item.product_id)
            if m:
                pw = rec.pack_weight_kg(m.recipe_id)
        if pw and pw > 0:
            return ledger.to_minor(per_kg * pw), "recipe", None
        return None, None, "recipe cost is per kg but the pack weight is unknown"
    return None, None, "no cost source (no confirmed recipe, no manual cost)"


# ---------------------------------------------------------------------------
# The two primitive movements
# ---------------------------------------------------------------------------
def receive(item, qty, value_minor, kind, journal_entry_id=None, note=None,
            user_id=None, **refs):
    """Add qty at a known integer value. Moves the weighted average."""
    if qty <= 0:
        raise CostingError("Receipt quantity must be positive.")
    if value_minor < 0:
        raise CostingError("Receipt value cannot be negative.")
    item.qty_on_hand = (item.qty_on_hand or 0) + qty
    item.value_on_hand = (item.value_on_hand or 0) + int(value_minor)
    mv = AccInvMovement(item_id=item.id, kind=kind, qty=qty,
                        value_ugx=int(value_minor),
                        qty_after=item.qty_on_hand,
                        value_after=item.value_on_hand,
                        journal_entry_id=journal_entry_id, note=note,
                        user_id=user_id, **refs)
    db.session.add(mv)
    return mv


def issue(item, qty, kind, journal_entry_id=None, note=None, user_id=None,
          value_minor=None, **refs):
    """Take qty out. Default: at the current weighted average — the COGS rule.

    ``value_minor`` overrides the average for IDENTIFIED-LAYER removals only:
    a purchase reversal must take out exactly the value that purchase brought
    in, or the subledger drifts from the GL (which reverses the bill at its
    own amount). Never use the override for sales.

    The last unit takes the last shilling: when the issue empties the item,
    the full remaining value goes with it, so no 1-shilling residue survives
    rounding."""
    if qty <= 0:
        raise CostingError("Issue quantity must be positive.")
    on_hand = item.qty_on_hand or 0
    if qty > on_hand + 1e-9:
        raise CostingError(
            f"Cannot issue {qty:g} {item.stock_unit or 'units'} of "
            f"'{item.name}': only {on_hand:g} on hand in the valued ledger.")
    if value_minor is not None:
        value_out = int(value_minor)
        if value_out < 0 or value_out > (item.value_on_hand or 0):
            raise CostingError(
                f"Identified-layer removal of {value_out:,} exceeds the "
                f"value on hand for '{item.name}'.")
        if abs(qty - on_hand) <= 1e-9 and value_out != (item.value_on_hand or 0):
            raise CostingError(
                f"Removing the last {qty:g} of '{item.name}' must take the "
                f"full remaining value; post an adjustment instead.")
    elif abs(qty - on_hand) <= 1e-9:
        value_out = item.value_on_hand or 0          # emptied: take it all
    else:
        value_out = round(qty * (item.value_on_hand or 0) / on_hand)
    item.qty_on_hand = on_hand - qty
    item.value_on_hand = (item.value_on_hand or 0) - value_out
    mv = AccInvMovement(item_id=item.id, kind=kind, qty=-qty,
                        value_ugx=-value_out,
                        qty_after=item.qty_on_hand,
                        value_after=item.value_on_hand,
                        journal_entry_id=journal_entry_id, note=note,
                        user_id=user_id, **refs)
    db.session.add(mv)
    return mv


# ---------------------------------------------------------------------------
# Opening stock
# ---------------------------------------------------------------------------
def opening_candidates():
    """Finished items with product stock on hand and no opening movement yet.

    Returns (ready, blocked): ready = [(item, qty, cost_minor, source)],
    blocked = [(item, qty, reason)]."""
    opened = {mv.item_id for mv in db.session.scalars(
        db.select(AccInvMovement).where(AccInvMovement.kind == "opening")).all()}
    ready, blocked = [], []
    items = db.session.scalars(
        db.select(AccItem).where(AccItem.product_id.isnot(None),
                                 AccItem.active.is_(True))).all()
    for item in items:
        if item.id in opened:
            continue
        qty = (item.product.stock_on_hand or 0) if item.product else 0
        if qty <= 0:
            continue
        cost, source, reason = unit_cost_minor(item)
        if cost is None:
            blocked.append((item, qty, reason))
        else:
            ready.append((item, qty, cost, source))
    return ready, blocked


def load_opening_stock(user_id=None, on=None):
    """Value the ready opening candidates and post ONE balanced journal:
    DR inventory stage accounts / CR 3900 Opening Balance Equity.

    Idempotent: items that already have an opening movement are skipped by
    opening_candidates(). Items without a cost are left on the worklist —
    never valued at a guess. Returns (entry, n_items, total_minor)."""
    ready, _blocked = opening_candidates()
    if not ready:
        return None, 0, 0

    stage_totals = {}
    for item, qty, cost, source in ready:
        stage_totals[item.stage] = stage_totals.get(item.stage, 0) + \
            int(round(qty * cost))

    total = sum(stage_totals.values())
    lines = [{"account": AccItem.STAGE_ACCOUNTS[stage], "debit": amount,
              "line_memo": f"Opening stock — {AccItem.STAGES[stage]}"}
             for stage, amount in sorted(stage_totals.items()) if amount > 0]
    lines.append({"account": "opening", "credit": total,
                  "line_memo": "Opening stock at weighted-average cost"})

    entry = ledger.post_entry(
        entry_date=(on or date.today()),
        memo="Opening inventory valuation",
        lines=lines, source_type="opening", user_id=user_id)

    for item, qty, cost, source in ready:
        item.cost_source = source
        receive(item, qty, int(round(qty * cost)), "opening",
                journal_entry_id=entry.id, user_id=user_id,
                note=f"Opening at {cost:,} UGX/{item.stock_unit or 'unit'} ({source})")
    db.session.commit()
    return entry, len(ready), total


# ---------------------------------------------------------------------------
# Valuation report + GL tie-out
# ---------------------------------------------------------------------------
def valuation_summary():
    """Totals per stage from the item subledger, next to the GL balance of the
    matching control account. They must be EQUAL — the report shows both so a
    drift (impossible by construction, but trust needs proof) is loud."""
    balances = ledger.account_balances()
    out = []
    for stage, syskey in AccItem.STAGE_ACCOUNTS.items():
        sub = db.session.scalar(
            db.select(db.func.coalesce(db.func.sum(AccItem.value_on_hand), 0))
            .where(AccItem.stage == stage)) or 0
        from services.coa import account_for
        try:
            acct = account_for(syskey)
            gl = balances.get(acct.id, 0)
        except LookupError:
            gl = 0
        out.append({"stage": stage, "label": AccItem.STAGES[stage],
                    "subledger": sub, "gl": gl, "tied": sub == gl})
    return out
