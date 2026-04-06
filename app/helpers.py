import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from functools import wraps

import bcrypt as _bcrypt
from flask import session, request, jsonify, redirect, url_for, g

from app.db import query, execute


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_pw(pw):
    """Hash a password with bcrypt."""
    return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()

def validate_password(pw):
    """Returns an error string if the password fails complexity rules, else None.
    Rules: 8+ chars, at least one uppercase letter, at least one digit or symbol."""
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r'[A-Z]', pw):
        return "Password must contain at least one uppercase letter."
    if not re.search(r'[0-9!@#$%^&*()\-_=+\[\]{}|;:,.<>?/\\\'"`~]', pw):
        return "Password must contain at least one number or special character."
    return None


def verify_pw(pw, stored_hash):
    """Verify a password against a stored hash.
    Accepts bcrypt hashes (current) and legacy HMAC-SHA256 hashes (pre-migration)."""
    if stored_hash.startswith("$2"):
        return _bcrypt.checkpw(pw.encode(), stored_hash.encode())
    # Legacy HMAC-SHA256 — accepted for accounts not yet migrated to bcrypt
    legacy = hmac.new(b"storelax-salt", pw.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(legacy, stored_hash)


# ── Rate limiting (SQLite-backed, shared across all Gunicorn workers) ─────────

LOGIN_MAX    = 10   # max login attempts per IP per window
LOGIN_WINDOW = 300  # seconds


def check_rate_limit(ip, endpoint="login", max_attempts=LOGIN_MAX, window=LOGIN_WINDOW):
    """Record an attempt and check whether the IP is over the limit.
    Returns (allowed: bool, retry_in: int seconds).
    Uses platform.db so limits are enforced across all workers."""
    from app.platform import get_platform_db
    now    = time.time()
    cutoff = datetime.fromtimestamp(now - window).strftime("%Y-%m-%d %H:%M:%S")
    now_s  = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    db = get_platform_db()
    try:
        db.execute("DELETE FROM rate_limits WHERE ts < ?", [cutoff])
        count = db.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE ip=? AND endpoint=?",
            [ip, endpoint]).fetchone()[0]
        if count >= max_attempts:
            oldest = db.execute(
                "SELECT ts FROM rate_limits WHERE ip=? AND endpoint=? "
                "ORDER BY ts ASC LIMIT 1", [ip, endpoint]).fetchone()
            db.commit()
            retry_in = window
            if oldest:
                oldest_ts = datetime.strptime(oldest[0], "%Y-%m-%d %H:%M:%S").timestamp()
                retry_in  = max(0, int(window - (now - oldest_ts)))
            return False, retry_in
        db.execute("INSERT INTO rate_limits (ip, endpoint, ts) VALUES (?,?,?)",
                   [ip, endpoint, now_s])
        db.commit()
        return True, 0
    finally:
        db.close()


def clear_rate_limit(ip, endpoint="login"):
    """Remove all rate-limit records for an IP+endpoint (call after successful action)."""
    from app.platform import get_platform_db
    db = get_platform_db()
    try:
        db.execute("DELETE FROM rate_limits WHERE ip=? AND endpoint=?", [ip, endpoint])
        db.commit()
    finally:
        db.close()


def check_login_rate(ip):
    return check_rate_limit(ip, "login")

def clear_login_rate(ip):
    clear_rate_limit(ip, "login")


# ── API rate-limit decorator ──────────────────────────────────────────────────

def api_rate_limit(max_attempts=60, window=60):
    """Rate-limit an API endpoint by remote IP.
    Default: 60 requests per 60 seconds. Shared across all workers via platform.db."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            allowed, retry_in = check_rate_limit(ip, f"api:{f.__name__}",
                                                 max_attempts, window)
            if not allowed:
                return jsonify({"ok": False,
                                "msg": f"Rate limit exceeded. Retry in {retry_in}s."}), 429
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Auth event logging ────────────────────────────────────────────────────────

def log_auth_event(event, username="", ip="", tenant="", detail=""):
    """Write a security event to the platform-level security_log table.
    Events: LOGIN_OK, LOGIN_FAIL, LOGOUT, PW_CHANGE, PW_RESET, RATE_LIMITED."""
    from app.platform import get_platform_db
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_platform_db()
    try:
        db.execute(
            "INSERT INTO security_log (ts, event, username, ip, tenant, detail) "
            "VALUES (?,?,?,?,?,?)",
            [now, event, username or "", ip or "", tenant or "", detail or ""])
        db.commit()
    finally:
        db.close()


# ── Permissions ───────────────────────────────────────────────────────────────

ALL_PERMISSIONS = [
    ("view_inventory",   "View Inventory",     "See the items list and search"),
    ("view_dashboard",   "View Dashboard",     "Access charts and financial summary"),
    ("view_audit",       "View Audit Log",     "See the full audit trail"),
    ("checkout_checkin", "Checkout / Check In","Check items in and out of inventory"),
    ("write_items",      "Add / Edit Items",   "Create new items and edit existing ones"),
    ("qty_adjust",       "Adjust Quantities",  "Add, remove or set stock quantities"),
    ("sell_items",       "Mark as Sold",       "Mark items as sold and record sale price"),
    ("delete_items",     "Delete Items",       "Permanently delete items"),
    ("import_export",    "Import / Export",    "Import Excel files and export CSV/Excel"),
]
PERM_KEYS            = [p[0] for p in ALL_PERMISSIONS]
ADMIN_DEFAULT_PERMS  = set(PERM_KEYS)
WORKER_DEFAULT_PERMS = {
    "view_inventory", "view_dashboard", "view_audit",
    "checkout_checkin", "write_items", "qty_adjust", "sell_items",
}

def get_user_perms(user_id=None, role=None, perm_str=None):
    if role == "admin":
        return ADMIN_DEFAULT_PERMS
    if not perm_str:
        return WORKER_DEFAULT_PERMS
    stored = set(perm_str.split(",")) if perm_str else set()
    return stored & set(PERM_KEYS)

def get_user_location_ids(user_id: int, role: str) -> list:
    """Return list of location IDs the user is allowed to see.
    Empty list means no restriction (see all). Admins always see all."""
    if role == "admin":
        return []
    rows = query("SELECT location_id FROM user_locations WHERE user_id=?", [user_id])
    return [r["location_id"] for r in rows]


def location_filter_sql(alias: str = "i") -> tuple:
    """Return (sql_fragment, args) to filter items by the current user's sites.
    Returns ('', []) if no restriction applies."""
    loc_ids = session.get("location_ids") or []
    if not loc_ids:
        return "", []
    placeholders = ",".join("?" * len(loc_ids))
    return f" AND {alias}.location_id IN ({placeholders})", list(loc_ids)


def has_perm(perm):
    role = session.get("role") or getattr(g, "api_user_role", None)
    if role == "admin":
        return True
    perms = session.get("permissions") or getattr(g, "api_user_permissions", "")
    return perm in set(perms.split(","))


# ── Auth decorators ───────────────────────────────────────────────────────────

def _auth_user_id():
    """Return current user ID from session or API key auth."""
    return session.get("user_id") or getattr(g, "api_user_id", None)

def _auth_role():
    """Return current user role from session or API key auth."""
    return session.get("role") or getattr(g, "api_user_role", None)

def _auth_perms():
    """Return current user permissions string from session or API key auth."""
    return session.get("permissions") or getattr(g, "api_user_permissions", "")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _auth_user_id():
            if request.path.startswith(("/api/", "/export", "/import")):
                return jsonify({"ok": False, "msg": "Not logged in"}), 401
            return redirect(url_for("auth.login_page"))
        return f(*args, **kwargs)
    return decorated

def perm_required(perm):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not _auth_user_id():
                if request.path.startswith(("/api/", "/export", "/import")):
                    return jsonify({"ok": False, "msg": "Not logged in"}), 401
                return redirect(url_for("auth.login_page"))
            if not has_perm(perm):
                if request.path.startswith(("/api/", "/export", "/import")):
                    return jsonify({"ok": False, "msg": f"Permission denied: {perm}"}), 403
                return redirect(url_for("main.inventory"))
            return f(*args, **kwargs)
        return decorated
    return decorator

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _auth_role() != "admin":
            if request.path.startswith(("/api/", "/export", "/import")):
                return jsonify({"ok": False, "msg": "Admin only"}), 403
            return redirect(url_for("main.inventory"))
        return f(*args, **kwargs)
    return decorated


# ── Audit logging ─────────────────────────────────────────────────────────────

def log_action(action, item_id=None, item_name=None, detail=None,
               before=None, after=None):
    username = session.get("username", "system")
    item_serial = item_sku = product_name = None
    if item_id:
        try:
            row = query("""SELECT i.serial, i.sku, i.internal_sku, p.name as pname
                           FROM items i LEFT JOIN products p ON p.id=i.product_id
                           WHERE i.id=?""", [item_id], one=True)
            if row:
                item_serial  = row["serial"] or None
                item_sku     = row["sku"] or row["internal_sku"] or None
                product_name = row["pname"] or None
        except Exception:
            pass
    execute(
        "INSERT INTO audit_log (ts,action,item_id,item_name,item_serial,item_sku,"
        "product_name,detail,before_state,after_state,username) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, item_id,
         item_name, item_serial, item_sku, product_name, detail,
         json.dumps(before) if before else None,
         json.dumps(after)  if after  else None,
         username])


# ── Low stock alerts ──────────────────────────────────────────────────────────

def get_low_stock_alerts():
    rows = query("""
        SELECT p.id, p.name, p.low_stock_threshold,
               COUNT(i.id) as available_count
        FROM products p
        LEFT JOIN items i ON i.product_id = p.id
            AND i.active = 1 AND i.sold = 0 AND i.checked_out = 0
        WHERE p.active = 1 AND p.low_stock_threshold > 0
        GROUP BY p.id
        HAVING available_count <= p.low_stock_threshold
        ORDER BY available_count ASC
    """)
    return [{"id": r["id"], "name": r["name"],
             "available_count": r["available_count"],
             "low_stock_threshold": r["low_stock_threshold"]}
            for r in rows]


# ── Item completeness ─────────────────────────────────────────────────────────

def _item_missing_fields(s):
    missing = []
    prod = query(
        "SELECT require_serial, require_vendor_sku FROM products WHERE id=?",
        [s.get("product_id")], one=True) if s.get("product_id") else None
    if prod:
        if prod["require_serial"]     and not s.get("serial"): missing.append("serial #")
        if prod["require_vendor_sku"] and not s.get("sku"):    missing.append("vendor SKU")
    if s.get("cost_price") is None: missing.append("cost")
    if not s.get("shelf"):          missing.append("shelf")
    return missing

def sync_item_task(item_id, item_name, missing_fields):
    existing = query("SELECT id, status FROM tasks WHERE item_id=?", [item_id], one=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if missing_fields:
        title = f"Complete item: {item_name}"
        notes = f"Missing: {', '.join(missing_fields)}"
        if existing:
            if existing["status"] == "done":
                execute("UPDATE tasks SET status='todo', notes=?, updated_at=? WHERE id=?",
                        [notes, now, existing["id"]])
        else:
            execute(
                "INSERT INTO tasks (title,urgency,status,notes,created_by,created_at,item_id) "
                "VALUES (?,?,?,?,?,?,?)",
                [title, "medium", "todo", notes, "system", now, item_id])
    else:
        if existing and existing["status"] != "done":
            execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                    [now, existing["id"]])


# ── Report helpers ────────────────────────────────────────────────────────────

def _parse_date_range(req):
    from datetime import timedelta
    from datetime import date as _date
    period = req.args.get("period", "")
    today  = _date.today()
    if period == "today":
        return today.isoformat(), today.isoformat()
    elif period == "week":
        return (today - timedelta(days=today.weekday())).isoformat(), today.isoformat()
    elif period == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    elif period == "quarter":
        q_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_month, day=1).isoformat(), today.isoformat()
    elif period == "year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    elif period == "last30":
        return (today - timedelta(days=30)).isoformat(), today.isoformat()
    elif period == "custom":
        return req.args.get("from", ""), req.args.get("to", "")
    return None, None

def _date_filter_sql(col, date_from, date_to):
    if date_from and date_to:
        return f" AND date({col}) >= ? AND date({col}) <= ?", [date_from, date_to]
    elif date_from:
        return f" AND date({col}) >= ?", [date_from]
    elif date_to:
        return f" AND date({col}) <= ?", [date_to]
    return "", []
