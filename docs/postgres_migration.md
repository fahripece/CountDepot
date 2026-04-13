# Postgres Migration Path

## When to migrate

CountDepot uses SQLite per tenant. This is fast and simple for most workloads.
Migrate a tenant to Postgres when:

- **Item count > 50,000** or inventory.db exceeds 500MB
- **Concurrent writes > 5/second** sustained (SQLite write lock becomes a bottleneck)
- **Read replicas needed** (e.g. reporting on a standby)
- **A client requires HA/DR guarantees** beyond filesystem backup

Most tenants will never need Postgres. One large enterprise tenant might.

---

## What changes

### Same across both:
- All schema DDL (CREATE TABLE IF NOT EXISTS, indexes)
- All query/execute call sites in routes — these use parameterized SQL already
- Data model: same tables, same columns, same row shapes

### Differences to resolve:
| SQLite | Postgres |
|--------|----------|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` or `BIGSERIAL` |
| `TEXT` for everything | Typed: `VARCHAR`, `TIMESTAMPTZ`, `BOOLEAN`, `JSONB` |
| `PRAGMA journal_mode=WAL` | Connection pooling (pgBouncer) |
| `SELECT * FROM rows` returns `sqlite3.Row` | Returns `psycopg2.extras.RealDictRow` |
| `?` placeholders | `%s` placeholders |
| `ON CONFLICT` / `INSERT OR REPLACE` | `ON CONFLICT DO UPDATE` (same syntax) |
| `json_extract(col, '$.key')` | `col->>'key'` or `col#>>'{key}'` |
| `COALESCE(col, 0)` | Same |
| `strftime('%Y-%m-%d', ts)` | `to_char(ts, 'YYYY-MM-DD')` |
| `datetime('now')` | `NOW()` |

---

## Migration plan

### Phase 1 — Adapter layer (1–2 days)

1. **Abstract the placeholder character.**
   Add a `DB_PLACEHOLDER` config: `?` for SQLite, `%s` for Postgres.
   `query()` and `execute()` in `app/db.py` substitute it automatically.

2. **Abstract the Row type.**
   `app/db.py` already wraps rows. Add `row_factory` argument so both backends return `dict`-like objects.

3. **Create `app/db_pg.py`** — Postgres-specific `get_db()`, `query()`, `execute()` using `psycopg2` with connection pooling via `psycopg2.pool.ThreadedConnectionPool`.

4. **Toggle via env var:**
   ```
   DB_BACKEND=sqlite   # default
   DB_BACKEND=postgres
   POSTGRES_DSN=postgresql://user:pass@host:5432/dbname
   ```

### Phase 2 — Schema porting (2–4 hours)

Convert `app/schema.py` DDL to Postgres:
- Replace `AUTOINCREMENT` → `SERIAL`
- Replace `TEXT DEFAULT '{}'` → `JSONB DEFAULT '{}'`
- Replace `TEXT` timestamps → `TIMESTAMPTZ`
- Keep `CREATE TABLE IF NOT EXISTS` (Postgres supports it)
- Keep all `CREATE INDEX IF NOT EXISTS` (Postgres supports it)

Run `schema.py` as migration on the target Postgres database.

### Phase 3 — Data migration (per tenant)

Use the built-in export (`/export/full-data`) to get CSVs, then import to Postgres:

```bash
# 1. Export from CountDepot
curl -H "Authorization: Bearer <key>" \
  https://<slug>.countdepot.com/export/full-data -o export.zip

# 2. Unzip and use COPY to load
unzip export.zip
psql $POSTGRES_DSN -c "\COPY items FROM 'items.csv' CSV HEADER"
# ... repeat for each table
```

Or use the migration script below.

### Phase 4 — Cutover (zero-downtime)

1. Put tenant into read-only mode (maintenance banner via settings).
2. Run final delta sync from SQLite → Postgres using `sqlite3` → `psycopg2`.
3. Switch `DB_BACKEND=postgres` env var and restart.
4. Verify, remove maintenance banner.

Estimated total downtime: **< 5 minutes** for tenants under 100k rows.

---

## Migration script skeleton

`scripts/migrate_to_postgres.py`:

```python
"""
Migrate a single tenant's SQLite DB to Postgres.
Usage: python scripts/migrate_to_postgres.py --slug acme --dsn postgresql://...
"""
import sqlite3
import argparse
import psycopg2
import psycopg2.extras
from config import Config
import os

TABLES_IN_ORDER = [
    "categories", "products", "items", "users", "locations",
    "checkout_log", "audit_log", "tasks", "distributors",
    "purchase_orders", "po_lines", "item_notes", "service_log",
    "settings", "webhooks", "notifications", "scheduled_reports",
]

def migrate(slug, dsn, dry_run=False):
    sqlite_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    dst = psycopg2.connect(dsn)
    dst.autocommit = False

    for table in TABLES_IN_ORDER:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"  {table}: 0 rows, skipping")
            continue
        cols = rows[0].keys()
        placeholders = ",".join(["%s"] * len(cols))
        col_names    = ",".join(cols)
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        if dry_run:
            print(f"  {table}: would insert {len(rows)} rows")
            continue
        with dst.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, [tuple(r) for r in rows], page_size=500)
        dst.commit()
        print(f"  {table}: {len(rows)} rows migrated")

    src.close()
    dst.close()
    print("Migration complete.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug",    required=True)
    ap.add_argument("--dsn",     required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    migrate(args.slug, args.dsn, args.dry_run)
```

---

## Rollback plan

If Postgres migration fails:
1. Switch `DB_BACKEND=sqlite` env var.
2. The original SQLite file is untouched — restart immediately reverts.

No data is deleted from SQLite during the migration. The SQLite file is the authoritative source until cutover is confirmed.

---

## Operational notes

- **Connection pooling**: Use `PG_POOL_MIN=2, PG_POOL_MAX=10` for a typical tenant.
- **Backups**: Use `pg_dump` instead of file copy. Add to the backup cron.
- **Monitoring**: Watch `pg_stat_activity` for long-running queries. The SQLite query patterns are already index-friendly.
- **Tenant isolation**: Each tenant gets its own Postgres database (`inventory_<slug>`) or schema (`<slug>`). Do not share a single database across tenants — isolation is a core guarantee.
