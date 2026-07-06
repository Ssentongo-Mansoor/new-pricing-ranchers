"""Daily operations digest: fulfilment queue, pending stock, today's orders,
at-risk customers and reorder-due customers.

Run it on a schedule (cron / Windows Task Scheduler). It always prints the
digest and writes it to instance/digests/. If SMTP settings are provided as
environment variables it also emails the digest.

Email (optional) env vars:
    RF_SMTP_HOST, RF_SMTP_PORT (default 587), RF_SMTP_USER, RF_SMTP_PASS,
    RF_DIGEST_FROM, RF_DIGEST_TO  (comma-separated recipients)

Usage:
    python daily_digest.py
"""
import os
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText

from app import create_app
from extensions import db
from models import SalesOrder, Customer


def build_digest():
    today = date.today()
    cutoff = today - timedelta(days=60)
    orders = db.session.scalars(db.select(SalesOrder)).all()

    def by(status):
        return [o for o in orders if o.status == status]

    # Reuse the canonical confirmed-status set from the reports blueprint so the
    # digest matches every other revenue/at-risk view (M17).
    from blueprints.reports import CONFIRMED as confirmed
    today_orders = [o for o in orders if o.status in confirmed and o.order_date == today]

    # at-risk + reorder
    at_risk, overdue = [], []
    for c in db.session.scalars(db.select(Customer)):
        co = [o for o in orders if o.customer_id == c.id and o.status in confirmed]
        dates = sorted({o.order_date for o in co if o.order_date})
        if not dates:
            continue
        if dates[-1] < cutoff:
            at_risk.append((c.name, dates[-1], (today - dates[-1]).days))
        if len(dates) >= 2:
            intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
            avg = sum(intervals) / len(intervals)
            predicted = dates[-1] + timedelta(days=round(avg))
            if predicted < today:
                overdue.append((c.name, predicted, (today - predicted).days))

    L = []
    L.append(f"RANCHERS FINEST — DAILY DIGEST — {today:%A %d %B %Y}")
    L.append("=" * 52)
    L.append("")
    L.append("FULFILMENT QUEUE")
    L.append(f"  To confirm (customer orders): {len(by('submitted'))}")
    L.append(f"  In fulfilment:                {len(by('in_fulfillment'))}")
    L.append(f"  Dispatched (awaiting deliver):{len(by('dispatched'))}")
    L.append(f"  Pending stock:                {len(by('pending'))}")
    L.append("")
    val = {}
    for o in today_orders:
        val[o.currency] = val.get(o.currency, 0) + float(o.total or 0)
    val_s = ", ".join(f"{c} {v:,.0f}" for c, v in val.items()) or "—"
    L.append(f"ORDERS TODAY: {len(today_orders)}  (value: {val_s})")
    L.append("")
    L.append(f"REORDER OVERDUE ({len(overdue)})")
    for name, pred, d in sorted(overdue, key=lambda x: -x[2])[:15]:
        L.append(f"  {name}: expected ~{pred}, {d} day(s) overdue")
    L.append("")
    L.append(f"CUSTOMERS GONE QUIET ({len(at_risk)})  — no order in 60 days")
    for name, last, d in sorted(at_risk, key=lambda x: -x[2])[:15]:
        L.append(f"  {name}: last order {last} ({d} days ago)")
    L.append("")

    # CRM follow-ups (only if the reminders feature is on)
    try:
        from services.features import feature_on
        if feature_on("reminders"):
            from models import Activity
            acts = db.session.scalars(
                db.select(Activity).where(Activity.next_action_date.isnot(None),
                                          Activity.follow_up_done.is_(False))).all()
            overdue_fu = sorted([a for a in acts if a.next_action_date < today],
                                key=lambda a: a.next_action_date)
            today_fu = [a for a in acts if a.next_action_date == today]
            L.append(f"FOLLOW-UPS OVERDUE ({len(overdue_fu)}) / DUE TODAY ({len(today_fu)})")
            for a in (overdue_fu + today_fu)[:15]:
                who = a.customer.name if a.customer else "—"
                L.append(f"  {who}: {a.next_action} (due {a.next_action_date})")
            L.append("")
    except Exception:
        pass

    L.append("Open the app for full detail and to act on these.")
    return "\n".join(L)


def maybe_email(text):
    host = os.environ.get("RF_SMTP_HOST")
    to = os.environ.get("RF_DIGEST_TO")
    if not host or not to:
        return False
    msg = MIMEText(text)
    msg["Subject"] = f"Ranchers Finest — Daily Digest {date.today():%d %b %Y}"
    msg["From"] = os.environ.get("RF_DIGEST_FROM", "digest@ranchersfinest.local")
    msg["To"] = to
    port = int(os.environ.get("RF_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        user = os.environ.get("RF_SMTP_USER")
        if user:
            s.login(user, os.environ.get("RF_SMTP_PASS", ""))
        s.sendmail(msg["From"], [a.strip() for a in to.split(",")], msg.as_string())
    return True


def main():
    app = create_app()
    with app.app_context():
        text = build_digest()
        print(text)
        from config import INSTANCE_DIR
        folder = os.path.join(INSTANCE_DIR, "digests")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"digest_{date.today():%Y%m%d}.txt")
        with open(path, "w") as f:
            f.write(text)
        print(f"\n[saved to {path}]")
        if maybe_email(text):
            print("[emailed]")


if __name__ == "__main__":
    main()
