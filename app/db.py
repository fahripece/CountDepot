import sqlite3
import os
from flask import g
from config import Config

# Track which tenant DBs have had their schema initialized in this process.
# Avoids re-running DDL on every single request (it's a no-op but wastes time).
_initialized_tenants: set = set()


def get_db():
    """Open (or reuse) the current tenant's inventory.db.

    Uses Flask's g object so we open at most one connection per request.
    The DB file lives at data/tenants/{slug}/inventory.db — completely
    separate from every other tenant's data.
    """
    if "db" not in g:
        db_path = _tenant_db_path(g.tenant_slug)
        g.db = sqlite3.connect(db_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA synchronous=NORMAL")
    return g.db


def close_db(e=None):
    """Close DB connection at end of request."""
    db = g.pop("db", None)
    if db:
        db.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def _tenant_db_path(slug):
    """Returns the absolute path to this tenant's inventory DB.
    Creates the directory if it doesn't exist yet."""
    tenant_dir = os.path.join(Config.TENANTS_DIR, slug)
    os.makedirs(tenant_dir, exist_ok=True)
    return os.path.join(tenant_dir, "inventory.db")
