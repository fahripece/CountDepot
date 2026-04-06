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


# ── Stripe diagnostics ───────────────────────────────────────────────────────

@bp.route("/stripe-check")
@platform_login_required
def stripe_check():
    """Show Stripe config and do a live API test."""
    sk = Config.STRIPE_SECRET_KEY or ""
    rows = []
    price_keys = [
        "STRIPE_PRICE_STARTER_MONTHLY",
        "STRIPE_PRICE_STARTER_YEARLY",
        "STRIPE_PRICE_PRO_MONTHLY",
        "STRIPE_PRICE_PRO_YEARLY",
        "STRIPE_PRICE_ENTERPRISE_MONTHLY",
        "STRIPE_PRICE_ENTERPRISE_YEARLY",
    ]
    price_ids = {k: getattr(Config, k, "") or "" for k in price_keys}

    api_test = None
    price_tests = {}
    if sk:
        try:
            import stripe
            stripe.api_key = sk
            stripe.Account.retrieve()
            api_test = "OK"
        except Exception as e:
            api_test = f"ERROR: {e}"

        for k, pid in price_ids.items():
            if not pid:
                price_tests[k] = "NOT SET"
                continue
            try:
                import stripe
                stripe.api_key = sk
                stripe.Price.retrieve(pid)
                price_tests[k] = "OK"
            except Exception as e:
                price_tests[k] = f"ERROR: {e}"

    sk_display = (sk[:12] + "..." + sk[-4:]) if len(sk) > 16 else (sk or "NOT SET")
    pk = Config.STRIPE_PUBLISHABLE_KEY or ""
    pk_display = (pk[:12] + "..." + pk[-4:]) if len(pk) > 16 else (pk or "NOT SET")

    rows_html = ""
    for k, pid in price_ids.items():
        pid_display = (pid[:16] + "..." + pid[-6:]) if len(pid) > 22 else (pid or "NOT SET")
        result = price_tests.get(k, "—")
        color = "#15803d" if result == "OK" else ("#b91c1c" if result.startswith("ERROR") else "#92400e")
        rows_html += f"<tr><td style='padding:6px 12px;font-family:monospace'>{k}</td><td style='padding:6px 12px;font-family:monospace'>{pid_display}</td><td style='padding:6px 12px;color:{color};font-weight:600'>{result}</td></tr>"

    api_color = "#15803d" if api_test == "OK" else "#b91c1c"
    html = f"""<!DOCTYPE html><html><body style='font-family:sans-serif;padding:40px;max-width:900px;margin:0 auto'>
<h2>Stripe Configuration Check</h2>
<table style='border-collapse:collapse;width:100%;margin-bottom:24px'>
<tr style='background:#f1f5f9'><th style='padding:8px 12px;text-align:left'>Key</th><th style='padding:8px 12px;text-align:left'>Value</th><th style='padding:8px 12px;text-align:left'>Status</th></tr>
<tr><td style='padding:6px 12px;font-family:monospace'>STRIPE_SECRET_KEY</td><td style='padding:6px 12px;font-family:monospace'>{sk_display}</td><td style='padding:6px 12px;color:{api_color};font-weight:600'>{api_test or ('NOT SET' if not sk else '—')}</td></tr>
<tr><td style='padding:6px 12px;font-family:monospace'>STRIPE_PUBLISHABLE_KEY</td><td style='padding:6px 12px;font-family:monospace'>{pk_display}</td><td style='padding:6px 12px'>—</td></tr>
{rows_html}
</table>
<a href='/_platform/' style='color:#0f172a'>← Back to dashboard</a>
</body></html>"""
    return html


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

        admin_email = request.form.get("admin_email", "").strip().lower()
        if not name:
            error = "Company name is required."
        elif not slug:
            error = "Slug is required."
        elif not _valid_slug(slug):
            error = "Slug must be lowercase letters, numbers and hyphens (2–32 chars)."
        elif get_tenant_by_slug(slug):
            error = f"Slug '{slug}' is already taken."
        elif not admin_email or "@" not in admin_email:
            error = "A valid admin email is required."
        elif len(admin_pw) < 8:
            error = "Admin password must be at least 8 characters."
        else:
            # 1. Register tenant in platform.db
            create_tenant(slug, name, plan)

            # 2. Bootstrap the tenant's inventory DB
            _bootstrap_tenant_db(slug, admin_pw, admin_email=admin_email)

            return redirect(url_for("platform.dashboard"))

    # Auto-suggest slug from name via JS
    return render_template("platform/tenant_new.html", error=error)


def _bootstrap_tenant_db(slug, admin_password, admin_email=None, pre_hashed_password=None):
    """Create schema + seed default data for a brand-new tenant.

    Either admin_password (plain text) or pre_hashed_password (already hashed) must be supplied.
    pre_hashed_password is used by the email-verification signup flow where the hash was stored
    in pending_signups before the tenant was created.
    """
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

    # Create admin user — email is the login identifier
    pw_hash = pre_hashed_password if pre_hashed_password else hash_pw(admin_password)
    smtp_on = bool(Config.SMTP_HOST)
    # Mark email verified if: (a) coming from verify-signup flow, (b) no email, or (c) no SMTP
    email_verified = 1 if (pre_hashed_password or not admin_email or not smtp_on) else 0
    db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password)"
        " VALUES (?,?,?,?,?,?,1)",
        [admin_email or "admin", pw_hash, "admin", "",
         admin_email, email_verified])

    # If coming from the verify-signup flow (pre_hashed_password set), email is already
    # verified — skip verification email. For platform-admin-created accounts send it.
    if admin_email and not pre_hashed_password and smtp_on:
        user_id = db.execute("SELECT id FROM users WHERE email=?", [admin_email]).fetchone()[0]
        verify_token = secrets.token_urlsafe(32)
        token_expires = (datetime.utcnow() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO email_verification_tokens (user_id, token, expires_at) VALUES (?,?,?)",
            [user_id, verify_token, token_expires])
        db.commit()
        db.close()
        from app.mailer import send_verification_email
        send_verification_email(admin_email, slug, verify_token)
    else:
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



# ── Manual backup ─────────────────────────────────────────────────────────────

@bp.route("/backup", methods=["POST"])
@platform_login_required
def platform_backup():
    from app.platform import backup_all_dbs
    try:
        backup_dir, files = backup_all_dbs()
        return jsonify({"ok": True, "dir": backup_dir, "files": files,
                        "count": len(files)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ── Grant permanent free access ───────────────────────────────────────────────

@bp.route("/tenant/<slug>/grant-free", methods=["POST"])
@platform_login_required
def tenant_grant_free(slug):
    plan = request.form.get("plan", "pro")
    note = request.form.get("note", "").strip() or \
           f"Free access granted {datetime.utcnow().strftime('%Y-%m-%d')} by platform admin"
    db = get_platform_db()
    db.execute(
        "UPDATE tenants SET subscription_status='active', trial_ends_at=NULL, plan=?, notes=? WHERE slug=?",
        [plan, note, slug])
    db.commit(); db.close()
    return redirect(url_for("platform.dashboard"))


# ── Extend trial ──────────────────────────────────────────────────────────────

@bp.route("/tenant/<slug>/extend-trial", methods=["POST"])
@platform_login_required
def tenant_extend_trial(slug):
    from datetime import date as _date
    days = max(1, int(request.form.get("days", 30)))
    db   = get_platform_db()
    row  = db.execute("SELECT trial_ends_at FROM tenants WHERE slug=?", [slug]).fetchone()
    current = (row["trial_ends_at"] or "")[:10] if row else ""
    try:
        base = _date.fromisoformat(current) if current else _date.today()
        if base < _date.today():
            base = _date.today()
    except Exception:
        base = _date.today()
    new_end = (base + timedelta(days=days)).isoformat()
    db.execute(
        "UPDATE tenants SET subscription_status='trial', trial_ends_at=? WHERE slug=?",
        [new_end, slug])
    db.commit(); db.close()
    return redirect(url_for("platform.dashboard"))


# ── Set / clear tenant banner ─────────────────────────────────────────────────

@bp.route("/tenant/<slug>/banner", methods=["POST"])
@platform_login_required
def tenant_banner(slug):
    import sqlite3 as _sq
    message = request.form.get("message", "").strip()
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return redirect(url_for("platform.dashboard"))
    db = _sq.connect(db_path)
    if message:
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('banner_message',?)", [message])
    else:
        db.execute("DELETE FROM settings WHERE key='banner_message'")
    db.commit(); db.close()
    return redirect(url_for("platform.dashboard"))


# ── Tenant users list (JSON) ──────────────────────────────────────────────────

@bp.route("/tenant/<slug>/users")
@platform_login_required
def tenant_users(slug):
    import sqlite3 as _sq
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return jsonify([])
    db = _sq.connect(db_path)
    db.row_factory = _sq.Row
    try:
        rows = db.execute(
            "SELECT id, username, email, role, last_login, session_token FROM users"
        ).fetchall()
    except Exception:
        rows = []
    finally:
        db.close()
    return jsonify([{
        "id":             r["id"],
        "username":       r["username"],
        "email":          r["email"] or "",
        "role":           r["role"],
        "last_login":     r["last_login"] or "",
        "active_session": bool(r["session_token"]),
    } for r in rows])


# ── Force-logout a user ───────────────────────────────────────────────────────

@bp.route("/tenant/<slug>/user/<int:user_id>/force-logout", methods=["POST"])
@platform_login_required
def tenant_force_logout(slug, user_id):
    import sqlite3 as _sq
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return jsonify({"ok": False, "msg": "Tenant not found"}), 404
    db = _sq.connect(db_path)
    db.execute("UPDATE users SET session_token=NULL WHERE id=?", [user_id])
    db.commit(); db.close()
    return jsonify({"ok": True})


# ── Reset a user's password ───────────────────────────────────────────────────

@bp.route("/tenant/<slug>/user/<int:user_id>/reset-password", methods=["POST"])
@platform_login_required
def tenant_reset_password(slug, user_id):
    import sqlite3 as _sq
    data   = request.get_json(silent=True) or {}
    new_pw = data.get("password", "").strip()
    if len(new_pw) < 8:
        return jsonify({"ok": False, "msg": "Password must be at least 8 characters."}), 400
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return jsonify({"ok": False, "msg": "Tenant not found"}), 404
    db = _sq.connect(db_path)
    db.execute(
        "UPDATE users SET password=?, must_change_password=1, session_token=NULL WHERE id=?",
        [hash_pw(new_pw), user_id])
    db.commit(); db.close()
    return jsonify({"ok": True})


# ── Impersonate: generate cross-login token ───────────────────────────────────

@bp.route("/tenant/<slug>/impersonate/<int:user_id>", methods=["POST"])
@platform_login_required
def tenant_impersonate(slug, user_id):
    import sqlite3 as _sq
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return jsonify({"ok": False, "msg": "Tenant DB not found"}), 404
    db = _sq.connect(db_path)
    user = db.execute("SELECT id FROM users WHERE id=?", [user_id]).fetchone()
    db.close()
    if not user:
        return jsonify({"ok": False, "msg": "User not found"}), 404

    token      = secrets.token_urlsafe(32)
    now        = datetime.utcnow()
    expires_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    pdb = get_platform_db()
    pdb.execute(
        "INSERT INTO cross_login_tokens (tenant_slug,user_id,token,expires_at,created_at) VALUES (?,?,?,?,?)",
        [slug, user_id, token, expires_at, now.strftime("%Y-%m-%d %H:%M:%S")])
    pdb.commit(); pdb.close()

    host = request.host
    if "localhost" in host or "127.0.0.1" in host:
        redirect_url = f"http://{host}/auto-login?token={token}"
    else:
        base = ".".join(host.split(".")[-2:])
        redirect_url = f"https://{slug}.{base}/auto-login?token={token}"

    return jsonify({"ok": True, "redirect": redirect_url})


# ── Download tenant DB ────────────────────────────────────────────────────────

@bp.route("/tenant/<slug>/download-db")
@platform_login_required
def tenant_download_db(slug):
    from flask import send_file as _sf
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        abort(404)
    return _sf(db_path, as_attachment=True, download_name=f"{slug}_inventory.db")


# ── Global user search (across all tenants) ───────────────────────────────────

@bp.route("/users/search")
@platform_login_required
def users_search():
    import sqlite3 as _sq
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    pdb     = get_platform_db()
    tenants = pdb.execute("SELECT slug, name FROM tenants WHERE active=1").fetchall()
    pdb.close()
    results = []
    for t in tenants:
        slug, tname = t[0], t[1]
        db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
        if not os.path.exists(db_path):
            continue
        db = _sq.connect(db_path)
        db.row_factory = _sq.Row
        try:
            rows = db.execute(
                "SELECT id, username, email, role, last_login, session_token FROM users "
                "WHERE LOWER(COALESCE(email,'')) LIKE ? OR LOWER(username) LIKE ?",
                [f"%{q}%", f"%{q}%"]
            ).fetchall()
        except Exception:
            rows = []
        finally:
            db.close()
        for u in rows:
            results.append({
                "tenant_slug":    slug,
                "tenant_name":    tname,
                "id":             u["id"],
                "username":       u["username"],
                "email":          u["email"] or "",
                "role":           u["role"],
                "last_login":     u["last_login"] or "",
                "active_session": bool(u["session_token"]),
            })
    return jsonify(results)


# ── Unlock rate-limited IP ────────────────────────────────────────────────────

@bp.route("/unlock-ip", methods=["POST"])
@platform_login_required
def unlock_ip():
    data = request.get_json(silent=True) or {}
    ip   = data.get("ip", "").strip()
    if not ip:
        return jsonify({"ok": False, "msg": "IP required"}), 400
    pdb = get_platform_db()
    pdb.execute("DELETE FROM rate_limits WHERE ip=?", [ip])
    pdb.commit(); pdb.close()
    return jsonify({"ok": True})


# ── Rate limits list ──────────────────────────────────────────────────────────

@bp.route("/rate-limits")
@platform_login_required
def rate_limits_list():
    pdb  = get_platform_db()
    rows = pdb.execute("""
        SELECT ip, endpoint, COUNT(*) as attempts, MAX(ts) as last_attempt
        FROM rate_limits
        GROUP BY ip, endpoint
        ORDER BY last_attempt DESC
        LIMIT 200
    """).fetchall()
    pdb.close()
    return jsonify([dict(r) for r in rows])


# ── Security log ──────────────────────────────────────────────────────────────

@bp.route("/security-log")
@platform_login_required
def security_log():
    q      = request.args.get("q", "").strip()
    page   = max(1, int(request.args.get("page", 1)))
    limit  = 100
    offset = (page - 1) * limit
    pdb    = get_platform_db()
    if q:
        like = f"%{q}%"
        rows  = pdb.execute(
            "SELECT * FROM security_log WHERE username LIKE ? OR ip LIKE ? "
            "OR tenant LIKE ? OR event LIKE ? OR detail LIKE ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            [like]*5 + [limit, offset]).fetchall()
        total = pdb.execute(
            "SELECT COUNT(*) FROM security_log WHERE username LIKE ? OR ip LIKE ? "
            "OR tenant LIKE ? OR event LIKE ? OR detail LIKE ?",
            [like]*5).fetchone()[0]
    else:
        rows  = pdb.execute(
            "SELECT * FROM security_log ORDER BY id DESC LIMIT ? OFFSET ?",
            [limit, offset]).fetchall()
        total = pdb.execute("SELECT COUNT(*) FROM security_log").fetchone()[0]
    pdb.close()
    pages = max(1, (total + limit - 1) // limit)
    return jsonify({"rows": [dict(r) for r in rows], "total": total,
                    "page": page, "pages": pages})
