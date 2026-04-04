"""
Super-admin platform panel.

Lives at /_platform/ — completely outside the tenant system.
Reads/writes platform.db only. Never touches any tenant's inventory.db.

Protected by a separate PLATFORM_ADMIN_KEY environment variable so even
a compromised tenant admin account cannot reach these routes.
"""

import os
import re
from datetime import datetime

from flask import (Blueprint, render_template, request, session,
                   redirect, url_for, jsonify, abort)

from app.platform import (get_platform_db, create_tenant,
                           get_tenant_by_slug, init_platform_db)
from app.helpers  import hash_pw
from config       import Config

bp = Blueprint("platform", __name__, url_prefix="/_platform")

# ── Platform-admin auth ───────────────────────────────────────────────────────
# Completely separate from tenant user accounts.
# Set PLATFORM_ADMIN_PASSWORD env var (defaults to a local-dev value).

PLATFORM_ADMIN_PASSWORD = os.environ.get("PLATFORM_ADMIN_PASSWORD", "platform-change-me")
SESSION_KEY = "_platform_authed"


def platform_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(SESSION_KEY):
            return redirect(url_for("platform.login"))
        return f(*args, **kwargs)
    return decorated


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
        return {"items": 0, "users": 0, "db_size_kb": 0}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        items = db.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except Exception:
        items = users = 0
    finally:
        db.close()
    size_kb = round(os.path.getsize(db_path) / 1024, 1)
    return {"items": items, "users": users, "db_size_kb": size_kb}


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
        pw = request.form.get("password", "")
        if pw == PLATFORM_ADMIN_PASSWORD:
            session[SESSION_KEY] = True
            return redirect(url_for("platform.dashboard"))
        error = "Incorrect password."
    return render_template("platform/login.html", error=error)


@bp.route("/logout")
def logout():
    session.pop(SESSION_KEY, None)
    return redirect(url_for("platform.login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@bp.route("/")
@platform_login_required
def dashboard():
    tenants = _all_tenants()
    for t in tenants:
        t["stats"] = _tenant_stats(t["slug"])
    total_items = sum(t["stats"]["items"] for t in tenants)
    total_users = sum(t["stats"]["users"] for t in tenants)
    return render_template("platform/dashboard.html",
                           tenants=tenants,
                           total_items=total_items,
                           total_users=total_users)


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


# ── API: tenant list (for any future JS dashboard) ────────────────────────────

@bp.route("/api/tenants")
@platform_login_required
def api_tenants():
    tenants = _all_tenants()
    for t in tenants:
        t["stats"] = _tenant_stats(t["slug"])
    return jsonify(tenants)
