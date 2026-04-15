import os
import sqlite3

from flask import session

from config import Config
from app.helpers import hash_pw
from app.blueprints.auth import login_page
from app.blueprints.api import (
    api_add_reservation,
    api_category_fields_save,
    api_item_add,
    api_items,
    api_product_edit,
    api_product_add,
    api_qty_adjust,
    api_user_add,
    api_user_invite,
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
        "INSERT INTO tenants (slug,name,plan,sector,onboarded,active,created_at,owner_email,subscription_status,trial_ends_at,cancellation_reason,cancelled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ["churnco", "Churn Co", "starter", "", 1, 1, "2026-04-01 00:00:00", "churn@example.com", "cancelled", None, "Too expensive", "2026-04-10 00:00:00"],
    )
    db.commit()
    db.close()

    with app.test_request_context("/_platform/", base_url="http://localhost:5000"):
        session[SESSION_KEY] = True
        response = _as_response(app, app.preprocess_request() or app.dispatch_request())

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "CEO Command Center" in body
    assert "Estimated MRR" in body
    assert "$50" in body
    assert "Cancellations and churn notes" in body
    assert "Too expensive" in body
