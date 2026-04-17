import json
import os
import sqlite3

from flask import session

from config import Config
from app.helpers import PERM_KEYS, hash_pw
from app.blueprints.auth import login_page
from app.blueprints.main import admin_page, categories_page, inventory, products_page
from app.blueprints.api import (
    api_add_reservation,
    api_cat_add,
    api_category_fields_save,
    api_item_add,
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
    api_set_user_locations,
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
    assert "First login guide" in body
    assert "Start with categories" in body
    assert "Create products" in body
    assert "Use sites and locations" in body

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


def test_incomplete_cost_requirement_can_be_disabled(app, tenant):
    login_response, saved_session = _login(app, tenant)
    assert login_response.status_code == 302

    db = sqlite3.connect(tenant["db_path"])
    category_id = db.execute(
        "INSERT INTO categories (name,color,is_expense) VALUES (?,?,0)",
        ["Cost Optional Category", "#ffffff"],
    ).lastrowid
    product_id = db.execute(
        "INSERT INTO products (name,category_id,active,created_at) VALUES (?,?,1,datetime('now'))",
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

    db = sqlite3.connect(tenant["db_path"])
    db.execute(
        "INSERT INTO settings (key,value) VALUES ('incomplete_requires_cost','0')"
    )
    db.commit()
    db.close()

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
    title = "CountDepot - Inventory Management for Any Industry"
    description = (
        "Track assets, scan barcodes, and generate financial reports. "
        "Inventory management that adapts to your industry. "
        "Free tier available, no credit card required."
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
    assert 'aria-label="Industry inventory software"' in body
    assert 'href="/inventory-management-for-repair-shops"' in body
    assert 'href="/inventory-software-for-nonprofits"' in body
    assert 'href="/inventory-tracking-for-veterinary-clinics"' in body
    assert "signup_click" in body
    assert "pricing_click" in body
    assert "demo_request" in body
    assert "seo_internal_link_click" in body


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


def test_repair_shop_seo_page_has_phase_two_content(app):
    client = app.test_client()

    response = client.get(
        "/inventory-management-for-repair-shops",
        base_url="http://countdepot.com",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Repair shop inventory workflow" in body
    assert "From scattered parts lists to a clear repair workflow." in body
    assert "Reserve inventory before a job starts" in body
    assert "Phone repair shop tracking screens" in body
    assert "Questions repair shops ask before replacing spreadsheets." in body
    assert '"@type": "FAQPage"' in body
    assert "Can CountDepot track both repair parts and tools?" in body


def test_generic_seo_pages_do_not_show_repair_shop_phase_two_content(app):
    response = app.test_client().get(
        "/inventory-software-for-nonprofits",
        base_url="http://countdepot.com",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Repair shop inventory workflow" not in body
    assert '"@type": "FAQPage"' not in body


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
    assert "Scan Item Barcode" in body
    assert "Use this like an app" in body
    assert "beforeinstallprompt" in body
    assert "scanAddSerial" in body
    assert "Sign in or permission required" in body


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
