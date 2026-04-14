"""
Shopify Admin API integration.

Token storage uses the tenant's settings table (key/value pairs):
  shopify_shop           – e.g. mystore.myshopify.com
  shopify_access_token   – Admin API access token from Custom App
  shopify_webhook_secret – HMAC secret for verifying incoming webhooks
  shopify_location_id    – Shopify location ID for inventory level tracking

Setup (Custom App — recommended for self-hosted):
  1. In Shopify Admin → Settings → Apps → Develop apps → Create an app
  2. Under "API credentials" → "Admin API access scopes" grant:
       read_products, write_products
       read_inventory, write_inventory
       read_orders, write_orders
  3. Click "Install app" and copy the Admin API access token
  4. Enter the store URL and token in CountDepot → Integrations → Shopify
  5. (Optional) Register a webhook for orders/paid pointing to:
       https://yourdomain.com/integrations/shopify/webhook

Item ↔ Shopify mapping:
  Each CountDepot item can be pushed as a single-variant Shopify product.
  The Shopify product ID and variant ID are stored back on the item row so
  we can update inventory levels and end listings later.
"""

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from app.db import query, execute

SHOPIFY_API_VERSION = "2024-01"


# ── Settings helpers ──────────────────────────────────────────────────────────

def _get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key=?", [key], one=True)
    return row["value"] if row else default


def _set_setting(key, value):
    existing = query("SELECT id FROM settings WHERE key=?", [key], one=True)
    if existing:
        execute("UPDATE settings SET value=? WHERE key=?", [value, key])
    else:
        execute("INSERT INTO settings (key,value) VALUES (?,?)", [key, value])


# ── Credential access ─────────────────────────────────────────────────────────

def shopify_get_credentials():
    shop  = _get_setting("shopify_shop", "")
    token = _get_setting("shopify_access_token", "")
    return {
        "shop":            shop,
        "access_token":    token,
        "webhook_secret":  _get_setting("shopify_webhook_secret", ""),
        "location_id":     _get_setting("shopify_location_id", ""),
        "connected":       bool(shop and token),
    }


def _base_url():
    shop = _get_setting("shopify_shop", "").strip().rstrip("/")
    if not shop:
        raise RuntimeError("Shopify store URL not configured.")
    if not shop.startswith("http"):
        shop = f"https://{shop}"
    return f"{shop}/admin/api/{SHOPIFY_API_VERSION}"


def _shopify_request(method, path, body=None):
    """Make an authenticated Shopify Admin API request. Returns parsed JSON."""
    token = _get_setting("shopify_access_token", "")
    if not token:
        raise RuntimeError("Shopify not connected. Enter your access token in Integrations.")
    url  = _base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type":           "application/json",
        "Accept":                 "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        try:
            err_json = json.loads(err_body)
            errors   = err_json.get("errors") or err_json.get("error") or err_body
            msg = errors if isinstance(errors, str) else json.dumps(errors)
        except Exception:
            msg = err_body[:300]
        raise RuntimeError(f"Shopify API {e.code}: {msg}") from e


# ── Locations ─────────────────────────────────────────────────────────────────

def shopify_get_locations():
    """Return list of Shopify locations [{id, name, active}]."""
    resp = _shopify_request("GET", "/locations.json")
    return [
        {"id": str(loc["id"]), "name": loc["name"], "active": loc.get("active", True)}
        for loc in resp.get("locations", [])
    ]


# ── Product / listing ─────────────────────────────────────────────────────────

def shopify_list_item(item, price, title=None, description=None,
                      quantity=1, vendor=None, product_type=None):
    """
    Create a Shopify product for a CountDepot item.

    Returns {"product_id": str, "variant_id": str, "listing_url": str}

    item  – dict from items table
    price – float, listing price
    """
    shop = _get_setting("shopify_shop", "").strip().rstrip("/").replace("https://", "").replace("http://", "")

    listing_title = (title or item.get("name") or "Item")[:255]
    listing_body  = description or item.get("notes") or ""
    sku           = item.get("serial") or item.get("sku") or item.get("internal_sku") or f"CD-{item['id']}"

    payload = {
        "product": {
            "title":        listing_title,
            "body_html":    listing_body,
            "vendor":       vendor or "CountDepot",
            "product_type": product_type or "",
            "status":       "active",
            "variants": [{
                "price":            str(round(price, 2)),
                "sku":              sku,
                "inventory_policy": "deny",
                "inventory_management": "shopify",
                "fulfillment_service": "manual",
                "requires_shipping": True,
                "taxable":          True,
            }],
        }
    }

    resp       = _shopify_request("POST", "/products.json", payload)
    product    = resp.get("product", {})
    product_id = str(product.get("id", ""))
    variant    = product.get("variants", [{}])[0]
    variant_id = str(variant.get("id", ""))

    if not product_id:
        raise RuntimeError(f"Shopify product creation failed: {resp}")

    # Set inventory level at the configured location
    location_id = _get_setting("shopify_location_id", "")
    inventory_item_id = str(variant.get("inventory_item_id", ""))
    if location_id and inventory_item_id:
        try:
            _shopify_request("POST", "/inventory_levels/set.json", {
                "location_id":         int(location_id),
                "inventory_item_id":   int(inventory_item_id),
                "available":           quantity,
            })
        except Exception:
            pass  # non-fatal: listing exists, inventory level may need manual set

    listing_url = f"https://{shop}/products/{product.get('handle', product_id)}"
    return {"product_id": product_id, "variant_id": variant_id, "listing_url": listing_url}


def shopify_update_inventory(variant_id, quantity):
    """Update inventory level for a variant at the configured location."""
    location_id = _get_setting("shopify_location_id", "")
    if not location_id:
        raise RuntimeError("No Shopify location configured.")
    # Look up inventory_item_id from the variant
    resp = _shopify_request("GET", f"/variants/{variant_id}.json")
    inv_item_id = str(resp.get("variant", {}).get("inventory_item_id", ""))
    if not inv_item_id:
        raise RuntimeError("Could not find inventory_item_id for variant.")
    _shopify_request("POST", "/inventory_levels/set.json", {
        "location_id":       int(location_id),
        "inventory_item_id": int(inv_item_id),
        "available":         quantity,
    })


def shopify_end_listing(product_id):
    """Unpublish (archive) a Shopify product so it's no longer visible in the store."""
    _shopify_request("PUT", f"/products/{product_id}.json", {
        "product": {"id": int(product_id), "status": "archived"}
    })


def shopify_delete_product(product_id):
    """Permanently delete a Shopify product."""
    _shopify_request("DELETE", f"/products/{product_id}.json")


# ── Order sync ────────────────────────────────────────────────────────────────

def shopify_pull_orders(since_days=30):
    """
    Fetch paid Shopify orders from the last N days.
    Returns a list of dicts: {order_id, order_name, variant_id, quantity, price, buyer, created_at}
    """
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp  = _shopify_request(
        "GET",
        f"/orders.json?status=any&financial_status=paid&created_at_min={urllib.parse.quote(since)}&limit=250"
    )
    orders = resp.get("orders", [])
    results = []
    for order in orders:
        buyer = (
            (order.get("billing_address") or {}).get("name")
            or order.get("email")
            or "unknown"
        )
        for line in order.get("line_items", []):
            results.append({
                "order_id":   str(order["id"]),
                "order_name": order.get("name", ""),
                "variant_id": str(line.get("variant_id") or ""),
                "quantity":   line.get("quantity", 1),
                "price":      float(line.get("price", 0)),
                "buyer":      buyer,
                "created_at": order.get("created_at", ""),
            })
    return results


# ── Webhook verification ───────────────────────────────────────────────────────

def verify_shopify_webhook(raw_body: bytes, hmac_header: str) -> bool:
    """Return True if the webhook signature is valid."""
    secret = _get_setting("shopify_webhook_secret", "")
    if not secret:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    import base64
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header or "")
