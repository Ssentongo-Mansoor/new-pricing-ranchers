-- Accounting Phase 4 — purchase integrity + indexes. Idempotent; run after
-- the app boots once on Phase 4 code. Back up first (sqlite3 .backup).

-- A posted purchase's money and identity freeze. status may move
-- posted -> reversed, paid_minor moves with Phase 5 payments.
CREATE TRIGGER IF NOT EXISTS acc_purchase_freeze_money
BEFORE UPDATE ON acc_purchase
WHEN OLD.status IN ('posted','reversed')
     AND (NEW.net_minor    IS NOT OLD.net_minor
          OR NEW.vat_minor    IS NOT OLD.vat_minor
          OR NEW.gross_minor  IS NOT OLD.gross_minor
          OR NEW.currency     IS NOT OLD.currency
          OR NEW.purchase_no  IS NOT OLD.purchase_no
          OR NEW.supplier_id  IS NOT OLD.supplier_id
          OR NEW.journal_entry_id IS NOT OLD.journal_entry_id)
BEGIN
  SELECT RAISE(ABORT, 'purchase: posted amounts are immutable; reverse it instead');
END;

CREATE TRIGGER IF NOT EXISTS acc_purchase_no_delete
BEFORE DELETE ON acc_purchase
BEGIN
  SELECT RAISE(ABORT, 'purchase: posted bills cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS acc_purchase_line_no_update
BEFORE UPDATE ON acc_purchase_line
BEGIN
  SELECT RAISE(ABORT, 'purchase lines are immutable');
END;

CREATE TRIGGER IF NOT EXISTS acc_purchase_line_no_delete
BEFORE DELETE ON acc_purchase_line
WHEN (SELECT status FROM acc_purchase WHERE id = OLD.purchase_id) IN ('posted','reversed')
BEGIN
  SELECT RAISE(ABORT, 'purchase lines cannot be deleted');
END;

CREATE INDEX IF NOT EXISTS ix_acc_purchase_supplier ON acc_purchase (supplier_id);
CREATE INDEX IF NOT EXISTS ix_acc_purchase_date     ON acc_purchase (purchase_date);
CREATE INDEX IF NOT EXISTS ix_acc_purchase_status   ON acc_purchase (status);
