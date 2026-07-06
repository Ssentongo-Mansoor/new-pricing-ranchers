"""Own shops (accounting Phase 7): locations, transfers, daily shop sales.

The accounting truth in three sentences:
  * A transfer to an own shop posts NO journal — same entity, same inventory
    account, only the location changes. Quantity moves from the plant pool
    into the shop's location row; the valued item total is untouched.
  * The shop's daily sales summary is where revenue is born:
        DR 1000 Cash             the day's takings
        CR 4000/4010 Revenue     net of VAT per line class
        CR 2100 VAT Output       extracted from the VAT-inclusive till price
        DR 5000/5010 COGS / CR 1210 Inventory   weighted average, per line
  * No EFRIS from here: the shop till's own device already fiscalized every
    receipt. The summary document carries efris_status 'not_required'.
"""
from datetime import date

from extensions import db
from models import (AccLocation, AccItemLocation, AccTransfer, AccTransferLine,
                    AccShopSale, AccShopSaleLine, AccItem)
from services import ledger
from services import inventory_costing as inv

SEED_LOCATIONS = [("Bwaise Plant", "plant", True),
                  ("Factory Shop", "shop", False),
                  ("Lugogo Shop", "shop", False),
                  ("Kabalagala Shop", "shop", False)]


class ShopError(ValueError):
    pass


def ensure_locations():
    """Seed the plant + the three own shops. Idempotent by name."""
    existing = {l.name for l in db.session.scalars(db.select(AccLocation)).all()}
    added = 0
    for name, kind, is_main in SEED_LOCATIONS:
        if name not in existing:
            db.session.add(AccLocation(name=name, kind=kind, is_main=is_main))
            added += 1
    db.session.commit()
    return added


def main_location():
    loc = db.session.scalar(db.select(AccLocation).where(AccLocation.is_main.is_(True)))
    if loc is None:
        ensure_locations()
        loc = db.session.scalar(db.select(AccLocation).where(AccLocation.is_main.is_(True)))
    return loc


def shop_qty(item_id, location_id):
    row = db.session.scalar(db.select(AccItemLocation).where(
        AccItemLocation.item_id == item_id,
        AccItemLocation.location_id == location_id))
    return row.qty if row else 0.0


def _adjust_shop_qty(item_id, location_id, delta):
    row = db.session.scalar(db.select(AccItemLocation).where(
        AccItemLocation.item_id == item_id,
        AccItemLocation.location_id == location_id))
    if row is None:
        row = AccItemLocation(item_id=item_id, location_id=location_id, qty=0)
        db.session.add(row)
    new = (row.qty or 0) + delta
    if new < -1e-9:
        item = db.session.get(AccItem, item_id)
        raise ShopError(f"'{item.name}': only {row.qty:g} at the shop; "
                        f"cannot remove {(-delta):g}.")
    row.qty = max(new, 0.0)
    return row


def post_transfer(from_loc, to_loc, lines, order=None, notes=None,
                  transfer_date=None, user_id=None):
    """Move quantities between locations. ``lines``: [(AccItem, qty), ...].

    Plant->shop: shop row rises (plant pool is implicit: valued total minus
    shop rows). Shop->plant: shop row falls. Shop->shop: both move. NO journal
    entry, by design; the operational plant stock (product.stock_on_hand) is
    handled by the caller (the order flow already deducts on fulfilment)."""
    if from_loc.id == to_loc.id:
        raise ShopError("Pick two different locations.")
    if not lines:
        raise ShopError("A transfer needs at least one line.")
    t = AccTransfer(from_location_id=from_loc.id, to_location_id=to_loc.id,
                    transfer_date=(transfer_date or date.today()),
                    order_id=(order.id if order else None),
                    notes=notes, created_by_id=user_id)
    db.session.add(t)
    db.session.flush()
    from services.order_vat import derive_number
    t.transfer_no = derive_number("TRF", t.id)
    for item, qty in lines:
        qty = float(qty)
        if qty <= 0:
            raise ShopError("Transfer quantities must be positive.")
        db.session.add(AccTransferLine(transfer_id=t.id, item_id=item.id, qty=qty))
        if from_loc.kind == "shop":
            _adjust_shop_qty(item.id, from_loc.id, -qty)
        if to_loc.kind == "shop":
            _adjust_shop_qty(item.id, to_loc.id, +qty)
    db.session.commit()
    return t


def transfer_for_order(order, user_id=None):
    """Fulfilment of an internal-shop order: the delivered lines become a
    plant->shop transfer. Idempotent per order."""
    existing = db.session.scalar(db.select(AccTransfer).where(
        AccTransfer.order_id == order.id))
    if existing:
        return existing
    to_loc = db.session.get(AccLocation,
                            order.customer.internal_location_id)
    if to_loc is None:
        raise ShopError(f"Customer {order.customer.name} is flagged internal "
                        "but has no location.")
    items_by_pid = {i.product_id: i for i in db.session.scalars(
        db.select(AccItem).where(AccItem.product_id.in_(
            [l.product_id for l in order.lines if l.product_id]))).all()}
    lines = []
    for l in order.lines:
        qty = l.delivered_qty or 0
        item = items_by_pid.get(l.product_id)
        if qty > 0 and item:
            lines.append((item, qty))
    if not lines:
        raise ShopError(f"Order {order.number} has no delivered lines to transfer.")
    return post_transfer(main_location(), to_loc, lines, order=order,
                         notes=f"Stock to {to_loc.name} per {order.number}",
                         user_id=user_id)


# ---------------------------------------------------------------------------
# Daily shop sales
# ---------------------------------------------------------------------------
def post_shop_sale(location, lines, sale_date=None, notes=None, user_id=None,
                   vat_rate=18.0):
    """One day's takings for one shop. ``lines``: [(AccItem, qty, gross_major)]
    — gross is the VAT-INCLUSIVE till money for that line.

    VAT is extracted only for vatable items (processed); fresh lines carry
    none. COGS issues at the entity weighted average and the shop's location
    quantity drops — selling stock the shop never received is refused."""
    if location.kind != "shop":
        raise ShopError("Daily sales are recorded per shop.")
    if not lines:
        raise ShopError("Record at least one line.")

    prepared, gross_t, net_t, vat_t = [], 0, 0, 0
    rev_by_key, cogs_by_key, cogs_movs = {}, {}, []
    for i, (item, qty, gross_major) in enumerate(lines):
        qty = float(qty)
        gross = ledger.to_minor(gross_major, "UGX")
        if qty <= 0 or gross < 0:
            raise ShopError(f"Line {i + 1}: quantity and takings must be positive.")
        if shop_qty(item.id, location.id) + 1e-9 < qty:
            raise ShopError(
                f"Line {i + 1}: '{item.name}' — the shop holds "
                f"{shop_qty(item.id, location.id):g}, cannot sell {qty:g}. "
                "Transfer stock first or correct the quantity.")
        vatable = bool(item.product and item.product.vat_applicable)
        if vatable:
            net = int(round(gross / (1 + vat_rate / 100.0)))
            vat = gross - net
            rev_key = "rev_processed"
            cogs_key = "cogs_processed"
        else:
            net, vat = gross, 0
            rev_key = "rev_fresh"
            cogs_key = "cogs_fresh"
        gross_t += gross
        net_t += net
        vat_t += vat
        rev_by_key[rev_key] = rev_by_key.get(rev_key, 0) + net
        prepared.append((item, qty, gross, net, vat, cogs_key))

    # ---- journal ----
    jlines = [{"account": "cash", "debit": gross_t,
               "line_memo": f"{location.name} takings"}]
    for key, amount in sorted(rev_by_key.items()):
        if amount:
            jlines.append({"account": key, "credit": amount,
                           "line_memo": "Retail revenue (net)"})
    if vat_t:
        jlines.append({"account": "vat_output", "credit": vat_t,
                       "line_memo": f"VAT extracted from till prices"})

    # COGS issues (valued, entity weighted average) — done before posting so
    # the amounts are known; movements link to the entry afterwards.
    for item, qty, gross, net, vat, cogs_key in prepared:
        mv = inv.issue(item, qty, "sale", user_id=user_id,
                       note=f"Shop sale — {location.name}")
        cogs_movs.append(mv)
        cogs_by_key[cogs_key] = cogs_by_key.get(cogs_key, 0) + (-mv.value_ugx)
    cogs_t = sum(cogs_by_key.values())
    for key, amount in sorted(cogs_by_key.items()):
        if amount:
            jlines.append({"account": key, "debit": amount, "line_memo": "COGS"})
    if cogs_t:
        jlines.append({"account": "inv_finished", "credit": cogs_t,
                       "line_memo": "Inventory out at weighted average"})

    entry = ledger.post_entry(
        entry_date=(sale_date or date.today()),
        memo=f"Shop sales — {location.name}",
        lines=jlines, source_type="invoice", channel="shop",
        user_id=user_id)
    for mv in cogs_movs:
        mv.journal_entry_id = entry.id

    sale = AccShopSale(location_id=location.id,
                       sale_date=(sale_date or date.today()),
                       gross_minor=gross_t, net_minor=net_t, vat_minor=vat_t,
                       cogs_minor=cogs_t, journal_entry_id=entry.id,
                       notes=notes, created_by_id=user_id, status="draft")
    db.session.add(sale)
    db.session.flush()
    from services.order_vat import derive_number
    sale.sale_no = derive_number("SHS", sale.id)
    for item, qty, gross, net, vat, _ck in prepared:
        db.session.add(AccShopSaleLine(sale_id=sale.id, item_id=item.id,
                                       qty=qty, gross_minor=gross,
                                       net_minor=net, vat_minor=vat))
        _adjust_shop_qty(item.id, location.id, -qty)
    db.session.flush()
    sale.status = "posted"
    db.session.commit()
    return sale


def location_stock(location_id):
    """[(item, qty)] held at a shop, nonzero only."""
    rows = db.session.scalars(
        db.select(AccItemLocation).where(
            AccItemLocation.location_id == location_id,
            AccItemLocation.qty > 0)).all()
    return sorted(((r.item, r.qty) for r in rows), key=lambda t: t[0].name)
