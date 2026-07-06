"""EFRIS client and retry queue (accounting Phase 3).

Hard rules:
  * Fiscalization NEVER blocks the ledger. The invoice and journal are already
    committed when this module runs. Failure here marks the invoice pending
    and leaves a queue row; a cron drains the queue with exponential backoff.
  * The full URA response is stored on the invoice for audit.
  * Credentials come from the environment only — never from the repo.

Modes (EFRIS_MODE environment variable):
  off        — default. No URA configured yet: invoices stay 'pending', the
               queue holds them, and the printed document says
               'fiscalization pending'. Nothing is lost; when credentials
               arrive and the mode flips, the queue drains the backlog.
  simulate   — test double: returns a fake FDN/verification/QR instantly.
  simulate_fail — test double: every call fails (proves the retry path).
  sandbox / production — the real URA integration. The wire adapter
               (_call_ura) is completed against the current URA integration
               guide once the TIN, device number and keys exist; the guide's
               interface codes and crypto get verified then, not guessed now.

Environment for the real modes:
  EFRIS_BASE_URL, EFRIS_TIN, EFRIS_DEVICE_NO, EFRIS_DEVICE_KEY_PATH,
  EFRIS_AES_KEY
"""
import json
import os
from datetime import datetime, timedelta

from extensions import db
from models import AccEfrisQueue, AccInvoice

MAX_ATTEMPTS = 12                 # then status 'failed'; manual retry allowed
BACKOFF_BASE_MIN = 5              # 5, 10, 20, 40, ... capped at 6 h
BACKOFF_CAP_MIN = 360


class EfrisError(Exception):
    pass


def mode():
    return (os.environ.get("EFRIS_MODE") or "off").strip().lower()


def _fake_result(invoice):
    n = invoice.id
    return {
        "fdn": f"324{n:012d}",
        "verification_code": f"{n:04d}-{(n * 7) % 10000:04d}-{(n * 13) % 10000:04d}",
        "qr": f"EFRIS-SIM|{invoice.invoice_no}|{invoice.gross_minor}",
        "invoice_id": f"SIM{n:010d}",
        "raw": {"simulated": True, "invoice_no": invoice.invoice_no,
                "gross_minor": invoice.gross_minor,
                "at": datetime.utcnow().isoformat() + "Z"},
    }


def _call_ura(invoice, action):
    """The single wire boundary to URA. Completed against the current EFRIS
    integration guide (interface codes, RSA/AES envelope) once credentials
    exist; kept deliberately unimplemented until then so nothing pretends to
    be fiscal."""
    m = mode()
    if m == "simulate":
        return _fake_result(invoice)
    if m == "simulate_fail":
        raise EfrisError("simulated URA outage")
    if m in ("sandbox", "production"):
        raise EfrisError(
            "EFRIS wire adapter not yet implemented — completes when URA "
            "credentials and the current integration guide are in hand.")
    raise EfrisError("EFRIS_MODE=off — URA not configured; invoice stays queued.")


def try_fiscalize(invoice, action="fiscalize"):
    """One attempt, right now. Safe to call after commit; commits its own
    result. Returns True on success. Never raises to the caller — the sale
    must already be safe in the books."""
    try:
        result = _call_ura(invoice, action)
    except Exception as e:
        _record_failure(invoice, str(e))
        return False
    invoice.efris_status = "fiscalized"
    invoice.efris_fdn = result["fdn"]
    invoice.efris_verification_code = result["verification_code"]
    invoice.efris_qr = result["qr"]
    invoice.efris_invoice_id = result.get("invoice_id")
    invoice.fiscalized_at = datetime.utcnow()
    invoice.efris_response = json.dumps(result.get("raw", result))
    q = _queue_row(invoice)
    if q:
        q.status = "done"
        q.done_at = datetime.utcnow()
    db.session.commit()
    return True


def _queue_row(invoice):
    return db.session.scalar(
        db.select(AccEfrisQueue).where(
            AccEfrisQueue.invoice_id == invoice.id,
            AccEfrisQueue.status.in_(("queued", "in_flight"))))


def _record_failure(invoice, error):
    q = _queue_row(invoice)
    if q is None:
        q = AccEfrisQueue(invoice_id=invoice.id,
                          action=("credit_note" if invoice.is_credit_note
                                  else "fiscalize"))
        db.session.add(q)
    q.attempts = (q.attempts or 0) + 1
    q.last_error = str(error)[:512]
    if q.attempts >= MAX_ATTEMPTS:
        q.status = "failed"
        invoice.efris_status = "failed"
    else:
        q.status = "queued"
        delay = min(BACKOFF_BASE_MIN * (2 ** (q.attempts - 1)), BACKOFF_CAP_MIN)
        q.next_attempt_at = datetime.utcnow() + timedelta(minutes=delay)
    db.session.commit()


def process_queue(limit=25):
    """Drain due queue rows. Called by cron (efris_retry.py) and by the
    'Retry now' button. Returns (succeeded, failed)."""
    due = db.session.scalars(
        db.select(AccEfrisQueue)
        .where(AccEfrisQueue.status == "queued",
               AccEfrisQueue.next_attempt_at <= datetime.utcnow())
        .order_by(AccEfrisQueue.next_attempt_at).limit(limit)).all()
    ok = bad = 0
    for q in due:
        q.status = "in_flight"
        db.session.commit()
        if try_fiscalize(q.invoice, q.action):
            ok += 1
        else:
            bad += 1
    return ok, bad


def pending_count():
    return db.session.scalar(
        db.select(db.func.count(AccEfrisQueue.id))
        .where(AccEfrisQueue.status.in_(("queued", "in_flight")))) or 0
