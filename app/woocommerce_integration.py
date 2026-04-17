import base64
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

from app.db import execute, query
from app.integration_catalog import connector_config, connector_status


PROVIDER = "woocommerce"


def _config():
    status = connector_status(PROVIDER)
    if not status["configured"]:
        missing = ", ".join(status["missing_fields"])
        raise RuntimeError(f"WooCommerce connector is missing required fields: {missing}")
    fields = connector_config(PROVIDER, include_secrets=True)["fields"]
    return {
        "store_url": fields["store_url"].rstrip("/"),
        "consumer_key": fields["consumer_key"],
        "consumer_secret": fields["consumer_secret"],
    }


def _request(method, path, payload=None, params=None):
    cfg = _config()
    params = dict(params or {})
    url = f"{cfg['store_url']}/wp-json/wc/v3/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(payload).encode() if payload is not None else None
    token = base64.b64encode(f"{cfg['consumer_key']}:{cfg['consumer_secret']}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "User-Agent": "CountDepot-WooCommerce/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"WooCommerce {method} {path} failed: {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        raise RuntimeError(f"WooCommerce {method} {path} failed: {e}")


def test_connection():
    return _request("GET", "products", params={"per_page": 1})


def _item_sku(item):
    return item["sku"] or item["serial"] or item["internal_sku"] or f"countdepot-{item['id']}"


def list_item(item_id, price=None, quantity=1, description=None):
    item = query("SELECT * FROM items WHERE id=? AND active=1", [item_id], one=True)
    if not item:
        raise RuntimeError("Item not found")
    ref = query(
        "SELECT * FROM integration_refs WHERE provider=? AND entity_type='item' AND entity_id=?",
        [PROVIDER, item_id],
        one=True,
    )
    if ref and ref["remote_id"]:
        raise RuntimeError("Item is already linked to WooCommerce")

    price = price if price is not None else item["sale_price"]
    if price in (None, "") or float(price) <= 0:
        raise RuntimeError("A WooCommerce listing price is required")

    payload = {
        "name": item["name"],
        "type": "simple",
        "regular_price": f"{float(price):.2f}",
        "sku": _item_sku(item),
        "manage_stock": True,
        "stock_quantity": int(quantity or item["qty"] or 1),
        "description": description or item["notes"] or item["name"],
    }
    result = _request("POST", "products", payload=payload)
    remote_id = str(result.get("id") or "")
    remote_url = result.get("permalink") or ""
    _save_ref("item", item_id, remote_id, remote_url, "listed", f"Listed at ${float(price):.2f}")
    return {"remote_id": remote_id, "remote_url": remote_url, "raw": result}


def sync_orders(days_back=30):
    orders = _request(
        "GET",
        "orders",
        params={
            "status": "processing,completed",
            "per_page": 100,
            "after": _iso_days_ago(days_back),
        },
    )
    synced = []
    skipped = 0
    for order in orders if isinstance(orders, list) else []:
        order_id = str(order.get("id") or "")
        for line in order.get("line_items", []):
            sku = line.get("sku") or ""
            if not sku:
                skipped += 1
                continue
            item = query(
                "SELECT * FROM items WHERE active=1 AND sold=0 AND (sku=? OR serial=? OR internal_sku=?)",
                [sku, sku, sku],
                one=True,
            )
            if not item:
                skipped += 1
                continue
            total = float(line.get("total") or order.get("total") or item["sale_price"] or 0)
            buyer = _buyer_name(order)
            now = datetime.now().strftime("%Y-%m-%d")
            execute(
                "UPDATE items SET sold=1, sold_date=?, sold_price=?, sold_to=?, checked_out=0 WHERE id=?",
                [now, total, f"{buyer} (WooCommerce #{order_id})", item["id"]],
            )
            execute("DELETE FROM tasks WHERE item_id=?", [item["id"]])
            _save_ref("item", item["id"], str(line.get("product_id") or ""), order.get("link") or "", "sold", f"WooCommerce order {order_id}")
            synced.append({"item_id": item["id"], "name": item["name"], "order_id": order_id})
    return {"orders_seen": len(orders) if isinstance(orders, list) else 0, "synced": synced, "skipped": skipped}


def _save_ref(entity_type, entity_id, remote_id, remote_url, status, detail):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute(
        """
        INSERT INTO integration_refs (provider,entity_type,entity_id,remote_id,remote_url,status,detail,synced_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(provider,entity_type,entity_id)
        DO UPDATE SET remote_id=excluded.remote_id, remote_url=excluded.remote_url,
                      status=excluded.status, detail=excluded.detail, synced_at=excluded.synced_at
        """,
        [PROVIDER, entity_type, entity_id, remote_id, remote_url, status, detail, now],
    )


def _iso_days_ago(days_back):
    from datetime import timedelta
    return (datetime.utcnow() - timedelta(days=int(days_back or 30))).isoformat() + "Z"


def _buyer_name(order):
    billing = order.get("billing") or {}
    name = " ".join([billing.get("first_name") or "", billing.get("last_name") or ""]).strip()
    return name or billing.get("email") or "WooCommerce customer"
