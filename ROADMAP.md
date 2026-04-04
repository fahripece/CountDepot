# CountDepot — Product Roadmap

## What's fully built ✓

### Infrastructure
- Multi-tenant architecture — one SQLite DB per tenant, zero cross-tenant leakage possible
- Subdomain routing — `acme.yourdomain.com` resolves to Acme's isolated database
- Platform super-admin panel at `/_platform/` — create, suspend, reactivate tenants
- Session expiry — 8-hour timeout, automatic cleanup
- Force password change on first login — all new accounts flagged
- Health check endpoint — `/_health` for load balancer monitoring
- Gunicorn production config — multi-worker, proper logging
- Backup script — `scripts/backup.sh`, optional S3 upload
- CLI tools — `scripts/create_tenant.py`, `scripts/list_tenants.py`

### Onboarding
- Sector picker on first login — 6 sectors: IT/MSP, Healthcare, Retail, Education, Construction, General
- Sector-specific category seeding — categories and field profiles tailored per sector
- Forced onboarding gate — no access to inventory until sector is chosen

### Core inventory
- 3-tier hierarchy: Category → Product → Item
- Product identifier requirements — require serial, vendor SKU, or auto-generate internal SKU
- Check-out / check-in with job reference
- Barcode scanner support (USB HID, F2 to open)
- Print + scan label workflow for new items
- Quantity tracking for consumables
- Item modifications log with component spawning
- Item lineage (parent/child relationships)
- eBay listing status tracking
- Audit log — immutable, append-only

### Financial
- Stock value calculation
- Revenue and profit tracking
- Tax rate per item
- Financial, inventory, and activity reports with date filtering

### Admin
- Role-based permissions (9 granular permission keys)
- Admin and worker roles
- Per-user permission customization
- Companies and distributors management
- Category field profiles (custom fields per category)
- Excel import / CSV + Excel export
- To Do task system with item linking

---

## High priority — build next

### 1. Public signup page `/signup`
Right now only the platform admin can create tenants. A self-serve signup page would let clients onboard themselves without you being involved.

What it needs:
- Public `/signup` form: company name, sector picker, admin email, password
- Creates tenant + bootstraps DB automatically
- Sends welcome email with their URL
- Optional: payment integration before tenant is activated

**Estimated effort:** 1–2 days

---

### 2. Email on user accounts + password reset
Users have an `email` column but no way to set it or use it yet.

What it needs:
- Email field in the tenant admin panel (add/edit user)
- `/forgot-password` page — enter email, get a reset link
- Time-limited reset tokens (store in DB, expire after 1 hour)
- Email sending via SMTP (use `smtplib` or `Flask-Mail`)
- `/reset-password/<token>` page to set the new password

**Why it matters:** Without this, forgotten passwords require you to manually reset them via the admin panel for every client.

**Estimated effort:** 1 day

---

### 3. Automated nightly email digest (optional per tenant)
A daily summary email to tenant admins: how many items, any low stock alerts, recent activity.

What it needs:
- `SMTP_*` env vars for email config
- A cron job or scheduled task that runs `scripts/send_digests.py`
- Simple HTML email template
- Per-tenant opt-in setting in platform panel

**Estimated effort:** 1 day

---

### 4. Per-tenant branding
Clients want their own company name visible in the UI, not "Storelax".

What it needs:
- `tenant_name` shown in the sidebar instead of "Storelax"
- Optional logo URL in the tenants table
- Base template reads from `g.tenant` which is already available on every request

**Estimated effort:** 2 hours

---

### 5. Tenant invite system
Instead of sharing a username/password, admin can invite users by email.

What it needs:
- Invite table in tenant DB (email, token, expires_at, role)
- `/invite` API endpoint — admin sends invite
- `/accept-invite/<token>` page — user sets their username + password
- Email with the invite link

**Estimated effort:** 1 day

---

## Medium priority

### 6. Item photos
No way to attach an image to an item currently.

What it needs:
- File upload endpoint `/api/item/upload-photo`
- Store files in `data/tenants/{slug}/uploads/`
- `photo_url` column on items table
- Display in item detail view

---

### 7. Bulk import improvements
Current Excel import is brittle and gives no preview.

What it needs:
- Template download — a pre-formatted Excel file they can fill in
- Dry-run mode — validate without committing, return a list of what would be imported
- Better error messages pointing to specific rows and columns

---

### 8. Email/SMS low stock notifications
Low stock alerts currently only show in the UI. No proactive notification.

What it needs:
- Per-product notification threshold already exists
- Daily check (cron job) that emails admin if any products are below threshold
- Optionally: Twilio SMS for urgent alerts

---

### 9. Billing / plan enforcement
Plans (standard, pro, healthcare) are tracked but not enforced.

What it needs:
- Per-plan limits: max users, max items, storage cap
- `check_plan_limits()` helper called before creates
- Platform panel shows usage vs limits per tenant
- Optional: Stripe integration for payment

---

### 10. Dark mode
The app has no dark mode. Power users working all day will ask for it.

What it needs:
- CSS variables for all colors in `base.html`
- `prefers-color-scheme: dark` media query or a manual toggle
- Toggle stored in localStorage

---

## Lower priority / nice to have

### 11. API keys for external integrations
Let clients integrate with their own tools (e.g. auto-create items from a purchase order system).

What it needs:
- `api_keys` table per tenant (key hash, label, permissions, last_used)
- API key authentication as alternative to session auth
- Rate limiting per key

---

### 12. Webhooks
Push events to client systems when something happens (item checked out, low stock, item sold).

What it needs:
- `webhooks` table (url, events, secret)
- Fire HTTP POST to registered URLs on key events
- Retry logic for failed deliveries

---

### 13. Mobile app / PWA
The web app works on mobile but isn't optimized for it.

What it needs:
- `manifest.json` + service worker for PWA install
- Mobile-first UI for the check-in/out flow (most common mobile action)
- Or: a React Native app that hits the existing `/api/*` endpoints

---

### 14. Multi-location support
Some clients have multiple warehouses or offices. Currently shelf is a free-text field.

What it needs:
- `locations` table per tenant
- Items assigned to a location
- Filter by location on the inventory page

---

## Known bugs / tech debt

- `init_db()` runs on every request (no-op if schema exists, but wastes a tiny bit of time) — should only run once on startup or first request per tenant
- `_login_attempts` rate limiter is in-memory — resets on gunicorn worker restart, doesn't work across multiple workers
- `hash_pw()` uses HMAC-SHA256 with a static salt — should migrate to bcrypt or Argon2 for new accounts
- `sell_checkout` page route exists in old `app.py` but wasn't ported to the new blueprints (it redirected to the checkout page anyway)
- Excel import ignores `must_change_password` — imported users have no password change enforced
