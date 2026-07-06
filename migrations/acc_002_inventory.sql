-- Accounting module Phase 2 — valued inventory movement integrity + indexes.
-- Run once against instance/pricing.db AFTER the app boots on Phase 2 code
-- (boot creates acc_item / acc_inv_movement via create_all). Back up first:
--   sqlite3 instance/pricing.db ".backup instance-backup.db"
--   sqlite3 instance/pricing.db < migrations/acc_002_inventory.sql
-- Idempotent.

-- Valued movements are append-only, like journal lines: corrections are new
-- adjustment movements, never edits, so the movement history always replays
-- to the current balance.
CREATE TRIGGER IF NOT EXISTS acc_inv_mv_no_update
BEFORE UPDATE ON acc_inv_movement
BEGIN
  SELECT RAISE(ABORT, 'inventory: valued movements are append-only; post an adjustment');
END;

CREATE TRIGGER IF NOT EXISTS acc_inv_mv_no_delete
BEFORE DELETE ON acc_inv_movement
BEGIN
  SELECT RAISE(ABORT, 'inventory: valued movements cannot be deleted');
END;

CREATE INDEX IF NOT EXISTS ix_acc_inv_movement_item_id ON acc_inv_movement (item_id);
CREATE INDEX IF NOT EXISTS ix_acc_inv_movement_journal ON acc_inv_movement (journal_entry_id);
CREATE INDEX IF NOT EXISTS ix_acc_inv_movement_kind    ON acc_inv_movement (kind);
CREATE INDEX IF NOT EXISTS ix_acc_item_stage           ON acc_item (stage);
