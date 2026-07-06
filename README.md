# Ranchers Finest — Pricing & Offers

A server-hosted, multi-user web application that is the single source of truth for
all Ranchers Finest U Ltd pricing. It replaces the scattered Excel pricelists with
one database that staff consult in a browser, edit in place with full history,
export to branded Excel/PDF, and use to build customer offers.

Built with Flask, SQLAlchemy and Bootstrap 5. SQLite by default (one file, easy to
back up) with a clean upgrade path to PostgreSQL. All assets — Bootstrap, fonts,
icons and the logo — are bundled locally, so the app renders with no internet.

---

## What it does

- **Consult** every generic pricelist in a fast, searchable, filterable table with
  all price tiers, box-packing quantities, currency-aware formatting, and validity
  badges. Read-only users see no edit controls.
- **Edit** prices inline (click, type, Enter). Every change logs old value, new
  value, who and when. A **bulk tool** applies a % or fixed change to a category, a
  tier or a whole list, with a preview before commit.
- **Customer pricelists** live under their own tab, created by copying a generic
  base list and editing freely. Visibility follows rep assignment.
- **Offers / quotes** are built from a customer and a chosen list+tier, with line
  discounts, live totals, automatic VAT (18% local) or zero-rating (export),
  PDF/Excel export, and one-click promotion to a customer pricelist.
- **Currency**: UGX is the base of value. USD/TZS figures derive from UGX at the
  managed exchange rate. Rates carry effective/expiry dates, are versioned and
  logged, show an impact preview before commit, and are **stamped onto issued
  offers** so a later rate change never restates them. **Fixed-price overrides**
  pin a standalone foreign price on any line.
- **Access control** is enforced server-side on every read and write: roles
  (admin / manager / rep), an independent edit/pricing right, and rep→customer
  assignment for customer-list visibility. Full **audit log** with CSV export.
- **Import**: the six source workbooks seed on first run; the Zanzibar and Skylight
  sheets land as customer pricelists. New workbooks upload through the UI with a
  column-mapping step — no code changes. Every unparsed row is logged to an import
  report.

---

## Requirements

- Python 3.10+
- The packages in `requirements.txt`

---

## Setup (first run)

```bash
cd ranchers_pricing
python3 -m venv .venv && source .venv/bin/activate     # optional but recommended
pip install -r requirements.txt

# 1) Create the database schema and the first admin account.
python setup.py
#    Prompts for an admin username and password. (Set RF_ADMIN_USER / RF_ADMIN_PASS
#    in the environment to run non-interactively.)
#    Also seeds the category tree and a starter UGX->USD rate (update it in-app).

# 2) Import the six source workbooks from ../seed_data
python importer.py
#    Or point at another folder:  python importer.py /path/to/workbooks
```

The seed import is idempotent — re-running skips lists already present by name.

### Seed data location

`importer.py` reads the six `.xlsx` files from the `seed_data/` folder next to the
app (override with the `SEED_DIR` environment variable or a path argument). The
folder already contains the supplied workbooks.

---

## Run

### Development

```bash
flask --app app run            # http://127.0.0.1:5000
```

### Production (gunicorn behind Nginx)

```bash
export SECRET_KEY="a-long-random-secret"
gunicorn --workers 3 --bind 127.0.0.1:8000 app:app
```

Nginx reverse-proxy snippet:

```nginx
server {
    listen 80;
    server_name pricing.ranchersfinest.local;

    client_max_body_size 32m;        # allows pricelist uploads

    location /static/ {
        alias /opt/ranchers_pricing/static/;
        expires 30d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Run gunicorn under a process manager (systemd, supervisor) so it restarts on boot.

---

## Configuration

Settings live in `config.py` and can be overridden with environment variables:

| Variable        | Purpose                                              | Default                       |
|-----------------|------------------------------------------------------|-------------------------------|
| `SECRET_KEY`    | Flask session signing key (set this in production)   | dev placeholder               |
| `DATABASE_URL`  | SQLAlchemy URL; use PostgreSQL to migrate            | `sqlite:///instance/pricing.db` |
| `SEED_DIR`      | Folder holding the source workbooks                  | `../seed_data`                |
| `RF_ADMIN_USER` / `RF_ADMIN_PASS` / `RF_ADMIN_NAME` | Non-interactive admin creation | unset |

Editable in-app (Admin → Settings): company name, VAT rate, USD/UGX rounding,
offer validity window.

### Moving to PostgreSQL

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@localhost/ranchers_pricing"
python setup.py
python importer.py
```

---

## Roles & rights

| Role    | Generic lists | Customer lists                | Users/settings |
|---------|---------------|-------------------------------|----------------|
| Admin   | view + edit*  | all                           | manage         |
| Manager | view + edit*  | all (create/edit)             | no             |
| Rep     | view (edit*)  | only assigned customers       | no             |

\* Editing prices and exchange rates also requires the **edit/pricing right**, a
flag assignable per user independent of role. Admins always have it. A user without
it is read-only and is blocked server-side from any write.

---

## Project layout

```
ranchers_pricing/
  app.py                 Flask entry point (app factory; `app:app` for gunicorn)
  config.py              Configuration
  extensions.py          Shared db / login-manager
  models.py              SQLAlchemy models
  setup.py               Create schema + first admin (run once)
  importer.py            Seed import (6 workbooks) + on-demand mapped import
  requirements.txt
  blueprints/            auth, dashboard, pricelists, customer_pricelists,
                         offers, customers, products, exchange_rates, admin
  services/              security (RBAC + bcrypt), currency, pricing, exports,
                         audit, settings
  templates/             Bootstrap 5 branded templates
  static/                bundled bootstrap, icons, fonts, brand.css, logo
seed_data/               the six source workbooks
```

---

## Daily digest (optional, scheduled)

`daily_digest.py` prints and saves a morning operations summary (fulfilment
queue, today's orders, reorder-due and at-risk customers) to `instance/digests/`.
It can also email it if SMTP variables are set:

```bash
python daily_digest.py
# Email as well:
export RF_SMTP_HOST=smtp.example.com RF_SMTP_PORT=587 \
       RF_SMTP_USER=you RF_SMTP_PASS=secret \
       RF_DIGEST_FROM=ops@ranchersfinest.local RF_DIGEST_TO="boss@firm.com,ops@firm.com"
python daily_digest.py
```

Schedule it to run each morning:

```cron
# macOS/Linux crontab — 6am daily
0 6 * * * cd /opt/ranchers_pricing && /usr/bin/python3 daily_digest.py >> instance/digests/cron.log 2>&1
```

On Windows use Task Scheduler: action = `python.exe`, argument = `daily_digest.py`,
"Start in" = the app folder, trigger = daily.

## Reports

Reports tab (managers, admins, order managers): Fulfilment, Sales (total /
customer / product / rep), Customer insights (product mix & trends), Lapsed &
at-risk, Reorder due, Customer scorecard (month-on-month), Product velocity,
Fulfilment KPIs (fill rate & on-time dispatch), and Offer conversion. Most
export to CSV.

## Security notes

- Passwords hashed with bcrypt.
- 5 failed logins lock an account for 15 minutes.
- Sessions expire after 8 hours of inactivity; "remember me" lasts 30 days.
- Authorization is enforced on the server for every protected route, not only in
  the UI.
- Set a strong `SECRET_KEY` and serve over HTTPS in production. Back up
  `instance/pricing.db` regularly (or use PostgreSQL with its own backups).
