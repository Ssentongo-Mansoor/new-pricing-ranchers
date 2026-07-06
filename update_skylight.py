"""Apply a Skylight revised-prices workbook (UGX/kg) to the Skylight UGX list.

Usage:
    python update_skylight.py [path-to-workbook]

With no argument it uses the latest revision found in ../seed_data
(file name containing "Skylight" and "Revised").
"""
import glob
import os
import sys

from app import create_app
from config import Config
import importer


def _default_file():
    pattern = os.path.join(Config.SEED_DIR, "*.xlsx")
    candidates = [f for f in glob.glob(pattern)
                  if "skylight" in os.path.basename(f).lower()
                  and "revis" in os.path.basename(f).lower()]
    return max(candidates, key=os.path.getmtime) if candidates else None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else _default_file()
    if not path or not os.path.exists(path):
        print("No revision workbook found. Pass the file path as an argument, or "
              "place it in seed_data with 'Skylight' and 'Revised' in the name.")
        sys.exit(1)
    app = create_app()
    with app.app_context():
        print(f"Applying Skylight revision from: {os.path.basename(path)}")
        importer.apply_skylight_revision(path)


if __name__ == "__main__":
    main()
