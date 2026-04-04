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
    cost_sold   = query("SELECT SUM(COALESCE(cost_price,0)) FROM items WHERE active=1 AND sold=1", one=True)[0] or 0
    profit      = round(revenue - cost_sold, 2)
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
                  "revenue": revenue, "profit": profit, "sold_count": sold_count},
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
    search  = request.args.get("q", "").strip()
    cat_id  = request.args.get("cat", "")
    status  = request.args.get("status", "")
    sort    = request.args.get("sort", "name")
    prod_id = request.args.get("product", "")
    sql  = """SELECT i.*, c.name as category, c.color,
                    p.name as product_name, p.serial_tracked, p.qty_tracked,
                    p.require_scan_checkout as product_scan_req,
                    p.require_serial as product_req_serial,
                    p.require_vendor_sku as product_req_vendor_sku,
                    p.require_internal_sku as product_req_internal_sku,
                    p.print_scan_label as product_print_scan,
                    co.name as company_name
             FROM items i
             LEFT JOIN categories c  ON c.id=i.category_id
             LEFT JOIN products p    ON p.id=i.product_id
             LEFT JOIN companies co  ON co.id=i.company_id
             WHERE i.active=1"""
    args = []
    if search:
        id_search = search.lstrip("#")
        sql += (" AND (i.name LIKE ? OR i.serial LIKE ? OR i.model LIKE ? "
                "OR i.sku LIKE ? OR i.internal_sku LIKE ? "
                "OR CAST(i.shelf AS TEXT) LIKE ? OR i.job_ref LIKE ? "
                "OR i.owner_company LIKE ? OR i.manufacturer LIKE ? "
                "OR i.po_number LIKE ? OR CAST(i.id AS TEXT) = ?)")
        s = f"%{search}%"
        args += [s]*10 + [id_search]
    if cat_id:  sql += " AND i.category_id=?"; args.append(cat_id)
    if prod_id: sql += " AND i.product_id=?";  args.append(prod_id)
    if status == "out":
        sql += " AND i.checked_out=1"
    elif status == "in":
        sql += " AND i.checked_out=0"
    elif status == "low":
        sql += """ AND i.product_id IN (
            SELECT p.id FROM products p
            LEFT JOIN items ii ON ii.product_id=p.id AND ii.active=1 AND ii.sold=0 AND ii.checked_out=0
            WHERE p.active=1 AND p.low_stock_threshold>0
            GROUP BY p.id HAVING COUNT(ii.id)<=p.low_stock_threshold)"""
    else:
        if request.args.get("hide_out", "1") == "1":
            sql += " AND i.checked_out=0 AND i.sold=0"
    order = {"name": "i.name", "shelf": "i.shelf", "cat": "c.name",
             "cost": "i.cost_price", "sale": "i.sale_price",
             "date": "i.purchase_date"}.get(sort, "i.name")
    sql += f" ORDER BY {order}"
    result = []
    for r in query(sql, args):
        d = dict(r)
        d["available"]   = ((d["qty"] or 0) - (d["qty_out"] or 0) if d["qty"] is not None else None)
        d["is_low"]      = False
        d["profit"]      = (round(d["sale_price"] - d["cost_price"], 2) if d["sale_price"] and d["cost_price"] else None)
        d["tax_amount"]  = (round(d["cost_price"] * (d["tax_rate"] or 0) / 100, 2) if d["cost_price"] and d["tax_paid"] == 1 else 0)
        missing = []
        if d.get("product_req_serial")     and not d.get("serial"): missing.append("serial #")
        if d.get("product_req_vendor_sku") and not d.get("sku"):    missing.append("vendor SKU")
        if d.get("cost_price") is None: missing.append("cost")
        if not d.get("shelf"):          missing.append("shelf")
        d["is_incomplete"]  = len(missing) > 0
        d["missing_fields"] = missing
        result.append(d)
    return jsonify(result)


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
    who  = d.get("who", "").strip() or session.get("username", "?")
    new_sale = d.get("update_sale_price")
    if new_sale is not None:
        execute("UPDATE items SET sale_price=? WHERE id=?", [new_sale, item["id"]])
    execute("UPDATE items SET checked_out=1,checkout_date=?,checkout_by=?,job_ref=? WHERE id=?",
            [now, who, d.get("job_ref", ""), item["id"]])
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
    execute("UPDATE items SET checked_out=0,checkout_date=NULL,checkout_by=NULL,job_ref=NULL WHERE id=?",
            [item["id"]])
    log_action("CHECKIN", item["id"], item["name"],
               f"Returned. Was on: {item['job_ref'] or '-'}",
               {"checked_out": 1}, {"checked_out": 0})
    return jsonify({"ok": True})


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
    d    = request.json
    if not d.get("username") or not d.get("password"):
        return jsonify({"ok": False, "msg": "Username and password required"})
    pw_err = validate_password(d["password"])
    if pw_err:
        return jsonify({"ok": False, "msg": pw_err})
    role = d.get("role", "worker")
    if role == "admin":
        perm_str = ",".join(ADMIN_DEFAULT_PERMS)
    else:
        custom = d.get("permissions")
        perm_str = (",".join(set(custom) & set(PERM_KEYS)) if custom is not None
                    else ",".join(WORKER_DEFAULT_PERMS))
    email = (d.get("email") or "").strip() or None
    try:
        uid = execute(
            "INSERT INTO users (username,password,role,permissions,email,must_change_password) VALUES (?,?,?,?,?,1)",
            [d["username"], hash_pw(d["password"]), role, perm_str, email])
        log_action("USER_ADD", detail=f"Added user: {d['username']} | role: {role}")
        return jsonify({"ok": True, "id": uid})
    except Exception:
        return jsonify({"ok": False, "msg": "Username already exists"})


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


@bp.route("/api/audit")
@login_required
@perm_required("view_audit")
def api_audit():
    page   = int(request.args.get("page", 1))
    limit  = 50
    offset = (page - 1) * limit
    search = request.args.get("q", "").strip()
    sql    = "SELECT * FROM audit_log"
    args   = []
    if search:
        sql += (" WHERE action LIKE ? OR item_name LIKE ? OR detail LIKE ? "
                "OR username LIKE ? OR item_serial LIKE ? OR item_sku LIKE ? "
                "OR product_name LIKE ? OR CAST(item_id AS TEXT) LIKE ?")
        s    = f"%{search}%"
        args = [s]*8
    total = query(f"SELECT COUNT(*) FROM ({sql})", args, one=True)[0]
    sql  += f" ORDER BY id DESC LIMIT {limit} OFFSET {offset}"
    return jsonify({"rows": [dict(r) for r in query(sql, args)], "total": total, "page": page})


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


@bp.route("/import/excel", methods=["POST"])
@login_required
@admin_required
def import_excel():
    try:
        import openpyxl
    except ImportError:
        return jsonify({"ok": False, "msg": "openpyxl not installed"})
    f = request.files.get("file")
    if not f: return jsonify({"ok": False, "msg": "No file uploaded"})
    try:
        wb   = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws   = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2: return jsonify({"ok": False, "msg": "File is empty"})
        cat_map  = {r["name"].lower(): r["id"] for r in query("SELECT id,name FROM categories")}
        prod_map = {r["name"].lower(): r["id"] for r in query("SELECT id,name FROM products WHERE active=1")}
        added = skipped = 0; errors = []
        for i, row in enumerate(rows[1:], 2):
            try:
                name = str(row[0] or "").strip()
                if not name: skipped += 1; continue
                _save_item(dict(name=name,
                    manufacturer=str(row[1] or "").strip() or None,
                    model=str(row[2] or "").strip() or None,
                    serial=str(row[3] or "").strip() or None,
                    sku=str(row[4] or "").strip() or None,
                    category_id=cat_map.get(str(row[5] or "").strip().lower()),
                    product_id=prod_map.get(str(row[6] or "").strip().lower()),
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
