import hashlib
import hmac
import json
import time
import threading
from datetime import datetime
from functools import wraps

from flask import session, request, jsonify, redirect, url_for, g

from app.db import query, execute


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_pw(pw):
    return hmac.new(b"storelax-salt", pw.encode(), hashlib.sha256).hexdigest()


# ── Login rate limiter (in-memory, per IP) ────────────────────────────────────
# Note: this is per-worker — in a multi-worker gunicorn setup each worker has
# its own counter. An attacker hitting different workers gets LOGIN_MAX attempts
# per worker. Acceptable for now; replace with Redis if stricter limits needed.

_login_attempts = {}
_login_lock     = threading.Lock()
LOGIN_WINDOW    = 300   # seconds
LOGIN_MAX       = 10    # attempts per window, per worker

def check_login_rate(ip):
    now = time.time()
    with _login_lock:
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW]
        _login_attempts[ip] = attempts
        if len(attempts) >= LOGIN_MAX:
            return False, int(LOGIN_WINDOW - (now - attempts[0]))
        _login_attempts[ip] = attempts + [now]
        return True, 0

def clear_login_rate(ip):
    with _login_lock:
        _login_attempts.pop(ip, None)


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

def has_perm(perm):
    if session.get("role") == "admin":
        return True
    return perm in set(session.get("permissions", "").split(","))


# ── Auth decorators ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith(("/api/", "/export", "/import")):
                return jsonify({"ok": False, "msg": "Not logged in"}), 401
            return redirect(url_for("auth.login_page"))
        return f(*args, **kwargs)
    return decorated

def perm_required(perm):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user_id"):
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
        if session.get("role") != "admin":
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
