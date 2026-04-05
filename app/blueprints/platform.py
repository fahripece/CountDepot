"""
Super-admin platform panel.

Lives at /_platform/ — completely outside the tenant system.
Reads/writes platform.db only. Never touches any tenant's inventory.db.

Protected by a separate PLATFORM_ADMIN_KEY environment variable so even
a compromised tenant admin account cannot reach these routes.
"""

import os
import re
import secrets
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, request, session,
                   redirect, url_for, jsonify, abort)

from app.platform import (get_platform_db, create_tenant,
                           get_tenant_by_slug, init_platform_db)
from app.helpers  import hash_pw, check_rate_limit
from config       import Config

bp = Blueprint("platform", __name__, url_prefix="/_platform")

# ── Platform-admin auth ───────────────────────────────────────────────────────

PLATFORM_ADMIN_PASSWORD = os.environ.get("PLATFORM_ADMIN_PASSWORD", "platform-change-me")
SESSION_KEY     = "_platform_authed"
MFA_PENDING_KEY = "_platform_mfa_pending"   # set after password, cleared after OTP
MFA_ENABLED     = bool(Config.PLATFORM_ADMIN_EMAIL)


def platform_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(SESSION_KEY):
            return redirect(url_for("platform.login"))
        return f(*args, **kwargs)
    return decorated


def _send_mfa_code():
    """Generate a 6-digit OTP, store it in platform.db, email it.
    Returns the token string so callers can log it if needed."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (datetime.utcnow() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    db = get_platform_db()
    try:
        # Invalidate any previous unused tokens
        db.execute("UPDATE platform_mfa_tokens SET used=1 WHERE used=0")
        db.execute("INSERT INTO platform_mfa_tokens (token, expires_at, used, created_at) VALUES (?,?,0,?)",
                   [code, expires_at, now])
        db.commit()
    finally:
        db.close()

    from app.mailer import send_email
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h2 style="font-size:18px;font-weight:700;margin-bottom:8px">CountDepot Platform — sign-in code</h2>
  <p style="color:#64748b;margin-bottom:24px;font-size:13px">Someone just entered the correct platform admin password. Use this code to complete sign-in. It expires in <strong>10 minutes</strong>.</p>
  <div style="background:#0f172a;color:#fff;font-size:36px;font-weight:700;letter-spacing:10px;text-align:center;padding:24px;border-radius:10px;font-family:monospace">{code}</div>
  <p style="margin-top:20px;font-size:12px;color:#94a3b8">If you did not attempt to log in, someone has your platform admin password — change it immediately.</p>
</div>"""
    send_email(Config.PLATFORM_ADMIN_EMAIL,
               "CountDepot platform sign-in code",
               html,
               f"Your CountDepot platform sign-in code: {code}\n\nExpires in 10 minutes.\n\nIf you did not attempt to log in, change your PLATFORM_ADMIN_PASSWORD immediately.")
    return code


def _verify_mfa_code(code: str) -> bool:
    """Check the OTP against platform.db. Marks it used on success."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    db = get_platform_db()
    try:
        row = db.execute(
            "SELECT id FROM platform_mfa_tokens WHERE token=? AND used=0 AND expires_at > ?",
            [code.strip(), now]).fetchone()
        if not row:
            return False
        db.execute("UPDATE platform_mfa_tokens SET used=1 WHERE id=?", [row[0]])
        db.commit()
        return True
    finally:
        db.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_tenants():
    db   = get_platform_db()
    rows = db.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
    db.close()
    return [dict(r) for r in rows]


def _tenant_stats(slug):
    """Return basic stats from a tenant's inventory DB without going through
    the request-scoped get_db() — we're outside a normal tenant request here."""
    import sqlite3
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return {"items": 0, "users": 0, "db_size_kb": 0, "last_login": None}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        items      = db.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
        users      = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        last_login = db.execute(
            "SELECT MAX(last_login) FROM users WHERE last_login IS NOT NULL"
        ).fetchone()[0]
    except Exception:
        items = users = 0
        last_login = None
    finally:
        db.close()
    size_kb = round(os.path.getsize(db_path) / 1024, 1)
    return {"items": items, "users": users, "db_size_kb": size_kb, "last_login": last_login}


def _valid_slug(slug):
    """Slugs must be lowercase alphanumeric + hyphens, 2-32 chars."""
    return bool(re.match(r'^[a-z0-9][a-z0-9\-]{1,31}$', slug))


# ── Auth routes ───────────────────────────────────────────────────────────────

@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get(SESSION_KEY):
        return redirect(url_for("platform.dashboard"))
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        allowed, retry_in = check_rate_limit(ip, "platform-login", max_attempts=5, window=300)
        if not allowed:
            error = f"Too many attempts. Try again in {retry_in // 60 + 1} minutes."
            return render_template("platform/login.html", error=error)
        pw = request.form.get("password", "")
        if pw == PLATFORM_ADMIN_PASSWORD:
            if MFA_ENABLED:
                _send_mfa_code()
                session[MFA_PENDING_KEY] = True
                return redirect(url_for("platform.verify_mfa"))
            session[SESSION_KEY] = True
            return redirect(url_for("platform.dashboard"))
        error = "Incorrect password."
    return render_template("platform/login.html", error=error, mfa_enabled=MFA_ENABLED)


@bp.route("/verify-mfa", methods=["GET", "POST"])
def verify_mfa():
    if session.get(SESSION_KEY):
        return redirect(url_for("platform.dashboard"))
    if not session.get(MFA_PENDING_KEY):
        return redirect(url_for("platform.login"))
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        allowed, retry_in = check_rate_limit(ip, "platform-mfa", max_attempts=5, window=300)
        if not allowed:
            error = f"Too many attempts. Try again in {retry_in // 60 + 1} minutes."
            return render_template("platform/verify_mfa.html", error=error,
                                   email=Config.PLATFORM_ADMIN_EMAIL)
        code = request.form.get("code", "").strip()
        if _verify_mfa_code(code):
            session.pop(MFA_PENDING_KEY, None)
            session[SESSION_KEY] = True
            return redirect(url_for("platform.dashboard"))
        error = "Invalid or expired code. Check your email and try again."
    return render_template("platform/verify_mfa.html", error=error,
                           email=Config.PLATFORM_ADMIN_EMAIL)


@bp.route("/logout")
def logout():
    session.pop(SESSION_KEY, None)
    session.pop(MFA_PENDING_KEY, None)
    return redirect(url_for("platform.login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@bp.route("/")
@platform_login_required
def dashboard():
    from datetime import date
    tenants = _all_tenants()
    for t in tenants:
        t["stats"] = _tenant_stats(t["slug"])
    today = date.today().isoformat()
    total_items   = sum(t["stats"]["items"] for t in tenants)
    total_users   = sum(t["stats"]["users"] for t in tenants)
    total_kb      = sum(t["stats"]["db_size_kb"] for t in tenants)
    count_active  = sum(1 for t in tenants if t["active"] and t.get("subscription_status") == "active")
    count_trial   = sum(1 for t in tenants if t["active"] and t.get("subscription_status") == "trial")
    count_overdue = sum(1 for t in tenants if t.get("subscription_status") == "overdue")
    return render_template("platform/dashboard.html",
                           tenants=tenants,
                           total_items=total_items,
                           total_users=total_users,
                           total_kb=total_kb,
                           count_active=count_active,
                           count_trial=count_trial,
                           count_overdue=count_overdue,
                           today=today)


# ── Create tenant ─────────────────────────────────────────────────────────────

@bp.route("/tenant/new", methods=["GET", "POST"])
@platform_login_required
def tenant_new():
    error = None
    if request.method == "POST":
        name  = request.form.get("name",  "").strip()
        slug  = request.form.get("slug",  "").strip().lower()
        plan  = request.form.get("plan",  "standard")
        admin_pw = request.form.get("admin_password", "").strip()

        if not name:
            error = "Company name is required."
        elif not slug:
            error = "Slug is required."
        elif not _valid_slug(slug):
            error = "Slug must be lowercase letters, numbers and hyphens (2–32 chars)."
        elif get_tenant_by_slug(slug):
            error = f"Slug '{slug}' is already taken."
        elif len(admin_pw) < 8:
            error = "Admin password must be at least 8 characters."
        else:
            # 1. Register tenant in platform.db
            create_tenant(slug, name, plan)

            # 2. Bootstrap the tenant's inventory DB
            _bootstrap_tenant_db(slug, admin_pw)

            return redirect(url_for("platform.dashboard"))

    # Auto-suggest slug from name via JS
    return render_template("platform/tenant_new.html", error=error)


def _bootstrap_tenant_db(slug, admin_password):
    """Create schema + seed default data for a brand-new tenant."""
    import sqlite3
    from app.schema import _init_db_conn

    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    # schema.py owns the DDL — _init_db_conn works on any raw connection
    _init_db_conn(db)

    # Seed distributors (not part of init_db — bootstrap-only)
    for d_name in ["CDW", "SHI", "Insight", "Zones", "PC Connection",
                   "Provantage", "B&H Photo", "Newegg Business",
                   "Amazon Business", "Staples Business",
                   "TigerDirect", "Micro Center"]:
        try:
            db.execute("INSERT INTO distributors (name) VALUES (?)", [d_name])
        except Exception:
            pass

    # Create admin user — must change password on first login
    db.execute(
        "INSERT INTO users (username,password,role,permissions,must_change_password) VALUES (?,?,?,?,1)",
        ["admin", hash_pw(admin_password), "admin", ""])

    db.commit()
    db.close()


# ── Suspend / reactivate tenant ───────────────────────────────────────────────

@bp.route("/tenant/<slug>/suspend", methods=["POST"])
@platform_login_required
def tenant_suspend(slug):
    db = get_platform_db()
    db.execute("UPDATE tenants SET active=0 WHERE slug=?", [slug])
    db.commit(); db.close()
    return redirect(url_for("platform.dashboard"))


@bp.route("/tenant/<slug>/activate", methods=["POST"])
@platform_login_required
def tenant_activate(slug):
    db = get_platform_db()
    db.execute("UPDATE tenants SET active=1 WHERE slug=?", [slug])
    db.commit(); db.close()
    return redirect(url_for("platform.dashboard"))


# ── Edit tenant (name / plan) ─────────────────────────────────────────────────

@bp.route("/tenant/<slug>/edit", methods=["POST"])
@platform_login_required
def tenant_edit(slug):
    name = request.form.get("name", "").strip()
    plan = request.form.get("plan", "standard")
    if name:
        db = get_platform_db()
        db.execute("UPDATE tenants SET name=?, plan=? WHERE slug=?", [name, plan, slug])
        db.commit(); db.close()
    return redirect(url_for("platform.dashboard"))


# ── Billing / subscription ────────────────────────────────────────────────────

@bp.route("/tenant/<slug>/billing", methods=["POST"])
@platform_login_required
def tenant_billing(slug):
    status     = request.form.get("subscription_status", "trial").strip()
    trial_ends = request.form.get("trial_ends_at", "").strip() or None
    notes      = request.form.get("notes", "").strip() or None
    db = get_platform_db()
    db.execute(
        "UPDATE tenants SET subscription_status=?, trial_ends_at=?, notes=? WHERE slug=?",
        [status, trial_ends, notes, slug])
    db.commit(); db.close()
    return redirect(url_for("platform.dashboard"))


# ── Delete tenant ─────────────────────────────────────────────────────────────

@bp.route("/tenant/<slug>/delete", methods=["POST"])
@platform_login_required
def tenant_delete(slug):
    import shutil
    confirm = request.form.get("confirm_slug", "").strip()
    if confirm != slug:
        return redirect(url_for("platform.dashboard"))
    db = get_platform_db()
    db.execute("DELETE FROM tenants WHERE slug=?", [slug])
    db.commit(); db.close()
    tenant_dir = os.path.join(Config.TENANTS_DIR, slug)
    if os.path.exists(tenant_dir):
        shutil.rmtree(tenant_dir)
    return redirect(url_for("platform.dashboard"))


# ── API: tenant list (for any future JS dashboard) ────────────────────────────

@bp.route("/api/tenants")
@platform_login_required
def api_tenants():
    tenants = _all_tenants()
    for t in tenants:
        t["stats"] = _tenant_stats(t["slug"])
    return jsonify(tenants)
