"""Money conversion tests — QA audit 5 Jul 2026 H2.

Proves:
  1. to_minor/from_minor round-trip exactly for UGX (0 dp) and USD (2 dp).
  2. HALF_UP at the boundary: UGX 12,345.5 -> 12,346; USD 10.505 -> 1051.
  3. line_money computes in Decimal and rounds per line, so a sum of many
     lines carries zero float drift (the classic 0.1+0.2 failure).
  4. line_money and to_minor agree: converting a line total to minor units
     equals the integer arithmetic done directly in minor units.

Run (no database needed):

    python3 tests/test_money_roundtrip.py
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-only-secret")

from services.ledger import to_minor, from_minor      # noqa: E402
from models import line_money, vat_money              # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# 1. Round-trips ------------------------------------------------------------
check("UGX round-trip 12345", from_minor(to_minor(12345, "UGX"), "UGX") == Decimal("12345"))
check("USD round-trip 10.50", from_minor(to_minor("10.50", "USD"), "USD") == Decimal("10.50"))
check("None -> 0", to_minor(None) == 0)
check("Decimal input accepted", to_minor(Decimal("99.99"), "USD") == 9999)
check("float input accepted", to_minor(0.1 + 0.2, "USD") == 30)

# 2. HALF_UP boundaries -----------------------------------------------------
check("UGX 12345.5 -> 12346", to_minor("12345.5", "UGX") == 12346)
check("UGX 12345.4 -> 12345", to_minor("12345.4", "UGX") == 12345)
check("USD 10.505 -> 1051 cents", to_minor("10.505", "USD") == 1051)
check("USD 10.504 -> 1050 cents", to_minor("10.504", "USD") == 1050)
check("negative HALF_UP: -10.505 -> -1051", to_minor("-10.505", "USD") == -1051)

# 3. Per-line Decimal rounding kills float drift ----------------------------
# 0.1 + 0.2 != 0.3 in float; in the line boundary it must be exact.
check("line_money exact: 3 x 0.1", line_money("0.1", 3, 0) == Decimal("0.30"))
check("line_money discount: 10000 x 1 @ 7.5%",
      line_money("10000", 1, 7.5) == Decimal("9250.00"))
check("line_money rounds HALF_UP: 1 x 0.005", line_money("0.005", 1, 0) == Decimal("0.01"))
total = sum(line_money("0.1", 1, 0) for _ in range(1000))
check("1000 x 0.1 sums to exactly 100", total == Decimal("100.00"))

# 4. line_money and to_minor agree ------------------------------------------
lt = line_money("5416.6667", 3, 0)             # 16250.0001 -> 16250.00
check("line then minor (UGX)", to_minor(lt, "UGX") == 16250)
check("VAT 18% on 16250 UGX", vat_money(lt, 18) == Decimal("2925.00"))
check("VAT rounds HALF_UP", vat_money("0.03", 18) == Decimal("0.01"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
