#!/usr/bin/env python3
"""
List all CountDepot tenants with basic stats.

Usage:
    python scripts/list_tenants.py
"""
import sys
import os
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.platform import get_platform_db
from config import Config


def tenant_stats(slug):
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return {"items": "—", "users": "—", "size": "—", "last_login": "—"}
    size_kb = round(os.path.getsize(db_path) / 1024, 1)
    db = sqlite3.connect(db_path)
    try:
        items = db.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        last = db.execute("SELECT MAX(last_login) FROM users WHERE last_login IS NOT NULL").fetchone()[0]
    except Exception:
        items = users = last = "—"
    finally:
        db.close()
    return {"items": items, "users": users, "size": f"{size_kb}KB", "last_login": last or "never"}


def main():
    app = create_app()
    with app.app_context():
        db = get_platform_db()
        tenants = db.execute("SELECT * FROM tenants ORDER BY created_at").fetchall()
        db.close()

        if not tenants:
            print("No tenants found.")
            return

        print(f"\n{'SLUG':<20} {'NAME':<25} {'PLAN':<12} {'SECTOR':<14} {'STATUS':<10} {'ITEMS':<7} {'USERS':<7} {'DB SIZE':<10} {'LAST LOGIN'}")
        print("─" * 130)
        for t in tenants:
            stats  = tenant_stats(t["slug"])
            status = "active" if t["active"] else "SUSPENDED"
            onbd   = "✓" if t["onboarded"] else "pending"
            print(f"{t['slug']:<20} {t['name']:<25} {t['plan']:<12} {(t['sector'] or onbd):<14} {status:<10} {str(stats['items']):<7} {str(stats['users']):<7} {stats['size']:<10} {stats['last_login']}")
        print(f"\n{len(tenants)} tenant(s) total.\n")


if __name__ == "__main__":
    main()
