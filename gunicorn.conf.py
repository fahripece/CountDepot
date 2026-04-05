import logging
import logging.handlers
import os

# ── Bind & workers ───────────────────────────────────────────────────────────

bind         = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers      = int(os.environ.get("WEB_CONCURRENCY", 4))
worker_class = "sync"
threads      = 2
timeout      = 60
keepalive    = 5

# ── Gunicorn logging ─────────────────────────────────────────────────────────

accesslog = "-"   # stdout → journald in production
errorlog  = "-"   # stderr → journald in production
loglevel  = os.environ.get("LOG_LEVEL", "info")

# ── Process naming ────────────────────────────────────────────────────────────

proc_name = "countdepot"

# ── Dev reload ───────────────────────────────────────────────────────────────

reload = os.environ.get("FLASK_ENV") == "development"


# ── Structured logging (Python logging) ──────────────────────────────────────
# Runs once in the master process before workers fork.

LOG_FILE    = os.environ.get("LOG_FILE", "")   # e.g. /home/countdepot/logs/countdepot.log
LOG_FORMAT  = "[%(asctime)s] %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _configure_logging():
    root = logging.getLogger()
    root.setLevel(getattr(logging, loglevel.upper(), logging.INFO))

    # Always: stream handler (stdout → journald)
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(stream_h)

    # Optional: rotating file (set LOG_FILE env var to enable)
    if LOG_FILE:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        file_h = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,   # 10 MB per file
            backupCount=5,                # keep 5 rotated files
            encoding="utf-8",
        )
        file_h.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(file_h)

    # Silence noisy libraries at WARNING level
    for noisy in ("werkzeug", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def on_starting(server):
    """Called once when the master process starts."""
    _configure_logging()
    logging.getLogger("countdepot").info(
        f"CountDepot starting — workers={workers} loglevel={loglevel}"
        + (f" logfile={LOG_FILE}" if LOG_FILE else "")
    )


def worker_exit(server, worker):
    """Called when a worker process exits — checkpoint all open SQLite WAL files."""
    try:
        import sqlite3
        from pathlib import Path
        from config import Config
        data_dir = Path(Config.DATA_DIR)

        dbs = list(data_dir.glob("platform.db")) + \
              list(data_dir.glob("tenants/*/inventory.db"))
        for db_path in dbs:
            try:
                with sqlite3.connect(str(db_path)) as con:
                    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
    except Exception:
        pass
