#!/usr/bin/env python3
"""
CountDepot overdue escalation reminder script.

Iterates all active tenants and fires the overdue reminder check for each.
Run this daily via cron to send reminder emails to users with overdue checkouts.

Usage:
    python scripts/send_overdue_reminders.py

Schedule daily via cron (run at 8am):
    0 8 * * * /home/countdepot/countdepot/venv/bin/python \
              /home/countdepot/countdepot/scripts/send_overdue_reminders.py \
              >> /home/countdepot/logs/overdue_reminders.log 2>&1

Requires the app to be importable (run from the project root or with PYTHONPATH set).
"""

import sys
import os
import logging
from datetime import date

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    from app import create_app
    from app.platform import get_platform_db
    from app.mailer import send_overdue_reminder
    from config import Config

    app = create_app()

    # Get all active tenants
    pdb = get_platform_db()
    tenants = pdb.execute(
        "SELECT slug FROM tenants WHERE active=1 ORDER BY slug"
    ).fetchall()
    pdb.close()

    log.info(f"Running overdue reminders for {len(tenants)} tenant(s)…")

    for t in tenants:
        slug = t[0]
        try:
            _run_for_tenant(app, slug, Config)
        except Exception as e:
            log.error(f"[{slug}] Error: {e}")

    log.info("Done.")


def _run_for_tenant(app, slug, Config):
    import sqlite3
    from pathlib import Path

    db_path = Path(Config.TENANTS_DIR) / slug / "inventory.db"
    if not db_path.exists():
        return

    today = date.today().isoformat()
    domain = Config.APP_DOMAIN
    base_url = f"https://{slug}.{domain}"

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Read settings
    rows = cur.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    enabled = settings.get("overdue_reminder_enabled", "1") == "1"
    days_1 = int(settings.get("overdue_reminder_days_1", "1") or 1)
    days_2 = int(settings.get("overdue_reminder_days_2", "7") or 7)

    if not enabled:
        log.info(f"[{slug}] Reminders disabled, skipping.")
        con.close()
        return

    overdue_rows = cur.execute("""
        SELECT cl.id as cl_id, cl.item_id, cl.checked_out_by, cl.checkout_date,
               cl.expected_return_date, i.name as item_name, i.serial,
               u.email as user_email
        FROM checkout_log cl
        JOIN items i ON i.id = cl.item_id
        LEFT JOIN users u ON lower(u.username) = lower(cl.checked_out_by)
        WHERE cl.checkin_date IS NULL
          AND cl.expected_return_date IS NOT NULL
          AND cl.expected_return_date < ?
    """, [today]).fetchall()

    sent_count = 0
    for row in overdue_rows:
        due = row["expected_return_date"]
        diff = (date.fromisoformat(today) - date.fromisoformat(due)).days
        email = row["user_email"]
        if not email:
            continue

        for (num, threshold) in [(1, days_1), (2, days_2)]:
            if diff < threshold:
                continue
            already = cur.execute(
                "SELECT id FROM overdue_reminders WHERE checkout_log_id=? AND reminder_num=?",
                [row["cl_id"], num]
            ).fetchone()
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
                cur.execute(
                    """INSERT INTO overdue_reminders
                       (checkout_log_id, item_id, reminded_at, reminder_num, sent_to)
                       VALUES (?,?,?,?,?)""",
                    [row["cl_id"], row["item_id"], today, num, email]
                )
                con.commit()
                sent_count += 1

    con.close()
    log.info(f"[{slug}] {sent_count} reminder(s) sent for {len(overdue_rows)} overdue checkout(s)")


if __name__ == "__main__":
    main()
