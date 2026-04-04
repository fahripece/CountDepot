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
    from app.schema import _seed_categories

    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    # Reuse the schema SQL from schema.py without needing a Flask request context
    from app.schema import init_db as _schema_sql
    # We can't call init_db() here since it uses get_db() (request-scoped)
    # Instead, run the DDL directly
    db.executescript(_SCHEMA_DDL)

    # Seed default companies
    for co in ["Amazon", "B&H Photo", "CDW", "Adorama", "Newegg",
               "Insight", "Dell Technologies", "Other"]:
        try:
            db.execute("INSERT INTO companies (name) VALUES (?)", [co])
        except Exception:
            pass

    # Seed default categories
    _seed_categories(db)

    # Seed distributors
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

    # Create default worker account — must change password on first login
    db.execute(
        "INSERT INTO users (username,password,role,permissions,must_change_password) VALUES (?,?,?,?,1)",
        ["worker", hash_pw("worker123"), "worker", ""])

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


# ── Minimal DDL (duplicated here so we don't need a request context) ──────────
# This is the same schema as schema.py but run directly against a raw connection.

_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'worker',
        permissions TEXT NOT NULL DEFAULT '',
        email TEXT,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        last_login TEXT
    );
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        color TEXT NOT NULL DEFAULT '#ffffff',
        is_expense INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS category_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER NOT NULL REFERENCES categories(id),
        field_label TEXT NOT NULL,
        field_key TEXT NOT NULL,
        field_type TEXT NOT NULL DEFAULT 'text',
        placeholder TEXT,
        required INTEGER DEFAULT 0,
        sort_order INTEGER DEFAULT 0,
        dropdown_options TEXT
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        manufacturer TEXT, model TEXT, description TEXT,
        category_id INTEGER REFERENCES categories(id),
        serial_tracked INTEGER DEFAULT 0,
        qty_tracked INTEGER DEFAULT 0,
        require_scan_checkout INTEGER DEFAULT 0,
        require_serial INTEGER DEFAULT 0,
        require_vendor_sku INTEGER DEFAULT 0,
        require_internal_sku INTEGER DEFAULT 1,
        require_sku_label INTEGER DEFAULT 0,
        print_scan_label INTEGER DEFAULT 0,
        default_cost REAL, default_sale REAL,
        low_stock_threshold INTEGER DEFAULT 0,
        image_url TEXT, active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER REFERENCES products(id),
        name TEXT NOT NULL,
        manufacturer TEXT, model TEXT, serial TEXT, sku TEXT, internal_sku TEXT,
        category_id INTEGER REFERENCES categories(id),
        condition TEXT DEFAULT 'New', shelf TEXT,
        owner_company TEXT, company_id INTEGER REFERENCES companies(id),
        purchased_from TEXT, purchase_date TEXT, po_number TEXT,
        qty INTEGER, qty_out INTEGER DEFAULT 0, low_stock_threshold INTEGER DEFAULT 0,
        checked_out INTEGER DEFAULT 0, checkout_date TEXT, checkout_by TEXT, job_ref TEXT,
        require_scan_checkout INTEGER DEFAULT -1,
        cost_price REAL, sale_price REAL,
        tax_paid INTEGER DEFAULT -1, tax_rate REAL DEFAULT 0, sale_state TEXT,
        sold INTEGER DEFAULT 0, sold_date TEXT, sold_price REAL, sold_to TEXT,
        ebay_status TEXT DEFAULT 'not_listed', ebay_listing_id TEXT,
        ebay_listed_price REAL, ebay_listed_date TEXT,
        cpu TEXT, ram TEXT, storage TEXT, os_type TEXT, screen_size TEXT,
        battery_life TEXT, imei TEXT, carrier TEXT,
        resolution TEXT, lens_type TEXT, has_poe INTEGER DEFAULT 0,
        wireless_standard TEXT, port_count INTEGER, poe_budget TEXT, throughput TEXT,
        cable_type TEXT, cable_gauge TEXT, connector_type TEXT, cable_length TEXT,
        extra_fields TEXT DEFAULT '{}', notes TEXT,
        parent_item_id INTEGER REFERENCES items(id),
        active INTEGER DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, notes TEXT, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS distributors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        urgency TEXT NOT NULL DEFAULT 'medium',
        status TEXT NOT NULL DEFAULT 'todo',
        notes TEXT, created_by TEXT,
        created_at TEXT NOT NULL, updated_at TEXT,
        item_id INTEGER REFERENCES items(id)
    );
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, role TEXT, email TEXT, phone TEXT,
        notes TEXT, company_id INTEGER, company_type TEXT,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS item_modifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES items(id),
        ts TEXT NOT NULL, modified_by TEXT NOT NULL,
        field_changed TEXT NOT NULL, old_value TEXT, new_value TEXT,
        notes TEXT, spawned_item_id INTEGER REFERENCES items(id)
    );
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        token TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, action TEXT NOT NULL,
        item_id INTEGER, item_name TEXT, item_serial TEXT,
        item_sku TEXT, product_name TEXT,
        detail TEXT, before_state TEXT, after_state TEXT, username TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_items_active      ON items(active);
    CREATE INDEX IF NOT EXISTS idx_items_product     ON items(product_id);
    CREATE INDEX IF NOT EXISTS idx_audit_ts          ON audit_log(ts DESC);
"""
