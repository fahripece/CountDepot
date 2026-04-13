"""
Migrate a single tenant's SQLite DB to a Postgres database.

Usage:
    python scripts/migrate_to_postgres.py --slug acme --dsn "postgresql://user:pass@host/dbname"
    python scripts/migrate_to_postgres.py --slug acme --dsn "..." --dry-run

Prerequisites:
    pip install psycopg2-binary
    The target Postgres database must already have the schema applied.
    Run schema.py DDL against Postgres first (after adapting placeholder syntax).

Tenant isolation:
    Each tenant gets its own Postgres database or schema. Recommended:
      postgresql://user:pass@host/inventory_acme

The source SQLite file is never modified — safe to run multiple times.
"""

import sqlite3
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

# Tables in dependency order (foreign keys respected)
TABLES_IN_ORDER = [
    "categories",
    "products",
    "product_vendors",
    "items",
    "users",
    "locations",
    "user_locations",
    "checkout_log",
    "audit_log",
    "tasks",
    "kits",
    "distributors",
    "vendor_catalog",
    "purchase_orders",
    "po_lines",
    "po_invoices",
    "po_invoice_lines",
    "item_notes",
    "item_photos",
    "item_modifications",
    "service_log",
    "overdue_reminders",
    "settings",
    "webhooks",
    "webhook_log",
    "notifications",
    "login_log",
    "scheduled_reports",
    "accounting_sync_log",
    "login_otp",
    "password_reset_tokens",
    "email_verification_tokens",
    "api_keys",
    "import_history",
    "import_rows",
    "invoice_imports",
    "invoice_import_rows",
]


def migrate(slug, dsn, dry_run=False):
    sqlite_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    if not os.path.exists(sqlite_path):
        print(f"ERROR: SQLite DB not found at {sqlite_path}")
        sys.exit(1)

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(dsn)
    dst.autocommit = False

    total_rows = 0
    errors     = []

    for table in TABLES_IN_ORDER:
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        except Exception as e:
            print(f"  {table}: SKIP (not in source — {e})")
            continue

        if not rows:
            print(f"  {table}: 0 rows")
            continue

        cols         = list(rows[0].keys())
        col_names    = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        sql          = (f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders}) '
                        f'ON CONFLICT DO NOTHING')

        if dry_run:
            print(f"  {table}: would insert {len(rows)} rows")
            continue

        try:
            with dst.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, sql, [tuple(r) for r in rows], page_size=500)
            dst.commit()
            print(f"  {table}: {len(rows)} rows OK")
            total_rows += len(rows)
        except Exception as e:
            dst.rollback()
            errors.append((table, str(e)))
            print(f"  {table}: ERROR — {e}")

    src.close()
    dst.close()

    print(f"\n{'DRY RUN — ' if dry_run else ''}Migration complete.")
    print(f"  Total rows migrated: {total_rows}")
    if errors:
        print(f"  Errors ({len(errors)} tables):")
        for t, e in errors:
            print(f"    {t}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Migrate tenant SQLite DB to Postgres")
    ap.add_argument("--slug",    required=True,       help="Tenant slug")
    ap.add_argument("--dsn",     required=True,       help="Postgres DSN")
    ap.add_argument("--dry-run", action="store_true", help="Print row counts only, no write")
    args = ap.parse_args()
    migrate(args.slug, args.dsn, args.dry_run)
