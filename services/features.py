"""Opt-in feature flags. Each module is off until an admin enables it on the
Admin > Features page. Flags are stored as Settings rows 'feature:<name>'."""
from extensions import db
from models import Setting

FEATURES = [
    ("pipeline", "Sales pipeline & deals",
     "Track deals, their value and stage (Lead → Won/Lost) per customer, with a pipeline board."),
    ("call_lists", "Scheduled call lists",
     "Build call lists for telesales shifts, assign them, and work through them with one-click logging."),
    ("reminders", "Automated reminders",
     "Surface follow-ups due and customers gone quiet on the dashboard and in the daily digest."),
    ("email", "Email from records",
     "Send and log emails to contacts. Needs an SMTP mailbox (set below)."),
    ("sms", "SMS from records",
     "Send and log SMS to contacts. Needs an SMS gateway (set below)."),
    ("telephony", "Click-to-dial & call recording",
     "Attach call recordings to logged calls. Click-to-dial via phone links already works."),
]


def feature_on(name):
    row = db.session.get(Setting, f"feature:{name}")
    return bool(row and row.value == "1")


def all_features():
    return {k: feature_on(k) for k, _l, _d in FEATURES}


def set_feature(name, on):
    key = f"feature:{name}"
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value="1" if on else "0"))
    else:
        row.value = "1" if on else "0"
