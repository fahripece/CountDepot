import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from app import create_app
from app.db import _initialized_tenants, _migrated_tenants
from app.blueprints.platform import _bootstrap_tenant_db
from app.platform import create_tenant, set_tenant_sector


@pytest.fixture
def app(monkeypatch):
    data_dir = ROOT / ".test_runs" / uuid.uuid4().hex
    tenants_dir = data_dir / "tenants"
    platform_db_path = data_dir / "platform.db"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DATA_DIR", str(data_dir), raising=False)
    monkeypatch.setattr(Config, "TENANTS_DIR", str(tenants_dir), raising=False)
    monkeypatch.setattr(Config, "PLATFORM_DB_PATH", str(platform_db_path), raising=False)
    monkeypatch.setattr(Config, "SECRET_KEY", "test-secret-key", raising=False)
    monkeypatch.setattr(Config, "APP_DOMAIN", "localhost:5000", raising=False)
    monkeypatch.setattr(Config, "DEV_TENANT_SLUG", "dev", raising=False)
    monkeypatch.setattr(Config, "SMTP_HOST", "", raising=False)
    monkeypatch.setattr(Config, "SMTP_PORT", 587, raising=False)
    monkeypatch.setattr(Config, "SMTP_USER", "", raising=False)
    monkeypatch.setattr(Config, "SMTP_PASSWORD", "", raising=False)
    monkeypatch.setattr(Config, "GOOGLE_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(Config, "GOOGLE_CLIENT_SECRET", "", raising=False)
    monkeypatch.setattr(Config, "STRIPE_SECRET_KEY", "", raising=False)
    monkeypatch.setattr(Config, "STRIPE_PUBLISHABLE_KEY", "", raising=False)
    monkeypatch.setattr(Config, "STRIPE_WEBHOOK_SECRET", "", raising=False)

    _initialized_tenants.clear()
    _migrated_tenants.clear()
    shutil.rmtree(data_dir, ignore_errors=True)

    flask_app = create_app()
    flask_app.config.update(TESTING=True)
    yield flask_app

    _initialized_tenants.clear()
    _migrated_tenants.clear()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def tenant(app):
    slug = "acme"
    email = "admin@example.com"
    password = "Password1!"

    with app.app_context():
        create_tenant(slug, "Acme Test", "starter", owner_email=email)
        _bootstrap_tenant_db(slug, password, admin_email=email)
        set_tenant_sector(slug, "it")
        db = sqlite3.connect(os.path.join(Config.TENANTS_DIR, slug, "inventory.db"))
        db.execute("UPDATE users SET must_change_password=0 WHERE email=?", [email])
        db.commit()
        db.close()

    return {
        "slug": slug,
        "email": email,
        "password": password,
        "base_url": "http://localhost:5000",
        "host": f"{slug}.localhost:5000",
        "db_path": os.path.join(Config.TENANTS_DIR, slug, "inventory.db"),
    }


@pytest.fixture
def tenant_db_path():
    return lambda slug: os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
