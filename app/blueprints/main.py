import json
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify

from app.db import query
from app.helpers import (login_required, perm_required, admin_required,
                         get_low_stock_alerts, ALL_PERMISSIONS, PERM_KEYS)

bp = Blueprint("main", __name__)


# ── Health check (no auth, no tenant required) ────────────────────────────────

@bp.route("/_health")
def health():
    return jsonify({"ok": True, "service": "countdepot"}), 200


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
        alerts       = get_low_stock_alerts(),
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
    from app.helpers import location_filter_sql
    loc = query("SELECT * FROM locations WHERE id=?", [loc_id], one=True)
    if not loc:
        from flask import abort
        abort(404)
    # Enforce site access for restricted users
    loc_ids = session.get("location_ids") or []
    if loc_ids and loc_id not in loc_ids:
        from flask import abort
        abort(403)
    return render_template("site_detail.html", location=dict(loc))


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
        "SELECT id,username,role,permissions FROM users ORDER BY role,username")]
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


# ── Low stock API (lives here because it's tightly coupled to the page) ───────

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


@bp.route("/importer")
@login_required
@perm_required("import_export")
def importer_page():
    locations = query("SELECT id, name FROM locations ORDER BY name")
    return render_template("importer.html", locations=locations)

@bp.route("/invoice-import")
@login_required
def invoice_import_redirect():
    from flask import redirect
    return redirect("/importer")
