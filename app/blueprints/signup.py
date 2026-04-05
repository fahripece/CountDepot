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
            create_tenant(slug, name, "starter")

            # 2. Bootstrap inventory DB — email is the login identifier
            _bootstrap_tenant_db(slug, password, admin_email=email)

            # must_change_password=0 for self-signup (they chose their own password)
            db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
            db = sqlite3.connect(db_path)
            db.execute("UPDATE users SET must_change_password=0 WHERE email=?", [email])
            db.commit()
            db.close()
            verify_token = None  # already sent by _bootstrap_tenant_db if SMTP configured

            # 4. Set 7-day trial + create Stripe customer
            from app.platform import get_platform_db
            trial_ends = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            pdb = get_platform_db()
            pdb.execute("UPDATE tenants SET subscription_status='trial', trial_ends_at=? WHERE slug=?",
                        [trial_ends, slug])
            pdb.commit()
            pdb.close()
            try:
                from app.stripe_billing import create_customer as _create_stripe_customer
                stripe_cid = _create_stripe_customer(email, name, slug)
                if stripe_cid:
                    pdb2 = get_platform_db()
                    pdb2.execute("UPDATE tenants SET stripe_customer_id=? WHERE slug=?",
                                 [stripe_cid, slug])
                    pdb2.commit()
                    pdb2.close()
            except Exception:
                pass

            # 5. Send welcome email (verification already sent by _bootstrap_tenant_db)
            send_welcome_email(email, name, slug, temp_password=None, verify_token=None)

            return render_template("signup_success.html",
                                   name=name,
                                   slug=slug,
                                   domain=Config.APP_DOMAIN)

    return render_template("signup.html", error=error)
