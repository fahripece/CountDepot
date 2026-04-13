"""
CountDepot Daily Ops Digest — email the platform admin a morning summary.

Collects: active tenant count, 500 errors in last 24h, total low-stock alerts,
overdue checkouts, warranties expiring in 30d, backup age, disk free pct.

Usage:
    python scripts/send_daily_digest.py

Schedule (cron — 7am UTC):
    0 7 * * * /home/countdepot/countdepot/venv/bin/python \
              /home/countdepot/countdepot/scripts/send_daily_digest.py \
              >> /var/log/countdepot-digest.log 2>&1
"""

import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("digest")

SCRIPT_DIR  = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from config import Config


def _pdb():
    db = sqlite3.connect(Config.PLATFORM_DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def _tenant_db(slug):
    path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def collect_stats() -> dict:
    stats = {}

    # ── Platform stats ────────────────────────────────────────────────────────
    try:
        db = _pdb()
        tenants = [r["slug"] for r in
                   db.execute("SELECT slug FROM tenants WHERE active=1").fetchall()]
        stats["tenants"] = len(tenants)

        # 500 errors in last 24h from security_log
        cutoff = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            errors = db.execute(
                "SELECT COUNT(*) FROM security_log WHERE event='ERROR_500' AND ts>=?",
                [cutoff]).fetchone()[0]
        except Exception:
            errors = 0
        stats["errors_24h"] = errors
        db.close()
    except Exception as e:
        log.error(f"Platform DB error: {e}")
        tenants = []
        stats["tenants"]   = 0
        stats["errors_24h"] = 0

    # ── Aggregate across all tenant DBs ───────────────────────────────────────
    low_stock_total = 0
    overdue_total   = 0
    warranty_total  = 0
    today           = datetime.utcnow().strftime("%Y-%m-%d")
    in_30d          = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")

    for slug in tenants:
        try:
            db = _tenant_db(slug)
            if not db:
                continue

            # Low stock: products with available qty <= threshold
            ls = db.execute("""
                SELECT COUNT(*) FROM products p
                LEFT JOIN items i ON i.product_id=p.id AND i.active=1 AND i.sold=0 AND i.checked_out=0
                WHERE p.active=1 AND p.low_stock_threshold>0
                GROUP BY p.id HAVING COUNT(i.id)<=p.low_stock_threshold
            """).fetchone()
            low_stock_total += (ls[0] if ls else 0)

            # Overdue checkouts
            od = db.execute(
                "SELECT COUNT(*) FROM items WHERE active=1 AND checked_out=1 "
                "AND expected_return_date IS NOT NULL AND expected_return_date<?",
                [today]).fetchone()
            overdue_total += (od[0] if od else 0)

            # Warranties expiring in 30d
            wt = db.execute(
                "SELECT COUNT(*) FROM items WHERE active=1 AND retired=0 "
                "AND warranty_expiry IS NOT NULL AND warranty_expiry!='' "
                "AND warranty_expiry<=? AND warranty_expiry>=?",
                [in_30d, today]).fetchone()
            warranty_total += (wt[0] if wt else 0)

            db.close()
        except Exception as e:
            log.warning(f"  {slug}: {e}")

    stats["low_stock_alerts"]     = low_stock_total
    stats["overdue_checkouts"]    = overdue_total
    stats["warranty_expiring_30d"] = warranty_total

    # ── Infrastructure stats ──────────────────────────────────────────────────
    try:
        usage = shutil.disk_usage(Config.DATA_DIR)
        stats["disk_free_pct"] = round(usage.free / usage.total * 100, 1)
        stats["disk_free_gb"]  = round(usage.free / 1_073_741_824, 2)
    except Exception:
        pass

    try:
        backup_dir = Path(Config.DATA_DIR).parent / "backups"
        archives   = sorted(backup_dir.glob("countdepot_*.tar.gz"),
                            key=lambda p: p.stat().st_mtime) if backup_dir.exists() else []
        if archives:
            stats["backup_age_hours"] = round(
                (time.time() - archives[-1].stat().st_mtime) / 3600, 1)
    except Exception:
        pass

    return stats


def main():
    to = Config.PLATFORM_ADMIN_EMAIL or Config.SUPPORT_EMAIL
    if not to:
        log.error("No PLATFORM_ADMIN_EMAIL or SUPPORT_EMAIL set — nowhere to send digest")
        sys.exit(1)

    log.info("Collecting stats...")
    stats = collect_stats()
    log.info(f"Stats: {stats}")

    from app.mailer import send_daily_digest
    ok = send_daily_digest(to, stats)
    if ok:
        log.info(f"Digest sent to {to}")
    else:
        log.error("Digest send failed — check SMTP config")
        sys.exit(1)


if __name__ == "__main__":
    main()
