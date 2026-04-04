# CountDepot — Optiplex Setup Guide
# Cloudflare Tunnel (no open ports, no static IP needed)

**What you need before starting:**
- The Optiplex running Ubuntu 22.04 (or you're about to install it)
- countdepot.com already on Cloudflare (you have this)
- About 1 hour

---

## Part 1 — Install Ubuntu on the Optiplex

If Ubuntu isn't on the machine yet:

1. Download Ubuntu 22.04 LTS Server ISO: https://ubuntu.com/download/server
2. Flash to a USB stick with Balena Etcher (free): https://etcher.balena.io
3. Boot the Optiplex from USB (spam F12 on boot to get boot menu)
4. Install with defaults — when it asks about disk, use the whole drive
5. Set username: `countdepot`, pick a strong password, enable SSH when asked
6. Let it finish, remove the USB, reboot

**After reboot, find the machine's local IP:**
```bash
ip addr show | grep "inet " | grep -v 127
# You'll see something like 192.168.1.105 — note this
```

From this point on you can do everything over SSH from your main computer:
```bash
ssh countdepot@192.168.1.105
```

---

## Part 2 — System setup

```bash
# Update everything
sudo apt update && sudo apt upgrade -y

# Install Python and dependencies
sudo apt install -y python3 python3-pip python3-venv git

# Keep the machine's clock accurate (important for SSL)
sudo apt install -y ntp
sudo systemctl enable ntp
```

---

## Part 3 — Install the app

```bash
# You should be logged in as the countdepot user
cd /home/countdepot

# Upload the countdepot.zip from your main computer:
# On your main computer (not the server), run:
#   scp countdepot.zip countdepot@192.168.1.105:/home/countdepot/
# Then back on the server:

sudo apt install -y unzip
unzip countdepot.zip
cd countdepot

# Create Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Part 4 — Environment variables

```bash
# Generate a SECRET_KEY first:
python3 -c "import secrets; print(secrets.token_hex(32))"
# Copy that output — you'll paste it below

nano /home/countdepot/countdepot/.env
```

Paste this, filling in the two REPLACE lines:

```
SECRET_KEY=PASTE_YOUR_GENERATED_KEY_HERE
PLATFORM_ADMIN_PASSWORD=CHOOSE_A_STRONG_PASSWORD

APP_DOMAIN=countdepot.com
DEV_TENANT_SLUG=dev
WEB_CONCURRENCY=4
LOG_LEVEL=info

# Email — fill in after you set up Postmark (can do this later)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=noreply@countdepot.com
SMTP_USE_TLS=true

SIGNUP_ENABLED=true
```

Save: Ctrl+X → Y → Enter

```bash
chmod 600 /home/countdepot/countdepot/.env
```

---

## Part 5 — Systemd service (auto-start on boot)

```bash
sudo nano /etc/systemd/system/countdepot.service
```

Paste exactly:

```ini
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
```

Save: Ctrl+X → Y → Enter

```bash
sudo systemctl daemon-reload
sudo systemctl enable countdepot
sudo systemctl start countdepot

# Check it's running
sudo systemctl status countdepot

# Should say: Active: active (running)
# If it failed, check: sudo journalctl -u countdepot -n 30
```

Test it's actually working locally:
```bash
curl http://localhost:5000/_health
# Should return: {"ok": true, "service": "countdepot"}
```

---

## Part 6 — Cloudflare Tunnel

This is what replaces Caddy + open ports. Cloudflare punches a secure tunnel from their network to your machine. Your domain works, HTTPS works, wildcard subdomains work — no port forwarding, no static IP needed.

### Step 6a — Install cloudflared

```bash
# Download and install the Cloudflare tunnel daemon
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb

# Verify
cloudflared --version
```

### Step 6b — Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This prints a URL. Open it on your main computer, log in to Cloudflare, and select **countdepot.com**. It will download a certificate to the Optiplex automatically. You'll see:

```
You have successfully logged in.
If you wish to copy your credentials to a server, they have been saved to:
/home/countdepot/.cloudflared/cert.pem
```

### Step 6c — Create the tunnel

```bash
cloudflared tunnel create countdepot
```

This creates a tunnel and prints something like:
```
Created tunnel countdepot with id abc123def456-...
```

**Copy the tunnel ID** — you need it in the next step.

### Step 6d — Configure the tunnel

```bash
mkdir -p ~/.cloudflared
nano ~/.cloudflared/config.yml
```

Paste this, replacing `YOUR-TUNNEL-ID` with the ID from the previous step:

```yaml
tunnel: YOUR-TUNNEL-ID
credentials-file: /home/countdepot/.cloudflared/YOUR-TUNNEL-ID.json

ingress:
  - hostname: "*.countdepot.com"
    service: http://localhost:5000
  - hostname: countdepot.com
    service: http://localhost:5000
  - service: http_status:404
```

Save: Ctrl+X → Y → Enter

### Step 6e — Point DNS to the tunnel

```bash
# This creates a CNAME record in Cloudflare automatically
# Replace YOUR-TUNNEL-ID with your actual tunnel ID
cloudflared tunnel route dns countdepot "*.countdepot.com"
cloudflared tunnel route dns countdepot countdepot.com
```

You should see:
```
Added CNAME *.countdepot.com which will route to this tunnel
Added CNAME countdepot.com which will route to this tunnel
```

**Verify in Cloudflare dashboard:**
- Go to countdepot.com → DNS
- You should see two CNAME records pointing to `YOUR-TUNNEL-ID.cfargotunnel.com`
- Make sure both are **Proxied** (orange cloud) — this is what gives you HTTPS

### Step 6f — Run the tunnel as a service (auto-starts on boot)

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
sudo systemctl status cloudflared
# Should say: Active: active (running)
```

---

## Part 7 — Test everything

```bash
# On the Optiplex, check both services are running:
sudo systemctl status countdepot
sudo systemctl status cloudflared
```

Now from your phone or another computer (not on the same network — use mobile data to really test it):

- Open `https://countdepot.com/_health` → should return `{"ok": true}`
- Open `https://countdepot.com/_platform/login` → should show the login page
- Log in with your `PLATFORM_ADMIN_PASSWORD`

Create a test tenant:
- Slug: `test`
- Company: Test Company
- Password: anything

Open `https://test.countdepot.com` → login → password change → sector picker → inventory

**If that works, you're live.**

---

## Part 8 — Backups

Your SQLite databases live at `/home/countdepot/countdepot/data/`. Back these up.

```bash
mkdir -p /home/countdepot/backups

cat > /home/countdepot/backup.sh << 'EOF'
#!/bin/bash
set -e
BACKUP_DIR="/home/countdepot/backups"
DATA_DIR="/home/countdepot/countdepot/data"
DATE=$(date +%Y%m%d_%H%M%S)
DEST="$BACKUP_DIR/$DATE"

mkdir -p "$DEST"
cp "$DATA_DIR/platform.db" "$DEST/platform.db"
for dir in "$DATA_DIR/tenants"/*/; do
    slug=$(basename "$dir")
    [ -f "$dir/inventory.db" ] && cp "$dir/inventory.db" "$DEST/${slug}_inventory.db"
done
tar -czf "$BACKUP_DIR/countdepot_$DATE.tar.gz" -C "$BACKUP_DIR" "$DATE"
rm -rf "$DEST"
find "$BACKUP_DIR" -name "countdepot_*.tar.gz" -mtime +14 -delete
echo "Backup done: countdepot_$DATE.tar.gz"
EOF

chmod +x /home/countdepot/backup.sh
```

Schedule it:
```bash
crontab -e
# Add this line:
0 2 * * * /home/countdepot/backup.sh >> /home/countdepot/backups/backup.log 2>&1
```

**Critical: also back up off the machine.** An external hard drive plugged into the Optiplex works. Or a cheap Backblaze B2 bucket ($0.006/GB — basically free for SQLite files). If the Optiplex dies and backups are on the same machine, you lose everything.

Quick offsite option — Backblaze B2:
```bash
pip install b2
b2 authorize-account YOUR_KEY_ID YOUR_APP_KEY
# Add to backup.sh after the tar line:
# b2 upload-file your-bucket-name "$BACKUP_DIR/countdepot_$DATE.tar.gz" "countdepot_$DATE.tar.gz"
```

---

## Part 9 — Keep the machine running

```bash
# Prevent the machine from sleeping
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Optional: if it has a monitor you can disconnect — disable screen blanking
sudo apt install -y x11-xserver-utils 2>/dev/null; true
```

If the Optiplex is in an office, plug it into a **UPS (battery backup)**. A basic APC BE600M1 (~$60) gives you 10–15 minutes of runtime during a power flicker — enough to survive most outages without a hard shutdown.

---

## Day-to-day operations

**Check everything is running:**
```bash
sudo systemctl status countdepot cloudflared
```

**View live logs:**
```bash
sudo journalctl -u countdepot -f
```

**Deploy an update:**
```bash
cd /home/countdepot/countdepot
# Upload new files here (scp from your main computer)
sudo systemctl restart countdepot
sudo systemctl status countdepot
```

**Add a client:**
Go to `https://countdepot.com/_platform/login` → New Tenant

**Check disk space:**
```bash
df -h /
du -sh /home/countdepot/countdepot/data/tenants/*/
```

---

## Troubleshooting

**App not responding:**
```bash
sudo systemctl status countdepot
sudo journalctl -u countdepot -n 50
# Restart it:
sudo systemctl restart countdepot
```

**Tunnel not working:**
```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 50
sudo systemctl restart cloudflared
```

**Subdomain not resolving:**
- Check Cloudflare DNS tab — both CNAMEs should be there and proxied (orange cloud)
- Wait 2 minutes after any DNS change

**Machine rebooted and app didn't come back:**
```bash
# Both services should auto-start, but check:
sudo systemctl enable countdepot cloudflared
sudo systemctl start countdepot cloudflared
```

**Forgot platform admin password:**
```bash
nano /home/countdepot/countdepot/.env
# Change PLATFORM_ADMIN_PASSWORD
sudo systemctl restart countdepot
```

---

## When you're ready to move to Hetzner

When you have 5+ active paying clients and want proper redundancy:

1. Rent the CAX21 on Hetzner
2. Copy `/home/countdepot/countdepot/data/` to the VPS — that's all the data
3. Follow the original DEPLOYMENT.md on the Hetzner server
4. Move the Cloudflare tunnel (or switch to Caddy on the VPS)
5. Keep the Optiplex as your dev/staging machine

The migration is just copying one folder. SQLite makes this trivially easy.
