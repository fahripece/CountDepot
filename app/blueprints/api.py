import csv
import io
import json
import threading
import time
from datetime import datetime, date as _date

from flask import Blueprint, request, jsonify, session, send_file

from app.db import query, execute
from app.helpers import (login_required, perm_required, admin_required,
                         log_action, get_low_stock_alerts, hash_pw, verify_pw,
                         validate_password, api_rate_limit,
                         _item_missing_fields, sync_item_task, sync_maintenance_tasks,
                         _parse_date_range, _date_filter_sql,
                         location_filter_sql, notify_low_stock_if_needed,
                         ALL_PERMISSIONS, PERM_KEYS,
                         ADMIN_DEFAULT_PERMS, WORKER_DEFAULT_PERMS)

bp = Blueprint("api", __name__)


# ── Internal SKU generation ───────────────────────────────────────────────────

_sku_lock = threading.Lock()

def _ensure_internal_sku(item_id, category_id):
    cat = query("SELECT name FROM categories WHERE id=?",
                [category_id], one=True) if category_id else None
    cat_code = (cat["name"][:4].upper().replace(" ", "") if cat else "GEN")
    today  = _date.today().strftime("%Y%m%d")
    prefix = f"{cat_code}-{today}-"
    with _sku_lock:
        existing = query("SELECT internal_sku FROM items WHERE internal_sku LIKE ?",
                         [f"{prefix}%"])
        nums = []
        for r in existing:
            try:
                nums.append(int(r["internal_sku"].split("-")[-1]))
            except Exception:
                pass
        seq = (max(nums) + 1) if nums else 1
        sku = f"{prefix}{seq:03d}"
        execute("UPDATE items SET internal_sku=? WHERE id=?", [sku, item_id])


# ── Save item (shared for add + edit) ─────────────────────────────────────────

def _save_item(d, iid=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if iid and d.get("qty") in (None, ""):
        existing = query("SELECT qty FROM items WHERE id=?", [iid], one=True)
        qty = existing["qty"] if existing else None
    else:
        qty = int(d["qty"]) if d.get("qty") not in (None, "") else None

    cost = float(d["cost_price"])  if d.get("cost_price")  not in (None, "") else None
    sale = float(d["sale_price"])  if d.get("sale_price")  not in (None, "") else None
    tax  = int(d.get("tax_paid", -1))
    rate = float(d.get("tax_rate", 0)) if d.get("tax_rate") not in (None, "") else 0

    if iid and d.get("low_stock_threshold") in (None, ""):
        e = query("SELECT low_stock_threshold FROM items WHERE id=?", [iid], one=True)
        lst = e["low_stock_threshold"] if e else 0
    else:
        lst = int(d.get("low_stock_threshold", 0)) if d.get("low_stock_threshold") not in (None, "") else 0

    fields = dict(
        product_id            = int(d["product_id"]) if d.get("product_id") not in (None, "") else None,
        name                  = d.get("name"),
        manufacturer          = d.get("manufacturer") or None,
        model                 = d.get("model") or None,
        serial                = d.get("serial") or None,
        sku                   = d.get("sku") or None,
        category_id           = d.get("category_id") or None,
        condition             = d.get("condition") or "New",
        owner_company         = d.get("owner_company") or None,
        purchase_date         = d.get("purchase_date") or None,
        shelf                 = d.get("shelf") or None,
        qty                   = qty,
        low_stock_threshold   = lst,
        notes                 = d.get("notes", ""),
        cost_price            = cost,
        sale_price            = sale,
        tax_paid              = tax,
        tax_rate              = rate,
        sold                  = int(d.get("sold", 0)),
        sold_date             = d.get("sold_date") or None,
        sold_price            = float(d["sold_price"]) if d.get("sold_price") not in (None, "") else None,
        sold_to               = d.get("sold_to") or None,
        require_scan_checkout = int(d["require_scan_checkout"]) if d.get("require_scan_checkout") not in (None, "") else -1,
        ebay_status           = d.get("ebay_status") or "not_listed",
        ebay_listing_id       = d.get("ebay_listing_id") or None,
        ebay_listed_price     = float(d["ebay_listed_price"]) if d.get("ebay_listed_price") not in (None, "") else None,
        ebay_listed_date      = d.get("ebay_listed_date") or None,
        company_id            = int(d["company_id"]) if d.get("company_id") not in (None, "") else None,
        po_number             = d.get("po_number") or None,
        resolution            = d.get("resolution") or None,
        lens_type             = d.get("lens_type") or None,
        has_poe               = int(d.get("has_poe", 0)),
        wireless_standard     = d.get("wireless_standard") or None,
        port_count            = int(d["port_count"]) if d.get("port_count") not in (None, "") else None,
        poe_budget            = d.get("poe_budget") or None,
        throughput            = d.get("throughput") or None,
        os_type               = d.get("os_type") or None,
        screen_size           = d.get("screen_size") or None,
        battery_life          = d.get("battery_life") or None,
        imei                  = d.get("imei") or None,
        carrier               = d.get("carrier") or None,
        cable_type            = d.get("cable_type") or None,
        cable_gauge           = d.get("cable_gauge") or None,
        connector_type        = d.get("connector_type") or None,
        cable_length          = d.get("cable_length") or None,
        extra_fields          = json.dumps(d.get("extra_fields") or {}),
        purchased_from        = d.get("purchased_from") or None,
        sale_state            = d.get("sale_state") or None,
        depreciation_rate     = float(d["depreciation_rate"]) if d.get("depreciation_rate") not in (None, "") else None,
        warranty_expiry       = d.get("warranty_expiry") or None,
        contract_expiry       = d.get("contract_expiry") or None,
        warranty_notes        = d.get("warranty_notes") or None,
        tags                  = ",".join(t.strip() for t in str(d.get("tags") or "").split(",") if t.strip()),
        location_id           = int(d["location_id"]) if d.get("location_id") not in (None, "") else None,
    )
    if iid:
        cols = ", ".join(f"{k}=?" for k in fields)
        execute(f"UPDATE items SET {cols} WHERE id=?", list(fields.values()) + [iid])
        row = query("SELECT sku, internal_sku, category_id FROM items WHERE id=?", [iid], one=True)
        if row and not row["sku"] and not row["internal_sku"]:
            _ensure_internal_sku(iid, row["category_id"])
        return iid
    else:
        fields["created_at"] = now
        cols         = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        new_id = execute(f"INSERT INTO items ({cols}) VALUES ({placeholders})",
                         list(fields.values()))
        _ensure_internal_sku(new_id, fields.get("category_id"))
        return new_id


# ── Report APIs ───────────────────────────────────────────────────────────────

@bp.route("/api/report/financial")
@login_required
@perm_required("view_dashboard")
def api_report_financial():
    date_from, date_to = _parse_date_range(request)
    df_sql, df_args = _date_filter_sql("i.created_at", date_from, date_to)
    sf_sql, sf_args = _date_filter_sql("i.sold_date",  date_from, date_to)

    val   = query(f"""SELECT COUNT(*) as cnt,
                    SUM(CASE WHEN qty IS NULL THEN COALESCE(cost_price,0)
                             ELSE COALESCE(cost_price,0)*COALESCE(qty,0) END) as stock_val,
                    SUM(COALESCE(cost_price,0)) as total_cost,
                    SUM(COALESCE(sale_price,0)) as total_sale
                FROM items i WHERE i.active=1 AND i.sold=0{df_sql}""", df_args, one=True)
    sales = query(f"""SELECT COUNT(*) as cnt,
                      SUM(COALESCE(sold_price,sale_price,0)) as revenue,
                      SUM(COALESCE(cost_price,0)) as cost
                  FROM items i WHERE i.active=1 AND i.sold=1{sf_sql}""", sf_args, one=True)
    tax   = query(f"""SELECT SUM(CASE WHEN tax_paid=1 THEN
                       COALESCE(cost_price,0)*(COALESCE(tax_rate,0)/100)*
                       CASE WHEN qty IS NULL THEN 1 ELSE COALESCE(qty,0) END
                   ELSE 0 END) as tax
                FROM items i WHERE i.active=1{df_sql}""", df_args, one=True)
    by_cat = query(f"""SELECT c.name, c.color,
                       SUM(CASE WHEN i.qty IS NULL THEN COALESCE(i.cost_price,0)
                                ELSE COALESCE(i.cost_price,0)*COALESCE(i.qty,0) END) as val,
                       COUNT(*) as cnt
                   FROM items i JOIN categories c ON c.id=i.category_id
                   WHERE i.active=1 AND i.sold=0{df_sql}
                   GROUP BY c.id ORDER BY val DESC""", df_args)
    sold_items = query(f"""SELECT i.id, i.name, i.sold_date, i.sold_price, i.cost_price,
                           i.condition, i.shelf, i.created_at, i.sold_to,
                           p.name as product_name
                       FROM items i LEFT JOIN products p ON p.id=i.product_id
                       WHERE i.active=1 AND i.sold=1{sf_sql}
                       ORDER BY i.sold_date DESC LIMIT 50""", sf_args)

    revenue   = round((sales["revenue"] or 0), 2)
    cost_sold = round((sales["cost"]    or 0), 2)
    return jsonify({
        "period": {"from": date_from, "to": date_to},
        "cards": {
            "stock_value": round(val["stock_val"]   or 0, 2),
            "total_items": val["cnt"],
            "total_cost":  round(val["total_cost"]  or 0, 2),
            "total_sale":  round(val["total_sale"]  or 0, 2),
            "revenue":     revenue,
            "cost_sold":   cost_sold,
            "profit":      round(revenue - cost_sold, 2),
            "sold_count":  sales["cnt"],
            "tax_paid":    round(tax["tax"] or 0, 2),
        },
        "by_category": [{"name": r["name"], "color": r["color"],
                          "value": round(r["val"] or 0, 2), "count": r["cnt"]}
                         for r in by_cat],
        "sold_items": [dict(r) for r in sold_items],
    })


@bp.route("/api/report/inventory")
@login_required
@perm_required("view_inventory")
def api_report_inventory():
    date_from, date_to = _parse_date_range(request)
    df_sql, df_args    = _date_filter_sql("i.created_at", date_from, date_to)
    items = query(f"""SELECT i.id, i.name, i.serial, i.sku, i.internal_sku,
                      i.condition, i.shelf, i.cost_price, i.sale_price,
                      i.qty, i.qty_out, i.checked_out, i.checkout_by,
                      i.created_at, i.sold, i.purchase_date,
                      c.name as category, c.color,
                      p.name as product_name, p.low_stock_threshold as prod_threshold,
                      (SELECT MAX(ts) FROM item_modifications m WHERE m.item_id=i.id) as last_modified
                  FROM items i
                  LEFT JOIN categories c ON c.id=i.category_id
                  LEFT JOIN products p ON p.id=i.product_id
                  WHERE i.active=1{df_sql}
                  ORDER BY i.name LIMIT 500""", df_args)
    low_prod_ids = set(r["id"] for r in query("""
        SELECT p.id FROM products p
        LEFT JOIN items ii ON ii.product_id=p.id AND ii.active=1 AND ii.sold=0 AND ii.checked_out=0
        WHERE p.active=1 AND p.low_stock_threshold>0
        GROUP BY p.id HAVING COUNT(ii.id)<=p.low_stock_threshold"""))
    result = []
    for r in items:
        d = dict(r)
        d["available"]    = ((d["qty"] or 0) - (d["qty_out"] or 0) if d["qty"] is not None else None)
        d["is_low"]       = False
        missing           = _item_missing_fields(d)
        d["is_incomplete"]  = len(missing) > 0
        d["missing_fields"] = missing
        result.append(d)
    totals = {
        "total":       len(result),
        "checked_out": sum(1 for i in result if i["checked_out"]),
        "low_stock":   len(low_prod_ids),
        "incomplete":  sum(1 for i in result if i["is_incomplete"]),
        "sold":        sum(1 for i in result if i["sold"]),
    }
    return jsonify({"period": {"from": date_from, "to": date_to},
                    "totals": totals, "items": result})


@bp.route("/api/report/depreciation")
@login_required
@perm_required("view_inventory")
def api_report_depreciation():
    """Return items with depreciation_rate set and their current book value."""
    from datetime import date
    today = date.today()
    rows = query("""
        SELECT i.id, i.name, i.serial, i.sku, i.internal_sku, i.shelf,
               i.cost_price, i.purchase_date, i.depreciation_rate,
               i.created_at, c.name as category, p.name as product_name
        FROM items i
        LEFT JOIN categories c ON c.id=i.category_id
        LEFT JOIN products p ON p.id=i.product_id
        WHERE i.active=1 AND COALESCE(i.retired,0)=0 AND i.sold=0
          AND i.depreciation_rate IS NOT NULL AND i.depreciation_rate > 0
        ORDER BY i.name
    """)
    result = []
    total_cost = 0.0
    total_current = 0.0
    for r in rows:
        d = dict(r)
        cost = r["cost_price"]
        rate = r["depreciation_rate"]
        years = 0.0
        if r["purchase_date"]:
            try:
                pd = date.fromisoformat(r["purchase_date"][:10])
                years = (today - pd).days / 365.25
            except Exception:
                pass
        if cost is not None:
            current = max(0.0, cost * (1 - (rate / 100) * years))
            d["current_value"] = round(current, 2)
            d["total_depreciated"] = round(cost - current, 2)
            d["years_held"] = round(years, 1)
            total_cost += cost
            total_current += current
        else:
            d["current_value"] = None
            d["total_depreciated"] = None
            d["years_held"] = round(years, 1)
        result.append(d)
    return jsonify({
        "ok": True,
        "today": today.isoformat(),
        "items": result,
        "totals": {
            "count": len(result),
            "total_cost": round(total_cost, 2),
            "total_current": round(total_current, 2),
            "total_depreciated": round(total_cost - total_current, 2),
        }
    })


@bp.route("/api/report/warranties")
@login_required
@perm_required("view_inventory")
def api_report_warranties():
    """Return items with warranty_expiry or contract_expiry, sorted soonest first."""
    horizon = request.args.get("days", "90")
    try:
        horizon = int(horizon)
    except Exception:
        horizon = 90
    from datetime import date, timedelta
    today = date.today().isoformat()
    cutoff = (date.today() + timedelta(days=horizon)).isoformat()
    rows = query("""
        SELECT i.id, i.name, i.serial, i.sku, i.internal_sku, i.shelf,
               i.warranty_expiry, i.contract_expiry, i.warranty_notes,
               i.checked_out, i.purchase_date, i.cost_price,
               c.name as category, p.name as product_name
        FROM items i
        LEFT JOIN categories c ON c.id=i.category_id
        LEFT JOIN products p ON p.id=i.product_id
        WHERE i.active=1 AND i.sold=0
          AND (
            (i.warranty_expiry IS NOT NULL AND i.warranty_expiry <= ?)
            OR (i.contract_expiry IS NOT NULL AND i.contract_expiry <= ?)
          )
        ORDER BY COALESCE(i.warranty_expiry, i.contract_expiry)
    """, [cutoff, cutoff])
    result = []
    for r in rows:
        d = dict(r)
        if r["warranty_expiry"]:
            diff = (date.fromisoformat(r["warranty_expiry"]) - date.today()).days
            d["warranty_days_left"] = diff
        else:
            d["warranty_days_left"] = None
        if r["contract_expiry"]:
            diff2 = (date.fromisoformat(r["contract_expiry"]) - date.today()).days
            d["contract_days_left"] = diff2
        else:
            d["contract_days_left"] = None
        result.append(d)
    return jsonify({"ok": True, "horizon": horizon, "today": today, "items": result})


@bp.route("/api/report/<report_type>/pdf")
@login_required
@perm_required("view_inventory")
def api_report_pdf(report_type):
    """Generate a PDF for any supported report type and return as download."""
    from flask import send_file
    import io as _io
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER
    except ImportError:
        return jsonify({"ok": False, "msg": "reportlab not installed"}), 500

    from datetime import date, timedelta
    from flask import g

    today = date.today().isoformat()
    tenant_name = (g.tenant.get("name", "") if hasattr(g, "tenant") and g.tenant else "CountDepot")

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)

    navy   = colors.HexColor("#0f172a")
    blue   = colors.HexColor("#1d4ed8")
    grey   = colors.HexColor("#64748b")
    lgrey  = colors.HexColor("#f8f7f4")
    red    = colors.HexColor("#b91c1c")
    amber  = colors.HexColor("#b45309")
    border = colors.HexColor("#e5e3de")

    h1  = ParagraphStyle("h1",  fontSize=18, textColor=navy,  fontName="Helvetica-Bold",  spaceAfter=4)
    sub = ParagraphStyle("sub", fontSize=10, textColor=grey,  fontName="Helvetica",        spaceAfter=0)
    th  = ParagraphStyle("th",  fontSize=8,  textColor=grey,  fontName="Helvetica-Bold")
    td  = ParagraphStyle("td",  fontSize=9,  textColor=navy,  fontName="Helvetica")
    tdr = ParagraphStyle("tdr", fontSize=9,  textColor=navy,  fontName="Helvetica",        alignment=TA_RIGHT)

    page_w = letter[0] - 1.5*inch
    story  = []

    def _header(title):
        story.append(Paragraph(tenant_name, sub))
        story.append(Paragraph(title, h1))
        story.append(Paragraph(f"Generated {today}", sub))
        story.append(Spacer(1, 14))

    REPORT_TABLE_STYLE = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0),  lgrey),
        ("TEXTCOLOR",   (0,0), (-1,0),  grey),
        ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0),  8),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#fafaf9")]),
        ("GRID",        (0,0), (-1,-1), 0.5, border),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING",(0,0), (-1,-1), 8),
        ("TOPPADDING",  (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
    ])

    if report_type == "inventory":
        _header("Inventory Report")
        items = query("""SELECT i.id, i.name, i.serial, i.sku, i.internal_sku,
                               i.shelf, i.condition, i.cost_price, i.checked_out,
                               i.sold, c.name as category, p.name as product_name
                        FROM items i
                        LEFT JOIN categories c ON c.id=i.category_id
                        LEFT JOIN products p ON p.id=i.product_id
                        WHERE i.active=1 AND COALESCE(i.retired,0)=0
                        ORDER BY i.name LIMIT 1000""")
        rows = [[Paragraph(h, th) for h in ["Item","Category","Serial/SKU","Shelf","Condition","Status","Cost"]]]
        for i in items:
            ident = i["serial"] or i["sku"] or i["internal_sku"] or f"ID-{i['id']}"
            status = "Sold" if i["sold"] else ("Out" if i["checked_out"] else "In")
            cost   = f"${i['cost_price']:,.2f}" if i["cost_price"] else "—"
            rows.append([Paragraph(i["name"][:40], td), Paragraph(i["category"] or "—", td),
                         Paragraph(ident, td), Paragraph(i["shelf"] or "—", td),
                         Paragraph(i["condition"] or "—", td), Paragraph(status, td),
                         Paragraph(cost, tdr)])
        col_w = [page_w*0.28, page_w*0.14, page_w*0.14, page_w*0.09, page_w*0.11, page_w*0.09, page_w*0.15]
        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        tbl.setStyle(REPORT_TABLE_STYLE)
        story.append(tbl)
        filename = "inventory_report.pdf"

    elif report_type == "financial":
        _header("Financial Report")
        items = query("""SELECT i.name, i.cost_price, i.sale_price, i.sold, i.sold_price,
                               i.sold_date, c.name as category
                        FROM items i LEFT JOIN categories c ON c.id=i.category_id
                        WHERE i.active=1 ORDER BY i.name LIMIT 1000""")
        rows = [[Paragraph(h, th) for h in ["Item","Category","Cost","Sale Price","Status","Sold For","Sold Date"]]]
        for i in items:
            status = "Sold" if i["sold"] else "In Stock"
            rows.append([Paragraph(i["name"][:40], td), Paragraph(i["category"] or "—", td),
                         Paragraph(f"${i['cost_price']:,.2f}" if i["cost_price"] else "—", tdr),
                         Paragraph(f"${i['sale_price']:,.2f}" if i["sale_price"] else "—", tdr),
                         Paragraph(status, td),
                         Paragraph(f"${i['sold_price']:,.2f}" if i["sold_price"] else "—", tdr),
                         Paragraph((i["sold_date"] or "")[:10] or "—", td)])
        col_w = [page_w*0.28, page_w*0.14, page_w*0.11, page_w*0.11, page_w*0.1, page_w*0.11, page_w*0.15]
        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        tbl.setStyle(REPORT_TABLE_STYLE)
        story.append(tbl)
        filename = "financial_report.pdf"

    elif report_type == "checkout-history":
        _header("Checkout History Report")
        rows_q = query("""SELECT cl.checkout_date, cl.checkin_date, cl.checked_out_by,
                                 cl.expected_return_date, i.name as item_name, i.serial
                          FROM checkout_log cl JOIN items i ON i.id=cl.item_id
                          ORDER BY cl.checkout_date DESC LIMIT 500""")
        rows = [[Paragraph(h, th) for h in ["Item","Serial","Checked Out By","Out Date","Due","In Date"]]]
        for r in rows_q:
            rows.append([Paragraph(r["item_name"][:35], td), Paragraph(r["serial"] or "—", td),
                         Paragraph(r["checked_out_by"] or "—", td),
                         Paragraph((r["checkout_date"] or "")[:10], td),
                         Paragraph((r["expected_return_date"] or "")[:10] or "—", td),
                         Paragraph((r["checkin_date"] or "")[:10] or "Open", td)])
        col_w = [page_w*0.28, page_w*0.14, page_w*0.16, page_w*0.14, page_w*0.14, page_w*0.14]
        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        tbl.setStyle(REPORT_TABLE_STYLE)
        story.append(tbl)
        filename = "checkout_history.pdf"

    elif report_type == "warranties":
        _header("Warranty & Contract Report")
        cutoff = (date.today() + timedelta(days=90)).isoformat()
        rows_q = query("""SELECT i.name, i.serial, i.shelf, i.warranty_expiry,
                                 i.contract_expiry, i.warranty_notes
                          FROM items i WHERE i.active=1 AND i.sold=0
                            AND ((i.warranty_expiry IS NOT NULL AND i.warranty_expiry<=?)
                              OR (i.contract_expiry IS NOT NULL AND i.contract_expiry<=?))
                          ORDER BY COALESCE(i.warranty_expiry,i.contract_expiry)""", [cutoff, cutoff])
        rows = [[Paragraph(h, th) for h in ["Item","Serial","Location","Warranty Expiry","Contract Expiry","Notes"]]]
        for r in rows_q:
            rows.append([Paragraph(r["name"][:35], td), Paragraph(r["serial"] or "—", td),
                         Paragraph(r["shelf"] or "—", td),
                         Paragraph((r["warranty_expiry"] or "—")[:10], td),
                         Paragraph((r["contract_expiry"] or "—")[:10], td),
                         Paragraph(r["warranty_notes"] or "—", td)])
        col_w = [page_w*0.26, page_w*0.14, page_w*0.12, page_w*0.14, page_w*0.14, page_w*0.20]
        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        tbl.setStyle(REPORT_TABLE_STYLE)
        story.append(tbl)
        filename = "warranties_report.pdf"

    elif report_type == "depreciation":
        _header("Depreciation Report")
        rows_q = query("""SELECT i.name, i.serial, i.cost_price, i.depreciation_rate,
                                 i.purchase_date, c.name as category
                          FROM items i LEFT JOIN categories c ON c.id=i.category_id
                          WHERE i.active=1 AND i.sold=0 AND COALESCE(i.retired,0)=0
                            AND i.depreciation_rate IS NOT NULL AND i.depreciation_rate>0
                          ORDER BY i.name""")
        rows = [[Paragraph(h, th) for h in ["Item","Category","Original Cost","Rate","Age","Current Value"]]]
        for r in rows_q:
            cost = r["cost_price"] or 0
            rate = r["depreciation_rate"] or 0
            yrs  = 0.0
            if r["purchase_date"]:
                try: yrs = (date.today() - date.fromisoformat(r["purchase_date"][:10])).days / 365.25
                except: pass
            cur = max(0.0, cost * (1 - (rate/100)*yrs))
            rows.append([Paragraph(r["name"][:35], td), Paragraph(r["category"] or "—", td),
                         Paragraph(f"${cost:,.2f}", tdr), Paragraph(f"{rate}%/yr", tdr),
                         Paragraph(f"{yrs:.1f}yr", tdr), Paragraph(f"${cur:,.2f}", tdr)])
        col_w = [page_w*0.30, page_w*0.16, page_w*0.13, page_w*0.10, page_w*0.10, page_w*0.21]
        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        tbl.setStyle(REPORT_TABLE_STYLE)
        story.append(tbl)
        filename = "depreciation_report.pdf"

    else:
        return jsonify({"ok": False, "msg": f"Unknown report type: {report_type}"}), 404

    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=filename)


@bp.route("/api/report/activity")
@login_required
@perm_required("view_audit")
def api_report_activity():
    date_from, date_to = _parse_date_range(request)
    df_sql, df_args    = _date_filter_sql("a.ts", date_from, date_to)
    top_items = query(f"""SELECT item_name, COUNT(*) as cnt FROM audit_log a
                      WHERE action='CHECKOUT' AND item_name IS NOT NULL{df_sql}
                      GROUP BY item_name ORDER BY cnt DESC LIMIT 10""", df_args)
    tech = query(f"""SELECT tech, COUNT(*) as cnt FROM (
                     SELECT NULLIF(TRIM(CASE WHEN detail LIKE 'By: %' THEN
                         SUBSTR(detail, 5, CASE WHEN INSTR(detail,' | ')>0
                             THEN INSTR(detail,' | ')-5 ELSE LENGTH(detail) END)
                     ELSE COALESCE(username,'Unknown') END),'') as tech
                     FROM audit_log a WHERE action='CHECKOUT'{df_sql}
                   ) WHERE tech IS NOT NULL AND tech != ''
                   GROUP BY tech ORDER BY cnt DESC LIMIT 10""", df_args)
    actions = query(f"""SELECT action, COUNT(*) as cnt FROM audit_log a
                    WHERE 1=1{df_sql} GROUP BY action ORDER BY cnt DESC""", df_args)
    recent  = query(f"""SELECT a.ts, a.action, a.item_name, a.username, a.detail,
                       a.item_serial, a.item_sku, a.product_name, a.before_state, a.after_state
                   FROM audit_log a WHERE 1=1{df_sql}
                   ORDER BY a.id DESC LIMIT 100""", df_args)
    # Daily activity counts for sparkline (last 30 days or within range)
    daily = query(f"""SELECT SUBSTR(a.ts,1,10) as day, COUNT(*) as cnt
                   FROM audit_log a WHERE 1=1{df_sql}
                   GROUP BY day ORDER BY day ASC LIMIT 60""", df_args)
    # Unique active users in period
    active_users_row = query(f"""SELECT COUNT(DISTINCT username) as cnt FROM audit_log a
                          WHERE username IS NOT NULL AND username != 'system'{df_sql}""", df_args,
                             one=True)
    return jsonify({
        "period":        {"from": date_from, "to": date_to},
        "top_items":     [{"name": r["item_name"], "count": r["cnt"]} for r in top_items],
        "tech_activity": [{"name": r["tech"],      "count": r["cnt"]} for r in tech],
        "actions":       [{"action": r["action"],  "count": r["cnt"]} for r in actions],
        "recent":        [dict(r) for r in recent],
        "daily":         [{"day": r["day"], "count": r["cnt"]} for r in daily],
        "active_users":  active_users_row["cnt"] if active_users_row else 0,
    })


@bp.route("/api/dashboard")
@login_required
@perm_required("view_dashboard")
def api_dashboard():
    total       = query("SELECT COUNT(*) FROM items WHERE active=1 AND sold=0",       one=True)[0]
    checked_out = query("SELECT COUNT(*) FROM items WHERE active=1 AND checked_out=1 AND sold=0", one=True)[0]
    low_stock   = query("""SELECT COUNT(*) FROM (
        SELECT p.id FROM products p
        LEFT JOIN items i ON i.product_id=p.id AND i.active=1 AND i.sold=0 AND i.checked_out=0
        WHERE p.active=1 AND p.low_stock_threshold>0
        GROUP BY p.id HAVING COUNT(i.id)<=p.low_stock_threshold)""", one=True)[0]
    sold_count  = query("SELECT COUNT(*) FROM items WHERE active=1 AND sold=1",        one=True)[0]
    stock_value = round(query("""SELECT SUM(CASE WHEN qty IS NULL THEN COALESCE(cost_price,0)
                                     ELSE COALESCE(cost_price,0)*COALESCE(qty,0) END)
                       FROM items WHERE active=1 AND sold=0""", one=True)[0] or 0, 2)
    total_tax   = round(query("""SELECT SUM(CASE WHEN tax_paid=1 THEN
                           COALESCE(cost_price,0)*(COALESCE(tax_rate,0)/100)*
                           CASE WHEN qty IS NULL THEN 1 ELSE COALESCE(qty,0) END
                       ELSE 0 END) FROM items WHERE active=1""", one=True)[0] or 0, 2)
    revenue     = round(query("SELECT SUM(COALESCE(sold_price,sale_price,0)) FROM items WHERE active=1 AND sold=1", one=True)[0] or 0, 2)
    cost_sold   = round(query("SELECT SUM(COALESCE(cost_price,0)) FROM items WHERE active=1 AND sold=1", one=True)[0] or 0, 2)
    profit      = round(revenue - cost_sold, 2)
    today_iso   = datetime.now().strftime("%Y-%m-%d")
    overdue     = query("SELECT COUNT(*) FROM items WHERE active=1 AND checked_out=1 AND expected_return_date IS NOT NULL AND expected_return_date < ?", [today_iso], one=True)[0]
    maint_due   = query("SELECT COUNT(*) FROM items WHERE active=1 AND sold=0 AND next_maintenance_date IS NOT NULL AND next_maintenance_date <= ?", [today_iso], one=True)[0]
    by_category = [{"name": r["name"], "color": r["color"],
                    "value": round(r["val"] or 0, 2), "count": r["cnt"]}
                   for r in query("""SELECT c.name, c.color,
                               SUM(CASE WHEN i.qty IS NULL THEN COALESCE(i.cost_price,0)
                                        ELSE COALESCE(i.cost_price,0)*COALESCE(i.qty,0) END) as val,
                               COUNT(*) as cnt
                        FROM items i JOIN categories c ON c.id=i.category_id
                        WHERE i.active=1 AND i.sold=0 GROUP BY c.id ORDER BY val DESC""")]
    top_items     = [{"name": r["item_name"], "count": r["cnt"]}
                     for r in query("""SELECT item_name, COUNT(*) as cnt FROM audit_log
                        WHERE action='CHECKOUT' AND item_name IS NOT NULL
                        GROUP BY item_name ORDER BY cnt DESC LIMIT 8""")]
    tech_activity = [{"name": r["tech"], "count": r["cnt"]}
                     for r in query("""SELECT CASE WHEN detail LIKE 'By: %' THEN
                               TRIM(SUBSTR(detail, 5, CASE WHEN INSTR(detail,' | ')>0
                                   THEN INSTR(detail,' | ')-5 ELSE LENGTH(detail) END))
                           ELSE username END as tech, COUNT(*) as cnt
                         FROM audit_log WHERE action='CHECKOUT'
                         GROUP BY tech ORDER BY cnt DESC LIMIT 6""")]
    monthly_spend = list(reversed([{"month": r["month"], "spend": round(r["spend"] or 0, 2), "items": r["items"]}
                     for r in query("""SELECT SUBSTR(purchase_date,1,7) as month,
                              SUM(COALESCE(cost_price,0)) as spend, COUNT(*) as items
                       FROM items WHERE active=1 AND purchase_date IS NOT NULL AND purchase_date!=''
                       GROUP BY month ORDER BY month DESC LIMIT 6""")]))
    return jsonify({
        "cards": {"total": total, "checked_out": checked_out, "low_stock": low_stock,
                  "stock_value": stock_value, "total_tax": total_tax,
                  "revenue": revenue, "profit": profit, "sold_count": sold_count,
                  "overdue": overdue, "maintenance_due": maint_due},
        "by_category":  by_category,
        "top_items":    top_items,
        "tech_activity": tech_activity,
        "monthly_spend": monthly_spend,
        "activity":     [dict(r) for r in query("SELECT ts, action, item_name, username, detail FROM audit_log ORDER BY id DESC LIMIT 12")],
        "low_items":    get_low_stock_alerts(),
    })


# ── Items ─────────────────────────────────────────────────────────────────────

@bp.route("/api/items")
@login_required
def api_items():
    search   = request.args.get("q", "").strip()
    cat_id   = request.args.get("cat", "")
    status   = request.args.get("status", "")
    sort     = request.args.get("sort", "name")
    prod_id  = request.args.get("product", "")
    page     = max(1, int(request.args.get("page", 1) or 1))
    per_page = min(200, max(10, int(request.args.get("per_page", 50) or 50)))
    # hide_out=0 is used by the auto-open ?item=ID flow — skip pagination for it
    no_paginate = request.args.get("hide_out", "1") == "0"

    loc_id   = request.args.get("loc", "")
    dept_id  = request.args.get("dept", "")
    tag      = request.args.get("tag", "").strip()
    cond_f   = request.args.get("cond", "").strip()
    show_retired = (status == "retired")
    base_where = """FROM items i
             LEFT JOIN categories c   ON c.id=i.category_id
             LEFT JOIN products p     ON p.id=i.product_id
             LEFT JOIN companies co   ON co.id=i.company_id
             LEFT JOIN locations l    ON l.id=i.location_id
             LEFT JOIN departments d  ON d.id=i.department_id
             LEFT JOIN (SELECT item_id, COUNT(*) as photo_count FROM item_photos GROUP BY item_id) ph ON ph.item_id=i.id
             WHERE i.active=1 AND COALESCE(i.retired,0)=""" + ("1" if show_retired else "0")
    sql  = """SELECT i.*, c.name as category, c.color,
                    p.name as product_name, p.serial_tracked, p.qty_tracked,
                    p.require_scan_checkout as product_scan_req,
                    p.require_serial as product_req_serial,
                    p.require_vendor_sku as product_req_vendor_sku,
                    p.require_internal_sku as product_req_internal_sku,
                    p.print_scan_label as product_print_scan,
                    co.name as company_name,
                    l.name as location_name,
                    d.name as department_name, d.color as department_color,
                    COALESCE(ph.photo_count, 0) as photo_count
             """ + base_where
    args = []
    if search:
        id_search = search.lstrip("#")
        cond = (" AND (i.name LIKE ? OR i.serial LIKE ? OR i.model LIKE ? "
                "OR i.sku LIKE ? OR i.internal_sku LIKE ? "
                "OR CAST(i.shelf AS TEXT) LIKE ? OR i.job_ref LIKE ? "
                "OR i.owner_company LIKE ? OR i.manufacturer LIKE ? "
                "OR i.po_number LIKE ? OR CAST(i.id AS TEXT) = ?)")
        s = f"%{search}%"
        args += [s]*10 + [id_search]
        sql += cond
    loc_sql, loc_args = location_filter_sql("i")
    sql += loc_sql; args += loc_args
    if cat_id:  sql += " AND i.category_id=?"; args.append(cat_id)
    if prod_id: sql += " AND i.product_id=?";  args.append(prod_id)
    if loc_id:  sql += " AND i.location_id=?";   args.append(loc_id)
    if dept_id: sql += " AND i.department_id=?"; args.append(dept_id)
    if cond_f:  sql += " AND i.condition=?";     args.append(cond_f)
    # Restrict worker to own department if flag is set
    user_dept = query("SELECT department_id, restrict_to_department FROM users WHERE id=?",
                      [session.get("user_id")], one=True) if session.get("role") != "admin" else None
    if user_dept and user_dept["restrict_to_department"] and user_dept["department_id"]:
        sql += " AND i.department_id=?"; args.append(user_dept["department_id"])
    if tag:     sql += " AND (',' || i.tags || ',') LIKE ?"; args.append(f"%,{tag},%")
    if status == "out":
        sql += " AND i.checked_out=1"
    elif status == "in":
        sql += " AND i.checked_out=0 AND i.sold=0"
    elif status == "low":
        sql += """ AND i.product_id IN (
            SELECT p.id FROM products p
            LEFT JOIN items ii ON ii.product_id=p.id AND ii.active=1 AND ii.sold=0 AND ii.checked_out=0
            WHERE p.active=1 AND p.low_stock_threshold>0
            GROUP BY p.id HAVING COUNT(ii.id)<=p.low_stock_threshold)"""
    elif status == "incomplete":
        sql += """ AND (
            (p.require_serial=1 AND (i.serial IS NULL OR i.serial='')) OR
            (p.require_vendor_sku=1 AND (i.sku IS NULL OR i.sku='')) OR
            i.cost_price IS NULL OR
            (i.shelf IS NULL OR i.shelf=''))"""
    elif status == "overdue":
        today_str = datetime.now().strftime("%Y-%m-%d")
        sql += " AND i.checked_out=1 AND i.expected_return_date IS NOT NULL AND i.expected_return_date < ?"
        args.append(today_str)
    elif status == "maintenance":
        today_str = datetime.now().strftime("%Y-%m-%d")
        sql += " AND i.sold=0 AND i.next_maintenance_date IS NOT NULL AND i.next_maintenance_date <= ?"
        args.append(today_str)
    elif status == "retired":
        pass  # base_where already filters to retired=1
    else:
        if not no_paginate:
            sql += " AND i.checked_out=0 AND i.sold=0"

    order = {"name": "i.name", "shelf": "i.shelf", "cat": "c.name",
             "cost": "i.cost_price", "sale": "i.sale_price",
             "date": "i.purchase_date"}.get(sort, "i.name")

    # Strip SELECT clause, keep from FROM onward (without ORDER BY)
    where_part = sql[sql.index("FROM"):]
    if "ORDER" in where_part: where_part = where_part[:where_part.rindex("ORDER")]

    # Single aggregate query replaces the previous 3 separate COUNT/SUM queries
    agg = query(
        "SELECT COUNT(*) as total,"
        " SUM(CASE WHEN i.checked_out=1 THEN 1 ELSE 0 END) as checked_out_count,"
        " COALESCE(SUM(COALESCE(i.cost_price,0)*CASE WHEN i.qty IS NULL THEN 1 ELSE COALESCE(i.qty,0) END),0) as stock_value "
        + where_part, args, one=True)
    total             = agg["total"] or 0
    checked_out_count = agg["checked_out_count"] or 0
    stock_value       = round(float(agg["stock_value"] or 0), 2)

    sql += f" ORDER BY {order}"
    if not no_paginate:
        sql += f" LIMIT {per_page} OFFSET {(page - 1) * per_page}"

    result = []
    for r in query(sql, args):
        d = dict(r)
        d["available"]   = ((d["qty"] or 0) - (d["qty_out"] or 0) if d["qty"] is not None else None)
        d["is_low"]      = False
        d["profit"]      = (round(d["sale_price"] - d["cost_price"], 2) if d["sale_price"] and d["cost_price"] else None)
        today_s = datetime.now().strftime("%Y-%m-%d")
        d["overdue"]     = bool(d.get("checked_out") and d.get("expected_return_date") and d["expected_return_date"] < today_s)
        d["maintenance_overdue"] = bool(d.get("next_maintenance_date") and d["next_maintenance_date"] < today_s)
        d["tax_amount"]  = (round(d["cost_price"] * (d["tax_rate"] or 0) / 100, 2) if d["cost_price"] and d["tax_paid"] == 1 else 0)
        missing = []
        if d.get("product_req_serial")     and not d.get("serial"): missing.append("serial #")
        if d.get("product_req_vendor_sku") and not d.get("sku"):    missing.append("vendor SKU")
        if d.get("cost_price") is None: missing.append("cost")
        if not d.get("shelf"):          missing.append("shelf")
        d["is_incomplete"]  = len(missing) > 0
        d["missing_fields"] = missing
        result.append(d)

    if no_paginate:
        return jsonify(result)
    pages = max(1, -(-total // per_page))  # ceil division
    return jsonify({"items": result, "total": total, "page": page,
                    "pages": pages, "per_page": per_page,
                    "totals": {"checked_out": checked_out_count, "stock_value": stock_value}})


@bp.route("/api/scan")
@login_required
def api_scan():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"found": False})
    row = query("""SELECT i.*, c.name as category, c.color
                   FROM items i LEFT JOIN categories c ON c.id=i.category_id
                   WHERE i.active=1 AND (i.serial=? OR i.model=? OR i.sku=? OR i.internal_sku=?) LIMIT 1""",
                [code, code, code, code], one=True)
    if not row:
        row = query("""SELECT i.*, c.name as category, c.color
                       FROM items i LEFT JOIN categories c ON c.id=i.category_id
                       WHERE i.active=1 AND (i.serial LIKE ? OR i.sku LIKE ? OR i.internal_sku LIKE ?) LIMIT 1""",
                    [f"%{code}%", f"%{code}%", f"%{code}%"], one=True)
    return jsonify({"found": bool(row), "item": dict(row) if row else None})


@bp.route("/api/checkout", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_checkout():
    d    = request.json
    item = query("""SELECT i.*, p.require_scan_checkout as product_scan_req
                    FROM items i LEFT JOIN products p ON p.id=i.product_id
                    WHERE i.id=? AND i.active=1""", [d["id"]], one=True)
    if not item: return jsonify({"ok": False, "msg": "Not found"})
    if item["sold"]:        return jsonify({"ok": False, "msg": "Item has been sold"})
    if item["checked_out"]: return jsonify({"ok": False, "msg": "Already checked out"})
    if item.get("out_of_service"):
        reason = item.get("out_of_service_reason") or "No reason given"
        return jsonify({"ok": False, "msg": f"Item is out of service: {reason}"})
    if item.get("recall_flag"):
        return jsonify({"ok": False, "msg": "Item is under recall and cannot be checked out"})
    item_override   = item["require_scan_checkout"] if item["require_scan_checkout"] is not None else -1
    scan_required   = item_override if item_override != -1 else (item["product_scan_req"] or 0)
    if scan_required:
        scanned = d.get("scanned_code", "").strip()
        if not scanned:
            return jsonify({"ok": False, "msg": "scan_required",
                            "detail": "This item requires a serial/SKU scan before checkout"})
        valid = [x for x in [item["serial"], item["sku"], item["internal_sku"], item["model"]] if x]
        if scanned not in valid:
            return jsonify({"ok": False, "msg": "scan_mismatch",
                            "detail": f"Scanned '{scanned}' does not match item serial/SKU/model"})
    now  = datetime.now().strftime("%m/%d/%y")
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    who  = d.get("who", "").strip() or session.get("username", "?")
    dept = (d.get("dept") or "").strip() or None
    new_sale = d.get("update_sale_price")
    expected_return = d.get("expected_return_date") or None
    if new_sale is not None:
        execute("UPDATE items SET sale_price=? WHERE id=?", [new_sale, item["id"]])
    execute("UPDATE items SET checked_out=1,checkout_date=?,checkout_by=?,job_ref=?,expected_return_date=?,checkout_dept=? WHERE id=?",
            [now, who, d.get("job_ref", ""), expected_return, dept, item["id"]])
    execute("""INSERT INTO checkout_log
               (item_id,item_name,checked_out_by,job_ref,checkout_date,expected_return_date,checkout_dept,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [item["id"], item["name"], who, d.get("job_ref",""), now, expected_return, dept, now_iso])
    log_action("CHECKOUT", item["id"], item["name"],
               f"By: {who}" + (f" | Dept: {dept}" if dept else "") + f" | Job: {d.get('job_ref','')}" +
               (f" | Sale price → ${new_sale:.2f}" if new_sale is not None else ""),
               {"checked_out": 0}, {"checked_out": 1})
    notify_low_stock_if_needed(item.get("product_id"))
    try:
        from app.webhooks import fire as _wh
        _wh("item.checked_out", {"id": item["id"], "name": item["name"],
                                  "checked_out_by": who, "job_ref": d.get("job_ref", "")})
    except Exception:
        pass
    return jsonify({"ok": True})


@bp.route("/api/checkin", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_checkin():
    d    = request.json
    item = query("SELECT * FROM items WHERE id=? AND active=1", [d["id"]], one=True)
    if not item:            return jsonify({"ok": False, "msg": "Not found"})
    if not item["checked_out"]: return jsonify({"ok": False, "msg": "Already checked in"})
    now_iso    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    checkin_note = (d.get("checkin_note") or "").strip() or None
    checkin_by   = session.get("username", "?")
    # Compute duration if we have a checkout timestamp in checkout_log
    log_row = query(
        "SELECT * FROM checkout_log WHERE item_id=? AND checkin_date IS NULL ORDER BY id DESC LIMIT 1",
        [item["id"]], one=True)
    duration_h = None
    if log_row and log_row["checkout_date"]:
        try:
            from datetime import datetime as _dt
            fmt_map = ["%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
            co_dt = None
            for fmt in fmt_map:
                try: co_dt = _dt.strptime(log_row["checkout_date"], fmt); break
                except ValueError: pass
            if co_dt:
                duration_h = round((datetime.now() - co_dt).total_seconds() / 3600, 2)
        except Exception:
            pass
    if log_row:
        execute("""UPDATE checkout_log SET checkin_date=?,checkin_note=?,checkin_by=?,duration_hours=?
                   WHERE id=?""",
                [now_iso, checkin_note, checkin_by, duration_h, log_row["id"]])
    execute("UPDATE items SET checked_out=0,checkout_date=NULL,checkout_by=NULL,job_ref=NULL,expected_return_date=NULL,checkout_dept=NULL WHERE id=?",
            [item["id"]])
    log_action("CHECKIN", item["id"], item["name"],
               f"Returned. Was on: {item['job_ref'] or '-'}" + (f" | Note: {checkin_note}" if checkin_note else ""),
               {"checked_out": 1}, {"checked_out": 0})
    try:
        from app.webhooks import fire as _wh
        _wh("item.checked_in", {"id": item["id"], "name": item["name"]})
    except Exception:
        pass
    return jsonify({"ok": True})


@bp.route("/api/locations")
@login_required
def api_get_locations():
    allowed_ids = session.get("location_ids") or []
    try:
        # Correlated subqueries avoid cross-product from joining items + user_locations
        sql = """
            SELECT l.*,
                   (SELECT COUNT(*) FROM items
                    WHERE location_id=l.id AND active=1)                          AS item_count,
                   (SELECT COUNT(*) FROM items
                    WHERE location_id=l.id AND active=1
                      AND checked_out=0 AND sold=0)                               AS available_count,
                   (SELECT COUNT(*) FROM user_locations
                    WHERE location_id=l.id)                                       AS member_count
            FROM locations l
        """
        if allowed_ids:
            sql += f" WHERE l.id IN ({','.join('?'*len(allowed_ids))})"
        sql += " ORDER BY l.name"
        rows = query(sql, allowed_ids if allowed_ids else [])
        result = [dict(r) for r in rows]
    except Exception:
        # user_locations table may not exist yet on older tenant DBs —
        # fall back to a simpler query without member_count
        sql = """
            SELECT l.*,
                   (SELECT COUNT(*) FROM items
                    WHERE location_id=l.id AND active=1)                          AS item_count,
                   (SELECT COUNT(*) FROM items
                    WHERE location_id=l.id AND active=1
                      AND checked_out=0 AND sold=0)                               AS available_count,
                   0                                                               AS member_count
            FROM locations l
        """
        if allowed_ids:
            sql += f" WHERE l.id IN ({','.join('?'*len(allowed_ids))})"
        sql += " ORDER BY l.name"
        rows = query(sql, allowed_ids if allowed_ids else [])
        result = [dict(r) for r in rows]

    # Attach member usernames (up to 5 per site)
    try:
        if result:
            loc_ids = [r["id"] for r in result]
            placeholders = ",".join("?" * len(loc_ids))
            member_rows = query(
                f"SELECT ul.location_id, u.username "
                f"FROM user_locations ul "
                f"JOIN users u ON u.id=ul.user_id AND COALESCE(u.active,1)=1 "
                f"WHERE ul.location_id IN ({placeholders}) "
                f"ORDER BY u.username",
                loc_ids)
            members_by_loc = {}
            for mr in member_rows:
                lid = mr["location_id"]
                if lid not in members_by_loc:
                    members_by_loc[lid] = []
                if len(members_by_loc[lid]) < 5:
                    members_by_loc[lid].append(mr["username"])
            for loc in result:
                loc["member_names"] = members_by_loc.get(loc["id"], [])
        else:
            for loc in result:
                loc["member_names"] = []
    except Exception:
        for loc in result:
            loc.setdefault("member_names", [])

    return jsonify(result)


@bp.route("/api/location/<int:loc_id>/items")
@login_required
def api_location_items(loc_id):
    """All active items at a specific site."""
    loc = query("SELECT * FROM locations WHERE id=?", [loc_id], one=True)
    if not loc:
        return jsonify({"ok": False, "msg": "Location not found"}), 404
    items = query("""
        SELECT i.*, c.name as category, c.color,
               p.name as product_name, co.name as company_name
        FROM items i
        LEFT JOIN categories c ON c.id=i.category_id
        LEFT JOIN products p   ON p.id=i.product_id
        LEFT JOIN companies co ON co.id=i.company_id
        WHERE i.location_id=? AND i.active=1
        ORDER BY i.name
    """, [loc_id])
    return jsonify({"location": dict(loc), "items": [dict(r) for r in items]})


@bp.route("/api/location/<int:loc_id>/users")
@login_required
@admin_required
def api_get_location_users(loc_id):
    """Return all active users with a flag for whether they're assigned to this location."""
    try:
        rows = query(
            "SELECT u.id, u.username, u.email, u.role, "
            "  CASE WHEN ul.location_id IS NOT NULL THEN 1 ELSE 0 END AS assigned "
            "FROM users u "
            "LEFT JOIN user_locations ul ON ul.user_id=u.id AND ul.location_id=? "
            "WHERE COALESCE(u.active, 1) = 1 ORDER BY u.role DESC, u.username",
            [loc_id])
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Could not load users: {e}"}), 500


@bp.route("/api/location/<int:loc_id>/users", methods=["POST"])
@login_required
@admin_required
def api_set_location_users(loc_id):
    """Replace user assignments for a location. Send user_ids: [] to clear all.
    Invalidates sessions of all affected users so their location_ids refresh on next login."""
    if not query("SELECT id FROM locations WHERE id=?", [loc_id], one=True):
        return jsonify({"ok": False, "msg": "Location not found"}), 404
    user_ids = (request.json or {}).get("user_ids", [])
    # Find all users currently or newly assigned — their sessions need refreshing
    old_ids = {r["user_id"] for r in query("SELECT user_id FROM user_locations WHERE location_id=?", [loc_id])}
    new_ids = set(int(u) for u in user_ids)
    affected = old_ids | new_ids
    execute("DELETE FROM user_locations WHERE location_id=?", [loc_id])
    for uid in user_ids:
        execute("INSERT OR IGNORE INTO user_locations (user_id, location_id) VALUES (?,?)", [uid, loc_id])
    # Invalidate sessions for affected users (excluding the current admin) so their
    # location_ids are refreshed on next login
    current_uid = session.get("user_id")
    for uid in affected:
        if uid != current_uid:
            execute("UPDATE users SET session_token=NULL WHERE id=?", [uid])
    log_action("SITE_USERS_UPDATE", detail=f"location_id={loc_id} users={user_ids}")
    return jsonify({"ok": True})


@bp.route("/api/user/<int:uid>/locations", methods=["GET"])
@login_required
@admin_required
def api_get_user_locations(uid):
    rows = query("SELECT location_id FROM user_locations WHERE user_id=?", [uid])
    return jsonify([r["location_id"] for r in rows])


@bp.route("/api/user/<int:uid>/locations", methods=["POST"])
@login_required
@admin_required
def api_set_user_locations(uid):
    """Replace the user's site assignments. Send location_ids: [] for unrestricted.
    Invalidates the affected user's session so location_ids refresh on next login."""
    user = query("SELECT id FROM users WHERE id=?", [uid], one=True)
    if not user:
        return jsonify({"ok": False, "msg": "User not found"}), 404
    loc_ids = request.json.get("location_ids", []) if request.json else []
    execute("DELETE FROM user_locations WHERE user_id=?", [uid])
    for lid in loc_ids:
        execute("INSERT OR IGNORE INTO user_locations (user_id, location_id) VALUES (?,?)", [uid, lid])
    # Invalidate the user's session so they re-login and pick up the new location_ids
    if uid != session.get("user_id"):
        execute("UPDATE users SET session_token=NULL WHERE id=?", [uid])
    log_action("USER_SITES_UPDATE", detail=f"user_id={uid} sites={loc_ids}")
    return jsonify({"ok": True})

@bp.route("/api/locations", methods=["POST"])
@login_required
@admin_required
def api_add_location():
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Name required"})
    try:
        lid = execute("INSERT INTO locations (name,description,email,created_at) VALUES (?,?,?,?)",
                      [name, d.get("description",""), (d.get("email") or "").strip(),
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        return jsonify({"ok": True, "id": lid, "name": name})
    except Exception:
        return jsonify({"ok": False, "msg": "Location name already exists"})

@bp.route("/api/location/<int:loc_id>", methods=["PUT"])
@login_required
@admin_required
def api_edit_location(loc_id):
    d = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Name required"})
    try:
        execute("UPDATE locations SET name=?, description=?, email=? WHERE id=?",
                [name, d.get("description", ""), (d.get("email") or "").strip(), loc_id])
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False, "msg": "Location name already exists"})


@bp.route("/api/location/<int:loc_id>", methods=["DELETE"])
@login_required
@admin_required
def api_delete_location(loc_id):
    execute("UPDATE items SET location_id=NULL WHERE location_id=?", [loc_id])
    execute("DELETE FROM user_locations WHERE location_id=?", [loc_id])
    execute("DELETE FROM locations WHERE id=?", [loc_id])
    return jsonify({"ok": True})


@bp.route("/api/location/<int:loc_id>/assign-items", methods=["POST"])
@login_required
@perm_required("write_items")
def api_assign_items_to_location(loc_id):
    """Bulk-assign a list of items to this site."""
    loc = query("SELECT id, name FROM locations WHERE id=?", [loc_id], one=True)
    if not loc:
        return jsonify({"ok": False, "msg": "Site not found"}), 404
    item_ids = (request.json or {}).get("item_ids", [])
    if not item_ids:
        return jsonify({"ok": False, "msg": "No items provided"})
    for iid in item_ids:
        execute("UPDATE items SET location_id=? WHERE id=? AND active=1", [loc_id, iid])
        log_action("ITEM_SITE_ASSIGN", item_id=iid,
                   detail=f"Assigned to site: {loc['name']}")
    return jsonify({"ok": True, "assigned": len(item_ids)})


@bp.route("/api/items/unassigned")
@login_required
def api_items_unassigned():
    """Items with no site assigned — used by the Assign Items modal."""
    rows = query("""
        SELECT i.id, i.name, i.serial, i.sku, i.internal_sku,
               c.name as category, c.color, p.name as product_name
        FROM items i
        LEFT JOIN categories c ON c.id=i.category_id
        LEFT JOIN products p   ON p.id=i.product_id
        WHERE i.location_id IS NULL AND i.active=1 AND i.sold=0
        ORDER BY i.name
    """)
    return jsonify([dict(r) for r in rows])


def _item_location_allowed(item_id):
    """Return True if the current user is allowed to access this item's location."""
    loc_ids = session.get("location_ids") or []
    if not loc_ids:
        return True
    item = query("SELECT location_id FROM items WHERE id=? AND active=1", [item_id], one=True)
    return item is not None and item["location_id"] in loc_ids


@bp.route("/api/item/<int:item_id>/notes")
@login_required
def api_item_notes(item_id):
    if not _item_location_allowed(item_id):
        return jsonify([])
    rows = query("SELECT * FROM item_notes WHERE item_id=? ORDER BY id DESC", [item_id])
    return jsonify([dict(r) for r in rows])

@bp.route("/api/item/<int:item_id>/note", methods=["POST"])
@login_required
@perm_required("write_items")
def api_add_note(item_id):
    if not _item_location_allowed(item_id):
        return jsonify({"ok": False, "msg": "Item not found"}), 403
    note = (request.json or {}).get("note", "").strip()
    if not note:
        return jsonify({"ok": False, "msg": "Note is required"})
    execute("INSERT INTO item_notes (item_id,note,username,created_at) VALUES (?,?,?,?)",
            [item_id, note, session.get("username","?"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    log_action("NOTE_ADD", item_id, None, note[:120])
    return jsonify({"ok": True})


# ── Item Reservations ─────────────────────────────────────────────────────────

@bp.route("/api/item/<int:item_id>/reservations")
@login_required
def api_item_reservations(item_id):
    if not _item_location_allowed(item_id):
        return jsonify([])
    rows = query("""SELECT * FROM item_reservations
                    WHERE item_id=? AND cancelled=0 ORDER BY reserved_from""", [item_id])
    return jsonify([dict(r) for r in rows])


@bp.route("/api/item/<int:item_id>/reservation", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_add_reservation(item_id):
    if not _item_location_allowed(item_id):
        return jsonify({"ok": False, "msg": "Item not found"}), 403
    d    = request.json or {}
    by   = (d.get("reserved_by") or "").strip()
    frm  = (d.get("reserved_from") or "").strip()
    to   = (d.get("reserved_to") or "").strip()
    if not by or not frm or not to:
        return jsonify({"ok": False, "msg": "reserved_by, reserved_from, reserved_to are required"})
    if frm > to:
        return jsonify({"ok": False, "msg": "Start date must be before end date"})
    # Conflict check — overlapping active reservations for same item
    conflict = query("""SELECT id FROM item_reservations
                        WHERE item_id=? AND cancelled=0
                          AND NOT (reserved_to < ? OR reserved_from > ?)""",
                     [item_id, frm, to], one=True)
    if conflict:
        return jsonify({"ok": False, "msg": "Conflicts with an existing reservation for this item"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rid = execute("""INSERT INTO item_reservations
                     (item_id,reserved_by,reserved_from,reserved_to,purpose,created_by,created_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  [item_id, by, frm, to, d.get("purpose",""), session.get("username","?"), now])
    log_action("RESERVATION_ADD", item_id, None,
               f"Reserved for {by} {frm}→{to}")
    return jsonify({"ok": True, "id": rid})


@bp.route("/api/reservation/<int:res_id>", methods=["DELETE"])
@login_required
def api_cancel_reservation(res_id):
    row = query("SELECT * FROM item_reservations WHERE id=?", [res_id], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"})
    execute("UPDATE item_reservations SET cancelled=1 WHERE id=?", [res_id])
    log_action("RESERVATION_CANCEL", row["item_id"], None,
               f"Reservation #{res_id} cancelled")
    return jsonify({"ok": True})


# ── Item Photos ───────────────────────────────────────────────────────────────

@bp.route("/api/item/<int:item_id>/photos")
@login_required
def api_item_photos(item_id):
    if not _item_location_allowed(item_id):
        return jsonify([])
    rows = query("SELECT id,caption,uploaded_by,created_at FROM item_photos WHERE item_id=? ORDER BY id",
                 [item_id])
    return jsonify([dict(r) for r in rows])


@bp.route("/api/item/<int:item_id>/photo", methods=["POST"])
@login_required
@perm_required("write_items")
def api_upload_photo(item_id):
    import base64
    f = request.files.get("photo")
    if not f:
        return jsonify({"ok": False, "msg": "No file uploaded"})
    if not f.content_type.startswith("image/"):
        return jsonify({"ok": False, "msg": "File must be an image"})
    data = f.read(3 * 1024 * 1024 + 1)  # read up to 3MB+1
    if len(data) > 3 * 1024 * 1024:
        return jsonify({"ok": False, "msg": "Image too large (max 3 MB)"})
    data_url = f"data:{f.content_type};base64,{base64.b64encode(data).decode()}"
    caption   = (request.form.get("caption") or "").strip() or None
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pid = execute("INSERT INTO item_photos (item_id,data_url,caption,uploaded_by,created_at) VALUES (?,?,?,?,?)",
                  [item_id, data_url, caption, session.get("username","?"), now])
    log_action("PHOTO_ADD", item_id, None, f"Photo #{pid} uploaded")
    return jsonify({"ok": True, "id": pid})


@bp.route("/api/photo/<int:photo_id>/data")
@login_required
def api_photo_data(photo_id):
    row = query("""SELECT ip.data_url, i.location_id
                   FROM item_photos ip
                   JOIN items i ON i.id = ip.item_id
                   WHERE ip.id=?""", [photo_id], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    loc_ids = session.get("location_ids") or []
    if loc_ids and row["location_id"] not in loc_ids:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    return jsonify({"ok": True, "data_url": row["data_url"]})


@bp.route("/api/photo/<int:photo_id>", methods=["DELETE"])
@login_required
@perm_required("write_items")
def api_delete_photo(photo_id):
    row = query("SELECT item_id FROM item_photos WHERE id=?", [photo_id], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"})
    execute("DELETE FROM item_photos WHERE id=?", [photo_id])
    log_action("PHOTO_DELETE", row["item_id"], None, f"Photo #{photo_id} deleted")
    return jsonify({"ok": True})


# ── Item History Timeline ─────────────────────────────────────────────────────

@bp.route("/api/item/<int:item_id>/timeline")
@login_required
def api_item_timeline(item_id):
    if not _item_location_allowed(item_id):
        return jsonify({"ok": False, "msg": "Not found"}), 404

    events = []

    # ── Audit log entries for this item ──────────────────────────────────────
    for r in query("""SELECT ts, action, detail, username, before_state, after_state
                      FROM audit_log WHERE item_id=? ORDER BY ts""", [item_id]):
        events.append({
            "ts":       r["ts"],
            "type":     "audit",
            "action":   r["action"],
            "detail":   r["detail"] or "",
            "actor":    r["username"] or "",
            "before":   r["before_state"],
            "after":    r["after_state"],
        })

    # ── Checkout log ─────────────────────────────────────────────────────────
    for r in query("""SELECT checkout_date, checkin_date, checked_out_by, job_ref,
                             checkin_by, checkin_note, expected_return_date
                      FROM checkout_log WHERE item_id=? ORDER BY checkout_date""", [item_id]):
        events.append({
            "ts":     r["checkout_date"] or "",
            "type":   "checkout",
            "action": "CHECKOUT",
            "detail": f"Checked out to {r['checked_out_by'] or '?'}"
                      + (f" · Job: {r['job_ref']}" if r["job_ref"] else "")
                      + (f" · Due: {r['expected_return_date'][:10]}" if r["expected_return_date"] else ""),
            "actor":  r["checked_out_by"] or "",
        })
        if r["checkin_date"]:
            events.append({
                "ts":     r["checkin_date"],
                "type":   "checkin",
                "action": "CHECKIN",
                "detail": f"Returned by {r['checkin_by'] or '?'}"
                          + (f" · {r['checkin_note']}" if r["checkin_note"] else ""),
                "actor":  r["checkin_by"] or "",
            })

    # ── Field edits ──────────────────────────────────────────────────────────
    for r in query("""SELECT ts, modified_by, field_changed, old_value, new_value, notes
                      FROM item_modifications WHERE item_id=? ORDER BY ts""", [item_id]):
        label = r["field_changed"].replace("_", " ").title()
        events.append({
            "ts":     r["ts"],
            "type":   "edit",
            "action": "EDIT",
            "detail": f"{label}: {r['old_value'] or '—'} → {r['new_value'] or '—'}"
                      + (f" · {r['notes']}" if r["notes"] else ""),
            "actor":  r["modified_by"] or "",
        })

    # ── Notes ────────────────────────────────────────────────────────────────
    for r in query("""SELECT created_at, username, note
                      FROM item_notes WHERE item_id=? ORDER BY created_at""", [item_id]):
        events.append({
            "ts":     r["created_at"],
            "type":   "note",
            "action": "NOTE",
            "detail": r["note"] or "",
            "actor":  r["username"] or "",
        })

    # ── Service log ──────────────────────────────────────────────────────────
    for r in query("""SELECT service_date, service_type, performed_by, provider,
                             notes, next_service_date, created_by
                      FROM service_log WHERE item_id=? ORDER BY service_date""", [item_id]):
        events.append({
            "ts":     r["service_date"],
            "type":   "service",
            "action": "SERVICE",
            "detail": f"{r['service_type']}"
                      + (f" · by {r['performed_by']}" if r["performed_by"] else "")
                      + (f" · {r['provider']}" if r["provider"] else "")
                      + (f" · {r['notes']}" if r["notes"] else "")
                      + (f" · Next: {r['next_service_date']}" if r["next_service_date"] else ""),
            "actor":  r["created_by"] or r["performed_by"] or "",
        })

    # ── Photos ───────────────────────────────────────────────────────────────
    for r in query("""SELECT created_at, uploaded_by, caption
                      FROM item_photos WHERE item_id=? ORDER BY created_at""", [item_id]):
        events.append({
            "ts":     r["created_at"],
            "type":   "photo",
            "action": "PHOTO",
            "detail": f"Photo added" + (f": {r['caption']}" if r["caption"] else ""),
            "actor":  r["uploaded_by"] or "",
        })

    # Sort all events by timestamp, most recent last
    events.sort(key=lambda e: e["ts"] or "")
    return jsonify({"ok": True, "events": events})


# ── Import history ────────────────────────────────────────────────────────────

@bp.route("/api/import-history")
@login_required
@admin_required
def api_import_history():
    vendor  = request.args.get("vendor", "").strip()
    site_q  = request.args.get("site", "").strip()
    from_d  = request.args.get("from", "").strip()
    to_d    = request.args.get("to", "").strip()
    page    = max(1, int(request.args.get("page", 1) or 1))
    per_page = 50

    sql  = """SELECT ii.*, l.name as site_name
              FROM invoice_imports ii
              LEFT JOIN locations l ON l.id = ii.site_id
              WHERE 1=1"""
    args = []
    if vendor:
        sql += " AND (ii.vendor LIKE ? OR ii.reference LIKE ?)"; args += [f"%{vendor}%", f"%{vendor}%"]
    if site_q:
        sql += " AND l.name LIKE ?"; args.append(f"%{site_q}%")
    if from_d:
        sql += " AND ii.imported_at >= ?"; args.append(from_d)
    if to_d:
        sql += " AND ii.imported_at < date(?, '+1 day')"; args.append(to_d)

    where_part = sql[sql.index("FROM"):]
    total = query("SELECT COUNT(*) " + where_part, args, one=True)[0]
    total_items = query("SELECT COALESCE(SUM(ii.line_count),0) " + where_part, args, one=True)[0]

    sql += " ORDER BY ii.imported_at DESC"
    sql += f" LIMIT {per_page} OFFSET {(page-1)*per_page}"

    rows = [dict(r) for r in query(sql, args)]
    pages = max(1, -(-total // per_page))
    return jsonify({"ok": True, "rows": rows, "total": total,
                    "total_items": int(total_items or 0),
                    "page": page, "pages": pages})


# ── Reports ───────────────────────────────────────────────────────────────────

@bp.route("/api/report/checkout-history")
@login_required
@perm_required("view_audit")
def api_report_checkout_history():
    tech    = request.args.get("tech", "").strip()
    item_q  = request.args.get("item", "").strip()
    from_d  = request.args.get("from", "")
    to_d    = request.args.get("to", "")
    page    = max(1, int(request.args.get("page", 1) or 1))
    per     = 50
    sql  = "FROM checkout_log WHERE 1=1"
    args = []
    if tech:
        sql += " AND LOWER(checked_out_by) LIKE ?"; args.append(f"%{tech.lower()}%")
    if item_q:
        sql += " AND LOWER(item_name) LIKE ?"; args.append(f"%{item_q.lower()}%")
    if from_d:
        sql += " AND checkout_date >= ?"; args.append(from_d)
    if to_d:
        sql += " AND checkout_date <= ?"; args.append(to_d)
    total = query(f"SELECT COUNT(*) {sql}", args, one=True)[0]
    rows  = query(f"SELECT * {sql} ORDER BY id DESC LIMIT {per} OFFSET {(page-1)*per}", args)
    return jsonify({
        "ok": True, "total": total, "page": page, "pages": max(1, -(-total // per)),
        "rows": [dict(r) for r in rows]
    })


@bp.route("/api/report/locations")
@login_required
def api_report_locations():
    allowed_ids = session.get("location_ids") or []  # empty = no restriction
    locs = query("SELECT * FROM locations ORDER BY name")
    if allowed_ids:
        locs = [l for l in locs if l["id"] in allowed_ids]
    result = []
    for loc in locs:
        items = query("""SELECT i.id, i.name, i.serial, i.sku, i.internal_sku,
                                i.condition, i.checked_out, i.checkout_by, i.cost_price,
                                i.expected_return_date, i.next_maintenance_date,
                                c.name as category, c.color
                         FROM items i
                         LEFT JOIN categories c ON c.id=i.category_id
                         WHERE i.location_id=? AND i.active=1 AND i.sold=0
                         ORDER BY i.name""", [loc["id"]])
        today = datetime.now().strftime("%Y-%m-%d")
        item_list = []
        for it in items:
            d = dict(it)
            d["overdue"] = bool(d.get("checked_out") and d.get("expected_return_date") and
                                d["expected_return_date"] < today)
            item_list.append(d)
        result.append({**dict(loc), "items": item_list,
                        "total": len(item_list),
                        "checked_out": sum(1 for i in item_list if i["checked_out"])})
    # Unassigned items — only show to unrestricted users (admins/regional managers)
    if not allowed_ids:
        unassigned_rows = [dict(r) for r in query("""SELECT i.id, i.name, i.serial, i.sku, i.internal_sku,
                                     i.condition, i.checked_out, i.checkout_by, i.cost_price,
                                     c.name as category, c.color
                              FROM items i
                              LEFT JOIN categories c ON c.id=i.category_id
                              WHERE (i.location_id IS NULL) AND i.active=1 AND i.sold=0
                              ORDER BY i.name""")]
        result.append({"id": None, "name": "Unassigned", "description": "",
                       "items": unassigned_rows,
                       "total": len(unassigned_rows),
                   "checked_out": sum(1 for r in unassigned_rows if r["checked_out"])})
    return jsonify({"ok": True, "locations": result})


@bp.route("/api/qty_adjust", methods=["POST"])
@login_required
@perm_required("qty_adjust")
def api_qty_adjust():
    d    = request.json
    item = query("SELECT * FROM items WHERE id=? AND active=1", [d["id"]], one=True)
    if not item or item["qty"] is None:
        return jsonify({"ok": False, "msg": "Not qty-tracked"})
    before = {"qty": item["qty"], "qty_out": item["qty_out"]}
    amount = int(d.get("amount", 0))
    note   = d.get("note", "")
    action = d.get("action")
    if action == "add":
        execute("UPDATE items SET qty=qty+? WHERE id=?", [amount, item["id"]])
        nq = query("SELECT qty FROM items WHERE id=?", [item["id"]], one=True)["qty"]
        log_action("QTY_ADD", item["id"], item["name"], f"+{amount}. {note}. Total:{nq}", before, {"qty": nq})
    elif action == "remove":
        execute("UPDATE items SET qty_out=qty_out+? WHERE id=?", [amount, item["id"]])
        u  = query("SELECT qty, qty_out FROM items WHERE id=?", [item["id"]], one=True)
        log_action("QTY_REMOVE", item["id"], item["name"], f"-{amount}. {note}. Left:{u['qty']-(u['qty_out'] or 0)}", before, {"qty_out": u["qty_out"]})
        notify_low_stock_if_needed(item.get("product_id"))
    elif action == "set":
        execute("UPDATE items SET qty=?,qty_out=0 WHERE id=?", [amount, item["id"]])
        log_action("QTY_SET", item["id"], item["name"], f"Set to {amount}. {note}", before, {"qty": amount})
        notify_low_stock_if_needed(item.get("product_id"))
    u = query("SELECT qty,qty_out FROM items WHERE id=?", [item["id"]], one=True)
    return jsonify({"ok": True, "qty": u["qty"], "qty_out": u["qty_out"]})


@bp.route("/api/item/sell", methods=["POST"])
@login_required
@perm_required("sell_items")
def api_item_sell():
    d    = request.json
    item = query("SELECT * FROM items WHERE id=? AND active=1", [d["id"]], one=True)
    if not item:            return jsonify({"ok": False, "msg": "Not found"})
    if item["checked_out"]: return jsonify({"ok": False, "msg": "Item is currently checked out"})
    loc_ids = session.get("location_ids") or []
    if loc_ids and item["location_id"] not in loc_ids:
        return jsonify({"ok": False, "msg": "Not found"})
    price   = float(d.get("price") or item["sale_price"] or 0)
    sold_to = d.get("sold_to", "").strip()
    now     = datetime.now().strftime("%Y-%m-%d")
    execute("UPDATE items SET sold=1,sold_date=?,sold_price=?,sold_to=?,checked_out=0 WHERE id=?",
            [now, price, sold_to, item["id"]])
    profit = round(price - (item["cost_price"] or 0), 2)
    log_action("ITEM_SOLD", item["id"], item["name"],
               f"Sold to: {sold_to or 'unknown'} | Price: ${price:.2f} | Profit: ${profit:.2f}",
               {"sold": 0}, {"sold": 1, "price": price})
    notify_low_stock_if_needed(item.get("product_id"))
    return jsonify({"ok": True, "profit": profit})


# ── Retirement ───────────────────────────────────────────────────────────────

@bp.route("/api/item/retire", methods=["POST"])
@login_required
@perm_required("write_items")
def api_item_retire():
    d      = request.json or {}
    iid    = d.get("id")
    method = (d.get("method") or "").strip()
    notes  = (d.get("notes") or "").strip() or None
    book_v = float(d["final_book_value"]) if d.get("final_book_value") not in (None, "") else None
    undo   = bool(d.get("undo", False))

    item = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
    if not item:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    loc_ids = session.get("location_ids") or []
    if loc_ids and item["location_id"] not in loc_ids:
        return jsonify({"ok": False, "msg": "Not found"}), 404

    if undo:
        execute("UPDATE items SET retired=0,retired_at=NULL,retirement_method=NULL,"
                "retirement_notes=NULL,final_book_value=NULL WHERE id=?", [iid])
        log_action("ITEM_UNRETIRED", iid, item["name"], "Retirement reversed")
        return jsonify({"ok": True})

    if item["checked_out"]:
        return jsonify({"ok": False, "msg": "Check item in before retiring it"})
    now = datetime.now().strftime("%Y-%m-%d")
    execute("UPDATE items SET retired=1,retired_at=?,retirement_method=?,retirement_notes=?,"
            "final_book_value=?,checked_out=0 WHERE id=?",
            [now, method or None, notes, book_v, iid])
    log_action("ITEM_RETIRED", iid, item["name"],
               f"Method: {method or 'unspecified'} | {notes or ''}")
    return jsonify({"ok": True})


# ── Out of service ────────────────────────────────────────────────────────────

@bp.route("/api/item/out-of-service", methods=["POST"])
@login_required
@perm_required("write_items")
def api_item_out_of_service():
    d      = request.json or {}
    iid    = d.get("id")
    active = bool(d.get("active", True))   # True = mark OOS, False = clear
    reason = (d.get("reason") or "").strip() or None
    item   = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
    if not item:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    loc_ids = session.get("location_ids") or []
    if loc_ids and item["location_id"] not in loc_ids:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    execute("UPDATE items SET out_of_service=?, out_of_service_reason=? WHERE id=?",
            [1 if active else 0, reason if active else None, iid])
    if active:
        log_action("OUT_OF_SERVICE", iid, item["name"], reason or "Marked out of service")
    else:
        log_action("RETURNED_TO_SERVICE", iid, item["name"], "Returned to service")
    return jsonify({"ok": True})


# ── Recall flag ───────────────────────────────────────────────────────────────

@bp.route("/api/item/recall", methods=["POST"])
@login_required
@perm_required("write_items")
def api_item_recall():
    d      = request.json or {}
    iid    = d.get("id")
    active = bool(d.get("active", True))
    notes  = (d.get("notes") or "").strip() or None
    item   = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
    if not item:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    loc_ids = session.get("location_ids") or []
    if loc_ids and item["location_id"] not in loc_ids:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    execute("UPDATE items SET recall_flag=?, recall_notes=? WHERE id=?",
            [1 if active else 0, notes if active else None, iid])
    if active:
        log_action("RECALL_FLAGGED", iid, item["name"], notes or "Flagged under recall")
    else:
        log_action("RECALL_CLEARED", iid, item["name"], "Recall flag cleared")
    return jsonify({"ok": True})


# ── Service log ───────────────────────────────────────────────────────────────

@bp.route("/api/item/<int:item_id>/service-log")
@login_required
def api_item_service_log(item_id):
    if not _item_location_allowed(item_id):
        return jsonify([])
    rows = query("SELECT * FROM service_log WHERE item_id=? ORDER BY service_date DESC, id DESC",
                 [item_id])
    return jsonify([dict(r) for r in rows])


@bp.route("/api/item/<int:item_id>/service-log", methods=["POST"])
@login_required
@perm_required("write_items")
def api_add_service_log(item_id):
    if not _item_location_allowed(item_id):
        return jsonify({"ok": False, "msg": "Item not found"}), 403
    d            = request.json or {}
    service_date = (d.get("service_date") or "").strip()
    service_type = (d.get("service_type") or "").strip()
    if not service_date or not service_type:
        return jsonify({"ok": False, "msg": "service_date and service_type are required"})
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rid  = execute(
        "INSERT INTO service_log (item_id,service_date,service_type,performed_by,provider,"
        "notes,next_service_date,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [item_id, service_date, service_type,
         (d.get("performed_by") or "").strip() or None,
         (d.get("provider") or "").strip() or None,
         (d.get("notes") or "").strip() or None,
         (d.get("next_service_date") or "").strip() or None,
         session.get("username", "?"), now])
    # If a next_service_date was given, write it into extra_fields and re-sync maintenance tasks
    next_svc = (d.get("next_service_date") or "").strip()
    if next_svc:
        item = query("SELECT extra_fields FROM items WHERE id=?", [item_id], one=True)
        if item:
            try:
                ef = json.loads(item["extra_fields"] or "{}")
            except Exception:
                ef = {}
            ef["next_service"] = next_svc
            execute("UPDATE items SET extra_fields=? WHERE id=?",
                    [json.dumps(ef), item_id])
            sync_maintenance_tasks(item_id)
    log_action("SERVICE_LOG", item_id, None,
               f"{service_type} on {service_date}" +
               (f" by {d.get('performed_by')}" if d.get("performed_by") else ""))
    return jsonify({"ok": True, "id": rid})


@bp.route("/api/item/add", methods=["POST"])
@login_required
@perm_required("write_items")
def api_item_add():
    allowed, limit_msg = _check_item_limit()
    if not allowed:
        return jsonify({"ok": False, "msg": limit_msg})
    d = request.json
    if not d.get("name", "").strip():
        return jsonify({"ok": False, "msg": "Name required"})
    cat_id  = d.get("category_id")
    prod_id = d.get("product_id")
    if cat_id  and not query("SELECT id FROM categories WHERE id=?", [cat_id],  one=True):
        return jsonify({"ok": False, "msg": "Invalid category"})
    if prod_id and not query("SELECT id FROM products WHERE id=?",   [prod_id], one=True):
        return jsonify({"ok": False, "msg": "Invalid product"})
    if prod_id:
        prod = query("SELECT require_serial, require_vendor_sku FROM products WHERE id=?", [prod_id], one=True)
        if prod:
            if prod["require_serial"]     and not (d.get("serial") or "").strip():
                return jsonify({"ok": False, "msg": "Serial number is required for this product", "field": "serial"})
            if prod["require_vendor_sku"] and not (d.get("sku")    or "").strip():
                return jsonify({"ok": False, "msg": "Vendor SKU is required for this product",   "field": "sku"})
    serial = (d.get("serial") or "").strip()
    if serial:
        # Serial uniqueness is scoped to the same product — the same SN can exist on different products
        if prod_id:
            dup = query("SELECT id,name FROM items WHERE serial=? AND product_id=? AND active=1", [serial, prod_id], one=True)
        else:
            dup = query("SELECT id,name FROM items WHERE serial=? AND product_id IS NULL AND active=1", [serial], one=True)
        if dup: return jsonify({"ok": False, "msg": f"Duplicate serial — already on item #{dup['id']} ({dup['name']})"})
    sku = (d.get("sku") or "").strip()
    if sku:
        dup = query("SELECT id,name FROM items WHERE sku=? AND active=1", [sku], one=True)
        if dup: return jsonify({"ok": False, "msg": f"Duplicate SKU — already on item #{dup['id']} ({dup['name']})"})
    try:
        iid   = _save_item(d)
        log_action("ITEM_ADD", iid, d.get("name"), f"Shelf {d.get('shelf')}")
        saved = query("SELECT * FROM items WHERE id=?", [iid], one=True)
        if saved:
            sync_item_task(iid, d.get("name", ""), _item_missing_fields(dict(saved)))
            sync_maintenance_tasks(iid)
        try:
            from app.webhooks import fire as _wh
            _wh("item.added", {"id": iid, "name": d.get("name"), "serial": d.get("serial")})
        except Exception:
            pass
        return jsonify({"ok": True, "id": iid})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Save failed: {str(e)}"})


@bp.route("/api/item/edit", methods=["POST"])
@login_required
@perm_required("write_items")
def api_item_edit():
    d    = request.json
    if not d.get("id"): return jsonify({"ok": False, "msg": "ID required"})
    item = query("SELECT * FROM items WHERE id=? AND active=1", [d["id"]], one=True)
    if not item:        return jsonify({"ok": False, "msg": "Not found"})
    if not d.get("name", "").strip(): return jsonify({"ok": False, "msg": "Name required"})
    cat_id = d.get("category_id")
    if cat_id and not query("SELECT id FROM categories WHERE id=?", [cat_id], one=True):
        return jsonify({"ok": False, "msg": "Invalid category"})
    serial = (d.get("serial") or "").strip()
    if serial:
        # Only check uniqueness if the serial is actually changing
        current_serial = (item["serial"] or "").strip()
        if serial != current_serial:
            edit_prod_id = d.get("product_id") or item["product_id"]
            if edit_prod_id:
                dup = query("SELECT id,name FROM items WHERE serial=? AND product_id=? AND active=1 AND id!=?", [serial, edit_prod_id, d["id"]], one=True)
            else:
                dup = query("SELECT id,name FROM items WHERE serial=? AND product_id IS NULL AND active=1 AND id!=?", [serial, d["id"]], one=True)
            if dup: return jsonify({"ok": False, "msg": f"Duplicate serial — already on item #{dup['id']} ({dup['name']})"})
    sku = (d.get("sku") or "").strip()
    if sku:
        # Only check uniqueness if the SKU is actually changing — avoids blocking
        # edits on items that already share a SKU from legacy data or bulk import.
        current_sku = (item["sku"] or "").strip()
        if sku != current_sku:
            dup = query("SELECT id,name FROM items WHERE sku=? AND active=1 AND id!=?", [sku, d["id"]], one=True)
            if dup: return jsonify({"ok": False, "msg": f"Duplicate SKU — already on item #{dup['id']} ({dup['name']})"})
    try:
        _save_item(d, d["id"])
        log_action("ITEM_EDIT", d["id"], d.get("name"), "Edited", dict(item), d)
        saved = query("SELECT * FROM items WHERE id=?", [d["id"]], one=True)
        if saved:
            sync_item_task(d["id"], d.get("name", ""), _item_missing_fields(dict(saved)))
            sync_maintenance_tasks(d["id"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Save failed: {str(e)}"})


@bp.route("/api/item/delete", methods=["POST"])
@login_required
@perm_required("delete_items")
def api_item_delete():
    d           = request.json
    ids         = d.get("ids") or ([d["id"]] if d.get("id") else [])
    loc_ids     = session.get("location_ids") or []
    deleted     = 0
    product_ids = set()
    for iid in ids:
        item = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
        if not item:
            continue
        if loc_ids and item["location_id"] not in loc_ids:
            continue
        execute("UPDATE items SET active=0 WHERE id=?", [iid])
        execute("DELETE FROM tasks WHERE item_id=?", [iid])
        log_action("ITEM_DELETE", iid, item["name"], "Deleted")
        deleted += 1
        if item.get("product_id"):
            product_ids.add(item["product_id"])
    for pid in product_ids:
        notify_low_stock_if_needed(pid)
    return jsonify({"ok": True, "deleted": deleted})


# ── Bulk operations ───────────────────────────────────────────────────────────

@bp.route("/api/items/bulk-checkout", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_bulk_checkout():
    d    = request.json
    ids  = [int(i) for i in (d.get("ids") or [])]
    who  = d.get("who", "").strip()
    job  = d.get("job_ref", "").strip()
    ret  = d.get("expected_return_date") or None
    if not ids:  return jsonify({"ok": False, "msg": "No items selected"})
    if not who:  return jsonify({"ok": False, "msg": "Checked-out-to is required"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    loc_ids = session.get("location_ids") or []
    done, skipped = 0, 0
    product_ids = set()
    for iid in ids:
        item = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
        if not item or item["checked_out"] or item["qty"] is not None or item["sold"]:
            skipped += 1; continue
        if loc_ids and item["location_id"] not in loc_ids:
            skipped += 1; continue
        if item.get("out_of_service") or item.get("recall_flag"):
            skipped += 1; continue
        execute("UPDATE items SET checked_out=1, checkout_by=?, checkout_date=?, job_ref=?, expected_return_date=?, checkout_dept=NULL WHERE id=?",
                [who, now, job or None, ret, iid])
        execute("INSERT INTO checkout_log (item_id,item_name,checked_out_by,job_ref,checkout_date,created_at) VALUES (?,?,?,?,?,?)",
                [iid, item["name"], who, job or None, now[:10], now])
        log_action("CHECKOUT", iid, item["name"], f"To: {who}{' | Job: '+job if job else ''}")
        done += 1
        if item.get("product_id"):
            product_ids.add(item["product_id"])
    for pid in product_ids:
        notify_low_stock_if_needed(pid)
    return jsonify({"ok": True, "done": done, "skipped": skipped})


@bp.route("/api/items/bulk-checkin", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_bulk_checkin():
    d    = request.json
    ids  = [int(i) for i in (d.get("ids") or [])]
    note = d.get("checkin_note", "").strip() or None
    if not ids: return jsonify({"ok": False, "msg": "No items selected"})
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user    = session.get("username", "")
    loc_ids = session.get("location_ids") or []
    done, skipped = 0, 0
    for iid in ids:
        item = query("SELECT * FROM items WHERE id=? AND active=1 AND checked_out=1", [iid], one=True)
        if not item: skipped += 1; continue
        if loc_ids and item["location_id"] not in loc_ids:
            skipped += 1; continue
        checkout_dt = item["checkout_date"] or now
        dur = None
        for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%m/%d/%y", 8)):
            try:
                dur = round((datetime.now() - datetime.strptime(checkout_dt[:width], fmt)).total_seconds() / 3600, 2)
                break
            except Exception:
                pass
        execute("UPDATE items SET checked_out=0, checkout_by=NULL, checkout_date=NULL, job_ref=NULL, expected_return_date=NULL, checkout_dept=NULL WHERE id=?", [iid])
        execute("""UPDATE checkout_log SET checkin_date=?, checkin_note=?, checkin_by=?, duration_hours=?
                   WHERE item_id=? AND checkin_date IS NULL""",
                [now[:10], note, user, dur, iid])
        log_action("CHECKIN", iid, item["name"], f"Note: {note}" if note else "")
        done += 1
    return jsonify({"ok": True, "done": done, "skipped": skipped})


@bp.route("/api/items/bulk-edit", methods=["POST"])
@login_required
@perm_required("write_items")
def api_bulk_edit():
    d   = request.json
    ids = [int(i) for i in (d.get("ids") or [])]
    if not ids: return jsonify({"ok": False, "msg": "No items selected"})
    updates, args = [], []
    if d.get("condition") not in (None, ""):
        updates.append("condition=?"); args.append(d["condition"])
    if d.get("shelf") not in (None, ""):
        updates.append("shelf=?"); args.append(d["shelf"])
    if d.get("location_id") not in (None, ""):
        updates.append("location_id=?"); args.append(int(d["location_id"]) if d["location_id"] else None)
    if d.get("tags") is not None:
        cleaned = ",".join(t.strip() for t in str(d["tags"]).split(",") if t.strip())
        updates.append("tags=?"); args.append(cleaned)
    if not updates: return jsonify({"ok": False, "msg": "Nothing to update"})
    placeholders = ",".join("?" for _ in ids)
    execute(f"UPDATE items SET {', '.join(updates)} WHERE id IN ({placeholders}) AND active=1",
            args + ids)
    for iid in ids:
        item = query("SELECT name FROM items WHERE id=?", [iid], one=True)
        if item: log_action("ITEM_EDIT", iid, item["name"], f"Bulk edit: {', '.join(updates)}")
    return jsonify({"ok": True, "updated": len(ids)})


# ── Clone item ────────────────────────────────────────────────────────────────

@bp.route("/api/item/<int:iid>/clone", methods=["POST"])
@login_required
@perm_required("write_items")
def api_item_clone(iid):
    allowed, limit_msg = _check_item_limit()
    if not allowed: return jsonify({"ok": False, "msg": limit_msg})
    item = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
    if not item: return jsonify({"ok": False, "msg": "Item not found"})
    d = dict(item)
    # Clear identity fields that must be unique
    d.pop("id", None); d.pop("serial", None); d.pop("sku", None)
    d.pop("internal_sku", None); d.pop("created_at", None)
    d["checked_out"] = 0; d["checkout_by"] = None; d["checkout_date"] = None
    d["job_ref"] = None; d["sold"] = 0; d["sold_date"] = None
    d["expected_return_date"] = None; d["kit_id"] = None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d["created_at"] = now
    cols = ", ".join(d.keys())
    placeholders = ", ".join("?" for _ in d)
    new_id = execute(f"INSERT INTO items ({cols}) VALUES ({placeholders})", list(d.values()))
    _ensure_internal_sku(new_id, d.get("category_id"))
    log_action("ITEM_ADD", new_id, item["name"], f"Cloned from #{iid}")
    return jsonify({"ok": True, "id": new_id})


# ── Tags autocomplete ─────────────────────────────────────────────────────────

@bp.route("/api/tags")
@login_required
def api_tags():
    rows = query("SELECT tags FROM items WHERE active=1 AND tags != ''")
    tag_set = set()
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t: tag_set.add(t)
    return jsonify(sorted(tag_set))


# ── Kits ──────────────────────────────────────────────────────────────────────

@bp.route("/api/kits", methods=["GET"])
@login_required
def api_kits():
    kits = query("""SELECT k.*, COUNT(i.id) as item_count,
                          SUM(CASE WHEN i.checked_out=0 THEN 1 ELSE 0 END) as available_count
                   FROM kits k LEFT JOIN items i ON i.kit_id=k.id AND i.active=1
                   GROUP BY k.id ORDER BY k.name""")
    return jsonify([dict(r) for r in kits])


@bp.route("/api/kits", methods=["POST"])
@login_required
@perm_required("write_items")
def api_kit_create():
    d    = request.json
    name = d.get("name", "").strip()
    if not name: return jsonify({"ok": False, "msg": "Name required"})
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kid  = execute("INSERT INTO kits (name,description,created_by,created_at) VALUES (?,?,?,?)",
                   [name, d.get("description", "") or None, session.get("username"), now])
    return jsonify({"ok": True, "id": kid, "name": name})


@bp.route("/api/kit/<int:kid>", methods=["PUT"])
@login_required
@perm_required("write_items")
def api_kit_update(kid):
    d = request.json
    name = d.get("name", "").strip()
    if not name: return jsonify({"ok": False, "msg": "Name required"})
    execute("UPDATE kits SET name=?, description=? WHERE id=?",
            [name, d.get("description", "") or None, kid])
    return jsonify({"ok": True})


@bp.route("/api/kit/<int:kid>", methods=["DELETE"])
@login_required
@perm_required("write_items")
def api_kit_delete(kid):
    execute("UPDATE items SET kit_id=NULL WHERE kit_id=?", [kid])
    execute("DELETE FROM kits WHERE id=?", [kid])
    return jsonify({"ok": True})


@bp.route("/api/kit/<int:kid>/items", methods=["GET"])
@login_required
def api_kit_items(kid):
    kit = query("SELECT * FROM kits WHERE id=?", [kid], one=True)
    if not kit: return jsonify({"ok": False, "msg": "Kit not found"}), 404
    items = query("""SELECT i.id, i.name, i.serial, i.sku, i.internal_sku,
                            i.condition, i.checked_out, i.checkout_by,
                            i.shelf, c.name as category, c.color
                     FROM items i LEFT JOIN categories c ON c.id=i.category_id
                     WHERE i.kit_id=? AND i.active=1 ORDER BY i.name""", [kid])
    return jsonify({"ok": True, "kit": dict(kit), "items": [dict(r) for r in items]})


@bp.route("/api/kit/<int:kid>/item/<int:iid>", methods=["POST"])
@login_required
@perm_required("write_items")
def api_kit_add_item(kid, iid):
    kit  = query("SELECT id,name FROM kits WHERE id=?", [kid], one=True)
    item = query("SELECT id,name FROM items WHERE id=? AND active=1", [iid], one=True)
    if not kit or not item: return jsonify({"ok": False, "msg": "Not found"}), 404
    execute("UPDATE items SET kit_id=? WHERE id=?", [kid, iid])
    log_action("ITEM_EDIT", iid, item["name"], f"Added to kit: {kit['name']}")
    return jsonify({"ok": True})


@bp.route("/api/kit/<int:kid>/item/<int:iid>", methods=["DELETE"])
@login_required
@perm_required("write_items")
def api_kit_remove_item(kid, iid):
    item = query("SELECT name FROM items WHERE id=?", [iid], one=True)
    execute("UPDATE items SET kit_id=NULL WHERE id=? AND kit_id=?", [iid, kid])
    if item: log_action("ITEM_EDIT", iid, item["name"], "Removed from kit")
    return jsonify({"ok": True})


@bp.route("/api/kit/<int:kid>/checkout", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_kit_checkout(kid):
    d   = request.json
    who = d.get("who", "").strip()
    job = d.get("job_ref", "").strip()
    ret = d.get("expected_return_date") or None
    if not who: return jsonify({"ok": False, "msg": "Checked-out-to is required"})
    kit = query("SELECT name FROM kits WHERE id=?", [kid], one=True)
    if not kit: return jsonify({"ok": False, "msg": "Kit not found"}), 404
    items = query("SELECT * FROM items WHERE kit_id=? AND active=1 AND checked_out=0 AND qty IS NULL AND sold=0", [kid])
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    done  = 0
    for item in items:
        execute("UPDATE items SET checked_out=1, checkout_by=?, checkout_date=?, job_ref=?, expected_return_date=?, checkout_dept=NULL WHERE id=?",
                [who, now, job or None, ret, item["id"]])
        execute("INSERT INTO checkout_log (item_id,item_name,checked_out_by,job_ref,checkout_date,created_at) VALUES (?,?,?,?,?,?)",
                [item["id"], item["name"], who, job or None, now[:10], now])
        log_action("CHECKOUT", item["id"], item["name"], f"Kit: {kit['name']} | To: {who}")
        done += 1
    return jsonify({"ok": True, "done": done})


@bp.route("/api/kit/<int:kid>/checkin", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_kit_checkin(kid):
    note = (request.json or {}).get("checkin_note", "").strip() or None
    kit  = query("SELECT name FROM kits WHERE id=?", [kid], one=True)
    if not kit: return jsonify({"ok": False, "msg": "Kit not found"}), 404
    items = query("SELECT * FROM items WHERE kit_id=? AND active=1 AND checked_out=1", [kid])
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user  = session.get("username", "")
    done  = 0
    for item in items:
        try:
            dur = round((datetime.now() - datetime.strptime((item["checkout_date"] or now)[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600, 2)
        except Exception:
            dur = None
        execute("UPDATE items SET checked_out=0, checkout_by=NULL, checkout_date=NULL, job_ref=NULL, expected_return_date=NULL, checkout_dept=NULL WHERE id=?", [item["id"]])
        execute("UPDATE checkout_log SET checkin_date=?, checkin_note=?, checkin_by=?, duration_hours=? WHERE item_id=? AND checkin_date IS NULL",
                [now[:10], note, user, dur, item["id"]])
        log_action("CHECKIN", item["id"], item["name"], f"Kit: {kit['name']}")
        done += 1
    return jsonify({"ok": True, "done": done})


@bp.route("/api/item/modifications")
@login_required
def api_item_mods_get():
    iid = request.args.get("item_id")
    if not iid: return jsonify({"ok": False, "msg": "item_id required"})
    mods = query("""SELECT m.*, i.name as spawned_name
                    FROM item_modifications m
                    LEFT JOIN items i ON i.id = m.spawned_item_id
                    WHERE m.item_id = ? ORDER BY m.id DESC""", [iid])
    return jsonify([dict(r) for r in mods])


@bp.route("/api/item/modify", methods=["POST"])
@login_required
@perm_required("write_items")
def api_item_modify():
    d   = request.json
    iid = d.get("item_id")
    if not iid: return jsonify({"ok": False, "msg": "item_id required"})
    item = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
    if not item: return jsonify({"ok": False, "msg": "Item not found"})
    loc_ids = session.get("location_ids") or []
    if loc_ids and item["location_id"] not in loc_ids:
        return jsonify({"ok": False, "msg": "Item not found"})
    field   = d.get("field_changed", "").strip()
    old_val = d.get("old_value", "")
    new_val = d.get("new_value", "")
    notes   = d.get("notes", "")
    if not field: return jsonify({"ok": False, "msg": "field_changed required"})
    if field in {"notes", "condition", "shelf", "sale_price", "cost_price"}:
        try:
            execute(f"UPDATE items SET {field}=? WHERE id=?", [new_val or None, iid])
        except Exception as e:
            return jsonify({"ok": False, "msg": f"Field update failed: {e}"})
    spawned_id = None
    spawn_data = d.get("spawn_item")
    if spawn_data and spawn_data.get("name", "").strip():
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        spawned_id = execute(
            "INSERT INTO items (name,product_id,category_id,condition,shelf,cost_price,"
            "notes,parent_item_id,created_at,active) VALUES (?,?,?,?,?,?,?,?,?,1)",
            [spawn_data["name"].strip(), item["product_id"],
             spawn_data.get("category_id") or item["category_id"],
             spawn_data.get("condition", "Used"),
             spawn_data.get("shelf") or item["shelf"],
             float(spawn_data["cost_price"]) if spawn_data.get("cost_price") not in (None, "") else None,
             spawn_data.get("notes", "") or f"Removed from: {item['name']} (ID {iid})",
             iid, now_ts])
        log_action("ITEM_ADD", spawned_id, spawn_data["name"],
                   f"Spawned from modification of '{item['name']}' (ID {iid})")
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute("INSERT INTO item_modifications (item_id,ts,modified_by,field_changed,"
            "old_value,new_value,notes,spawned_item_id) VALUES (?,?,?,?,?,?,?,?)",
            [iid, ts_now, session.get("username", "?"), field, old_val, new_val, notes, spawned_id])
    log_action("ITEM_MODIFY", iid, item["name"],
               f"{field}: '{old_val}' -> '{new_val}'" + (f" | Spawned item ID {spawned_id}" if spawned_id else ""),
               {field: old_val}, {field: new_val})
    updated = query("SELECT * FROM items WHERE id=?", [iid], one=True)
    if updated:
        sync_item_task(iid, item["name"], _item_missing_fields(dict(updated)))
    return jsonify({"ok": True, "spawned_id": spawned_id, "item": dict(updated)})


@bp.route("/api/item/lineage")
@login_required
def api_item_lineage():
    iid  = int(request.args.get("item_id", 0))
    if not iid: return jsonify({"ok": False, "msg": "item_id required"})
    item = query("SELECT * FROM items WHERE id=?", [iid], one=True)
    if not item: return jsonify({"ok": False, "msg": "Not found"})
    parent = None
    if item["parent_item_id"]:
        p = query("SELECT id,name,ram,cpu,storage,condition,shelf FROM items WHERE id=?",
                  [item["parent_item_id"]], one=True)
        if p: parent = dict(p)
    children = query("SELECT id,name,ram,cpu,storage,condition,shelf,created_at "
                     "FROM items WHERE parent_item_id=? AND active=1", [iid])
    return jsonify({"ok": True, "parent": parent, "children": [dict(c) for c in children]})


@bp.route("/api/item/ebay", methods=["POST"])
@login_required
@perm_required("sell_items")
def api_item_ebay():
    d   = request.json
    iid = d.get("item_id")
    if not iid: return jsonify({"ok": False, "msg": "item_id required"})
    item = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
    if not item: return jsonify({"ok": False, "msg": "Not found"})
    status = d.get("ebay_status", "not_listed")
    if status not in ("not_listed", "listed", "sold"):
        return jsonify({"ok": False, "msg": "Invalid status"})
    listing_id   = d.get("ebay_listing_id", "").strip() or None
    listed_price = float(d["ebay_listed_price"]) if d.get("ebay_listed_price") not in (None, "") else None
    listed_date  = d.get("ebay_listed_date") or None
    execute("UPDATE items SET ebay_status=?,ebay_listing_id=?,ebay_listed_price=?,ebay_listed_date=? WHERE id=?",
            [status, listing_id, listed_price, listed_date, iid])
    old_status = item["ebay_status"] or "not_listed"
    log_action("EBAY_UPDATE", iid, item["name"],
               f"eBay: {old_status} -> {status}" + (f" | Listing: {listing_id}" if listing_id else ""),
               {"ebay_status": old_status}, {"ebay_status": status})
    return jsonify({"ok": True})


# ── Products ──────────────────────────────────────────────────────────────────

@bp.route("/api/products")
@login_required
def api_products():
    q   = request.args.get("q", "")
    cat = request.args.get("cat", "")
    sql = """SELECT p.*, c.name as category_name, c.color as category_color,
                    (SELECT COUNT(*) FROM items i WHERE i.product_id=p.id AND i.active=1) as item_count
             FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1"""
    args = []
    if q:
        sql += " AND (p.name LIKE ? OR p.manufacturer LIKE ? OR p.model LIKE ?)"
        s = f"%{q}%"; args += [s, s, s]
    if cat:
        sql += " AND p.category_id=?"; args.append(cat)
    sql += " ORDER BY p.name"
    return jsonify([dict(r) for r in query(sql, args)])


@bp.route("/api/product/add", methods=["POST"])
@login_required
@perm_required("write_items")
def api_product_add():
    d = request.json
    if not d.get("name", "").strip(): return jsonify({"ok": False, "msg": "Name required"})
    req_serial   = int(d.get("require_serial",       0))
    req_vendor   = int(d.get("require_vendor_sku",   0))
    req_internal = int(d.get("require_internal_sku", 0))
    if not req_serial and not req_vendor and not req_internal:
        return jsonify({"ok": False, "msg": "At least one identifier must be selected"})
    if req_vendor and req_internal:
        return jsonify({"ok": False, "msg": "Vendor SKU and Internal SKU are mutually exclusive"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        pid = execute(
            "INSERT INTO products (name,manufacturer,model,description,category_id,"
            "serial_tracked,qty_tracked,require_scan_checkout,"
            "require_serial,require_vendor_sku,require_internal_sku,print_scan_label,"
            "require_sku_label,default_cost,default_sale,low_stock_threshold,vendor_sku,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [d["name"].strip(), d.get("manufacturer") or None, d.get("model") or None,
             d.get("description") or None, d.get("category_id") or None,
             int(d.get("serial_tracked", 0)), int(d.get("qty_tracked", 0)),
             int(d.get("require_scan_checkout", 0)),
             req_serial, req_vendor, req_internal, int(d.get("print_scan_label", 0)), 0,
             float(d["default_cost"]) if d.get("default_cost") not in (None, "") else None,
             float(d["default_sale"]) if d.get("default_sale") not in (None, "") else None,
             int(d.get("low_stock_threshold", 0)) if d.get("low_stock_threshold") not in (None, "") else 0,
             d.get("vendor_sku") or None, now])
        log_action("PRODUCT_ADD", detail=f"Added product: {d['name']}")
        return jsonify({"ok": True, "id": pid})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@bp.route("/api/product/edit", methods=["POST"])
@login_required
@perm_required("write_items")
def api_product_edit():
    d = request.json
    req_serial   = int(d.get("require_serial",       0))
    req_vendor   = int(d.get("require_vendor_sku",   0))
    req_internal = int(d.get("require_internal_sku", 0))
    if not req_serial and not req_vendor and not req_internal:
        req_internal = 1
    if req_vendor and req_internal:
        return jsonify({"ok": False, "msg": "Vendor SKU and Internal SKU are mutually exclusive"})
    execute(
        "UPDATE products SET name=?,manufacturer=?,model=?,description=?,category_id=?,"
        "serial_tracked=?,qty_tracked=?,require_scan_checkout=?,"
        "require_serial=?,require_vendor_sku=?,require_internal_sku=?,print_scan_label=?,"
        "default_cost=?,default_sale=?,low_stock_threshold=?,vendor_sku=? WHERE id=?",
        [d["name"], d.get("manufacturer") or None, d.get("model") or None,
         d.get("description") or None, d.get("category_id") or None,
         int(d.get("serial_tracked", 0)), int(d.get("qty_tracked", 0)),
         int(d.get("require_scan_checkout", 0)),
         req_serial, req_vendor, req_internal, int(d.get("print_scan_label", 0)),
         float(d["default_cost"]) if d.get("default_cost") not in (None, "") else None,
         float(d["default_sale"]) if d.get("default_sale") not in (None, "") else None,
         int(d.get("low_stock_threshold", 0)) if d.get("low_stock_threshold") not in (None, "") else 0,
         d.get("vendor_sku") or None,
         d["id"]])
    log_action("PRODUCT_EDIT", detail=f"Edited product: {d['name']}")
    return jsonify({"ok": True})


@bp.route("/api/product/delete", methods=["POST"])
@login_required
@perm_required("delete_items")
def api_product_delete():
    d     = request.json
    count = query("SELECT COUNT(*) FROM items WHERE product_id=? AND active=1", [d["id"]], one=True)[0]
    if count > 0:
        return jsonify({"ok": False, "msg": f"Cannot delete — {count} active item(s) use this product"})
    execute("UPDATE products SET active=0 WHERE id=?", [d["id"]])
    log_action("PRODUCT_DELETE", detail=f"Deleted product {d['id']}")
    return jsonify({"ok": True})


@bp.route("/api/product/to_item", methods=["POST"])
@login_required
@perm_required("write_items")
def api_product_to_item():
    d    = request.json
    prod = query("SELECT * FROM products WHERE id=? AND active=1", [d["id"]], one=True)
    if not prod: return jsonify({"ok": False, "msg": "Product not found"})
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qty     = 0 if prod["qty_tracked"] else None
    item_id = execute(
        "INSERT INTO items (name,manufacturer,model,product_id,category_id,"
        "cost_price,sale_price,condition,qty,active,created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
        [prod["name"], prod["manufacturer"], prod["model"],
         prod["id"], prod["category_id"],
         prod["default_cost"], prod["default_sale"], "New", qty, now])
    _ensure_internal_sku(item_id, prod["category_id"])
    log_action("ITEM_ADD", item_id, prod["name"], f"Created from product ID {d['id']}")
    return jsonify({"ok": True, "item_id": item_id})


# ── Categories ────────────────────────────────────────────────────────────────

@bp.route("/api/category/add", methods=["POST"])
@login_required
@admin_required
def api_cat_add():
    d = request.json
    try:
        cid = execute("INSERT INTO categories (name,color,is_expense) VALUES (?,?,?)",
                      [d["name"], d.get("color", "#ffffff"), int(d.get("is_expense", 0))])
        log_action("CAT_ADD", detail=f"Added: {d['name']}")
        return jsonify({"ok": True, "id": cid, "name": d["name"], "color": d.get("color", "#ffffff")})
    except Exception:
        return jsonify({"ok": False, "msg": "Already exists"})


@bp.route("/api/category/edit", methods=["POST"])
@login_required
@admin_required
def api_cat_edit():
    d = request.json
    execute("UPDATE categories SET name=?,color=?,is_expense=? WHERE id=?",
            [d["name"], d.get("color", "#ffffff"), int(d.get("is_expense", 0)), d["id"]])
    log_action("CAT_EDIT", detail=f"Edited category {d['id']}: {d['name']}")
    return jsonify({"ok": True})


@bp.route("/api/category/delete", methods=["POST"])
@login_required
@admin_required
def api_cat_delete():
    d          = request.json
    item_count = query("SELECT COUNT(*) FROM items WHERE category_id=? AND active=1", [d["id"]], one=True)[0]
    prod_count = query("SELECT COUNT(*) FROM products WHERE category_id=? AND active=1", [d["id"]], one=True)[0]
    if item_count > 0: return jsonify({"ok": False, "msg": f"Cannot delete — {item_count} item(s) use this category"})
    if prod_count > 0: return jsonify({"ok": False, "msg": f"Cannot delete — {prod_count} product(s) use this category"})
    execute("DELETE FROM category_fields WHERE category_id=?", [d["id"]])
    execute("DELETE FROM categories WHERE id=?", [d["id"]])
    log_action("CAT_DELETE", detail=f"Deleted category {d['id']}")
    return jsonify({"ok": True})


@bp.route("/api/category/fields")
@login_required
def api_category_fields():
    cat_id = request.args.get("cat_id")
    rows   = (query("SELECT * FROM category_fields WHERE category_id=? ORDER BY sort_order, id", [cat_id])
              if cat_id else
              query("SELECT * FROM category_fields ORDER BY category_id, sort_order, id"))
    result = []
    for r in rows:
        d = dict(r)
        if d.get("dropdown_options"):
            try: d["dropdown_options"] = json.loads(d["dropdown_options"])
            except Exception: d["dropdown_options"] = []
        result.append(d)
    return jsonify(result)


@bp.route("/api/category/fields/save", methods=["POST"])
@login_required
@admin_required
def api_category_fields_save():
    d      = request.json
    cat_id = d.get("category_id")
    fields = d.get("fields", [])
    if not cat_id: return jsonify({"ok": False, "msg": "category_id required"})
    execute("DELETE FROM category_fields WHERE category_id=?", [cat_id])
    for i, f in enumerate(fields):
        label    = (f.get("field_label") or "").strip()
        key      = (f.get("field_key")   or "").strip().lower().replace(" ", "_")
        if not label or not key: continue
        opts     = f.get("dropdown_options")
        opts_str = json.dumps(opts) if isinstance(opts, list) else (opts or None)
        execute("INSERT INTO category_fields (category_id,field_label,field_key,"
                "field_type,placeholder,required,sort_order,dropdown_options) VALUES (?,?,?,?,?,?,?,?)",
                [cat_id, label, key, f.get("field_type", "text"),
                 f.get("placeholder", ""), int(f.get("required", 0)), i, opts_str])
    return jsonify({"ok": True})


# ── Companies & Distributors ──────────────────────────────────────────────────

@bp.route("/api/companies")
@login_required
def api_companies():
    return jsonify([dict(r) for r in query("SELECT * FROM companies WHERE active=1 ORDER BY name")])


@bp.route("/api/company/add", methods=["POST"])
@login_required
def api_company_add():
    d    = request.json
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"ok": False, "msg": "Name required"})
    try:
        cid = execute("INSERT INTO companies (name,notes) VALUES (?,?)", [name, d.get("notes", "")])
        log_action("COMPANY_ADD", detail=f"Added company: {name}")
        return jsonify({"ok": True, "id": cid, "name": name})
    except Exception:
        return jsonify({"ok": False, "msg": "Company already exists"})


@bp.route("/api/company/edit", methods=["POST"])
@login_required
@admin_required
def api_company_edit():
    d        = request.json
    new_name = (d.get("name") or "").strip()
    if not new_name: return jsonify({"ok": False, "msg": "Name required"})
    old = query("SELECT name FROM companies WHERE id=?", [d["id"]], one=True)
    execute("UPDATE companies SET name=?,notes=? WHERE id=?", [new_name, d.get("notes", ""), d["id"]])
    if old and old["name"] != new_name:
        execute("UPDATE items SET owner_company=? WHERE owner_company=?", [new_name, old["name"]])
    return jsonify({"ok": True})


@bp.route("/api/company/delete", methods=["POST"])
@login_required
@admin_required
def api_company_delete():
    execute("UPDATE companies SET active=0 WHERE id=?", [request.json["id"]])
    return jsonify({"ok": True})


@bp.route("/api/distributor/add", methods=["POST"])
@login_required
def api_distributor_add():
    d    = request.json
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"ok": False, "msg": "Name required"})
    try:
        execute("INSERT INTO distributors (name) VALUES (?)", [name])
        log_action("DISTRIBUTOR_ADD", detail=f"Added distributor: {name}")
        return jsonify({"ok": True, "name": name})
    except Exception:
        return jsonify({"ok": False, "msg": "Distributor already exists"})


@bp.route("/api/distributor/edit", methods=["POST"])
@login_required
@admin_required
def api_distributor_edit():
    d        = request.json
    old      = query("SELECT name FROM distributors WHERE id=?", [d["id"]], one=True)
    new_name = (d.get("name") or "").strip()
    if not new_name: return jsonify({"ok": False, "msg": "Name required"})
    try:
        execute("UPDATE distributors SET name=? WHERE id=?", [new_name, d["id"]])
        if old: execute("UPDATE items SET purchased_from=? WHERE purchased_from=?", [new_name, old["name"]])
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False, "msg": "Name already exists"})


@bp.route("/api/distributor/delete", methods=["POST"])
@login_required
@admin_required
def api_distributor_delete():
    execute("DELETE FROM distributors WHERE id=?", [request.json["id"]])
    return jsonify({"ok": True})


# ── Tasks ─────────────────────────────────────────────────────────────────────

@bp.route("/api/tasks")
@login_required
def api_tasks():
    # Purge stale auto-tasks whose item has been deleted
    execute("DELETE FROM tasks WHERE item_id IS NOT NULL "
            "AND item_id NOT IN (SELECT id FROM items WHERE active=1)")
    rows = query(
        "SELECT t.*, i.name AS item_name "
        "FROM tasks t "
        "LEFT JOIN items i ON i.id = t.item_id AND i.active = 1 "
        "WHERE t.item_id IS NULL OR i.id IS NOT NULL "
        "ORDER BY CASE t.urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, t.created_at DESC")
    return jsonify([dict(r) for r in rows])


@bp.route("/api/tasks/clear-done", methods=["POST"])
@login_required
def api_tasks_clear_done():
    """Delete all manually-created done tasks (item-linked tasks are managed by the system)."""
    execute("DELETE FROM tasks WHERE status='done' AND item_id IS NULL")
    return jsonify({"ok": True})


@bp.route("/api/task/add", methods=["POST"])
@login_required
def api_task_add():
    d     = request.json
    title = (d.get("title") or "").strip()
    if not title: return jsonify({"ok": False, "msg": "Title required"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tid = execute("INSERT INTO tasks (title,urgency,status,notes,created_by,created_at) VALUES (?,?,?,?,?,?)",
                  [title, d.get("urgency", "medium"), d.get("status", "todo"),
                   d.get("notes", ""), session.get("username", ""), now])
    return jsonify({"ok": True, "id": tid})


@bp.route("/api/task/update", methods=["POST"])
@login_required
def api_task_update():
    d      = request.json
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields = []
    args   = []
    for k in ["title", "urgency", "status", "notes"]:
        if k in d: fields.append(f"{k}=?"); args.append(d[k])
    if not fields: return jsonify({"ok": False, "msg": "Nothing to update"})
    fields.append("updated_at=?"); args.append(now); args.append(d["id"])
    execute(f"UPDATE tasks SET {','.join(fields)} WHERE id=?", args)
    return jsonify({"ok": True})


@bp.route("/api/task/delete", methods=["POST"])
@login_required
def api_task_delete():
    execute("DELETE FROM tasks WHERE id=?", [request.json["id"]])
    return jsonify({"ok": True})


# ── Contacts ──────────────────────────────────────────────────────────────────

@bp.route("/api/contact/add", methods=["POST"])
@login_required
@perm_required("write_items")
def api_contact_add():
    d    = request.json
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"ok": False, "msg": "Name required"})
    cid = execute("INSERT INTO contacts (name,role,email,phone,notes,company_id,company_type) VALUES (?,?,?,?,?,?,?)",
                  [name, d.get("role",""), d.get("email",""), d.get("phone",""),
                   d.get("notes",""), d.get("company_id"), d.get("company_type","")])
    return jsonify({"ok": True, "id": cid})


@bp.route("/api/contact/edit", methods=["POST"])
@login_required
@perm_required("write_items")
def api_contact_edit():
    d = request.json
    execute("UPDATE contacts SET name=?,role=?,email=?,phone=?,notes=?,company_id=?,company_type=? WHERE id=?",
            [(d.get("name") or "").strip(), d.get("role",""), d.get("email",""),
             d.get("phone",""), d.get("notes",""),
             d.get("company_id"), d.get("company_type",""), d["id"]])
    return jsonify({"ok": True})


@bp.route("/api/contact/delete", methods=["POST"])
@login_required
@perm_required("delete_items")
def api_contact_delete():
    execute("UPDATE contacts SET active=0 WHERE id=?", [request.json["id"]])
    return jsonify({"ok": True})


# ── Users & Admin ─────────────────────────────────────────────────────────────

@bp.route("/api/user/add", methods=["POST"])
@login_required
@admin_required
@api_rate_limit(max_attempts=20, window=60)
def api_user_add():
    from flask import g
    from app.stripe_billing import PLANS
    plan_key  = g.tenant.get("plan", "starter") if hasattr(g, "tenant") and g.tenant else "starter"
    plan_cfg  = PLANS.get(plan_key, PLANS["starter"])
    max_users = plan_cfg.get("max_users")
    if max_users is not None:
        current = query("SELECT COUNT(*) FROM users", one=True)[0]
        if current >= max_users:
            return jsonify({"ok": False,
                            "msg": f"User limit reached ({max_users} on {plan_cfg['name']} plan). "
                                   f"Upgrade your plan to add more users."})
    d    = request.json
    email = (d.get("username") or d.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"ok": False, "msg": "Valid email address required"})
    role = d.get("role", "worker")
    if role == "admin":
        perm_str = ",".join(ADMIN_DEFAULT_PERMS)
    else:
        custom = d.get("permissions")
        perm_str = (",".join(set(custom) & set(PERM_KEYS)) if custom is not None
                    else ",".join(WORKER_DEFAULT_PERMS))
    import secrets as _sec
    from datetime import datetime as _dt, timedelta as _td
    from config import Config as _Cfg
    # Create user with unusable random password — invite link sets the real one
    placeholder_pw = hash_pw(_sec.token_hex(32))
    try:
        uid = execute(
            "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password)"
            " VALUES (?,?,?,?,?,0,1)",
            [email, placeholder_pw, role, perm_str, email])
        log_action("USER_ADD", detail=f"Invited user: {email} | role: {role}")
        # Create invite token (reuses password_reset_tokens table)
        tok = _sec.token_urlsafe(32)
        exp = (_dt.utcnow() + _td(hours=72)).strftime("%Y-%m-%d %H:%M:%S")
        execute("INSERT INTO password_reset_tokens (user_id,token,expires_at) VALUES (?,?,?)",
                [uid, tok, exp])
        from app.mailer import send_invite_email
        send_invite_email(email, g.tenant_slug, tok, inviter=session.get("username", "Your admin"))
        return jsonify({"ok": True, "id": uid,
                        "msg": f"Invite sent to {email}. They'll receive a link to set their password."})
    except Exception:
        return jsonify({"ok": False, "msg": "Email already in use"})


@bp.route("/api/user/permissions", methods=["POST"])
@login_required
@admin_required
def api_user_permissions():
    d     = request.json
    user  = query("SELECT * FROM users WHERE id=?", [d.get("id")], one=True)
    if not user: return jsonify({"ok": False, "msg": "User not found"})
    if user["role"] == "admin": return jsonify({"ok": False, "msg": "Admin always has full permissions"})
    perm_str = ",".join(set(d.get("permissions", [])) & set(PERM_KEYS))
    execute("UPDATE users SET permissions=? WHERE id=?", [perm_str, d["id"]])
    log_action("USER_PERMS", detail=f"Updated perms for {user['username']}: {perm_str}")
    return jsonify({"ok": True})


@bp.route("/api/user/password", methods=["POST"])
@login_required
@admin_required
def api_user_password():
    d = request.json
    if not d.get("password"): return jsonify({"ok": False, "msg": "Password required"})
    pw_err = validate_password(d["password"])
    if pw_err: return jsonify({"ok": False, "msg": pw_err})
    execute("UPDATE users SET password=?, session_token=NULL WHERE id=?",
            [hash_pw(d["password"]), d["id"]])
    return jsonify({"ok": True})


@bp.route("/api/user/email", methods=["POST"])
@login_required
@admin_required
def api_user_email():
    d = request.json
    uid   = d.get("id")
    email = (d.get("email") or "").strip().lower()
    if not uid:
        return jsonify({"ok": False, "msg": "User ID required"})
    if email and "@" not in email:
        return jsonify({"ok": False, "msg": "Invalid email address"})
    user = query("SELECT username FROM users WHERE id=?", [uid], one=True)
    if not user:
        return jsonify({"ok": False, "msg": "User not found"})
    # Check uniqueness (only if setting an email)
    if email:
        clash = query("SELECT id FROM users WHERE LOWER(COALESCE(email,''))=? AND id!=?",
                      [email, uid], one=True)
        if clash:
            return jsonify({"ok": False, "msg": "That email is already used by another account"})
    execute("UPDATE users SET email=? WHERE id=?", [email or None, uid])
    log_action("USER_EMAIL", detail=f"Updated email for {user['username']}: {email or '(cleared)'}")
    return jsonify({"ok": True})


@bp.route("/api/user/delete", methods=["POST"])
@login_required
@admin_required
@api_rate_limit(max_attempts=20, window=60)
def api_user_delete():
    d = request.json
    if d["id"] == session.get("user_id"):
        return jsonify({"ok": False, "msg": "Cannot delete yourself"})
    execute("DELETE FROM users WHERE id=?", [d["id"]])
    return jsonify({"ok": True})


@bp.route("/api/user/force-logout", methods=["POST"])
@login_required
@admin_required
def api_user_force_logout():
    d = request.json
    uid = d.get("id")
    if not uid:
        return jsonify({"ok": False, "msg": "User ID required"})
    if uid == session.get("user_id"):
        return jsonify({"ok": False, "msg": "Cannot force-logout yourself"})
    user = query("SELECT username FROM users WHERE id=?", [uid], one=True)
    if not user:
        return jsonify({"ok": False, "msg": "User not found"})
    execute("UPDATE users SET session_token=NULL WHERE id=?", [uid])
    log_action("FORCE_LOGOUT", detail=f"Force-logged-out user: {user['username']}")
    return jsonify({"ok": True})


@bp.route("/api/user/me")
@login_required
def api_user_me():
    from app.helpers import _auth_user_id
    uid = _auth_user_id()
    row = query("SELECT id, username, email, role, two_fa_enabled FROM users WHERE id=?",
                [uid], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"})
    return jsonify({**dict(row), "ok": True})


@bp.route("/api/change_password", methods=["POST"])
@login_required
def api_change_password():
    d    = request.json
    user = query("SELECT * FROM users WHERE id=?", [session["user_id"]], one=True)
    if not user or not verify_pw(d.get("old_password", ""), user["password"]):
        return jsonify({"ok": False, "msg": "Current password incorrect"})
    pw_err = validate_password(d.get("new_password", ""), user["password"])
    if pw_err: return jsonify({"ok": False, "msg": pw_err})
    execute("UPDATE users SET password=? WHERE id=?",
            [hash_pw(d["new_password"]), session["user_id"]])
    return jsonify({"ok": True})


# ── Alerts & Audit ────────────────────────────────────────────────────────────

@bp.route("/api/alerts")
@login_required
def api_alerts():
    loc_id = request.args.get("loc", "").strip()
    if loc_id:
        rows = query("""
            SELECT p.id, p.name, p.low_stock_threshold,
                   COUNT(i.id) as available_count
            FROM products p
            LEFT JOIN items i ON i.product_id = p.id
                AND i.active = 1 AND i.sold = 0 AND i.checked_out = 0
                AND i.location_id = ?
            WHERE p.active = 1 AND p.low_stock_threshold > 0
            GROUP BY p.id
            HAVING available_count <= p.low_stock_threshold
            ORDER BY available_count ASC
        """, [loc_id])
        return jsonify([{"id": r["id"], "name": r["name"],
                         "available_count": r["available_count"],
                         "low_stock_threshold": r["low_stock_threshold"]}
                        for r in rows])
    return jsonify(get_low_stock_alerts())


@bp.route("/api/support", methods=["POST"])
@login_required
@api_rate_limit(max_attempts=5, window=300)
def api_support():
    d = request.json or {}
    subject = (d.get("subject") or "").strip()
    message = (d.get("message") or "").strip()
    if not subject or not message:
        return jsonify({"ok": False, "msg": "Subject and message are required"})
    if len(message) > 5000:
        return jsonify({"ok": False, "msg": "Message too long (max 5000 characters)"})
    from flask import g
    from app.mailer import send_support_message
    user_email = query("SELECT email FROM users WHERE id=?",
                       [session["user_id"]], one=True)
    from_email = (user_email["email"] if user_email and user_email["email"] else "")
    sent = send_support_message(
        subject=subject,
        message=message,
        from_name=session.get("username", "Unknown"),
        from_email=from_email,
        tenant=getattr(g, "tenant_slug", "unknown")
    )
    return jsonify({"ok": True, "emailed": sent})


@bp.route("/api/smtp-status")
@login_required
def api_smtp_status():
    from config import Config as _Cfg
    return jsonify({"ok": bool(_Cfg.SMTP_HOST)})


@bp.route("/api/audit")
@login_required
@perm_required("view_audit")
def api_audit():
    page     = max(1, int(request.args.get("page", 1)))
    limit    = min(200, max(1, int(request.args.get("per_page", 50))))
    offset   = (page - 1) * limit
    search   = request.args.get("q", "").strip()
    f_user   = request.args.get("user", "").strip()
    f_action = request.args.get("action", "").strip()
    f_from   = request.args.get("from", "").strip()
    f_to     = request.args.get("to", "").strip()

    conditions, args = [], []

    if search:
        conditions.append("(action LIKE ? OR item_name LIKE ? OR detail LIKE ? "
                          "OR username LIKE ? OR item_serial LIKE ? OR item_sku LIKE ? "
                          "OR product_name LIKE ? OR CAST(item_id AS TEXT) LIKE ?)")
        s = f"%{search}%"
        args += [s] * 8
    if f_user:
        conditions.append("username = ?")
        args.append(f_user)
    if f_action:
        conditions.append("action = ?")
        args.append(f_action)
    if f_from:
        conditions.append("ts >= ?")
        args.append(f_from)
    if f_to:
        conditions.append("ts < date(?, '+1 day')")
        args.append(f_to)

    sql = "SELECT * FROM audit_log"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    total = query(f"SELECT COUNT(*) FROM ({sql})", args, one=True)[0]
    pages = max(1, (total + limit - 1) // limit)
    sql  += f" ORDER BY id DESC LIMIT {limit} OFFSET {offset}"
    return jsonify({"rows": [dict(r) for r in query(sql, args)],
                    "total": total, "page": page, "pages": pages})


# ── Settings (tenant branding) ────────────────────────────────────────────────

@bp.route("/api/settings")
@login_required
def api_get_settings():
    rows = query("SELECT key, value FROM settings")
    return jsonify({r["key"]: r["value"] for r in rows})

ALLOWED_SETTINGS = {
    "brand_name", "brand_color",
    "low_stock_alerts_enabled", "low_stock_alert_email",
    "overdue_reminder_days_1", "overdue_reminder_days_2",
    "overdue_reminder_enabled",
    "slack_webhook_url", "slack_enabled",
    "teams_webhook_url", "teams_enabled",
    "slack_events", "teams_events",
}

@bp.route("/api/settings", methods=["POST"])
@login_required
@admin_required
def api_save_settings():
    d = request.json or {}
    for key, value in d.items():
        if key not in ALLOWED_SETTINGS:
            continue
        existing = query("SELECT key FROM settings WHERE key=?", [key], one=True)
        if existing:
            execute("UPDATE settings SET value=? WHERE key=?", [value, key])
        else:
            execute("INSERT INTO settings (key,value) VALUES (?,?)", [key, value])
    log_action("SETTINGS_UPDATE", detail=str(d))
    return jsonify({"ok": True})


@bp.route("/api/alert-emails", methods=["GET"])
@login_required
@admin_required
def api_get_alert_emails():
    """Return current alert email list and all users with their alert opt-in status."""
    settings = {r["key"]: r["value"] for r in query("SELECT key,value FROM settings")}
    raw = settings.get("low_stock_alert_email", "") or ""
    emails = [e.strip() for e in raw.split(",") if e.strip()]
    users = query(
        "SELECT id, username, email, role, COALESCE(low_stock_alerts,0) AS low_stock_alerts "
        "FROM users WHERE active=1 ORDER BY role DESC, username")
    return jsonify({
        "ok": True,
        "emails": emails,
        "users": [dict(u) for u in users],
    })


@bp.route("/api/alert-emails", methods=["POST"])
@login_required
@admin_required
def api_save_alert_emails():
    """Save comma-separated alert emails and per-user alert flags."""
    d = request.json or {}
    # emails — deduplicate, basic validation
    raw_emails = d.get("emails", [])
    clean = list({e.strip() for e in raw_emails if "@" in (e or "")})
    csv_value = ",".join(clean)
    existing = query("SELECT key FROM settings WHERE key='low_stock_alert_email'", one=True)
    if existing:
        execute("UPDATE settings SET value=? WHERE key='low_stock_alert_email'", [csv_value])
    else:
        execute("INSERT INTO settings (key,value) VALUES ('low_stock_alert_email',?)", [csv_value])
    # per-user flags
    user_flags = d.get("user_alerts", {})  # {user_id: 0|1}
    for uid, flag in user_flags.items():
        execute("UPDATE users SET low_stock_alerts=? WHERE id=?", [1 if flag else 0, uid])
    log_action("SETTINGS_UPDATE", detail=f"Alert emails updated: {len(clean)} address(es), {len(user_flags)} user flags")
    return jsonify({"ok": True})


@bp.route("/api/low-stock/send-report", methods=["POST"])
@login_required
@admin_required
def api_send_low_stock_report():
    """Send a low stock report email to the configured alert address."""
    from app.mailer import send_email
    from config import Config as _Cfg
    from flask import g

    if not _Cfg.SMTP_HOST:
        return jsonify({"ok": False, "msg": "SMTP is not configured on this server."})

    settings = {r["key"]: r["value"] for r in query("SELECT key,value FROM settings")}
    raw = settings.get("low_stock_alert_email", "") or ""
    alert_emails = [e.strip() for e in raw.split(",") if e.strip()]
    # also include users who have opted in and have an email address
    user_rows = query(
        "SELECT email FROM users WHERE COALESCE(low_stock_alerts,0)=1 AND email IS NOT NULL AND email != '' AND active=1")
    alert_emails += [r["email"] for r in user_rows if r["email"] not in alert_emails]
    if not alert_emails:
        return jsonify({"ok": False, "msg": "No alert recipients configured. Add an email address or assign users in the Low Stock Alerts settings."})

    alerts = get_low_stock_alerts()
    if not alerts:
        return jsonify({"ok": False, "msg": "No products are currently at or below their minimum threshold."})

    rows_html = "".join(
        f"<tr style='border-bottom:1px solid #f0efec'>"
        f"<td style='padding:10px 14px;font-weight:500'>{a['name']}</td>"
        f"<td style='padding:10px 14px;font-family:monospace;color:#b91c1c;font-weight:700'>{a['available_count']}</td>"
        f"<td style='padding:10px 14px;font-family:monospace'>{a['low_stock_threshold']}</td>"
        f"<td style='padding:10px 14px;font-family:monospace;color:#b91c1c'>{max(0, a['low_stock_threshold'] - a['available_count'])}</td>"
        f"</tr>"
        for a in alerts
    )
    slug = getattr(g, "tenant_slug", "")
    domain = _Cfg.APP_DOMAIN
    link = f"https://{slug}.{domain}/low-stock"

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
      <h2 style="color:#0f172a;margin-bottom:4px">Low Stock Alert</h2>
      <p style="color:#64748b;margin-top:0">{len(alerts)} product{'s' if len(alerts)!=1 else ''} at or below minimum threshold</p>
      <table style="width:100%;border-collapse:collapse;border:1px solid #e5e3de;border-radius:8px;overflow:hidden">
        <thead>
          <tr style="background:#f8f7f5">
            <th style="padding:10px 14px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Product</th>
            <th style="padding:10px 14px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Have</th>
            <th style="padding:10px 14px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Min</th>
            <th style="padding:10px 14px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Short</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <div style="margin-top:20px">
        <a href="{link}" style="background:#0f172a;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:500">View Low Stock →</a>
      </div>
      <p style="font-size:11px;color:#94a3b8;margin-top:24px">Sent from CountDepot · {slug}.{domain}</p>
    </div>"""

    plain = f"Low Stock Alert — {len(alerts)} product(s) below minimum.\n\n" + \
            "\n".join(f"• {a['name']}: {a['available_count']} available (min {a['low_stock_threshold']})" for a in alerts) + \
            f"\n\nView: {link}"

    subject = f"Low Stock Alert — {len(alerts)} product(s) need restocking"
    sent_to, failed = [], []
    for addr in alert_emails:
        if send_email(addr, subject, html, plain):
            sent_to.append(addr)
        else:
            failed.append(addr)
    if sent_to:
        log_action("LOW_STOCK_REPORT_SENT", detail=f"Sent to {', '.join(sent_to)}, {len(alerts)} items")
        msg = f"Report sent to {', '.join(sent_to)}"
        if failed:
            msg += f" (failed: {', '.join(failed)})"
        return jsonify({"ok": True, "msg": msg})
    return jsonify({"ok": False, "msg": "Failed to send email. Check SMTP settings on the server."})


# ── Overdue Escalation Reminders ─────────────────────────────────────────────

@bp.route("/api/admin/overdue-reminders/run", methods=["POST"])
@login_required
@admin_required
def api_run_overdue_reminders():
    """Check all overdue checkouts and send reminder emails. Safe to call repeatedly — tracks sent reminders."""
    from app.mailer import send_overdue_reminder
    from config import Config as _Cfg
    from flask import g
    from datetime import date

    settings    = {r["key"]: r["value"] for r in query("SELECT key,value FROM settings")}
    enabled     = settings.get("overdue_reminder_enabled", "1") == "1"
    days_1      = int(settings.get("overdue_reminder_days_1", "1") or 1)
    days_2      = int(settings.get("overdue_reminder_days_2", "7") or 7)

    if not enabled:
        return jsonify({"ok": False, "msg": "Overdue reminders are disabled in settings."})
    if not _Cfg.SMTP_HOST:
        return jsonify({"ok": False, "msg": "SMTP is not configured on this server."})

    today    = date.today().isoformat()
    domain   = _Cfg.APP_DOMAIN
    slug     = getattr(g, "tenant_slug", "")
    base_url = f"https://{slug}.{domain}"

    # All open checkouts past their due date
    overdue_rows = query("""
        SELECT cl.id as cl_id, cl.item_id, cl.checked_out_by, cl.checkout_date,
               cl.expected_return_date, i.name as item_name, i.serial,
               u.email as user_email
        FROM checkout_log cl
        JOIN items i ON i.id = cl.item_id
        LEFT JOIN users u ON lower(u.username) = lower(cl.checked_out_by)
        WHERE cl.checkin_date IS NULL
          AND cl.expected_return_date IS NOT NULL
          AND cl.expected_return_date < ?
    """, [today])

    sent_count = 0
    for row in overdue_rows:
        due   = row["expected_return_date"]
        diff  = (date.fromisoformat(today) - date.fromisoformat(due)).days
        email = row["user_email"]
        if not email:
            continue

        for (num, threshold) in [(1, days_1), (2, days_2)]:
            if diff < threshold:
                continue
            already = query("""SELECT id FROM overdue_reminders
                               WHERE checkout_log_id=? AND reminder_num=?""",
                            [row["cl_id"], num], one=True)
            if already:
                continue
            ok = send_overdue_reminder(
                to=email,
                item_name=row["item_name"],
                serial=row["serial"],
                checked_out_by=row["checked_out_by"],
                checkout_date=(row["checkout_date"] or "")[:10],
                due_date=due[:10] if due else "",
                days_overdue=diff,
                reminder_num=num,
                workspace_url=base_url,
            )
            if ok:
                execute("""INSERT INTO overdue_reminders
                           (checkout_log_id, item_id, reminded_at, reminder_num, sent_to)
                           VALUES (?,?,?,?,?)""",
                        [row["cl_id"], row["item_id"], today, num, email])
                sent_count += 1

    log_action("OVERDUE_REMINDERS_RUN", detail=f"Sent {sent_count} reminder(s)")
    return jsonify({"ok": True, "sent": sent_count,
                    "msg": f"{sent_count} reminder(s) sent" if sent_count else "No new reminders needed"})


# ── Alert channel test ────────────────────────────────────────────────────────

@bp.route("/api/admin/alerts/test", methods=["POST"])
@login_required
@admin_required
def api_test_alerts():
    """Fire a test message on every configured alert channel and report results."""
    from app.mailer import send_email as _se
    from config import Config as _Cfg
    results = {}

    # Email
    to = _Cfg.PLATFORM_ADMIN_EMAIL or _Cfg.SUPPORT_EMAIL or _Cfg.SMTP_FROM
    if _Cfg.SMTP_HOST and to:
        ok = _se(to,
                 "CountDepot alert test",
                 "<div style='font-family:sans-serif;padding:24px'>"
                 "<h2 style='margin:0 0 8px'>Alert test</h2>"
                 "<p style='color:#64748b'>This is a test alert from CountDepot. "
                 "Your email alert channel is working correctly.</p></div>",
                 "CountDepot alert test — email channel is working.")
        results["email"] = {"ok": ok, "to": to}
    else:
        results["email"] = {"ok": False, "reason": "SMTP not configured or no admin email set"}

    # Slack
    settings = {r["key"]: r["value"] for r in query("SELECT key,value FROM settings WHERE key LIKE 'slack%' OR key LIKE 'teams%'")}
    if settings.get("slack_enabled") == "1" and settings.get("slack_webhook_url"):
        try:
            from app.messenger import send_slack as _sl
            _sl(settings["slack_webhook_url"], "test",
                "CountDepot alert test — Slack channel is working.")
            results["slack"] = {"ok": True}
        except Exception as ex:
            results["slack"] = {"ok": False, "reason": str(ex)}
    else:
        results["slack"] = {"ok": None, "reason": "Slack not configured"}

    # Teams
    if settings.get("teams_enabled") == "1" and settings.get("teams_webhook_url"):
        try:
            from app.messenger import send_teams as _tm
            _tm(settings["teams_webhook_url"], "test",
                "CountDepot alert test — Teams channel is working.")
            results["teams"] = {"ok": True}
        except Exception as ex:
            results["teams"] = {"ok": False, "reason": str(ex)}
    else:
        results["teams"] = {"ok": None, "reason": "Teams not configured"}

    log_action("ALERT_TEST", detail="Alert channel test triggered by admin")
    return jsonify({"ok": True, "results": results})


# ── Scheduled Reports ────────────────────────────────────────────────────────

REPORT_TYPES = {
    "low_stock":        "Low Stock Alert",
    "overdue":          "Overdue Items",
    "inventory":        "Inventory Summary",
    "warranties":       "Warranty Expiry (next 30d)",
    "depreciation":     "Depreciation Summary",
}

@bp.route("/api/scheduled-reports")
@login_required
@admin_required
def api_scheduled_reports_list():
    rows = query("SELECT * FROM scheduled_reports ORDER BY id DESC")
    return jsonify({"ok": True, "reports": [dict(r) for r in rows],
                    "report_types": REPORT_TYPES})


@bp.route("/api/scheduled-reports", methods=["POST"])
@login_required
@admin_required
def api_scheduled_reports_create():
    d = request.json or {}
    rtype = d.get("report_type", "").strip()
    sched = d.get("schedule", "daily").strip()
    email = d.get("email", "").strip()
    if rtype not in REPORT_TYPES:
        return jsonify({"ok": False, "msg": "Invalid report type"})
    if sched not in ("daily", "weekly", "monthly"):
        return jsonify({"ok": False, "msg": "Schedule must be daily, weekly, or monthly"})
    if not email:
        return jsonify({"ok": False, "msg": "Email is required"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    iid = execute("""INSERT INTO scheduled_reports
                     (report_type, schedule, email, enabled, created_by, created_at)
                     VALUES (?,?,?,1,?,?)""",
                  [rtype, sched, email, session.get("username"), now])
    log_action("SCHEDULED_REPORT_CREATED", detail=f"{rtype} {sched} → {email}")
    return jsonify({"ok": True, "id": iid})


@bp.route("/api/scheduled-reports/<int:rid>", methods=["DELETE"])
@login_required
@admin_required
def api_scheduled_reports_delete(rid):
    row = query("SELECT * FROM scheduled_reports WHERE id=?", [rid], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    execute("DELETE FROM scheduled_reports WHERE id=?", [rid])
    log_action("SCHEDULED_REPORT_DELETED", detail=f"#{rid} {row['report_type']}")
    return jsonify({"ok": True})


@bp.route("/api/scheduled-reports/<int:rid>/toggle", methods=["POST"])
@login_required
@admin_required
def api_scheduled_reports_toggle(rid):
    row = query("SELECT enabled FROM scheduled_reports WHERE id=?", [rid], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    new_val = 0 if row["enabled"] else 1
    execute("UPDATE scheduled_reports SET enabled=? WHERE id=?", [new_val, rid])
    return jsonify({"ok": True, "enabled": new_val})


@bp.route("/api/scheduled-reports/run", methods=["POST"])
@login_required
@admin_required
def api_run_scheduled_reports():
    """Run all due scheduled reports. Safe to call from cron."""
    from app.mailer import send_email
    from config import Config as _Cfg
    from datetime import date
    if not _Cfg.SMTP_HOST:
        return jsonify({"ok": False, "msg": "SMTP not configured"})
    today = date.today()
    day_of_week = today.weekday()  # 0=Monday
    day_of_month = today.day
    rows = query("SELECT * FROM scheduled_reports WHERE enabled=1")
    sent = 0
    for r in rows:
        last = r["last_sent"]
        sched = r["schedule"]
        if last:
            last_d = date.fromisoformat(last[:10])
            if sched == "daily" and (today - last_d).days < 1:
                continue
            elif sched == "weekly" and (today - last_d).days < 7:
                continue
            elif sched == "monthly" and (today - last_d).days < 28:
                continue
        # Weekly: only send on Monday; Monthly: only on 1st
        if sched == "weekly" and day_of_week != 0:
            continue
        if sched == "monthly" and day_of_month != 1:
            continue
        ok = _send_scheduled_report(r["report_type"], r["email"])
        if ok:
            execute("UPDATE scheduled_reports SET last_sent=? WHERE id=?",
                    [today.isoformat(), r["id"]])
            sent += 1
    return jsonify({"ok": True, "sent": sent, "msg": f"{sent} report(s) sent"})


def _send_scheduled_report(report_type: str, to: str) -> bool:
    """Build and send one scheduled report email."""
    from app.mailer import send_email
    from datetime import date, timedelta
    today = date.today().isoformat()
    label = REPORT_TYPES.get(report_type, report_type)

    if report_type == "low_stock":
        from app.helpers import get_low_stock_alerts
        alerts = get_low_stock_alerts()
        if not alerts:
            return False  # nothing to report
        rows_html = "".join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{a["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de;color:#b91c1c">{a["available_count"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{a["low_stock_threshold"]}</td></tr>'
            for a in alerts
        )
        body_html = f"""<h2 style="font-size:16px;margin-bottom:12px">Low Stock Alert — {len(alerts)} product(s)</h2>
        <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e3de;border-radius:8px;overflow:hidden">
          <thead><tr style="background:#f8f7f4">
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Product</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Available</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Minimum</th>
          </tr></thead><tbody>{rows_html}</tbody></table>"""

    elif report_type == "overdue":
        overdues = query("""SELECT cl.item_id, i.name, i.serial, cl.checked_out_by,
                               cl.expected_return_date, cl.checkout_date
                            FROM checkout_log cl JOIN items i ON i.id=cl.item_id
                            WHERE cl.checkin_date IS NULL AND cl.expected_return_date IS NOT NULL
                              AND cl.expected_return_date < ? ORDER BY cl.expected_return_date""", [today])
        if not overdues:
            return False
        rows_html = "".join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["checked_out_by"] or "—"}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de;color:#b91c1c">{(r["expected_return_date"] or "")[:10]}</td></tr>'
            for r in overdues
        )
        body_html = f"""<h2 style="font-size:16px;margin-bottom:12px">Overdue Items — {len(overdues)} item(s)</h2>
        <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e3de;border-radius:8px;overflow:hidden">
          <thead><tr style="background:#f8f7f4">
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Item</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Checked Out By</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Due Date</th>
          </tr></thead><tbody>{rows_html}</tbody></table>"""

    elif report_type == "inventory":
        total  = query("SELECT COUNT(*) FROM items WHERE active=1 AND COALESCE(retired,0)=0 AND sold=0", one=True)[0]
        out    = query("SELECT COUNT(*) FROM items WHERE active=1 AND checked_out=1", one=True)[0]
        val_r  = query("SELECT COALESCE(SUM(COALESCE(cost_price,0)),0) FROM items WHERE active=1 AND sold=0", one=True)
        val    = round(val_r[0], 2) if val_r else 0
        body_html = f"""<h2 style="font-size:16px;margin-bottom:12px">Inventory Summary</h2>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
          <div style="background:#f8f7f4;border-radius:8px;padding:14px 18px"><div style="font-size:24px;font-weight:700">{total}</div><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;margin-top:4px">Total Items</div></div>
          <div style="background:#fef2f2;border-radius:8px;padding:14px 18px"><div style="font-size:24px;font-weight:700;color:#b91c1c">{out}</div><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;margin-top:4px">Checked Out</div></div>
          <div style="background:#f0fdf4;border-radius:8px;padding:14px 18px"><div style="font-size:24px;font-weight:700;color:#15803d">${val:,.2f}</div><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;margin-top:4px">Stock Value</div></div>
        </div>"""

    elif report_type == "warranties":
        cutoff = (date.today() + __import__('datetime').timedelta(days=30)).isoformat()
        exp = query("""SELECT i.name, i.serial, i.warranty_expiry, i.contract_expiry
                       FROM items i WHERE i.active=1 AND i.sold=0
                         AND ((i.warranty_expiry IS NOT NULL AND i.warranty_expiry<=?)
                           OR (i.contract_expiry IS NOT NULL AND i.contract_expiry<=?))
                       ORDER BY COALESCE(i.warranty_expiry,i.contract_expiry)""", [cutoff, cutoff])
        if not exp:
            return False
        rows_html = "".join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de;color:#b45309">{(r["warranty_expiry"] or "")[:10] or "—"}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de;color:#b45309">{(r["contract_expiry"] or "")[:10] or "—"}</td></tr>'
            for r in exp
        )
        body_html = f"""<h2 style="font-size:16px;margin-bottom:12px">Warranties Expiring in 30 Days — {len(exp)} item(s)</h2>
        <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e3de;border-radius:8px;overflow:hidden">
          <thead><tr style="background:#f8f7f4">
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Item</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Warranty Expiry</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Contract Expiry</th>
          </tr></thead><tbody>{rows_html}</tbody></table>"""

    elif report_type == "depreciation":
        from datetime import date as _date
        items = query("""SELECT name, cost_price, depreciation_rate, purchase_date
                         FROM items WHERE active=1 AND sold=0 AND COALESCE(retired,0)=0
                           AND depreciation_rate IS NOT NULL AND depreciation_rate>0 ORDER BY name""")
        if not items:
            return False
        today_d = _date.today()
        total_cost, total_cur = 0.0, 0.0
        rows_html = ""
        for r in items:
            cost = r["cost_price"] or 0
            rate = r["depreciation_rate"] or 0
            yrs = 0.0
            if r["purchase_date"]:
                try: yrs = (_date.today() - _date.fromisoformat(r["purchase_date"][:10])).days / 365.25
                except: pass
            cur = max(0.0, cost * (1 - (rate/100) * yrs))
            total_cost += cost; total_cur += cur
            rows_html += (f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["name"]}</td>'
                          f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de">${cost:,.2f}</td>'
                          f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de;color:#1d4ed8">${cur:,.2f}</td></tr>')
        body_html = f"""<h2 style="font-size:16px;margin-bottom:12px">Depreciation Summary</h2>
        <p style="font-size:13px;color:#64748b;margin-bottom:12px">Total original cost: <strong>${total_cost:,.2f}</strong> → Current book value: <strong style="color:#1d4ed8">${total_cur:,.2f}</strong></p>
        <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e3de;border-radius:8px;overflow:hidden">
          <thead><tr style="background:#f8f7f4">
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Item</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Original Cost</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#94a3b8;text-transform:uppercase">Current Value</th>
          </tr></thead><tbody>{rows_html}</tbody></table>"""
    else:
        return False

    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <div style="font-size:11px;color:#94a3b8;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">Scheduled Report · {today}</div>
  <h1 style="font-size:20px;font-weight:700;margin-bottom:20px">{label}</h1>
  {body_html}
  <p style="font-size:11px;color:#94a3b8;margin-top:24px">Sent automatically by CountDepot. Manage scheduled reports in Settings → Admin.</p>
</div>"""
    return send_email(to, f"[CountDepot] {label} — {today}", html)


# ── API Keys ──────────────────────────────────────────────────────────────────

@bp.route("/api/keys")
@login_required
@admin_required
def api_list_keys():
    from flask import g
    from app.stripe_billing import PLANS
    plan_key = g.tenant.get("plan", "starter") if hasattr(g, "tenant") and g.tenant else "starter"
    if not PLANS.get(plan_key, {}).get("api_access", False):
        return jsonify({"ok": False, "msg": "API key access requires Enterprise plan."})
    rows = query("""SELECT ak.id, ak.name, ak.key_prefix, ak.created_at, ak.last_used,
                           ak.active, u.email as user_email
                    FROM api_keys ak JOIN users u ON u.id=ak.user_id
                    ORDER BY ak.id DESC""")
    return jsonify({"ok": True, "keys": [dict(r) for r in rows]})

@bp.route("/api/keys", methods=["POST"])
@login_required
@admin_required
def api_create_key():
    import hashlib as _hl
    import secrets as _sec
    from flask import g
    from app.stripe_billing import PLANS
    plan_key = g.tenant.get("plan", "starter") if hasattr(g, "tenant") and g.tenant else "starter"
    if not PLANS.get(plan_key, {}).get("api_access", False):
        return jsonify({"ok": False, "msg": "API key access requires Enterprise plan."})
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Key name required"})
    raw_key  = "sk_live_" + _sec.token_hex(32)
    key_hash = _hl.sha256(raw_key.encode()).hexdigest()
    prefix   = raw_key[:16] + "…"
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kid = execute("INSERT INTO api_keys (user_id,name,key_hash,key_prefix,created_at) VALUES (?,?,?,?,?)",
                  [session["user_id"], name, key_hash, prefix, now])
    log_action("API_KEY_CREATE", detail=f"Key '{name}' created (#{kid})")
    return jsonify({"ok": True, "id": kid, "key": raw_key,
                    "msg": "Save this key — it won't be shown again."})

@bp.route("/api/key/<int:key_id>", methods=["DELETE"])
@login_required
@admin_required
def api_delete_key(key_id):
    execute("UPDATE api_keys SET active=0 WHERE id=?", [key_id])
    log_action("API_KEY_REVOKE", detail=f"Key #{key_id} revoked")
    return jsonify({"ok": True})


# ── 2FA management ────────────────────────────────────────────────────────────

@bp.route("/api/user/2fa", methods=["POST"])
@login_required
def api_toggle_2fa():
    d       = request.json or {}
    enabled = bool(d.get("enabled"))
    uid     = session.get("user_id")
    user    = query("SELECT email FROM users WHERE id=?", [uid], one=True)
    if not user or not user["email"]:
        return jsonify({"ok": False, "msg": "Account has no email address"})
    from config import Config as _Cfg
    if enabled and not _Cfg.SMTP_HOST:
        return jsonify({"ok": False, "msg": "2FA requires email. Ask your admin to configure SMTP."})
    execute("UPDATE users SET two_fa_enabled=? WHERE id=?", [1 if enabled else 0, uid])
    log_action("2FA_TOGGLE", detail=f"2FA {'enabled' if enabled else 'disabled'} for user #{uid}")
    return jsonify({"ok": True, "enabled": enabled})


# ── Item limit enforcement ────────────────────────────────────────────────────

def _check_item_limit():
    """Returns (allowed, msg). Call before inserting a new item."""
    from flask import g
    from app.stripe_billing import PLANS
    plan_key  = g.tenant.get("plan", "starter") if hasattr(g, "tenant") and g.tenant else "starter"
    max_items = PLANS.get(plan_key, PLANS["starter"]).get("max_items")
    if max_items is None:
        return True, None
    current = query("SELECT COUNT(*) FROM items WHERE active=1", one=True)[0]
    if current >= max_items:
        plan_name = PLANS.get(plan_key, {}).get("name", plan_key)
        return False, (f"Item limit reached ({max_items} on {plan_name} plan). "
                       f"Upgrade your plan to add more items.")
    return True, None


# ── Webhooks ──────────────────────────────────────────────────────────────────

WEBHOOK_EVENTS = [
    "item.added", "item.updated", "item.deleted",
    "item.checked_out", "item.checked_in", "item.sold", "item.retired",
    "low_stock", "po.created", "po.approved", "po.rejected", "po.received",
    "warranty.expiring",
]


@bp.route("/api/webhooks")
@login_required
@admin_required
def api_webhooks_list():
    rows = query("SELECT * FROM webhooks ORDER BY id DESC")
    return jsonify({"ok": True, "webhooks": [dict(r) for r in rows],
                    "events": WEBHOOK_EVENTS})


@bp.route("/api/webhooks", methods=["POST"])
@login_required
@admin_required
def api_webhooks_create():
    d      = request.json or {}
    url    = (d.get("url") or "").strip()
    events = (d.get("events") or ",".join(WEBHOOK_EVENTS)).strip()
    secret = (d.get("secret") or "").strip()
    if not url or not url.startswith("http"):
        return jsonify({"ok": False, "msg": "Valid HTTPS URL required"}), 400
    import secrets as _sec
    if not secret:
        secret = _sec.token_hex(20)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wid = execute(
        "INSERT INTO webhooks (url,events,secret,enabled,created_by,created_at) VALUES (?,?,?,1,?,?)",
        [url, events, secret, session.get("username"), now])
    return jsonify({"ok": True, "id": wid, "secret": secret})


@bp.route("/api/webhooks/<int:wid>", methods=["DELETE"])
@login_required
@admin_required
def api_webhooks_delete(wid):
    execute("DELETE FROM webhooks WHERE id=?", [wid])
    return jsonify({"ok": True})


@bp.route("/api/webhooks/<int:wid>/toggle", methods=["POST"])
@login_required
@admin_required
def api_webhooks_toggle(wid):
    row = query("SELECT enabled FROM webhooks WHERE id=?", [wid], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    new_val = 0 if row["enabled"] else 1
    execute("UPDATE webhooks SET enabled=? WHERE id=?", [new_val, wid])
    return jsonify({"ok": True, "enabled": bool(new_val)})


@bp.route("/api/webhooks/<int:wid>/test", methods=["POST"])
@login_required
@admin_required
def api_webhooks_test(wid):
    row = query("SELECT * FROM webhooks WHERE id=?", [wid], one=True)
    if not row:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    from app.webhooks import _deliver
    _deliver(row["id"], row["url"], row["secret"] or "", "ping",
             {"message": "This is a test webhook from CountDepot"})
    return jsonify({"ok": True})


@bp.route("/api/webhooks/log")
@login_required
@admin_required
def api_webhooks_log():
    rows = query(
        "SELECT wl.*, w.url FROM webhook_log wl "
        "JOIN webhooks w ON w.id=wl.webhook_id "
        "ORDER BY wl.id DESC LIMIT 100")
    return jsonify({"ok": True, "log": [dict(r) for r in rows]})


# ── Notifications ─────────────────────────────────────────────────────────────

@bp.route("/api/notifications")
@login_required
def api_notifications_list():
    uid  = session["user_id"]
    rows = query(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", [uid])
    unread = sum(1 for r in rows if not r["read"])
    return jsonify({"ok": True, "notifications": [dict(r) for r in rows], "unread": unread})


@bp.route("/api/notifications/read-all", methods=["POST"])
@login_required
def api_notifications_read_all():
    execute("UPDATE notifications SET read=1 WHERE user_id=? AND read=0", [session["user_id"]])
    return jsonify({"ok": True})


@bp.route("/api/notifications/<int:nid>/read", methods=["POST"])
@login_required
def api_notification_read(nid):
    execute("UPDATE notifications SET read=1 WHERE id=? AND user_id=?",
            [nid, session["user_id"]])
    return jsonify({"ok": True})


@bp.route("/api/notifications/dismiss-all", methods=["POST"])
@login_required
def api_notifications_dismiss_all():
    execute("DELETE FROM notifications WHERE user_id=? AND read=1", [session["user_id"]])
    return jsonify({"ok": True})


@bp.route("/api/notifications/generate", methods=["POST"])
@login_required
@admin_required
def api_notifications_generate():
    """Scan for low stock, overdue checkouts, expiring warranties and push notifications."""
    from app.helpers import push_notification
    from datetime import date, timedelta
    today = date.today()
    count = 0

    # Low stock
    low = query("""
        SELECT p.id, p.name, p.low_stock_threshold,
               COALESCE(SUM(i.qty),0)-COALESCE(SUM(i.qty_out),0) AS avail
        FROM products p
        JOIN items i ON i.product_id=p.id
        WHERE p.low_stock_threshold IS NOT NULL AND p.low_stock_threshold>0
          AND COALESCE(i.sold,0)=0 AND COALESCE(i.retired,0)=0
        GROUP BY p.id
        HAVING avail < p.low_stock_threshold
    """)
    for row in low:
        push_notification("low_stock",
                          f"Low stock: {row['name']}",
                          f"Only {int(row['avail'])} available (threshold {row['low_stock_threshold']})",
                          "/inventory")
        count += 1

    # Overdue checkouts (checked out, no due date or due date past)
    overdue = query("""
        SELECT cl.id, i.name, cl.checked_out_by, cl.due_date
        FROM checkout_log cl
        JOIN items i ON i.id=cl.item_id
        WHERE cl.returned_at IS NULL
          AND cl.due_date IS NOT NULL AND cl.due_date < ?
    """, [today.isoformat()])
    for row in overdue:
        push_notification("overdue_checkout",
                          f"Overdue checkout: {row['name']}",
                          f"Checked out by {row['checked_out_by']}, due {row['due_date']}",
                          "/inventory")
        count += 1

    # Warranties expiring within 30 days
    in_30 = (today + timedelta(days=30)).isoformat()
    expiring = query("""
        SELECT id, name, warranty_expiry FROM items
        WHERE warranty_expiry IS NOT NULL
          AND warranty_expiry >= ? AND warranty_expiry <= ?
          AND COALESCE(sold,0)=0 AND COALESCE(retired,0)=0
    """, [today.isoformat(), in_30])
    for row in expiring:
        push_notification("warranty_expiry",
                          f"Warranty expiring: {row['name']}",
                          f"Warranty expires {row['warranty_expiry']}",
                          "/report/warranties")
        count += 1

    return jsonify({"ok": True, "pushed": count})


# ── Login Activity ────────────────────────────────────────────────────────────

@bp.route("/api/login-activity")
@login_required
@admin_required
def api_login_activity():
    """Return recent login log entries for this tenant."""
    limit  = min(int(request.args.get("limit", 200)), 1000)
    result = request.args.get("result", "").strip()
    user_f = request.args.get("username", "").strip()
    wheres = []
    args   = []
    if result:
        wheres.append("result=?")
        args.append(result)
    if user_f:
        wheres.append("LOWER(username) LIKE ?")
        args.append(f"%{user_f.lower()}%")
    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    rows = query(
        f"SELECT * FROM login_log {where_sql} ORDER BY id DESC LIMIT ?",
        args + [limit]
    )
    return jsonify({"ok": True, "entries": [dict(r) for r in rows]})


# ── Export / Import ───────────────────────────────────────────────────────────

@bp.route("/export/full-data")
@login_required
@admin_required
def export_full_data():
    """Download all tenant data as a ZIP archive of CSV files."""
    import csv, io as _io, zipfile
    from datetime import datetime as _dt
    from flask import send_file, g

    today = _dt.now().strftime("%Y-%m-%d")
    slug  = getattr(g, "tenant_slug", "export")

    # Map of filename → (SQL query, column names list or None to auto-detect)
    exports = [
        ("items.csv",            "SELECT * FROM items WHERE active=1 ORDER BY name"),
        ("products.csv",         "SELECT * FROM products WHERE active=1 ORDER BY name"),
        ("categories.csv",       "SELECT * FROM categories WHERE active=1 ORDER BY name"),
        ("audit_log.csv",        "SELECT * FROM audit_log ORDER BY ts DESC LIMIT 50000"),
        ("checkout_log.csv",     "SELECT * FROM checkout_log ORDER BY checkout_date DESC LIMIT 20000"),
        ("item_notes.csv",       "SELECT * FROM item_notes ORDER BY created_at DESC"),
        ("users.csv",            "SELECT id,username,email,role,permissions,created_at,active FROM users ORDER BY id"),
        ("purchase_orders.csv",  "SELECT * FROM purchase_orders ORDER BY id DESC"),
        ("po_lines.csv",         "SELECT * FROM po_lines ORDER BY po_id"),
    ]

    zip_buf = _io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, sql in exports:
            try:
                rows = query(sql)
            except Exception:
                continue
            if not rows:
                continue
            csv_buf = _io.StringIO()
            cols = rows[0].keys()
            writer = csv.DictWriter(csv_buf, fieldnames=cols)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
            zf.writestr(f"{slug}_data/{filename}", csv_buf.getvalue())

        # Write a README
        readme = (f"CountDepot Full Data Export\n"
                  f"Workspace: {slug}\n"
                  f"Exported: {today}\n\n"
                  f"Files:\n" +
                  "\n".join(f"  {fn}" for fn, _ in exports) +
                  "\n\nThis export contains all active data. Deleted/inactive records are excluded.\n")
        zf.writestr(f"{slug}_data/README.txt", readme)

    zip_buf.seek(0)
    log_action("FULL_DATA_EXPORT", detail=f"Full data export downloaded by {session.get('username')}")
    return send_file(zip_buf, mimetype="application/zip",
                     as_attachment=True, download_name=f"{slug}_data_{today}.zip")


@bp.route("/export/csv")
@login_required
@perm_required("import_export")
@api_rate_limit(max_attempts=10, window=60)
def export_csv():
    rows = query("""SELECT i.name,i.manufacturer,i.model,i.serial,i.sku,
                           c.name as cat_name,p.name as prod_name,
                           i.condition,i.owner_company,i.purchase_date,i.shelf,i.qty,
                           COALESCE(i.qty_out,0),i.checked_out,i.checkout_by,i.job_ref,
                           i.cost_price,i.sale_price,i.tax_paid,i.tax_rate,
                           i.low_stock_threshold,i.sale_state,i.po_number,
                           i.internal_sku,i.notes,i.created_at
                    FROM items i
                    LEFT JOIN categories c ON c.id=i.category_id
                    LEFT JOIN products p ON p.id=i.product_id
                    WHERE i.active=1 ORDER BY i.name""")
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Name","Manufacturer","Model","Serial","SKU","Category","Product",
                "Condition","Owner/Company","Purchase Date","Shelf","Qty","Qty Out",
                "Checked Out","Checked Out By","Job Ref","Cost ($)","Sale ($)",
                "Tax Paid","Tax Rate (%)","Low Stock Threshold","Sale State","PO #",
                "Internal SKU","Notes","Created At"])
    for r in rows:
        row = list(r)
        row[13] = "Yes" if row[13] else "No"
        row[18] = {1: "Yes", 0: "No"}.get(row[18], "Not Set")
        w.writerow(row)
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()), mimetype="text/csv",
                     as_attachment=True,
                     download_name=f"countdepot_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")


@bp.route("/export/excel")
@login_required
@perm_required("import_export")
@api_rate_limit(max_attempts=10, window=60)
def export_excel():
    from flask import g
    from app.stripe_billing import PLANS
    plan_key = g.tenant.get("plan", "starter") if hasattr(g, "tenant") and g.tenant else "starter"
    if not PLANS.get(plan_key, {}).get("excel", False):
        return jsonify({"ok": False, "msg": "Excel export requires Pro or Enterprise plan."}), 403
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return "openpyxl not installed", 500
    rows = query("""SELECT i.name,i.manufacturer,i.model,i.serial,i.sku,
                           c.name as cat_name,p.name as prod_name,
                           i.condition,i.owner_company,i.purchase_date,i.shelf,i.qty,
                           COALESCE(i.qty_out,0),i.checked_out,i.checkout_by,i.job_ref,
                           i.cost_price,i.sale_price,i.tax_paid,i.tax_rate,
                           i.low_stock_threshold,i.sale_state,i.po_number,
                           i.internal_sku,i.notes,i.created_at
                    FROM items i
                    LEFT JOIN categories c ON c.id=i.category_id
                    LEFT JOIN products p ON p.id=i.product_id
                    WHERE i.active=1 ORDER BY c.name,i.name""")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inventory"
    hdrs  = ["Name","Manufacturer","Model","Serial","SKU","Category","Product",
             "Condition","Owner/Company","Purchase Date","Shelf","Qty Total","Qty Out",
             "Checked Out","Checked Out By","Job Ref","Cost ($)","Sale ($)",
             "Tax Paid","Tax Rate (%)","Low Stock Threshold","Sale State","PO #",
             "Internal SKU","Notes","Created At"]
    hfill = PatternFill("solid", fgColor="0F172A")
    hfont = Font(color="FFFFFF", bold=True, size=10)
    for col, h in enumerate(hdrs, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = hfill; c.font = hfont; c.alignment = Alignment(horizontal="center")
    for ri, r in enumerate(rows, 2):
        vals = list(r)
        vals[13] = "Yes" if vals[13] else "No"
        vals[18] = {1: "Yes", 0: "No"}.get(vals[18], "Not Set")
        for ci, v in enumerate(vals, 1):
            ws.cell(row=ri, column=ci, value=v)
    ws2 = wb.create_sheet("Summary")
    total = query("SELECT COUNT(*) FROM items WHERE active=1", one=True)[0]
    out_c = query("SELECT COUNT(*) FROM items WHERE active=1 AND checked_out=1", one=True)[0]
    tcost = query("SELECT SUM(cost_price) FROM items WHERE active=1", one=True)[0] or 0
    tsale = query("SELECT SUM(sale_price) FROM items WHERE active=1 AND sale_price IS NOT NULL", one=True)[0] or 0
    ws2.append(["Metric", "Value"])
    for row in [("Total Items", total), ("Checked Out", out_c), ("Available", total-out_c),
                ("Total Cost Value", round(tcost, 2)), ("Total Sale Value", round(tsale, 2)),
                ("Potential Profit", round(tsale-tcost, 2)),
                ("Export Date", datetime.now().strftime("%Y-%m-%d %H:%M"))]:
        ws2.append(list(row))
    output = io.BytesIO()
    wb.save(output); output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"countdepot_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")


@bp.route("/import/template")
@login_required
@perm_required("import_export")
def import_template():
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return "openpyxl not installed", 500
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Import Template"
    headers = ["Name *", "Category", "Product Template", "Serial Number",
               "Vendor SKU", "Manufacturer", "Model", "Condition",
               "Shelf / Location", "Owner / Company", "Purchase Date (YYYY-MM-DD)",
               "Distributor", "PO Number", "Cost Price ($)", "Sale Price ($)",
               "Qty (leave blank for serial-tracked)", "Notes"]
    example = ["Dell Latitude 5520", "Laptops", "Dell Latitude", "ABC123456",
               "", "Dell", "Latitude 5520", "New", "A-12", "Acme Corp",
               "2024-01-15", "CDW", "PO-2024-001", "850.00", "1200.00", "", "Arrived in good condition"]
    hfill = PatternFill("solid", fgColor="0F172A")
    hfont = Font(color="FFFFFF", bold=True, size=10)
    rfill = PatternFill("solid", fgColor="F0F9FF")
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = hfill; c.font = hfont
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = max(16, len(h) + 2)
    for col, v in enumerate(example, 1):
        c = ws.cell(row=2, column=col, value=v)
        c.fill = rfill; c.font = Font(size=10, italic=True, color="475569")
    ws2 = wb.create_sheet("Instructions")
    instructions = [
        ("CountDepot Import Template — Instructions", True),
        ("", False),
        ("Required columns:", True),
        ("  Name *  — Item name (required)", False),
        ("", False),
        ("Optional but recommended:", True),
        ("  Category       — Must match an existing category name exactly", False),
        ("  Serial Number  — Required if product has serial tracking enabled", False),
        ("  Purchase Date  — Format: YYYY-MM-DD (e.g. 2024-06-15)", False),
        ("  Cost Price     — Numbers only, no $ sign (e.g. 850.00)", False),
        ("  Condition      — One of: New, Used-VG, Used-Good, Used, For Parts", False),
        ("", False),
        ("Tips:", True),
        ("  - Leave Qty blank for serial-tracked items (one row = one physical item)", False),
        ("  - Fill in Qty for bulk/consumable items (one row = many units)", False),
        ("  - Delete the example row (row 2) before importing", False),
        ("  - Use the Preview button to check for errors before committing", False),
    ]
    ws2.column_dimensions["A"].width = 60
    for row, (text, bold) in enumerate(instructions, 1):
        c = ws2.cell(row=row, column=1, value=text)
        c.font = Font(bold=bold, size=11 if bold else 10)
    output = io.BytesIO()
    wb.save(output); output.seek(0)
    return send_file(output,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="countdepot_import_template.xlsx")


@bp.route("/import/excel", methods=["POST"])
@login_required
@admin_required
def import_excel():
    from flask import g
    from app.stripe_billing import PLANS
    plan_key = g.tenant.get("plan", "starter") if hasattr(g, "tenant") and g.tenant else "starter"
    if not PLANS.get(plan_key, {}).get("excel", False):
        return jsonify({"ok": False, "msg": "Excel import requires Pro or Enterprise plan."}), 403
    try:
        import openpyxl
    except ImportError:
        return jsonify({"ok": False, "msg": "openpyxl not installed"})
    f = request.files.get("file")
    if not f: return jsonify({"ok": False, "msg": "No file uploaded"})
    dry_run = request.form.get("dry_run", "0") == "1"
    try:
        wb   = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws   = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2: return jsonify({"ok": False, "msg": "File is empty"})
        cat_map  = {r["name"].lower(): r["id"] for r in query("SELECT id,name FROM categories")}
        prod_map = {r["name"].lower(): r["id"] for r in query("SELECT id,name FROM products WHERE active=1")}
        added = skipped = 0; errors = []; preview = []
        for i, row in enumerate(rows[1:], 2):
            try:
                name = str(row[0] or "").strip()
                if not name:
                    skipped += 1
                    if dry_run: preview.append({"row": i, "name": "—", "status": "skip", "note": "Empty name"})
                    continue
                cat_name  = str(row[5] or "").strip()
                prod_name = str(row[6] or "").strip()
                row_errors = []
                if cat_name and not cat_map.get(cat_name.lower()):
                    row_errors.append(f"unknown category '{cat_name}'")
                if prod_name and not prod_map.get(prod_name.lower()):
                    row_errors.append(f"unknown product '{prod_name}'")
                if dry_run:
                    status = "error" if row_errors else "ok"
                    preview.append({"row": i, "name": name,
                                    "category": cat_name or "—", "product": prod_name or "—",
                                    "serial": str(row[3] or "").strip() or "—",
                                    "shelf":  str(row[10] or "").strip() or "—",
                                    "cost":   row[16], "status": status,
                                    "note":   "; ".join(row_errors) if row_errors else ""})
                    if not row_errors: added += 1
                    else: errors.append(f"Row {i}: {'; '.join(row_errors)}")
                    continue
                _save_item(dict(name=name,
                    manufacturer=str(row[1] or "").strip() or None,
                    model=str(row[2] or "").strip() or None,
                    serial=str(row[3] or "").strip() or None,
                    sku=str(row[4] or "").strip() or None,
                    category_id=cat_map.get(cat_name.lower()),
                    product_id=prod_map.get(prod_name.lower()),
                    condition=str(row[7] or "New").strip(),
                    owner_company=str(row[8] or "").strip() or None,
                    purchase_date=str(row[9] or "").strip() or None,
                    shelf=str(row[10] or "").strip() or None,
                    qty=row[11], tax_paid=-1, tax_rate=row[19] or 0,
                    low_stock_threshold=int(row[20]) if row[20] else 0,
                    notes=str(row[24] or "").strip(),
                    cost_price=row[16], sale_price=row[17]))
                log_action("ITEM_IMPORT", detail=f"Imported: {name}")
                added += 1
            except Exception as e:
                errors.append(f"Row {i}: {e}")
        if dry_run:
            return jsonify({"ok": True, "dry_run": True, "preview": preview,
                            "would_add": added, "skipped": skipped, "errors": errors[:20]})
        return jsonify({"ok": True, "added": added, "skipped": skipped, "errors": errors[:10]})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ── Asset Reservations ───────────────────────────────────────────────────────

@bp.route("/api/reservations")
@login_required
def api_reservations_list():
    from_d = request.args.get("from", "")
    to_d   = request.args.get("to", "")
    item_id = request.args.get("item_id", "")
    user_id = request.args.get("user_id", "")
    sql  = """SELECT r.*, i.name as item_name, i.serial, i.sku, i.internal_sku,
                     c.name as category, c.color
              FROM reservations r
              JOIN items i ON i.id=r.item_id
              LEFT JOIN categories c ON c.id=i.category_id
              WHERE r.status != 'cancelled'"""
    args = []
    if item_id: sql += " AND r.item_id=?"; args.append(item_id)
    if user_id: sql += " AND r.user_id=?"; args.append(user_id)
    if from_d:  sql += " AND r.end_date>=?"; args.append(from_d)
    if to_d:    sql += " AND r.start_date<=?"; args.append(to_d)
    sql += " ORDER BY r.start_date ASC"
    rows = query(sql, args)
    return jsonify({"ok": True, "reservations": [dict(r) for r in rows]})


@bp.route("/api/reservations", methods=["POST"])
@login_required
def api_reservations_create():
    d          = request.json or {}
    item_id    = int(d.get("item_id", 0))
    start_date = (d.get("start_date") or "").strip()
    end_date   = (d.get("end_date") or "").strip()
    reserved_by = (d.get("reserved_by") or session.get("username", "")).strip()
    notes      = (d.get("notes") or "").strip()
    if not item_id or not start_date or not end_date or not reserved_by:
        return jsonify({"ok": False, "msg": "item_id, start_date, end_date, reserved_by required"})
    if end_date < start_date:
        return jsonify({"ok": False, "msg": "end_date must be on or after start_date"})

    item = query("SELECT * FROM items WHERE id=? AND active=1", [item_id], one=True)
    if not item:
        return jsonify({"ok": False, "msg": "Item not found"})

    # Conflict check — for serial/non-qty items: any overlap blocks booking
    # For qty-tracked items: sum reserved qty and compare to available qty
    if item.get("qty_tracked") and item.get("qty"):
        reserved_qty = query(
            "SELECT COALESCE(SUM(1),0) FROM reservations "
            "WHERE item_id=? AND status NOT IN ('cancelled') "
            "AND NOT (end_date<? OR start_date>?)",
            [item_id, start_date, end_date], one=True)[0]
        available = (item["qty"] or 0) - (item["qty_out"] or 0)
        if reserved_qty >= available:
            return jsonify({"ok": False, "msg": f"No available units for that date range (available: {available}, already reserved: {reserved_qty})"})
    else:
        conflict = query(
            "SELECT 1 FROM reservations WHERE item_id=? AND status NOT IN ('cancelled') "
            "AND NOT (end_date<? OR start_date>?) LIMIT 1",
            [item_id, start_date, end_date], one=True)
        if conflict:
            return jsonify({"ok": False, "msg": "This item is already reserved for those dates"})

    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_id = execute(
        "INSERT INTO reservations (item_id,user_id,reserved_by,start_date,end_date,status,notes,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [item_id, session.get("user_id"), reserved_by, start_date, end_date, "confirmed", notes, now])
    log_action("RESERVATION_CREATE", item_id=item_id, item_name=item["name"],
               detail=f"Reserved {start_date} → {end_date} for {reserved_by}")
    return jsonify({"ok": True, "id": new_id})


@bp.route("/api/reservations/<int:res_id>/cancel", methods=["POST"])
@login_required
def api_reservations_cancel(res_id):
    res = query("SELECT r.*, i.name as item_name FROM reservations r JOIN items i ON i.id=r.item_id WHERE r.id=?",
                [res_id], one=True)
    if not res:
        return jsonify({"ok": False, "msg": "Not found"})
    # Workers can only cancel their own; admins can cancel any
    if session.get("role") != "admin" and res["user_id"] != session.get("user_id"):
        return jsonify({"ok": False, "msg": "Not authorised"}), 403
    execute("UPDATE reservations SET status='cancelled' WHERE id=?", [res_id])
    log_action("RESERVATION_CANCEL", item_id=res["item_id"], item_name=res["item_name"],
               detail=f"Reservation cancelled for {res['reserved_by']}")
    return jsonify({"ok": True})


@bp.route("/api/reservations/availability")
@login_required
def api_reservations_availability():
    """Return booked date ranges for an item — used to highlight unavailable dates in the picker."""
    item_id = request.args.get("item_id", "")
    if not item_id:
        return jsonify({"ok": False, "msg": "item_id required"})
    rows = query(
        "SELECT start_date, end_date, reserved_by, status FROM reservations "
        "WHERE item_id=? AND status NOT IN ('cancelled') AND end_date>=DATE('now') "
        "ORDER BY start_date",
        [item_id])
    return jsonify({"ok": True, "booked": [dict(r) for r in rows]})


# ── Departments ──────────────────────────────────────────────────────────────

@bp.route("/api/departments")
@login_required
def api_departments_list():
    rows = query("""SELECT d.*, u.username as manager_name
                    FROM departments d
                    LEFT JOIN users u ON u.id=d.manager_user_id
                    ORDER BY d.name""")
    return jsonify({"ok": True, "departments": [dict(r) for r in rows]})


@bp.route("/api/departments", methods=["POST"])
@login_required
@admin_required
def api_departments_create():
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Name required"})
    color   = (d.get("color") or "#64748b").strip()
    manager = int(d["manager_user_id"]) if d.get("manager_user_id") else None
    budget  = float(d["budget"]) if d.get("budget") not in (None, "") else None
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        new_id = execute(
            "INSERT INTO departments (name,color,manager_user_id,budget,created_at) VALUES (?,?,?,?,?)",
            [name, color, manager, budget, now])
        log_action("DEPT_CREATE", detail=f"Department created: {name}")
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@bp.route("/api/departments/<int:dept_id>", methods=["POST"])
@login_required
@admin_required
def api_departments_update(dept_id):
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Name required"})
    color   = (d.get("color") or "#64748b").strip()
    manager = int(d["manager_user_id"]) if d.get("manager_user_id") else None
    budget  = float(d["budget"]) if d.get("budget") not in (None, "") else None
    execute("UPDATE departments SET name=?,color=?,manager_user_id=?,budget=? WHERE id=?",
            [name, color, manager, budget, dept_id])
    log_action("DEPT_UPDATE", detail=f"Department updated: {name}")
    return jsonify({"ok": True})


@bp.route("/api/departments/<int:dept_id>/delete", methods=["POST"])
@login_required
@admin_required
def api_departments_delete(dept_id):
    dept = query("SELECT name FROM departments WHERE id=?", [dept_id], one=True)
    if not dept:
        return jsonify({"ok": False, "msg": "Not found"})
    # Clear department from users and items before deleting
    execute("UPDATE users SET department_id=NULL WHERE department_id=?", [dept_id])
    execute("UPDATE items SET department_id=NULL WHERE department_id=?", [dept_id])
    execute("DELETE FROM departments WHERE id=?", [dept_id])
    log_action("DEPT_DELETE", detail=f"Department deleted: {dept['name']}")
    return jsonify({"ok": True})


@bp.route("/api/departments/<int:dept_id>/assign-user", methods=["POST"])
@login_required
@admin_required
def api_departments_assign_user(dept_id):
    d = request.json or {}
    user_id = int(d.get("user_id", 0))
    execute("UPDATE users SET department_id=? WHERE id=?", [dept_id or None, user_id])
    return jsonify({"ok": True})


# ── Onboarding checklist ─────────────────────────────────────────────────────

ONBOARDING_STEPS = [
    ("add_item",       "Add your first item",              "Add at least one item to inventory"),
    ("checkout",       "Check out an item",                "Use the checkout flow to assign an item"),
    ("invite_user",    "Invite a team member",             "Add a second user account"),
    ("low_stock",      "Set a low stock threshold",        "Set a threshold on any product"),
    ("alert_email",    "Configure alert emails",           "Add an email in Settings → Alerts"),
]

@bp.route("/api/onboarding/progress")
@login_required
def api_onboarding_progress():
    """Returns completion state of each onboarding step. Computed from live DB."""
    from flask import g
    from app.platform import get_platform_db as _gpdb
    from datetime import datetime as _dt, timedelta as _td

    # Only show checklist for tenants < 60 days old or not dismissed
    dismissed = query("SELECT value FROM settings WHERE key='onboarding_dismissed'", one=True)
    if dismissed and dismissed["value"] == "1":
        return jsonify({"ok": True, "dismissed": True, "steps": []})

    done = {
        "add_item":    bool(query("SELECT 1 FROM items WHERE active=1 LIMIT 1", one=True)),
        "checkout":    bool(query("SELECT 1 FROM checkout_log LIMIT 1", one=True)),
        "invite_user": (query("SELECT COUNT(*) FROM users WHERE active=1", one=True) or [0])[0] > 1,
        "low_stock":   bool(query("SELECT 1 FROM products WHERE low_stock_threshold>0 LIMIT 1", one=True)),
        "alert_email": bool(query("SELECT 1 FROM settings WHERE key='low_stock_alert_email' AND value!='' LIMIT 1", one=True)),
    }
    steps = [{"key": k, "label": lbl, "hint": hint, "done": done[k]}
             for k, lbl, hint in ONBOARDING_STEPS]
    all_done = all(s["done"] for s in steps)
    # Auto-dismiss when everything is complete
    if all_done:
        execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('onboarding_dismissed','1')")
    return jsonify({"ok": True, "dismissed": False, "steps": steps,
                    "all_done": all_done,
                    "pct": int(sum(1 for s in steps if s["done"]) / len(steps) * 100)})


@bp.route("/api/onboarding/dismiss", methods=["POST"])
@login_required
@admin_required
def api_onboarding_dismiss():
    execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('onboarding_dismissed','1')")
    return jsonify({"ok": True})


# ── Backward-compat aliases ───────────────────────────────────────────────────

@bp.route("/api/catalog")
@login_required
def api_catalog(): return api_products()

@bp.route("/api/catalog/add", methods=["POST"])
@login_required
@perm_required("write_items")
def api_catalog_add(): return api_product_add()

@bp.route("/api/catalog/edit", methods=["POST"])
@login_required
@perm_required("write_items")
def api_catalog_edit(): return api_product_edit()

@bp.route("/api/catalog/delete", methods=["POST"])
@login_required
@perm_required("delete_items")
def api_catalog_delete(): return api_product_delete()

@bp.route("/api/catalog/to_item", methods=["POST"])
@login_required
@perm_required("write_items")
def api_catalog_to_item(): return api_product_to_item()

@bp.route("/api/product_types")
@login_required
def api_product_types():
    prods = query("SELECT * FROM products WHERE active=1 ORDER BY name")
    return jsonify([{"id": r["id"], "name": r["name"],
                     "serial_required": r["serial_tracked"], "sku_required": 0,
                     "qty_tracked": r["qty_tracked"],
                     "require_scan_checkout": r["require_scan_checkout"], "active": r["active"]}
                    for r in prods])


# ── Sector switcher (admin-only, dev/test tool) ───────────────────────────────

@bp.route("/api/admin/switch-sector", methods=["POST"])
@login_required
@admin_required
def api_switch_sector():
    from flask import g
    from app.sectors import SECTORS, seed_sector_categories
    from app.platform import set_tenant_sector
    from app.db import get_db
    d = request.json or {}
    sector_key = d.get("sector", "").strip()
    if sector_key not in SECTORS:
        return jsonify({"ok": False, "msg": "Unknown sector"})
    db = get_db()
    seed_sector_categories(db, sector_key)
    db.commit()
    set_tenant_sector(g.tenant_slug, sector_key)
    log_action("SECTOR_SWITCH", detail=f"Sector switched to {sector_key}")
    return jsonify({"ok": True, "sector": sector_key, "label": SECTORS[sector_key]["label"]})


@bp.route("/api/admin/sector", methods=["GET"])
@login_required
@admin_required
def api_get_sector():
    from flask import g
    from app.sectors import SECTORS
    from app.platform import get_tenant_by_slug
    tenant = get_tenant_by_slug(g.tenant_slug)
    sector_key = tenant["sector"] if tenant and tenant["sector"] else None
    sector_info = SECTORS.get(sector_key, {}) if sector_key else {}
    return jsonify({
        "ok": True,
        "sector": sector_key,
        "label": sector_info.get("label", "Unknown"),
        "icon": sector_info.get("icon", ""),
        "all_sectors": [
            {"key": k, "label": v["label"], "icon": v["icon"], "description": v["description"]}
            for k, v in SECTORS.items()
        ]
    })


# ── Importer shared helpers ────────────────────────────────────────────────────

def _resolve_site(site_id, new_site_name):
    """Return a site_id: use an existing one or create a new location."""
    if site_id not in (None, "", 0):
        try:
            return int(site_id)
        except (ValueError, TypeError):
            pass
    if new_site_name and str(new_site_name).strip():
        name = str(new_site_name).strip()
        existing = query("SELECT id FROM locations WHERE LOWER(name)=LOWER(?)", [name], one=True)
        if existing:
            return existing["id"]
        now = datetime.utcnow().isoformat()
        return execute("INSERT INTO locations (name, created_at) VALUES (?, ?)", [name, now])
    return None


def _detect_site_in_rows(rows_raw):
    """Scan column headers for a site/ship-to field and return a suggestion dict."""
    site_keywords = ['ship to', 'shipto', 'site', 'warehouse', 'facility',
                     'branch', 'store', 'office', 'plant', 'location']
    site_col = None
    for key in (rows_raw[0].keys() if rows_raw else []):
        kl = key.lower().strip()
        if any(kw in kl for kw in site_keywords):
            site_col = key
            break
    if not site_col:
        return None
    values = [str(r.get(site_col, "") or "").strip() for r in rows_raw]
    values = [v for v in values if v]
    if not values:
        return None
    # Most common non-empty value
    from collections import Counter
    site_name = Counter(values).most_common(1)[0][0]
    existing = query("SELECT id, name FROM locations WHERE LOWER(name)=LOWER(?)", [site_name], one=True)
    return {
        "value":        site_name,
        "matched_id":   existing["id"]   if existing else None,
        "matched_name": existing["name"] if existing else None,
    }


def _detect_columns(headers):
    """Map original column headers to CountDepot field names.
    Returns dict of {field_name → original_header}."""
    # Ordered by priority — more specific patterns listed first
    FIELD_PATTERNS = [
        ("sku",             ["vendor sku", "vendor_sku", "vendor sku/description",
                             "item sku", "product sku", "our sku", "internal sku", "sku #",
                             "part number", "part #", "part no", "part no.", "part num",
                             "item #", "item no", "item no.", "item number", "item code", "item id",
                             "product code", "product #", "product no", "prod #", "prod code",
                             "sku", "code", "barcode", "upc", "upc #", "upc code", "upc/ean", "catalog #", "catalog no",
                             "mfr part", "mfr #", "vendor part"]),
        ("qty",             ["qty ordered", "qty shipped", "qty received", "qty invoiced",
                             "quantity ordered", "quantity shipped", "quantity received",
                             "on hand", "on_hand", "qty on hand", "qty", "quantity",
                             "units ordered", "units shipped", "units", "ordered", "shipped",
                             "received", "stock", "amount", "pcs", "pieces"]),
        ("cost_price",      ["unit cost", "unit price", "unit value", "each", "price each",
                             "cost price", "purchase price", "buy price", "wholesale price",
                             "wholesale", "cost per unit", "cost"]),
        ("sale_price",      ["retail sale price", "sale price", "retail price", "sell price",
                             "selling price", "msrp", "retail"]),
        ("serial",          ["serial number", "serial no", "serial #", "serial num", "serial", "sn", "s/n"]),
        ("name",            ["item name", "product name", "item description", "name", "product", "title"]),
        ("flavor",          ["flavor", "flavour", "scent", "variety", "flavor/scent", "flavor / scent"]),
        ("pill_count",      ["pill/bottle count", "pills/bottle", "pill count", "tablet count",
                             "capsule count", "count per bottle", "pills per bottle",
                             "tablets per bottle", "capsules per bottle", "count per unit",
                             "pills per container", "serving count", "unit count"]),
        ("category",        ["category", "cat", "class", "department", "dept", "type"]),
        ("shelf",           ["shelf location", "bin location", "shelf", "bin", "aisle", "row", "storage"]),
        ("site",            ["ship to", "shipto", "warehouse", "facility", "branch", "site",
                             "store", "office", "plant", "location"]),
        ("expiration_date", ["expiration date", "expiration", "exp date", "exp", "expiry",
                             "best by", "use by", "expires", "best before", "bb date", "use before"]),
        ("lot_number",      ["lot number", "lot #", "lot no", "lot nbr", "lot num",
                             "batch number", "batch no", "batch #", "batch", "lot"]),
        ("notes",           ["notes", "note", "comments", "comment", "remarks", "memo"]),
        ("manufacturer",    ["manufacturer", "brand", "make", "mfr", "mfg", "vendor", "supplier"]),
        ("model",           ["model number", "model #", "model no", "model name", "model"]),
        ("description",     ["description", "desc", "details", "product description", "item description"]),
        ("purchase_date",   ["purchase date", "received date", "date received", "date", "po date"]),
        ("po_number",       ["po number", "po #", "po no", "purchase order", "order #", "p.o."]),
    ]

    col_map = {}   # field → original header
    claimed = set()

    for field, patterns in FIELD_PATTERNS:
        # 1) Exact match (normalised)
        for h in headers:
            if h in claimed:
                continue
            if h.lower().strip() in patterns:
                col_map[field] = h
                claimed.add(h)
                break
        if field in col_map:
            continue
        # 2) Substring match
        for h in headers:
            if h in claimed:
                continue
            hn = h.lower().strip()
            for p in patterns:
                if p in hn or hn in p:
                    col_map[field] = h
                    claimed.add(h)
                    break
            if field in col_map:
                break

    return col_map


def _norm_date_str(val):
    """Normalise date string or datetime object to YYYY-MM-DD."""
    if not val:
        return ""
    # Handle Python datetime / date objects (from openpyxl)
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    val = str(val).strip()
    if not val:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y",
                "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%y"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return val


def _cell_str(c):
    """Convert an Excel cell value to a clean string. Handles dates, floats, etc."""
    if c is None:
        return ""
    if hasattr(c, "strftime"):           # datetime / date object
        return c.strftime("%Y-%m-%d")
    return str(c).strip()


def _read_file_rows(f):
    """Read an uploaded Excel, CSV, or PDF file.
    Returns (headers, list-of-dicts, meta) where meta is a dict with optional
    keys: invoice_no, sales_order, customer_po, vendor_name, ship_to.
    Raises ValueError with a user-readable message on bad/unsupported input."""
    filename = (f.filename or "").lower()

    if filename.endswith(".csv"):
        content = f.read().decode("utf-8-sig", errors="replace")
        reader  = list(csv.DictReader(io.StringIO(content)))
        if not reader:
            raise ValueError("CSV file is empty or has no data rows")
        headers = list(reader[0].keys())
        return headers, [dict(r) for r in reader], {}

    elif filename.endswith((".xlsx", ".xls")):
        import openpyxl
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        if not all_rows:
            raise ValueError("Excel file is empty")
        # Find the best header row in the first 20 rows.
        # Score each row by how many cells match known column header keywords.
        # Falls back to the row with the most non-empty cells if nothing scores.
        HEADER_HINTS = {
            'name', 'serial', 'serial number', 'serial no', 'serial #', 'sn', 's/n',
            'sku', 'vendor sku', 'part number', 'part #', 'item #', 'item number',
            'item code', 'product code', 'barcode', 'upc', 'code',
            'qty', 'quantity', 'count', 'units', 'stock', 'amount',
            'cost', 'cost price', 'unit cost', 'unit price', 'price',
            'sale price', 'retail price',
            'category', 'class', 'department', 'type',
            'shelf', 'bin', 'location', 'site', 'warehouse',
            'description', 'desc', 'product', 'model', 'manufacturer', 'brand',
            'notes', 'note', 'po number', 'po #', 'purchase date', 'date',
            'expiration', 'expiration date', 'exp date', 'lot', 'lot number', 'batch',
            'flavor', 'flavour', 'variety', 'retail sale price', 'retail price',
            'pill count', 'pill/bottle count', 'tablet count', 'capsule count',
            'item sku', 'product sku', 'our sku', 'internal sku',
        }
        best_idx = 0
        best_score = -1
        for i, row in enumerate(all_rows[:20]):
            cells = [str(c or '').strip() for c in row]
            non_empty = sum(1 for c in cells if c)
            if non_empty == 0:
                continue
            keyword_hits = sum(1 for c in cells if c.lower() in HEADER_HINTS)
            # Weight: keyword hits count for a lot; non-empty count is a tiebreaker
            score = keyword_hits * 100 + non_empty
            if score > best_score:
                best_score = score
                best_idx = i
        header_row_idx = best_idx
        headers = [str(h or "").strip() for h in all_rows[header_row_idx]]
        # Deduplicate blank headers
        seen = {}
        clean_headers = []
        for h in headers:
            if not h:
                h = "_col"
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 0
            clean_headers.append(h)
        rows = []
        for row in all_rows[header_row_idx + 1:]:
            if not any(c is not None and str(c).strip() for c in row):
                continue
            rows.append(dict(zip(clean_headers, [_cell_str(c) for c in row])))
        return clean_headers, rows, {}

    elif filename.endswith(".pdf"):
        return _read_pdf_rows(f)

    else:
        raise ValueError("Unsupported file type. Upload .xlsx, .csv, or .pdf")


def _read_pdf_rows(f):
    """Parse an invoice PDF. Uses three tiers in order:
      1. pdfplumber extract_tables() — works when PDF has ruled table borders.
      2. Word-position column detection — works for fixed-position text layouts.
      3. Generic row/column extraction — always works; user maps columns in UI.
    Returns (headers, rows, meta).
    """
    import re
    try:
        import pdfplumber
    except ImportError:
        raise ValueError("PDF parsing requires pdfplumber. Run: pip install pdfplumber")

    TOTALS_PHRASES = ('sales total', 'subtotal', 'tax total', 'order total',
                      'balance due', 'total (usd)', 'total paid', 'amount due',
                      'total due', 'invoice total', 'grand total')
    DATE_RE = re.compile(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}')

    content = f.read()
    meta = {}

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        lines = [l.strip() for l in full_text.split('\n') if l.strip()]

        # ── Extract invoice metadata from full text ────────────────────────
        for pattern, key in [
            (r'Invoice\s*(?:No\.?|#)\s*[:\-]?\s*(\S+)', 'invoice_no'),
            (r'Sales\s+Order\s*(?:No\.?)?\s*[:\-]?\s*(\S+)', 'sales_order'),
            (r'Customer\s+PO\s*[:\-]?\s*(\S+)', 'customer_po'),
            (r'P\.?O\.?\s*(?:Number|No\.?|#)\s*[:\-]?\s*(\S+)', 'customer_po'),
        ]:
            if key not in meta:
                m = re.search(pattern, full_text, re.IGNORECASE)
                if m:
                    meta[key] = m.group(1).strip().rstrip('.,')

        for i, line in enumerate(lines[:15]):
            if re.search(r'Invoice|Order|Receipt|Packing Slip', line, re.IGNORECASE):
                if i > 0 and len(lines[i-1]) > 3:
                    meta['vendor_name'] = lines[i-1]
                break

        SECTION_HEADERS = re.compile(
            r'^(bill\s*to|ship\s*to|sold\s*to|remit\s*to|from|vendor|supplier'
            r'|attn|attention|contact|po\s*box|item|qty|description|total)',
            re.IGNORECASE
        )
        for i, line in enumerate(lines):
            if re.search(r'SHIP\s*TO', line, re.IGNORECASE):
                after = re.split(r'SHIP\s*TO\s*:?\s*', line, flags=re.IGNORECASE)[-1].strip()
                # Collect content lines: start from the inline remainder or the next line
                content_lines = []
                if after:
                    # "BILL TO: SHIP TO: CompanyName..." — everything after SHIP TO: on this line
                    # When BILL TO and SHIP TO are side-by-side, pdfplumber may put both
                    # addresses on the same line. Split by taking the right half.
                    parts = after.split()
                    half = len(parts) // 2
                    content_lines.append(' '.join(parts[half:]).strip() if half else after)
                    start = i + 1
                else:
                    start = i + 1

                # Grab up to 4 more address lines after the first content
                for j in range(start, min(start + 4, len(lines))):
                    candidate = lines[j].strip()
                    if not candidate:
                        break
                    if SECTION_HEADERS.match(candidate):
                        break
                    # Same side-by-side duplicate handling: if the line content
                    # repeats itself (left=right column), take the right half
                    words = candidate.split()
                    mid = len(words) // 2
                    if mid >= 2 and words[:mid] == words[mid:]:
                        candidate = ' '.join(words[mid:])
                    content_lines.append(candidate)

                if not content_lines and i + 1 < len(lines):
                    # fallback: just take the next line as-is
                    content_lines = [lines[i + 1].strip()]

                if content_lines:
                    meta['ship_to']         = content_lines[0]
                    meta['ship_to_address'] = ', '.join(content_lines)
                break

        # ── Tier 1: extract_tables() ───────────────────────────────────────
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if not table or len(table) < 2:
                    continue
                # First non-empty row is headers
                hdr_row = next((r for r in table if any(c for c in r)), None)
                if not hdr_row or len([c for c in hdr_row if c]) < 2:
                    continue
                headers = [str(c or '').strip() for c in hdr_row]
                # Deduplicate blank headers
                seen = {}
                clean = []
                for h in headers:
                    h = h or 'Column'
                    key = h
                    if key in seen:
                        seen[key] += 1
                        h = f"{h} {seen[key]}"
                    else:
                        seen[key] = 0
                    clean.append(h)
                headers = clean
                rows = []
                for row in table[table.index(hdr_row)+1:]:
                    if not any(c for c in row):
                        continue
                    row_text = ' '.join(str(c or '') for c in row).lower()
                    if any(ph in row_text for ph in TOTALS_PHRASES):
                        break
                    rows.append(dict(zip(headers, [str(c or '').strip() for c in row])))
                if rows:
                    return headers, rows, meta

        # ── Tier 2: word-position column detection ─────────────────────────
        HEADER_KEYWORDS = {
            'item': 'item', 'description': 'item', 'product': 'item', 'part': 'item',
            'qty': 'qty', 'quantity': 'qty', 'units': 'qty', 'count': 'qty', 'ordered': 'qty',
            'shipped': 'qty', 'received': 'qty',
            'expiration': 'exp_date', 'exp': 'exp_date', 'expires': 'exp_date',
            'best': 'exp_date', 'expiry': 'exp_date', 'bb': 'exp_date',
            'lot': 'lot_nbr', 'batch': 'lot_nbr',
            'unit': 'unit_val', 'price': 'unit_val', 'cost': 'unit_val',
            'total': 'total_val', 'amount': 'total_val', 'extended': 'total_val',
        }
        FRIENDLY = {
            'item': 'Description/SKU', 'qty': 'Quantity', 'exp_date': 'Expiration Date',
            'lot_nbr': 'Lot Number', 'unit_val': 'Unit Price', 'total_val': 'Total',
        }

        tier2_rows = []
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if not words:
                continue

            # Group into visual rows — use 8pt bands (more forgiving than 4pt)
            y_groups = {}
            for w in words:
                y_key = round(w['top'] / 8) * 8
                y_groups.setdefault(y_key, []).append(w)
            sorted_ys = sorted(y_groups.keys())

            # Find header row: row where at least 2 words match known header keywords
            header_y = None
            col_x = {}  # col_key → x0

            for y in sorted_ys:
                row_words = sorted(y_groups[y], key=lambda w: w['x0'])
                matches = {}
                for w in row_words:
                    wl = w['text'].lower().rstrip('.:')
                    if wl in HEADER_KEYWORDS:
                        ck = HEADER_KEYWORDS[wl]
                        if ck not in matches:
                            matches[ck] = w['x0']
                if len(matches) >= 2:
                    header_y = y
                    col_x = matches
                    break

            if header_y is None or 'qty' not in col_x:
                continue

            qty_x = col_x['qty']
            non_item_cols = [(name, x) for name, x in col_x.items() if name != 'item']

            def word_col(w):
                # Anything clearly left of the qty column = item/description
                if w['x0'] < qty_x - 5:
                    return 'item'
                # For all other words, assign to whichever column header is closest
                if not non_item_cols:
                    return None
                return min(non_item_cols, key=lambda nc: abs(w['x0'] - nc[1]))[0]

            totals_y = None
            for y in sorted_ys:
                if y <= header_y:
                    continue
                rt = ' '.join(w['text'] for w in y_groups[y]).lower()
                if any(ph in rt for ph in TOTALS_PHRASES):
                    totals_y = y
                    break

            current = None
            for y in sorted_ys:
                if y <= header_y + 5:
                    continue
                if totals_y and y >= totals_y:
                    break
                row_words = sorted(y_groups[y], key=lambda w: w['x0'])
                if not row_words:
                    continue

                buckets = {}
                for w in row_words:
                    ck = word_col(w)
                    if ck:
                        buckets.setdefault(ck, []).append(w['text'])

                qty_str = ' '.join(buckets.get('qty', [])).strip()
                exp_str = ' '.join(buckets.get('exp_date', [])).strip()
                item_words = buckets.get('item', [])
                first_x = row_words[0]['x0']

                has_qty_val  = bool(qty_str and re.match(r'^\d[\d,]*$', qty_str))
                has_date_val = bool(exp_str and DATE_RE.search(exp_str))

                if has_qty_val or has_date_val:
                    if current:
                        tier2_rows.append(current)
                    # Split item column: first word = Vendor SKU, rest = Description
                    vendor_sku  = item_words[0].rstrip(':') if item_words else ''
                    description = ' '.join(item_words[1:]).strip()
                    current = {
                        "Vendor SKU":      vendor_sku,
                        "Description":     description,
                        "Quantity":        qty_str,
                        "Expiration Date": exp_str,
                        "Lot Number":      ' '.join(buckets.get('lot_nbr', [])).strip(),
                        "Unit Price":      ' '.join(buckets.get('unit_val', [])).replace('$','').strip(),
                        "Total":           ' '.join(buckets.get('total_val',[])).replace('$','').strip(),
                    }
                elif current and first_x < qty_x * 0.95:
                    cont = ' '.join(w['text'] for w in row_words if w['x0'] < qty_x).strip()
                    if cont:
                        current["Description"] = (current["Description"] + ' ' + cont).strip()

            if current:
                tier2_rows.append(current)

        if tier2_rows:
            hdrs = ["Vendor SKU", "Description", "Quantity", "Expiration Date",
                    "Lot Number", "Unit Price", "Total"]
            return hdrs, tier2_rows, meta

        # ── Tier 3: generic column extraction — always returns something ───
        # Cluster all words by x-position into columns, return every row.
        # User maps columns in the UI.
        all_words = []
        for page in pdf.pages:
            ws = page.extract_words(x_tolerance=3, y_tolerance=3)
            all_words.extend(ws or [])

        if not all_words:
            raise ValueError("Could not extract any text from this PDF. It may be a scanned image.")

        # Find distinct x-position clusters (column bands)
        xs = sorted(set(round(w['x0'] / 10) * 10 for w in all_words))
        # Merge xs within 20pts of each other
        clusters = []
        for x in xs:
            if clusters and x - clusters[-1] < 20:
                continue
            clusters.append(x)
        if not clusters:
            raise ValueError("Could not detect column structure in this PDF.")

        # Assign each word to the nearest cluster
        def nearest_cluster(x):
            return min(clusters, key=lambda c: abs(c - x))

        headers = [f"Column {i+1}" for i in range(len(clusters))]
        cluster_to_header = {c: h for c, h in zip(clusters, headers)}

        y_groups = {}
        for w in all_words:
            y_key = round(w['top'] / 8) * 8
            y_groups.setdefault(y_key, []).append(w)

        tier3_rows = []
        for y in sorted(y_groups.keys()):
            row = {}
            for w in y_groups[y]:
                h = cluster_to_header[nearest_cluster(w['x0'])]
                row[h] = (row.get(h, '') + ' ' + w['text']).strip()
            if any(v for v in row.values()):
                rt = ' '.join(row.values()).lower()
                if not any(ph in rt for ph in TOTALS_PHRASES):
                    tier3_rows.append(row)

        if not tier3_rows:
            raise ValueError("Could not extract any rows from this PDF.")

        return headers, tier3_rows, meta


# ── Invoice Import ─────────────────────────────────────────────────────────────

@bp.route("/invoice-import/template")
@login_required
@perm_required("import_export")
def invoice_import_template():
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return "openpyxl not installed", 500
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Invoice Import"
    headers = ["Vendor SKU *", "Description", "Quantity *", "Unit Price ($)",
               "Expiration Date (MM/DD/YYYY)", "Lot Number"]
    example = ["PROD-1234", "Example Product - 90 count", "24", "7.50",
               "12/31/2026", "LOT-ABC123"]
    hfill = PatternFill("solid", fgColor="0F172A")
    hfont = Font(color="FFFFFF", bold=True, size=10)
    rfill = PatternFill("solid", fgColor="F0F9FF")
    col_widths = [20, 40, 12, 16, 28, 20]
    for col, (h, w) in enumerate(zip(headers, col_widths), 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = hfill; c.font = hfont
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = w
    for col, v in enumerate(example, 1):
        c = ws.cell(row=2, column=col, value=v)
        c.fill = rfill; c.font = Font(size=10, italic=True, color="475569")
    ws2 = wb.create_sheet("Instructions")
    ws2.column_dimensions["A"].width = 65
    instructions = [
        ("CountDepot — Invoice Import Template", True),
        ("", False),
        ("How to use:", True),
        ("  1. Fill in one row per line item from your invoice.", False),
        ("  2. Vendor SKU and Quantity are required. All other columns are optional.", False),
        ("  3. Upload this file on the Invoice Import page.", False),
        ("  4. Review the preview — matched items show in green, unmatched in yellow.", False),
        ("  5. Confirm to add items to your inventory.", False),
        ("", False),
        ("Column guide:", True),
        ("  Vendor SKU *        — The supplier's product code (e.g. PROD-1234)", False),
        ("                        Must match the Vendor SKU set on the product in CountDepot.", False),
        ("  Description         — Optional. Used for display in the preview only.", False),
        ("  Quantity *          — Number of units received.", False),
        ("  Unit Price ($)      — Cost per unit. Stored on the inventory record.", False),
        ("  Expiration Date     — Format: MM/DD/YYYY or YYYY-MM-DD", False),
        ("  Lot Number          — Supplier lot or batch number.", False),
        ("", False),
        ("Tips:", True),
        ("  - One row = one lot/shipment line. Each row creates one inventory record.", False),
        ("  - Set up Vendor SKU on your products (Products page) before importing.", False),
        ("  - Unmatched SKUs are skipped — no data is lost.", False),
    ]
    for row, (text, bold) in enumerate(instructions, 1):
        c = ws2.cell(row=row, column=1, value=text)
        c.font = Font(bold=bold, size=11 if bold else 10)
    out = io.BytesIO()
    wb.save(out); out.seek(0)
    return send_file(out,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="countdepot_invoice_import_template.xlsx")


@bp.route("/api/invoice/parse", methods=["POST"])
@login_required
@perm_required("import_export")
def api_invoice_parse():
    """Parse an uploaded invoice file. Returns raw rows + headers + auto-detected
    column mapping so the UI can show a column-mapping step before the preview."""
    try:
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "msg": "No file uploaded"})
        try:
            headers, rows_raw, file_meta = _read_file_rows(f)
        except ValueError as e:
            return jsonify({"ok": False, "msg": str(e)})
        except Exception as e:
            return jsonify({"ok": False, "msg": "Could not read file: " + str(e)})

        if not rows_raw:
            return jsonify({"ok": False, "msg": "File has no data rows"})

        auto_map = _detect_columns(headers)
        sample_rows = rows_raw[:5]

        site_suggestion = _detect_site_in_rows(rows_raw)
        if not site_suggestion and file_meta.get("ship_to"):
            ship_name = file_meta["ship_to"]
            existing  = query("SELECT id, name FROM locations WHERE LOWER(name)=LOWER(?)", [ship_name], one=True)
            site_suggestion = {
                "value":        ship_name,
                "address":      file_meta.get("ship_to_address", ship_name),
                "matched_id":   existing["id"]   if existing else None,
                "matched_name": existing["name"] if existing else None,
            }

        return jsonify({
            "ok":             True,
            "headers":        headers,
            "rows_raw":       rows_raw,
            "sample_rows":    sample_rows,
            "auto_map":       auto_map,
            "file_meta":      file_meta,
            "site_suggestion": site_suggestion,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": "Unexpected error: " + str(e)})


@bp.route("/api/invoice/preview", methods=["POST"])
@login_required
@perm_required("import_export")
def api_invoice_preview():
    """Apply a user-confirmed column mapping to raw rows and return a matched preview."""
    try:
        d = request.json or {}
        rows_raw = d.get("rows_raw", [])
        mapping  = d.get("mapping", {})  # {field_key: header_name}

        if not rows_raw:
            return jsonify({"ok": False, "msg": "No rows to preview"})

        # Build product lookup maps
        prod_by_vendor_sku = {}
        for p in query("SELECT id, name, vendor_sku FROM products WHERE active=1 AND vendor_sku IS NOT NULL AND vendor_sku != ''"):
            prod_by_vendor_sku[p["vendor_sku"].strip().lower()] = {"id": p["id"], "name": p["name"]}
        prod_by_item_sku = {}
        for r in query("""SELECT DISTINCT i.sku, i.product_id, p.name as product_name
                          FROM items i JOIN products p ON p.id=i.product_id
                          WHERE i.sku IS NOT NULL AND i.sku != '' AND i.product_id IS NOT NULL AND p.active=1"""):
            prod_by_item_sku[r["sku"].strip().lower()] = {"id": r["product_id"], "name": r["product_name"]}

        def _get(row, field):
            col = mapping.get(field)
            if not col:
                return ""
            v = str(row.get(col) or "").strip()
            return "" if v.lower() in ("none", "n/a", "—", "-", "null") else v

        result_rows = []
        matched = unmatched = 0

        for row in rows_raw:
            sku       = _get(row, "sku")
            desc      = _get(row, "description")
            qty_raw   = _get(row, "qty")
            price_raw = _get(row, "cost_price")
            exp_raw   = _get(row, "expiration_date")
            lot       = _get(row, "lot_number")

            if not sku:
                continue
            try:
                qty = int(float(qty_raw.replace(',', ''))) if qty_raw else None
            except (ValueError, TypeError):
                qty = None
            if not qty:
                continue
            try:
                unit_price = float(price_raw.replace(',', '').replace('$', '')) if price_raw else None
            except (ValueError, TypeError):
                unit_price = None

            sku_key = sku.lower()
            product = prod_by_vendor_sku.get(sku_key) or prod_by_item_sku.get(sku_key)
            status  = "matched" if product else "unmatched"
            if product: matched += 1
            else:       unmatched += 1

            result_rows.append({
                "vendor_sku":      sku,
                "description":     desc,
                "qty":             qty,
                "unit_price":      unit_price,
                "expiration_date": _norm_date_str(exp_raw),
                "lot_number":      lot,
                "product_id":      product["id"]   if product else None,
                "product_name":    product["name"] if product else None,
                "status":          status,
            })

        if not result_rows:
            return jsonify({"ok": False, "msg": "No rows with a valid SKU and Quantity found. Check your column mapping."})

        return jsonify({"ok": True, "rows": result_rows,
                        "matched": matched, "unmatched": unmatched})
    except Exception as e:
        return jsonify({"ok": False, "msg": "Unexpected error: " + str(e)})


@bp.route("/api/invoice/mappings", methods=["GET"])
@login_required
@perm_required("import_export")
def api_invoice_get_mapping():
    """Return saved column mapping for a vendor name."""
    vendor = (request.args.get("vendor") or "").strip()
    if not vendor:
        return jsonify({"ok": True, "mapping": None})
    row = query("SELECT mapping FROM invoice_mappings WHERE LOWER(vendor_name)=LOWER(?)",
                [vendor], one=True)
    if not row:
        return jsonify({"ok": True, "mapping": None})
    try:
        return jsonify({"ok": True, "mapping": json.loads(row["mapping"])})
    except Exception:
        return jsonify({"ok": True, "mapping": None})


@bp.route("/api/invoice/mappings", methods=["POST"])
@login_required
@perm_required("import_export")
def api_invoice_save_mapping():
    """Save or update a column mapping for a vendor."""
    d = request.json or {}
    vendor  = (d.get("vendor_name") or "").strip()
    mapping = d.get("mapping")
    if not vendor or not mapping:
        return jsonify({"ok": False, "msg": "vendor_name and mapping required"})
    now = datetime.utcnow().isoformat()
    existing = query("SELECT id FROM invoice_mappings WHERE LOWER(vendor_name)=LOWER(?)",
                     [vendor], one=True)
    if existing:
        execute("UPDATE invoice_mappings SET mapping=?, updated_at=? WHERE id=?",
                [json.dumps(mapping), now, existing["id"]])
    else:
        execute("INSERT INTO invoice_mappings (vendor_name, mapping, created_at, updated_at) VALUES (?,?,?,?)",
                [vendor, json.dumps(mapping), now, now])
    return jsonify({"ok": True})


@bp.route("/api/invoice/commit", methods=["POST"])
@login_required
@perm_required("import_export")
def api_invoice_commit():
    """Commit a parsed invoice import. Creates qty-tracked items for matched rows."""
    d = request.json or {}
    rows    = d.get("rows", [])
    site_id = _resolve_site(d.get("site_id"), d.get("new_site_name"))
    ref     = (d.get("reference") or "").strip() or None
    vendor  = (d.get("vendor") or "").strip() or None
    notes   = (d.get("notes") or "").strip() or None

    new_rows    = d.get("new_rows", [])  # user-confirmed unmatched rows to create
    import_rows = [r for r in rows if r.get("status") == "matched" and r.get("product_id") and r.get("qty")]
    if not import_rows and not new_rows:
        return jsonify({"ok": False, "msg": "No matched rows to import"})

    now = datetime.utcnow().isoformat()
    created_ids = []

    for r in import_rows:
        product_id  = int(r["product_id"])
        qty         = int(r["qty"])
        vendor_sku  = r.get("vendor_sku") or None
        description = r.get("description") or None
        unit_price  = float(r["unit_price"]) if r.get("unit_price") not in (None, "") else None
        exp_date    = (r.get("expiration_date") or "").strip() or None
        lot_number  = (r.get("lot_number") or "").strip() or None

        # Get product info for item name
        prod = query("SELECT name, category_id FROM products WHERE id=?", [product_id], one=True)
        if not prod:
            continue

        # Build extra_fields
        extra = {}
        if exp_date:
            extra["expiration_date"] = exp_date
        if lot_number:
            extra["lot_number"] = lot_number
        if ref:
            extra["invoice_ref"] = ref

        item_name = prod["name"]
        if description and description.lower() != item_name.lower():
            item_name = description

        iid = execute("""
            INSERT INTO items
                (product_id, name, sku, category_id, qty, cost_price,
                 location_id, extra_fields, purchased_from, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, [product_id, item_name, vendor_sku, prod["category_id"],
              qty, unit_price, site_id, json.dumps(extra), vendor, now])

        log_action("INVOICE_IMPORT", iid, item_name,
                   f"Invoice import: qty={qty}, vendor_sku={vendor_sku}, lot={lot_number}, exp={exp_date}, ref={ref}",
                   None, {"qty": qty, "lot_number": lot_number, "expiration_date": exp_date})
        created_ids.append(iid)

    # ── Process user-confirmed new rows (unmatched SKUs the user chose to create) ──
    for r in new_rows:
        vendor_sku    = (r.get("vendor_sku") or "").strip() or None
        prod_name     = (r.get("description") or vendor_sku or "Unknown Item").strip()
        qty           = int(r["qty"]) if r.get("qty") else 0
        unit_price    = float(r["unit_price"]) if r.get("unit_price") not in (None, "") else None
        exp_date      = (r.get("expiration_date") or "").strip() or None
        lot_number    = (r.get("lot_number") or "").strip() or None
        new_cat_name  = (r.get("new_category_name") or "").strip()
        cat_id        = r.get("category_id")

        if not qty:
            continue

        # 1. Resolve / create category
        if not cat_id and new_cat_name:
            existing_cat = query("SELECT id FROM categories WHERE LOWER(name)=LOWER(?)",
                                 [new_cat_name], one=True)
            if existing_cat:
                cat_id = existing_cat["id"]
            else:
                cat_id = execute(
                    "INSERT INTO categories (name, color) VALUES (?, ?)",
                    [new_cat_name, "#6366f1"])
        if not cat_id:
            continue

        # 2. Find or create product
        existing_prod = query(
            "SELECT id FROM products WHERE LOWER(name)=LOWER(?) AND category_id=? AND active=1",
            [prod_name, cat_id], one=True)
        if existing_prod:
            product_id = existing_prod["id"]
        else:
            product_id = execute("""
                INSERT INTO products
                    (name, category_id, qty_tracked, serial_tracked,
                     require_vendor_sku, require_internal_sku, vendor_sku, active, created_at)
                VALUES (?, ?, 1, 0, 1, 0, ?, 1, ?)""",
                [prod_name, cat_id, vendor_sku, now])

        # 3. Create item
        extra = {}
        if exp_date:   extra["expiration_date"] = exp_date
        if lot_number: extra["lot_number"]       = lot_number
        if ref:        extra["invoice_ref"]      = ref

        iid = execute("""
            INSERT INTO items
                (product_id, name, sku, category_id, qty, cost_price,
                 location_id, extra_fields, purchased_from, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            [product_id, prod_name, vendor_sku, cat_id,
             qty, unit_price, site_id, json.dumps(extra), vendor, now])

        log_action("INVOICE_IMPORT", iid, prod_name,
                   f"Invoice import (new product created): qty={qty}, vendor_sku={vendor_sku}, "
                   f"lot={lot_number}, exp={exp_date}, ref={ref}",
                   None, {"qty": qty, "lot_number": lot_number, "expiration_date": exp_date})
        created_ids.append(iid)

    if not created_ids:
        return jsonify({"ok": False, "msg": "No items were created"})

    # Record import summary
    execute("""INSERT INTO invoice_imports (reference, vendor, site_id, imported_by, imported_at, line_count, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [ref, vendor, site_id, session.get("username"), now, len(created_ids), notes])

    # Link to PO if requested: mark PO as received + record po_invoice for 3-way match
    linked_po_id = d.get("linked_po_id")
    if linked_po_id:
        po = query("SELECT * FROM purchase_orders WHERE id=?", [linked_po_id], one=True)
        if po and po["status"] in ("draft", "sent", "partial"):
            execute("UPDATE purchase_orders SET status='received' WHERE id=?", [linked_po_id])
            log_action("PO_RECEIVE", None, po["po_number"],
                       f"Marked received via invoice import: ref={ref}")
        if po:
            # Build a po_invoice from the import rows for 3-way match
            inv_total = sum(
                (float(r.get("unit_price") or 0) * int(r.get("qty") or 0))
                for r in import_rows
            )
            inv_id = execute("""
                INSERT INTO po_invoices (po_id, invoice_ref, invoice_date, vendor_name,
                                         total_amount, status, notes, created_by, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, [linked_po_id, ref, None, vendor, inv_total, "pending",
                  f"Auto-recorded from invoice import", session.get("username"), now])
            # Match import rows to po_lines by product_id or vendor_sku
            po_lines_map = {r["product_id"]: r for r in
                            query("SELECT * FROM po_lines WHERE po_id=?", [linked_po_id])}
            po_lines_sku_map = {r["vendor_sku"]: r for r in
                                query("SELECT * FROM po_lines WHERE po_id=?", [linked_po_id])
                                if r["vendor_sku"]}
            for r in import_rows:
                qty       = int(r.get("qty") or 0)
                price     = float(r.get("unit_price") or 0)
                line_tot  = round(qty * price, 4)
                prod_id   = int(r["product_id"]) if r.get("product_id") else None
                vsku      = r.get("vendor_sku") or None
                desc      = r.get("description") or ""
                # Try to link to a po_line
                po_line   = po_lines_map.get(prod_id) or (po_lines_sku_map.get(vsku) if vsku else None)
                po_line_id = po_line["id"] if po_line else None
                execute("""
                    INSERT INTO po_invoice_lines
                        (invoice_id, po_line_id, description, vendor_sku, qty_billed, unit_price, line_total)
                    VALUES (?,?,?,?,?,?,?)
                """, [inv_id, po_line_id, desc, vsku, qty, price, line_tot])

    return jsonify({"ok": True, "created": len(created_ids), "item_ids": created_ids})


# ── Inventory Importer (general spreadsheet) ───────────────────────────────────

@bp.route("/importer/inventory-template")
@login_required
@perm_required("import_export")
def inventory_importer_template():
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return "openpyxl not installed", 500
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Inventory"
    headers = ["Name *", "Category", "Qty", "Serial Number",
               "Vendor SKU", "Manufacturer", "Model",
               "Cost Price ($)", "Sale Price ($)",
               "Site / Warehouse", "Shelf / Bin",
               "Expiration Date (MM/DD/YYYY)", "Lot Number",
               "Purchase Date (YYYY-MM-DD)", "PO Number", "Notes"]
    example = ["Widget Pro 500mg", "Supplements", "24", "",
               "PROD-1234", "Acme Distributors", "",
               "7.50", "14.95",
               "Main Warehouse", "Shelf A-3",
               "12/31/2026", "LOT-ABC123",
               "2026-04-07", "PO-2026-001", "Received 2026-04-07"]
    hfill = PatternFill("solid", fgColor="0F172A")
    hfont = Font(color="FFFFFF", bold=True, size=10)
    rfill = PatternFill("solid", fgColor="F0F9FF")
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = hfill; c.font = hfont
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = max(14, len(h))
    for col, v in enumerate(example, 1):
        c = ws.cell(row=2, column=col, value=v)
        c.fill = rfill; c.font = Font(size=10, italic=True, color="475569")
    ws2 = wb.create_sheet("Instructions")
    ws2.column_dimensions["A"].width = 70
    instructions = [
        ("CountDepot - Inventory Import Template", True),
        ("", False),
        ("Use this template to import your existing inventory from any spreadsheet.", True),
        ("", False),
        ("Tips:", True),
        ("  - Only Name is required. All other columns are optional.", False),
        ("  - Fill in Qty for bulk items. Leave blank for individual serial-tracked items.", False),
        ("  - Site / Warehouse: if the name matches an existing site it will be linked.", False),
        ("    If it is a new name, CountDepot will create that site automatically.", False),
        ("  - Category must match an existing category name (case-insensitive).", False),
        ("  - You do not need to use this exact template - any Excel with column headers works.", False),
        ("    CountDepot auto-detects common column names like Qty, Serial Number, Cost, etc.", False),
    ]
    for row, (text, bold) in enumerate(instructions, 1):
        c = ws2.cell(row=row, column=1, value=text)
        c.font = Font(bold=bold, size=11 if bold else 10)
    out = io.BytesIO()
    wb.save(out); out.seek(0)
    return send_file(out,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="countdepot_inventory_template.xlsx")


@bp.route("/api/inventory/parse", methods=["POST"])
@login_required
@perm_required("import_export")
def api_inventory_parse():
    try:
        return _api_inventory_parse_inner()
    except Exception as e:
        return jsonify({"ok": False, "msg": "Unexpected error: " + str(e)})


def _api_inventory_parse_inner():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "msg": "No file uploaded"})
    try:
        headers, rows_raw, file_meta = _read_file_rows(f)
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "msg": "Could not read file: " + str(e)})

    if not rows_raw:
        return jsonify({"ok": False, "msg": "File has no data rows"})

    col_map = _detect_columns(headers)

    if "name" not in col_map:
        return jsonify({"ok": False,
                        "msg": "Could not find a Name/Item/Product column. "
                               "Make sure your file has a header row with a column called Name, Item, or Product."})

    cat_map = {r["name"].lower(): r["id"] for r in query("SELECT id, name FROM categories")}

    def _get(row, field):
        col = col_map.get(field)
        if not col: return ""
        v = str(row.get(col) or "").strip()
        return "" if v.lower() in ("none", "n/a", "—", "-") else v

    # Columns that didn't map to any standard field — potential custom fields
    import re as _re
    mapped_cols = set(col_map.values())
    undetected = [h for h in headers if h not in mapped_cols]

    # Only treat as custom field candidates if the column actually has data
    def _col_key(label):
        return _re.sub(r'[^a-z0-9]+', '_', label.lower().strip()).strip('_')

    # Terms that are order/invoice-level and shouldn't become product fields
    _SKIP_CUSTOM = {
        'total', 'line total', 'order total', 'ext price', 'extended price',
        'ext cost', 'subtotal', 'sub total', 'cases ordered', 'cases',
        'amount due', 'invoice total', 'grand total',
    }

    custom_field_candidates = []
    for h in undetected:
        # Skip blank/empty headers (openpyxl deduplicates as _col, _col_1, etc.)
        if _re.match(r'^_col\d*$', h.strip()):
            continue
        # Skip order-level aggregation columns
        if h.lower().strip() in _SKIP_CUSTOM:
            continue
        if any(str(row.get(h) or '').strip() for row in rows_raw):
            custom_field_candidates.append({"label": h, "key": _col_key(h)})

    # Also treat flavor and pill_count as custom fields that need category_fields created,
    # even though they're already mapped to standard keys
    _semi_custom = [
        ("flavor",     "Flavor"),
        ("pill_count", "Pill/Bottle Count"),
    ]
    semi_custom_candidates = []
    for key, label in _semi_custom:
        if key in col_map:
            semi_custom_candidates.append({"label": label, "key": key})

    result_rows = []
    for row in rows_raw:
        name = _get(row, "name") or _get(row, "description")
        if not name:
            continue
        qty_raw = _get(row, "qty")
        try:
            qty = int(float(qty_raw)) if qty_raw else None
        except (ValueError, TypeError):
            qty = None
        try:
            cost = float(_get(row, "cost_price")) if _get(row, "cost_price") else None
        except (ValueError, TypeError):
            cost = None
        try:
            sale = float(_get(row, "sale_price")) if _get(row, "sale_price") else None
        except (ValueError, TypeError):
            sale = None

        cat_name = _get(row, "category")
        cat_id   = cat_map.get(cat_name.lower()) if cat_name else None

        # Collect values for any custom field columns
        custom_vals = {}
        for cf in custom_field_candidates:
            v = str(row.get(cf["label"]) or '').strip()
            if v and v.lower() not in ('none', 'n/a', '—', '-'):
                custom_vals[cf["key"]] = v

        result_rows.append({
            "name":            name,
            "qty":             qty,
            "serial":          _get(row, "serial") or None,
            "sku":             _get(row, "sku") or None,
            "category":        cat_name or None,
            "category_id":     cat_id,
            "cat_matched":     bool(cat_id) if cat_name else None,
            "shelf":           _get(row, "shelf") or None,
            "site_value":      _get(row, "site") or None,
            "cost_price":      cost,
            "sale_price":      sale,
            "expiration_date": _norm_date_str(_get(row, "expiration_date")),
            "lot_number":      _get(row, "lot_number") or None,
            "flavor":          _get(row, "flavor") or None,
            "pill_count":      _get(row, "pill_count") or None,
            "manufacturer":    _get(row, "manufacturer") or None,
            "model":           _get(row, "model") or None,
            "notes":           _get(row, "notes") or None,
            "purchase_date":   _norm_date_str(_get(row, "purchase_date")),
            "po_number":       _get(row, "po_number") or None,
            "custom_fields":   custom_vals,
        })

    if not result_rows:
        return jsonify({"ok": False, "msg": "No valid rows found (all rows missing a name/item value)."})

    detected_labels = {k: v for k, v in col_map.items() if k != "site"}
    site_suggestion = _detect_site_in_rows(rows_raw)
    if not site_suggestion and file_meta.get("ship_to"):
        ship_name = file_meta["ship_to"]
        existing  = query("SELECT id, name FROM locations WHERE LOWER(name)=LOWER(?)", [ship_name], one=True)
        site_suggestion = {
            "value":        ship_name,
            "address":      file_meta.get("ship_to_address", ship_name),
            "matched_id":   existing["id"]   if existing else None,
            "matched_name": existing["name"] if existing else None,
        }

    all_custom_candidates = semi_custom_candidates + custom_field_candidates

    return jsonify({
        "ok":                      True,
        "rows":                    result_rows,
        "total":                   len(result_rows),
        "col_map":                 detected_labels,
        "undetected":              [h for h in undetected if not any(cf["label"] == h for cf in custom_field_candidates)],
        "custom_field_candidates": all_custom_candidates,
        "site_suggestion":         site_suggestion,
    })


@bp.route("/api/inventory/commit", methods=["POST"])
@login_required
@perm_required("import_export")
def api_inventory_commit():
    d = request.json or {}
    rows         = d.get("rows", [])
    site_id      = _resolve_site(d.get("site_id"), d.get("new_site_name"))
    notes_global = (d.get("notes") or "").strip() or None

    if not rows:
        return jsonify({"ok": False, "msg": "No rows to import"})

    now = datetime.utcnow().isoformat()
    created_ids = []
    custom_field_candidates = d.get("custom_field_candidates", [])

    # ── Caches so we don't query the DB on every row ──────────────────────────
    cat_cache  = {}   # name.lower() → category_id
    prod_cache = {}   # (name.lower(), cat_id) → product_id
    cf_done    = set()  # (cat_id, field_key) already ensured

    def _get_or_create_category(cat_name):
        key = cat_name.strip().lower()
        if key in cat_cache:
            return cat_cache[key]
        row = query("SELECT id FROM categories WHERE LOWER(name)=?", [key], one=True)
        if row:
            cat_cache[key] = row["id"]
            return row["id"]
        cid = execute("INSERT INTO categories (name, color) VALUES (?, ?)",
                      [cat_name.strip(), "#6366f1"])
        log_action("CATEGORY_CREATE", cid, cat_name.strip(),
                   "Auto-created during inventory import", None, None)
        cat_cache[key] = cid
        return cid

    def _ensure_custom_fields(cat_id):
        """Create any custom field candidates on this category if not already present."""
        if not custom_field_candidates or not cat_id:
            return
        for cf in custom_field_candidates:
            ck = (cat_id, cf["key"])
            if ck in cf_done:
                continue
            existing = query("SELECT id FROM category_fields WHERE category_id=? AND field_key=?",
                             [cat_id, cf["key"]], one=True)
            if not existing:
                sort_n = query("SELECT COUNT(*) as c FROM category_fields WHERE category_id=?",
                               [cat_id], one=True)["c"]
                execute("""INSERT INTO category_fields
                               (category_id, field_label, field_key, field_type, required, sort_order)
                               VALUES (?, ?, ?, 'text', 0, ?)""",
                        [cat_id, cf["label"], cf["key"], sort_n])
            cf_done.add(ck)

    def _get_or_create_product(name, cat_id, sku, has_serial, cost, sale, mfr, model):
        """Find an existing product by name+category, or create one."""
        key = (name.strip().lower(), cat_id)
        if key in prod_cache:
            return prod_cache[key]
        # Try to match by name (and category if set)
        if cat_id:
            row = query("SELECT id FROM products WHERE LOWER(name)=? AND category_id=? AND active=1",
                        [name.strip().lower(), cat_id], one=True)
        else:
            row = query("SELECT id FROM products WHERE LOWER(name)=? AND active=1",
                        [name.strip().lower()], one=True)
        if row:
            prod_cache[key] = row["id"]
            return row["id"]
        # Create product — infer tracking mode from the data
        serial_tracked      = 1 if has_serial else 0
        qty_tracked         = 0 if has_serial else 1
        require_serial      = 1 if has_serial else 0
        require_vendor_sku  = 1 if (sku and not has_serial) else 0
        require_internal_sku = 0 if (has_serial or sku) else 1
        pid = execute("""
            INSERT INTO products
                (name, category_id, manufacturer, model, vendor_sku,
                 serial_tracked, qty_tracked,
                 require_serial, require_vendor_sku, require_internal_sku,
                 default_cost, default_sale, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, [name.strip(), cat_id, mfr, model, sku,
              serial_tracked, qty_tracked,
              require_serial, require_vendor_sku, require_internal_sku,
              cost, sale, now])
        log_action("PRODUCT_CREATE", pid, name.strip(),
                   "Auto-created during inventory import", None, None)
        prod_cache[key] = pid
        return pid

    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue

        qty      = int(r["qty"]) if r.get("qty") not in (None, "") else None
        serial   = r.get("serial") or None
        sku      = r.get("sku") or None
        shelf    = r.get("shelf") or None
        cost     = float(r["cost_price"]) if r.get("cost_price") not in (None, "") else None
        sale     = float(r["sale_price"]) if r.get("sale_price") not in (None, "") else None
        mfr      = r.get("manufacturer") or None
        model    = r.get("model") or None
        notes    = r.get("notes") or notes_global
        pur_date = r.get("purchase_date") or None
        po_num   = r.get("po_number") or None

        # ── Step 1: resolve category ──────────────────────────────────────────
        cat_id = r.get("category_id") or None
        if not cat_id and r.get("category"):
            cat_id = _get_or_create_category(r["category"])
        if not cat_id:
            cat_id = _get_or_create_category("Uncategorized")

        # ── Step 2: ensure custom fields exist on this category ───────────────
        _ensure_custom_fields(cat_id)

        # ── Step 3: resolve product ───────────────────────────────────────────
        product_id = _get_or_create_product(
            name, cat_id, sku, bool(serial), cost, sale, mfr, model)

        # ── Step 4: per-row site ──────────────────────────────────────────────
        row_site_id = site_id
        row_site = r.get("site_value")
        if row_site and row_site.strip():
            resolved = _resolve_site(None, row_site.strip())
            if resolved:
                row_site_id = resolved

        # ── Step 5: extra fields JSON ─────────────────────────────────────────
        extra = {}
        if r.get("expiration_date"):
            extra["expiration_date"] = r["expiration_date"]
        if r.get("lot_number"):
            extra["lot_number"] = r["lot_number"]
        if r.get("flavor"):
            extra["flavor"] = r["flavor"]
        if r.get("pill_count"):
            extra["pill_count"] = r["pill_count"]
        extra.update(r.get("custom_fields") or {})

        # ── Step 6: create item ───────────────────────────────────────────────
        iid = execute("""
            INSERT INTO items
                (product_id, name, serial, sku, category_id, qty, cost_price, sale_price,
                 manufacturer, model, shelf, location_id, notes, purchase_date,
                 po_number, extra_fields, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, [product_id, name, serial, sku, cat_id, qty, cost, sale,
              mfr, model, shelf, row_site_id, notes, pur_date,
              po_num, json.dumps(extra), now])

        log_action("INVENTORY_IMPORT", iid, name,
                   "Inventory import: product_id={}, qty={}, serial={}, sku={}".format(
                       product_id, qty, serial, sku),
                   None, {"qty": qty, "product_id": product_id})
        created_ids.append(iid)

    if not created_ids:
        return jsonify({"ok": False, "msg": "No items were created"})

    execute("""INSERT INTO invoice_imports (reference, vendor, site_id, imported_by, imported_at, line_count, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [None, None, site_id, session.get("username"), now, len(created_ids), notes_global])


# ── Procurement ───────────────────────────────────────────────────────────────

def _po_number():
    """Generate next PO number: PO-YYYY-NNNN."""
    year = datetime.now().strftime("%Y")
    prefix = f"PO-{year}-"
    existing = query("SELECT po_number FROM purchase_orders WHERE po_number LIKE ?",
                     [f"{prefix}%"])
    nums = []
    for r in existing:
        try:
            nums.append(int(r["po_number"].split("-")[-1]))
        except Exception:
            pass
    seq = (max(nums) + 1) if nums else 1
    return f"{prefix}{seq:04d}"


def _recalc_po_total(po_id):
    """Recalculate and store purchase_orders.total_cost."""
    row = query("SELECT SUM(total_cost) as t FROM po_lines WHERE po_id=?", [po_id], one=True)
    total = row["t"] or 0
    execute("UPDATE purchase_orders SET total_cost=? WHERE id=?", [total, po_id])
    return total


@bp.route("/api/procurement/pos", methods=["GET"])
@login_required
@admin_required
def api_po_list():
    rows = query("""
        SELECT po.*, d.name as vendor_display, l.name as site_display
        FROM purchase_orders po
        LEFT JOIN distributors d ON d.id = po.vendor_id
        LEFT JOIN locations l ON l.id = po.site_id
        ORDER BY po.id DESC
    """)
    pos = [dict(r) for r in rows]
    # stat cards
    total_pos = len(pos)
    open_pos  = sum(1 for p in pos if p["status"] in ("draft","sent","partial"))
    total_spend = sum(p["total_cost"] or 0 for p in pos if p["status"] == "received")
    pending_spend = sum(p["total_cost"] or 0 for p in pos if p["status"] in ("sent","partial"))
    return jsonify({
        "ok": True,
        "pos": pos,
        "stats": {
            "total": total_pos,
            "open": open_pos,
            "total_spend": total_spend,
            "pending_spend": pending_spend,
        }
    })


@bp.route("/api/procurement/pos", methods=["POST"])
@login_required
@admin_required
def api_po_create():
    d = request.json or {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vendor_id     = d.get("vendor_id") or None
    vendor_name   = d.get("vendor_name") or None
    site_id       = d.get("site_id") or None
    expected_date = d.get("expected_date") or None
    notes         = d.get("notes") or None
    po_number     = _po_number()
    po_id = execute("""
        INSERT INTO purchase_orders
          (po_number, vendor_id, vendor_name, site_id, status, created_by, created_at,
           expected_date, notes, total_cost)
        VALUES (?,?,?,?,?,?,?,?,?,0)
    """, [po_number, vendor_id, vendor_name, site_id, "draft",
          session.get("username"), now, expected_date, notes])
    log_action("PO_CREATE", None, po_number,
               f"Purchase order created: {po_number}, vendor={vendor_name}")
    return jsonify({"ok": True, "id": po_id, "po_number": po_number})


@bp.route("/api/procurement/pos/<int:po_id>", methods=["GET"])
@login_required
@admin_required
def api_po_get(po_id):
    po = query("""
        SELECT po.*, d.name as vendor_display, d.email as _vendor_email,
               l.name as site_display
        FROM purchase_orders po
        LEFT JOIN distributors d ON d.id = po.vendor_id
        LEFT JOIN locations l ON l.id = po.site_id
        WHERE po.id=?
    """, [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    lines = [dict(r) for r in query(
        "SELECT * FROM po_lines WHERE po_id=? ORDER BY id", [po_id])]
    return jsonify({"ok": True, "po": dict(po), "lines": lines})


@bp.route("/api/procurement/pos/<int:po_id>", methods=["PATCH"])
@login_required
@admin_required
def api_po_update(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    d = request.json or {}
    allowed_statuses = ("draft", "sent", "partial", "received", "closed")
    updates = []
    vals    = []
    if "status" in d:
        if d["status"] not in allowed_statuses:
            return jsonify({"ok": False, "msg": "Invalid status"}), 400
        updates.append("status=?"); vals.append(d["status"])
        if d["status"] == "sent":
            updates.append("sent_at=?"); vals.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if "vendor_id" in d:
        updates.append("vendor_id=?"); vals.append(d["vendor_id"] or None)
    if "vendor_name" in d:
        updates.append("vendor_name=?"); vals.append(d["vendor_name"] or None)
    if "site_id" in d:
        updates.append("site_id=?"); vals.append(d["site_id"] or None)
    if "expected_date" in d:
        updates.append("expected_date=?"); vals.append(d["expected_date"] or None)
    if "notes" in d:
        updates.append("notes=?"); vals.append(d["notes"] or None)
    if not updates:
        return jsonify({"ok": False, "msg": "Nothing to update"}), 400
    vals.append(po_id)
    execute(f"UPDATE purchase_orders SET {', '.join(updates)} WHERE id=?", vals)
    log_action("PO_UPDATE", None, po["po_number"],
               f"PO updated: {', '.join(updates)}")
    return jsonify({"ok": True})


@bp.route("/api/procurement/pos/<int:po_id>", methods=["DELETE"])
@login_required
@admin_required
def api_po_delete(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    if po["status"] not in ("draft",):
        return jsonify({"ok": False, "msg": "Only draft POs can be deleted"}), 400
    execute("DELETE FROM po_lines WHERE po_id=?", [po_id])
    execute("DELETE FROM purchase_orders WHERE id=?", [po_id])
    log_action("PO_DELETE", None, po["po_number"], f"Draft PO deleted: {po['po_number']}")
    return jsonify({"ok": True})


@bp.route("/api/procurement/pos/<int:po_id>/lines", methods=["POST"])
@login_required
@admin_required
def api_po_line_add(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    if po["status"] not in ("draft", "sent"):
        return jsonify({"ok": False, "msg": "Cannot add lines to a received or closed PO"}), 400
    d = request.json or {}
    description = (d.get("description") or "").strip()
    if not description:
        return jsonify({"ok": False, "msg": "Description required"}), 400
    qty_ordered = int(d.get("qty_ordered") or 1)
    unit_cost   = float(d.get("unit_cost") or 0)
    total_cost  = round(qty_ordered * unit_cost, 4)
    product_id  = d.get("product_id") or None
    vendor_sku  = (d.get("vendor_sku") or "").strip() or None
    lid = execute("""
        INSERT INTO po_lines (po_id, product_id, description, vendor_sku,
                              qty_ordered, qty_received, unit_cost, total_cost)
        VALUES (?,?,?,?,?,0,?,?)
    """, [po_id, product_id, description, vendor_sku, qty_ordered, unit_cost, total_cost])
    new_total = _recalc_po_total(po_id)
    log_action("PO_LINE_ADD", None, po["po_number"],
               f"Line added: {description}, qty={qty_ordered}, unit=${unit_cost}")
    return jsonify({"ok": True, "id": lid, "total_cost": new_total})


@bp.route("/api/procurement/pos/<int:po_id>/lines/<int:lid>", methods=["PUT"])
@login_required
@admin_required
def api_po_line_update(po_id, lid):
    line = query("SELECT * FROM po_lines WHERE id=? AND po_id=?", [lid, po_id], one=True)
    if not line:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    d = request.json or {}
    description = (d.get("description") or "").strip() or line["description"]
    qty_ordered = int(d.get("qty_ordered") or line["qty_ordered"])
    unit_cost   = float(d.get("unit_cost") if "unit_cost" in d else line["unit_cost"])
    vendor_sku  = (d.get("vendor_sku") or "").strip() or None
    product_id  = d.get("product_id") if "product_id" in d else line["product_id"]
    total_cost  = round(qty_ordered * unit_cost, 4)
    execute("""UPDATE po_lines SET description=?, vendor_sku=?, product_id=?,
               qty_ordered=?, unit_cost=?, total_cost=? WHERE id=?""",
            [description, vendor_sku, product_id, qty_ordered, unit_cost, total_cost, lid])
    new_total = _recalc_po_total(po_id)
    return jsonify({"ok": True, "total_cost": new_total})


@bp.route("/api/procurement/pos/<int:po_id>/lines/<int:lid>", methods=["DELETE"])
@login_required
@admin_required
def api_po_line_delete(po_id, lid):
    line = query("SELECT * FROM po_lines WHERE id=? AND po_id=?", [lid, po_id], one=True)
    if not line:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    execute("DELETE FROM po_lines WHERE id=?", [lid])
    new_total = _recalc_po_total(po_id)
    return jsonify({"ok": True, "total_cost": new_total})


@bp.route("/api/procurement/pos/<int:po_id>/receive", methods=["POST"])
@login_required
@admin_required
def api_po_receive(po_id):
    """Record qty received per line, auto-create inventory items, set PO status."""
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    if po["status"] not in ("sent", "partial"):
        return jsonify({"ok": False, "msg": "PO must be Sent or Partial to receive goods"}), 400
    d = request.json or {}
    lines_input   = d.get("lines", [])
    site_id       = d.get("site_id") or po["site_id"] or None
    create_items  = d.get("create_items", True)  # default on
    if not lines_input:
        return jsonify({"ok": False, "msg": "No lines provided"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_items   = []   # {item_id, name, qty, needs_serial}
    needs_serial    = []   # lines that need serial entry

    for entry in lines_input:
        lid      = int(entry.get("id", 0))
        qty_recv = int(entry.get("qty_received", 0))
        if qty_recv <= 0:
            continue
        line = query("SELECT * FROM po_lines WHERE id=? AND po_id=?", [lid, po_id], one=True)
        if not line:
            continue
        new_total_recv = line["qty_received"] + qty_recv
        execute("UPDATE po_lines SET qty_received=? WHERE id=?", [new_total_recv, lid])

        # ── Auto-create inventory items ──────────────────────────────────────
        if not create_items or not line["product_id"]:
            continue
        prod = query("""SELECT id, name, category_id, serial_tracked, qty_tracked,
                               require_serial, manufacturer, model
                        FROM products WHERE id=? AND active=1""",
                     [line["product_id"]], one=True)
        if not prod:
            continue

        if prod["serial_tracked"] or prod["require_serial"]:
            # Can't auto-create without serial numbers — flag for user
            needs_serial.append({
                "line_id":     lid,
                "product_id":  prod["id"],
                "product_name": prod["name"],
                "qty":         qty_recv,
            })
        elif prod["qty_tracked"]:
            # qty-tracked: one item with qty = qty_recv
            item_id = execute("""
                INSERT INTO items (product_id, name, manufacturer, model, category_id,
                                   qty, cost_price, po_number, location_id,
                                   active, created_at, condition, notes,
                                   extra_fields, tags)
                VALUES (?,?,?,?,?,?,?,?,?,1,?,'New','','{}','')
            """, [prod["id"], prod["name"], prod.get("manufacturer"),
                  prod.get("model"), prod["category_id"],
                  qty_recv, line["unit_cost"], po["po_number"],
                  site_id, now])
            _ensure_internal_sku(item_id, prod["category_id"])
            from app.helpers import sync_item_task
            sync_item_task(item_id, prod["name"],
                           ["cost"] if not line["unit_cost"] else [])
            log_action("ITEM_ADD", item_id, prod["name"],
                       f"Auto-created from PO receipt: {po['po_number']}, qty={qty_recv}",
                       None, {"qty": qty_recv, "source": "po_receipt"})
            created_items.append({
                "item_id": item_id, "name": prod["name"],
                "qty": qty_recv, "needs_serial": False
            })
        else:
            # non-qty, non-serial (unique items): create one item per unit
            for _ in range(qty_recv):
                item_id = execute("""
                    INSERT INTO items (product_id, name, manufacturer, model, category_id,
                                       cost_price, po_number, location_id,
                                       active, created_at, condition, notes,
                                       extra_fields, tags)
                    VALUES (?,?,?,?,?,?,?,?,1,?,'New','','{}','')
                """, [prod["id"], prod["name"], prod.get("manufacturer"),
                      prod.get("model"), prod["category_id"],
                      line["unit_cost"], po["po_number"],
                      site_id, now])
                _ensure_internal_sku(item_id, prod["category_id"])
                log_action("ITEM_ADD", item_id, prod["name"],
                           f"Auto-created from PO receipt: {po['po_number']}",
                           None, {"source": "po_receipt"})
            created_items.append({
                "item_id": item_id, "name": prod["name"],
                "qty": qty_recv, "needs_serial": False
            })

    # ── Update PO status ─────────────────────────────────────────────────────
    all_lines = query("SELECT qty_ordered, qty_received FROM po_lines WHERE po_id=?", [po_id])
    fully_received = all(r["qty_received"] >= r["qty_ordered"] for r in all_lines)
    any_received   = any(r["qty_received"] > 0 for r in all_lines)
    new_status = "received" if fully_received else ("partial" if any_received else po["status"])
    execute("UPDATE purchase_orders SET status=? WHERE id=?", [new_status, po_id])
    log_action("PO_RECEIVE", None, po["po_number"],
               f"Goods received; status→{new_status}; {len(created_items)} items created")
    return jsonify({
        "ok": True,
        "new_status":    new_status,
        "created_items": created_items,
        "needs_serial":  needs_serial,
    })


# ── Vendor Catalog ────────────────────────────────────────────────────────────

def _parse_catalog_csv(file_storage):
    text = file_storage.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def _parse_catalog_xlsx(file_storage):
    import openpyxl
    wb = openpyxl.load_workbook(file_storage, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h or "").strip() for h in next(rows_iter, [])]
    if not any(headers):
        raise ValueError("Empty header row")
    result = []
    for row in rows_iter:
        if not any(c for c in row if c is not None):
            continue
        result.append({headers[i]: (str(row[i]) if row[i] is not None else "")
                       for i in range(len(headers))})
    return result


def _normalize_catalog_row(raw):
    aliases = {
        "product_name":    ["product_name","product name","name","description","item name","item description"],
        "vendor_sku":      ["vendor_sku","vendor sku","sku","part number","part#","item#","item number",
                            "mfr part","manufacturer part","catalog number","cat#"],
        "unit_price":      ["unit_price","unit price","price","cost","unit cost","list price"],
        "unit_of_measure": ["unit_of_measure","uom","unit","unit of measure"],
        "min_order_qty":   ["min_order_qty","moq","min qty","minimum qty","minimum order","min order qty"],
        "lead_days":       ["lead_days","lead time","lead time (days)","lead days"],
    }
    lc = {k.lower().strip(): v for k, v in raw.items()}
    out = {}
    for key, opts in aliases.items():
        for opt in opts:
            if opt in lc:
                out[key] = lc[opt]
                break
        if key not in out:
            out[key] = ""
    return out


@bp.route("/api/procurement/catalog/template")
@login_required
@admin_required
def api_vc_template():
    output = io.StringIO()
    output.write("product_name,vendor_sku,unit_price,unit_of_measure,min_order_qty,lead_days\n")
    output.write("Example Product,SKU-001,29.99,EA,1,5\n")
    output.seek(0)
    return send_file(
        io.BytesIO(output.read().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="vendor_catalog_template.csv"
    )


@bp.route("/api/procurement/catalog/upload", methods=["POST"])
@login_required
@admin_required
def api_vc_upload():
    vendor_id = request.form.get("vendor_id")
    if not vendor_id:
        return jsonify({"ok": False, "msg": "vendor_id required"}), 400
    vendor_id = int(vendor_id)
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "msg": "No file uploaded"}), 400
    fname = (f.filename or "").lower()
    try:
        if fname.endswith(".csv"):
            rows = _parse_catalog_csv(f)
        elif fname.endswith(".xlsx"):
            rows = _parse_catalog_xlsx(f)
        else:
            return jsonify({"ok": False, "msg": "Only CSV and XLSX files are supported"}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Could not parse file: {e}"}), 400
    if not rows:
        return jsonify({"ok": False, "msg": "File is empty or has no data rows"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    skipped  = 0
    replace_mode = request.form.get("replace", "0") == "1"
    if replace_mode:
        execute("DELETE FROM vendor_catalog WHERE vendor_id=?", [vendor_id])

    for raw in rows:
        row = _normalize_catalog_row(raw)
        name = row.get("product_name", "").strip()
        if not name:
            skipped += 1
            continue
        sku   = row.get("vendor_sku", "").strip() or None
        try:
            price = float(row["unit_price"]) if row.get("unit_price") else None
        except (ValueError, TypeError):
            price = None
        uom   = row.get("unit_of_measure", "").strip() or None
        try:
            moq   = int(float(row["min_order_qty"])) if row.get("min_order_qty") else 1
        except (ValueError, TypeError):
            moq = 1
        try:
            lead  = int(float(row["lead_days"])) if row.get("lead_days") else 0
        except (ValueError, TypeError):
            lead = 0
        if sku and not replace_mode:
            existing = query(
                "SELECT id FROM vendor_catalog WHERE vendor_id=? AND vendor_sku=?",
                [vendor_id, sku], one=True)
            if existing:
                execute("""UPDATE vendor_catalog SET product_name=?, unit_price=?,
                           unit_of_measure=?, min_order_qty=?, lead_days=?, updated_at=?
                           WHERE id=?""",
                        [name, price, uom, moq, lead, now, existing["id"]])
                inserted += 1
                continue
        execute("""INSERT INTO vendor_catalog
                   (vendor_id, product_name, vendor_sku, unit_price, unit_of_measure,
                    min_order_qty, lead_days, active, updated_at)
                   VALUES (?,?,?,?,?,?,?,1,?)""",
                [vendor_id, name, sku, price, uom, moq, lead, now])
        inserted += 1

    log_action("CATALOG_UPLOAD", None, None,
               f"Vendor catalog upload: vendor_id={vendor_id}, {inserted} rows, replace={replace_mode}")
    return jsonify({"ok": True, "inserted": inserted, "skipped": skipped})


@bp.route("/api/procurement/catalog", methods=["GET"])
@login_required
@admin_required
def api_vc_list():
    vendor_id = request.args.get("vendor_id")
    q = request.args.get("q", "").strip()
    sql = """
        SELECT vc.*, d.name as vendor_name,
               pv.product_id as linked_product_id, p.name as linked_product_name
        FROM vendor_catalog vc
        LEFT JOIN distributors d ON d.id = vc.vendor_id
        LEFT JOIN product_vendors pv ON pv.vendor_id = vc.vendor_id
            AND pv.vendor_sku = vc.vendor_sku AND pv.active=1
        LEFT JOIN products p ON p.id = pv.product_id
        WHERE vc.active=1
    """
    args = []
    if vendor_id:
        sql += " AND vc.vendor_id=?"; args.append(int(vendor_id))
    if q:
        sql += " AND (vc.product_name LIKE ? OR vc.vendor_sku LIKE ?)"; args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY vc.product_name LIMIT 500"
    rows = [dict(r) for r in query(sql, args)]
    count_sql = "SELECT COUNT(*) FROM vendor_catalog WHERE active=1"
    count_args = []
    if vendor_id:
        count_sql += " AND vendor_id=?"; count_args.append(int(vendor_id))
    total = query(count_sql, count_args, one=True)[0]
    return jsonify({"ok": True, "rows": rows, "total": total})


@bp.route("/api/procurement/catalog/<int:entry_id>/link", methods=["POST"])
@login_required
@admin_required
def api_vc_link(entry_id):
    entry = query("SELECT * FROM vendor_catalog WHERE id=? AND active=1", [entry_id], one=True)
    if not entry:
        return jsonify({"ok": False, "msg": "Entry not found"}), 404
    d = request.json or {}
    product_id = d.get("product_id")
    if not product_id:
        execute("DELETE FROM product_vendors WHERE vendor_id=? AND vendor_sku=?",
                [entry["vendor_id"], entry["vendor_sku"] or ""])
        return jsonify({"ok": True, "unlinked": True})
    existing = query("SELECT id FROM product_vendors WHERE product_id=? AND vendor_id=?",
                     [product_id, entry["vendor_id"]], one=True)
    if existing:
        execute("UPDATE product_vendors SET vendor_sku=?, unit_price=?, active=1 WHERE id=?",
                [entry["vendor_sku"], entry["unit_price"], existing["id"]])
    else:
        execute("""INSERT INTO product_vendors (product_id, vendor_id, vendor_sku, unit_price, preferred, active)
                   VALUES (?,?,?,?,0,1)""",
                [product_id, entry["vendor_id"], entry["vendor_sku"], entry["unit_price"]])
    log_action("CATALOG_LINK", product_id, entry["product_name"],
               f"Linked vendor catalog entry to product_id={product_id}")
    return jsonify({"ok": True})


@bp.route("/api/procurement/catalog/<int:entry_id>", methods=["DELETE"])
@login_required
@admin_required
def api_vc_delete(entry_id):
    entry = query("SELECT id FROM vendor_catalog WHERE id=?", [entry_id], one=True)
    if not entry:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    execute("UPDATE vendor_catalog SET active=0 WHERE id=?", [entry_id])
    return jsonify({"ok": True})


@bp.route("/api/procurement/catalog/vendor/<int:vendor_id>/search")
@login_required
@admin_required
def api_vc_vendor_search(vendor_id):
    """Quick search a vendor catalog — used by PO line item picker."""
    q = request.args.get("q", "").strip()
    args = [vendor_id]
    sql = """SELECT id, product_name, vendor_sku, unit_price, min_order_qty, lead_days
             FROM vendor_catalog WHERE vendor_id=? AND active=1"""
    if q:
        sql += " AND (product_name LIKE ? OR vendor_sku LIKE ?)"; args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY product_name LIMIT 50"
    rows = [dict(r) for r in query(sql, args)]
    return jsonify({"ok": True, "rows": rows})


@bp.route("/api/procurement/products/<int:product_id>/preferred-vendor", methods=["POST"])
@login_required
@admin_required
def api_vc_set_preferred_vendor(product_id):
    d = request.json or {}
    vendor_id = d.get("vendor_id")
    if not vendor_id:
        return jsonify({"ok": False, "msg": "vendor_id required"}), 400
    execute("UPDATE product_vendors SET preferred=0 WHERE product_id=?", [product_id])
    existing = query("SELECT id FROM product_vendors WHERE product_id=? AND vendor_id=?",
                     [product_id, vendor_id], one=True)
    if existing:
        execute("UPDATE product_vendors SET preferred=1, active=1 WHERE id=?", [existing["id"]])
    else:
        execute("""INSERT INTO product_vendors (product_id, vendor_id, vendor_sku, unit_price, preferred, active)
                   VALUES (?,?,NULL,NULL,1,1)""", [product_id, vendor_id])
    log_action("SET_PREFERRED_VENDOR", product_id, None,
               f"Preferred vendor set to vendor_id={vendor_id}")
    return jsonify({"ok": True})


# ── PO PDF generation + email sending ────────────────────────────────────────

def _generate_po_pdf(po, lines, tenant_name):
    """Generate a professional PDF for a purchase order. Returns bytes."""
    import io as _io
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)

    styles = getSampleStyleSheet()
    navy   = colors.HexColor("#0f172a")
    blue   = colors.HexColor("#1d4ed8")
    grey   = colors.HexColor("#64748b")
    lgrey  = colors.HexColor("#f8f7f4")
    border = colors.HexColor("#e5e3de")

    h1  = ParagraphStyle("h1",  fontSize=22, textColor=navy,  fontName="Helvetica-Bold",  spaceAfter=2)
    sub = ParagraphStyle("sub", fontSize=10, textColor=grey,  fontName="Helvetica",        spaceAfter=0)
    lbl = ParagraphStyle("lbl", fontSize=8,  textColor=grey,  fontName="Helvetica-Bold",   spaceAfter=2,
                         textTransform="uppercase", letterSpacing=0.5)
    val = ParagraphStyle("val", fontSize=11, textColor=navy,  fontName="Helvetica-Bold",   spaceAfter=0)
    sm  = ParagraphStyle("sm",  fontSize=9,  textColor=grey,  fontName="Helvetica")
    th  = ParagraphStyle("th",  fontSize=8,  textColor=grey,  fontName="Helvetica-Bold",   spaceAfter=0)
    td  = ParagraphStyle("td",  fontSize=10, textColor=navy,  fontName="Helvetica")
    tdr = ParagraphStyle("tdr", fontSize=10, textColor=navy,  fontName="Helvetica",        alignment=TA_RIGHT)
    tdc = ParagraphStyle("tdc", fontSize=10, textColor=navy,  fontName="Helvetica",        alignment=TA_CENTER)
    tot = ParagraphStyle("tot", fontSize=11, textColor=navy,  fontName="Helvetica-Bold",   alignment=TA_RIGHT)

    page_w = letter[0] - 1.5*inch

    def fmtm(n):
        return f"${float(n or 0):,.2f}"

    story = []

    # ── Header ──────────────────────────────────────────────────────────────────
    header_data = [[
        Paragraph(tenant_name or "CountDepot", h1),
        Paragraph("PURCHASE ORDER", ParagraphStyle("po", fontSize=14, textColor=blue,
                   fontName="Helvetica-Bold", alignment=TA_RIGHT)),
    ]]
    header_tbl = Table(header_data, colWidths=[page_w*0.6, page_w*0.4])
    header_tbl.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "MIDDLE")]))
    story.append(header_tbl)
    story.append(HRFlowable(width="100%", thickness=1, color=border, spaceAfter=14, spaceBefore=8))

    # ── PO meta ──────────────────────────────────────────────────────────────────
    status_colors = {
        "draft": "#475569", "sent": "#1d4ed8", "partial": "#92400e",
        "received": "#166534", "closed": "#64748b"
    }
    sc = status_colors.get(po.get("status",""), "#475569")
    meta_left = [
        [Paragraph("PO NUMBER", lbl), Paragraph("VENDOR", lbl)],
        [Paragraph(po["po_number"], ParagraphStyle("pn", fontSize=14, textColor=navy,
                   fontName="Helvetica-Bold")),
         Paragraph(po.get("vendor_name") or "—", val)],
        [Spacer(1,4), Spacer(1,4)],
        [Paragraph("STATUS", lbl), Paragraph("SITE / SHIP TO", lbl)],
        [Paragraph(po.get("status","").upper(),
                   ParagraphStyle("st", fontSize=10, textColor=colors.HexColor(sc),
                                  fontName="Helvetica-Bold")),
         Paragraph(po.get("site_display") or "—", val)],
    ]
    meta_right = [
        [Paragraph("DATE ISSUED", lbl), Paragraph("EXPECTED DELIVERY", lbl)],
        [Paragraph((po.get("created_at") or "")[:10], val),
         Paragraph(po.get("expected_date") or "—", val)],
        [Spacer(1,4), Spacer(1,4)],
        [Paragraph("CREATED BY", lbl), Paragraph("", lbl)],
        [Paragraph(po.get("created_by") or "—", val), Paragraph("", val)],
    ]
    col = page_w / 4
    meta_tbl = Table(
        [[Table(meta_left,  colWidths=[col, col]),
          Table(meta_right, colWidths=[col, col])]],
        colWidths=[page_w*0.5, page_w*0.5]
    )
    meta_tbl.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP")]))
    story.append(meta_tbl)
    story.append(Spacer(1, 16))

    if po.get("notes"):
        story.append(Paragraph("Notes", lbl))
        story.append(Paragraph(po["notes"],
                                ParagraphStyle("notes", fontSize=10, textColor=grey,
                                               fontName="Helvetica", spaceAfter=12)))
    story.append(HRFlowable(width="100%", thickness=1, color=border, spaceAfter=10, spaceBefore=4))

    # ── Line items table ─────────────────────────────────────────────────────────
    col_w = [page_w*0.35, page_w*0.18, page_w*0.10, page_w*0.10, page_w*0.13, page_w*0.14]
    tbl_data = [[
        Paragraph("Description", th), Paragraph("Vendor SKU", th),
        Paragraph("Qty", ParagraphStyle("thc", fontSize=8, textColor=grey,
                         fontName="Helvetica-Bold", alignment=TA_CENTER)),
        Paragraph("Rcv'd", ParagraphStyle("thc2", fontSize=8, textColor=grey,
                          fontName="Helvetica-Bold", alignment=TA_CENTER)),
        Paragraph("Unit Cost", ParagraphStyle("thr", fontSize=8, textColor=grey,
                          fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        Paragraph("Total", ParagraphStyle("thr2", fontSize=8, textColor=grey,
                         fontName="Helvetica-Bold", alignment=TA_RIGHT)),
    ]]
    for line in lines:
        tbl_data.append([
            Paragraph(str(line.get("description") or ""), td),
            Paragraph(str(line.get("vendor_sku") or "—"),
                      ParagraphStyle("sku", fontSize=9, textColor=grey, fontName="Helvetica")),
            Paragraph(str(line.get("qty_ordered") or 0), tdc),
            Paragraph(str(line.get("qty_received") or 0), tdc),
            Paragraph(fmtm(line.get("unit_cost")), tdr),
            Paragraph(fmtm(line.get("total_cost")), tdr),
        ])
    # Total row
    tbl_data.append([
        Paragraph("", td), Paragraph("", td), Paragraph("", td), Paragraph("", td),
        Paragraph("TOTAL", ParagraphStyle("totl", fontSize=9, textColor=grey,
                           fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        Paragraph(fmtm(po.get("total_cost")), tot),
    ])

    lines_tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)
    row_count = len(tbl_data)
    lines_tbl.setStyle(TableStyle([
        # Header
        ("BACKGROUND",  (0, 0), (-1, 0), lgrey),
        ("TOPPADDING",  (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING",(0, 0), (-1, 0), 8),
        ("LINEBELOW",   (0, 0), (-1, 0), 1, border),
        # Data rows
        ("TOPPADDING",  (0, 1), (-1, -2), 7),
        ("BOTTOMPADDING",(0, 1), (-1, -2), 7),
        ("LINEBELOW",   (0, 1), (-1, -2), 0.5, colors.HexColor("#f5f4f1")),
        # Total row
        ("TOPPADDING",  (0, -1), (-1, -1), 8),
        ("BOTTOMPADDING",(0, -1), (-1, -1), 8),
        ("LINEABOVE",   (0, -1), (-1, -1), 1.5, navy),
        ("BACKGROUND",  (0, -1), (-1, -1), lgrey),
        # Outer border
        ("BOX",         (0, 0), (-1, -1), 1, border),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(lines_tbl)

    # ── Footer ───────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=border, spaceAfter=8))
    story.append(Paragraph(
        f"This purchase order was generated by {tenant_name or 'CountDepot'}. "
        f"Please confirm receipt and expected delivery by replying to this email.",
        ParagraphStyle("footer", fontSize=8, textColor=grey, fontName="Helvetica")))

    doc.build(story)
    return buf.getvalue()


@bp.route("/api/procurement/pos/<int:po_id>/pdf")
@login_required
@admin_required
def api_po_pdf(po_id):
    """Generate and return the PO as a PDF download."""
    po = query("""
        SELECT po.*, d.name as vendor_display, l.name as site_display
        FROM purchase_orders po
        LEFT JOIN distributors d ON d.id = po.vendor_id
        LEFT JOIN locations l ON l.id = po.site_id
        WHERE po.id=?
    """, [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    lines = [dict(r) for r in query("SELECT * FROM po_lines WHERE po_id=? ORDER BY id", [po_id])]
    po_d  = dict(po)
    if not po_d.get("vendor_name"):
        po_d["vendor_name"] = po_d.get("vendor_display")

    from flask import g
    tenant_name = g.tenant.get("name", "") if hasattr(g, "tenant") and g.tenant else ""
    pdf_bytes = _generate_po_pdf(po_d, lines, tenant_name)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{po_d['po_number']}.pdf"
    )


@bp.route("/api/procurement/pos/<int:po_id>/send", methods=["POST"])
@login_required
@admin_required
def api_po_send(po_id):
    """Generate PDF and email it to the vendor."""
    po = query("""
        SELECT po.*, d.name as vendor_display, d.email as vendor_email,
               l.name as site_display
        FROM purchase_orders po
        LEFT JOIN distributors d ON d.id = po.vendor_id
        LEFT JOIN locations l ON l.id = po.site_id
        WHERE po.id=?
    """, [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404

    d = request.json or {}
    to_email    = (d.get("to_email") or po["vendor_email"] or "").strip()
    extra_notes = (d.get("notes") or po["notes"] or "").strip()

    if not to_email:
        return jsonify({"ok": False, "msg": "Vendor email required"}), 400

    po_d = dict(po)
    if not po_d.get("vendor_name"):
        po_d["vendor_name"] = po_d.get("vendor_display") or "Vendor"

    lines = [dict(r) for r in query("SELECT * FROM po_lines WHERE po_id=? ORDER BY id", [po_id])]

    from flask import g
    tenant_name = g.tenant.get("name", "") if hasattr(g, "tenant") and g.tenant else "CountDepot"

    try:
        pdf_bytes = _generate_po_pdf(po_d, lines, tenant_name)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"PDF generation failed: {e}"}), 500

    from app.mailer import send_po_email
    ok = send_po_email(
        to=to_email,
        po_number=po_d["po_number"],
        vendor_name=po_d["vendor_name"],
        sender_name=tenant_name,
        notes=extra_notes,
        pdf_bytes=pdf_bytes
    )

    # Mark PO as sent if it was draft
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if po_d["status"] == "draft":
        execute("UPDATE purchase_orders SET status='sent', sent_at=? WHERE id=?", [now, po_id])

    # Save email back to distributor if we have a vendor_id and no email on file
    if po_d.get("vendor_id") and not po_d.get("vendor_email"):
        execute("UPDATE distributors SET email=? WHERE id=?", [to_email, po_d["vendor_id"]])

    log_action("PO_SENT", None, po_d["po_number"],
               f"PO emailed to {to_email}; smtp_ok={ok}")

    return jsonify({"ok": True, "emailed": ok,
                    "new_status": "sent" if po_d["status"] == "draft" else po_d["status"]})


# ── 3-Way Match ───────────────────────────────────────────────────────────────

@bp.route("/api/procurement/pos/<int:po_id>/invoices", methods=["GET"])
@login_required
@admin_required
def api_po_invoices_list(po_id):
    po = query("SELECT id FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    invoices = [dict(r) for r in query(
        "SELECT * FROM po_invoices WHERE po_id=? ORDER BY created_at DESC", [po_id])]
    for inv in invoices:
        inv["lines"] = [dict(r) for r in query(
            "SELECT * FROM po_invoice_lines WHERE invoice_id=? ORDER BY id", [inv["id"]])]
    return jsonify({"ok": True, "invoices": invoices})


@bp.route("/api/procurement/pos/<int:po_id>/invoices", methods=["POST"])
@login_required
@admin_required
def api_po_invoice_create(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    d    = request.json or {}
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inv_ref  = (d.get("invoice_ref") or "").strip() or None
    inv_date = (d.get("invoice_date") or "").strip() or None
    vendor   = (d.get("vendor_name") or po["vendor_name"] or "").strip() or None
    notes    = (d.get("notes") or "").strip() or None
    inv_lines = d.get("lines", [])
    total = sum(float(l.get("qty_billed", 0)) * float(l.get("unit_price", 0)) for l in inv_lines)
    inv_id = execute("""
        INSERT INTO po_invoices
            (po_id, invoice_ref, invoice_date, vendor_name, total_amount, status, notes, created_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, [po_id, inv_ref, inv_date, vendor, round(total, 4), "pending", notes,
          session.get("username"), now])
    for l in inv_lines:
        qty      = float(l.get("qty_billed") or 0)
        price    = float(l.get("unit_price") or 0)
        line_tot = round(qty * price, 4)
        po_lid   = int(l["po_line_id"]) if l.get("po_line_id") else None
        execute("""
            INSERT INTO po_invoice_lines
                (invoice_id, po_line_id, description, vendor_sku, qty_billed, unit_price, line_total)
            VALUES (?,?,?,?,?,?,?)
        """, [inv_id, po_lid, (l.get("description") or "").strip(), l.get("vendor_sku") or None,
              qty, price, line_tot])
    log_action("PO_INVOICE_ADD", None, po["po_number"],
               f"Invoice recorded: ref={inv_ref}, total=${total:.2f}")
    return jsonify({"ok": True, "id": inv_id})


@bp.route("/api/procurement/pos/<int:po_id>/invoices/<int:inv_id>", methods=["PATCH"])
@login_required
@admin_required
def api_po_invoice_update(po_id, inv_id):
    inv = query("SELECT * FROM po_invoices WHERE id=? AND po_id=?", [inv_id, po_id], one=True)
    if not inv:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    d = request.json or {}
    updates, vals = [], []
    status = d.get("status")
    notes  = d.get("notes")
    if status and status in ("pending", "approved", "flagged", "rejected"):
        updates.append("status=?"); vals.append(status)
    if notes is not None:
        updates.append("notes=?"); vals.append(notes)
    if not updates:
        return jsonify({"ok": False, "msg": "Nothing to update"}), 400
    vals.append(inv_id)
    execute(f"UPDATE po_invoices SET {', '.join(updates)} WHERE id=?", vals)
    po = query("SELECT po_number FROM purchase_orders WHERE id=?", [po_id], one=True)
    log_action("PO_INVOICE_UPDATE", None, po["po_number"] if po else None,
               f"Invoice {inv['invoice_ref'] or inv_id} → {status}")
    return jsonify({"ok": True})


@bp.route("/api/procurement/pos/<int:po_id>/invoices/<int:inv_id>", methods=["DELETE"])
@login_required
@admin_required
def api_po_invoice_delete(po_id, inv_id):
    inv = query("SELECT * FROM po_invoices WHERE id=? AND po_id=?", [inv_id, po_id], one=True)
    if not inv:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    execute("DELETE FROM po_invoice_lines WHERE invoice_id=?", [inv_id])
    execute("DELETE FROM po_invoices WHERE id=?", [inv_id])
    po = query("SELECT po_number FROM purchase_orders WHERE id=?", [po_id], one=True)
    log_action("PO_INVOICE_DELETE", None, po["po_number"] if po else None,
               f"Invoice {inv['invoice_ref'] or inv_id} deleted")
    return jsonify({"ok": True})


@bp.route("/api/procurement/pos/<int:po_id>/match", methods=["GET"])
@login_required
@admin_required
def api_po_match(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    po_lines = [dict(r) for r in query(
        "SELECT * FROM po_lines WHERE po_id=? ORDER BY id", [po_id])]
    invoices = [dict(r) for r in query(
        "SELECT * FROM po_invoices WHERE po_id=? ORDER BY created_at DESC", [po_id])]
    for inv in invoices:
        inv["lines"] = [dict(r) for r in query(
            "SELECT * FROM po_invoice_lines WHERE invoice_id=? ORDER BY id", [inv["id"]])]

    PRICE_TOL = 0.01  # 1% tolerance before flagging
    match_rows  = []
    total_billed = 0.0

    for pl in po_lines:
        billed_qty   = 0.0
        price_issues = []
        for inv in invoices:
            for il in inv["lines"]:
                if il["po_line_id"] == pl["id"]:
                    billed_qty  += il["qty_billed"]
                    total_billed += il["line_total"]
                    if pl["unit_cost"] and pl["unit_cost"] > 0:
                        var_pct = abs(il["unit_price"] - pl["unit_cost"]) / pl["unit_cost"]
                        if var_pct > PRICE_TOL:
                            price_issues.append({
                                "invoice_ref":   inv["invoice_ref"],
                                "invoice_price": il["unit_price"],
                                "po_price":      pl["unit_cost"],
                                "variance_pct":  round(var_pct * 100, 2),
                            })
        issues = []
        if price_issues:
            issues.append(f"Price mismatch on {len(price_issues)} invoice(s)")
        if billed_qty > 0 and abs(billed_qty - pl["qty_received"]) > 0.001:
            issues.append(f"Qty billed ({billed_qty:g}) ≠ qty received ({pl['qty_received']})")
        match_rows.append({
            "po_line_id":   pl["id"],
            "description":  pl["description"],
            "vendor_sku":   pl["vendor_sku"],
            "qty_ordered":  pl["qty_ordered"],
            "qty_received": pl["qty_received"],
            "po_unit_cost": pl["unit_cost"],
            "qty_billed":   billed_qty,
            "status":       "flagged" if issues else ("ok" if billed_qty > 0 else "pending"),
            "issues":       issues,
            "price_issues": price_issues,
        })

    return jsonify({
        "ok":          True,
        "match_rows":  match_rows,
        "invoices":    invoices,
        "total_billed": round(total_billed, 4),
        "has_issues":  any(r["status"] == "flagged" for r in match_rows),
        "po_total":    po["total_cost"] or 0,
    })


# ── Spend Analytics ───────────────────────────────────────────────────────────

@bp.route("/api/procurement/analytics")
@login_required
@admin_required
def api_procurement_analytics():
    date_from, date_to = _parse_date_range(request)
    df_sql, df_args = _date_filter_sql("po.created_at", date_from, date_to)

    # All non-draft POs count as spend committed
    status_clause = "po.status IN ('sent','partial','received','closed')"
    base_where  = f"WHERE {status_clause}{df_sql}"
    base_args   = df_args[:]

    # ── KPIs ─────────────────────────────────────────────────────────────────
    kpi = query(
        f"SELECT COUNT(*) as po_count, SUM(po.total_cost) as total_spend, "
        f"AVG(po.total_cost) as avg_po_value "
        f"FROM purchase_orders po {base_where}", base_args, one=True)

    month_start = datetime.now().strftime("%Y-%m-01")
    this_month  = query(
        "SELECT SUM(total_cost) as t FROM purchase_orders "
        f"WHERE {status_clause.replace('po.','').replace('po.','')} AND created_at >= ?",
        [month_start], one=True)

    open_val = query(
        "SELECT SUM(total_cost) as t FROM purchase_orders "
        "WHERE status IN ('draft','sent','partial')", one=True)

    # ── By vendor ─────────────────────────────────────────────────────────────
    by_vendor_rows = query(
        f"SELECT COALESCE(po.vendor_name, d.name, 'Unknown') as vendor_name, "
        f"COUNT(*) as po_count, SUM(po.total_cost) as total_spend "
        f"FROM purchase_orders po LEFT JOIN distributors d ON d.id = po.vendor_id "
        f"{base_where} GROUP BY vendor_name ORDER BY total_spend DESC LIMIT 20",
        base_args)
    by_vendor = [dict(r) for r in by_vendor_rows]
    grand_vendor = sum(r["total_spend"] or 0 for r in by_vendor)
    for r in by_vendor:
        r["percent"] = round((r["total_spend"] or 0) / grand_vendor * 100, 1) if grand_vendor else 0

    # ── By month (last 12 months regardless of period filter) ─────────────────
    by_month = [dict(r) for r in query(
        "SELECT strftime('%Y-%m', created_at) as month, "
        "SUM(total_cost) as total_spend, COUNT(*) as po_count "
        f"FROM purchase_orders WHERE {status_clause.replace('po.', '')} "
        "AND created_at >= datetime('now', '-12 months') "
        "GROUP BY month ORDER BY month")]

    # ── Top line items ────────────────────────────────────────────────────────
    top_items = [dict(r) for r in query(
        "SELECT pl.description, COUNT(*) as line_count, "
        "SUM(pl.total_cost) as total_spend, "
        "SUM(pl.qty_ordered) as total_qty, "
        "AVG(pl.unit_cost) as avg_unit_cost "
        "FROM po_lines pl JOIN purchase_orders po ON po.id = pl.po_id "
        f"{base_where} GROUP BY LOWER(pl.description) "
        "ORDER BY total_spend DESC LIMIT 15", base_args)]

    # ── Monthly budget (from settings) ────────────────────────────────────────
    budget_row = query("SELECT value FROM settings WHERE key='monthly_po_budget'", one=True)
    monthly_budget = float(budget_row["value"]) if budget_row and budget_row["value"] else 0

    return jsonify({
        "ok":     True,
        "period": {"from": date_from, "to": date_to},
        "kpi":    {
            "po_count":      kpi["po_count"]    or 0,
            "total_spend":   kpi["total_spend"] or 0,
            "avg_po_value":  kpi["avg_po_value"]or 0,
            "this_month":    this_month["t"]    or 0,
            "open_value":    open_val["t"]      or 0,
        },
        "by_vendor":      by_vendor,
        "by_month":       by_month,
        "top_items":      top_items,
        "monthly_budget": monthly_budget,
    })


# ── PO Approval Workflow ──────────────────────────────────────────────────────

@bp.route("/api/procurement/settings", methods=["GET"])
@login_required
@admin_required
def api_procurement_settings_get():
    row = query("SELECT value FROM settings WHERE key='po_approval_threshold'", one=True)
    threshold = float(row["value"]) if row and row["value"] else 0
    return jsonify({"ok": True, "po_approval_threshold": threshold})


@bp.route("/api/procurement/settings", methods=["POST"])
@login_required
@admin_required
def api_procurement_settings_save():
    d = request.json or {}
    threshold = d.get("po_approval_threshold")
    if threshold is not None:
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "Invalid threshold"}), 400
        execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ["po_approval_threshold", str(threshold)])
    return jsonify({"ok": True})


@bp.route("/api/procurement/pos/<int:po_id>/submit", methods=["POST"])
@login_required
@admin_required
def api_po_submit_approval(po_id):
    """Submit a draft PO for approval. Auto-approves if below threshold."""
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    if po["status"] != "draft":
        return jsonify({"ok": False, "msg": "Only draft POs can be submitted"}), 400

    row       = query("SELECT value FROM settings WHERE key='po_approval_threshold'", one=True)
    threshold = float(row["value"]) if row and row["value"] else 0
    total     = po["total_cost"] or 0
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if threshold > 0 and total >= threshold:
        # Needs approval
        execute("UPDATE purchase_orders SET status='pending_approval', approval_required=1 WHERE id=?",
                [po_id])
        log_action("PO_SUBMITTED", None, po["po_number"],
                   f"Submitted for approval; total=${total:.2f}; threshold=${threshold:.2f}")
        # Email all admins
        _notify_approvers(po, total, threshold)
        try:
            from app.messenger import notify as _notify
            _notify("po_submitted",
                    f"PO Approval Required: {po['po_number']}",
                    f"Total ${total:.2f} — submitted by {session.get('username','?')}",
                    f"/procurement/{po_id}")
        except Exception:
            pass
        return jsonify({"ok": True, "new_status": "pending_approval", "requires_approval": True})
    else:
        # Auto-approve: no threshold or below threshold — just send
        return jsonify({"ok": True, "new_status": "draft", "requires_approval": False,
                        "msg": "No approval required — use Send to deliver the PO."})


@bp.route("/api/procurement/pos/<int:po_id>/approve", methods=["POST"])
@login_required
@admin_required
def api_po_approve(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    if po["status"] != "pending_approval":
        return jsonify({"ok": False, "msg": "PO is not pending approval"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE purchase_orders SET status='draft', approved_by=?, approved_at=?, "
            "approval_required=0 WHERE id=?",
            [session.get("username"), now, po_id])
    log_action("PO_APPROVED", None, po["po_number"],
               f"Approved by {session.get('username')}")
    try:
        from app.messenger import notify as _notify
        _notify("po_approved",
                f"PO Approved: {po['po_number']}",
                f"Approved by {session.get('username','?')}",
                f"/procurement/{po_id}")
    except Exception:
        pass
    return jsonify({"ok": True, "new_status": "draft"})


@bp.route("/api/procurement/pos/<int:po_id>/reject", methods=["POST"])
@login_required
@admin_required
def api_po_reject(po_id):
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        return jsonify({"ok": False, "msg": "Not found"}), 404
    if po["status"] != "pending_approval":
        return jsonify({"ok": False, "msg": "PO is not pending approval"}), 400
    d      = request.json or {}
    reason = (d.get("reason") or "").strip() or None
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE purchase_orders SET status='draft', rejected_by=?, rejected_at=?, "
            "rejection_reason=?, approval_required=0 WHERE id=?",
            [session.get("username"), now, reason, po_id])
    log_action("PO_REJECTED", None, po["po_number"],
               f"Rejected by {session.get('username')}; reason={reason}")
    return jsonify({"ok": True, "new_status": "draft"})


def _notify_approvers(po, total, threshold):
    """Email all admin users to notify them a PO needs their approval."""
    try:
        from app.mailer import send_email
        admins = query("SELECT email, username FROM users WHERE role='admin' AND email IS NOT NULL AND email != ''")
        for admin in admins:
            subject = f"PO Approval Needed: {po['po_number']} (${total:,.2f})"
            body = (
                f"A purchase order requires your approval before it can be sent.\n\n"
                f"PO Number: {po['po_number']}\n"
                f"Vendor:    {po['vendor_name'] or 'Unknown'}\n"
                f"Total:     ${total:,.2f}\n"
                f"Threshold: ${threshold:,.2f}\n\n"
                f"Log in to CountDepot to review and approve or reject this PO."
            )
            send_email(admin["email"], subject, body)
    except Exception:
        pass  # email failure must not block the API response


@bp.route("/api/procurement/analytics/budget", methods=["POST"])
@login_required
@admin_required
def api_procurement_set_budget():
    d = request.json or {}
    val = d.get("monthly_budget")
    if val is None:
        return jsonify({"ok": False, "msg": "monthly_budget required"}), 400
    try:
        val = float(val)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "Invalid value"}), 400
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ["monthly_po_budget", str(val)])
    return jsonify({"ok": True})


# ── Accounting Integrations (QuickBooks & Xero) ───────────────────────────────

@bp.route("/api/integrations/accounting/status")
@login_required
@admin_required
def api_accounting_status():
    from app.accounting import qb_get_credentials, xero_get_credentials
    return jsonify({
        "ok":        True,
        "quickbooks": qb_get_credentials(),
        "xero":       xero_get_credentials(),
    })


@bp.route("/api/integrations/accounting/qb/save", methods=["POST"])
@login_required
@admin_required
def api_qb_save_credentials():
    from app.accounting import _set_setting
    d = request.json or {}
    for key in ("qb_client_id", "qb_client_secret", "qb_sandbox"):
        if key in d:
            _set_setting(key, str(d[key]))
    return jsonify({"ok": True})


@bp.route("/api/integrations/accounting/qb/auth-url")
@login_required
@admin_required
def api_qb_auth_url():
    from app.accounting import qb_auth_url, _get_setting
    from flask import request as req
    client_id    = _get_setting("qb_client_id", "")
    if not client_id:
        return jsonify({"ok": False, "msg": "QuickBooks client ID not configured"}), 400
    redirect_uri = req.host_url.rstrip("/") + "/integrations/accounting/qb/callback"
    url          = qb_auth_url(client_id, redirect_uri)
    return jsonify({"ok": True, "url": url})


@bp.route("/api/integrations/accounting/qb/disconnect", methods=["POST"])
@login_required
@admin_required
def api_qb_disconnect():
    from app.accounting import qb_disconnect
    qb_disconnect()
    return jsonify({"ok": True})


@bp.route("/api/integrations/accounting/xero/save", methods=["POST"])
@login_required
@admin_required
def api_xero_save_credentials():
    from app.accounting import _set_setting
    d = request.json or {}
    for key in ("xero_client_id", "xero_client_secret"):
        if key in d:
            _set_setting(key, str(d[key]))
    return jsonify({"ok": True})


@bp.route("/api/integrations/accounting/xero/auth-url")
@login_required
@admin_required
def api_xero_auth_url():
    from app.accounting import xero_auth_url, _get_setting
    from flask import request as req
    client_id    = _get_setting("xero_client_id", "")
    if not client_id:
        return jsonify({"ok": False, "msg": "Xero client ID not configured"}), 400
    redirect_uri = req.host_url.rstrip("/") + "/integrations/accounting/xero/callback"
    url          = xero_auth_url(client_id, redirect_uri)
    return jsonify({"ok": True, "url": url})


@bp.route("/api/integrations/accounting/xero/disconnect", methods=["POST"])
@login_required
@admin_required
def api_xero_disconnect():
    from app.accounting import xero_disconnect
    xero_disconnect()
    return jsonify({"ok": True})


@bp.route("/api/integrations/accounting/sync-po/<int:po_id>", methods=["POST"])
@login_required
@admin_required
def api_accounting_sync_po(po_id):
    d        = request.json or {}
    provider = d.get("provider", "").lower()
    if provider not in ("quickbooks", "xero"):
        return jsonify({"ok": False, "msg": "provider must be quickbooks or xero"}), 400
    try:
        if provider == "quickbooks":
            from app.accounting import qb_sync_po
            remote_id = qb_sync_po(po_id)
        else:
            from app.accounting import xero_sync_po
            remote_id = xero_sync_po(po_id)
        return jsonify({"ok": True, "remote_id": remote_id})
    except Exception as e:
        now = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        execute(
            "INSERT INTO accounting_sync_log (provider,entity_type,entity_id,status,detail,synced_at) "
            "VALUES (?,?,?,?,?,?)",
            [provider, "purchase_order", po_id, "error", str(e)[:500], now])
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.route("/api/integrations/accounting/sync-log")
@login_required
@admin_required
def api_accounting_sync_log():
    rows = query("SELECT * FROM accounting_sync_log ORDER BY id DESC LIMIT 100")
    return jsonify({"ok": True, "log": [dict(r) for r in rows]})


# ── Slack / Teams Notifications ───────────────────────────────────────────────

@bp.route("/api/integrations/messenger/test", methods=["POST"])
@login_required
@admin_required
def api_messenger_test():
    d        = request.json or {}
    provider = d.get("provider", "")
    try:
        from app.messenger import send_slack, send_teams, _get_setting
        if provider == "slack":
            url = _get_setting("slack_webhook_url", "")
            if not url:
                return jsonify({"ok": False, "msg": "Slack webhook URL not configured"}), 400
            send_slack(url, "default", "CountDepot Test Notification",
                       "This is a test message from CountDepot.", None)
        elif provider == "teams":
            url = _get_setting("teams_webhook_url", "")
            if not url:
                return jsonify({"ok": False, "msg": "Teams webhook URL not configured"}), 400
            send_teams(url, "default", "CountDepot Test Notification",
                       "This is a test message from CountDepot.", None)
        else:
            return jsonify({"ok": False, "msg": "Unknown provider"}), 400
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ── Amazon Business Integration ───────────────────────────────────────────────

_AMZ_SETTING_KEYS = [
    "amz_lwa_client_id", "amz_lwa_client_secret", "amz_lwa_refresh_token",
    "amz_aws_access_key", "amz_aws_secret_key",
    "amz_marketplace_id", "amz_seller_id", "amz_cxml_identity",
    "amz_cxml_secret", "amz_cxml_from_domain", "amz_buyer_id",
]


def _amz_creds():
    """Load Amazon credentials from settings. Returns dict."""
    rows = query("SELECT key, value FROM settings WHERE key LIKE 'amz_%'")
    return {r["key"]: r["value"] for r in rows}


def _amz_save(d):
    """Save Amazon credential key/values from a dict to settings."""
    for k in _AMZ_SETTING_KEYS:
        if k in d:
            val = (d[k] or "").strip()
            execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [k, val])


@bp.route("/api/integrations/amazon", methods=["GET"])
@login_required
@admin_required
def api_amz_get():
    creds = _amz_creds()
    # Return redacted versions (show whether they are set, not the values)
    safe = {}
    for k in _AMZ_SETTING_KEYS:
        v = creds.get(k, "")
        if k in ("amz_lwa_client_secret", "amz_aws_secret_key", "amz_cxml_secret",
                 "amz_lwa_refresh_token"):
            safe[k] = "***" if v else ""
        else:
            safe[k] = v
    configured = bool(creds.get("amz_lwa_client_id") and creds.get("amz_lwa_refresh_token"))
    return jsonify({"ok": True, "credentials": safe, "configured": configured})


@bp.route("/api/integrations/amazon", methods=["POST"])
@login_required
@admin_required
def api_amz_save():
    d = request.json or {}
    _amz_save(d)
    return jsonify({"ok": True})


@bp.route("/api/integrations/amazon/test", methods=["POST"])
@login_required
@admin_required
def api_amz_test():
    """Test Amazon credentials by fetching an access token."""
    creds = _amz_creds()
    client_id     = creds.get("amz_lwa_client_id", "")
    client_secret = creds.get("amz_lwa_client_secret", "")
    refresh_token = creds.get("amz_lwa_refresh_token", "")
    if not (client_id and client_secret and refresh_token):
        return jsonify({"ok": False, "msg": "Amazon credentials are not fully configured."})
    try:
        from app.amazon import get_access_token
        result = get_access_token(client_id, client_secret, refresh_token)
        if result.get("access_token"):
            return jsonify({"ok": True, "msg": "Connection successful. Access token received."})
        return jsonify({"ok": False, "msg": f"Unexpected response: {result}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@bp.route("/api/integrations/amazon/sync-orders", methods=["POST"])
@login_required
@admin_required
def api_amz_sync_orders():
    """
    Sync Amazon Business order history.
    Creates invoice_import records + items from any orders not already imported.
    """
    creds = _amz_creds()
    client_id     = creds.get("amz_lwa_client_id", "")
    client_secret = creds.get("amz_lwa_client_secret", "")
    refresh_token = creds.get("amz_lwa_refresh_token", "")
    marketplace   = creds.get("amz_marketplace_id", "ATVPDKIKX0DER")
    aws_key       = creds.get("amz_aws_access_key") or None
    aws_secret    = creds.get("amz_aws_secret_key") or None
    if not (client_id and client_secret and refresh_token):
        return jsonify({"ok": False, "msg": "Amazon credentials not configured."})
    d          = request.json or {}
    days_back  = int(d.get("days_back", 90))
    try:
        from app.amazon import sync_orders, get_order_items
        orders = sync_orders(marketplace, client_id, client_secret, refresh_token,
                             aws_key, aws_secret, days_back)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Amazon API error: {e}"})

    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    imported = []
    skipped  = []

    for order in orders:
        oid = order["amazon_order_id"]
        # Check if already imported
        existing = query("SELECT id FROM invoice_imports WHERE reference=?", [oid], one=True)
        if existing:
            skipped.append(oid)
            continue
        try:
            items = get_order_items(oid, client_id, client_secret, refresh_token,
                                    aws_key, aws_secret)
        except Exception:
            items = []

        # Create one invoice_import per order
        iid = execute(
            "INSERT INTO invoice_imports (reference, vendor, imported_by, imported_at, line_count, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [oid, "Amazon Business", session.get("username"), now,
             len(items), f"Amazon order {oid} — {order['status']}"]
        )

        # Create items for each line
        for li in items:
            if not li.get("qty_ordered"):
                continue
            prod_name = (li.get("title") or li.get("seller_sku") or "Amazon Item")[:200]
            qty       = li["qty_ordered"]
            price     = li.get("unit_price")
            vendor_sku= li.get("seller_sku") or li.get("asin") or None

            # Find or create product
            existing_prod = query(
                "SELECT id FROM products WHERE LOWER(name)=LOWER(?) AND active=1 LIMIT 1",
                [prod_name], one=True)
            if existing_prod:
                product_id = existing_prod["id"]
                cat_id     = query("SELECT category_id FROM products WHERE id=?",
                                   [product_id], one=True)["category_id"]
            else:
                # Find a generic category
                cat = query("SELECT id FROM categories WHERE LOWER(name) IN "
                            "('accessories','consumables','general','other') LIMIT 1", one=True)
                cat_id = cat["id"] if cat else None
                if not cat_id:
                    # Use first category
                    first = query("SELECT id FROM categories LIMIT 1", one=True)
                    cat_id = first["id"] if first else None
                product_id = execute(
                    "INSERT INTO products (name, category_id, qty_tracked, serial_tracked, "
                    "require_vendor_sku, require_internal_sku, vendor_sku, active, created_at) "
                    "VALUES (?, ?, 1, 0, 1, 0, ?, 1, ?)",
                    [prod_name, cat_id, vendor_sku, now]
                )

            item_id = execute(
                "INSERT INTO items (product_id, name, sku, category_id, qty, cost_price, "
                "purchased_from, po_number, extra_fields, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                [product_id, prod_name, vendor_sku, cat_id, qty, price,
                 "Amazon Business", oid, json.dumps({"amazon_order_id": oid}), now]
            )
            log_action("AMAZON_IMPORT", item_id, prod_name,
                       f"Amazon order {oid}: qty={qty}, price={price}")

        imported.append(oid)

    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ["amz_last_sync", now])
    return jsonify({
        "ok":      True,
        "imported": len(imported),
        "skipped":  len(skipped),
        "orders":   imported,
    })


@bp.route("/api/integrations/amazon/price-check", methods=["POST"])
@login_required
@admin_required
def api_amz_price_check():
    """Look up Amazon Business prices for a list of queries."""
    creds = _amz_creds()
    client_id     = creds.get("amz_lwa_client_id", "")
    client_secret = creds.get("amz_lwa_client_secret", "")
    refresh_token = creds.get("amz_lwa_refresh_token", "")
    marketplace   = creds.get("amz_marketplace_id", "ATVPDKIKX0DER")
    aws_key       = creds.get("amz_aws_access_key") or None
    aws_secret    = creds.get("amz_aws_secret_key") or None
    if not (client_id and client_secret and refresh_token):
        return jsonify({"ok": False, "msg": "Amazon credentials not configured."})
    d = request.json or {}
    queries = d.get("queries", [])
    if not queries:
        return jsonify({"ok": False, "msg": "No queries provided."}), 400
    try:
        from app.amazon import lookup_prices
        results = lookup_prices(queries, marketplace, client_id, client_secret,
                                refresh_token, aws_key, aws_secret)
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@bp.route("/api/integrations/amazon/punchout/initiate", methods=["POST"])
@login_required
@admin_required
def api_amz_punchout_initiate():
    """Initiate an Amazon Business cXML punch-out session."""
    creds       = _amz_creds()
    identity    = creds.get("amz_cxml_identity", "")
    secret      = creds.get("amz_cxml_secret", "")
    from_domain = creds.get("amz_cxml_from_domain", "countdepot.com")
    buyer_id    = creds.get("amz_buyer_id", "")
    if not (identity and secret):
        return jsonify({"ok": False, "msg": "cXML credentials not configured."})
    d        = request.json or {}
    order_id = d.get("po_id") or f"CD-{int(time.time())}"
    # Return URL — browser will POST the cart XML here
    from flask import g, request as _req
    base_url    = _req.host_url.rstrip("/")
    return_url  = f"{base_url}/api/integrations/amazon/punchout/return"
    try:
        from app.amazon import send_punchout_setup
        result = send_punchout_setup(return_url, identity, secret, from_domain,
                                     buyer_id, str(order_id))
        if result["ok"]:
            # Store the buyer_cookie → po_id mapping so we can match on return
            execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    [f"amz_punchout_{result['buyer_cookie']}", str(order_id)])
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@bp.route("/api/integrations/amazon/punchout/return", methods=["POST"])
def api_amz_punchout_return():
    """
    Receive the cXML PunchOutOrderMessage when user checks out on Amazon Business.
    Creates a draft PO from the cart items.
    """
    xml_body = request.get_data(as_text=True)
    try:
        from app.amazon import parse_punchout_order_message
        cart = parse_punchout_order_message(xml_body)
    except Exception as e:
        return f"<cXML><Response><Status code='400' text='Bad Request'>{e}</Status></Response></cXML>", 400, {
            "Content-Type": "text/xml"
        }

    buyer_cookie = cart.get("buyer_cookie", "")
    po_id_row    = query("SELECT value FROM settings WHERE key=?",
                         [f"amz_punchout_{buyer_cookie}"], one=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Create a draft PO from the cart
    po_number = _po_number()  # use api.py's own _po_number helper

    po_id = execute(
        "INSERT INTO purchase_orders (po_number, vendor_name, status, created_by, created_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [po_number, "Amazon Business", "draft", "amazon_punchout", now,
         f"Created via Amazon Business punch-out (cookie: {buyer_cookie[:20]})"]
    )
    total = 0.0
    for item in cart.get("items", []):
        qty  = item.get("qty", 1)
        price = item.get("unit_price", 0)
        lt   = round(qty * price, 4)
        total += lt
        execute(
            "INSERT INTO po_lines (po_id, description, vendor_sku, qty_ordered, unit_cost, total_cost) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [po_id, item.get("description", "Amazon Item"),
             item.get("vendor_sku"), qty, price, lt]
        )
    execute("UPDATE purchase_orders SET total_cost=? WHERE id=?", [total, po_id])
    log_action("PO_CREATE", None, po_number,
               f"Draft PO created from Amazon Business punch-out; {len(cart.get('items',[]))} items")

    # Clean up buyer_cookie mapping
    execute("DELETE FROM settings WHERE key=?", [f"amz_punchout_{buyer_cookie}"])

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cXML><Response><Status code="200" text="OK">PO {po_number} created</Status></Response></cXML>""", 200, {
        "Content-Type": "text/xml"
    }


# ── Distributor cXML Punch-out (Grainger, TD SYNNEX, Ingram Micro, Henry Schein) ──

# Registry of supported distributors and their cXML endpoint templates.
# Credentials are stored in settings as: punchout_{key}_identity, punchout_{key}_secret,
# punchout_{key}_from_domain, punchout_{key}_url
CXML_DISTRIBUTORS = {
    "grainger": {
        "name":     "Grainger",
        "url":      "https://www.grainger.com/punchout/cxml",
        "logo":     "https://www.grainger.com/favicon.ico",
        "note":     "Contact Grainger eProcurement to obtain your cXML credentials.",
    },
    "tdsysnex": {
        "name":     "TD SYNNEX",
        "url":      "https://www.tdsynnex.com/punchout/cxml",
        "logo":     "",
        "note":     "Contact TD SYNNEX to obtain your cXML buyer identity and shared secret.",
    },
    "ingram": {
        "name":     "Ingram Micro",
        "url":      "https://ec.ingrammicro.com/Punchout/cxml",
        "logo":     "",
        "note":     "Contact Ingram Micro eProcurement to get cXML access.",
    },
    "henryschein": {
        "name":     "Henry Schein",
        "url":      "https://www.henryschein.com/punchout/cxml",
        "logo":     "",
        "note":     "Contact Henry Schein eProcurement to get your cXML account.",
    },
}


@bp.route("/api/integrations/punchout/distributors")
@login_required
@admin_required
def api_punchout_distributors():
    """Return the list of supported distributors and their configuration status."""
    result = []
    for key, info in CXML_DISTRIBUTORS.items():
        identity = query("SELECT value FROM settings WHERE key=?",
                         [f"punchout_{key}_identity"], one=True)
        result.append({
            "key":       key,
            "name":      info["name"],
            "note":      info["note"],
            "configured": bool(identity and identity["value"]),
        })
    return jsonify({"ok": True, "distributors": result})


@bp.route("/api/integrations/punchout/<dist_key>/save", methods=["POST"])
@login_required
@admin_required
def api_punchout_save(dist_key):
    if dist_key not in CXML_DISTRIBUTORS:
        return jsonify({"ok": False, "msg": "Unknown distributor"}), 404
    d = request.json or {}
    for field in ("identity", "secret", "from_domain", "url"):
        val = d.get(field, "").strip()
        if val:
            execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                    [f"punchout_{dist_key}_{field}", val])
    return jsonify({"ok": True})


@bp.route("/api/integrations/punchout/<dist_key>/initiate", methods=["POST"])
@login_required
@admin_required
def api_punchout_initiate(dist_key):
    if dist_key not in CXML_DISTRIBUTORS:
        return jsonify({"ok": False, "msg": "Unknown distributor"}), 404
    dist_info = CXML_DISTRIBUTORS[dist_key]

    def _gs(key):
        row = query("SELECT value FROM settings WHERE key=?", [key], one=True)
        return row["value"] if row else ""

    identity    = _gs(f"punchout_{dist_key}_identity")
    secret      = _gs(f"punchout_{dist_key}_secret")
    from_domain = _gs(f"punchout_{dist_key}_from_domain") or "countdepot.com"
    url_override = _gs(f"punchout_{dist_key}_url") or dist_info["url"]

    if not (identity and secret):
        return jsonify({"ok": False,
                        "msg": f"{dist_info['name']} credentials not configured."})

    d        = request.json or {}
    order_id = d.get("po_id") or f"CD-{int(time.time())}"
    base_url = request.host_url.rstrip("/")
    return_url = f"{base_url}/api/integrations/punchout/{dist_key}/return"

    try:
        from app.amazon import send_punchout_setup
        result = send_punchout_setup(return_url, identity, secret, from_domain,
                                     "", str(order_id), endpoint_url=url_override)
        if result.get("ok"):
            execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                    [f"punchout_{dist_key}_{result['buyer_cookie']}", str(order_id)])
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@bp.route("/api/integrations/punchout/<dist_key>/return", methods=["POST"])
def api_punchout_return(dist_key):
    """Generic cXML PunchOutOrderMessage return handler for any distributor."""
    if dist_key not in CXML_DISTRIBUTORS:
        return "<cXML><Response><Status code='404' text='Not Found'/></Response></cXML>", 404, {"Content-Type": "text/xml"}
    dist_info = CXML_DISTRIBUTORS[dist_key]
    xml_body  = request.get_data(as_text=True)
    try:
        from app.amazon import parse_punchout_order_message
        cart = parse_punchout_order_message(xml_body)
    except Exception as e:
        return f"<cXML><Response><Status code='400' text='Bad Request'>{e}</Status></Response></cXML>", 400, {"Content-Type": "text/xml"}

    buyer_cookie = cart.get("buyer_cookie", "")
    now          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    po_number    = _po_number()
    po_id        = execute(
        "INSERT INTO purchase_orders (po_number, vendor_name, status, created_by, created_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [po_number, dist_info["name"], "draft", f"{dist_key}_punchout", now,
         f"Created via {dist_info['name']} punch-out"])
    total = 0.0
    for item in cart.get("items", []):
        qty   = item.get("qty", 1)
        price = item.get("unit_price", 0)
        lt    = round(qty * price, 4)
        total += lt
        execute(
            "INSERT INTO po_lines (po_id, description, vendor_sku, qty_ordered, unit_cost, total_cost) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [po_id, item.get("description", f"{dist_info['name']} Item"),
             item.get("vendor_sku"), qty, price, lt])
    execute("UPDATE purchase_orders SET total_cost=? WHERE id=?", [total, po_id])
    log_action("PO_CREATE", None, po_number,
               f"Draft PO via {dist_info['name']} punch-out; {len(cart.get('items', []))} items")
    execute("DELETE FROM settings WHERE key=?", [f"punchout_{dist_key}_{buyer_cookie}"])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cXML><Response><Status code="200" text="OK">PO {po_number} created</Status></Response></cXML>""", 200, {
        "Content-Type": "text/xml"
    }


# ── SSO / SAML Admin ──────────────────────────────────────────────────────────

@bp.route("/api/auth/providers")
def api_auth_providers():
    """Public — which login methods are active (used by login page to show buttons)."""
    from config import Config as _Cfg
    # Google: enabled when GOOGLE_CLIENT_ID is set and authlib is installed
    google_on = bool(_Cfg.GOOGLE_CLIENT_ID)
    if google_on:
        try:
            from authlib.integrations.flask_client import OAuth as _O  # noqa
        except ImportError:
            google_on = False
    # SAML SSO: enabled when tenant has configured it
    try:
        saml_row = query("SELECT enabled, idp_entity_id, idp_sso_url, idp_cert FROM sso_config LIMIT 1", one=True)
        saml_on  = bool(saml_row and saml_row["enabled"] and saml_row["idp_entity_id"]
                        and saml_row["idp_sso_url"] and saml_row["idp_cert"])
    except Exception:
        saml_on = False
    return jsonify({"ok": True, "google": google_on, "saml": saml_on})


@bp.route("/api/admin/sso", methods=["GET"])
@login_required
@admin_required
def api_sso_get():
    """Return current SSO config. Certificate is returned (admins need to see it)."""
    row = query("SELECT * FROM sso_config LIMIT 1", one=True)
    if not row:
        return jsonify({
            "ok": True, "configured": False,
            "enabled": False, "idp_entity_id": "", "idp_sso_url": "",
            "idp_slo_url": "", "idp_cert": "",
            "attr_email": "email", "attr_username": "username",
            "attr_firstname": "firstName", "attr_lastname": "lastName",
            "jit_enabled": True, "jit_default_role": "worker",
            "jit_default_permissions": "",
        })
    return jsonify({
        "ok": True,
        "configured":              bool(row["idp_entity_id"] and row["idp_sso_url"] and row["idp_cert"]),
        "enabled":                 bool(row["enabled"]),
        "idp_entity_id":           row["idp_entity_id"] or "",
        "idp_sso_url":             row["idp_sso_url"] or "",
        "idp_slo_url":             row["idp_slo_url"] or "",
        "idp_cert":                row["idp_cert"] or "",
        "attr_email":              row["attr_email"] or "email",
        "attr_username":           row["attr_username"] or "username",
        "attr_firstname":          row["attr_firstname"] or "firstName",
        "attr_lastname":           row["attr_lastname"] or "lastName",
        "jit_enabled":             bool(row["jit_enabled"]),
        "jit_default_role":        row["jit_default_role"] or "worker",
        "jit_default_permissions": row["jit_default_permissions"] or "",
    })


@bp.route("/api/admin/sso", methods=["POST"])
@login_required
@admin_required
def api_sso_save():
    """Create or update the tenant's SSO/SAML configuration."""
    d = request.json or {}
    enabled    = 1 if d.get("enabled") else 0
    idp_eid    = (d.get("idp_entity_id") or "").strip()
    idp_sso    = (d.get("idp_sso_url") or "").strip()
    idp_slo    = (d.get("idp_slo_url") or "").strip()
    idp_cert   = (d.get("idp_cert") or "").strip()
    # Strip PEM header/footer if accidentally pasted — python3-saml wants raw base64
    idp_cert   = idp_cert.replace("-----BEGIN CERTIFICATE-----", "") \
                         .replace("-----END CERTIFICATE-----", "") \
                         .replace("\n", "").replace("\r", "").strip()

    if enabled and (not idp_eid or not idp_sso or not idp_cert):
        return jsonify({"ok": False,
                        "msg": "IdP Entity ID, SSO URL, and certificate are required to enable SSO."})

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = query("SELECT id FROM sso_config LIMIT 1", one=True)
    if existing:
        execute(
            "UPDATE sso_config SET enabled=?,idp_entity_id=?,idp_sso_url=?,idp_slo_url=?,"
            "idp_cert=?,attr_email=?,attr_username=?,attr_firstname=?,attr_lastname=?,"
            "jit_enabled=?,jit_default_role=?,jit_default_permissions=?,updated_at=? WHERE id=?",
            [enabled, idp_eid, idp_sso, idp_slo, idp_cert,
             d.get("attr_email", "email"), d.get("attr_username", "username"),
             d.get("attr_firstname", "firstName"), d.get("attr_lastname", "lastName"),
             1 if d.get("jit_enabled", True) else 0,
             d.get("jit_default_role", "worker"),
             d.get("jit_default_permissions", ""),
             now, existing["id"]])
    else:
        execute(
            "INSERT INTO sso_config (enabled,idp_entity_id,idp_sso_url,idp_slo_url,idp_cert,"
            "attr_email,attr_username,attr_firstname,attr_lastname,"
            "jit_enabled,jit_default_role,jit_default_permissions,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [enabled, idp_eid, idp_sso, idp_slo, idp_cert,
             d.get("attr_email", "email"), d.get("attr_username", "username"),
             d.get("attr_firstname", "firstName"), d.get("attr_lastname", "lastName"),
             1 if d.get("jit_enabled", True) else 0,
             d.get("jit_default_role", "worker"),
             d.get("jit_default_permissions", ""),
             now])

    log_action("SSO_CONFIG_SAVE", None, "sso_config",
               f"SSO {'enabled' if enabled else 'disabled'}; IdP={idp_eid or '(none)'}")
    return jsonify({"ok": True})
