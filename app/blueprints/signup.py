"""
Public self-serve signup — /signup

Flow:
  1. User fills form (name, slug, email, password)
  2. Validation runs; pending_signups row written to platform.db
  3. Verification email sent with /verify-signup/<token>
  4. "Check your email" page shown — NO tenant created yet
  5. User clicks link → /verify-signup/<token>
  6. Token validated (24h TTL, single-use), tenant created + DB bootstrapped
  7. Redirect to workspace URL

Disabled when SIGNUP_ENABLED=false (env var).
"""

import re
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for

from app.platform            import create_tenant, get_tenant_by_slug, get_tenant_by_owner_email, get_platform_db
from app.blueprints.platform import _bootstrap_tenant_db
from app.mailer              import send_signup_verification_email, send_welcome_email
from app.helpers             import hash_pw
from config                  import Config

bp = Blueprint("signup", __name__)


def _valid_slug(slug):
    return bool(re.match(r'^[a-z0-9][a-z0-9\-]{1,31}$', slug))


def _slug_belongs_to_email(slug, email):
    """Return True if `email` is already an admin user of tenant `slug`.
    Used to allow re-verification when a tenant was manually created."""
    if not email:
        return False
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT id FROM users WHERE email=?", [email]).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _clean_expired_pending():
    """Delete expired/used pending signup rows."""
    try:
        db = get_platform_db()
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        db.execute("DELETE FROM pending_signups WHERE expires_at < ? OR used = 1", [now])
        db.commit()
        db.close()
    except Exception:
        pass


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
        elif get_tenant_by_slug(slug) and not _slug_belongs_to_email(slug, email if "@" in (email or "") else ""):
            error = f"The subdomain '{slug}' is already taken. Please choose another."
        elif not email or "@" not in email:
            error = "A valid email address is required."
        elif get_tenant_by_owner_email(email):
            error = "An account already exists for that email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        else:
            _clean_expired_pending()

            # Check if there's already a pending (unexpired) signup for this email
            db = get_platform_db()
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            existing = db.execute(
                "SELECT id FROM pending_signups WHERE email=? AND used=0 AND expires_at>?",
                [email, now]
            ).fetchone()

            if existing:
                # Resend — update the token so it's fresh
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                db.execute(
                    "UPDATE pending_signups SET token=?, expires_at=?, slug=?, name=?, password_hash=? WHERE id=?",
                    [token, expires, slug, name, hash_pw(password), existing["id"]]
                )
            else:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                db.execute(
                    "INSERT INTO pending_signups (name, slug, email, password_hash, token, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [name, slug, email, hash_pw(password), token, now, expires]
                )
            db.commit()
            db.close()

            sent = send_signup_verification_email(email, name, token)

            return render_template("signup_verify_sent.html",
                                   email=email,
                                   smtp_enabled=bool(Config.SMTP_HOST),
                                   dev_token=token if not Config.SMTP_HOST else None)

    return render_template("signup.html", error=error)


@bp.route("/verify-signup/<token>")
def verify_signup(token):
    _clean_expired_pending()

    db = get_platform_db()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        "SELECT * FROM pending_signups WHERE token=? AND used=0 AND expires_at>?",
        [token, now]
    ).fetchone()

    if not row:
        db.close()
        # Check if there's a newer pending signup for the same email (form double-submitted)
        # — in that case the token was replaced but not yet used, so guide them to check inbox.
        # Also handle the case where the workspace was already created (link clicked twice).
        # We can detect the latter by checking if any tenant with that email exists.
        # For simplicity: show a helpful message with a "go to login" option if workspace exists,
        # otherwise tell them to check for a newer email.
        from app.platform import get_tenant_by_owner_email as _get_by_email
        import os as _os
        # Try to find tenant from email by scanning pending_signups for the token (already used)
        db2 = get_platform_db()
        used_row = db2.execute(
            "SELECT * FROM pending_signups WHERE token=?", [token]
        ).fetchone()
        db2.close()
        if used_row:
            slug_hint = used_row["slug"]
            email_hint = used_row["email"]
            tenant_exists = get_tenant_by_slug(slug_hint)
            if tenant_exists and _slug_belongs_to_email(slug_hint, email_hint):
                # Workspace already created — this is a duplicate click
                workspace_url = f"https://{slug_hint}.{Config.APP_DOMAIN}"
                return render_template("signup_success.html",
                                       name=used_row["name"],
                                       slug=slug_hint,
                                       domain=Config.APP_DOMAIN,
                                       smtp_enabled=bool(Config.SMTP_HOST),
                                       workspace_url=workspace_url)
            # Token was replaced by a newer one (double form-submit) — guide them to inbox
            return render_template("signup_verify_error.html",
                                   reason="This link has been replaced. Check your inbox for the most recent verification email.")
        return render_template("signup_verify_error.html",
                               reason="This verification link has expired or already been used.")

    slug  = row["slug"]
    name  = row["name"]
    email = row["email"]
    phash = row["password_hash"]

    # Slug might already exist (e.g. manually created by platform admin, or double-click on link)
    if get_tenant_by_slug(slug):
        # Check if this email is already the admin of that tenant — if so, just send them to login
        db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
        already_there = False
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                u = conn.execute("SELECT id FROM users WHERE email=?", [email]).fetchone()
                conn.close()
                if u:
                    already_there = True
            except Exception:
                pass
        db.execute("UPDATE pending_signups SET used=1 WHERE id=?", [row["id"]])
        db.commit()
        db.close()
        if already_there:
            workspace_url = f"https://{slug}.{Config.APP_DOMAIN}"
            return render_template("signup_success.html",
                                   name=name, slug=slug, domain=Config.APP_DOMAIN,
                                   smtp_enabled=bool(Config.SMTP_HOST),
                                   workspace_url=workspace_url)
        return render_template("signup_verify_error.html",
                               reason=f"The subdomain '{slug}' was taken while you were waiting. "
                                      "Please sign up again with a different subdomain.")

    # Mark token used first (idempotent safety)
    db.execute("UPDATE pending_signups SET used=1 WHERE id=?", [row["id"]])
    db.commit()
    db.close()

    # Create tenant + DB
    create_tenant(slug, name, "starter", owner_email=email)
    _bootstrap_tenant_db(slug, None, admin_email=email, pre_hashed_password=phash)

    # Clear must_change_password (they chose their own password)
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE users SET must_change_password=0 WHERE email=?", [email])
    conn.commit()
    conn.close()

    # Set 7-day trial
    trial_ends = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    pdb = get_platform_db()
    pdb.execute("UPDATE tenants SET subscription_status='trial', trial_ends_at=? WHERE slug=?",
                [trial_ends, slug])
    pdb.commit()
    pdb.close()

    # Stripe customer (optional)
    try:
        from app.stripe_billing import create_customer as _create_stripe_customer
        stripe_cid = _create_stripe_customer(email, name, slug)
        if stripe_cid:
            pdb2 = get_platform_db()
            pdb2.execute("UPDATE tenants SET stripe_customer_id=? WHERE slug=?", [stripe_cid, slug])
            pdb2.commit()
            pdb2.close()
    except Exception:
        pass

    # Welcome email
    send_welcome_email(email, name, slug, temp_password=None, verify_token=None)

    workspace_url = f"https://{slug}.{Config.APP_DOMAIN}"
    return render_template("signup_success.html",
                           name=name,
                           slug=slug,
                           domain=Config.APP_DOMAIN,
                           smtp_enabled=bool(Config.SMTP_HOST),
                           workspace_url=workspace_url)


@bp.route("/resend-signup-verify", methods=["POST"])
def resend_signup_verify():
    """Resend the verification email for a pending signup."""
    email = (request.form.get("email") or request.json.get("email", "")).strip().lower()
    if not email:
        return ("Email required", 400)

    db = get_platform_db()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        "SELECT * FROM pending_signups WHERE email=? AND used=0 AND expires_at>?",
        [email, now]
    ).fetchone()

    if row:
        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE pending_signups SET token=?, expires_at=? WHERE id=?",
                   [token, expires, row["id"]])
        db.commit()
        send_signup_verification_email(email, row["name"], token)

    db.close()
    # Always show the same page (don't leak whether email exists)
    return render_template("signup_verify_sent.html",
                           email=email,
                           smtp_enabled=bool(Config.SMTP_HOST),
                           dev_token=None,
                           resent=True)
