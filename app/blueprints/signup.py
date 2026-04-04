"""
Public self-serve signup — /signup

Disabled when SIGNUP_ENABLED=false (env var), which forces all tenant
creation to go through the /_platform/ super-admin panel.

Flow:
  1. User fills in company name, slug, email, password
  2. Tenant created + DB bootstrapped instantly
  3. Welcome email sent (logs to console if SMTP not configured)
  4. Success page shown with their workspace URL
"""

import re
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request

from app.platform            import create_tenant, get_tenant_by_slug
from app.blueprints.platform import _bootstrap_tenant_db
from app.mailer              import send_welcome_email
from config                  import Config

bp = Blueprint("signup", __name__)


def _valid_slug(slug):
    return bool(re.match(r'^[a-z0-9][a-z0-9\-]{1,31}$', slug))


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if not Config.SIGNUP_ENABLED:
        return render_template("signup_disabled.html"), 404

    error = None

    if request.method == "POST":
        name     = request.form.get("name",     "").strip()
        slug     = request.form.get("slug",     "").strip().lower()
        email    = request.form.get("email",    "").strip().lower()
        password = request.form.get("password", "")

        if not name:
            error = "Company name is required."
        elif not slug:
            error = "A subdomain is required."
        elif not _valid_slug(slug):
            error = "Subdomain must be lowercase letters, numbers and hyphens (2–32 chars)."
        elif get_tenant_by_slug(slug):
            error = f"The subdomain '{slug}' is already taken. Please choose another."
        elif not email or "@" not in email:
            error = "A valid email address is required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        else:
            # 1. Register tenant in platform.db + create data directory
            create_tenant(slug, name, "standard")

            # 2. Bootstrap inventory DB (schema + seed + admin account)
            _bootstrap_tenant_db(slug, password)

            # 3. Stamp the admin's email + set email_verified=0, generate verify token
            db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
            db = sqlite3.connect(db_path)
            db.execute(
                "UPDATE users SET email=?, must_change_password=0, email_verified=0 "
                "WHERE username='admin'",
                [email])
            user_id = db.execute(
                "SELECT id FROM users WHERE username='admin'").fetchone()[0]
            verify_token  = secrets.token_urlsafe(32)
            token_expires = (datetime.utcnow() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "INSERT INTO email_verification_tokens (user_id, token, expires_at) "
                "VALUES (?,?,?)",
                [user_id, verify_token, token_expires])
            db.commit()
            db.close()

            # 4. Send welcome email with verification link (no-op if SMTP not configured)
            send_welcome_email(email, name, slug, password, verify_token=verify_token)

            return render_template("signup_success.html",
                                   name=name,
                                   slug=slug,
                                   domain=Config.APP_DOMAIN)

    return render_template("signup.html", error=error)
