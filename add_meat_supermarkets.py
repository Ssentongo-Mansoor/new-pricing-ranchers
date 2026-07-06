"""Import the Ranchers Finest Meat Supermarkets workbook as a generic pricelist.

Usage:
    python add_meat_supermarkets.py [path-to-workbook]

With no argument it uses seed_data/RANCHERS_FINEST_MEAT_SUPERMARKETS.xlsx.
Idempotent: skips if a list with the same name already exists.
"""
import os
import sys

from app import create_app
from config import Config
from extensions import db
from models import Pricelist
import importer

LIST_NAME = "Ranchers Finest Meat Supermarkets"
DEFAULT_FILE = os.path.join(Config.SEED_DIR, "RANCHERS_FINEST_MEAT_SUPERMARKETS.xlsx")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE
    if not os.path.exists(path):
        print(f"Workbook not found: {path}")
        sys.exit(1)

    app = create_app()
    with app.app_context():
        if db.session.scalar(db.select(Pricelist).filter_by(name=LIST_NAME)):
            print(f"'{LIST_NAME}' already exists — nothing to do.")
            return

        sheet = importer.list_sheets(path)[0]   # 'LUGOGO AND KABALAGALA'
        mapping = {
            "header_row": 4, "data_start": 5,
            "art_col": 0, "barcode_col": 1, "desc_col": 2, "pack_col": 3,
            "box_small_col": None, "box_medium_col": None, "box_large_col": None,
            "tier_cols": [4, 5],
            "tier_labels": ["Price Excl VAT", "Price Incl VAT"],
        }
        meta = {
            "name": LIST_NAME, "channel": "supermarket", "market": "local",
            "currency": "UGX", "vat_applicable": True, "is_customer": False,
            "customer_id": None,
        }
        pl, report = importer.import_mapped_sheet(
            path, sheet, mapping, meta, source_label=os.path.basename(path))
        db.session.commit()
        print(f"Imported '{pl.name}': {report['ok']} rows, {report['failed']} skipped.")


if __name__ == "__main__":
    main()
