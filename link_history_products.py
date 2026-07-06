"""Link sales_history.product (free text) to catalogue products.

1) Token-match each distinct historical product name to a catalogue Product.
2) Apply manual overrides from imports/product_map.csv if present
   (columns: product_name, article_no  — article_no '' or 'OMIT' leaves it off-catalogue).
3) Write sales_history.product_id, and export still-unmatched names with revenue
   to imports/unmatched_products.csv for manual mapping.

Run: DATABASE_URL=sqlite:////tmp/work.db python link_history_products.py
"""
import os
import re
import csv
import collections

from app import create_app
from extensions import db
from models import Product, SalesHistory

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.path.join(HERE, "imports", "product_map.csv")
OUT_FILE = os.path.join(HERE, "imports", "unmatched_products.csv")

STOP = set("THE A OF KG KGS GR GRS G ML L PC PCS X CATERING RR SALES WHOLE WITH "
           "FOR PER AND".split())


def toks(s):
    s = (s or "").upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\[.*?\]", " ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    return set(w for w in s.split() if len(w) > 1 and w not in STOP)


def run():
    prods = [(p.id, p.article_no, p.description) for p in db.session.scalars(db.select(Product))]
    ptoks = [(pid, art, toks(desc)) for pid, art, desc in prods]
    by_art = {str(art).strip().upper(): pid for pid, art, _ in prods}

    def best(htk):
        bp, bs = None, 0
        for pid, _art, pt in ptoks:
            if not pt:
                continue
            inter = len(htk & pt)
            if not inter:
                continue
            j = inter / len(htk | pt)
            score = j + (0.5 if (pt <= htk or htk <= pt) else 0)
            if score > bs:
                bs, bp = score, pid
        return bp, bs

    # manual overrides
    manual = {}
    if os.path.exists(MAP_FILE):
        with open(MAP_FILE, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                nm = (row.get("product_name") or "").strip()
                art = (row.get("article_no") or "").strip().upper()
                if not nm:
                    continue
                if art in ("", "OMIT", "NONE"):
                    manual[nm] = None
                elif art in by_art:
                    manual[nm] = by_art[art]
                else:
                    manual[nm] = "BADART:" + art

    # revenue per distinct name
    rev_by = collections.defaultdict(float)
    for s in db.session.scalars(db.select(SalesHistory)):
        rev_by[s.product] += float(s.revenue or 0)

    name_to_pid = {}
    bad = []
    for name in rev_by:
        if name in manual:
            v = manual[name]
            if isinstance(v, str) and v.startswith("BADART:"):
                bad.append((name, v))
                name_to_pid[name] = None
            else:
                name_to_pid[name] = v
            continue
        pid, score = best(toks(name))
        name_to_pid[name] = pid if (pid and score >= 0.5) else None

    # write back
    n_linked = 0
    for s in db.session.scalars(db.select(SalesHistory)):
        s.product_id = name_to_pid.get(s.product)
        if s.product_id:
            n_linked += 1
    db.session.commit()

    total = sum(rev_by.values())
    linked_rev = sum(r for nm, r in rev_by.items() if name_to_pid.get(nm))
    unmatched = sorted(((r, nm) for nm, r in rev_by.items() if not name_to_pid.get(nm)),
                       reverse=True)
    print(f"Distinct names: {len(rev_by)} | linked: {sum(1 for v in name_to_pid.values() if v)} "
          f"({linked_rev/total*100:.1f}% of revenue)")
    print(f"Unmatched names: {len(unmatched)} ({(total-linked_rev)/total*100:.1f}% of revenue)")
    if bad:
        print("WARNING bad article_no in map:", bad[:10])

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["product_name", "revenue_ugx", "article_no"])  # fill article_no, or OMIT
        for r, nm in unmatched:
            w.writerow([nm, f"{r:.0f}", ""])
    print("Wrote unmatched list ->", OUT_FILE)


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        run()
