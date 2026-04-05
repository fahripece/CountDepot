#!/bin/bash
# CountDepot backup wrapper — delegates to backup.py (WAL-safe SQLite backup API).
# Schedule nightly via cron or the systemd timer in DEPLOYMENT.md.
#
# Cron example (runs at 2 AM):
#   0 2 * * * /home/countdepot/countdepot/scripts/backup.sh >> /home/countdepot/backups/backup.log 2>&1

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$(dirname "$SCRIPT_DIR")/venv/bin/python3}"

# Fall back to system python3 if venv not found
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

exec "$PYTHON" "$SCRIPT_DIR/backup.py" "$@"
