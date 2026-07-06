"""One-off importer for the April/2026 customer pricelists.

Creates five customers (with categories) and imports each uploaded workbook as
that customer's own pricelist, linked to them. Idempotent: re-running skips any
customer pricelist whose name already exists.

Run:  python import_customer_lists.py
The source workbooks live in ./imports/.
"""
import os
from datetime import date

from app import create_app
from extensions import db
from models import Customer, CustomerCategory, Pricelist, ImportReport
from blueprints.customers import ensure_customer_categories
from importer import _load_rows, _import_sheet_core
from services import settings as settings_svc

IMPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imports")

# (key, label, column) tier sets
HORECA_TIERS = [("excl_vat", "Price Excl VAT", 10), ("incl_vat", "Price Incl VAT", 11)]
SOKONI_TIERS = [("excl_vat", "Price Excl VAT", 4), ("incl_vat", "Price Incl VAT", 5)]
CARREFOUR_TIERS = [("excl_vat", "Price Excl VAT", 4), ("incl_vat", "Price Incl VAT", 5), ("rrp", "RRP", 6)]
CAPITAL_TIERS = [("excl_vat", "Price Excl VAT", 4), ("incl_vat", "Price Incl VAT", 5), ("rrp", "RRP", 7)]

JOBS = [
    dict(file="ATS PRICELIST UPDATED APRIL,2026.xlsx", sheet="ATS",
         customer="ATS", category="Caterer", channel="horeca",
         plname="ATS — Pricelist (April 2026)",
         map=dict(data_start=12, art_col=0, desc_col=1, pack_col=2, units_col=2,
                  box_small_col=4, box_medium_col=6, box_large_col=8,
                  tiers=HORECA_TIERS, art_required=True)),
    dict(file="GCC PRICELIST UPDATED (4).xlsx", sheet="GCC PRICE LIST",
         customer="GCC", category="Caterer", channel="horeca",
         plname="GCC — Pricelist (May 2026)",
         map=dict(data_start=11, art_col=0, desc_col=1, pack_col=2, units_col=2,
                  box_small_col=4, box_medium_col=6, box_large_col=8,
                  tiers=HORECA_TIERS, art_required=True)),
    dict(file="SOKONI PRICELIST 1ST APRIL,2026 (2).xlsx", sheet="SOKONI PRICELIST",
         customer="Sokoni", category="Hotel", channel="horeca",
         plname="Sokoni — Pricelist (April 2026)",
         map=dict(data_start=9, art_col=0, desc_col=1, pack_col=2, units_col=2,
                  tiers=SOKONI_TIERS, art_required=True)),
    dict(file="CARREFOUR PRICELIST UPDATED - APRIL ,2026 (3).xlsx", sheet="Local ",
         customer="Carrefour", category="Supermarket", channel="supermarket",
         plname="Carrefour — Pricelist (April 2026)",
         map=dict(data_start=6, art_col=0, barcode_col=1, desc_col=2, pack_col=3,
                  units_col=3, box_small_col=7, tiers=CARREFOUR_TIERS, art_required=True)),
    dict(file="CAPITAL SHOPPERS PRICELIST UPDATED MARCH 2025 (2).xlsx", sheet="CAPITAL SHOPPERS",
         customer="Capital Shoppers", category="Supermarket", channel="supermarket",
         plname="Capital Shoppers — Pricelist (March 2026)",
         map=dict(data_start=6, art_col=0, barcode_col=1, desc_col=2, pack_col=3,
                  units_col=3, tiers=CAPITAL_TIERS, art_required=True)),
]


def _cat_id(name):
    c = db.session.scalar(db.select(CustomerCategory).filter_by(name=name))
    return c.id if c else None


def _get_customer(name, category):
    c = db.session.scalar(db.select(Customer).filter_by(name=name))
    if c is None:
        c = Customer(name=name)
        db.session.add(c)
    c.segment = "customer"
    c.market = "local"
    c.default_currency = "UGX"
    c.category_id = _cat_id(category)
    db.session.flush()
    return c


def run(verbose=True):
    ensure_customer_categories()
    vat_rate = settings_svc.get_float("vat_rate", 18.0)
    summary = []
    for job in JOBS:
        path = os.path.join(IMPORTS_DIR, job["file"])
        if not os.path.exists(path):
            summary.append((job["customer"], "FILE NOT FOUND", 0, 0))
            continue

        customer = _get_customer(job["customer"], job["category"])

        if db.session.scalar(db.select(Pricelist).filter_by(name=job["plname"])):
            summary.append((job["customer"], "skip (exists)", 0, 0))
            continue

        pl = Pricelist(
            name=job["plname"], channel=job["channel"], market="local",
            currency="UGX", vat_applicable=True, vat_rate=vat_rate,
            effective_date=date.today(), is_customer=True,
            customer_id=customer.id,
            source_file=f"{job['file']} :: {job['sheet']}",
            notes=f"Imported {date.today()} from {job['file']}.")
        db.session.add(pl)
        db.session.flush()

        rows = _load_rows(path, job["sheet"])
        report = {"ok": 0, "failed": 0, "lines": []}
        _import_sheet_core(rows, job["map"], pl, report,
                           synth_prefix=job["customer"][:3].upper())
        db.session.add(ImportReport(
            source=pl.source_file, rows_ok=report["ok"],
            rows_failed=report["failed"], detail="\n".join(report["lines"])))
        db.session.commit()
        summary.append((job["customer"], job["category"], report["ok"], report["failed"]))
        if verbose:
            print(f"  + {job['customer']} [{job['category']}] -> {job['plname']}: "
                  f"{report['ok']} ok, {report['failed']} skipped")
    return summary


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        print("Importing customer pricelists...")
        rows = run()
        print("\nDone:")
        for name, cat, ok, failed in rows:
            print(f"   {name}: {cat} — {ok} lines ({failed} skipped)")
