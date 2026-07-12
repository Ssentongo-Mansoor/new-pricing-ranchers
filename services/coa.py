"""Chart of accounts for Ranchers Finest Uganda Ltd — meat processing and
distribution, VAT-registered, EFRIS-fiscalized. Seeded idempotently at startup:
missing codes are inserted, existing accounts are never edited or removed, so
an admin can rename or add accounts without the seed fighting back.

Layout (gaps left for growth):
  1xxx assets, 2xxx liabilities, 3xxx equity, 4xxx income, 5xxx cost of goods
  sold, 6xxx operating expenses, 7xxx finance/FX.

``system_key`` marks the control accounts the posting rules look up by key
(never by hard-coded id). Header accounts (is_postable=False) group the chart
for reports and cannot carry journal lines.
"""
from extensions import db
from models import AccAccount

# (code, name, type, is_postable, system_key, parent_code)
CHART = [
    # ---- Assets -----------------------------------------------------------
    ("1000", "Cash on Hand",                        "asset",     True,  "cash",          None),
    ("1010", "Bank — UGX",                          "asset",     True,  "bank_ugx",      None),
    ("1020", "Bank — USD",                          "asset",     True,  "bank_usd",      None),
    ("1050", "Mobile Money",                        "asset",     True,  "momo",          None),
    ("1100", "Accounts Receivable",                 "asset",     True,  "ar_control",    None),
    ("1200", "Inventory — Raw Materials",           "asset",     True,  "inv_raw",       None),
    ("1210", "Inventory — Finished Goods",          "asset",     True,  "inv_finished",  None),
    ("1220", "Inventory — Packaging & Consumables", "asset",     True,  "inv_packaging", None),
    ("1300", "VAT Input Receivable",                "asset",     True,  "vat_input",     None),
    ("1320", "Withholding Tax Receivable",          "asset",     True,  "wht_recv",      None),
    ("1400", "Prepayments",                         "asset",     True,  None,            None),
    ("1500", "Property, Plant & Equipment",         "asset",     True,  None,            None),
    ("1510", "Accumulated Depreciation",            "asset",     True,  None,            None),
    # ---- Liabilities ------------------------------------------------------
    ("2000", "Accounts Payable",                    "liability", True,  "ap_control",    None),
    ("2100", "VAT Output Payable",                  "liability", True,  "vat_output",    None),
    ("2150", "Withholding Tax Payable",             "liability", True,  None,            None),
    ("2200", "PAYE / NSSF / LST Payable",           "liability", True,  None,            None),
    ("2300", "Loans Payable",                       "liability", True,  None,            None),
    ("2400", "Accrued Expenses",                    "liability", True,  None,            None),
    # ---- Equity -----------------------------------------------------------
    ("3000", "Share Capital",                       "equity",    True,  None,            None),
    ("3100", "Retained Earnings",                   "equity",    True,  None,            None),
    ("3900", "Opening Balance Equity",              "equity",    True,  "opening",       None),
    # ---- Income -----------------------------------------------------------
    ("4000", "Sales — Fresh Cuts (Local)",          "income",    True,  "rev_fresh",     None),
    ("4010", "Sales — Processed (Local)",           "income",    True,  "rev_processed", None),
    ("4020", "Sales — Byproducts & Other",          "income",    True,  None,            None),
    ("4100", "Sales — Export (zero-rated)",         "income",    True,  "rev_export",    None),
    ("4900", "Discounts & Rebates Allowed",         "income",    True,  None,            None),
    # ---- Cost of goods sold ------------------------------------------------
    ("5000", "COGS — Fresh Cuts",                   "cogs",      True,  "cogs_fresh",    None),
    ("5010", "COGS — Processed",                    "cogs",      True,  "cogs_processed", None),
    ("5100", "Production Yield Loss (abnormal)",    "cogs",      True,  None,            None),
    ("5200", "Inventory Shrinkage & Adjustments",   "cogs",      True,  None,            None),
    ("5300", "Inventory Write-downs (wastage)",     "cogs",      True,  None,            None),
    # ---- Operating expenses -------------------------------------------------
    ("6000", "Salaries & Wages",                    "expense",   True,  None,            None),
    ("6100", "Rent & Utilities",                    "expense",   True,  None,            None),
    ("6200", "Delivery & Vehicle Costs",            "expense",   True,  None,            None),
    ("6300", "Repairs & Maintenance",               "expense",   True,  None,            None),
    ("6400", "Marketing & Selling",                 "expense",   True,  None,            None),
    ("6500", "Professional Fees",                   "expense",   True,  None,            None),
    ("6600", "Bank & Mobile Money Charges",         "expense",   True,  None,            None),
    ("6700", "Licences, URA Fees & Levies",         "expense",   True,  None,            None),
    ("6900", "Other Operating Expenses",            "expense",   True,  None,            None),
    # ---- Finance ------------------------------------------------------------
    ("7000", "FX Gain / Loss",                      "expense",   True,  "fx",            None),
    ("7100", "Interest Expense",                    "expense",   True,  None,            None),
]


def seed_chart():
    """Insert any CHART account whose code is missing. Never updates or deletes.
    Safe to run on every startup (create_all pattern)."""
    existing = set(db.session.scalars(db.select(AccAccount.code)).all())
    added = 0
    by_code = {}
    for code, name, typ, postable, syskey, parent_code in CHART:
        if code in existing:
            continue
        normalized_name = name.replace("—", "-").replace("–", "-")
        acct = AccAccount(code=code, name=normalized_name, type=typ,
                          is_postable=postable, system_key=syskey)
        if parent_code and parent_code in by_code:
            acct.parent = by_code[parent_code]
        db.session.add(acct)
        by_code[code] = acct
        added += 1
    if added:
        db.session.commit()
    return added


def account_for(system_key):
    """The control account for a posting rule, by stable key. Raises if absent
    so a broken chart is loud, not silently mis-posted."""
    acct = db.session.scalar(
        db.select(AccAccount).where(AccAccount.system_key == system_key))
    if acct is None:
        raise LookupError(f"No account with system_key '{system_key}' — "
                          "chart of accounts incomplete.")
    return acct
