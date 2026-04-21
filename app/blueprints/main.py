import html
import json
import re
from flask import (Blueprint, render_template, request, session, redirect, url_for,
                   jsonify, current_app, make_response, send_from_directory)

from datetime import datetime, timezone

from app.db import query, execute
from app.helpers import (login_required, perm_required, admin_required,
                         check_rate_limit, log_action, ALL_PERMISSIONS, PERM_KEYS)
from app.seo_pages import seo_page_for_slug, seo_slugs

bp = Blueprint("main", __name__)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Health check (no auth, no tenant required) ────────────────────────────────

@bp.route("/_health")
def health():
    import shutil
    import time
    from pathlib import Path
    from config import Config

    checks = {}
    ok = True

    # Platform DB connectivity
    try:
        from app.platform import get_platform_db as _gpdb
        _db = _gpdb()
        _db.execute("SELECT COUNT(*) FROM tenants").fetchone()
        _db.close()
        checks["platform_db"] = "ok"
    except Exception as e:
        checks["platform_db"] = f"error: {e}"
        ok = False

    # Disk space (warn below 15%, fail below 5%)
    try:
        usage = shutil.disk_usage(Config.DATA_DIR)
        free_pct = usage.free / usage.total * 100
        checks["disk_free_pct"]  = round(free_pct, 1)
        checks["disk_free_gb"]   = round(usage.free / 1_073_741_824, 2)
        checks["disk"] = "ok" if free_pct >= 15 else ("warning" if free_pct >= 5 else "critical")
        if free_pct < 5:
            ok = False
    except Exception as e:
        checks["disk"] = f"error: {e}"

    # Backup freshness (warn if last backup > 25h, fail if > 49h)
    try:
        backup_dir = Path(Config.DATA_DIR).parent / "backups"
        archives   = sorted(backup_dir.glob("countdepot_*.tar.gz"),
                            key=lambda p: p.stat().st_mtime) if backup_dir.exists() else []
        if archives:
            age_h = (time.time() - archives[-1].stat().st_mtime) / 3600
            checks["backup_age_hours"] = round(age_h, 1)
            checks["backup"] = "ok" if age_h < 25 else ("stale" if age_h < 49 else "critical")
            if age_h >= 49:
                ok = False
        else:
            checks["backup"] = "no_backups"
    except Exception as e:
        checks["backup"] = f"error: {e}"

    return jsonify({"ok": ok, "service": "countdepot", "checks": checks}), 200 if ok else 503


@bp.route("/manifest.webmanifest")
def web_manifest():
    return send_from_directory(
        current_app.static_folder,
        "manifest.webmanifest",
        mimetype="application/manifest+json",
    )


@bp.route("/sw.js")
def service_worker():
    response = make_response(send_from_directory(current_app.static_folder, "sw.js"))
    response.headers["Content-Type"] = "application/javascript; charset=utf-8"
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/robots.txt")
def robots_txt():
    body = "User-agent: *\nAllow: /\nSitemap: " + request.url_root.rstrip("/") + "/sitemap\n"
    response = make_response(body)
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    return response


@bp.route("/sitemap.xml")
@bp.route("/sitemap")
def sitemap_xml():
    base = request.url_root.rstrip("/")
    urls = [base + "/"] + [base + "/" + slug for slug in seo_slugs()]
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        body += f"  <url><loc>{html.escape(url)}</loc></url>\n"
    body += "</urlset>\n"
    response = make_response(body)
    response.headers["Content-Type"] = "application/xml; charset=utf-8"
    return response


@bp.route("/<path:slug>")
def seo_landing_page(slug):
    page = seo_page_for_slug(slug)
    if not page:
        return redirect(url_for("auth.login_page"))
    canonical_url = request.url_root.rstrip("/") + "/" + slug.strip("/")
    return render_template("seo_landing.html", page=page, canonical_url=canonical_url)


@bp.route("/api-docs")
@login_required
@admin_required
def api_docs_page():
    return render_template("api_docs.html")


@bp.route("/webhooks")
@login_required
@admin_required
def webhooks_page():
    return render_template("webhooks.html")


@bp.route("/docs")
@login_required
@perm_required("view_docs")
def docs_page():
    user_perms = set((session.get("permissions") or "").split(","))
    return render_template(
        "docs.html",
        can_write_docs=session.get("role") == "admin" or "write_docs" in user_perms,
        can_delete_docs=session.get("role") == "admin" or "delete_docs" in user_perms,
    )


@bp.route("/api/demo-request", methods=["POST"])
def api_demo_request():
    """Public endpoint — store a demo request and optionally email the team."""
    from flask import request as req
    ip = req.headers.get("X-Forwarded-For", req.remote_addr or "unknown").split(",")[0].strip()
    allowed, retry_in = check_rate_limit(ip, "demo-request", max_attempts=5, window=900)
    if not allowed:
        return jsonify({"ok": False, "msg": f"Too many demo requests. Try again in {retry_in // 60 + 1} minutes."}), 429

    d       = req.get_json(silent=True) or {}
    name    = (d.get("name") or "").strip()[:120]
    company = (d.get("company") or "").strip()[:160]
    email   = (d.get("email") or "").strip().lower()[:254]
    size    = (d.get("size") or "").strip()[:40]
    if not name or not company or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"ok": False, "msg": "Name, company, and valid email are required"}), 400
    # Store in platform DB
    try:
        from app.platform import get_platform_db as _pdb
        from datetime import datetime as _dt
        db = _pdb()
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS demo_requests "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, "
                "email TEXT, team_size TEXT, created_at TEXT)")
            db.commit()
        except Exception:
            pass
        db.execute(
            "INSERT INTO demo_requests (name,company,email,team_size,created_at) VALUES (?,?,?,?,?)",
            [name, company, email, size, _utc_now().strftime("%Y-%m-%d %H:%M:%S")])
        db.commit()
        db.close()
    except Exception:
        pass
    # Email notification to admin (best-effort)
    try:
        from config import Config as _Cfg
        if _Cfg.SMTP_HOST and _Cfg.PLATFORM_ADMIN_EMAIL:
            from app.mailer import send_email as _send
            _send(
                _Cfg.PLATFORM_ADMIN_EMAIL,
                "New demo request from CountDepot",
                f"<p><b>Name:</b> {html.escape(name)}<br><b>Company:</b> {html.escape(company)}<br>"
                f"<b>Email:</b> {html.escape(email)}<br><b>Team size:</b> {html.escape(size)}</p>",
                f"New demo request: {name} ({company}) — {email}")
    except Exception:
        pass
    return jsonify({"ok": True})


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
                           generated_at=_utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"))


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
        "require_cost,print_scan_label,require_sku_label,default_cost,default_sale "
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


@bp.route("/mobile")
@login_required
def mobile_app():
    return render_template("mobile.html",
        categories=query("SELECT * FROM categories ORDER BY name"),
        products=_products_list(),
        user_perms=list(session.get("permissions", "").split(",")))


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
@perm_required("write_items")
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
@perm_required("write_items")
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
@perm_required("write_items")
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
        expiry = (_utc_now() + timedelta(seconds=tokens["expires_in"])).isoformat()
        _set_setting("qb_access_token",  tokens["access_token"])
        _set_setting("qb_refresh_token", tokens["refresh_token"])
        _set_setting("qb_token_expiry",  expiry)
        _set_setting("qb_realm_id",      realm)
    except Exception as e:
        return render_template("integrations_accounting.html",
                               flash_error=f"QuickBooks token exchange failed: {e}")
    return redirect(url_for("main.integrations_accounting") + "?connected=qb")


@bp.route("/integrations/ebay")
@login_required
@admin_required
def integrations_ebay():
    from app.ebay import ebay_get_credentials
    creds = ebay_get_credentials()
    return render_template("integrations_ebay.html", creds=creds)


@bp.route("/integrations/ebay/callback")
@login_required
@admin_required
def ebay_oauth_callback():
    """eBay OAuth2 callback — exchange code for tokens."""
    from app.ebay import (ebay_exchange_code, _get_setting, _set_setting)
    from datetime import datetime, timedelta
    code  = request.args.get("code", "")
    error = request.args.get("error", "")
    if error or not code:
        return render_template("integrations_ebay.html",
                               creds={},
                               flash_error=f"eBay authorization failed: {error or 'No code received'}")
    try:
        tokens = ebay_exchange_code(
            _get_setting("ebay_client_id", ""),
            _get_setting("ebay_client_secret", ""),
            _get_setting("ebay_ru_name", ""),
            code)
        expiry = (_utc_now() + timedelta(seconds=tokens.get("expires_in", 7200))).isoformat()
        _set_setting("ebay_access_token",  tokens["access_token"])
        _set_setting("ebay_refresh_token", tokens.get("refresh_token", ""))
        _set_setting("ebay_token_expiry",  expiry)
    except Exception as e:
        return render_template("integrations_ebay.html",
                               creds={},
                               flash_error=f"eBay token exchange failed: {e}")
    return redirect(url_for("main.integrations_ebay") + "?connected=1")


@bp.route("/integrations/shopify")
@login_required
@admin_required
def integrations_shopify():
    from app.shopify_integration import shopify_get_credentials
    creds = shopify_get_credentials()
    return render_template("integrations_shopify.html", creds=creds)


@bp.route("/integrations/shopify/webhook", methods=["POST"])
def shopify_webhook():
    """
    Shopify webhook handler — processes orders/paid events to auto-mark items sold.
    No login required; protected by HMAC signature verification.
    """
    from app.shopify_integration import verify_shopify_webhook, _get_setting
    from app.helpers import log_action
    raw_body  = request.get_data()
    hmac_hdr  = request.headers.get("X-Shopify-Hmac-Sha256", "")
    topic     = request.headers.get("X-Shopify-Topic", "")

    # Verify signature if a secret is configured
    secret = _get_setting("shopify_webhook_secret", "")
    if secret and not verify_shopify_webhook(raw_body, hmac_hdr):
        return "Unauthorized", 401

    if topic != "orders/paid":
        return "ok", 200

    try:
        payload = request.json or {}
    except Exception:
        return "ok", 200

    now = datetime.now().strftime("%Y-%m-%d")
    buyer = (
        (payload.get("billing_address") or {}).get("name")
        or payload.get("email")
        or "Shopify"
    )
    order_name = payload.get("name", "")

    for line in payload.get("line_items", []):
        variant_id = str(line.get("variant_id") or "")
        if not variant_id:
            continue
        item = query(
            "SELECT * FROM items WHERE shopify_variant_id=? AND active=1 AND sold=0",
            [variant_id], one=True)
        if not item:
            continue
        price = float(line.get("price") or item.get("sale_price") or 0)
        execute(
            "UPDATE items SET sold=1, sold_date=?, sold_price=?, sold_to=?, "
            "shopify_status='sold', checked_out=0 WHERE id=?",
            [now, price, f"{buyer} (Shopify {order_name})", item["id"]])
        execute("DELETE FROM tasks WHERE item_id=?", [item["id"]])
        log_action("ITEM_SOLD", item["id"], item["name"],
                   f"Sold via Shopify order {order_name} to {buyer} for ${price:.2f}",
                   {"sold": 0}, {"sold": 1})

    return "ok", 200


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
        expiry = (_utc_now() + timedelta(seconds=tokens["expires_in"])).isoformat()
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


@bp.route("/integrations/accounting/zoho/callback")
@login_required
@admin_required
def zoho_oauth_callback():
    """Zoho Books OAuth2 callback, token exchange, and organization selection."""
    from app.accounting import (zoho_exchange_code, zoho_get_organizations, _get_setting, _set_setting)
    code = request.args.get("code", "")
    error = request.args.get("error", "")
    accounts_server = request.args.get("accounts-server", "")
    if accounts_server:
        accounts_server = accounts_server.rstrip("/")
        _set_setting("zoho_accounts_url", accounts_server)
        if "accounts.zoho" in accounts_server and not _get_setting("zoho_api_base", ""):
            suffix = accounts_server.split("accounts.zoho", 1)[1]
            if suffix:
                _set_setting("zoho_api_base", f"https://www.zohoapis{suffix}/books/v3")
    if error or not code:
        return render_template("integrations_accounting.html",
                               flash_error=f"Zoho authorization failed: {error or 'No code received'}")
    try:
        redirect_uri = request.host_url.rstrip("/") + "/integrations/accounting/zoho/callback"
        tokens = zoho_exchange_code(
            _get_setting("zoho_client_id", ""),
            _get_setting("zoho_client_secret", ""),
            code, redirect_uri)
        from datetime import timedelta
        expiry = (_utc_now() + timedelta(seconds=int(tokens.get("expires_in", 3600)))).isoformat()
        _set_setting("zoho_access_token", tokens["access_token"])
        if tokens.get("refresh_token"):
            _set_setting("zoho_refresh_token", tokens["refresh_token"])
        _set_setting("zoho_token_expiry", expiry)
        organizations = zoho_get_organizations(tokens["access_token"])
        if organizations:
            org = organizations[0]
            _set_setting("zoho_organization_id", str(org.get("organization_id", "")))
            _set_setting("zoho_organization_name", org.get("name") or org.get("organization_name") or "")
    except Exception as e:
        return render_template("integrations_accounting.html",
                               flash_error=f"Zoho token exchange failed: {e}")
    return redirect(url_for("main.integrations_accounting") + "?connected=zoho")


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


@bp.route("/reservations")
@login_required
def reservations_page():
    items = query("""SELECT i.id, i.name, i.serial, i.sku, i.internal_sku,
                            c.name as category, c.color
                     FROM items i
                     LEFT JOIN categories c ON c.id=i.category_id
                     WHERE i.active=1 AND i.sold=0 AND COALESCE(i.retired,0)=0
                     ORDER BY i.name""")
    return render_template("reservations.html", items=items)
