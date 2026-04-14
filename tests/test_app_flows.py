import os
import sqlite3

from flask import session

from config import Config
from app.helpers import hash_pw
from app.blueprints.auth import login_page
from app.blueprints.api import (
    api_add_reservation,
    api_item_add,
    api_items,
    api_product_add,
    api_qty_adjust,
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
