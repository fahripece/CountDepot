import sqlite3
import os
from config import Config


def get_platform_db():
    """Open a connection to platform.db (tenant registry).
    Completely separate from any tenant's inventory DB."""
    os.makedirs(Config.DATA_DIR, exist_ok=True)
    db = sqlite3.connect(Config.PLATFORM_DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_platform_db():
    """Create the tenants table if it doesn't exist."""
    db = get_platform_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tenants (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            plan        TEXT NOT NULL DEFAULT 'standard',
            sector      TEXT NOT NULL DEFAULT '',
            onboarded   INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rate_limits (
            ip       TEXT NOT NULL,
            endpoint TEXT NOT NULL DEFAULT 'login',
            ts       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rate_limits ON rate_limits(ip, endpoint, ts);
        CREATE TABLE IF NOT EXISTS security_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            event    TEXT NOT NULL,
            username TEXT,
            ip       TEXT,
            tenant   TEXT,
            detail   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_security_log_ts ON security_log(ts DESC);
    """)
    # Migrate existing rows
    cols = [r[1] for r in db.execute("PRAGMA table_info(tenants)").fetchall()]
    if "sector" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN sector TEXT NOT NULL DEFAULT ''")
    if "onboarded" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
    db.commit()
    db.close()


def get_tenant_by_slug(slug):
    """Look up a tenant by slug. Returns a Row or None."""
    db = get_platform_db()
    row = db.execute(
        "SELECT * FROM tenants WHERE slug = ?", [slug]
    ).fetchone()
    db.close()
    return row


def create_tenant(slug, name, plan="standard"):
    """Create a new tenant and their data directory."""
    from datetime import datetime
    db = get_platform_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO tenants (slug, name, plan, sector, onboarded, active, created_at) VALUES (?,?,?,'',0,1,?)",
        [slug, name, plan, now]
    )
    db.commit()
    db.close()
    # Create the tenant's data directory
    tenant_dir = os.path.join(Config.TENANTS_DIR, slug)
    os.makedirs(tenant_dir, exist_ok=True)


def set_tenant_sector(slug, sector):
    """Mark a tenant as onboarded with a chosen sector."""
    db = get_platform_db()
    db.execute("UPDATE tenants SET sector=?, onboarded=1 WHERE slug=?", [sector, slug])
    db.commit()
    db.close()
