-- Accounting Phase 7 — own shops: transfer/shop-sale integrity + indexes.
-- Idempotent. The customer.internal_location_id column comes from the app's
-- runtime migration on first boot.

CREATE TRIGGER IF NOT EXISTS acc_transfer_no_delete
BEFORE DELETE ON acc_transfer
BEGIN
  SELECT RAISE(ABORT, 'transfer: stock documents cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS acc_transfer_line_no_update
BEFORE UPDATE ON acc_transfer_line
BEGIN
  SELECT RAISE(ABORT, 'transfer lines are immutable');
END;

CREATE TRIGGER IF NOT EXISTS acc_shop_sale_freeze
BEFORE UPDATE ON acc_shop_sale
WHEN OLD.status IN ('posted','reversed')
     AND (NEW.gross_minor IS NOT OLD.gross_minor
          OR NEW.net_minor  IS NOT OLD.net_minor
          OR NEW.vat_minor  IS NOT OLD.vat_minor
          OR NEW.cogs_minor IS NOT OLD.cogs_minor
          OR NEW.sale_no    IS NOT OLD.sale_no
          OR NEW.location_id IS NOT OLD.location_id
          OR NEW.journal_entry_id IS NOT OLD.journal_entry_id)
BEGIN
  SELECT RAISE(ABORT, 'shop sale: posted amounts are immutable; reverse via journal');
END;

CREATE TRIGGER IF NOT EXISTS acc_shop_sale_no_delete
BEFORE DELETE ON acc_shop_sale
BEGIN
  SELECT RAISE(ABORT, 'shop sale: posted documents cannot be deleted');
END;

CREATE INDEX IF NOT EXISTS ix_acc_transfer_date    ON acc_transfer (transfer_date);
CREATE INDEX IF NOT EXISTS ix_acc_shop_sale_date   ON acc_shop_sale (sale_date);
CREATE INDEX IF NOT EXISTS ix_acc_item_loc_lookup  ON acc_item_location (location_id, item_id);
