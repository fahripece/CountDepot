#!/usr/bin/env python3
"""
One-shot cleanup: remove tasks linked to sold, retired, or deleted items
across all tenant databases.

Background: tasks for items that were sold or retired were not cleaned up
because those actions kept active=1. This script purges the stale rows.

Usage:
    python scripts/cleanup_stale_tasks.py           # dry run (default)
    python scripts/cleanup_stale_tasks.py --fix     # actually delete
"""
import sys
import os
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.platform import get_platform_db
from config import Config

DRY_RUN = "--fix" not in sys.argv


def cleanup_tenant(slug, dry_run):
    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(db_path):
        return 0, "no db"

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        # Find stale tasks: linked to items that are deleted (active=0),
        # sold (sold=1), or retired (retired=1).
        rows = db.execute("""
            SELECT t.id, t.title, t.status,
                   i.name AS item_name,
                   COALESCE(i.active, 1) AS active,
                   COALESCE(i.sold, 0)   AS sold,
                   COALESCE(i.retired, 0) AS retired
            FROM tasks t
            JOIN items i ON i.id = t.item_id
            WHERE t.item_id IS NOT NULL
              AND (COALESCE(i.active,1)=0
                   OR COALESCE(i.sold,0)=1
                   OR COALESCE(i.retired,0)=1)
        """).fetchall()

        # Also tasks whose item_id no longer exists at all
        orphaned = db.execute("""
            SELECT t.id, t.title, t.status
            FROM tasks t
            WHERE t.item_id IS NOT NULL
              AND t.item_id NOT IN (SELECT id FROM items)
        """).fetchall()

        stale_ids = [r["id"] for r in rows] + [r["id"] for r in orphaned]

        count = len(stale_ids)
        if count == 0:
            return 0, "clean"

        details = []
        for r in rows:
            reason = []
            if r["active"] == 0:  reason.append("deleted")
            if r["sold"]   == 1:  reason.append("sold")
            if r["retired"]== 1:  reason.append("retired")
            details.append(f"  task #{r['id']} '{r['title']}' → item '{r['item_name']}' ({', '.join(reason)})")
        for r in orphaned:
            details.append(f"  task #{r['id']} '{r['title']}' → item no longer exists")

        if not dry_run:
            placeholders = ",".join("?" * len(stale_ids))
            db.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", stale_ids)
            db.commit()

        return count, "\n".join(details)
    except Exception as e:
        return -1, f"error: {e}"
    finally:
        db.close()


def main():
    print(f"\n{'DRY RUN — pass --fix to apply changes' if DRY_RUN else '*** APPLYING CHANGES ***'}\n")

    app = create_app()
    with app.app_context():
        pdb = get_platform_db()
        tenants = pdb.execute("SELECT slug, name FROM tenants ORDER BY slug").fetchall()
        pdb.close()

    total_stale = 0
    for t in tenants:
        count, detail = cleanup_tenant(t["slug"], DRY_RUN)
        if count > 0:
            verb = "would remove" if DRY_RUN else "removed"
            print(f"[{t['slug']}] {verb} {count} stale task(s):")
            print(detail)
            total_stale += count
        elif count == 0:
            print(f"[{t['slug']}] clean")
        else:
            print(f"[{t['slug']}] {detail}")

    print(f"\nTotal stale tasks {'found' if DRY_RUN else 'deleted'}: {total_stale}")
    if DRY_RUN and total_stale > 0:
        print("Run with --fix to apply.\n")


if __name__ == "__main__":
    main()
