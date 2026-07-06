"""Authentication: login, logout, password change, with lockout protection."""
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import (Blueprint, render_template, redirect, url_for, request,
                   flash, current_app)
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db
from models import User
from services.security import verify_password, hash_password
from services.audit import log

bp = Blueprint("auth", __name__)


def _safe_next(nxt):
    """Return nxt only if it is a relative, same-site path; else None.

    Blocks open-redirect phishing via ?next=https://evil.example.com. A valid
    target is a path beginning with a single "/" and carrying no scheme or
    network location (netloc). Protocol-relative "//host" URLs are rejected
    because their netloc is truthy.
    """
    if not nxt:
        return None
    parsed = urlparse(nxt)
    if parsed.scheme or parsed.netloc:
        return None
    if not nxt.startswith("/") or nxt.startswith("//"):
        return None
    return nxt


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        user = db.session.scalar(db.select(User).filter_by(username=username))

        now = datetime.utcnow()
        if user and user.locked_until and user.locked_until > now:
            mins = int((user.locked_until - now).total_seconds() // 60) + 1
            flash(f"Account locked. Try again in {mins} minute(s).", "danger")
            return render_template("login.html")

        if user and not user.is_active:
            flash("This account is disabled. Contact an administrator.", "danger")
            return render_template("login.html")

        if user and verify_password(password, user.password_hash):
            user.failed_attempts = 0
            user.locked_until = None
            user.last_login = now
            db.session.commit()
            login_user(user, remember=remember,
                       duration=current_app.config["REMEMBER_COOKIE_DURATION"] if remember else None)
            log("login", "user", user.id, detail="successful login", commit=True)
            nxt = _safe_next(request.args.get("next"))
            if nxt:
                return redirect(nxt)
            if user.is_customer_user:
                return redirect(url_for("portal.home"))
            return redirect(url_for("dashboard.home"))

        # Failed login. Keep the lockout counter running for real accounts, but
        # show one generic message for every wrong-credential case (unknown user,
        # wrong password, and lockout counting) so the message text cannot be
        # used to enumerate which usernames exist. The distinct locked/disabled
        # messages above are account states the user already knows about.
        if user:
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= current_app.config["MAX_LOGIN_ATTEMPTS"]:
                user.locked_until = now + timedelta(minutes=current_app.config["LOCKOUT_MINUTES"])
                user.failed_attempts = 0
            db.session.commit()
        flash("Invalid username or password.", "danger")
        return render_template("login.html")

    return render_template("login.html")


@bp.route("/login-image")
def login_image():
    """Serve the admin-uploaded login image (public)."""
    import os
    from flask import current_app, send_file, abort
    from services import settings as settings_svc
    name = settings_svc.get("login_image", None)
    if not name:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], "branding", name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@bp.route("/logout")
@login_required
def logout():
    log("logout", "user", current_user.id, commit=True)
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not verify_password(current, current_user.password_hash):
            flash("Current password is incorrect.", "danger")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "danger")
        elif new != confirm:
            flash("New passwords do not match.", "danger")
        else:
            current_user.password_hash = hash_password(new)
            db.session.commit()
            log("password_change", "user", current_user.id, commit=True)
            flash("Password updated.", "success")
        return redirect(url_for("auth.account"))
    return render_template("account.html")
