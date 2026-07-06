"""Which pricelists a customer may be quoted/ordered from.

A customer's allowed lists = the generic lists explicitly allocated to them in
setup, plus their own customer pricelists. Nothing else is offered. If a customer
has no pricelist linked yet, order/offer/portal screens show none until a manager
links one on the customer record.
"""
from extensions import db
from models import Customer, Pricelist
from services.security import can_see_customer_pricelist


def _live(p):
    return not p.archived and (p.approval_status or "approved") == "approved"


def allowed_pricelists_for(customer):
    own = [p for p in customer.pricelists if _live(p)]
    allocated = [p for p in (customer.allowed_pricelists or []) if _live(p)]
    seen, res = set(), []
    for p in allocated + own:
        if p.id not in seen:
            seen.add(p.id)
            res.append(p)
    return res


def selectable_customers(user):
    custs = [c for c in db.session.scalars(db.select(Customer).order_by(Customer.name))
             if not c.archived]
    if user.can_manage_all or getattr(user, "is_order_manager", False):
        return custs
    assigned = {c.id for c in user.assigned_customers}
    return [c for c in custs if c.id in assigned]


def build_allocation(user, customers):
    """Return (alloc_map, lists): a {customer_id: [pricelist_id,...]} map and the
    union of pricelists to render as <option>s, filtered by what the user can see."""
    alloc, union = {}, {}
    for c in customers:
        allowed = [p for p in allowed_pricelists_for(c)
                   if can_see_customer_pricelist(user, p)]
        alloc[c.id] = [p.id for p in allowed]
        for p in allowed:
            union[p.id] = p
    lists = sorted(union.values(), key=lambda p: (p.is_customer, p.name.lower()))
    return alloc, lists


def is_allowed(user, customer, pricelist):
    if not can_see_customer_pricelist(user, pricelist):
        return False
    return pricelist.id in {p.id for p in allowed_pricelists_for(customer)}
