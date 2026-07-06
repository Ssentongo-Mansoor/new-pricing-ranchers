-- Accounting Phase 3 — invoice integrity + indexes. Idempotent; run after the
-- app boots once on Phase 3 code (tables come from create_all). Back up first.

-- Refinement of the Phase 2 movement trigger: a valued movement stays
-- append-only in every ECONOMIC column, but linking it to its journal entry
-- (journal_entry_id NULL -> value, once) is allowed — the COGS issues are
-- written before the journal id exists inside one posting transaction.
DROP TRIGGER IF EXISTS acc_inv_mv_no_update;
CREATE TRIGGER acc_inv_mv_no_update
BEFORE UPDATE ON acc_inv_movement
WHEN NOT (OLD.journal_entry_id IS NULL
          AND NEW.journal_entry_id IS NOT NULL
          AND NEW.item_id      IS OLD.item_id
          AND NEW.kind         IS OLD.kind
          AND NEW.qty          IS OLD.qty
          AND NEW.value_ugx    IS OLD.value_ugx
          AND NEW.qty_after    IS OLD.qty_after
          AND NEW.value_after  IS OLD.value_after
          AND NEW.order_id     IS OLD.order_id
          AND NEW.created_at   IS OLD.created_at)
BEGIN
  SELECT RAISE(ABORT, 'inventory: valued movements are append-only; post an adjustment');
END;

-- A posted invoice's money and identity freeze. EFRIS result fields stay
-- writable (URA answers after the fact), and status may move posted->credited.
CREATE TRIGGER IF NOT EXISTS acc_invoice_freeze_money
BEFORE UPDATE ON acc_invoice
WHEN OLD.status IN ('posted','credited')
     AND (NEW.net_minor   IS NOT OLD.net_minor
          OR NEW.vat_minor   IS NOT OLD.vat_minor
          OR NEW.gross_minor IS NOT OLD.gross_minor
          OR NEW.currency    IS NOT OLD.currency
          OR NEW.invoice_no  IS NOT OLD.invoice_no
          OR NEW.order_id    IS NOT OLD.order_id
          OR NEW.journal_entry_id IS NOT OLD.journal_entry_id)
BEGIN
  SELECT RAISE(ABORT, 'invoice: posted amounts are immutable; use a credit note');
END;

CREATE TRIGGER IF NOT EXISTS acc_invoice_no_delete
BEFORE DELETE ON acc_invoice
BEGIN
  SELECT RAISE(ABORT, 'invoice: fiscal documents cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS acc_invoice_line_no_update
BEFORE UPDATE ON acc_invoice_line
BEGIN
  SELECT RAISE(ABORT, 'invoice lines are immutable');
END;

CREATE TRIGGER IF NOT EXISTS acc_invoice_line_no_delete
BEFORE DELETE ON acc_invoice_line
BEGIN
  SELECT RAISE(ABORT, 'invoice lines cannot be deleted');
END;

CREATE INDEX IF NOT EXISTS ix_acc_invoice_order   ON acc_invoice (order_id);
CREATE INDEX IF NOT EXISTS ix_acc_invoice_efris   ON acc_invoice (efris_status);
CREATE INDEX IF NOT EXISTS ix_acc_invoice_date    ON acc_invoice (invoice_date);
CREATE INDEX IF NOT EXISTS ix_acc_efris_queue_due ON acc_efris_queue (status, next_attempt_at);
