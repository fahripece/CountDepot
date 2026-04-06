import csv
import io
import json
import threading
from datetime import datetime, date as _date

from flask import Blueprint, request, jsonify, session, send_file

from app.db import query, execute
from app.helpers import (login_required, perm_required, admin_required,
                         log_action, get_low_stock_alerts, hash_pw, verify_pw,
                         validate_password, api_rate_limit,
                         _item_missing_fields, sync_item_task,
                         _parse_date_range, _date_filter_sql,
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
        tags                  = ",".join(t.strip() for t in str(d.get("tags") or "").split(",") if t.strip()),
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


@bp.route("/api/report/activity")
@login_required
@perm_required("view_audit")
def api_report_activity():
    date_from, date_to = _parse_date_range(request)
    df_sql, df_args    = _date_filter_sql("a.ts", date_from, date_to)
    top_items = query(f"""SELECT item_name, COUNT(*) as cnt FROM audit_log a
                      WHERE action='CHECKOUT' AND item_name IS NOT NULL{df_sql}
                      GROUP BY item_name ORDER BY cnt DESC LIMIT 10""", df_args)
    tech = query(f"""SELECT
                     CASE WHEN detail LIKE 'By: %' THEN
                         TRIM(SUBSTR(detail, 5, CASE WHEN INSTR(detail,' | ')>0
                             THEN INSTR(detail,' | ')-5 ELSE LENGTH(detail) END))
                     ELSE username END as tech, COUNT(*) as cnt
                   FROM audit_log a WHERE action='CHECKOUT'{df_sql}
                   GROUP BY tech ORDER BY cnt DESC LIMIT 10""", df_args)
    actions = query(f"""SELECT action, COUNT(*) as cnt FROM audit_log a
                    WHERE 1=1{df_sql} GROUP BY action ORDER BY cnt DESC""", df_args)
    recent  = query(f"""SELECT a.ts, a.action, a.item_name, a.username, a.detail,
                       a.item_serial, a.item_sku, a.product_name
                   FROM audit_log a WHERE 1=1{df_sql}
                   ORDER BY a.id DESC LIMIT 100""", df_args)
    return jsonify({
        "period":        {"from": date_from, "to": date_to},
        "top_items":     [{"name": r["item_name"], "count": r["cnt"]} for r in top_items],
        "tech_activity": [{"name": r["tech"],      "count": r["cnt"]} for r in tech],
        "actions":       [{"action": r["action"],  "count": r["cnt"]} for r in actions],
        "recent":        [dict(r) for r in recent],
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
    tag      = request.args.get("tag", "").strip()
    cond_f   = request.args.get("cond", "").strip()
    base_where = """FROM items i
             LEFT JOIN categories c  ON c.id=i.category_id
             LEFT JOIN products p    ON p.id=i.product_id
             LEFT JOIN companies co  ON co.id=i.company_id
             LEFT JOIN locations l   ON l.id=i.location_id
             WHERE i.active=1"""
    sql  = """SELECT i.*, c.name as category, c.color,
                    p.name as product_name, p.serial_tracked, p.qty_tracked,
                    p.require_scan_checkout as product_scan_req,
                    p.require_serial as product_req_serial,
                    p.require_vendor_sku as product_req_vendor_sku,
                    p.require_internal_sku as product_req_internal_sku,
                    p.print_scan_label as product_print_scan,
                    co.name as company_name,
                    l.name as location_name
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
    if cat_id:  sql += " AND i.category_id=?"; args.append(cat_id)
    if prod_id: sql += " AND i.product_id=?";  args.append(prod_id)
    if loc_id:  sql += " AND i.location_id=?"; args.append(loc_id)
    if cond_f:  sql += " AND i.condition=?";   args.append(cond_f)
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
    else:
        if not no_paginate:
            sql += " AND i.checked_out=0 AND i.sold=0"

    order = {"name": "i.name", "shelf": "i.shelf", "cat": "c.name",
             "cost": "i.cost_price", "sale": "i.sale_price",
             "date": "i.purchase_date"}.get(sort, "i.name")

    # Count total matching rows
    count_sql = "SELECT COUNT(*) " + sql.split("FROM", 1)[1].split("ORDER")[0]
    # Strip SELECT clause, keep from FROM onward (without ORDER BY)
    where_part = sql[sql.index("FROM"):]
    if "ORDER" in where_part: where_part = where_part[:where_part.rindex("ORDER")]
    total = query("SELECT COUNT(*) " + where_part, args, one=True)[0]

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
                    "pages": pages, "per_page": per_page})


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
    new_sale = d.get("update_sale_price")
    expected_return = d.get("expected_return_date") or None
    if new_sale is not None:
        execute("UPDATE items SET sale_price=? WHERE id=?", [new_sale, item["id"]])
    execute("UPDATE items SET checked_out=1,checkout_date=?,checkout_by=?,job_ref=?,expected_return_date=? WHERE id=?",
            [now, who, d.get("job_ref", ""), expected_return, item["id"]])
    execute("""INSERT INTO checkout_log
               (item_id,item_name,checked_out_by,job_ref,checkout_date,expected_return_date,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            [item["id"], item["name"], who, d.get("job_ref",""), now, expected_return, now_iso])
    log_action("CHECKOUT", item["id"], item["name"],
               f"By: {who} | Job: {d.get('job_ref','')}" +
               (f" | Sale price → ${new_sale:.2f}" if new_sale is not None else ""),
               {"checked_out": 0}, {"checked_out": 1})
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
    execute("UPDATE items SET checked_out=0,checkout_date=NULL,checkout_by=NULL,job_ref=NULL,expected_return_date=NULL WHERE id=?",
            [item["id"]])
    log_action("CHECKIN", item["id"], item["name"],
               f"Returned. Was on: {item['job_ref'] or '-'}" + (f" | Note: {checkin_note}" if checkin_note else ""),
               {"checked_out": 1}, {"checked_out": 0})
    return jsonify({"ok": True})


@bp.route("/api/locations")
@login_required
def api_get_locations():
    return jsonify([dict(r) for r in query("SELECT * FROM locations ORDER BY name")])

@bp.route("/api/locations", methods=["POST"])
@login_required
@admin_required
def api_add_location():
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Name required"})
    try:
        lid = execute("INSERT INTO locations (name,description,created_at) VALUES (?,?,?)",
                      [name, d.get("description",""), datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        return jsonify({"ok": True, "id": lid, "name": name})
    except Exception:
        return jsonify({"ok": False, "msg": "Location name already exists"})

@bp.route("/api/location/<int:loc_id>", methods=["DELETE"])
@login_required
@admin_required
def api_delete_location(loc_id):
    execute("UPDATE items SET location_id=NULL WHERE location_id=?", [loc_id])
    execute("DELETE FROM locations WHERE id=?", [loc_id])
    return jsonify({"ok": True})

@bp.route("/api/item/<int:item_id>/notes")
@login_required
def api_item_notes(item_id):
    rows = query("SELECT * FROM item_notes WHERE item_id=? ORDER BY id DESC", [item_id])
    return jsonify([dict(r) for r in rows])

@bp.route("/api/item/<int:item_id>/note", methods=["POST"])
@login_required
def api_add_note(item_id):
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
    rows = query("""SELECT * FROM item_reservations
                    WHERE item_id=? AND cancelled=0 ORDER BY reserved_from""", [item_id])
    return jsonify([dict(r) for r in rows])


@bp.route("/api/item/<int:item_id>/reservation", methods=["POST"])
@login_required
def api_add_reservation(item_id):
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
    row = query("SELECT data_url FROM item_photos WHERE id=?", [photo_id], one=True)
    if not row:
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
    locs = query("SELECT * FROM locations ORDER BY name")
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
    # Items with no location
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
        nq = item["qty"] + amount
        execute("UPDATE items SET qty=? WHERE id=?", [nq, item["id"]])
        log_action("QTY_ADD", item["id"], item["name"], f"+{amount}. {note}. Total:{nq}", before, {"qty": nq})
    elif action == "remove":
        no = (item["qty_out"] or 0) + amount
        execute("UPDATE items SET qty_out=? WHERE id=?", [no, item["id"]])
        log_action("QTY_REMOVE", item["id"], item["name"], f"-{amount}. {note}. Left:{item['qty']-no}", before, {"qty_out": no})
    elif action == "set":
        execute("UPDATE items SET qty=?,qty_out=0 WHERE id=?", [amount, item["id"]])
        log_action("QTY_SET", item["id"], item["name"], f"Set to {amount}. {note}", before, {"qty": amount})
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
    price   = float(d.get("price") or item["sale_price"] or 0)
    sold_to = d.get("sold_to", "").strip()
    now     = datetime.now().strftime("%Y-%m-%d")
    execute("UPDATE items SET sold=1,sold_date=?,sold_price=?,sold_to=?,checked_out=0 WHERE id=?",
            [now, price, sold_to, item["id"]])
    profit = round(price - (item["cost_price"] or 0), 2)
    log_action("ITEM_SOLD", item["id"], item["name"],
               f"Sold to: {sold_to or 'unknown'} | Price: ${price:.2f} | Profit: ${profit:.2f}",
               {"sold": 0}, {"sold": 1, "price": price})
    return jsonify({"ok": True, "profit": profit})


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
        dup = query("SELECT id,name FROM items WHERE serial=? AND active=1", [serial], one=True)
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
        dup = query("SELECT id,name FROM items WHERE serial=? AND active=1 AND id!=?", [serial, d["id"]], one=True)
        if dup: return jsonify({"ok": False, "msg": f"Duplicate serial — already on item #{dup['id']} ({dup['name']})"})
    sku = (d.get("sku") or "").strip()
    if sku:
        dup = query("SELECT id,name FROM items WHERE sku=? AND active=1 AND id!=?", [sku, d["id"]], one=True)
        if dup: return jsonify({"ok": False, "msg": f"Duplicate SKU — already on item #{dup['id']} ({dup['name']})"})
    try:
        _save_item(d, d["id"])
        log_action("ITEM_EDIT", d["id"], d.get("name"), "Edited", dict(item), d)
        saved = query("SELECT * FROM items WHERE id=?", [d["id"]], one=True)
        if saved:
            sync_item_task(d["id"], d.get("name", ""), _item_missing_fields(dict(saved)))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Save failed: {str(e)}"})


@bp.route("/api/item/delete", methods=["POST"])
@login_required
@perm_required("delete_items")
def api_item_delete():
    d   = request.json
    ids = d.get("ids") or ([d["id"]] if d.get("id") else [])
    for iid in ids:
        item = query("SELECT * FROM items WHERE id=?", [iid], one=True)
        if item:
            execute("UPDATE items SET active=0 WHERE id=?", [iid])
            execute("DELETE FROM tasks WHERE item_id=?", [iid])
            log_action("ITEM_DELETE", iid, item["name"], "Deleted")
    return jsonify({"ok": True, "deleted": len(ids)})


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
    done, skipped = 0, 0
    for iid in ids:
        item = query("SELECT * FROM items WHERE id=? AND active=1", [iid], one=True)
        if not item or item["checked_out"] or item["qty"] is not None or item["sold"]:
            skipped += 1; continue
        execute("UPDATE items SET checked_out=1, checkout_by=?, checkout_date=?, job_ref=?, expected_return_date=? WHERE id=?",
                [who, now, job or None, ret, iid])
        execute("INSERT INTO checkout_log (item_id,item_name,checked_out_by,job_ref,checkout_date,created_at) VALUES (?,?,?,?,?,?)",
                [iid, item["name"], who, job or None, now[:10], now])
        log_action("CHECKOUT", iid, item["name"], f"To: {who}{' | Job: '+job if job else ''}")
        done += 1
    return jsonify({"ok": True, "done": done, "skipped": skipped})


@bp.route("/api/items/bulk-checkin", methods=["POST"])
@login_required
@perm_required("checkout_checkin")
def api_bulk_checkin():
    d    = request.json
    ids  = [int(i) for i in (d.get("ids") or [])]
    note = d.get("checkin_note", "").strip() or None
    if not ids: return jsonify({"ok": False, "msg": "No items selected"})
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user = session.get("username", "")
    done, skipped = 0, 0
    for iid in ids:
        item = query("SELECT * FROM items WHERE id=? AND active=1 AND checked_out=1", [iid], one=True)
        if not item: skipped += 1; continue
        checkout_dt = item["checkout_date"] or now
        try:
            dur = round((datetime.now() - datetime.strptime(checkout_dt[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600, 2)
        except Exception:
            dur = None
        execute("UPDATE items SET checked_out=0, checkout_by=NULL, checkout_date=NULL, job_ref=NULL, expected_return_date=NULL WHERE id=?", [iid])
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
        execute("UPDATE items SET checked_out=1, checkout_by=?, checkout_date=?, job_ref=?, expected_return_date=? WHERE id=?",
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
        execute("UPDATE items SET checked_out=0, checkout_by=NULL, checkout_date=NULL, job_ref=NULL, expected_return_date=NULL WHERE id=?", [item["id"]])
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
            "require_sku_label,default_cost,default_sale,low_stock_threshold,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [d["name"].strip(), d.get("manufacturer") or None, d.get("model") or None,
             d.get("description") or None, d.get("category_id") or None,
             int(d.get("serial_tracked", 0)), int(d.get("qty_tracked", 0)),
             int(d.get("require_scan_checkout", 0)),
             req_serial, req_vendor, req_internal, int(d.get("print_scan_label", 0)), 0,
             float(d["default_cost"]) if d.get("default_cost") not in (None, "") else None,
             float(d["default_sale"]) if d.get("default_sale") not in (None, "") else None,
             int(d.get("low_stock_threshold", 0)) if d.get("low_stock_threshold") not in (None, "") else 0,
             now])
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
        "default_cost=?,default_sale=?,low_stock_threshold=? WHERE id=?",
        [d["name"], d.get("manufacturer") or None, d.get("model") or None,
         d.get("description") or None, d.get("category_id") or None,
         int(d.get("serial_tracked", 0)), int(d.get("qty_tracked", 0)),
         int(d.get("require_scan_checkout", 0)),
         req_serial, req_vendor, req_internal, int(d.get("print_scan_label", 0)),
         float(d["default_cost"]) if d.get("default_cost") not in (None, "") else None,
         float(d["default_sale"]) if d.get("default_sale") not in (None, "") else None,
         int(d.get("low_stock_threshold", 0)) if d.get("low_stock_threshold") not in (None, "") else 0,
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
    rows = query("SELECT * FROM tasks ORDER BY CASE urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC")
    return jsonify([dict(r) for r in rows])


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
def api_contact_edit():
    d = request.json
    execute("UPDATE contacts SET name=?,role=?,email=?,phone=?,notes=?,company_id=?,company_type=? WHERE id=?",
            [(d.get("name") or "").strip(), d.get("role",""), d.get("email",""),
             d.get("phone",""), d.get("notes",""),
             d.get("company_id"), d.get("company_type",""), d["id"]])
    return jsonify({"ok": True})


@bp.route("/api/contact/delete", methods=["POST"])
@login_required
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
    pw_err = validate_password(d.get("new_password", ""))
    if pw_err: return jsonify({"ok": False, "msg": pw_err})
    execute("UPDATE users SET password=? WHERE id=?",
            [hash_pw(d["new_password"]), session["user_id"]])
    return jsonify({"ok": True})


# ── Alerts & Audit ────────────────────────────────────────────────────────────

@bp.route("/api/alerts")
@login_required
def api_alerts():
    return jsonify(get_low_stock_alerts())


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
    alert_email = settings.get("low_stock_alert_email", "").strip()
    if not alert_email:
        return jsonify({"ok": False, "msg": "No alert email address set. Add one in the Low Stock Alerts settings."})

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

    ok = send_email(alert_email, f"Low Stock Alert — {len(alerts)} product(s) need restocking", html, plain)
    if ok:
        log_action("LOW_STOCK_REPORT_SENT", detail=f"Sent to {alert_email}, {len(alerts)} items")
        return jsonify({"ok": True, "msg": f"Report sent to {alert_email}"})
    return jsonify({"ok": False, "msg": "Failed to send email. Check SMTP settings on the server."})


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


# ── Export / Import ───────────────────────────────────────────────────────────

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
