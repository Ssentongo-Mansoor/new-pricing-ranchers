# Deploying Ranchers Finest Sales Hub on cPanel (no-downtime updates)

This app is built so code and data are separate. The database and all uploaded
files live in `instance/`, which is gitignored. Deploys only change code; they
never touch `instance/`. On restart the app runs additive migrations, so schema
changes apply without losing data.

Updates use Passenger's graceful restart: touching `tmp/restart.txt` starts new
workers on the new code while current requests finish on the old ones. For a
code update this is effectively no downtime.

---

## One-time setup

### 1. Push this code to a private GitHub repo
From the app folder (where `app.py` lives):

```
git init
git add .
git commit -m "Initial deploy"
git branch -M main
git remote add origin git@github.com:YOURNAME/ranchers-sales-hub.git
git push -u origin main
```

`instance/` (database + uploads) is gitignored, so none of your live data is
pushed. Good.

### 2. Create the Python app in cPanel
cPanel → **Setup Python App** → Create Application:
- Python version: 3.11 (or the newest 3.x offered)
- Application root: e.g. `ranchers_app`
- Application URL: your domain/subdomain
- Application startup file: `passenger_wsgi.py`
- Application Entry point: `application`
- Add **Environment variables**:
  - `SECRET_KEY` = a long random string (required; do not commit it)
  - `DATABASE_URL` = only if you move off SQLite (see note at bottom)

Click Create. cPanel makes a virtualenv and shows a line like
`source /home/CPUSER/virtualenv/ranchers_app/3.11/bin/activate` — note that path,
you need it for `.cpanel.yml`.

### 3. Connect the repo in cPanel
cPanel → **Git Version Control** → Create:
- Clone URL: your private repo (use a read-only deploy key on GitHub for this)
- Repository path: e.g. `/home/CPUSER/repositories/ranchers`

### 4. Point `.cpanel.yml` at your account
Edit `.cpanel.yml` (already in this repo) and set the two variables at the top:
- `APPDIR` = the Application root path from step 2
  (e.g. `/home/CPUSER/ranchers_app`)
- `VENVBIN` = the `bin` folder from the activate path in step 2
Commit and push the change.

### 5. First deploy + load your data
- In Git Version Control, click **Update from Remote**, then **Deploy HEAD Commit**.
  This copies code into the app dir, installs requirements, fixes permissions, and restarts.
- Upload your existing `instance/pricing.db` (and the `instance/uploads/` folder)
  into the app's `instance/` directory via cPanel File Manager or SFTP. This is a
  one-time move of your current data. It is never overwritten by future deploys.
- Set file permissions if needed: folders 755, files 644 (Passenger refuses to
  run if files are group/other-writable).

Visit your URL — the app should be live.

---

## Every update after that

1. On your machine: make changes, then
   ```
   git add -A && git commit -m "what changed" && git push
   ```
2. In cPanel → Git Version Control: **Update from Remote** → **Deploy HEAD Commit**.

That runs `.cpanel.yml`: syncs code (keeping `instance/` and `tmp/`), installs
any new requirements, fixes permissions, and touches `tmp/restart.txt` for a
graceful restart. Your database and uploads are untouched. Any new database
columns are added automatically on the first request after restart.

To roll back: in GitHub revert the commit (or `git revert`), push, then Deploy
again.

---

## Optional: fully automatic "push = live" (needs SSH)

If your host gives SSH access, you can skip the two cPanel clicks. Add a GitHub
Actions workflow that SSHes in on every push to `main` and runs:

```
cd ~/ranchers_app && git pull && \
  ~/virtualenv/ranchers_app/3.11/bin/pip install -r requirements.txt && \
  mkdir -p tmp && touch tmp/restart.txt
```

(For this, clone the repo directly as the app directory instead of using rsync.)
Store the SSH key as a GitHub Actions secret. Ask me and I'll write the workflow.

---

## Note on the database engine

SQLite (your current `instance/pricing.db`) works on cPanel and deploys never
touch it. Its one limit is many people writing at the exact same second, which
can cause brief "database is locked" waits. If that becomes an issue, cPanel
includes MySQL/MariaDB: create a database there, set `DATABASE_URL` in Setup
Python App to point at it, and I can migrate your data across. No code changes
needed — the app already reads `DATABASE_URL`.
