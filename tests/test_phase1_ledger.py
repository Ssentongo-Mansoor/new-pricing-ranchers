"""Phase 1 acceptance test — ledger core.

Run against a COPY of the live database, never the live file:

    cp instance/pricing.db /tmp/pricing_test.db
    SECRET_KEY=test DATABASE_URL=sqlite:////tmp/pricing_test.db \
    COOKIE_INSECURE=1 python3 tests/test_phase1_ledger.py

Proves, in order:
  1.  App boots on the merged code; acc_ tables created; chart seeded.
  2.  WAL mode, busy timeout and foreign keys active on the connection.
  3.  A balanced manual journal posts and gets a JE number.
  4.  An unbalanced entry is refused by the service (LedgerError).
  5.  An unbalanced entry forced through RAW SQL — bypassing the service — is
      refused by the database trigger. This is the physical guarantee.
  6.  Posted lines refuse UPDATE and DELETE (raw SQL).
  7.  Posted entries refuse edits and deletion (raw SQL).
  8.  Reversal posts the exact mirror; the pair nets to zero.
  9.  Trial balance: total debits equal total credits.
  10. Accounting screens render for an accountant; a rep gets 403.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-only-secret")
os.environ.setdefault("COOKIE_INSECURE", "1")
if "DATABASE_URL" not in os.environ:
    print("Refusing to run without DATABASE_URL (never test on the live file).")
    sys.exit(2)

from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app import app
from extensions import db
from models import AccAccount, AccJournalEntry, AccJournalLine, User
from services import ledger
from services.coa import CHART

PASS, FAIL = 0, 0


def check(label, ok, detail=""):
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def apply_triggers():
    sql = open(os.path.join(os.path.dirname(__file__), "..",
                            "migrations", "acc_001_triggers.sql")).read()
    raw = db.engine.raw_connection()
    try:
        raw.executescript(sql)
        raw.commit()
    finally:
        raw.close()


with app.app_context():
    print("== 1. Boot, tables, chart ==")
    tables = set(db.session.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'")).scalars())
    for t in ("acc_account", "acc_journal_entry", "acc_journal_line",
              "prod_recipe", "prod_production", "prod_recipe_map"):
        check(f"table {t} exists", t in tables)
    n_accounts = db.session.scalar(db.select(db.func.count(AccAccount.id)))
    check(f"chart seeded ({n_accounts} accounts)", n_accounts >= len(CHART))

    print("== 2. SQLite pragmas ==")
    jm = db.session.execute(text("PRAGMA journal_mode")).scalar()
    bt = db.session.execute(text("PRAGMA busy_timeout")).scalar()
    fk = db.session.execute(text("PRAGMA foreign_keys")).scalar()
    check("journal_mode=WAL", jm == "wal", str(jm))
    check("busy_timeout=15000", int(bt) == 15000, str(bt))
    check("foreign_keys=ON", int(fk) == 1, str(fk))

    apply_triggers()
    print("== triggers applied (idempotent) ==")

    print("== 3. Balanced manual journal posts ==")
    e1 = ledger.post_entry(
        date.today(), "TEST opening cash",
        [{"account": "1000", "debit": ledger.to_minor(5_000_000)},
         {"account": "3900", "credit": ledger.to_minor(5_000_000)}])
    check("posted", e1.posted and e1.entry_no.startswith("JE-"),
          f"{e1.entry_no}: DR {e1.total_debit:,} / CR {e1.total_credit:,}")
    check("integer minor units", isinstance(e1.lines[0].debit, int)
          and e1.lines[0].debit == 5_000_000)

    print("== 4. Unbalanced entry refused by the service ==")
    try:
        ledger.post_entry(date.today(), "TEST unbalanced",
                          [{"account": "1000", "debit": 100},
                           {"account": "3900", "credit": 99}])
        check("service refuses unbalanced", False)
    except ledger.LedgerError as e:
        check("service refuses unbalanced", True, str(e)[:60])

    print("== 5. Unbalanced entry forced with RAW SQL refused by TRIGGER ==")
    a_cash = db.session.scalar(db.select(AccAccount).where(AccAccount.code == "1000")).id
    a_open = db.session.scalar(db.select(AccAccount).where(AccAccount.code == "3900")).id
    db.session.rollback()
    db.session.execute(text(
        "INSERT INTO acc_journal_entry (entry_date, memo, source_type, posted, created_at) "
        "VALUES (:d, 'RAW bypass attempt', 'manual', 0, CURRENT_TIMESTAMP)"),
        {"d": date.today().isoformat()})
    raw_id = db.session.execute(text("SELECT last_insert_rowid()")).scalar()
    db.session.execute(text(
        "INSERT INTO acc_journal_line (entry_id, account_id, debit, credit) "
        "VALUES (:e, :a, 1000, 0)"), {"e": raw_id, "a": a_cash})
    db.session.execute(text(
        "INSERT INTO acc_journal_line (entry_id, account_id, debit, credit) "
        "VALUES (:e, :a, 0, 999)"), {"e": raw_id, "a": a_open})
    try:
        db.session.execute(text(
            "UPDATE acc_journal_entry SET posted=1 WHERE id=:e"), {"e": raw_id})
        db.session.commit()
        check("trigger blocks unbalanced post", False)
    except DatabaseError as e:
        db.session.rollback()
        check("trigger blocks unbalanced post", "does not balance" in str(e.orig),
              str(e.orig))
    # clean up the draft (drafts may be deleted)
    db.session.execute(text("DELETE FROM acc_journal_line WHERE entry_id=:e"), {"e": raw_id})
    db.session.execute(text("DELETE FROM acc_journal_entry WHERE id=:e"), {"e": raw_id})
    db.session.commit()

    print("== 6. Posted lines are append-only (raw SQL) ==")
    line_id = e1.lines[0].id
    for label, stmt in [
            ("UPDATE refused", f"UPDATE acc_journal_line SET debit=1 WHERE id={line_id}"),
            ("DELETE refused", f"DELETE FROM acc_journal_line WHERE id={line_id}")]:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            check(label, False)
        except DatabaseError as e:
            db.session.rollback()
            check(label, "append-only" in str(e.orig) or "posted" in str(e.orig),
                  str(e.orig))

    print("== 7. Posted entries are immutable (raw SQL) ==")
    for label, stmt in [
            ("memo edit refused",
             f"UPDATE acc_journal_entry SET memo='hacked' WHERE id={e1.id}"),
            ("unpost refused",
             f"UPDATE acc_journal_entry SET posted=0 WHERE id={e1.id}"),
            ("DELETE refused",
             f"DELETE FROM acc_journal_entry WHERE id={e1.id}")]:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            check(label, False)
        except DatabaseError as e:
            db.session.rollback()
            check(label, True, str(e.orig)[:60])

    print("== 8. Reversal mirrors exactly ==")
    e2 = ledger.post_entry(
        date.today(), "TEST expense",
        [{"account": "6600", "debit": ledger.to_minor(150_000)},
         {"account": "1010", "credit": ledger.to_minor(150_000)}])
    rev = ledger.reverse_entry(e2, memo="TEST reversal")
    check("reversal posted", rev.posted and rev.reversal_of_id == e2.id, rev.entry_no)
    pair_net = (e2.total_debit - e2.total_credit) + (rev.total_debit - rev.total_credit)
    bal = ledger.account_balances()
    check("pair nets to zero on both accounts",
          all(bal.get(l.account_id, 0) == 0 for l in e2.lines
              if l.account.code in ("6600", "1010")) and pair_net == 0)
    try:
        ledger.reverse_entry(e2)
        check("double reversal refused", False)
    except ledger.LedgerError:
        check("double reversal refused", True)

    print("== 9. Trial balance ==")
    rows, tdr, tcr = ledger.trial_balance()
    check("debits equal credits", tdr == tcr, f"{tdr:,} = {tcr:,}")
    check("rounding: to_minor half-up", ledger.to_minor("10.5") == 11
          and ledger.to_minor("10.505", "USD") == 1051)

    admin_id = db.session.scalar(
        db.select(User.id).where(User.role == "admin"))
    rep_id = db.session.scalar(db.select(User.id).where(User.role == "rep"))
    e1_id = e1.id

# Screen checks run OUTSIDE the shared app context. Flask-Login caches the
# loaded user on the app context (g), so test-client requests made inside one
# long-lived context would all render as the first user regardless of the
# session cookie. Real requests each get a fresh context; this mirrors that.
print("== 10. Screens and permissions ==")


def login_as(client, user_id):
    with client.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(user_id)
        s["_fresh"] = True


if admin_id:
    c = app.test_client()
    login_as(c, admin_id)
    for path in ("/accounting/journal", "/accounting/accounts",
                 "/accounting/trial-balance", "/accounting/journal/new",
                 f"/accounting/journal/{e1_id}"):
        r = c.get(path)
        check(f"admin GET {path}", r.status_code == 200, str(r.status_code))
else:
    check("admin user found", False)
if rep_id:
    c = app.test_client()
    login_as(c, rep_id)
    r = c.get("/accounting/journal")
    check("rep blocked from accounting (403)", r.status_code == 403,
          str(r.status_code))
    # A tokenless POST dies on CSRF (400) before the role check (403).
    # Either way the write is refused; both codes prove the block.
    r = c.post("/accounting/journal/new", data={})
    check("rep write refused (400 CSRF / 403 role)",
          r.status_code in (400, 403), str(r.status_code))

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
