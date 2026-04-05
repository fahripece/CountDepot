# CountDepot — Deployment & Operations Guide

Everything you need to go from zero to running in production. Code handles itself — this is only the infrastructure and configuration layer.

**Time to first working tenant: ~45 minutes**

---

## Prerequisites

- A Linux VPS — Ubuntu 22.04, at least 2 GB RAM, 20 GB disk
- A domain name you control (e.g. `countdepot.com` or your own)
- SSH access to the server

---

## Step 1 — DNS

Add two DNS records pointing to your server's IP:

| Type | Name | Value |
|------|------|-------|
| A | `@` | Your server IP |
| A | `*` | Your server IP |

The wildcard `*` means every subdomain (`acme.countdepot.com`, `clientb.countdepot.com`) routes to your server automatically.

**Cloudflare users:** Set the wildcard record to **DNS Only** (grey cloud) until SSL is working, then you can enable proxy.

Wait 5–30 min for DNS to propagate.

---

## Step 2 — Server setup

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git

# Create non-root user to run the app
useradd -m -s /bin/bash countdepot
```

---

## Step 3 — Install Caddy (SSL + reverse proxy)

Caddy automatically gets and renews SSL certificates — no certbot needed.

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install caddy -y
```

### Option A — Wildcard SSL (recommended, requires Cloudflare)

Wildcard certs need a DNS challenge. If your domain is on Cloudflare:

1. Get a Cloudflare API token: My Profile → API Tokens → Create Token → "Edit zone DNS"
2. Download a Caddy binary with the Cloudflare plugin from https://caddyserver.com/download (select **caddy-dns/cloudflare**)
3. Replace `/usr/bin/caddy` with the downloaded binary

```bash
cat > /etc/caddy/Caddyfile << 'EOF'
*.countdepot.com, countdepot.com {
    tls {
        dns cloudflare YOUR_CF_API_TOKEN
    }
    reverse_proxy localhost:5000
}
EOF
```

### Option B — Per-subdomain SSL (simpler, works with any DNS)

Manually add each subdomain to the Caddyfile as you create tenants:

```
acme.countdepot.com {
    reverse_proxy localhost:5000
}
clientb.countdepot.com {
    reverse_proxy localhost:5000
}
```

```bash
systemctl enable caddy && systemctl start caddy
```

---

## Step 4 — Deploy the app

```bash
su - countdepot
cd /home/storelax

# Upload countdepot/ here via scp, rsync, git, or any method
# e.g.: scp -r countdepot/ storelax@yourserver.com:/home/countdepot/

cd countdepot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 5 — Environment variables

```bash
cat > /home/countdepot/countdepot/.env << 'EOF'
# REQUIRED — generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=REPLACE_WITH_LONG_RANDOM_STRING

# REQUIRED — password for /_platform/ super-admin panel
PLATFORM_ADMIN_PASSWORD=REPLACE_WITH_SECURE_PASSWORD

# Your domain (no https://, no trailing slash)
APP_DOMAIN=countdepot.com

# Dev mode: which tenant slug resolves on localhost (no subdomain)
DEV_TENANT_SLUG=dev

# Workers (2 × CPU cores + 1 is a good starting point)
WEB_CONCURRENCY=4
LOG_LEVEL=info

# Optional: write rotating log files in addition to journald
# LOG_FILE=/home/countdepot/logs/countdepot.log

# ── Email (optional but strongly recommended) ──────────────────────────────
# Leave SMTP_HOST empty and emails are printed to logs instead of sent
# Works with any SMTP provider: Postmark, Mailgun, SendGrid, Gmail, etc.
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=your-api-key-here
SMTP_PASSWORD=your-api-key-here
SMTP_FROM=noreply@countdepot.com
SMTP_USE_TLS=true

# ── Self-serve signup ──────────────────────────────────────────────────────
# Set to false to disable /signup — all tenants must be created via /_platform/
SIGNUP_ENABLED=true
EOF

chmod 600 /home/countdepot/countdepot/.env
```

**Generate SECRET_KEY:**
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Step 6 — Systemd service

```bash
exit   # back to root

cat > /etc/systemd/system/countdepot.service << 'EOF'
[Unit]
Description=CountDepot Inventory SaaS
After=network.target

[Service]
Type=simple
User=countdepot
WorkingDirectory=/home/countdepot/countdepot
EnvironmentFile=/home/countdepot/countdepot/.env
ExecStart=/home/countdepot/countdepot/venv/bin/gunicorn -c gunicorn.conf.py run:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable countdepot
systemctl start countdepot
systemctl status countdepot
```

---

## Step 7 — Create your first tenant

Go to `https://countdepot.com/_platform/login` → log in with `PLATFORM_ADMIN_PASSWORD`.

Click **+ New Tenant**:
- **Slug** → becomes the subdomain (`acme` → `acme.countdepot.com`)
- **Company name** → displayed inside the app
- **Admin password** → temporary; they'll be forced to change it on first login

The tenant's database is created instantly.

**Tell your client:**
- URL: `https://[slug].countdepot.com`
- Username: `admin`
- Temp password: whatever you set
- They'll change the password on first login, then pick their business type (IT, Healthcare, Retail, etc.)

### Or let clients sign up themselves

If `SIGNUP_ENABLED=true`, clients can go to `https://countdepot.com/signup` and create their own workspace. They'll get a welcome email with their URL and credentials.

---

## Step 8 — Set up backups (critical)

Without backups, one failed disk loses all client data.

The backup script (`scripts/backup.py`) uses SQLite's native backup API — safe to run while the app is live, even with WAL mode active. It also runs a WAL checkpoint after each backup to keep WAL files small.

```bash
mkdir -p /home/countdepot/backups
chmod +x /home/countdepot/countdepot/scripts/backup.sh
```

### Option A — systemd timer (recommended)

Systemd timers log to journald and restart automatically on failure.

```bash
# Service that runs the backup
cat > /etc/systemd/system/countdepot-backup.service << 'EOF'
[Unit]
Description=CountDepot nightly backup
After=network.target

[Service]
Type=oneshot
User=countdepot
WorkingDirectory=/home/countdepot/countdepot
Environment=COUNTDEPOT_BACKUP_DIR=/home/countdepot/backups
Environment=COUNTDEPOT_BACKUP_KEEP_DAYS=14
ExecStart=/home/countdepot/countdepot/scripts/backup.sh
StandardOutput=journal
StandardError=journal
EOF

# Timer that fires it at 2 AM every day
cat > /etc/systemd/system/countdepot-backup.timer << 'EOF'
[Unit]
Description=Run CountDepot backup nightly at 2 AM

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now countdepot-backup.timer
systemctl list-timers countdepot-backup.timer
```

### Option B — cron

```bash
crontab -u countdepot -e
# Add:
0 2 * * * COUNTDEPOT_BACKUP_DIR=/home/countdepot/backups /home/countdepot/countdepot/scripts/backup.sh >> /home/countdepot/backups/backup.log 2>&1
```

### Test it now

```bash
su - countdepot -c "COUNTDEPOT_BACKUP_DIR=/home/countdepot/backups /home/countdepot/countdepot/scripts/backup.sh"
ls -lh /home/countdepot/backups/
```

### Check backup logs

```bash
# systemd timer logs
journalctl -u countdepot-backup.service --since today

# Trigger manually
systemctl start countdepot-backup.service
```

---

## Step 9 — Verify checklist

- [ ] `https://countdepot.com/_platform/login` → can log in
- [ ] `https://countdepot.com/_health` → returns `{"ok": true}`
- [ ] Create test tenant slug `test`
- [ ] `https://test.countdepot.com` → login page loads
- [ ] Login with `admin` + temp password → redirected to password change
- [ ] Set new password → onboarding sector picker appears
- [ ] Pick a sector → inventory page loads with correct categories
- [ ] Suspend test tenant from platform panel → `https://test.countdepot.com` shows 403
- [ ] Reactivate → works again
- [ ] Backup script ran and produced a `.tar.gz`

---

## Email providers (pick one)

Email is optional — without it, reset links and welcome emails print to logs. But for a real product you want email working.

### Postmark (recommended — great deliverability, free tier: 100 emails/month)
1. Sign up at https://postmarkapp.com
2. Create a Server → get the API key
3. Set `SMTP_HOST=smtp.postmarkapp.com`, `SMTP_PORT=587`, `SMTP_USER=<api-key>`, `SMTP_PASSWORD=<api-key>`
4. Verify your sending domain in their dashboard

### Mailgun
- `SMTP_HOST=smtp.mailgun.org`, port `587`
- Use the SMTP credentials from your Mailgun dashboard

### Gmail (for testing only — not reliable for bulk)
- `SMTP_HOST=smtp.gmail.com`, port `587`
- Use an App Password (not your real password): Google Account → Security → App passwords

---

## Updating the app

```bash
su - countdepot
cd /home/countdepot/countdepot

# Upload new files here (scp / rsync / git pull)

# Restart — DB migrations run automatically on startup
systemctl restart countdepot
systemctl status countdepot
journalctl -u countdepot --since "1 minute ago"
```

---

## CLI tools

Useful admin commands you can run from the server:

```bash
source /home/countdepot/countdepot/venv/bin/activate
cd /home/countdepot/countdepot

# List all tenants with stats
python scripts/list_tenants.py

# Create a tenant from the command line (bypasses the web UI)
python scripts/create_tenant.py --slug acme --name "Acme Corp" --password admin123
```

---

## Monitoring

```bash
# Live logs
journalctl -u countdepot -f

# Today's logs
journalctl -u countdepot --since today

# Health check
curl https://countdepot.com/_health

# Disk usage per tenant
du -sh /home/countdepot/countdepot/data/tenants/*/
```

---

## Troubleshooting

**App won't start:**
```bash
journalctl -u countdepot -n 50
# Common: wrong .env path, missing dependency, port in use
```

**Can't reach the site:**
```bash
systemctl status caddy
curl http://localhost:5000/_health   # is app running locally?
```

**Subdomain not resolving:**
```bash
dig acme.countdepot.com   # should return your server IP
# If using Option B SSL, you also need to add the subdomain to Caddyfile and restart Caddy
```

**Forgot platform admin password:**
```bash
nano /home/countdepot/countdepot/.env   # change PLATFORM_ADMIN_PASSWORD
systemctl restart countdepot
```

**User can't reset password (no email):**
- Check `SMTP_HOST` is set in `.env`
- Check logs: `journalctl -u countdepot | grep MAIL`
- If you see `[MAIL no-SMTP]` lines, SMTP is not configured

**Restore a tenant from backup:**
```bash
systemctl stop countdepot

cd /home/countdepot/backups
tar -xzf storelax_YYYYMMDD_HHMMSS.tar.gz

# Restore one tenant
cp YYYYMMDD_HHMMSS/acme_inventory.db /home/countdepot/countdepot/data/tenants/acme/inventory.db

systemctl start countdepot
```

---

## Security checklist (before real client data)

- [ ] `SECRET_KEY` is a random 64-char string
- [ ] `PLATFORM_ADMIN_PASSWORD` is strong
- [ ] `.env` is `chmod 600` (owner-read only)
- [ ] SSH password auth disabled — use keys only
- [ ] Firewall: `ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw enable`
- [ ] Auto security updates: `apt install unattended-upgrades -y`
- [ ] Backups running and tested
- [ ] `/_platform/` URL not publicly advertised

---

## What you do NOT need to do manually

- Run any SQL or create databases — handled on first request
- Renew SSL — Caddy does it automatically
- Add DB columns after updates — migrations run on startup
- Create category presets — happens during onboarding
