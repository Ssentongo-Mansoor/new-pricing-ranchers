"""Purchase posting (accounting Phase 4).

The double entry (each rule commented where it posts):

  Stock line (livestock, carcass, ingredients, packaging):
      DR 1200/1210/1220 Inventory (the item's stage account)    net
      + a weighted-average receive() so the item's cost moves
  Expense line (fuel, repairs, airtime — anything non-stock):
      DR 6xxx chosen expense account                            net
  Input VAT (only when the supplier is VAT-registered):
      DR 1300 VAT Input Receivable
  And the other side, by how the bill is settled:
      CR 2000 Accounts Payable        (on account — supplier balance grows)
      CR 1000/1010/1050 Cash/Bank/MoMo (paid on the spot)

STOCK NEVER HITS EXPENSE. That line of separation is what makes the gross
margin real. The form and this service refuse a stock line without a valued
item, and refuse an expense line pointed at a non-expense account.

Corrections: a posted purchase reverses in full (mirrored journal + the goods
issued back out at exactly the received value). No edits to posted bills.
"""
from datetime import date

from extensions import db
from models import (AccPurchase, AccPurchaseLine, AccSupplier, AccItem,
                    AccInvMovement)
from services import ledger
from services import inventory_costing as inv

PAY_ACCOUNTS = {"cash": "cash", "bank_ugx": "bank_ugx", "momo": "momo"}


class PurchaseError(ValueError):
    pass


def post_purchase(supplier, lines, pay_from="account", purchase_date=None,
                  bill_ref=None, due_date=None, currency="UGX", fx_rate=None,
                  notes=None, user_id=None):
    """Create and post one purchase in a single transaction.

    ``lines``: list of dicts —
      stock:   {"type": "stock", "item_id": .., "qty": .., "unit_cost": <major>,
                "vat": bool}
      expense: {"type": "expense", "account": <id|code>, "description": ..,
                "amount": <major>, "vat": bool}
    Amounts arrive in MAJOR units of ``currency`` and convert here, once.
    """
    if pay_from not in AccPurchase.PAY_FROM:
        raise PurchaseError("Unknown payment source.")
    if currency != "UGX" and not fx_rate:
        raise PurchaseError(f"A {currency} bill needs an exchange rate.")
    if not lines:
        raise PurchaseError("A purchase needs at least one line.")

    def to_ugx(doc_minor):
        if currency == "UGX":
            return int(doc_minor)
        return int(round(doc_minor / 100 * float(fx_rate)))

    prepared, net_total, vat_total = [], 0, 0
    inv_by_stage = {}          # stage account key -> UGX minor
    exp_by_account = {}        # AccAccount -> UGX minor
    receives = []              # (item, qty, value_ugx, line dict)
    vat_ok = bool(supplier.vat_registered)

    for i, ln in enumerate(lines):
        t = ln.get("type")
        if t == "stock":
            item = db.session.get(AccItem, ln.get("item_id") or 0)
            if item is None:
                raise PurchaseError(f"Line {i + 1}: pick a stock item.")
            if not item.stock_unit:
                raise PurchaseError(
                    f"Line {i + 1}: '{item.name}' has no stock unit yet — set "
                    "one on the inventory worklist before buying it.")
            try:
                qty = float(ln.get("qty") or 0)
                unit_cost = float(ln.get("unit_cost") or 0)
            except (TypeError, ValueError):
                raise PurchaseError(f"Line {i + 1}: qty and unit cost must be numbers.")
            if qty <= 0 or unit_cost < 0:
                raise PurchaseError(f"Line {i + 1}: quantity must be positive.")
            net = ledger.to_minor(qty * unit_cost, currency)
            vat = ledger.to_minor(qty * unit_cost * 0.18, currency) \
                if (vat_ok and ln.get("vat")) else 0
            stage_key = AccItem.STAGE_ACCOUNTS[item.stage]
            inv_by_stage[stage_key] = inv_by_stage.get(stage_key, 0) + to_ugx(net)
            receives.append((item, qty, to_ugx(net), ln))
            prepared.append(dict(line_type="stock", item_id=item.id,
                                 description=item.name, qty=qty,
                                 unit_cost_minor=ledger.to_minor(unit_cost, currency),
                                 net_minor=net, vat_minor=vat))
        elif t == "expense":
            acct = ledger._resolve_account(ln.get("account"))
            if acct.type not in ("expense", "cogs"):
                raise PurchaseError(
                    f"Line {i + 1}: '{acct.name}' is not an expense account. "
                    "Stock belongs on a stock line, not in the P&L.")
            try:
                amount = float(ln.get("amount") or 0)
            except (TypeError, ValueError):
                raise PurchaseError(f"Line {i + 1}: amount must be a number.")
            if amount <= 0:
                raise PurchaseError(f"Line {i + 1}: amount must be positive.")
            net = ledger.to_minor(amount, currency)
            vat = ledger.to_minor(amount * 0.18, currency) \
                if (vat_ok and ln.get("vat")) else 0
            exp_by_account[acct.id] = exp_by_account.get(acct.id, 0) + to_ugx(net)
            prepared.append(dict(line_type="expense", expense_account_id=acct.id,
                                 description=(ln.get("description") or acct.name),
                                 net_minor=net, vat_minor=vat))
        else:
            raise PurchaseError(f"Line {i + 1}: type must be stock or expense.")
        net_total += prepared[-1]["net_minor"]
        vat_total += prepared[-1]["vat_minor"]
    gross_total = net_total + vat_total

    # ---- journal ----
    fx = {"orig_currency": currency, "fx_rate": fx_rate} if currency != "UGX" else {}
    jlines = []
    for key, amount in sorted(inv_by_stage.items()):
        jlines.append({"account": key, "debit": amount,
                       "line_memo": "Stock purchase"})
    for acct_id, amount in sorted(exp_by_account.items()):
        jlines.append({"account": acct_id, "debit": amount,
                       "line_memo": "Expense"})
    vat_ugx = to_ugx(vat_total)
    if vat_ugx:
        jlines.append({"account": "vat_input", "debit": vat_ugx,
                       "line_memo": "Input VAT 18%"})
    credit_key = "ap_control" if pay_from == "account" else PAY_ACCOUNTS[pay_from]
    jlines.append({"account": credit_key, "credit": to_ugx(gross_total),
                   "orig_amount_minor": gross_total if currency != "UGX" else None,
                   "line_memo": f"{supplier.name}" + (f" ref {bill_ref}" if bill_ref else ""),
                   **fx})

    entry = ledger.post_entry(
        entry_date=(purchase_date or date.today()),
        memo=f"Purchase — {supplier.name}" + (f" ({bill_ref})" if bill_ref else ""),
        lines=jlines, source_type="purchase", user_id=user_id)

    # ---- purchase record (draft -> numbered -> posted, freeze applies after) --
    p = AccPurchase(supplier_id=supplier.id, bill_ref=bill_ref,
                    purchase_date=(purchase_date or date.today()),
                    due_date=due_date, currency=currency, fx_rate=fx_rate,
                    pay_from=pay_from, net_minor=net_total,
                    vat_minor=vat_total, gross_minor=gross_total,
                    journal_entry_id=entry.id, status="draft",
                    notes=notes, created_by_id=user_id)
    db.session.add(p)
    db.session.flush()
    from services.order_vat import derive_number
    p.purchase_no = derive_number("PUR", p.id)
    for d in prepared:
        db.session.add(AccPurchaseLine(purchase_id=p.id, **d))
    # weighted-average receives, linked to the journal
    for item, qty, value_ugx, _ln in receives:
        inv.receive(item, qty, value_ugx, "purchase",
                    journal_entry_id=entry.id, user_id=user_id,
                    note=f"Purchase {p.purchase_no} — {supplier.name}")
        if item.cost_source == "none":
            item.cost_source = "manual"   # it now carries real purchase value
    db.session.flush()
    p.status = "posted"
    db.session.commit()
    return p


def reverse_purchase(purchase, user_id=None, reason=None):
    """Full reversal: mirrored journal + goods issued back out at exactly the
    received value. The purchase stays on the books, marked reversed."""
    if purchase.status != "posted":
        raise PurchaseError(f"{purchase.purchase_no} is already reversed.")
    entry = purchase.journal_entry
    rev = ledger.reverse_entry(entry, user_id=user_id,
                               memo=f"Reversal of {purchase.purchase_no}"
                                    + (f" — {reason}" if reason else ""))
    # take the received stock back out at the exact received value
    recv = db.session.scalars(
        db.select(AccInvMovement).where(
            AccInvMovement.journal_entry_id == entry.id,
            AccInvMovement.kind == "purchase")).all()
    for mv in recv:
        item = db.session.get(AccItem, mv.item_id)
        if (item.qty_on_hand or 0) + 1e-9 < mv.qty:
            raise PurchaseError(
                f"Cannot reverse {purchase.purchase_no}: {item.name} no longer "
                f"holds the purchased quantity ({item.qty_on_hand:g} < {mv.qty:g}). "
                "Post an adjustment instead.")
        # Identified-layer removal: the goods leave at EXACTLY the value this
        # purchase brought in, matching the mirrored journal to the shilling.
        # Weighted average here would break the subledger-to-GL tie.
        inv.issue(item, mv.qty, "adjustment", value_minor=mv.value_ugx,
                  journal_entry_id=rev.id, user_id=user_id,
                  note=f"Reversal of {purchase.purchase_no}")
    purchase.status = "reversed"
    purchase.reversal_entry_id = rev.id
    db.session.commit()
    return rev
