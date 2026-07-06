-- Accounting Phase 5 — cash & bank integrity + indexes. Idempotent.
-- NOTE: the acc_invoice.paid_minor column is added by the app's runtime
-- migration (app.py _run_migrations) on first boot, BEFORE this script runs.

CREATE TRIGGER IF NOT EXISTS acc_receipt_freeze
BEFORE UPDATE ON acc_receipt
WHEN OLD.status IN ('posted','reversed')
     AND (NEW.amount_minor IS NOT OLD.amount_minor
          OR NEW.wht_minor    IS NOT OLD.wht_minor
          OR NEW.currency     IS NOT OLD.currency
          OR NEW.receipt_no   IS NOT OLD.receipt_no
          OR NEW.customer_id  IS NOT OLD.customer_id
          OR NEW.journal_entry_id IS NOT OLD.journal_entry_id)
BEGIN
  SELECT RAISE(ABORT, 'receipt: posted amounts are immutable; reverse it instead');
END;

CREATE TRIGGER IF NOT EXISTS acc_receipt_no_delete
BEFORE DELETE ON acc_receipt
BEGIN
  SELECT RAISE(ABORT, 'receipt: money documents cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS acc_payment_freeze
BEFORE UPDATE ON acc_supplier_payment
WHEN OLD.status IN ('posted','reversed')
     AND (NEW.amount_minor IS NOT OLD.amount_minor
          OR NEW.payment_no  IS NOT OLD.payment_no
          OR NEW.supplier_id IS NOT OLD.supplier_id
          OR NEW.journal_entry_id IS NOT OLD.journal_entry_id)
BEGIN
  SELECT RAISE(ABORT, 'payment: posted amounts are immutable; reverse it instead');
END;

CREATE TRIGGER IF NOT EXISTS acc_payment_no_delete
BEFORE DELETE ON acc_supplier_payment
BEGIN
  SELECT RAISE(ABORT, 'payment: money documents cannot be deleted');
END;

CREATE INDEX IF NOT EXISTS ix_acc_receipt_customer ON acc_receipt (customer_id);
CREATE INDEX IF NOT EXISTS ix_acc_receipt_date     ON acc_receipt (receipt_date);
CREATE INDEX IF NOT EXISTS ix_acc_payment_supplier ON acc_supplier_payment (supplier_id);
CREATE INDEX IF NOT EXISTS ix_acc_recon_account    ON acc_reconciliation (account_id, status);
