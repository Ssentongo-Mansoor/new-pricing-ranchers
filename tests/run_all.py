"""Run every acceptance suite against its OWN fresh copy of the database.

QA audit 5 Jul 2026 M5: the suites mutate data (orders, invoices, receipts),
so running them in sequence against one shared copy produces false failures
from cross-test pollution. This runner gives each suite a private copy of
instance/pricing.db in a temp directory, runs it as a subprocess with its own
DATABASE_URL, and reports a summary. A real regression now fails loudly
instead of hiding behind noise.

Usage (from the app root):

    SECRET_KEY=test python3 tests/run_all.py            # all suites
    SECRET_KEY=test python3 tests/run_all.py phase3     # by substring
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
LIVE_DB = os.path.join(ROOT, "instance", "pricing.db")

# Order mirrors the accounting phases; standalone lint/unit tests run first
# (no database needed).
NO_DB_SUITES = ["test_templates_csrf.py", "test_money_roundtrip.py"]
DB_SUITES = [
    "test_phase1_ledger.py", "test_phase2_inventory.py",
    "test_phase3_sales.py", "test_phase4_purchases.py",
    "test_phase5_cash.py", "test_phase6_reports.py",
    "test_phase7_shops.py", "test_cost_floor.py",
    "test_lot_traceability.py",
]


def run(name, env):
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))
    r = subprocess.run([sys.executable, os.path.join(TESTS, name)],
                       cwd=ROOT, env=env)
    return r.returncode == 0


def main():
    pick = sys.argv[1] if len(sys.argv) > 1 else ""
    base_env = dict(os.environ)
    base_env.setdefault("SECRET_KEY", "test-only-secret")
    base_env.setdefault("COOKIE_INSECURE", "1")
    base_env.setdefault("EFRIS_MODE", "simulate")

    if not os.path.exists(LIVE_DB):
        print(f"Live database not found at {LIVE_DB}.")
        return 2

    results = {}
    for name in NO_DB_SUITES:
        if pick and pick not in name:
            continue
        env = dict(base_env)
        env.pop("DATABASE_URL", None)
        results[name] = run(name, env)

    for name in DB_SUITES:
        if pick and pick not in name:
            continue
        with tempfile.TemporaryDirectory(prefix="rf_test_") as tmp:
            db_copy = os.path.join(tmp, "test.db")
            shutil.copy(LIVE_DB, db_copy)
            env = dict(base_env)
            env["DATABASE_URL"] = f"sqlite:///{db_copy}"
            results[name] = run(name, env)

    print("\n" + "=" * 68)
    failed = [n for n, ok in results.items() if not ok]
    for n, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print(f"\n{len(results) - len(failed)}/{len(results)} suites passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
