"""
CountDepot database schema + migration system.

HOW UPDATES WORK (read this before touching this file):
=========================================================
  - CREATE TABLE IF NOT EXISTS  →  safe to re-run, never drops data
  - CREATE INDEX IF NOT EXISTS  →  same
  - _run_migrations()           →  adds new columns to existing tables
                                   using ALTER TABLE ... ADD COLUMN IF NOT EXISTS
                                   (safe: silently skips if column already exists)

To add a new column in a future version:
  1. Add it to the CREATE TABLE block (for new installs)
  2. Add an entry to MIGRATIONS list with the same column + type
  3. Deploy — existing DBs get the column automatically on next restart

NEVER:
  - DROP a table
  - RENAME a column
  - Change a column type
  - Remove a row from MIGRATIONS
"""

from datetime import datetime
from app.db import get_db
from app.helpers import hash_pw


# ── Migration registry ────────────────────────────────────────────────────────
# Every column that was added after initial release lives here.
# Format: (table, column, sqlite_type_with_default)
# init_db() runs these on every startup — safe to re-run, idempotent.

MIGRATIONS = [
    # users table additions
    ("users", "email",                "TEXT"),
    ("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_login",           "TEXT"),

    # products additions
    ("products", "require_serial",       "INTEGER DEFAULT 0"),
    ("products", "require_vendor_sku",   "INTEGER DEFAULT 0"),
    ("products", "require_internal_sku", "INTEGER DEFAULT 1"),
    ("products", "require_sku_label",    "INTEGER DEFAULT 0"),
    ("products", "print_scan_label",     "INTEGER DEFAULT 0"),
    ("products", "low_stock_threshold",    "INTEGER DEFAULT 0"),
    ("products", "low_stock_last_alerted", "TEXT"),

    # items additions
    ("items", "extra_fields",    "TEXT DEFAULT '{}'"),
    ("items", "parent_item_id", "INTEGER"),

    # tasks additions
    ("tasks", "item_id",    "INTEGER"),
    ("tasks", "updated_at", "TEXT"),

    # audit_log additions
    ("audit_log", "item_serial",  "TEXT"),
    ("audit_log", "item_sku",     "TEXT"),
    ("audit_log", "product_name", "TEXT"),

    # users — email verification (default 1 = verified for all pre-existing accounts)
    ("users", "email_verified",  "INTEGER NOT NULL DEFAULT 1"),
    # users — session token for server-side invalidation
    ("users", "session_token",   "TEXT"),

    # users — 2FA
    ("users", "two_fa_enabled", "INTEGER NOT NULL DEFAULT 0"),

    # items — inventory features
    ("items", "expected_return_date",  "TEXT"),
    ("items", "next_maintenance_date", "TEXT"),
    ("items", "location_id",           "INTEGER"),
    ("items", "depreciation_rate",     "REAL"),

    # items — new inventory features
    ("items", "tags",   "TEXT NOT NULL DEFAULT ''"),
    ("items", "kit_id", "INTEGER"),

    # items — out-of-service + recall
    ("items", "out_of_service",        "INTEGER NOT NULL DEFAULT 0"),
    ("items", "out_of_service_reason", "TEXT"),
    ("items", "recall_flag",           "INTEGER NOT NULL DEFAULT 0"),
    ("items", "recall_notes",          "TEXT"),

    # items — checkout department/ward
    ("items", "checkout_dept", "TEXT"),

    # checkout_log — department/ward
    ("checkout_log", "checkout_dept", "TEXT"),

    # tasks — key for maintenance-type tasks
    ("tasks", "task_key", "TEXT"),

    # locations — per-site alert email
    ("locations", "email", "TEXT"),

    # ── ADD NEW COLUMNS HERE when you update the app ──────────────────────────
]


def init_db():
    """Create schema + run migrations. Safe to call on every request startup."""
    _init_db_conn(get_db())


def _init_db_conn(db):
    """Apply full schema, migrations, and seed data to a raw sqlite3 connection.
    Safe to call on any fresh connection — all DDL uses CREATE IF NOT EXISTS.
    Used by init_db() (request-scoped) and _bootstrap_tenant_db (out-of-context)."""

    # ── Schema DDL ─────────────────────────────────────────────────────────────
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            username             TEXT UNIQUE NOT NULL,
            password             TEXT NOT NULL,
            role                 TEXT NOT NULL DEFAULT 'worker',
            permissions          TEXT NOT NULL DEFAULT '',
            email                TEXT,
            email_verified       INTEGER NOT NULL DEFAULT 1,
            session_token        TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            last_login           TEXT
        );
        CREATE TABLE IF NOT EXISTS categories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            color      TEXT NOT NULL DEFAULT '#ffffff',
            is_expense INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS category_fields (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id      INTEGER NOT NULL REFERENCES categories(id),
            field_label      TEXT NOT NULL,
            field_key        TEXT NOT NULL,
            field_type       TEXT NOT NULL DEFAULT 'text',
            placeholder      TEXT,
            required         INTEGER DEFAULT 0,
            sort_order       INTEGER DEFAULT 0,
            dropdown_options TEXT
        );
        CREATE TABLE IF NOT EXISTS products (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            name                  TEXT NOT NULL,
            manufacturer          TEXT,
            model                 TEXT,
            description           TEXT,
            category_id           INTEGER REFERENCES categories(id),
            serial_tracked        INTEGER DEFAULT 0,
            qty_tracked           INTEGER DEFAULT 0,
            require_scan_checkout INTEGER DEFAULT 0,
            require_serial        INTEGER DEFAULT 0,
            require_vendor_sku    INTEGER DEFAULT 0,
            require_internal_sku  INTEGER DEFAULT 1,
            require_sku_label     INTEGER DEFAULT 0,
            print_scan_label      INTEGER DEFAULT 0,
            default_cost          REAL,
            default_sale          REAL,
            low_stock_threshold   INTEGER DEFAULT 0,
            image_url             TEXT,
            active                INTEGER DEFAULT 1,
            created_at            TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS items (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id            INTEGER REFERENCES products(id),
            name                  TEXT NOT NULL,
            manufacturer          TEXT,
            model                 TEXT,
            serial                TEXT,
            sku                   TEXT,
            internal_sku          TEXT,
            category_id           INTEGER REFERENCES categories(id),
            condition             TEXT DEFAULT 'New',
            shelf                 TEXT,
            owner_company         TEXT,
            company_id            INTEGER REFERENCES companies(id),
            purchased_from        TEXT,
            purchase_date         TEXT,
            po_number             TEXT,
            qty                   INTEGER,
            qty_out               INTEGER DEFAULT 0,
            low_stock_threshold   INTEGER DEFAULT 0,
            checked_out           INTEGER DEFAULT 0,
            checkout_date         TEXT,
            checkout_by           TEXT,
            job_ref               TEXT,
            require_scan_checkout INTEGER DEFAULT -1,
            cost_price            REAL,
            sale_price            REAL,
            tax_paid              INTEGER DEFAULT -1,
            tax_rate              REAL DEFAULT 0,
            sale_state            TEXT,
            sold                  INTEGER DEFAULT 0,
            sold_date             TEXT,
            sold_price            REAL,
            sold_to               TEXT,
            ebay_status           TEXT DEFAULT 'not_listed',
            ebay_listing_id       TEXT,
            ebay_listed_price     REAL,
            ebay_listed_date      TEXT,
            cpu                   TEXT,
            ram                   TEXT,
            storage               TEXT,
            os_type               TEXT,
            screen_size           TEXT,
            battery_life          TEXT,
            imei                  TEXT,
            carrier               TEXT,
            resolution            TEXT,
            lens_type             TEXT,
            has_poe               INTEGER DEFAULT 0,
            wireless_standard     TEXT,
            port_count            INTEGER,
            poe_budget            TEXT,
            throughput            TEXT,
            cable_type            TEXT,
            cable_gauge           TEXT,
            connector_type        TEXT,
            cable_length          TEXT,
            extra_fields          TEXT DEFAULT '{}',
            notes                 TEXT,
            parent_item_id        INTEGER REFERENCES items(id),
            active                INTEGER DEFAULT 1,
            created_at            TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS companies (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            name   TEXT UNIQUE NOT NULL,
            notes  TEXT,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS distributors (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            urgency    TEXT NOT NULL DEFAULT 'medium',
            status     TEXT NOT NULL DEFAULT 'todo',
            notes      TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            item_id    INTEGER REFERENCES items(id)
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            role         TEXT,
            email        TEXT,
            phone        TEXT,
            notes        TEXT,
            company_id   INTEGER,
            company_type TEXT,
            active       INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS item_modifications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id         INTEGER NOT NULL REFERENCES items(id),
            ts              TEXT NOT NULL,
            modified_by     TEXT NOT NULL,
            field_changed   TEXT NOT NULL,
            old_value       TEXT,
            new_value       TEXT,
            notes           TEXT,
            spawned_item_id INTEGER REFERENCES items(id)
        );
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            token      TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            token      TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS locations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER NOT NULL REFERENCES items(id),
            note       TEXT NOT NULL,
            username   TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS kits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT,
            created_by  TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            name        TEXT NOT NULL,
            key_hash    TEXT NOT NULL UNIQUE,
            key_prefix  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            last_used   TEXT,
            active      INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS login_otp (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            otp        TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS checkout_log (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id              INTEGER REFERENCES items(id),
            item_name            TEXT,
            checked_out_by       TEXT,
            job_ref              TEXT,
            checkout_date        TEXT,
            expected_return_date TEXT,
            checkin_date         TEXT,
            checkin_note         TEXT,
            checkin_by           TEXT,
            duration_hours       REAL,
            created_at           TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_reservations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id       INTEGER NOT NULL REFERENCES items(id),
            reserved_by   TEXT NOT NULL,
            reserved_from TEXT NOT NULL,
            reserved_to   TEXT NOT NULL,
            purpose       TEXT,
            created_by    TEXT,
            created_at    TEXT NOT NULL,
            cancelled     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS item_photos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL REFERENCES items(id),
            data_url    TEXT NOT NULL,
            caption     TEXT,
            uploaded_by TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS service_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id           INTEGER NOT NULL REFERENCES items(id),
            service_date      TEXT NOT NULL,
            service_type      TEXT NOT NULL,
            performed_by      TEXT,
            provider          TEXT,
            notes             TEXT,
            next_service_date TEXT,
            created_by        TEXT,
            created_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_service_log_item ON service_log(item_id);
        CREATE TABLE IF NOT EXISTS user_locations (
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, location_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            action       TEXT NOT NULL,
            item_id      INTEGER,
            item_name    TEXT,
            item_serial  TEXT,
            item_sku     TEXT,
            product_name TEXT,
            detail       TEXT,
            before_state TEXT,
            after_state  TEXT,
            username     TEXT
        );
        CREATE TABLE IF NOT EXISTS schema_version (
            id      INTEGER PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 0,
            applied_at TEXT NOT NULL
        );
    """)

    # ── Indexes ────────────────────────────────────────────────────────────────
    db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_items_active      ON items(active);
        CREATE INDEX IF NOT EXISTS idx_items_serial      ON items(serial);
        CREATE INDEX IF NOT EXISTS idx_items_category    ON items(category_id);
        CREATE INDEX IF NOT EXISTS idx_items_product     ON items(product_id);
        CREATE INDEX IF NOT EXISTS idx_items_checked_out ON items(checked_out);
        CREATE INDEX IF NOT EXISTS idx_items_sold        ON items(sold);
        CREATE INDEX IF NOT EXISTS idx_items_sku         ON items(internal_sku);
        CREATE INDEX IF NOT EXISTS idx_items_company     ON items(company_id);
        CREATE INDEX IF NOT EXISTS idx_items_parent      ON items(parent_item_id);
        CREATE INDEX IF NOT EXISTS idx_items_ebay        ON items(ebay_status);
        CREATE INDEX IF NOT EXISTS idx_audit_ts          ON audit_log(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_item        ON audit_log(item_id);
        CREATE INDEX IF NOT EXISTS idx_audit_user        ON audit_log(username);
        CREATE INDEX IF NOT EXISTS idx_audit_serial      ON audit_log(item_serial);
        CREATE INDEX IF NOT EXISTS idx_audit_sku         ON audit_log(item_sku);
        CREATE INDEX IF NOT EXISTS idx_audit_product     ON audit_log(product_name);
        CREATE INDEX IF NOT EXISTS idx_mods_item         ON item_modifications(item_id);
        CREATE INDEX IF NOT EXISTS idx_products_cat      ON products(category_id);
    """)

    # ── Run migrations ─────────────────────────────────────────────────────────
    _run_migrations(db)

    # ── Seed default data if tables are empty ──────────────────────────────────
    if not db.execute("SELECT COUNT(*) FROM companies").fetchone()[0]:
        for co in ["Other"]:
            try:
                db.execute("INSERT INTO companies (name) VALUES (?)", [co])
            except Exception:
                pass

    if not db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]:
        _seed_categories(db)

    db.commit()


def _run_migrations(db):
    """
    Apply all entries in MIGRATIONS to the live database.
    Each migration is idempotent — if the column already exists it's skipped silently.
    This means you can deploy a new version and existing data is never touched.
    """
    for table, column, typedef in MIGRATIONS:
        # Check if column already exists
        existing = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing:
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
            except Exception as e:
                # Log but don't crash — a failed migration shouldn't take down the app
                import logging
                logging.getLogger(__name__).warning(
                    f"Migration failed: ALTER TABLE {table} ADD COLUMN {column} — {e}")


def seed_tenant(slug):
    """
    Seed a brand-new tenant DB with default users and distributors.
    Called once when a tenant is first created via the platform panel.
    """
    db = get_db()

    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return  # already seeded

    db.execute(
        "INSERT INTO users (username,password,role,must_change_password) VALUES (?,?,?,1)",
        ["admin", hash_pw("admin123"), "admin"])

    for name in ["Other"]:
        try:
            db.execute("INSERT INTO distributors (name) VALUES (?)", [name])
        except Exception:
            pass

    db.commit()


def _seed_categories(db):
    """Seed generic IT categories. Replaced by sector-specific categories on onboarding."""
    IT_CATS = [
        ("Laptops", "#1d4ed8", [
            ("CPU",          "cpu",          "text", "e.g. Intel Core i7-1360P", 1),
            ("RAM",          "ram",          "text", "e.g. 16GB DDR5",           1),
            ("Storage",      "storage",      "text", "e.g. 512GB NVMe SSD",      1),
            ("Screen Size",  "screen_size",  "text", 'e.g. 14" FHD',             1),
            ("OS",           "os_type",      "text", "e.g. Windows 11 Pro",      0),
            ("Battery Life", "battery_life", "text", "e.g. 12 hrs",              0),
        ]),
        ("Desktop PCs", "#0369a1", [
            ("CPU",     "cpu",     "text", "e.g. Intel Core i9-13900K", 1),
            ("RAM",     "ram",     "text", "e.g. 32GB DDR5",            1),
            ("Storage", "storage", "text", "e.g. 1TB NVMe SSD",         1),
            ("OS",      "os_type", "text", "e.g. Windows 11 Pro",       0),
        ]),
        ("Servers", "#1e3a5f", [
            ("CPU",             "cpu",     "text", "e.g. 2x Intel Xeon Gold 6248", 1),
            ("RAM",             "ram",     "text", "e.g. 128GB ECC DDR4",          1),
            ("Storage",         "storage", "text", "e.g. 8x1.8TB SAS RAID",        1),
            ("OS / Hypervisor", "os_type", "text", "e.g. VMware ESXi 8.0",         0),
        ]),
        ("Switches", "#0f766e", [
            ("Port Count", "port_count", "number",   "e.g. 24",         1),
            ("Speed",      "throughput", "text",     "e.g. 1Gbps",      1),
            ("PoE Budget", "poe_budget", "text",     "e.g. 195W total", 0),
            ("Has PoE",    "has_poe",    "checkbox", "",                 0),
        ]),
        ("Routers", "#0f766e", [
            ("Throughput",        "throughput",        "text", "e.g. 1Gbps",   1),
            ("Wireless Standard", "wireless_standard", "text", "e.g. Wi-Fi 6", 0),
        ]),
        ("Firewalls", "#b91c1c", [
            ("Throughput", "throughput", "text", "e.g. 1Gbps NGFW", 1),
        ]),
        ("Access Points", "#0891b2", [
            ("Wireless Standard", "wireless_standard", "text",     "e.g. Wi-Fi 6E", 1),
            ("Has PoE",           "has_poe",            "checkbox", "",              0),
        ]),
        ("Cameras", "#7c3aed", [
            ("Resolution", "resolution", "text",     "e.g. 4MP, 8MP, 4K", 1),
            ("Has PoE",    "has_poe",    "checkbox", "",                   0),
        ]),
        ("NVRs / DVRs", "#6d28d9", [
            ("Channel Count",  "port_count", "number", "e.g. 16",      1),
            ("Max Resolution", "resolution", "text",   "e.g. 4K",      1),
            ("Storage",        "storage",    "text",   "e.g. 4TB HDD", 0),
        ]),
        ("Phones / VoIP", "#0369a1", [("Has PoE", "has_poe", "checkbox", "", 0)]),
        ("Mobile Devices", "#0f766e", [
            ("OS",       "os_type",    "text", "e.g. iOS 17, Android 14", 1),
            ("Screen",   "screen_size","text", 'e.g. 6.1"',               0),
            ("Storage",  "storage",    "text", "e.g. 128GB",              0),
            ("IMEI",     "imei",       "text", "15-digit IMEI",           0),
            ("Carrier",  "carrier",    "text", "e.g. Unlocked, Verizon",  0),
        ]),
        ("Monitors", "#374151", [
            ("Screen Size", "screen_size", "text", 'e.g. 27" 4K IPS',    1),
            ("Resolution",  "resolution",  "text", "e.g. 3840x2160",      1),
        ]),
        ("Cabling", "#92400e", [
            ("Cable Type",  "cable_type",     "text", "e.g. Cat6A, Fiber OS2", 1),
            ("Length",      "cable_length",   "text", "e.g. 3ft, 10ft",        1),
            ("Connectors",  "connector_type", "text", "e.g. RJ45, LC/LC",      0),
            ("Gauge / AWG", "cable_gauge",    "text", "e.g. 23AWG",            0),
        ]),
        ("UPS / Power", "#b45309", [
            ("Capacity (VA)", "throughput",   "text", "e.g. 1500VA / 900W",       1),
            ("Runtime",       "battery_life", "text", "e.g. 10 min at full load", 0),
        ]),
        ("Printers / Scanners", "#374151", [
            ("Type", "cable_type", "text", "e.g. Laser, Inkjet, Label", 1),
        ]),
        ("Storage Devices", "#1e3a5f", [
            ("Capacity", "storage", "text", "e.g. 20TB usable", 1),
        ]),
        ("Accessories", "#6b7280", []),
        ("Consumables",  "#9ca3af", []),
    ]
    for cat_name, color, fields in IT_CATS:
        try:
            cid = db.execute(
                "INSERT INTO categories (name,color) VALUES (?,?)",
                [cat_name, color]).lastrowid
            for i, (label, key, ftype, ph, req) in enumerate(fields):
                db.execute(
                    "INSERT INTO category_fields "
                    "(category_id,field_label,field_key,field_type,placeholder,required,sort_order) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [cid, label, key, ftype, ph, req, i])
        except Exception:
            pass
