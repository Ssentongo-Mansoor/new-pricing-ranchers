"""Outbound email and SMS. Both are best-effort and never raise: if the channel
is not configured (or sending fails) we return (False, reason) so the caller can
still log the interaction. Credentials live in Settings (Admin > Features)."""
import smtplib
import ssl
import urllib.parse
import urllib.request

from services import settings as settings_svc


def email_configured():
    return bool(settings_svc.get("smtp_host"))


def send_email(to_addr, subject, body):
    if not to_addr:
        return (False, "no email address")
    if not email_configured():
        return (False, "SMTP not configured")
    from email.message import EmailMessage
    host = settings_svc.get("smtp_host")
    try:
        port = int(settings_svc.get("smtp_port") or 587)
    except ValueError:
        port = 587
    user = settings_svc.get("smtp_user")
    pw = settings_svc.get("smtp_pass")
    frm = settings_svc.get("smtp_from") or user or "no-reply@localhost"
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body or "")
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            try:
                s.starttls(context=ssl.create_default_context())
            except smtplib.SMTPException:
                pass
            if user:
                s.login(user, pw)
            s.send_message(msg)
        return (True, "sent")
    except Exception as e:   # noqa: BLE001 - report, never crash
        return (False, str(e))


def sms_configured():
    return bool(settings_svc.get("sms_api_key") and settings_svc.get("sms_username"))


def send_sms(to_number, body):
    if not to_number:
        return (False, "no phone number")
    if not sms_configured():
        return (False, "SMS gateway not configured")
    provider = (settings_svc.get("sms_provider") or "africastalking").lower()
    if provider in ("africastalking", "africa's talking", "at"):
        try:
            data = urllib.parse.urlencode({
                "username": settings_svc.get("sms_username"),
                "to": to_number, "message": body or "",
                "from": settings_svc.get("sms_sender") or "",
            }).encode()
            req = urllib.request.Request(
                "https://api.africastalking.com/version1/messaging", data=data,
                headers={"apiKey": settings_svc.get("sms_api_key"),
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            return (True, "sent")
        except Exception as e:   # noqa: BLE001
            return (False, str(e))
    return (False, f"unknown SMS provider '{provider}'")
