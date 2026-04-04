# CountDepot — AI Development Guide

## What this project is

CountDepot is a **multi-tenant SaaS inventory management system** built with Flask and SQLite. It lets businesses track physical assets — IT equipment, medical devices, tools, retail stock — with check-out/check-in, barcode scanning, financial reporting, and audit logging.

The product is live with paying clients. Treat every change as production code.

---

## Architecture in one sentence

One Flask app, one SQLite file per tenant, subdomains for routing, a separate `platform.db` for the tenant registry.

---

## Project structure

```
storelax_v2/
├── run.py                          # Entry point: python run.py
├── config.py                       # All paths and env vars
├── data/
│   ├── platform.db                 # Tenant registry (NOT inventory data)
│   └── tenants/
│       ├── acme/inventory.db       # Acme's isolated DB
│       └── clientb/inventory.db    # Client B's isolated DB
├── app/
│   ├── __init__.py                 # App factory, before_request, blueprints
│   ├── db.py                       # Per-tenant DB connection (get_db, query, execute)
│   ├── platform.py                 # platform.db helpers (tenant registry)
│   ├── tenant.py                   # Subdomain → tenant resolution middleware
│   ├── helpers.py                  # Shared: auth decorators, permissions, audit log
│   ├── schema.py                   # DB schema DDL, migrations, seeding
│   ├── sectors.py                  # 6 sector definitions for onboarding
│   └── blueprints/
│       ├── auth.py                 # /login  /logout
│       ├── main.py                 # All page routes (/, /add-item, /products, etc.)
│       ├── api.py                  # All /api/* routes
│       ├── onboarding.py           # /onboarding  /onboarding/choose
│       └── platform.py             # /_platform/* super-admin panel
└── templates/
    ├── base.html                   # Sidebar nav, shared styles
    ├── inventory.html              # Main item list
    ├── add_item.html               # Add item form with print/scan workflow
    ├── products.html               # Product template management
    ├── onboarding.html             # Sector picker (first login)
    └── platform/
        ├── login.html              # Platform admin login
        ├── dashboard.html          # Tenant management
        └── tenant_new.html         # Create tenant form
```

---

## The tenant isolation model

**This is the most important thing to understand.** Every tenant's data lives in a completely separate SQLite file at `data/tenants/{slug}/inventory.db`. There is no shared database table between tenants. A bug in a query cannot leak data from one tenant to another because the data is in different files.

- `platform.db` — only knows about tenants (slug, name, plan, sector, onboarded, active). Never contains inventory data.
- `data/tenants/{slug}/inventory.db` — all inventory data for that tenant. Isolated.
- `g.tenant_slug` — set by `tenant.py` on every request, used by `db.py` to open the right file.

**Never** call `get_db()` from `app/blueprints/platform.py` — that blueprint operates on `platform.db` only via `get_platform_db()`.

---

## Request lifecycle

1. Request arrives at `clienta.storelax.com/api/items`
2. `before_request` in `app/__init__.py`:
   - If path starts with `/_platform` → skip tenant resolution entirely
   - `resolve_tenant()` reads subdomain `clienta`, looks up in `platform.db`, sets `g.tenant_slug` and `g.tenant`
   - Unknown slug → 404. Suspended tenant → 403.
   - `init_db()` creates tables in `clienta/inventory.db` if first run (no-op otherwise)
   - If tenant not yet onboarded → redirect to `/onboarding`
3. Route handler runs, calls `query()` / `execute()` which opens `clienta/inventory.db`
4. `teardown_appcontext` closes the DB connection

**Local dev**: `localhost` resolves to `DEV_TENANT_SLUG` (default: `"dev"`). Set `DEV_TENANT_SLUG=yourslug` env var to develop against a specific tenant.

---

## Key files explained

### `config.py`
All configuration. Environment variables override defaults.
```python
SECRET_KEY           = os.environ.get("SECRET_KEY", "change-me-in-production")
DEV_TENANT_SLUG      = os.environ.get("DEV_TENANT_SLUG", "dev")
PLATFORM_DB_PATH     = data/platform.db
TENANTS_DIR          = data/tenants/
```

### `app/db.py`
The only way routes touch the DB. Always use these — never open sqlite3 directly in route code.
```python
query(sql, args=(), one=False)   # SELECT — returns list of Rows, or one Row if one=True
execute(sql, args=())            # INSERT/UPDATE/DELETE — returns lastrowid
get_db()                         # Raw connection if needed (rare)
```

### `app/helpers.py`
All shared utilities. Import from here, don't duplicate.
- `login_required` / `perm_required(perm)` / `admin_required` — decorators
- `log_action(action, item_id, item_name, detail, before, after)` — audit log
- `hash_pw(pw)` — HMAC-SHA256, always use this for passwords
- `get_low_stock_alerts()` — returns products below threshold
- `_item_missing_fields(item_dict)` — returns list of missing required fields
- `sync_item_task(item_id, name, missing)` — creates/closes To Do task for incomplete items
- `_parse_date_range(request)` / `_date_filter_sql(col, from, to)` — report date filtering

### `app/schema.py`
- `init_db()` — creates all tables + indexes for a tenant. Idempotent, safe to call on every request.
- `seed_tenant(slug)` — adds default users + distributors. Called once on new tenant creation.
- `_seed_categories(db)` — seeds IT categories. Used as fallback; prefer `seed_sector_categories()`.

### `app/sectors.py`
Six sector definitions (it_msp, healthcare, retail, education, construction, general). Each defines which categories and field profiles get seeded on onboarding.
- `SECTORS` — dict of all sector definitions
- `seed_sector_categories(db, sector_key)` — wipes existing categories, seeds sector-specific ones

### `app/platform.py`
Operates on `platform.db` only. Never call `get_db()` here.
- `get_platform_db()` — raw sqlite3 connection to platform.db
- `init_platform_db()` — creates tenants table + runs migrations
- `get_tenant_by_slug(slug)` — returns tenant Row or None
- `create_tenant(slug, name, plan)` — registers tenant + creates data directory
- `set_tenant_sector(slug, sector)` — marks tenant onboarded

### `app/blueprints/platform.py`
Super-admin panel at `/_platform/`. Protected by `PLATFORM_ADMIN_PASSWORD` env var — completely separate from tenant user accounts. Uses a separate session key `_platform_authed`.

---

## Database schema (per tenant)

Three-tier hierarchy:

```
Category (1) → Products (many) → Items (many)
```

- **categories** — Laptops, Switches, etc. Each has color + custom field definitions in `category_fields`
- **products** — Master template (e.g. "ThinkPad X1 Carbon"). Defines tracking behavior: `require_serial`, `require_vendor_sku`, `require_internal_sku`, `serial_tracked`, `qty_tracked`, `require_scan_checkout`, `print_scan_label`
- **items** — Physical unit. Has `serial`, `sku`, `internal_sku`, `shelf`, `checked_out`, `sold`, pricing, etc.
- **audit_log** — Immutable. Every action written here. Never update or delete rows.
- **tasks** — To Do items. Linked to items via `item_id`. Auto-created when item is incomplete, auto-closed when complete.

### Identifier rules (per product)
Exactly one of these must be set on every product:
- `require_serial=1` — serial # field shown and required on add
- `require_vendor_sku=1` — vendor SKU field shown and required on add
- `require_internal_sku=1` — system auto-generates `LAPT-20250319-001` style SKU silently

`require_vendor_sku` and `require_internal_sku` are mutually exclusive. `require_serial` can combine with either.

`print_scan_label` is an independent flag — when set, the add-item flow shows a print-then-scan confirmation step.

---

## Auth & permissions

### Tenant users
- Stored in each tenant's `users` table
- `role`: `admin` or `worker`
- `permissions`: comma-separated string of permission keys
- Admin always has all permissions regardless of the `permissions` column
- Permission keys: `view_inventory`, `view_dashboard`, `view_audit`, `checkout_checkin`, `write_items`, `qty_adjust`, `sell_items`, `delete_items`, `import_export`

### Platform admin
- Single password via `PLATFORM_ADMIN_PASSWORD` env var
- Session key `_platform_authed` — separate from tenant sessions
- Access to `/_platform/*` only

### Session keys set on login
```python
session["user_id"]              # int
session["username"]             # str
session["role"]                 # "admin" | "worker"
session["permissions"]          # comma-separated perm keys
session["must_change_password"] # bool — if True, /change-password intercept fires
session["expires_at"]           # ISO datetime string, 8 hours from login
```

Sessions expire after **8 hours**. `check_session_expiry()` in `auth.py` validates this on every request. Expired sessions are cleared and the user is redirected to login.

New accounts always have `must_change_password=1`. On next login the user is intercepted at `/change-password` before they can do anything.

---

## Adding a new route

### Page route (returns HTML)
Add to `app/blueprints/main.py`:
```python
@bp.route("/my-page")
@login_required
def my_page():
    data = query("SELECT * FROM items WHERE active=1")
    return render_template("my_page.html", items=data)
```

### API route (returns JSON)
Add to `app/blueprints/api.py`:
```python
@bp.route("/api/my-endpoint", methods=["POST"])
@login_required
@perm_required("write_items")
def api_my_endpoint():
    d = request.json
    # validate...
    result = execute("INSERT INTO ...", [...])
    log_action("MY_ACTION", item_id=result, item_name=d.get("name"))
    return jsonify({"ok": True, "id": result})
```

**Always** call `log_action()` for any write that affects items, products, or categories.

---

## Platform admin: creating a tenant

```python
from app.platform import create_tenant
from app.blueprints.platform import _bootstrap_tenant_db

create_tenant("acme", "Acme Corp", "standard")       # registers in platform.db
_bootstrap_tenant_db("acme", "their-admin-password") # creates inventory.db with schema + seed data
```

The tenant will hit `/onboarding` on first login and pick their sector, which wipes the default IT categories and seeds their sector-specific ones.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | `change-me-in-production` | Flask session key. **Must change in production.** |
| `DEV_TENANT_SLUG` | `dev` | Tenant slug used when running on localhost |
| `PLATFORM_ADMIN_PASSWORD` | `platform-change-me` | Super-admin panel password. **Must change in production.** |

---

## Running locally

```bash
# Install deps
pip install flask

# Run
python run.py
# → http://localhost:5000

# The app resolves localhost to the "dev" tenant
# Make sure a dev tenant exists in platform.db first:
python -c "
from app import create_app
from app.platform import create_tenant
from app.blueprints.platform import _bootstrap_tenant_db
app = create_app()
with app.app_context():
    create_tenant('dev', 'Dev Tenant')
    _bootstrap_tenant_db('dev', 'admin123')
"
```

Default login: `admin` / `admin123`

---

## What's not built yet (planned)

These are known gaps, in rough priority order:

### Must-have for production
- **`gunicorn.conf.py`** — ✅ done
- **`requirements.txt`** — ✅ done
- **`/_health`** endpoint — ✅ done
- **Session expiry** — ✅ done (8 hours, set via SESSION_LIFETIME_HOURS in auth.py)
- **Force password change on first login** — ✅ done (must_change_password=1 on all new accounts)
- **HTTPS enforcement** — must be done at proxy level (nginx/Caddy), not in app code

### Auth improvements
- **Email field on users** — needed before password reset is possible
- **Password reset flow** — no self-service password reset exists
- **Tenant invite system** — admin can invite users by email instead of sharing credentials

### Growth features
- **Public `/signup` page** — self-serve tenant creation (currently only platform admin can create tenants)
- **Per-tenant branding** — company name/logo in the UI
- **Billing hooks** — track plan, flag overdue accounts, usage limits

### Reliability
- **Automated backups** — nightly cron to copy each tenant's `.db` file to S3 or another location
- **Structured error logging** — exceptions go to stderr only right now

### Product features
- **Item photos** — no image attachment support
- **Email/SMS notifications** — low stock alerts don't actually notify anyone
- **Bulk import improvements** — template download, dry-run preview before committing
- **Dark mode**

---

## Things that must never happen

- **Never share a DB connection between tenants.** `get_db()` always opens the current tenant's file via `g.tenant_slug`.
- **Never call `get_db()` from `app/blueprints/platform.py`** — that blueprint uses `get_platform_db()` exclusively.
- **Never write to `audit_log` from the frontend** — only `log_action()` in Python may write audit entries.
- **Never delete or update `audit_log` rows** — it is append-only by design.
- **Never skip `log_action()`** on item/product/category writes — every change must be traceable.
- **Never trust user-supplied `tenant_id`** — tenant is always resolved from the subdomain, never from request data.
- **Never hardcode passwords** — use env vars. The defaults (`change-me-in-production`, `platform-change-me`) are for local dev only and will warn loudly if used.
