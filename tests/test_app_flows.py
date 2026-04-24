import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import session

from config import Config
from app.helpers import PERM_KEYS, hash_pw, verify_pw
from app.blueprints.auth import login_page
from app.blueprints.main import admin_page, categories_page, docs_page, integrations_page, inventory, products_page
from app.blueprints.api import (
    api_add_reservation,
    api_cat_add,
    api_category_fields_save,
    api_item_add,
    api_item_clone,
    api_items,
    api_product_edit,
    api_product_add,
    api_products,
    api_items_bulk_serial_add,
    api_item_bulk_clone,
    api_bulk_edit,
    api_qty_adjust,
    api_user_add,
    api_user_invite,
    api_user_permissions,
    api_user_role,
    api_user_intro_tour_complete,
    api_integrations_catalog,
    api_integrations_catalog_save,
    api_integrations_catalog_test,
    api_doc_categories,
    api_doc_category_add,
    api_docs_list,
    api_doc_get,
    api_doc_add,
    api_doc_update,
    api_doc_delete,
    api_woocommerce_test,
    api_item_woocommerce_list,
    api_woocommerce_sync_orders,
    api_set_user_locations,
    api_toggle_2fa,
    api_verify_2fa_setup,
    api_alerts,
)


def _as_response(app, result):
    return app.make_response(result)


def _login(app, tenant, email=None, password=None):
    with app.test_request_context(
        "/login",
        base_url=f"http://{tenant['host']}",
        method="POST",
        data={
            "csrf_token": "test-csrf-token",
            "username": email or tenant["email"],
            "password": password or tenant["password"],
        },
    ):
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or login_page())
        return response, dict(session)


def _seed_logged_in_client(client, tenant):
    expires_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).isoformat()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"
        sess["permissions"] = ",".join(PERM_KEYS)
        sess["expires_at"] = expires_at
        sess["location_ids"] = []
        sess["_csrf_token"] = "test-csrf-token"


def test_login_redirects_to_inventory_for_onboarded_tenant(app, tenant):
    response, saved_session = _login(app, tenant)

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_login_requires_totp_for_tenant_user_when_enabled(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "UPDATE users SET totp_enabled=1, totp_secret=?, must_change_password=0 WHERE email=?",
        ["JBSWY3DPEHPK3PXP", tenant["email"]],
    )
    db.commit()
    db.close()

    response, saved_session = _login(app, tenant)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Two-factor verification" in body
    assert "authenticator app" in body
    assert saved_session["pending_2fa_method"] == "totp"
    assert saved_session["pending_2fa_user_id"] == 1


def test_login_requires_email_otp_for_tenant_user_when_enabled(app, tenant, monkeypatch):
    sent = {}

    def fake_send_email(to, subject, html, text):
        sent["to"] = to
        sent["subject"] = subject
        sent["html"] = html
        sent["text"] = text

    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr("app.mailer.send_email", fake_send_email)

    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "UPDATE users SET two_fa_enabled=1, totp_enabled=0, totp_secret=NULL, must_change_password=0 WHERE email=?",
        [tenant["email"]],
    )
    db.commit()
    db.close()

    response, saved_session = _login(app, tenant)
    body = response.get_data(as_text=True)

    db = sqlite3.connect(tenant["db_path"])
    otp_row = db.execute(
        "SELECT otp FROM login_otp WHERE user_id=1 AND used=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    db.close()

    assert response.status_code == 200
    assert "Two-factor verification" in body
    assert "email address" in body
    assert saved_session["pending_2fa_method"] == "email"
    assert saved_session["pending_2fa_user_id"] == 1
    assert sent["to"] == tenant["email"]
    assert sent["subject"] == "Your CountDepot login code"
    assert otp_row is not None


def test_login_blocks_when_email_otp_enabled_but_smtp_missing(app, tenant, monkeypatch):
    monkeypatch.setattr(Config, "SMTP_HOST", "", raising=False)

    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "UPDATE users SET two_fa_enabled=1, totp_enabled=0, totp_secret=NULL, must_change_password=0 WHERE email=?",
        [tenant["email"]],
    )
    db.commit()
    db.close()

    response, saved_session = _login(app, tenant)
    body = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "email delivery is not configured on this server" in body
    assert "pending_2fa_user_id" not in saved_session
    assert saved_session.get("user_id") is None


def test_login_blocks_when_email_otp_enabled_but_user_has_no_email(app, tenant, monkeypatch):
    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com", raising=False)

    db = sqlite3.connect(tenant["db_path"])
    username = db.execute("SELECT username FROM users WHERE id=1").fetchone()[0]
    db.execute(
        "UPDATE users SET email=NULL, two_fa_enabled=1, totp_enabled=0, totp_secret=NULL, must_change_password=0 WHERE id=1"
    )
    db.commit()
    db.close()

    response, saved_session = _login(app, tenant, email=username)
    body = response.get_data(as_text=True)

    assert response.status_code == 403
    assert "no email address is set" in body
    assert "pending_2fa_user_id" not in saved_session
    assert saved_session.get("user_id") is None


def test_email_2fa_enable_requires_verification_code(app, client, tenant, monkeypatch):
    sent = {}

    def fake_send_email(to, subject, html, text):
        sent["to"] = to
        sent["subject"] = subject
        sent["html"] = html
        sent["text"] = text
        return True

    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr("app.mailer.send_email", fake_send_email)

    with app.test_request_context(
        "/api/user/2fa",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"enabled": True},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session["user_id"] = 1
        session["username"] = "admin"
        session["role"] = "admin"
        session["permissions"] = ",".join(PERM_KEYS)
        session["expires_at"] = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).isoformat()
        session["_csrf_token"] = "test-csrf-token"
        setup_res = _as_response(app, app.preprocess_request() or api_toggle_2fa())
        setup_payload = setup_res.get_json()

    db = sqlite3.connect(tenant["db_path"])
    otp_row = db.execute(
        "SELECT otp FROM login_otp WHERE user_id=1 AND used=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    db.close()

    assert setup_res.status_code == 200
    assert setup_payload["ok"] is True
    assert setup_payload["requires_verification"] is True
    assert sent["to"] == tenant["email"]
    assert sent["subject"] == "Your CountDepot verification code"
    assert otp_row is not None

    with app.test_request_context(
        "/api/user/2fa/verify",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"code": otp_row[0]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session["user_id"] = 1
        session["username"] = "admin"
        session["role"] = "admin"
        session["permissions"] = ",".join(PERM_KEYS)
        session["expires_at"] = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).isoformat()
        session["_csrf_token"] = "test-csrf-token"
        session["pending_2fa_setup_user_id"] = 1
        verify_res = _as_response(app, app.preprocess_request() or api_verify_2fa_setup())
        verify_payload = verify_res.get_json()

    db = sqlite3.connect(tenant["db_path"])
    enabled = db.execute("SELECT two_fa_enabled FROM users WHERE id=1").fetchone()[0]
    db.close()

    assert verify_res.status_code == 200
    assert verify_payload == {"ok": True, "enabled": True}
    assert enabled == 1


def test_first_login_shows_intro_tour_and_completion_persists(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    db.execute(
        "UPDATE users SET last_login=NULL, intro_tour_completed_at=NULL WHERE email=?",
        [tenant["email"]],
    )
    db.commit()
    db.close()

    response, saved_session = _login(app, tenant)
    assert response.status_code == 302
    assert saved_session["show_intro_tour"] is True

    with app.test_request_context(
        "/",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        page = _as_response(app, app.preprocess_request() or inventory())

    body = page.get_data(as_text=True)
    assert "Want a quick CountDepot walkthrough?" in body
    assert "Start tutorial now" in body
    assert "Later" in body
    assert "I don&#39;t need a tutorial" in body or "I don't need a tutorial" in body
    assert "First login guide" in body
    assert "Start with categories" in body
    assert "Create products" in body
    assert "Use sites and locations" in body
    assert "Add SOPs and team docs" in body
    assert 'data-path="/docs"' in body
    assert 'data-tour="docs"' in body
    assert "countDepotIntroTourActive" in body
    assert "goToIntroTourStep" in body
    assert 'id="introTourSpotlight"' in body
    assert 'data-target="categories"' in body
    assert 'data-tour="categories"' in body
    assert "positionIntroTourSpotlight" in body

    with app.test_request_context(
        "/api/user/intro-tour-complete",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        complete = _as_response(app, app.preprocess_request() or api_user_intro_tour_complete())
        completed_session = dict(session)

    assert complete.status_code == 200
    assert complete.get_json()["ok"] is True
    assert completed_session["show_intro_tour"] is False

    db = sqlite3.connect(tenant["db_path"])
    completed_at = db.execute(
        "SELECT intro_tour_completed_at FROM users WHERE email=?",
        [tenant["email"]],
    ).fetchone()[0]
    db.close()
    assert completed_at

    second_response, second_session = _login(app, tenant)
    assert second_response.status_code == 302
    assert not second_session.get("show_intro_tour")


def test_low_stock_alerts_are_scoped_per_site(app, client, tenant):
    _seed_logged_in_client(client, tenant)

    db = sqlite3.connect(tenant["db_path"])
    db.execute("INSERT INTO locations (name, active, created_at) VALUES ('Warehouse A', 1, datetime('now'))")
    site_a = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO locations (name, active, created_at) VALUES ('Warehouse B', 1, datetime('now'))")
    site_b = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO products (name, low_stock_threshold, active, created_at) VALUES (?,?,1,datetime('now'))",
        ["Test Router", 2],
    )
    product_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO items (product_id, name, location_id, active, sold, checked_out, created_at) VALUES (?,?,?,?,0,0,datetime('now'))",
        [product_id, "Test Router A1", site_a, 1],
    )
    for idx in range(4):
        db.execute(
            "INSERT INTO items (product_id, name, location_id, active, sold, checked_out, created_at) VALUES (?,?,?,?,0,0,datetime('now'))",
            [product_id, f"Test Router B{idx}", site_b, 1],
        )
    db.commit()
    db.close()

    with app.test_request_context("/api/alerts", base_url=f"http://{tenant['host']}"):
        session["user_id"] = 1
        session["username"] = "admin"
        session["role"] = "admin"
        session["permissions"] = ",".join(PERM_KEYS)
        session["expires_at"] = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).isoformat()
        session["location_ids"] = []
        res = _as_response(app, app.preprocess_request() or api_alerts())
        payload = res.get_json()

    with app.test_request_context(f"/api/alerts?loc={site_b}", base_url=f"http://{tenant['host']}"):
        session["user_id"] = 1
        session["username"] = "admin"
        session["role"] = "admin"
        session["permissions"] = ",".join(PERM_KEYS)
        session["expires_at"] = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).isoformat()
        session["location_ids"] = []
        site_b_res = _as_response(app, app.preprocess_request() or api_alerts())
        site_b_payload = site_b_res.get_json()

    assert res.status_code == 200
    assert any(a["name"] == "Test Router" and a["location_name"] == "Warehouse A" for a in payload)
    assert not any(a["name"] == "Test Router" and a["location_name"] == "Warehouse B" for a in payload)
    assert site_b_payload == []


def test_intro_tour_shows_until_completed_even_if_user_has_logged_in_before(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "UPDATE users SET last_login='2026-04-17 09:00:00', intro_tour_completed_at=NULL WHERE email=?",
        [tenant["email"]],
    )
    db.commit()
    db.close()

    response, saved_session = _login(app, tenant)
    assert response.status_code == 302
    assert saved_session["show_intro_tour"] is True


def test_onboarding_gate_redirects_unfinished_tenant(app):
    slug = "fresh"
    email = "fresh@example.com"
    password = "Password1!"

    from app.platform import create_tenant
    from app.blueprints.platform import _bootstrap_tenant_db

    with app.app_context():
        create_tenant(slug, "Fresh Tenant", "starter", owner_email=email)
        _bootstrap_tenant_db(slug, password, admin_email=email)

    db_path = os.path.join(Config.TENANTS_DIR, slug, "inventory.db")
    db = sqlite3.connect(db_path)
    db.execute("UPDATE users SET must_change_password=0 WHERE email=?", [email])
    db.commit()
    db.close()

    tenant = {
        "slug": slug,
        "email": email,
        "password": password,
        "host": f"{slug}.localhost:5000",
    }

    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context("/", base_url=f"http://{tenant['host']}", method="GET"):
        session.update(saved_session)
        response = _as_response(app, app.preprocess_request())

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding")


def test_admin_can_create_product_and_item_and_fetch_inventory(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302
    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Roadmap Test Category", "#ffffff"],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/product/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Roadmap Test Product",
            "category_id": category_id,
            "manufacturer": "Acme",
            "model": "MODEL-100",
            "vendor_sku": "SUPPLIER-PART-12345",
            "serial_tracked": 0,
            "qty_tracked": 0,
            "require_scan_checkout": 0,
            "print_scan_label": 0,
            "require_serial": 0,
            "require_vendor_sku": 1,
            "require_internal_sku": 0,
            "low_stock_threshold": 0,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        product_response = _as_response(app, app.preprocess_request() or api_product_add())
        product_data = product_response.get_json()

    assert product_response.status_code == 200
    assert product_data["ok"] is True
    assert product_data["product"]["id"] == product_data["id"]
    assert product_data["product"]["item_count"] == 0

    with app.test_request_context(
        "/api/item/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
                "name": "Roadmap Test Item",
                "product_id": product_data["id"],
                "category_id": category_id,
            "sku": "SUPPLIER-PART-12345-UNIT",
            "condition": "New",
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        item_response = _as_response(app, app.preprocess_request() or api_item_add())
        item_data = item_response.get_json()

    assert item_response.status_code == 200
    assert item_data["ok"] is True

    with app.test_request_context(
        "/api/products",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        products_response = _as_response(app, app.preprocess_request() or api_products())
        products = products_response.get_json()

    assert products_response.status_code == 200
    assert any(
        product["id"] == product_data["id"] and product["item_count"] == 1
        for product in products
    )

    with app.test_request_context(
        "/api/items?q=Roadmap%20Test&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        inventory_response = _as_response(app, app.preprocess_request() or api_items())
        items = inventory_response.get_json()

    assert inventory_response.status_code == 200
    assert any(item["name"] == "Roadmap Test Item" for item in items)


def test_inventory_category_filter_matches_integer_category_ids(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_a = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Filter Cat A", "#ffffff"],
    ).lastrowid
    category_b = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Filter Cat B", "#000000"],
    ).lastrowid
    db.execute(
        "INSERT INTO items (name,category_id,shelf,cost_price,active,created_at) VALUES (?,?,?,?,1,datetime('now'))",
        ["Category Match Item", category_a, "A1", 10.0],
    )
    db.execute(
        "INSERT INTO items (name,category_id,shelf,cost_price,active,created_at) VALUES (?,?,?,?,1,datetime('now'))",
        ["Wrong Category Item", category_b, "B1", 20.0],
    )
    db.commit()
    db.close()

    with app.test_request_context(
        f"/api/items?cat={category_a}&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        inventory_response = _as_response(app, app.preprocess_request() or api_items())
        items = inventory_response.get_json()

    assert inventory_response.status_code == 200
    assert len(items) == 1
    assert items[0]["name"] == "Category Match Item"


def test_internal_sku_product_item_is_visible_in_inventory(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302
    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Internal SKU Test Category", "#ffffff"],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/product/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Internal SKU Visibility Product",
            "category_id": category_id,
            "manufacturer": "Acme",
            "model": "AUTO-100",
            "serial_tracked": 0,
            "qty_tracked": 0,
            "require_scan_checkout": 0,
            "print_scan_label": 0,
            "require_serial": 0,
            "require_vendor_sku": 0,
            "require_internal_sku": 1,
            "low_stock_threshold": 0,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        product_response = _as_response(app, app.preprocess_request() or api_product_add())
        product_data = product_response.get_json()

    assert product_response.status_code == 200
    assert product_data["ok"] is True

    with app.test_request_context(
        "/api/item/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
                "name": "Internal SKU Visibility Item",
                "product_id": product_data["id"],
                "category_id": category_id,
            "condition": "New",
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        item_response = _as_response(app, app.preprocess_request() or api_item_add())
        item_data = item_response.get_json()

    assert item_response.status_code == 200
    assert item_data["ok"] is True

    with app.test_request_context(
        f"/api/items?q=%23{item_data['id']}&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        inventory_response = _as_response(app, app.preprocess_request() or api_items())
        items = inventory_response.get_json()

    assert inventory_response.status_code == 200
    assert any(
        item["id"] == item_data["id"] and item["internal_sku"]
        for item in items
    )


def test_product_category_is_inherited_when_item_payload_omits_category(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Inherited Product Category", "#123456"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_internal_sku,active,created_at) "
        "VALUES (?,?,1,1,datetime('now'))",
        ["Inherited Category Product", category_id],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/item/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Inherited Category Item",
            "product_id": product_id,
            "condition": "New",
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        item_response = _as_response(app, app.preprocess_request() or api_item_add())
        item_data = item_response.get_json()

    assert item_response.status_code == 200
    assert item_data["ok"] is True

    db = sqlite3.connect(tenant["db_path"])
    saved_category_id = db.execute(
        "SELECT category_id FROM items WHERE id=?", [item_data["id"]]
    ).fetchone()[0]
    legacy_item_id = db.execute(
        "INSERT INTO items (name,product_id,category_id,active,created_at) "
        "VALUES (?,?,NULL,1,datetime('now'))",
        ["Legacy Missing Category Item", product_id],
    ).lastrowid
    db.commit()
    db.close()

    assert saved_category_id == category_id

    with app.test_request_context(
        "/api/items?hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        inventory_response = _as_response(app, app.preprocess_request() or api_items())
        items = inventory_response.get_json()

    created = next(item for item in items if item["id"] == item_data["id"])
    legacy = next(item for item in items if item["id"] == legacy_item_id)
    assert created["category"] == "Inherited Product Category"
    assert created["color"] == "#123456"
    assert legacy["category"] == "Inherited Product Category"
    assert legacy["product_category_id"] == category_id


def test_data_repair_backfills_existing_item_category_from_product(app, tenant):
    from app.schema import _run_data_repairs

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Repair Product Category", "#abcdef"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,active,created_at) VALUES (?,?,1,datetime('now'))",
        ["Repair Category Product", category_id],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,product_id,category_id,active,created_at) "
        "VALUES (?,?,NULL,1,datetime('now'))",
        ["Repair Missing Category Item", product_id],
    ).lastrowid
    db.execute(
        "INSERT OR REPLACE INTO settings (key,value) VALUES ('repair_items_category_from_product_v2','0')"
    )
    db.commit()

    _run_data_repairs(db)
    db.commit()

    saved_category_id = db.execute(
        "SELECT category_id FROM items WHERE id=?", [item_id]
    ).fetchone()[0]
    repair_flag = db.execute(
        "SELECT value FROM settings WHERE key='repair_items_category_from_product_v2'"
    ).fetchone()[0]
    db.close()

    assert saved_category_id == category_id
    assert repair_flag == "1"


def test_product_category_edit_updates_existing_items(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    old_category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Old Product Category", "#111111"],
    ).lastrowid
    new_category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["New Product Category", "#222222"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_internal_sku,active,created_at) "
        "VALUES (?,?,1,1,datetime('now'))",
        ["Moved Category Product", old_category_id],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,product_id,category_id,active,created_at) "
        "VALUES (?,?,?,1,datetime('now'))",
        ["Moved Category Item", product_id, old_category_id],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/product/edit",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "id": product_id,
            "name": "Moved Category Product",
            "category_id": new_category_id,
            "require_internal_sku": 1,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_product_edit())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True

    db = sqlite3.connect(tenant["db_path"])
    item_category_id = db.execute(
        "SELECT category_id FROM items WHERE id=?", [item_id]
    ).fetchone()[0]
    db.close()
    assert item_category_id == new_category_id


def test_admin_add_user_reports_invite_email_failure(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/api/user/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "username": "new.user@example.com",
            "role": "worker",
            "permissions": ["view_inventory"],
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_user_add())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["emailed"] is False
    assert "reset-password" in data["invite_url"]
    assert "new.user@example.com" not in data["invite_url"]

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    user = db.execute(
        "SELECT id, email_verified, must_change_password FROM users WHERE email=?",
        ["new.user@example.com"],
    ).fetchone()
    token = db.execute(
        "SELECT token, used FROM password_reset_tokens WHERE user_id=?",
        [user["id"]],
    ).fetchone()
    db.close()

    assert user["email_verified"] == 0
    assert user["must_change_password"] == 1
    assert token["used"] == 0


def test_admin_can_resend_user_invite(app, tenant, monkeypatch):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    user_id = db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,0,1)",
        ["resend@example.com", hash_pw("placeholder"), "worker", "view_inventory", "resend@example.com"],
    ).lastrowid
    db.commit()
    db.close()

    sent = {}

    def fake_send_invite(to, slug, token, inviter="Your admin"):
        sent.update({"to": to, "slug": slug, "token": token, "inviter": inviter})
        return True

    import importlib
    mailer = importlib.import_module("app.mailer")
    monkeypatch.setattr(mailer, "send_invite_email", fake_send_invite)

    with app.test_request_context(
        "/api/user/invite",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"id": user_id},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_user_invite())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["emailed"] is True
    assert sent["to"] == "resend@example.com"
    assert sent["slug"] == tenant["slug"]
    assert sent["token"]


def test_password_reset_token_post_does_not_require_existing_csrf_session(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    user_id = db.execute(
        "SELECT id FROM users WHERE email=?",
        [tenant["email"]],
    ).fetchone()["id"]
    token = "reset-no-csrf-session-token"
    db.execute(
        "INSERT INTO password_reset_tokens (user_id,token,expires_at,used) "
        "VALUES (?,?,datetime('now','+1 hour'),0)",
        [user_id, token],
    )
    db.commit()
    db.close()

    client = app.test_client()
    response = client.post(
        f"/reset-password/{token}",
        base_url=f"http://{tenant['host']}",
        data={
            "new_password": "NewPassword1!",
            "confirm_password": "NewPassword1!",
        },
    )

    assert response.status_code == 200
    assert "Password updated" in response.get_data(as_text=True)

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT u.password, u.must_change_password, u.email_verified, t.used "
        "FROM users u JOIN password_reset_tokens t ON t.user_id=u.id "
        "WHERE t.token=?",
        [token],
    ).fetchone()
    db.close()

    assert verify_pw("NewPassword1!", row["password"])
    assert row["must_change_password"] == 0
    assert row["email_verified"] == 1
    assert row["used"] == 1


def test_required_category_field_change_marks_existing_items_incomplete(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Compliance Gear", "#ffffff"],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,category_id,shelf,cost_price,extra_fields,active,created_at) "
        "VALUES (?,?,?,?,?,1,'2026-01-01 00:00:00')",
        ["Harness", category_id, "A1", 25.0, "{}"],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/category/fields/save",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "category_id": category_id,
            "fields": [{
                "field_label": "Inspection Date",
                "field_key": "inspection_date",
                "field_type": "date",
                "required": 1,
            }],
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_category_fields_save())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["resynced"]["checked"] == 1
    assert data["resynced"]["incomplete"] == 1

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    task = db.execute("SELECT title, notes, status FROM tasks WHERE item_id=?", [item_id]).fetchone()
    db.close()

    assert task["status"] == "todo"
    assert "Inspection Date" in task["notes"]

    with app.test_request_context(
        "/api/items?status=incomplete&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        inventory_response = _as_response(app, app.preprocess_request() or api_items())
        items = inventory_response.get_json()

    target = next(item for item in items if item["id"] == item_id)
    assert target["is_incomplete"] is True
    assert "Inspection Date" in target["missing_fields"]


def test_product_requirement_change_marks_existing_items_incomplete(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    product_id = db.execute(
        "INSERT INTO products (name,require_internal_sku,require_serial,require_vendor_sku,active,created_at) "
        "VALUES (?,1,0,0,1,'2026-01-01 00:00:00')",
        ["Tracked Widget"],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,product_id,shelf,cost_price,active,created_at) "
        "VALUES (?,?,?,?,1,'2026-01-01 00:00:00')",
        ["Widget Unit", product_id, "B1", 10.0],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/product/edit",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "id": product_id,
            "name": "Tracked Widget",
            "require_serial": 1,
            "require_vendor_sku": 0,
            "require_internal_sku": 1,
            "serial_tracked": 0,
            "qty_tracked": 0,
            "require_scan_checkout": 0,
            "print_scan_label": 0,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_product_edit())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["resynced"]["checked"] == 1
    assert data["resynced"]["incomplete"] == 1

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    task = db.execute("SELECT notes, status FROM tasks WHERE item_id=?", [item_id]).fetchone()
    db.close()

    assert task["status"] == "todo"
    assert "serial #" in task["notes"]


def test_required_category_field_is_enforced_server_side_on_add(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Server Validation", "#ffffff"],
    ).lastrowid
    db.execute(
        "INSERT INTO category_fields "
        "(category_id,field_label,field_key,field_type,required,sort_order) "
        "VALUES (?,?,?,?,1,0)",
        [category_id, "Asset Tag", "asset_tag", "text"],
    )
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/item/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Missing Asset Tag",
            "category_id": category_id,
            "shelf": "C1",
            "cost_price": 15.0,
            "extra_fields": {},
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_item_add())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert "Asset Tag" in data["msg"]


def test_bulk_serial_add_creates_individual_items(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Bulk Serial Category", "#ffffff"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_serial,require_internal_sku,active,created_at) "
        "VALUES (?,?,1,1,1,'2026-01-01 00:00:00')",
        ["Bulk Serial Product", category_id],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/items/bulk-serial-add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Bulk Serial Product",
            "product_id": product_id,
            "category_id": category_id,
            "condition": "New",
            "shelf": "BULK-A",
            "cost_price": 11.5,
            "serials": ["BSN-001", "BSN-002", "BSN-003"],
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_items_bulk_serial_add())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["count"] == 3

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT name, serial, sku, shelf, cost_price FROM items WHERE product_id=? ORDER BY serial",
        [product_id],
    ).fetchall()
    db.close()

    assert [r["serial"] for r in rows] == ["BSN-001", "BSN-002", "BSN-003"]
    assert all(r["name"] == "Bulk Serial Product" for r in rows)
    assert all(r["sku"] is None for r in rows)
    assert all(r["shelf"] == "BULK-A" for r in rows)


def test_bulk_serial_add_rejects_existing_duplicate_serial(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Bulk Duplicate Category", "#ffffff"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_serial,require_internal_sku,active,created_at) "
        "VALUES (?,?,1,1,1,'2026-01-01 00:00:00')",
        ["Bulk Duplicate Product", category_id],
    ).lastrowid
    db.execute(
        "INSERT INTO items (name,product_id,category_id,serial,shelf,cost_price,active,created_at) "
        "VALUES (?,?,?,?,?,?,1,'2026-01-01 00:00:00')",
        ["Existing", product_id, category_id, "DUP-001", "A1", 5.0],
    )
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/items/bulk-serial-add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Bulk Duplicate Product",
            "product_id": product_id,
            "category_id": category_id,
            "condition": "New",
            "shelf": "BULK-B",
            "cost_price": 11.5,
            "serials": ["DUP-001", "DUP-002"],
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_items_bulk_serial_add())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert "DUP-001" in data["msg"]


def test_bulk_clone_creates_same_item_with_new_serials(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Bulk Clone Category", "#ffffff"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_serial,require_internal_sku,active,created_at) "
        "VALUES (?,?,1,1,1,'2026-01-01 00:00:00')",
        ["Bulk Clone Product", category_id],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,product_id,category_id,serial,shelf,cost_price,condition,active,created_at) "
        "VALUES (?,?,?,?,?,?,?,1,'2026-01-01 00:00:00')",
        ["Bulk Clone Product", product_id, category_id, "SRC-001", "CLONE-A", 22.0, "New"],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        f"/api/item/{item_id}/bulk-clone",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"serials": ["CLONE-001", "CLONE-002"]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_item_bulk_clone(item_id))
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["count"] == 2

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT name, serial, shelf, cost_price FROM items WHERE product_id=? AND serial LIKE 'CLONE-%' ORDER BY serial",
        [product_id],
    ).fetchall()
    db.close()

    assert [r["serial"] for r in rows] == ["CLONE-001", "CLONE-002"]
    assert all(r["name"] == "Bulk Clone Product" for r in rows)
    assert all(r["shelf"] == "CLONE-A" for r in rows)


def test_single_clone_accepts_scanned_serial(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Single Clone Category", "#ffffff"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_serial,require_internal_sku,active,created_at) "
        "VALUES (?,?,1,1,1,'2026-01-01 00:00:00')",
        ["Single Clone Product", category_id],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,product_id,category_id,serial,shelf,cost_price,condition,active,created_at) "
        "VALUES (?,?,?,?,?,?,?,1,'2026-01-01 00:00:00')",
        ["Single Clone Product", product_id, category_id, "SRC-SINGLE", "A1", 33.0, "New"],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        f"/api/item/{item_id}/clone",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"serial": "SCANNED-SINGLE-001"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_item_clone(item_id))
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    cloned = db.execute("SELECT serial, shelf, cost_price FROM items WHERE id=?", [data["id"]]).fetchone()
    db.close()

    assert cloned["serial"] == "SCANNED-SINGLE-001"
    assert cloned["shelf"] == "A1"
    assert cloned["cost_price"] == 33.0


def test_bulk_clone_handles_legacy_null_numeric_fields(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Legacy Clone Category", "#ffffff"],
    ).lastrowid
    db.execute(
        "INSERT INTO category_fields (category_id,field_label,field_key,field_type,required,sort_order) "
        "VALUES (?,?,?,?,1,1)",
        [category_id, "Asset Tag", "asset_tag", "text"],
    )
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_serial,require_internal_sku,active,created_at) "
        "VALUES (?,?,1,1,1,'2026-01-01 00:00:00')",
        ["Legacy Clone Product", category_id],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,product_id,category_id,serial,shelf,cost_price,condition,tax_paid,has_poe,extra_fields,active,created_at) "
        "VALUES (?,?,?,?,?,?,?,NULL,NULL,?,1,'2026-01-01 00:00:00')",
        [
            "Legacy Clone Product",
            product_id,
            category_id,
            "SRC-LEGACY",
            "LEG-A",
            22.0,
            "New",
            json.dumps({"asset_tag": "AT-100"}),
        ],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        f"/api/item/{item_id}/bulk-clone",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"serials": ["LEG-001", "LEG-002"]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_item_bulk_clone(item_id))
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["count"] == 2

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT serial, tax_paid, has_poe, extra_fields FROM items WHERE product_id=? AND serial LIKE 'LEG-%' ORDER BY serial",
        [product_id],
    ).fetchall()
    db.close()

    assert [r["serial"] for r in rows] == ["LEG-001", "LEG-002"]
    assert all(r["tax_paid"] == -1 for r in rows)
    assert all(r["has_poe"] == 0 for r in rows)
    assert all(json.loads(r["extra_fields"]) == {"asset_tag": "AT-100"} for r in rows)


def test_inventory_load_handles_double_encoded_extra_fields(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Double Encoded Category", "#ffffff"],
    ).lastrowid
    db.execute(
        "INSERT INTO category_fields (category_id,field_label,field_key,field_type,required,sort_order) "
        "VALUES (?,?,?,?,1,1)",
        [category_id, "Asset Tag", "asset_tag", "text"],
    )
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_serial,active,created_at) "
        "VALUES (?,?,1,1,'2026-01-01 00:00:00')",
        ["Double Encoded Product", category_id],
    ).lastrowid
    db.execute(
        "INSERT INTO items (name,product_id,category_id,serial,shelf,cost_price,extra_fields,active,created_at) "
        "VALUES (?,?,?,?,?,?,?,1,'2026-01-01 00:00:00')",
        [
            "Double Encoded Product",
            product_id,
            category_id,
            "DE-001",
            "DE-A",
            10.0,
            json.dumps(json.dumps({"asset_tag": "AT-200"})),
        ],
    )
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/items",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        response = _as_response(app, app.preprocess_request() or api_items())
        data = response.get_json()

    assert response.status_code == 200
    rows = [row for row in data["items"] if row["serial"] == "DE-001"]
    assert len(rows) == 1
    assert rows[0]["is_incomplete"] is False


def test_bulk_edit_can_update_cost_and_sale_prices(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Bulk Price Category", "#ffffff"],
    ).lastrowid
    ids = []
    for name in ["Price Item A", "Price Item B"]:
        ids.append(db.execute(
            "INSERT INTO items (name,category_id,shelf,cost_price,sale_price,active,created_at) "
            "VALUES (?,?,?,?,?,1,'2026-01-01 00:00:00')",
            [name, category_id, "PRICE-A", None, None],
        ).lastrowid)
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/items/bulk-edit",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"ids": ids, "cost_price": "12.50", "sale_price": "20.00"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_bulk_edit())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["updated"] == 2

    db = sqlite3.connect(tenant["db_path"])
    db.row_factory = sqlite3.Row
    rows = db.execute(
        f"SELECT id, cost_price, sale_price FROM items WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id",
        ids,
    ).fetchall()
    tasks = db.execute(
        f"SELECT item_id, status FROM tasks WHERE item_id IN ({','.join('?' for _ in ids)}) ORDER BY item_id",
        ids,
    ).fetchall()
    db.close()

    assert all(row["cost_price"] == 12.5 for row in rows)
    assert all(row["sale_price"] == 20.0 for row in rows)
    assert all(task["status"] == "done" for task in tasks)


def test_permission_changes_apply_to_active_worker_session(app, tenant):
    admin_response, admin_session = _login(app, tenant)
    assert admin_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Permission Refresh Category", "#ffffff"],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,category_id,serial,shelf,active,created_at) "
        "VALUES (?,?,?,?,1,'2026-01-01 00:00:00')",
        ["Permission Refresh Item", category_id, "PERM-001", "OLD"],
    ).lastrowid
    worker_id = db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,1,0)",
        [
            "perm.worker@example.com",
            hash_pw("Password1!"),
            "worker",
            "view_inventory",
            "perm.worker@example.com",
        ],
    ).lastrowid
    db.commit()
    db.close()

    worker_response, worker_session = _login(
        app, tenant, "perm.worker@example.com", "Password1!"
    )
    assert worker_response.status_code == 302
    assert worker_session["permissions"] == "view_inventory"

    with app.test_request_context(
        "/api/user/permissions",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"id": worker_id, "permissions": ["view_inventory", "write_items"]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(admin_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_user_permissions())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True

    with app.test_request_context(
        "/api/items/bulk-edit",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"ids": [item_id], "shelf": "NEW"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(worker_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_bulk_edit())
        data = response.get_json()
        refreshed_permissions = session["permissions"]

    assert response.status_code == 200
    assert data["ok"] is True
    assert "write_items" in refreshed_permissions.split(",")

    db = sqlite3.connect(tenant["db_path"])
    shelf = db.execute("SELECT shelf FROM items WHERE id=?", [item_id]).fetchone()[0]
    db.close()
    assert shelf == "NEW"


def test_admin_can_promote_worker_to_admin_and_active_session_refreshes(app, tenant):
    admin_response, admin_session = _login(app, tenant)
    assert admin_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    worker_id = db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,1,0)",
        [
            "promote.worker@example.com",
            hash_pw("Password1!"),
            "worker",
            "view_inventory",
            "promote.worker@example.com",
        ],
    ).lastrowid
    db.commit()
    db.close()

    worker_response, worker_session = _login(
        app, tenant, "promote.worker@example.com", "Password1!"
    )
    assert worker_response.status_code == 302
    assert worker_session["role"] == "worker"

    with app.test_request_context(
        "/api/user/role",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"id": worker_id, "role": "admin"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(admin_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_user_role())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True

    db = sqlite3.connect(tenant["db_path"])
    row = db.execute(
        "SELECT role, permissions FROM users WHERE id=?", [worker_id]
    ).fetchone()
    db.close()
    assert row[0] == "admin"
    assert set(row[1].split(",")) == set(PERM_KEYS)

    with app.test_request_context(
        "/admin",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(worker_session)
        response = app.preprocess_request()
        html = response or admin_page()
        refreshed_role = session["role"]

    assert refreshed_role == "admin"
    assert "Users &amp; Permissions" in html


def test_admin_cannot_change_own_role(app, tenant):
    admin_response, admin_session = _login(app, tenant)
    assert admin_response.status_code == 302

    with app.test_request_context(
        "/api/user/role",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"id": admin_session["user_id"], "role": "worker"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(admin_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_user_role())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert "own role" in data["msg"]

    db = sqlite3.connect(tenant["db_path"])
    role = db.execute(
        "SELECT role FROM users WHERE id=?", [admin_session["user_id"]]
    ).fetchone()[0]
    db.close()
    assert role == "admin"


def test_location_changes_do_not_force_logout_active_worker(app, tenant):
    admin_response, admin_session = _login(app, tenant)
    assert admin_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    loc_id = db.execute(
        "INSERT INTO locations (name,created_at) VALUES (?,datetime('now'))",
        ["Worker Site"],
    ).lastrowid
    worker_id = db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,1,0)",
        [
            "site.worker@example.com",
            hash_pw("Password1!"),
            "worker",
            "view_inventory",
            "site.worker@example.com",
        ],
    ).lastrowid
    db.commit()
    db.close()

    worker_response, worker_session = _login(
        app, tenant, "site.worker@example.com", "Password1!"
    )
    assert worker_response.status_code == 302

    with app.test_request_context(
        f"/api/user/{worker_id}/locations",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"location_ids": [loc_id]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(admin_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_set_user_locations(worker_id))
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True

    with app.test_request_context(
        "/api/items",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(worker_session)
        response = _as_response(app, app.preprocess_request() or api_items())
        data = response.get_json()
        refreshed_locations = session["location_ids"]

    assert response.status_code == 200
    assert "items" in data
    assert refreshed_locations == [loc_id]


def test_full_permission_worker_sees_relevant_settings_nav(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,1,0)",
        [
            "full.worker@example.com",
            hash_pw("Password1!"),
            "worker",
            "view_inventory,view_dashboard,view_audit,checkout_checkin,write_items,qty_adjust,sell_items,delete_items,import_export",
            "full.worker@example.com",
        ],
    )
    db.commit()
    db.close()

    response, saved_session = _login(
        app, tenant, "full.worker@example.com", "Password1!"
    )
    assert response.status_code == 302

    with app.test_request_context("/", base_url=f"http://{tenant['host']}", method="GET"):
        session.update(saved_session)
        response = app.preprocess_request()
        html = response or inventory()

    assert "Settings" in html
    assert ">Categories<" in html
    assert ">Products<" in html
    assert ">Contacts<" in html
    assert ">Importer<" in html
    assert "Users &amp; Permissions" not in html
    assert ">Billing<" not in html


def test_write_items_worker_can_manage_categories_and_add_products(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    worker_id = db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,1,0)",
        [
            "product.manager@example.com",
            hash_pw("Password1!"),
            "worker",
            "view_inventory,write_items",
            "product.manager@example.com",
        ],
    ).lastrowid
    db.commit()
    db.close()

    response, saved_session = _login(
        app, tenant, "product.manager@example.com", "Password1!"
    )
    assert response.status_code == 302
    assert saved_session["user_id"] == worker_id

    with app.test_request_context(
        "/categories",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        html = app.preprocess_request() or categories_page()

    assert "+ New Category" in html
    assert "Fields" in html

    with app.test_request_context(
        "/api/category/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"name": "Worker Product Category", "color": "#ffffff"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        cat_response = _as_response(app, app.preprocess_request() or api_cat_add())
        cat_data = cat_response.get_json()

    assert cat_response.status_code == 200
    assert cat_data["ok"] is True
    category_id = cat_data["id"]

    with app.test_request_context(
        "/api/category/fields/save",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "category_id": category_id,
            "fields": [
                {
                    "field_label": "Asset Tag",
                    "field_key": "asset_tag",
                    "field_type": "text",
                    "required": 1,
                }
            ],
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        fields_response = _as_response(
            app, app.preprocess_request() or api_category_fields_save()
        )
        fields_data = fields_response.get_json()

    assert fields_response.status_code == 200
    assert fields_data["ok"] is True

    with app.test_request_context(
        "/products",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        html = app.preprocess_request() or products_page()

    assert "+ New Product" in html

    with app.test_request_context(
        "/api/product/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Worker Created Product",
            "category_id": category_id,
            "require_internal_sku": 1,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        product_response = _as_response(app, app.preprocess_request() or api_product_add())
        product_data = product_response.get_json()

    assert product_response.status_code == 200
    assert product_data["ok"] is True


def test_product_cost_requirement_change_updates_existing_items(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Cost Optional Category", "#ffffff"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,require_cost,active,created_at) VALUES (?,?,1,1,datetime('now'))",
        ["Cost Optional Product", category_id],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,category_id,product_id,shelf,cost_price,active,created_at) VALUES (?,?,?,?,NULL,1,datetime('now'))",
        ["Cost Optional Item", category_id, product_id, "A1"],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/items?status=incomplete&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        default_response = _as_response(app, app.preprocess_request() or api_items())
        default_data = default_response.get_json()
        default_items = default_data["items"] if isinstance(default_data, dict) else default_data

    assert any(item["id"] == item_id and "cost" in item["missing_fields"] for item in default_items)

    with app.test_request_context(
        "/api/product/edit",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "id": product_id,
            "name": "Cost Optional Product",
            "category_id": category_id,
            "require_cost": 0,
            "require_serial": 0,
            "require_vendor_sku": 0,
            "require_internal_sku": 1,
            "serial_tracked": 0,
            "qty_tracked": 0,
            "require_scan_checkout": 0,
            "print_scan_label": 0,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        edit_response = _as_response(app, app.preprocess_request() or api_product_edit())
        edit_data = edit_response.get_json()

    assert edit_response.status_code == 200
    assert edit_data["ok"] is True
    assert edit_data["resynced"]["checked"] == 1
    assert edit_data["resynced"]["incomplete"] == 0

    with app.test_request_context(
        "/api/items?status=incomplete&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        optional_response = _as_response(app, app.preprocess_request() or api_items())
        optional_data = optional_response.get_json()
        optional_items = optional_data["items"] if isinstance(optional_data, dict) else optional_data

    assert not any(item["id"] == item_id for item in optional_items)


def test_quantity_reservation_reduces_available_and_blocks_over_reserve(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/api/product/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Reservation Qty Product",
            "category_id": 1,
            "serial_tracked": 0,
            "qty_tracked": 1,
            "require_scan_checkout": 0,
            "print_scan_label": 0,
            "require_serial": 0,
            "require_vendor_sku": 0,
            "require_internal_sku": 0,
            "low_stock_threshold": 0,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        product_data = _as_response(app, app.preprocess_request() or api_product_add()).get_json()
    if not product_data.get("ok"):
        db = sqlite3.connect(tenant["db_path"])
        product_data = {"id": db.execute(
            "INSERT INTO products (name,category_id,qty_tracked,serial_tracked,active,created_at) VALUES (?,?,?,?,1,datetime('now'))",
            ["Reservation Qty Product Direct", 1, 1, 0],
        ).lastrowid}
        db.commit()
        db.close()

    with app.test_request_context(
        "/api/item/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"name": "Reservation Qty Item", "product_id": product_data["id"], "category_id": 1, "qty": 10},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        item_data = _as_response(app, app.preprocess_request() or api_item_add()).get_json()
    if not item_data.get("ok"):
        db = sqlite3.connect(tenant["db_path"])
        item_data = {"id": db.execute(
            "INSERT INTO items (name,category_id,product_id,qty,qty_out,active,created_at) VALUES (?,?,?,?,0,1,datetime('now'))",
            ["Reservation Qty Item", 1, product_data["id"], 10],
        ).lastrowid}
        db.commit()
        db.close()

    with app.test_request_context(
        f"/api/item/{item_data['id']}/reservation",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "reserved_by": "Install Team",
            "reserved_from": "2026-05-01",
            "reserved_to": "2026-05-05",
            "qty_reserved": 5,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        reserve_data = _as_response(app, app.preprocess_request() or api_add_reservation(item_data["id"])).get_json()

    assert reserve_data["ok"] is True

    with app.test_request_context(
        "/api/items?q=Reservation%20Qty%20Item&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        items = _as_response(app, app.preprocess_request() or api_items()).get_json()

    item = next(i for i in items if i["id"] == item_data["id"])
    assert item["qty"] == 10
    assert item["reserved_qty"] == 5
    assert item["available"] == 5

    with app.test_request_context(
        f"/api/item/{item_data['id']}/reservation",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "reserved_by": "Second Team",
            "reserved_from": "2026-05-02",
            "reserved_to": "2026-05-04",
            "qty_reserved": 6,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        over_data = _as_response(app, app.preprocess_request() or api_add_reservation(item_data["id"])).get_json()

    assert over_data["ok"] is False
    assert "Only 5" in over_data["msg"]


def test_quantity_use_for_reservation_fulfills_remaining_units(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    product_id = db.execute(
        "INSERT INTO products (name,category_id,qty_tracked,serial_tracked,active,created_at) VALUES (?,?,?,?,1,datetime('now'))",
        ["Reservation Fulfill Product", 1, 1, 0],
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO items (name,category_id,product_id,qty,qty_out,active,created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
        ["Reservation Fulfill Item", 1, product_id, 10, 0, 1],
    ).lastrowid
    reservation_id = db.execute(
        """INSERT INTO item_reservations
           (item_id,reserved_by,reserved_from,reserved_to,purpose,qty_reserved,qty_remaining,created_by,created_at,cancelled)
           VALUES (?,?,?,?,?,?,?,?,datetime('now'),0)""",
        [item_id, "Install Team", "2026-05-01", "2026-05-05", "", 3, 3, "admin"],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/qty_adjust",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "id": item_id,
            "action": "remove",
            "amount": 3,
            "use_reservation": True,
            "reservation_id": reservation_id,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        qty_data = _as_response(app, app.preprocess_request() or api_qty_adjust()).get_json()

    assert qty_data["ok"] is True

    db = sqlite3.connect(tenant["db_path"])
    row = db.execute("SELECT qty_remaining, fulfilled FROM item_reservations WHERE id=?", [reservation_id]).fetchone()
    db.close()
    assert row == (0, 1)


def test_quantity_mobile_return_reduces_checked_out_units(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    item_id = db.execute(
        "INSERT INTO items (name,category_id,qty,qty_out,active,created_at) VALUES (?,?,?,?,1,datetime('now'))",
        ["Mobile Qty Return", 1, 10, 4],
    ).lastrowid
    db.commit()
    db.close()

    with app.test_request_context(
        "/api/qty_adjust",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"id": item_id, "action": "return", "amount": 3, "note": "Mobile check-in"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or api_qty_adjust())
        data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["qty"] == 10
    assert data["qty_out"] == 1


def test_viewer_cannot_add_items(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            "viewer@example.com",
            hash_pw("Password1!"),
            "viewer",
            "",
            "viewer@example.com",
            1,
            0,
        ],
    )
    db.commit()
    db.close()

    response, saved_session = _login(app, tenant, email="viewer@example.com", password="Password1!")
    assert response.status_code == 302

    with app.test_request_context(
        "/api/item/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"name": "Should Not Save", "category_id": 1},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        add_response = _as_response(app, app.preprocess_request() or api_item_add())
        add_data = add_response.get_json()

    assert add_response.status_code == 403
    assert add_data["ok"] is False
    assert "Permission denied" in add_data["msg"]


def test_public_demo_request_skips_csrf_and_bare_domain_landing(app):
    client = app.test_client()

    response = client.post(
        "/api/demo-request",
        base_url="http://countdepot.com",
        json={
            "name": "Fahri Pece",
            "company": "DSSIT",
            "email": "fpece@dssitny.com",
            "size": "1-10 people",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    from app.platform import get_platform_db

    db = get_platform_db()
    row = db.execute(
        "SELECT name, company, email, team_size FROM demo_requests WHERE email=?",
        ["fpece@dssitny.com"],
    ).fetchone()
    db.close()

    assert tuple(row) == ("Fahri Pece", "DSSIT", "fpece@dssitny.com", "1-10 people")


def test_homepage_has_core_seo_meta_tags(app):
    response = app.test_client().get("/", base_url="http://countdepot.com")
    body = response.get_data(as_text=True)
    title = "CountDepot - Barcode Inventory Management Software for Any Team"
    description = (
        "Barcode inventory management software for tracking assets, scanning items, "
        "managing reservations, and monitoring stock by site. Start a 30-day free trial."
    )

    assert response.status_code == 200
    assert f"<title>{title}</title>" in body
    assert body.count("<title>") == 1
    assert f'<meta name="description" content="{description}">' in body
    assert '<link rel="canonical" href="https://countdepot.com/">' in body
    assert f'<meta property="og:title" content="{title}">' in body
    assert f'<meta property="og:description" content="{description}">' in body
    assert '<meta property="og:url" content="https://countdepot.com/">' in body
    assert f'<meta name="twitter:title" content="{title}">' in body


def test_homepage_has_schema_internal_links_and_marketing_events(app):
    response = app.test_client().get("/", base_url="http://countdepot.com")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '"@type": "Organization"' in body
    assert '"@type": "SoftwareApplication"' in body
    assert '"applicationCategory": "BusinessApplication"' in body
    assert 'aria-label="Industry inventory software"' not in body
    assert 'href="/inventory-management-for-repair-shops"' not in body
    assert 'href="/inventory-software-for-nonprofits"' not in body
    assert 'href="/inventory-tracking-for-veterinary-clinics"' not in body
    assert "signup_click" in body
    assert "pricing_click" in body
    assert "demo_request" in body
    assert "seo_internal_link_click" not in body


def test_signup_page_has_seo_meta_and_submit_event(app):
    response = app.test_client().get("/signup", base_url="http://countdepot.com")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<title>Start Free Inventory Management Trial | CountDepot</title>" in body
    assert body.count("<title>") == 1
    assert (
        '<meta name="description" content="Create your CountDepot workspace in under a minute. '
        'Start a 30-day free inventory management trial with barcode scanning and no credit card required.">'
    ) in body
    assert '<link rel="canonical" href="https://countdepot.com/signup">' in body
    assert "signup_submit" in body
    assert 'name="form_started"' in body
    assert 'name="website"' in body


def test_signup_blocks_honeypot_bot_submission(app):
    client = app.test_client()
    get_response = client.get("/signup", base_url="http://countdepot.com")
    body = get_response.get_data(as_text=True)
    match = re.search(r'name="form_started" value="(\d+)"', body)
    csrf_match = re.search(r'name="csrf_token" value="([a-f0-9]+)"', body)
    assert match is not None
    assert csrf_match is not None

    response = client.post(
        "/signup",
        base_url="http://countdepot.com",
        data={
            "csrf_token": csrf_match.group(1),
            "form_started": match.group(1),
            "website": "https://spam.example",
            "name": "Bot Company",
            "slug": "bot-company",
            "email": "bot@example.com",
            "password": "Password1!",
        },
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "We could not process that signup. Please try again." in html


def test_signup_rejects_disposable_email_domains(app):
    client = app.test_client()
    get_response = client.get("/signup", base_url="http://countdepot.com")
    body = get_response.get_data(as_text=True)
    match = re.search(r'name="form_started" value="(\d+)"', body)
    csrf_match = re.search(r'name="csrf_token" value="([a-f0-9]+)"', body)
    assert match is not None
    assert csrf_match is not None
    form_started = str(int(match.group(1)) - 10)

    response = client.post(
        "/signup",
        base_url="http://countdepot.com",
        data={
            "csrf_token": csrf_match.group(1),
            "form_started": form_started,
            "website": "",
            "name": "Temp Mail Co",
            "slug": "temp-mail-co",
            "email": "temp@mailinator.com",
            "password": "Password1!",
        },
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Please use your work email address." in html


def test_www_countdepot_redirects_to_apex(app):
    response = app.test_client().get(
        "/inventory-management-for-repair-shops?utm_source=test",
        base_url="https://www.countdepot.com",
    )

    assert response.status_code == 301
    assert response.headers["Location"] == (
        "https://countdepot.com/inventory-management-for-repair-shops?utm_source=test"
    )


def test_public_seo_pages_render_on_bare_domain(app):
    from app.seo_pages import SEO_PAGES

    client = app.test_client()

    for slug, page in SEO_PAGES.items():
        response = client.get(f"/{slug}", base_url="http://countdepot.com")
        body = response.get_data(as_text=True)
        assert response.status_code == 200, slug
        assert page["keyword"] in body
        assert page["title"] in body
        assert f'href="http://countdepot.com/{slug}"' in body
        assert "Start free trial" in body


def test_resources_hub_renders_on_bare_domain(app):
    response = app.test_client().get("/resources", base_url="http://countdepot.com")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "CountDepot Resources" in body
    assert "Real buyer guides, not fake filler pages." in body
    assert 'href="/resources/inventory-management-software-vs-spreadsheets"' in body
    assert '"@type": "CollectionPage"' in body
    assert '"@type": "ItemList"' in body
    assert body.count('href="/resources/') >= 6


def test_resource_article_renders_on_bare_domain(app):
    response = app.test_client().get(
        "/resources/inventory-management-software-vs-spreadsheets",
        base_url="http://countdepot.com",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Inventory management software vs. spreadsheets" in body
    assert "Where spreadsheets usually break" in body
    assert "How buyers should compare the options" in body
    assert '<link rel="canonical" href="http://countdepot.com/resources/inventory-management-software-vs-spreadsheets">' in body
    assert '"@type": "Article"' in body
    assert '"@type": "BreadcrumbList"' in body
    assert "Related resources" in body
    assert 'href="/resources/what-to-look-for-in-inventory-management-software"' in body


def test_inventory_management_resource_article_renders_on_bare_domain(app):
    response = app.test_client().get(
        "/resources/inventory-management",
        base_url="http://countdepot.com",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Inventory management: what it is, how it works, and how to improve it" in body
    assert "What inventory management actually means" in body
    assert "How to improve inventory management" in body
    assert '<link rel="canonical" href="http://countdepot.com/resources/inventory-management">' in body
    assert '"@type": "Article"' in body
    assert 'href="/resources/what-to-look-for-in-inventory-management-software"' in body


def test_homepage_links_to_inventory_management_resource(app):
    response = app.test_client().get("/", base_url="http://countdepot.com")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'href="/resources/inventory-management"' in body
    assert "Inventory management: what it is, how it works, and how to improve it" in body


def test_seo_landing_links_to_inventory_management_resource(app):
    response = app.test_client().get(
        "/inventory-management-for-repair-shops",
        base_url="http://countdepot.com",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'href="/resources/inventory-management"' in body
    assert "Inventory management guide" in body


def test_public_marketing_pages_do_not_render_img_tags_without_alt(app):
    client = app.test_client()
    for path in (
        "/",
        "/resources",
        "/resources/inventory-management-software-vs-spreadsheets",
        "/inventory-management-for-repair-shops",
    ):
        body = client.get(path, base_url="http://countdepot.com").get_data(as_text=True)
        assert "<img" not in body, path


def test_repair_shop_seo_page_has_phase_two_content(app):
    client = app.test_client()

    response = client.get(
        "/inventory-management-for-repair-shops",
        base_url="http://countdepot.com",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "repair shops workflow" in body
    assert "How inventory management for repair shops works in a real team." in body
    assert "Reserve inventory before a job starts" in body
    assert "Phone repair shop tracking screens" in body
    assert "Questions repair shops ask before replacing spreadsheets." in body
    assert '"@type": "FAQPage"' in body
    assert '"@type": "Review"' not in body
    assert "Can CountDepot track both repair parts and tools?" in body


def test_generic_seo_pages_do_not_show_repair_shop_phase_two_content(app):
    response = app.test_client().get(
        "/inventory-software-for-nonprofits",
        base_url="http://countdepot.com",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "repair shops workflow" not in body
    assert "SOPs and team docs" in body
    assert '"@type": "FAQPage"' in body
    assert "Questions nonprofits ask before replacing spreadsheets." in body
    assert "Can CountDepot track both serialized items and quantity-based inventory?" in body
    assert "Phone repair shop tracking screens" not in body


def test_google_analytics_tag_is_present_once_on_public_and_app_pages(app, tenant):
    client = app.test_client()
    tag_src = "https://www.googletagmanager.com/gtag/js?id=G-Z7G78GLGDQ"

    landing = client.get("/", base_url="http://countdepot.com")
    assert landing.status_code == 200
    assert landing.get_data(as_text=True).count(tag_src) == 1

    seo = client.get("/inventory-management-for-repair-shops", base_url="http://countdepot.com")
    assert seo.status_code == 200
    assert seo.get_data(as_text=True).count(tag_src) == 1

    signup = client.get("/signup", base_url="http://countdepot.com")
    assert signup.status_code == 200
    assert signup.get_data(as_text=True).count(tag_src) == 1

    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302
    with app.test_request_context(
        "/mobile",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        mobile = _as_response(app, app.preprocess_request() or app.dispatch_request())

    assert mobile.status_code == 200
    assert mobile.get_data(as_text=True).count(tag_src) == 1


def test_landing_page_promotes_available_and_planned_integrations(app):
    response = app.test_client().get("/", base_url="http://countdepot.com")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '<nav class="site-nav">' in body
    assert '<a href="#features">Features</a>' in body
    assert '<a href="#how">How it works</a>' in body
    assert '<a href="/resources">Resources</a>' in body
    assert '<a href="#pricing">Pricing</a>' in body
    assert '<div class="nav-actions">\n    <a href="#signin" class="nav-link-signin">Sign in</a>\n    <a href="/signup" class="nav-cta">Start free trial</a>' in body
    assert "\nnav{" not in body
    assert "footer-seo" not in body
    assert "Repair shops" not in body
    assert "Veterinary clinics" not in body
    assert 'id="integrations"' in body
    assert "Available now" in body
    assert "Expanded connector catalog" in body
    assert "Amazon Business" in body
    assert "QuickBooks Online" in body
    assert "Shopify" in body
    assert "SOPs &amp; Team Docs" in body
    assert "WooCommerce" in body
    assert "Avalara tax" in body
    assert "Grainger cXML" in body


def test_admin_can_configure_expanded_integration_catalog(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/api/integrations/catalog",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        catalog = _as_response(app, app.preprocess_request() or api_integrations_catalog())

    data = catalog.get_json()
    assert catalog.status_code == 200
    assert data["ok"] is True
    names = {connector["name"] for group in data["groups"] for connector in group["connectors"]}
    assert {"WooCommerce", "Etsy", "EasyPost", "Stripe", "Avalara", "Zapier", "Twilio SMS"} <= names

    with app.test_request_context(
        "/api/integrations/catalog/woocommerce",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "enabled": True,
            "fields": {
                "store_url": "https://store.example.com",
                "consumer_key": "ck_test_123",
                "consumer_secret": "cs_test_456",
            },
            "notes": "Priority ecommerce connector",
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        saved = _as_response(app, app.preprocess_request() or api_integrations_catalog_save("woocommerce"))

    assert saved.status_code == 200
    assert saved.get_json()["configured"] is True
    assert saved.get_json()["enabled"] is True

    with app.test_request_context(
        "/api/integrations/catalog/woocommerce/test",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        tested = _as_response(app, app.preprocess_request() or api_integrations_catalog_test("woocommerce"))

    assert tested.status_code == 200
    assert tested.get_json()["ok"] is True


def test_integrations_page_uses_single_page_scroll_layout(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/integrations",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or integrations_page())

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'class="integrations-page"' in body
    assert "integrations-main-scroll" in body
    assert "flex:1;overflow-y:auto;padding:20px 24px" not in body


def test_docs_page_and_api_support_permissioned_sops(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/docs",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        page = _as_response(app, app.preprocess_request() or docs_page())

    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "SOPs &amp; Docs" in body
    assert "New SOP" in body
    assert '<div class="overlay" id="docModal">' in body
    assert '<div class="overlay" id="docCategoryModal">' in body
    assert '<div class="modal" id="docModal">' not in body
    assert '<div class="modal" id="docCategoryModal">' not in body

    with app.test_request_context(
        "/api/docs/categories",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        categories = _as_response(app, app.preprocess_request() or api_doc_categories())

    category_data = categories.get_json()
    assert categories.status_code == 200
    assert category_data["ok"] is True
    category_names = {c["name"] for c in category_data["categories"]}
    assert {"Receiving", "Check In / Check Out", "Safety"} <= category_names
    receiving_id = next(c["id"] for c in category_data["categories"] if c["name"] == "Receiving")

    with app.test_request_context(
        "/api/docs/categories",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"name": "Field Work", "description": "Field procedures"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        new_category = _as_response(app, app.preprocess_request() or api_doc_category_add())

    assert new_category.status_code == 200
    assert new_category.get_json()["ok"] is True

    with app.test_request_context(
        "/api/docs",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "category_id": receiving_id,
            "title": "Receiving Serialized Equipment",
            "body": "1. Verify PO\n2. Inspect item\n3. Scan serial\n4. Apply label",
            "status": "published",
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        created = _as_response(app, app.preprocess_request() or api_doc_add())

    assert created.status_code == 200
    created_data = created.get_json()
    assert created_data["ok"] is True
    doc_id = created_data["doc"]["id"]

    with app.test_request_context(
        "/api/docs",
        base_url=f"http://{tenant['host']}",
        method="GET",
        query_string={"q": "serialized"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        listed = _as_response(app, app.preprocess_request() or api_docs_list())

    assert listed.status_code == 200
    assert any(d["id"] == doc_id for d in listed.get_json()["docs"])

    with app.test_request_context(
        f"/api/docs/{doc_id}",
        base_url=f"http://{tenant['host']}",
        method="PUT",
        json={
            "category_id": receiving_id,
            "title": "Receiving Serialized Equipment v2",
            "body": "Updated receiving steps",
            "status": "draft",
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        updated = _as_response(app, app.preprocess_request() or api_doc_update(doc_id))

    assert updated.status_code == 200
    assert updated.get_json()["doc"]["status"] == "draft"

    with app.test_request_context(
        f"/api/docs/{doc_id}",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        fetched = _as_response(app, app.preprocess_request() or api_doc_get(doc_id))

    assert fetched.status_code == 200
    assert fetched.get_json()["doc"]["title"] == "Receiving Serialized Equipment v2"

    with app.test_request_context(
        f"/api/docs/{doc_id}",
        base_url=f"http://{tenant['host']}",
        method="DELETE",
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        deleted = _as_response(app, app.preprocess_request() or api_doc_delete(doc_id))

    assert deleted.status_code == 200
    assert deleted.get_json()["ok"] is True


def test_docs_view_only_permission_blocks_create_and_delete(app, tenant):
    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "INSERT INTO users (username,password,role,permissions,email,email_verified,must_change_password) "
        "VALUES (?,?,?,?,?,1,0)",
        ["docs.viewer@example.com", hash_pw("Password1!"), "worker", "view_docs", "docs.viewer@example.com"],
    )
    db.commit()
    db.close()

    login_response, worker_session = _login(app, tenant, email="docs.viewer@example.com", password="Password1!")
    assert login_response.status_code == 302
    assert worker_session["permissions"] == "view_docs"

    with app.test_request_context(
        "/docs",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(worker_session)
        session["_csrf_token"] = "test-csrf-token"
        page = _as_response(app, app.preprocess_request() or docs_page())

    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "SOPs &amp; Docs" in html
    assert 'onclick="openDocModal()">New SOP' not in html

    with app.test_request_context(
        "/api/docs",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"title": "Blocked", "body": "Should not save"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(worker_session)
        session["_csrf_token"] = "test-csrf-token"
        blocked = _as_response(app, app.preprocess_request() or api_doc_add())

    assert blocked.status_code == 403
    assert blocked.get_json()["msg"] == "Permission denied: write_docs"


def test_zapier_connector_creates_real_outbound_webhook(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/api/integrations/catalog/zapier",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "enabled": True,
            "fields": {"webhook_url": "https://hooks.zapier.com/hooks/catch/123/abc"},
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        saved = _as_response(app, app.preprocess_request() or api_integrations_catalog_save("zapier"))

    assert saved.status_code == 200
    assert saved.get_json()["enabled"] is True

    db = sqlite3.connect(tenant["db_path"])
    row = db.execute(
        "SELECT url, events, enabled FROM webhooks WHERE secret='zapier-connector'"
    ).fetchone()
    db.close()
    assert row is not None
    assert row[0] == "https://hooks.zapier.com/hooks/catch/123/abc"
    assert "item.added" in row[1]
    assert row[2] == 1


def test_twilio_connector_sends_sms_from_notification_pipeline(app, tenant, monkeypatch):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/api/integrations/catalog/twilio",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "enabled": True,
            "fields": {
                "account_sid": "AC123",
                "auth_token": "secret",
                "from_number": "+15550000001",
                "alert_to_number": "+15550000002",
            },
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        saved = _as_response(app, app.preprocess_request() or api_integrations_catalog_save("twilio"))

    assert saved.status_code == 200
    calls = []

    def fake_sms(account_sid, auth_token, from_number, to_number, body):
        calls.append((account_sid, auth_token, from_number, to_number, body))
        return {"sid": "SM123"}

    monkeypatch.setattr("app.messenger.send_twilio_sms", fake_sms)
    with app.test_request_context("/", base_url=f"http://{tenant['host']}"):
        session.update(saved_session)
        app.preprocess_request()
        from app.messenger import notify
        notify("low_stock", "Low Stock: Filters", "Only 2 available", "/inventory")

    assert calls == [("AC123", "secret", "+15550000001", "+15550000002", "Low Stock: Filters\nOnly 2 available\n/inventory")]


def test_woocommerce_deep_connector_lists_item_and_syncs_order(app, tenant, monkeypatch):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/api/integrations/catalog/woocommerce",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "enabled": True,
            "fields": {
                "store_url": "https://store.example.com",
                "consumer_key": "ck_test",
                "consumer_secret": "cs_test",
            },
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        saved = _as_response(app, app.preprocess_request() or api_integrations_catalog_save("woocommerce"))

    assert saved.status_code == 200

    db = sqlite3.connect(tenant["db_path"])
    item_id = db.execute(
        "INSERT INTO items (name,sku,sale_price,qty,active,created_at) VALUES (?,?,?,?,1,datetime('now'))",
        ["Woo Widget", "WOO-001", 19.99, 3],
    ).lastrowid
    db.commit()
    db.close()

    def fake_request(method, path, payload=None, params=None):
        if method == "GET" and path == "products":
            return [{"id": 1}]
        if method == "POST" and path == "products":
            assert payload["sku"] == "WOO-001"
            assert payload["regular_price"] == "19.99"
            return {"id": 222, "permalink": "https://store.example.com/product/woo-widget"}
        if method == "GET" and path == "orders":
            return [{
                "id": 9001,
                "total": "19.99",
                "billing": {"first_name": "Jane", "last_name": "Buyer", "email": "jane@example.com"},
                "line_items": [{"sku": "WOO-001", "product_id": 222, "total": "19.99"}],
            }]
        raise AssertionError((method, path))

    monkeypatch.setattr("app.woocommerce_integration._request", fake_request)

    with app.test_request_context(
        "/api/integrations/woocommerce/test",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        tested = _as_response(app, app.preprocess_request() or api_woocommerce_test())
    assert tested.status_code == 200

    with app.test_request_context(
        "/api/item/woocommerce/list",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"item_id": item_id, "price": 19.99, "quantity": 3},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        listed = _as_response(app, app.preprocess_request() or api_item_woocommerce_list())
    assert listed.status_code == 200
    assert listed.get_json()["remote_id"] == "222"

    with app.test_request_context(
        "/api/integrations/woocommerce/sync-orders",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={"days_back": 30},
        headers={"X-CSRF-Token": "test-csrf-token"},
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        synced = _as_response(app, app.preprocess_request() or api_woocommerce_sync_orders())
    assert synced.status_code == 200
    assert len(synced.get_json()["synced"]) == 1

    db = sqlite3.connect(tenant["db_path"])
    sold = db.execute("SELECT sold, sold_to FROM items WHERE id=?", [item_id]).fetchone()
    ref = db.execute(
        "SELECT remote_id, status FROM integration_refs WHERE provider='woocommerce' AND entity_id=?",
        [item_id],
    ).fetchone()
    db.close()
    assert sold[0] == 1
    assert "WooCommerce #9001" in sold[1]
    assert ref == ("222", "sold")


def test_content_security_policy_allows_google_analytics(app):
    response = app.test_client().get("/", base_url="http://countdepot.com")
    csp = response.headers["Content-Security-Policy"]

    assert "https://www.googletagmanager.com" in csp
    assert "https://www.google-analytics.com" in csp
    assert "https://analytics.google.com" in csp
    assert "https://region1.google-analytics.com" in csp
    assert "https://stats.g.doubleclick.net" in csp


def test_public_sitemap_and_robots_include_seo_pages(app):
    from app.seo_pages import seo_slugs

    client = app.test_client()

    sitemap = client.get("/sitemap.xml", base_url="http://countdepot.com")
    sitemap_body = sitemap.get_data(as_text=True)
    assert sitemap.status_code == 200
    assert "application/xml" in sitemap.headers["Content-Type"]
    for slug in seo_slugs():
        assert f"http://countdepot.com/{slug}" in sitemap_body

    sitemap_alt = client.get("/sitemap", base_url="http://countdepot.com")
    assert sitemap_alt.status_code == 200
    assert "application/xml" in sitemap_alt.headers["Content-Type"]

    robots = client.get("/robots.txt", base_url="http://countdepot.com")
    assert robots.status_code == 200
    assert "Sitemap: http://countdepot.com/sitemap" in robots.get_data(as_text=True)


def test_mobile_app_assets_are_public_on_bare_domain(app):
    client = app.test_client()

    manifest = client.get("/manifest.webmanifest", base_url="http://countdepot.com")
    assert manifest.status_code == 200
    assert manifest.get_json()["short_name"] == "CountDepot"

    sw = client.get("/sw.js", base_url="http://countdepot.com")
    assert sw.status_code == 200
    assert sw.headers["Service-Worker-Allowed"] == "/"
    assert "countdepot-mobile" in sw.get_data(as_text=True)

    icon = client.get("/static/icons/icon.svg", base_url="http://countdepot.com")
    assert icon.status_code == 200
    assert "image/svg" in icon.headers["Content-Type"]


def test_mobile_page_is_scanner_first_and_installable(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/mobile",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '<body class="mobile-page">' in body
    assert "Full CountDepot shortcuts" in body
    assert "body:has(.m-app)" not in body
    assert ".mob-sidebar-overlay{display:flex!important}" not in body
    assert ".sidebar .navlink span:not(.nb){display:none}" in body
    assert ".mob-sidebar .navlink span{display:initial}" in body
    assert "Scan Item Barcode" in body
    assert "All inventory" in body
    assert "/procurement" in body
    assert "Use this like an app" not in body
    assert "beforeinstallprompt" not in body
    assert "installPrompt" not in body
    assert "/static/vendor/zxing-library-0.21.3.min.js?v=3" in body
    assert "ZXing library camera scanner failed" in body
    assert "Scanner startup failed:" in body
    assert "BarcodeDetector" in body
    assert "ZXingBrowser" in body
    assert "frameRate:{ideal:30,max:60}" in body
    assert "focusMode:'continuous'" in body
    assert "When the right code appears, tap Capture" in body
    assert "_toggleTorch" in body
    assert "_refocusCamera" in body
    assert "_captureCameraCode" in body
    assert 'id="camCaptureBtn"' in body
    assert "Camera requires HTTPS" in body
    assert "navigator.serviceWorker.register('/sw.js')" not in body
    assert ".unregister()" in body
    assert "scanAddSerial" in body
    assert "scanAddField('addSerial')" in body
    assert "scanAddField('addSku')" in body
    assert 'id="addSku" placeholder="Scan or type SKU"' in body
    assert "Bulk checkout queue" in body
    assert "submitMobileBulkCheckout" in body
    assert "Sign in or permission required" in body


def test_checkout_page_has_camera_scan_button(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/checkout",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "scanCheckoutWithCamera" in body
    assert "openCameraScanner" in body
    assert "Camera</button>" in body
    assert "Bulk Checkout Queue" in body
    assert "submitBulkCheckout" in body
    assert "bulkModeToggle" in body


def test_add_item_identifier_fields_have_camera_scan_buttons(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/add-item",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "scanIdentifierField('fi-serial')" in body
    assert "scanIdentifierField('fi-sku')" in body
    assert "openCameraScanner(code=>" in body
    assert 'placeholder="Scan or type serial number"' in body
    assert 'placeholder="Scan or type supplier part number"' in body


def test_inventory_clone_modal_has_single_and_bulk_scan_actions(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Scan Single Serial" in body
    assert "Scan Next Bulk Serial" in body
    assert "scanSingleCloneSerial" in body
    assert "scanNextBulkCloneSerial" in body
    assert "Multiple serials entered. Use Create Bulk Clones." in body


def test_admin_page_shows_email_otp_only_for_2fa(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/admin",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Two-Factor Authentication" in body
    assert "Email OTP" in body
    assert "next fresh login" in body
    assert "Authenticator App (TOTP)" not in body
    assert "startTotpSetup" not in body
    assert "/api/user/totp/setup" not in body


def test_demo_request_email_goes_to_platform_admin(app, monkeypatch):
    sent = {}

    def fake_send_email(to, subject, html, text):
        sent.update({"to": to, "subject": subject, "html": html, "text": text})
        return True

    import importlib
    from config import Config
    mailer = importlib.import_module("app.mailer")

    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(Config, "PLATFORM_ADMIN_EMAIL", "admin@countdepot.com", raising=False)
    monkeypatch.setattr(Config, "SMTP_FROM", "noreply@countdepot.com", raising=False)
    monkeypatch.setattr(mailer, "send_email", fake_send_email)

    response = app.test_client().post(
        "/api/demo-request",
        base_url="http://countdepot.com",
        json={
            "name": "Demo Lead",
            "company": "Example Co",
            "email": "lead@example.com",
            "size": "20+ people",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert sent["to"] == "admin@countdepot.com"
    assert sent["subject"] == "New demo request from CountDepot"


def test_demo_request_rejects_missing_company(app):
    response = app.test_client().post(
        "/api/demo-request",
        base_url="http://countdepot.com",
        json={
            "name": "Demo Lead",
            "email": "lead@example.com",
            "size": "20+ people",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_demo_request_escapes_admin_email_html(app, monkeypatch):
    sent = {}

    def fake_send_email(to, subject, html, text):
        sent.update({"to": to, "subject": subject, "html": html, "text": text})
        return True

    import importlib
    from config import Config
    mailer = importlib.import_module("app.mailer")

    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(Config, "PLATFORM_ADMIN_EMAIL", "admin@countdepot.com", raising=False)
    monkeypatch.setattr(mailer, "send_email", fake_send_email)

    response = app.test_client().post(
        "/api/demo-request",
        base_url="http://countdepot.com",
        json={
            "name": "<script>alert(1)</script>",
            "company": "DSSIT & Partners",
            "email": "lead@example.com",
            "size": "20+ people",
        },
    )

    assert response.status_code == 200
    assert "&lt;script&gt;" in sent["html"]
    assert "<script>" not in sent["html"]
    assert "DSSIT &amp; Partners" in sent["html"]


def test_demo_request_is_rate_limited(app):
    client = app.test_client()

    for idx in range(5):
        response = client.post(
            "/api/demo-request",
            base_url="http://countdepot.com",
            environ_base={"REMOTE_ADDR": "203.0.113.5"},
            json={
                "name": f"Demo Lead {idx}",
                "company": "Example Co",
                "email": f"lead{idx}@example.com",
                "size": "20+ people",
            },
        )
        assert response.status_code == 200

    limited = client.post(
        "/api/demo-request",
        base_url="http://countdepot.com",
        environ_base={"REMOTE_ADDR": "203.0.113.5"},
        json={
            "name": "Demo Lead 6",
            "company": "Example Co",
            "email": "lead6@example.com",
            "size": "20+ people",
        },
    )

    assert limited.status_code == 429
    assert limited.get_json()["ok"] is False


def test_core_authenticated_pages_render(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    paths = [
        "/",
        "/add-item",
        "/checkout",
        "/categories",
        "/checked-out",
        "/sites",
        "/low-stock",
        "/audit",
        "/admin",
        "/contacts",
        "/todo",
        "/products",
        "/report/financial",
        "/report/inventory",
        "/report/activity",
        "/report/user-activity",
        "/report/checkout-history",
        "/report/import-history",
        "/report/locations",
        "/report/warranties",
        "/report/depreciation",
        "/report/login-activity",
        "/kits",
        "/integrations",
        "/integrations/accounting",
        "/integrations/ebay",
        "/integrations/shopify",
        "/docs",
        "/procurement",
        "/procurement/catalog",
        "/procurement/analytics",
        "/support",
        "/importer",
        "/reservations",
        "/api-docs",
        "/webhooks",
        "/billing",
        "/mobile",
    ]

    for path in paths:
        with app.test_request_context(
            path,
            base_url=f"http://{tenant['host']}",
            method="GET",
        ):
            session.update(saved_session)
            session["_csrf_token"] = "test-csrf-token"
            response = _as_response(app, app.preprocess_request() or app.dispatch_request())

        assert response.status_code == 200, path


def test_accounting_page_uses_existing_disconnect_routes(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/integrations/accounting",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        session["_csrf_token"] = "test-csrf-token"
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "/api/integrations/accounting/qb/disconnect" in body
    assert "/api/integrations/accounting/xero/disconnect" in body
    assert "/api/integrations/accounting/zoho/disconnect" in body
    assert "/api/integrations/accounting/${key}/disconnect" not in body
    assert "Zoho Books" in body
    assert "zoho-vendor-id" in body


def test_platform_dashboard_renders_owner_metrics(app):
    from app.platform import get_platform_db
    from app.blueprints.platform import SESSION_KEY

    db = get_platform_db()
    db.execute(
        "INSERT INTO tenants (slug,name,plan,sector,onboarded,active,created_at,owner_email,subscription_status,trial_ends_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,datetime('now','+3 days'))",
        ["trialco", "Trial Co", "starter", "", 1, 1, "2026-04-01 00:00:00", "trial@example.com", "trial"],
    )
    db.execute(
        "INSERT INTO tenants (slug,name,plan,sector,onboarded,active,created_at,owner_email,subscription_status,trial_ends_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        ["payco", "Pay Co", "pro", "", 1, 1, "2026-04-01 00:00:00", "pay@example.com", "active"],
    )
    db.execute(
        "INSERT INTO tenants (slug,name,plan,sector,onboarded,active,created_at,owner_email,subscription_status,trial_ends_at,free_access,notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            "freeco",
            "Free Co",
            "pro",
            "",
            1,
            1,
            "2026-04-01 00:00:00",
            "free@example.com",
            "active",
            None,
            1,
            "Lifetime access",
        ],
    )
    db.execute(
        "INSERT INTO tenants (slug,name,plan,sector,onboarded,active,created_at,owner_email,subscription_status,trial_ends_at,cancellation_reason,cancelled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ["churnco", "Churn Co", "starter", "", 1, 1, "2026-04-01 00:00:00", "churn@example.com", "cancelled", None, "Too expensive", "2026-04-10 00:00:00"],
    )
    db.execute(
        "INSERT INTO tenants (slug,name,plan,sector,onboarded,active,created_at,owner_email,subscription_status,trial_ends_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ["deadtrial", "Dead Trial", "starter", "", 1, 1, "2024-01-01 00:00:00", "dead@example.com", "trial", "2024-02-01 00:00:00"],
    )
    db.commit()
    db.close()

    payco_dir = os.path.join(Config.TENANTS_DIR, "payco")
    os.makedirs(payco_dir, exist_ok=True)
    tenant_db = sqlite3.connect(os.path.join(payco_dir, "inventory.db"))
    tenant_db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, last_login TEXT, session_token TEXT);
        CREATE TABLE login_log (ts TEXT, result TEXT);
        CREATE TABLE items (active INTEGER, sold INTEGER, sold_price REAL, sale_price REAL, cost_price REAL);
        CREATE TABLE checkout_log (checkout_date TEXT);
        CREATE TABLE audit_log (ts TEXT);
    """)
    tenant_db.execute("INSERT INTO users (username,last_login,session_token) VALUES (?,?,?)", ["owner", None, "live"])
    tenant_db.execute("INSERT INTO login_log (ts,result) VALUES (?,?)", ["2026-04-12 09:30:00", "ok_google"])
    tenant_db.execute("INSERT INTO items (active,sold,sold_price,cost_price) VALUES (1,1,100,60)")
    tenant_db.execute("INSERT INTO audit_log (ts) VALUES (?)", ["2026-04-12 10:00:00"])
    tenant_db.commit()
    tenant_db.close()

    with app.test_request_context("/_platform/", base_url="http://localhost:5000"):
        session[SESSION_KEY] = True
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "CEO Command Center" in body
    assert "Estimated MRR" in body
    assert "$50" in body
    assert "$600" in body
    assert "Free/Partner" in body
    assert "2026-04-12" in body
    assert "Expired trial ready for deletion" in body
    assert "Ready for deletion" in body
    assert "Cancellations and churn notes" in body
    assert "Too expensive" in body
