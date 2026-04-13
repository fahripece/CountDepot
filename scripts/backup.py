#!/usr/bin/env python3
"""
CountDepot backup script — uses SQLite's native backup API for WAL-safe hot backups.

Usage:
    python scripts/backup.py [--data-dir PATH] [--backup-dir PATH] [--keep-days N]

Environment variables (override defaults):
    COUNTDEPOT_DATA_DIR        Path to the data/ directory  (default: ../data)
    COUNTDEPOT_BACKUP_DIR      Where to store backups        (default: ../../backups)
    COUNTDEPOT_BACKUP_KEEP_DAYS  How many days to retain     (default: 14)

Schedule nightly via cron:
    0 2 * * * /home/countdepot/countdepot/venv/bin/python \
              /home/countdepot/countdepot/scripts/backup.py \
              >> /home/countdepot/backups/backup.log 2>&1

Or use the systemd timer in DEPLOYMENT.md.
"""

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

# Optional S3 upload — only needed when S3_BACKUP_BUCKET is set
try:
    import boto3
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backup")

SCRIPT_DIR  = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent


def _default(env_key, fallback):
    return os.environ.get(env_key) or fallback


def sqlite_backup(src_path: Path, dest_path: Path) -> int:
    """Hot-copy a SQLite database using the backup API.

    Safe even when the source DB has an active WAL — creates a fully consistent
    snapshot without ever locking out writers for more than a page at a time.

    Returns the page count of the destination DB, or -1 on error.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(str(src_path)) as src, \
             sqlite3.connect(str(dest_path)) as dst:
            src.backup(dst, pages=100)          # copy 100 pages at a time
        return dest_path.stat().st_size
    except Exception as exc:
        log.error(f"  ✗ backup failed for {src_path.name}: {exc}")
        return -1


def wal_checkpoint(db_path: Path) -> None:
    """Run a TRUNCATE checkpoint on a SQLite DB to keep WAL files small.

    TRUNCATE resets the WAL file to zero bytes after checkpointing — the safest
    way to prevent unbounded WAL growth. No-op if the DB has no WAL.
    """
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        log.warning(f"  WAL checkpoint failed for {db_path.name}: {exc}")


def run_backup(data_dir: Path, backup_dir: Path, keep_days: int) -> bool:
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_dir = backup_dir / f"staging_{timestamp}"
    archive     = backup_dir / f"countdepot_{timestamp}.tar.gz"

    log.info(f"Starting backup — data={data_dir}, dest={backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    ok = True

    # ── platform.db ──────────────────────────────────────────────────────────
    platform_src = data_dir / "platform.db"
    if platform_src.exists():
        sz = sqlite_backup(platform_src, staging_dir / "platform.db")
        if sz >= 0:
            log.info(f"  ✓ platform.db ({sz:,} bytes)")
            wal_checkpoint(platform_src)
        else:
            ok = False
    else:
        log.warning("  platform.db not found — skipping")

    # ── tenant DBs ───────────────────────────────────────────────────────────
    tenants_dir  = data_dir / "tenants"
    tenant_count = 0
    if tenants_dir.exists():
        for tenant_dir in sorted(tenants_dir.iterdir()):
            if not tenant_dir.is_dir():
                continue
            src = tenant_dir / "inventory.db"
            if not src.exists():
                continue
            slug = tenant_dir.name
            dst  = staging_dir / f"{slug}_inventory.db"
            sz   = sqlite_backup(src, dst)
            if sz >= 0:
                log.info(f"  ✓ {slug} ({sz:,} bytes)")
                wal_checkpoint(src)
                tenant_count += 1
            else:
                ok = False
    else:
        log.warning("  tenants/ directory not found — skipping")

    # ── Compress staging → archive ────────────────────────────────────────────
    try:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging_dir, arcname=f"countdepot_{timestamp}")
        shutil.rmtree(staging_dir)
        size_mb = archive.stat().st_size / 1_048_576
        log.info(f"  → {archive.name} ({size_mb:.2f} MB, {tenant_count} tenant(s))")
    except Exception as exc:
        log.error(f"  ✗ compression failed: {exc}")
        ok = False

    # ── Rotate old backups ────────────────────────────────────────────────────
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for old in backup_dir.glob("countdepot_*.tar.gz"):
        try:
            if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        log.info(f"  Rotated {removed} backup(s) older than {keep_days} days")

    log.info("Backup complete." if ok else "Backup completed with errors — check output above.")
    return ok


def upload_to_s3(archive: Path, bucket: str, prefix: str) -> bool:
    """Upload a backup archive to S3. Returns True on success."""
    if not _BOTO3_AVAILABLE:
        log.error("boto3 not installed — cannot upload to S3. Run: pip install boto3")
        return False
    key = f"{prefix.rstrip('/')}/{archive.name}"
    try:
        s3 = boto3.client("s3")
        s3.upload_file(str(archive), bucket, key,
                       ExtraArgs={"ServerSideEncryption": "AES256"})
        log.info(f"  ↑ S3: s3://{bucket}/{key}")
        return True
    except Exception as exc:
        log.error(f"  ✗ S3 upload failed: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="CountDepot WAL-safe backup")
    parser.add_argument("--data-dir",   default=_default("COUNTDEPOT_DATA_DIR",
                                                          str(PROJECT_DIR / "data")))
    parser.add_argument("--backup-dir", default=_default("COUNTDEPOT_BACKUP_DIR",
                                                          str(PROJECT_DIR.parent / "backups")))
    parser.add_argument("--keep-days",  type=int,
                        default=int(_default("COUNTDEPOT_BACKUP_KEEP_DAYS", "14")))
    parser.add_argument("--s3-bucket",  default=_default("S3_BACKUP_BUCKET", ""),
                        help="S3 bucket name (also reads S3_BACKUP_BUCKET env var)")
    parser.add_argument("--s3-prefix",  default=_default("S3_BACKUP_PREFIX", "countdepot-backups"))
    args = parser.parse_args()

    success = run_backup(
        data_dir   = Path(args.data_dir).resolve(),
        backup_dir = Path(args.backup_dir).resolve(),
        keep_days  = args.keep_days,
    )

    if success and args.s3_bucket:
        backup_dir = Path(args.backup_dir).resolve()
        archives   = sorted(backup_dir.glob("countdepot_*.tar.gz"),
                            key=lambda p: p.stat().st_mtime)
        if archives:
            if not upload_to_s3(archives[-1], args.s3_bucket, args.s3_prefix):
                success = False
        else:
            log.error("No archive found to upload to S3")
            success = False

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
