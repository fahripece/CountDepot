#!/bin/bash
# CountDepot backup script
# Backs up platform.db and all tenant inventory.db files
# Run nightly via cron: 0 2 * * * /path/to/countdepot_v2/scripts/backup.sh

set -e

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
BACKUP_DIR="${STORELAX_BACKUP_DIR:-$PROJECT_DIR/../backups}"
KEEP_DAYS="${STORELAX_BACKUP_KEEP_DAYS:-14}"
S3_BUCKET="${STORELAX_S3_BUCKET:-}"   # optional: set to copy to S3
DATE=$(date +%Y%m%d_%H%M%S)
DEST="$BACKUP_DIR/staging_$DATE"
ARCHIVE="$BACKUP_DIR/countdepot_$DATE.tar.gz"

# ── Run ───────────────────────────────────────────────────────────────────────
echo "[$(date)] Starting backup..."
mkdir -p "$DEST"

# Back up platform registry
if [ -f "$DATA_DIR/platform.db" ]; then
    cp "$DATA_DIR/platform.db" "$DEST/platform.db"
    echo "  ✓ platform.db"
fi

# Back up each tenant DB
TENANT_COUNT=0
for dir in "$DATA_DIR/tenants"/*/; do
    slug=$(basename "$dir")
    if [ -f "$dir/inventory.db" ]; then
        cp "$dir/inventory.db" "$DEST/${slug}_inventory.db"
        TENANT_COUNT=$((TENANT_COUNT + 1))
        echo "  ✓ $slug"
    fi
done

# Compress everything
tar -czf "$ARCHIVE" -C "$BACKUP_DIR" "staging_$DATE"
rm -rf "$DEST"
SIZE=$(du -sh "$ARCHIVE" | cut -f1)
echo "  → $ARCHIVE ($SIZE, $TENANT_COUNT tenant DBs)"

# Upload to S3 if configured
if [ -n "$S3_BUCKET" ]; then
    aws s3 cp "$ARCHIVE" "s3://$S3_BUCKET/countdepot/$(basename "$ARCHIVE")" --quiet
    echo "  ✓ Uploaded to s3://$S3_BUCKET/countdepot/"
fi

# Remove old backups
find "$BACKUP_DIR" -name "countdepot_*.tar.gz" -mtime "+$KEEP_DAYS" -delete
echo "[$(date)] Backup complete."
