import sqlite3
import os
from datetime import datetime, timedelta, timezone
from config import Config


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
            plan        TEXT NOT NULL DEFAULT 'free',
            sector      TEXT NOT NULL DEFAULT '',
            onboarded   INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            owner_email TEXT,
            subscription_status TEXT NOT NULL DEFAULT 'active',
            trial_ends_at TEXT,
            trial_expired_at TEXT,
            scheduled_delete_at TEXT,
            notes TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            cancellation_reason TEXT,
            cancelled_at TEXT,
            free_access INTEGER NOT NULL DEFAULT 0,
            free_inactive_warned_at TEXT,
            free_inactive_delete_at TEXT,
            free_inactive_reason TEXT
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
        CREATE TABLE IF NOT EXISTS platform_mfa_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token      TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cross_login_tokens (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_slug TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            token       TEXT NOT NULL UNIQUE,
            expires_at  TEXT NOT NULL,
            used        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_signups (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            slug          TEXT NOT NULL,
            email         TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            selected_plan TEXT,
            selected_period TEXT,
            token         TEXT NOT NULL UNIQUE,
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            used          INTEGER NOT NULL DEFAULT 0
        );
    """)
    # Migrate existing rows
    cols = [r[1] for r in db.execute("PRAGMA table_info(tenants)").fetchall()]
    if "sector" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN sector TEXT NOT NULL DEFAULT ''")
    if "onboarded" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
    if "subscription_status" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'trial'")
    if "trial_ends_at" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN trial_ends_at TEXT")
    if "trial_expired_at" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN trial_expired_at TEXT")
    if "scheduled_delete_at" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN scheduled_delete_at TEXT")
    if "notes" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN notes TEXT")
    if "stripe_customer_id" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT")
    if "stripe_subscription_id" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN stripe_subscription_id TEXT")
    if "owner_email" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN owner_email TEXT")
    if "cancellation_reason" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN cancellation_reason TEXT")
    if "cancelled_at" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN cancelled_at TEXT")
    if "free_access" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN free_access INTEGER NOT NULL DEFAULT 0")
    if "free_inactive_warned_at" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN free_inactive_warned_at TEXT")
    if "free_inactive_delete_at" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN free_inactive_delete_at TEXT")
    if "free_inactive_reason" not in cols:
        db.execute("ALTER TABLE tenants ADD COLUMN free_inactive_reason TEXT")
    pending_cols = [r[1] for r in db.execute("PRAGMA table_info(pending_signups)").fetchall()]
    if "selected_plan" not in pending_cols:
        db.execute("ALTER TABLE pending_signups ADD COLUMN selected_plan TEXT")
    if "selected_period" not in pending_cols:
        db.execute("ALTER TABLE pending_signups ADD COLUMN selected_period TEXT")
    db.execute(
        "UPDATE tenants SET plan='free', subscription_status='active', trial_ends_at=NULL, "
        "trial_expired_at=NULL, scheduled_delete_at=NULL "
        "WHERE subscription_status='trial' AND COALESCE(stripe_subscription_id,'')='' "
        "AND COALESCE(free_access,0)=0"
    )
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


def get_tenant_by_owner_email(email):
    """Look up a tenant by owner email. Returns a Row or None."""
    if not email:
        return None
    db = get_platform_db()
    row = db.execute(
        "SELECT * FROM tenants WHERE LOWER(owner_email) = LOWER(?)", [email]
    ).fetchone()
    db.close()
    return row


def create_tenant(slug, name, plan="free", owner_email=None):
    """Create a new tenant and their data directory."""
    db  = get_platform_db()
    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    subscription_status = "active" if plan == "free" else "trial"
    trial_ends = None if plan == "free" else (_utc_now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO tenants (slug, name, plan, sector, onboarded, active, created_at, "
        "owner_email, subscription_status, trial_ends_at) VALUES (?,?,?,'',0,1,?,?,?,?)",
        [slug, name, plan, now, owner_email, subscription_status, trial_ends]
    )
    db.commit()
    db.close()
    # Create the tenant's data directory
    tenant_dir = os.path.join(Config.TENANTS_DIR, slug)
    os.makedirs(tenant_dir, exist_ok=True)


def backup_all_dbs():
    """Copy all tenant DBs + platform.db to data/backups/YYYY-MM-DD_HH-MM/.
    Returns (backup_dir, list_of_files_backed_up)."""
    import shutil
    from datetime import datetime as _dt
    stamp      = _dt.now().strftime("%Y-%m-%d_%H-%M")
    backup_dir = os.path.join(Config.DATA_DIR, "backups", stamp)
    os.makedirs(backup_dir, exist_ok=True)
    backed_up  = []
    # Platform DB
    if os.path.exists(Config.PLATFORM_DB_PATH):
        dst = os.path.join(backup_dir, "platform.db")
        shutil.copy2(Config.PLATFORM_DB_PATH, dst)
        backed_up.append("platform.db")
    # Tenant DBs
    if os.path.isdir(Config.TENANTS_DIR):
        for slug in os.listdir(Config.TENANTS_DIR):
            db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
            if os.path.exists(db_path):
                dst = os.path.join(backup_dir, f"{slug}_inventory.db")
                shutil.copy2(db_path, dst)
                backed_up.append(f"{slug}/inventory.db")
    return backup_dir, backed_up


def set_tenant_sector(slug, sector):
    """Mark a tenant as onboarded with a chosen sector."""
    db = get_platform_db()
    db.execute("UPDATE tenants SET sector=?, onboarded=1 WHERE slug=?", [sector, slug])
    db.commit()
    db.close()
