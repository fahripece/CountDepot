import json
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify

from datetime import datetime

from app.db import query, execute
from app.helpers import (login_required, perm_required, admin_required,
                         log_action, ALL_PERMISSIONS, PERM_KEYS)

bp = Blueprint("main", __name__)


# ── Health check (no auth, no tenant required) ────────────────────────────────

@bp.route("/_health")
def health():
    return jsonify({"ok": True, "service": "countdepot"}), 200


@bp.route("/status")
def status_page():
    """Public uptime/status page — no auth required."""
    from app.platform import get_platform_db
    import time, os
    checks = []
    overall = "operational"

    # Platform DB check
    try:
        t0 = time.monotonic()
        pdb = get_platform_db()
        pdb.execute("SELECT 1").fetchone()
        pdb.close()
        ms = int((time.monotonic() - t0) * 1000)
        checks.append({"name": "Platform Database", "status": "ok", "latency_ms": ms})
    except Exception as e:
        checks.append({"name": "Platform Database", "status": "error", "detail": str(e)})
        overall = "degraded"

    # Tenant DB check (current tenant if available)
    try:
        from flask import g
        if hasattr(g, "tenant_slug") and g.tenant_slug:
            t0 = time.monotonic()
            query("SELECT 1")
            ms = int((time.monotonic() - t0) * 1000)
            checks.append({"name": "Tenant Database", "status": "ok", "latency_ms": ms})
    except Exception as e:
        checks.append({"name": "Tenant Database", "status": "error", "detail": str(e)})
        overall = "degraded"

    # Disk space check
    try:
        import shutil
        from config import Config as _Cfg
        usage = shutil.disk_usage(_Cfg.TENANTS_DIR)
        free_pct = usage.free / usage.total * 100
        status = "ok" if free_pct > 10 else ("warn" if free_pct > 5 else "error")
        if status == "error":
            overall = "degraded"
        checks.append({"name": "Disk Space", "status": status,
                        "detail": f"{free_pct:.0f}% free ({usage.free // (1024**3)} GB)"})
    except Exception as e:
        checks.append({"name": "Disk Space", "status": "unknown", "detail": str(e)})

    return render_template("status.html",
                           checks=checks,
                           overall=overall,
                           generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))


# ── Shared helpers ────────────────────────────────────────────────────────────

def _cat_fields():
    """Build the category_fields dict used by inventory/add-item/products pages."""
    all_cat_fields = {}
    for row in query("SELECT * FROM category_fields ORDER BY category_id, sort_order, id"):
        cid = str(row["category_id"])
        f = dict(row)
        if f.get("dropdown_options"):
            try:
                f["dropdown_options"] = json.loads(f["dropdown_options"])
            except Exception:
                f["dropdown_options"] = []
        all_cat_fields.setdefault(cid, []).append(f)
    return all_cat_fields

def _products_list():
    """Product list for the product picker dropdown."""
    return [dict(r) for r in query(
        "SELECT id,name,manufacturer,model,category_id,serial_tracked,qty_tracked,"
        "require_scan_checkout,require_serial,require_vendor_sku,require_internal_sku,"
        "print_scan_label,require_sku_label,default_cost,default_sale "
        "FROM products WHERE active=1 ORDER BY name")]


# ── Page routes ───────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
def inventory():
    return render_template("inventory.html",
        categories   = query("SELECT * FROM categories ORDER BY name"),
        companies    = [dict(r) for r in query("SELECT * FROM companies WHERE active=1 ORDER BY name")],
        distributors = [r["name"] for r in query("SELECT name FROM distributors ORDER BY name")],
        category_fields = _cat_fields(),
        products     = _products_list(),
        user_perms   = list(session.get("permissions", "").split(",")))


@bp.route("/add-item")
@login_required
@perm_required("write_items")
def add_item_page():
    return render_template("add_item.html",
        categories   = query("SELECT * FROM categories ORDER BY name"),
        companies    = [dict(r) for r in query("SELECT * FROM companies WHERE active=1 ORDER BY name")],
        distributors = [r["name"] for r in query("SELECT name FROM distributors ORDER BY name")],
        category_fields = _cat_fields(),
        products     = _products_list())


@bp.route("/checkout")
@login_required
def checkout_page():
    return render_template("checkout.html")


@bp.route("/categories")
@login_required
def categories_page():
    cats = [dict(r) for r in query("SELECT * FROM categories ORDER BY name")]
    for cat in cats:
        cat["item_count"] = query(
            "SELECT COUNT(*) FROM items WHERE category_id=? AND active=1",
            [cat["id"]], one=True)[0]
        cat["product_count"] = query(
            "SELECT COUNT(*) FROM products WHERE category_id=? AND active=1",
            [cat["id"]], one=True)[0]
        cat["fields"] = []
        for f in query("SELECT * FROM category_fields WHERE category_id=? ORDER BY sort_order,id",
                       [cat["id"]]):
            fd = dict(f)
            if fd.get("dropdown_options"):
                try:
                    fd["dropdown_options"] = json.loads(fd["dropdown_options"])
                except Exception:
                    fd["dropdown_options"] = []
            cat["fields"].append(fd)
    return render_template("categories.html", categories=cats)


@bp.route("/checked-out")
@login_required
def checked_out_page():
    return render_template("sold_items.html",
        categories=query("SELECT * FROM categories ORDER BY name"))


@bp.route("/sites")
@login_required
def sites_page():
    return render_template("sites.html")


@bp.route("/sites/<int:loc_id>")
@login_required
def site_detail_page(loc_id):
    loc = query("SELECT * FROM locations WHERE id=?", [loc_id], one=True)
    if not loc:
        from flask import abort
        abort(404)
    # Enforce site access for restricted users
    loc_ids = session.get("location_ids") or []
    if loc_ids and loc_id not in loc_ids:
        from flask import abort
        abort(403)
    return render_template("inventory.html",
        categories      = query("SELECT * FROM categories ORDER BY name"),
        companies       = [dict(r) for r in query("SELECT * FROM companies WHERE active=1 ORDER BY name")],
        distributors    = [r["name"] for r in query("SELECT name FROM distributors ORDER BY name")],
        category_fields = _cat_fields(),
        products        = _products_list(),
        user_perms      = list(session.get("permissions", "").split(",")),
        site_locked     = dict(loc))


@bp.route("/low-stock")
@login_required
def low_stock_page():
    return render_template("low_stock.html",
        categories=query("SELECT * FROM categories ORDER BY name"))


@bp.route("/audit")
@login_required
@perm_required("view_audit")
def audit():
    return render_template("audit.html")


@bp.route("/dashboard")
@login_required
@perm_required("view_dashboard")
def dashboard():
    return redirect(url_for("main.report_financial"))


@bp.route("/admin")
@login_required
@admin_required
def admin_page():
    users = [dict(r) for r in query(
        "SELECT id,username,role,permissions,email FROM users ORDER BY role,username")]
    for u in users:
        u["perm_set"] = set(u["permissions"].split(",")) if u["permissions"] else set()
    return render_template("admin.html", users=users,
                           all_permissions=ALL_PERMISSIONS, perm_keys=PERM_KEYS,
                           current_user_id=session.get("user_id"))


@bp.route("/contacts")
@login_required
def contacts_page():
    return render_template("contacts.html",
        companies    = [dict(r) for r in query("SELECT * FROM companies WHERE active=1 ORDER BY name")],
        distributors = [dict(r) for r in query("SELECT * FROM distributors ORDER BY name")],
        people       = [dict(r) for r in query("SELECT * FROM contacts WHERE active=1 ORDER BY name")])


@bp.route("/todo")
@login_required
def todo_page():
    return render_template("todo.html")


@bp.route("/products")
@login_required
def products_page():
    return render_template("products.html",
        categories      = [dict(r) for r in query("SELECT * FROM categories ORDER BY name")],
        category_fields = _cat_fields())


# ── Report pages ──────────────────────────────────────────────────────────────

@bp.route("/report/financial")
@login_required
@perm_required("view_dashboard")
def report_financial():
    return render_template("report_financial.html")


@bp.route("/report/inventory")
@login_required
@perm_required("view_inventory")
def report_inventory():
    return render_template("report_inventory.html")


@bp.route("/report/activity")
@login_required
@perm_required("view_audit")
def report_activity():
    return render_template("report_activity.html")


@bp.route("/report/user-activity")
@login_required
@perm_required("view_audit")
def report_user_activity():
    users = query("SELECT id, username, email, role FROM users ORDER BY username")
    return render_template("report_user_activity.html", users=users)


@bp.route("/report/checkout-history")
@login_required
@perm_required("view_audit")
def report_checkout_history():
    return render_template("report_checkout_history.html")


@bp.route("/report/import-history")
@login_required
@admin_required
def report_import_history():
    return render_template("report_import_history.html")


@bp.route("/report/locations")
@login_required
@perm_required("view_inventory")
def report_locations():
    return render_template("report_locations.html")


@bp.route("/kits")
@login_required
@perm_required("view_inventory")
def kits_page():
    return render_template("kits.html")


@bp.route("/report/warranties")
@login_required
@perm_required("view_inventory")
def report_warranties():
    return render_template("report_warranties.html")


@bp.route("/report/depreciation")
@login_required
@perm_required("view_inventory")
def report_depreciation():
    return render_template("report_depreciation.html")


@bp.route("/report/login-activity")
@login_required
@admin_required
def report_login_activity():
    return render_template("report_login_activity.html")


@bp.route("/integrations/accounting")
@login_required
@admin_required
def integrations_accounting():
    return render_template("integrations_accounting.html")


@bp.route("/integrations/accounting/qb/callback")
@login_required
@admin_required
def qb_oauth_callback():
    """QuickBooks OAuth2 callback — exchange code for tokens."""
    from app.accounting import (qb_exchange_code, _get_setting, _set_setting)
    code  = request.args.get("code", "")
    realm = request.args.get("realmId", "")
    error = request.args.get("error", "")
    if error or not code:
        return render_template("integrations_accounting.html",
                               flash_error=f"QuickBooks authorization failed: {error or 'No code received'}")
    try:
        redirect_uri = request.host_url.rstrip("/") + "/integrations/accounting/qb/callback"
        tokens = qb_exchange_code(
            _get_setting("qb_client_id", ""),
            _get_setting("qb_client_secret", ""),
            code, redirect_uri)
        from datetime import datetime, timedelta
        expiry = (datetime.utcnow() + timedelta(seconds=tokens["expires_in"])).isoformat()
        _set_setting("qb_access_token",  tokens["access_token"])
        _set_setting("qb_refresh_token", tokens["refresh_token"])
        _set_setting("qb_token_expiry",  expiry)
        _set_setting("qb_realm_id",      realm)
    except Exception as e:
        return render_template("integrations_accounting.html",
                               flash_error=f"QuickBooks token exchange failed: {e}")
    return redirect(url_for("main.integrations_accounting") + "?connected=qb")


@bp.route("/integrations/accounting/xero/callback")
@login_required
@admin_required
def xero_oauth_callback():
    """Xero OAuth2 callback — exchange code for tokens and fetch tenant ID."""
    from app.accounting import (xero_exchange_code, xero_get_tenants, _get_setting, _set_setting)
    code  = request.args.get("code", "")
    error = request.args.get("error", "")
    if error or not code:
        return render_template("integrations_accounting.html",
                               flash_error=f"Xero authorization failed: {error or 'No code received'}")
    try:
        redirect_uri = request.host_url.rstrip("/") + "/integrations/accounting/xero/callback"
        tokens = xero_exchange_code(
            _get_setting("xero_client_id", ""),
            _get_setting("xero_client_secret", ""),
            code, redirect_uri)
        from datetime import datetime, timedelta
        expiry = (datetime.utcnow() + timedelta(seconds=tokens["expires_in"])).isoformat()
        _set_setting("xero_access_token",  tokens["access_token"])
        _set_setting("xero_refresh_token", tokens["refresh_token"])
        _set_setting("xero_token_expiry",  expiry)
        # Fetch Xero tenant ID
        tenants = xero_get_tenants(tokens["access_token"])
        if tenants:
            _set_setting("xero_tenant_id", tenants[0]["tenantId"])
    except Exception as e:
        return render_template("integrations_accounting.html",
                               flash_error=f"Xero token exchange failed: {e}")
    return redirect(url_for("main.integrations_accounting") + "?connected=xero")


# ── Procurement helpers ───────────────────────────────────────────────────────

def _po_number():
    year   = datetime.now().strftime("%Y")
    prefix = f"PO-{year}-"
    existing = query("SELECT po_number FROM purchase_orders WHERE po_number LIKE ?", [f"{prefix}%"])
    nums = []
    for r in existing:
        try:
            nums.append(int(r["po_number"].split("-")[-1]))
        except Exception:
            pass
    return f"{prefix}{((max(nums) + 1) if nums else 1):04d}"


def _recalc_po_total(po_id):
    row = query("SELECT SUM(total_cost) as t FROM po_lines WHERE po_id=?", [po_id], one=True)
    total = row["t"] or 0
    execute("UPDATE purchase_orders SET total_cost=? WHERE id=?", [total, po_id])
    return total


# ── Procurement pages ─────────────────────────────────────────────────────────

@bp.route("/procurement")
@login_required
@admin_required
def procurement_page():
    vendors = [dict(r) for r in query("SELECT id, name FROM distributors ORDER BY name")]
    sites   = [dict(r) for r in query("SELECT id, name FROM locations WHERE active=1 ORDER BY name")]
    products = _products_list()
    return render_template("procurement.html",
                           vendors=vendors, sites=sites, products=products)


@bp.route("/procurement/catalog")
@login_required
@admin_required
def procurement_catalog_page():
    vendors  = [dict(r) for r in query("SELECT id, name FROM distributors ORDER BY name")]
    products = _products_list()
    return render_template("procurement_catalog.html", vendors=vendors, products=products)


@bp.route("/procurement/<int:po_id>")
@login_required
@admin_required
def procurement_detail_page(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        from flask import abort
        abort(404)
    vendors  = [dict(r) for r in query("SELECT id, name FROM distributors ORDER BY name")]
    sites    = [dict(r) for r in query("SELECT id, name FROM locations WHERE active=1 ORDER BY name")]
    products = _products_list()
    return render_template("procurement_detail.html",
                           po=dict(po), vendors=vendors, sites=sites, products=products)


@bp.route("/procurement/analytics")
@login_required
@admin_required
def procurement_analytics_page():
    return render_template("procurement_analytics.html")


@bp.route("/integrations")
@login_required
@admin_required
def integrations_page():
    return render_template("integrations.html")


# ── Low stock API (lives here because it's tightly coupled to the page) ───────

@bp.route("/api/procurement/low-stock-orderables")
@login_required
@admin_required
def api_low_stock_orderables():
    """Products below threshold that have a preferred vendor set."""
    rows = query("""
        SELECT p.id as product_id, p.name as product_name,
               p.manufacturer, p.model, p.low_stock_threshold,
               COUNT(i.id) as available_count,
               pv.vendor_id, d.name as vendor_name,
               pv.vendor_sku, pv.unit_price,
               vc.min_order_qty, vc.lead_days
        FROM products p
        LEFT JOIN items i ON i.product_id = p.id
            AND i.active=1 AND i.sold=0 AND i.checked_out=0
        JOIN product_vendors pv ON pv.product_id = p.id AND pv.preferred=1 AND pv.active=1
        JOIN distributors d ON d.id = pv.vendor_id
        LEFT JOIN vendor_catalog vc ON vc.vendor_id = pv.vendor_id
            AND vc.vendor_sku = pv.vendor_sku AND vc.active=1
        WHERE p.active=1 AND p.low_stock_threshold > 0
        GROUP BY p.id
        HAVING COUNT(i.id) < p.low_stock_threshold
        ORDER BY COUNT(i.id) ASC, p.name
    """)
    result = []
    for r in rows:
        d = dict(r)
        avail  = d["available_count"]
        thresh = d["low_stock_threshold"]
        moq    = d["min_order_qty"] or 1
        needed = thresh - avail
        # round up to nearest MOQ
        suggested = max(moq, (needed + moq - 1) // moq * moq)
        d["suggested_qty"] = suggested
        d["missing"]       = needed
        result.append(d)
    return jsonify({"ok": True, "orderables": result})


@bp.route("/api/procurement/pos/from-low-stock", methods=["POST"])
@login_required
@admin_required
def api_po_from_low_stock():
    """Create draft PO(s) from selected low-stock products, grouped by vendor."""
    d = request.json or {}
    lines = d.get("lines", [])  # [{product_id, vendor_id, description, qty_ordered, unit_cost, vendor_sku}]
    if not lines:
        return jsonify({"ok": False, "msg": "No lines provided"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Group by vendor_id
    by_vendor = {}
    for line in lines:
        vid = int(line.get("vendor_id", 0))
        if not vid:
            continue
        by_vendor.setdefault(vid, []).append(line)

    if not by_vendor:
        return jsonify({"ok": False, "msg": "No valid vendor assignments"}), 400

    created_pos = []
    for vendor_id, vendor_lines in by_vendor.items():
        vendor = query("SELECT name FROM distributors WHERE id=?", [vendor_id], one=True)
        vendor_name = vendor["name"] if vendor else None
        po_number = _po_number()
        po_id = execute("""
            INSERT INTO purchase_orders
              (po_number, vendor_id, vendor_name, status, created_by, created_at, total_cost)
            VALUES (?,?,?,'draft',?,?,0)
        """, [po_number, vendor_id, vendor_name, session.get("username"), now])

        for line in vendor_lines:
            qty    = int(line.get("qty_ordered") or 1)
            cost   = float(line.get("unit_cost") or 0)
            total  = round(qty * cost, 4)
            execute("""
                INSERT INTO po_lines
                  (po_id, product_id, description, vendor_sku, qty_ordered, qty_received, unit_cost, total_cost)
                VALUES (?,?,?,?,?,0,?,?)
            """, [po_id, line.get("product_id"), line.get("description", ""),
                  line.get("vendor_sku") or None, qty, cost, total])

        _recalc_po_total(po_id)
        log_action("PO_CREATE", None, po_number,
                   f"Auto-drafted from low stock: {len(vendor_lines)} lines, vendor={vendor_name}")
        created_pos.append({"id": po_id, "po_number": po_number, "vendor_name": vendor_name})

    return jsonify({"ok": True, "pos": created_pos})


@bp.route("/api/low-stock")
@login_required
def api_low_stock():
    rows = query("""
        SELECT p.id, p.name, p.manufacturer, p.model, p.description,
               p.category_id, p.low_stock_threshold,
               c.name as category_name, c.color as category_color,
               COUNT(i.id) as available_count
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN items i ON i.product_id = p.id
            AND i.active = 1 AND i.sold = 0 AND i.checked_out = 0
        WHERE p.active = 1 AND p.low_stock_threshold > 0
        GROUP BY p.id
        ORDER BY
            CASE WHEN COUNT(i.id) = 0 THEN 0
                 WHEN COUNT(i.id) <= p.low_stock_threshold THEN 1
                 WHEN COUNT(i.id) <= ROUND(p.low_stock_threshold * 1.5) THEN 2
                 ELSE 3 END, p.name
    """)
    result = []
    for r in rows:
        d = dict(r)
        avail, thresh = d["available_count"], d["low_stock_threshold"]
        d["status"]  = "red" if avail <= thresh else ("yellow" if avail <= round(thresh * 1.5) else "green")
        d["missing"] = max(0, thresh - avail)
        result.append(d)
    return jsonify(result)


@bp.route("/support")
@login_required
def support_page():
    return render_template("support.html")


@bp.route("/importer")
@login_required
@perm_required("import_export")
def importer_page():
    locations  = query("SELECT id, name FROM locations ORDER BY name")
    categories = query("SELECT id, name, color FROM categories ORDER BY name")
    return render_template("importer.html", locations=locations, categories=categories)

@bp.route("/invoice-import")
@login_required
def invoice_import_redirect():
    from flask import redirect
    return redirect("/importer")
