#!/usr/bin/env python3
"""
CountDepot scheduled report sender.

Runs all due scheduled reports for all active tenants.

Usage:
    python scripts/send_scheduled_reports.py

Schedule daily via cron (run at 7am):
    0 7 * * * /home/countdepot/countdepot/venv/bin/python \
              /home/countdepot/countdepot/scripts/send_scheduled_reports.py \
              >> /home/countdepot/logs/scheduled_reports.log 2>&1
"""

import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    from app.platform import get_platform_db
    from config import Config

    pdb = get_platform_db()
    tenants = pdb.execute("SELECT slug FROM tenants WHERE active=1 ORDER BY slug").fetchall()
    pdb.close()

    log.info(f"Running scheduled reports for {len(tenants)} tenant(s)…")

    for t in tenants:
        slug = t[0]
        try:
            _run_for_tenant(slug, Config)
        except Exception as e:
            log.error(f"[{slug}] Error: {e}", exc_info=True)

    log.info("Done.")


def _run_for_tenant(slug: str, Config):
    import sqlite3
    from pathlib import Path
    from datetime import date

    db_path = Path(Config.TENANTS_DIR) / slug / "inventory.db"
    if not db_path.exists():
        return

    # Use the app's request context approach for the report sender
    # by importing and calling the send function directly
    from app.blueprints.api import _send_scheduled_report

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    today = date.today()
    day_of_week = today.weekday()
    day_of_month = today.day

    rows = cur.execute("SELECT * FROM scheduled_reports WHERE enabled=1").fetchall()
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
        if sched == "weekly" and day_of_week != 0:
            continue
        if sched == "monthly" and day_of_month != 1:
            continue

        # _send_scheduled_report needs DB context — use the raw DB approach
        try:
            ok = _send_report_raw(cur, r["report_type"], r["email"], slug, Config)
        except Exception as e:
            log.error(f"[{slug}] Report {r['report_type']}: {e}")
            ok = False
        if ok:
            cur.execute("UPDATE scheduled_reports SET last_sent=? WHERE id=?",
                        [today.isoformat(), r["id"]])
            con.commit()
            sent += 1

    con.close()
    log.info(f"[{slug}] {sent} report(s) sent")


def _send_report_raw(cur, report_type: str, to: str, slug: str, Config) -> bool:
    """Send a scheduled report using a raw SQLite cursor (no Flask context)."""
    from app.mailer import send_email
    from datetime import date

    today = date.today().isoformat()
    LABELS = {
        "low_stock": "Low Stock Alert",
        "overdue": "Overdue Items",
        "inventory": "Inventory Summary",
        "warranties": "Warranty Expiry (30d)",
        "depreciation": "Depreciation Summary",
    }
    label = LABELS.get(report_type, report_type)

    if report_type == "low_stock":
        rows = cur.execute("""
            SELECT p.name, COUNT(i.id) as available_count, p.low_stock_threshold
            FROM products p
            LEFT JOIN items i ON i.product_id=p.id AND i.active=1 AND i.sold=0 AND i.checked_out=0
            WHERE p.active=1 AND p.low_stock_threshold>0
            GROUP BY p.id HAVING COUNT(i.id)<=p.low_stock_threshold
        """).fetchall()
        if not rows:
            return False
        rows_html = "".join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de;color:#b91c1c">{r["available_count"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["low_stock_threshold"]}</td></tr>'
            for r in rows
        )
        body_html = f'<h2 style="font-size:16px;margin-bottom:12px">Low Stock — {len(rows)} product(s)</h2><table style="width:100%;border-collapse:collapse"><thead><tr><th>Product</th><th>Available</th><th>Minimum</th></tr></thead><tbody>{rows_html}</tbody></table>'

    elif report_type == "overdue":
        rows = cur.execute("""
            SELECT i.name, cl.checked_out_by, cl.expected_return_date
            FROM checkout_log cl JOIN items i ON i.id=cl.item_id
            WHERE cl.checkin_date IS NULL AND cl.expected_return_date < ?
            ORDER BY cl.expected_return_date
        """, [today]).fetchall()
        if not rows:
            return False
        rows_html = "".join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de">{r["checked_out_by"] or "—"}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e3de;color:#b91c1c">{(r["expected_return_date"] or "")[:10]}</td></tr>'
            for r in rows
        )
        body_html = f'<h2 style="font-size:16px;margin-bottom:12px">Overdue Items — {len(rows)}</h2><table style="width:100%;border-collapse:collapse"><thead><tr><th>Item</th><th>Out By</th><th>Due</th></tr></thead><tbody>{rows_html}</tbody></table>'

    elif report_type == "inventory":
        total = cur.execute("SELECT COUNT(*) FROM items WHERE active=1 AND COALESCE(retired,0)=0 AND sold=0").fetchone()[0]
        out   = cur.execute("SELECT COUNT(*) FROM items WHERE active=1 AND checked_out=1").fetchone()[0]
        val   = cur.execute("SELECT COALESCE(SUM(COALESCE(cost_price,0)),0) FROM items WHERE active=1 AND sold=0").fetchone()[0]
        body_html = f'<h2 style="font-size:16px;margin-bottom:12px">Inventory Summary</h2><p>Total: <strong>{total}</strong> · Checked out: <strong>{out}</strong> · Stock value: <strong>${val:,.2f}</strong></p>'

    else:
        return False

    html = f"""
<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">{slug} · {today}</div>
  <h1 style="font-size:20px;font-weight:700;margin-bottom:20px">{label}</h1>
  {body_html}
  <p style="font-size:11px;color:#94a3b8;margin-top:24px">Sent by CountDepot scheduled reports.</p>
</div>"""
    return send_email(to, f"[CountDepot] {label} — {today}", html)


if __name__ == "__main__":
    main()
