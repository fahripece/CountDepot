import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt as _bcrypt
from flask import session, request, jsonify, redirect, url_for, g

from app.db import query, execute


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_pw(pw):
    """Hash a password with bcrypt."""
    return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()

def validate_password(pw, current_hash=None):
    """Returns an error string if the password fails complexity rules, else None.
    Rules: 10+ chars, at least one uppercase letter, at least one digit or symbol.
    Pass current_hash to also reject reuse of the current password."""
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r'[A-Z]', pw):
        return "Password must contain at least one uppercase letter."
    if not re.search(r'[0-9!@#$%^&*()\-_=+\[\]{}|;:,.<>?/\\\'"`~]', pw):
        return "Password must contain at least one number or special character."
    if current_hash and verify_pw(pw, current_hash):
        return "New password must be different from your current password."
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


# ── Per-account lockout (username-level, separate from IP rate limit) ─────────
# After 5 consecutive failures for a specific email address, lock that account
# for 15 minutes regardless of which IP the next attempt comes from.

def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


LOCKOUT_MAX    = 5    # failures before lockout
LOCKOUT_WINDOW = 900  # 15 minutes

def check_account_lockout(username: str):
    """Returns (allowed: bool, locked_until_str: str | None).
    Uses the login_log in the current tenant DB (already open in g)."""
    # Count consecutive failures in the last LOCKOUT_WINDOW seconds
    cutoff_ts = _utc_now().timestamp() - LOCKOUT_WINDOW
    cutoff_s  = datetime.fromtimestamp(cutoff_ts, timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        row = query(
            "SELECT COUNT(*) FROM login_log "
            "WHERE LOWER(username)=LOWER(?) AND result='fail' AND ts>=?",
            [username, cutoff_s], one=True)
        failures = row[0] if row else 0
        if failures >= LOCKOUT_MAX:
            # Compute when lockout expires: oldest failure in window + LOCKOUT_WINDOW
            oldest = query(
                "SELECT ts FROM login_log "
                "WHERE LOWER(username)=LOWER(?) AND result='fail' AND ts>=? "
                "ORDER BY ts ASC LIMIT 1", [username, cutoff_s], one=True)
            if oldest:
                oldest_ts  = datetime.strptime(oldest[0], "%Y-%m-%d %H:%M:%S").timestamp()
                unlock_ts  = oldest_ts + LOCKOUT_WINDOW
                unlock_str = datetime.fromtimestamp(unlock_ts, timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
                return False, unlock_str
        return True, None
    except Exception:
        return True, None  # fail open — don't lock out due to DB errors


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
    ("view_docs",        "View Docs / SOPs",   "Read company procedures and shared documents"),
    ("write_docs",       "Create / Edit Docs", "Create and update company procedures"),
    ("delete_docs",      "Delete Docs",        "Remove company procedures"),
]
PERM_KEYS            = [p[0] for p in ALL_PERMISSIONS]
ADMIN_DEFAULT_PERMS  = set(PERM_KEYS)
WORKER_DEFAULT_PERMS = {
    "view_inventory", "view_dashboard", "view_audit",
    "checkout_checkin", "write_items", "qty_adjust", "sell_items",
    "view_docs",
}
VIEWER_DEFAULT_PERMS = {"view_inventory", "view_docs"}

def get_user_perms(user_id=None, role=None, perm_str=None):
    if role == "admin":
        return ADMIN_DEFAULT_PERMS
    if role == "viewer":
        return VIEWER_DEFAULT_PERMS
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
    Returns ('', []) if no restriction applies.
    Items with no location (NULL) are always visible to everyone."""
    loc_ids = session.get("location_ids") or []
    if not loc_ids:
        return "", []
    placeholders = ",".join("?" * len(loc_ids))
    return f" AND ({alias}.location_id IN ({placeholders}) OR {alias}.location_id IS NULL)", list(loc_ids)


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


# ── Low stock notification ────────────────────────────────────────────────────

def notify_low_stock_if_needed(product_id):
    """Check if a product is below threshold and send an alert email if so.
    Silently no-ops if SMTP is not configured, product has no threshold, or an
    alert was already sent within the last 24 hours. Never raises."""
    if not product_id:
        return
    from config import Config
    if not Config.SMTP_HOST:
        return
    try:
        product = query("""
            SELECT p.id, p.name, p.low_stock_threshold, p.low_stock_last_alerted,
                   COUNT(i.id) AS available_count
            FROM products p
            LEFT JOIN items i ON i.product_id = p.id
                AND i.active = 1 AND i.sold = 0 AND i.checked_out = 0
            WHERE p.id = ? AND p.active = 1 AND p.low_stock_threshold > 0
            GROUP BY p.id
            HAVING available_count <= p.low_stock_threshold
        """, [product_id], one=True)
        if not product:
            return
        last = product["low_stock_last_alerted"]
        if last:
            last_dt = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
            if _utc_now() - last_dt < timedelta(hours=24):
                return
        # Prefer site-specific alert emails over global settings
        site_rows = query("""
            SELECT DISTINCT l.email
            FROM items i
            JOIN locations l ON l.id = i.location_id
            WHERE i.product_id = ? AND i.active = 1
              AND l.email IS NOT NULL AND l.email != ''
        """, [product_id])
        emails = [r["email"] for r in site_rows if r.get("email")]
        if not emails:
            # Global alert emails from settings (comma-separated)
            setting = query("SELECT value FROM settings WHERE key='low_stock_alert_email'", one=True)
            raw = (setting["value"] if setting else "") or ""
            emails = [e.strip() for e in raw.split(",") if e.strip()]
            # Users with low_stock_alerts flag set
            user_rows = query(
                "SELECT email FROM users WHERE COALESCE(low_stock_alerts,0)=1 "
                "AND email IS NOT NULL AND email != '' AND active=1")
            for r in user_rows:
                if r["email"] not in emails:
                    emails.append(r["email"])
        if not emails:
            # Final fallback: all admin users
            admins = query(
                "SELECT email FROM users WHERE role='admin' AND email IS NOT NULL AND email != '' AND active=1")
            emails = [r["email"] for r in admins if r.get("email")]
        if not emails:
            return
        now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
        execute("UPDATE products SET low_stock_last_alerted=? WHERE id=?", [now, product_id])
        from app.mailer import send_low_stock_alert
        for email in emails:
            send_low_stock_alert(email, product["name"],
                                 product["available_count"], product["low_stock_threshold"])
        try:
            from app.messenger import notify as _notify
            _notify("low_stock",
                    f"Low Stock: {product['name']}",
                    f"Only {product['available_count']} available (threshold {product['low_stock_threshold']})",
                    "/inventory")
        except Exception:
            pass
        try:
            push_notification(
                "low_stock",
                f"Low stock: {product['name']}",
                f"Only {product['available_count']} available (threshold {product['low_stock_threshold']})",
                "/inventory")
        except Exception:
            pass
    except Exception:
        pass  # never crash the request


# ── Item completeness ─────────────────────────────────────────────────────────

def incomplete_cost_required():
    row = query("SELECT value FROM settings WHERE key='incomplete_requires_cost'", one=True)
    return not row or str(row["value"]) != "0"


def _extra_fields_dict(raw):
    if isinstance(raw, dict):
        return raw
    for _ in range(2):
        if not isinstance(raw, str):
            break
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def _value_missing(value, field_type="text"):
    if field_type == "checkbox":
        return value not in (True, 1, "1", "true", "True", "yes", "on")
    return value is None or str(value).strip() == ""


def required_category_field_missing(s):
    cat_id = s.get("category_id")
    if not cat_id:
        return []
    rows = query(
        "SELECT field_label, field_key, field_type FROM category_fields "
        "WHERE category_id=? AND required=1 ORDER BY sort_order, id",
        [cat_id])
    extra = _extra_fields_dict(s.get("extra_fields"))
    missing = []
    for row in rows:
        value = extra.get(row["field_key"])
        if _value_missing(value, row["field_type"] or "text"):
            missing.append(row["field_label"])
    return missing


def _item_missing_fields(s, cost_required=None):
    if cost_required is None:
        cost_required = incomplete_cost_required()
    missing = []
    prod = query(
        "SELECT require_serial, require_vendor_sku FROM products WHERE id=?",
        [s.get("product_id")], one=True) if s.get("product_id") else None
    if prod:
        if prod["require_serial"]     and not s.get("serial"): missing.append("serial #")
        if prod["require_vendor_sku"] and not s.get("sku"):    missing.append("vendor SKU")
    missing.extend(required_category_field_missing(s))
    if cost_required and s.get("cost_price") is None: missing.append("cost")
    if not s.get("shelf"):          missing.append("shelf")
    return missing

def sync_maintenance_tasks(item_id):
    """Create, update, or close maintenance/calibration tasks based on dates in extra_fields.
    Runs after any item save. Never raises."""
    try:
        item = query("SELECT name, extra_fields FROM items WHERE id=? AND active=1",
                     [item_id], one=True)
        if not item:
            return
        from datetime import date as _date, timedelta
        today = _date.today()
        warn_days = 30
        try:
            ef = json.loads(item["extra_fields"] or "{}")
        except Exception:
            ef = {}

        checks = [
            ("service",     ef.get("next_service"),    "Service due"),
            ("calibration", ef.get("calibration_due"), "Calibration due"),
        ]
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for task_key, date_str, label in checks:
            existing = query(
                "SELECT id, status FROM tasks WHERE item_id=? AND task_key=?",
                [item_id, task_key], one=True)

            if not date_str:
                # No date set — close any open task
                if existing and existing["status"] != "done":
                    execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                            [now_ts, existing["id"]])
                continue

            try:
                due = _date.fromisoformat(date_str[:10])
            except Exception:
                continue

            days_until = (due - today).days
            if days_until < 0:
                urgency = "high"
                detail  = f"{label}: {item['name']} — OVERDUE since {date_str[:10]}"
            elif days_until <= warn_days:
                urgency = "high" if days_until <= 7 else "medium"
                detail  = f"{label}: {item['name']} — due {date_str[:10]} ({days_until}d)"
            else:
                # Due date is far out — close any open task
                if existing and existing["status"] != "done":
                    execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                            [now_ts, existing["id"]])
                continue

            if existing:
                execute(
                    "UPDATE tasks SET title=?, urgency=?, notes=?, status='todo', updated_at=? WHERE id=?",
                    [detail, urgency, date_str[:10], now_ts, existing["id"]])
            else:
                execute(
                    "INSERT INTO tasks (title,urgency,status,notes,created_by,created_at,item_id,task_key) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    [detail, urgency, "todo", date_str[:10], "system", now_ts, item_id, task_key])
    except Exception:
        pass


def sync_item_task(item_id, item_name, missing_fields):
    existing = query("SELECT id, status FROM tasks WHERE item_id=?", [item_id], one=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if missing_fields:
        title = f"Complete item: {item_name}"
        notes = f"Missing: {', '.join(missing_fields)}"
        if existing:
            execute("UPDATE tasks SET status='todo', title=?, notes=?, updated_at=? WHERE id=?",
                    [title, notes, now, existing["id"]])
        else:
            execute(
                "INSERT INTO tasks (title,urgency,status,notes,created_by,created_at,item_id) "
                "VALUES (?,?,?,?,?,?,?)",
                [title, "medium", "todo", notes, "system", now, item_id])
    else:
        if existing and existing["status"] != "done":
            execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                    [now, existing["id"]])


# ── Plan limits ───────────────────────────────────────────────────────────────

PLAN_LIMITS = {
    "free":     {"items": 100, "users": 1,  "label": "Free"},
    "starter":  {"items": 500, "users": 3,  "label": "Starter"},
    "standard": {"items": None, "users": None, "label": "Standard"},
    "trial":    {"items": None, "users": None, "label": "Trial"},
    "enterprise": {"items": None, "users": None, "label": "Enterprise"},
}


def get_plan_limits(plan):
    """Return limit dict for a plan name. Unknown plans get standard (unlimited)."""
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["standard"])


def check_plan_item_limit(plan):
    """Return (allowed, current_count, limit) for item creation.
    allowed=True means adding one more item is within the plan limit."""
    limits = get_plan_limits(plan)
    max_items = limits["items"]
    if max_items is None:
        return True, None, None
    current = query("SELECT COUNT(*) AS c FROM items WHERE COALESCE(active,1)=1 AND COALESCE(sold,0)=0", one=True)
    count = current["c"] if current else 0
    return count < max_items, count, max_items


def check_plan_user_limit(plan):
    """Return (allowed, current_count, limit) for user creation."""
    limits = get_plan_limits(plan)
    max_users = limits["users"]
    if max_users is None:
        return True, None, None
    current = query("SELECT COUNT(*) AS c FROM users WHERE COALESCE(active,1)=1", one=True)
    count = current["c"] if current else 0
    return count < max_users, count, max_users


# ── Notifications ─────────────────────────────────────────────────────────────

def push_notification(event_type, title, body=None, link=None, user_id=None):
    """Insert a notification row for a specific user (or user_id=None for all admins)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if user_id is not None:
        execute(
            "INSERT INTO notifications (user_id,event_type,title,body,link,read,created_at) "
            "VALUES (?,?,?,?,?,0,?)",
            [user_id, event_type, title, body, link, now])
    else:
        # Fan out to all admin users
        admins = query("SELECT id FROM users WHERE role='admin'")
        for admin in admins:
            execute(
                "INSERT INTO notifications (user_id,event_type,title,body,link,read,created_at) "
                "VALUES (?,?,?,?,?,0,?)",
                [admin["id"], event_type, title, body, link, now])


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
