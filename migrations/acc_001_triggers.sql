-- Accounting module Phase 1 — ledger integrity triggers and indexes.
--
-- Run ONCE against instance/pricing.db AFTER the app has booted at least once
-- on the new code (the boot creates the acc_ tables via create_all).
-- ALWAYS back up the database file first and rehearse on a copy:
--
--   cp instance/pricing.db instance/pricing.db.backup_YYYYMMDD
--   sqlite3 instance/pricing.db < migrations/acc_001_triggers.sql
--
-- Idempotent: every statement uses IF NOT EXISTS.
--
-- Why triggers: application checks give friendly errors, but only the database
-- itself can guarantee that no code path — including a future bug, a raw SQL
-- session, or a second process — ever books an unbalanced or edited entry.
-- SQLite has no deferred constraints, so balance is enforced at the moment an
-- entry flips from draft (posted=0) to posted (posted=1). Drafts are invisible
-- to every report.

-- 1. Journal lines are append-only. -----------------------------------------
CREATE TRIGGER IF NOT EXISTS acc_line_no_update
BEFORE UPDATE ON acc_journal_line
BEGIN
  SELECT RAISE(ABORT, 'ledger: journal lines are append-only; post a reversing entry');
END;

CREATE TRIGGER IF NOT EXISTS acc_line_no_delete
BEFORE DELETE ON acc_journal_line
WHEN (SELECT posted FROM acc_journal_entry WHERE id = OLD.entry_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'ledger: lines of a posted entry cannot be deleted');
END;

-- 2. No line may be added to an already-posted entry. ------------------------
CREATE TRIGGER IF NOT EXISTS acc_line_no_insert_posted
BEFORE INSERT ON acc_journal_line
WHEN (SELECT posted FROM acc_journal_entry WHERE id = NEW.entry_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'ledger: entry is already posted');
END;

-- 3. Lines must be sane at insert: non-negative, exactly one side non-zero. --
CREATE TRIGGER IF NOT EXISTS acc_line_shape
BEFORE INSERT ON acc_journal_line
WHEN NEW.debit < 0 OR NEW.credit < 0
     OR (NEW.debit > 0 AND NEW.credit > 0)
     OR (NEW.debit = 0 AND NEW.credit = 0)
BEGIN
  SELECT RAISE(ABORT, 'ledger: a line carries exactly one positive side');
END;

-- 4. THE BALANCE GUARANTEE: posting only succeeds when debits = credits ------
--    and the entry has at least two lines.
CREATE TRIGGER IF NOT EXISTS acc_entry_post_check
BEFORE UPDATE OF posted ON acc_journal_entry
WHEN NEW.posted = 1 AND OLD.posted = 0
BEGIN
  SELECT RAISE(ABORT, 'ledger: entry does not balance (debits != credits)')
  WHERE (SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0)
           FROM acc_journal_line WHERE entry_id = NEW.id) != 0;
  SELECT RAISE(ABORT, 'ledger: an entry needs at least two lines')
  WHERE (SELECT COUNT(*) FROM acc_journal_line WHERE entry_id = NEW.id) < 2;
END;

-- 5. Posted entries are immutable: no unposting, no edits, no deletion. ------
CREATE TRIGGER IF NOT EXISTS acc_entry_no_edit_posted
BEFORE UPDATE ON acc_journal_entry
WHEN OLD.posted = 1
     AND (NEW.posted != 1
          OR NEW.entry_date  IS NOT OLD.entry_date
          OR NEW.memo        IS NOT OLD.memo
          OR NEW.source_type IS NOT OLD.source_type
          OR NEW.source_id   IS NOT OLD.source_id
          OR NEW.entry_no    IS NOT OLD.entry_no)
BEGIN
  SELECT RAISE(ABORT, 'ledger: posted entries are immutable; post a reversing entry');
END;

CREATE TRIGGER IF NOT EXISTS acc_entry_no_delete
BEFORE DELETE ON acc_journal_entry
WHEN OLD.posted = 1
BEGIN
  SELECT RAISE(ABORT, 'ledger: posted entries cannot be deleted');
END;

-- 6. Indexes for the ledger's hot paths. -------------------------------------
CREATE INDEX IF NOT EXISTS ix_acc_journal_line_entry_id   ON acc_journal_line (entry_id);
CREATE INDEX IF NOT EXISTS ix_acc_journal_line_account_id ON acc_journal_line (account_id);
CREATE INDEX IF NOT EXISTS ix_acc_journal_entry_date      ON acc_journal_entry (entry_date);
CREATE INDEX IF NOT EXISTS ix_acc_journal_entry_source    ON acc_journal_entry (source_type, source_id);
