import os
import sqlite3

from flask import session

from config import Config
from app.helpers import hash_pw
from app.blueprints.auth import login_page
from app.blueprints.api import api_item_add, api_items, api_product_add


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


def test_login_redirects_to_inventory_for_onboarded_tenant(app, tenant):
    response, saved_session = _login(app, tenant)

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert saved_session["username"] == tenant["email"]
    assert saved_session["role"] == "admin"
    assert saved_session["user_id"]


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

    with app.test_request_context(
        "/api/product/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Roadmap Test Product",
            "category_id": 1,
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

    with app.test_request_context(
        "/api/item/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Roadmap Test Item",
            "product_id": product_data["id"],
            "category_id": 1,
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
        "/api/items?q=Roadmap%20Test&hide_out=0",
        base_url=f"http://{tenant['host']}",
        method="GET",
    ):
        session.update(saved_session)
        inventory_response = _as_response(app, app.preprocess_request() or api_items())
        items = inventory_response.get_json()

    assert inventory_response.status_code == 200
    assert any(item["name"] == "Roadmap Test Item" for item in items)


def test_internal_sku_product_item_is_visible_in_inventory(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    with app.test_request_context(
        "/api/product/add",
        base_url=f"http://{tenant['host']}",
        method="POST",
        json={
            "name": "Internal SKU Visibility Product",
            "category_id": 1,
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
            "category_id": 1,
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
