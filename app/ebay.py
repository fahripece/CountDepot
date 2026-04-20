"""
eBay Inventory API integration.

Token storage uses the tenant's settings table (key/value pairs):
  ebay_client_id, ebay_client_secret, ebay_ru_name
  ebay_access_token, ebay_refresh_token, ebay_token_expiry
  ebay_sandbox  (1 = use sandbox environment)

OAuth callback URL:
  /integrations/ebay/callback

Flow:
  1. Admin enters App ID (client_id), Cert ID (client_secret), RuName in settings
  2. User clicks "Connect eBay" → redirected to eBay auth page
  3. eBay redirects back to /integrations/ebay/callback with ?code=...
  4. App exchanges code for access + refresh tokens
  5. When listing an item: ebay_list_item() creates inventory item + offer, publishes it
"""

import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

from app.db import query, execute


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


# ── eBay environment ──────────────────────────────────────────────────────────

EBAY_PROD_BASE    = "https://api.ebay.com"
EBAY_SANDBOX_BASE = "https://api.sandbox.ebay.com"
EBAY_AUTH_PROD    = "https://auth.ebay.com/oauth2/authorize"
EBAY_AUTH_SANDBOX = "https://auth.sandbox.ebay.com/oauth2/authorize"
EBAY_TOKEN_PROD   = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_TOKEN_SANDBOX = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"

EBAY_SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])


def _base_url():
    return EBAY_SANDBOX_BASE if _get_setting("ebay_sandbox", "1") == "1" else EBAY_PROD_BASE


def _token_url():
    return EBAY_TOKEN_SANDBOX if _get_setting("ebay_sandbox", "1") == "1" else EBAY_TOKEN_PROD


def _auth_url_base():
    return EBAY_AUTH_SANDBOX if _get_setting("ebay_sandbox", "1") == "1" else EBAY_AUTH_PROD


# ── Credential access ─────────────────────────────────────────────────────────

def ebay_get_credentials():
    return {
        "client_id":     _get_setting("ebay_client_id", ""),
        "client_secret": _get_setting("ebay_client_secret", ""),
        "ru_name":       _get_setting("ebay_ru_name", ""),
        "access_token":  _get_setting("ebay_access_token", ""),
        "refresh_token": _get_setting("ebay_refresh_token", ""),
        "token_expiry":  _get_setting("ebay_token_expiry", ""),
        "sandbox":       _get_setting("ebay_sandbox", "1"),
        "connected":     bool(_get_setting("ebay_access_token")),
    }


def ebay_auth_url(client_id, ru_name, state=""):
    """Build the eBay OAuth consent URL."""
    params = urllib.parse.urlencode({
        "client_id":     client_id,
        "response_type": "code",
        "redirect_uri":  ru_name,
        "scope":         EBAY_SCOPES,
        "state":         state,
    })
    return f"{_auth_url_base()}?{params}"


def _basic_auth_header(client_id, client_secret):
    import base64
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {creds}"


def ebay_exchange_code(client_id, client_secret, ru_name, code):
    """Exchange auth code for access + refresh tokens."""
    data = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": ru_name,
    }).encode()
    req = urllib.request.Request(
        _token_url(),
        data=data,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def ebay_refresh_access_token(client_id, client_secret, refresh_token):
    """Use refresh token to get a new access token."""
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "scope":         EBAY_SCOPES,
    }).encode()
    req = urllib.request.Request(
        _token_url(),
        data=data,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _get_valid_access_token():
    """Return a valid access token, refreshing if needed. Raises if not connected."""
    creds = ebay_get_credentials()
    if not creds["access_token"]:
        raise RuntimeError("eBay not connected. Set up credentials in Integrations.")
    if not creds["client_id"] or not creds["client_secret"]:
        raise RuntimeError("eBay client credentials missing.")

    expiry = creds.get("token_expiry") or ""
    try:
        exp_dt = datetime.fromisoformat(expiry) if expiry else _utc_now()
        needs_refresh = _utc_now() >= exp_dt - timedelta(minutes=5)
    except Exception:
        needs_refresh = True

    if needs_refresh and creds.get("refresh_token"):
        resp = ebay_refresh_access_token(
            creds["client_id"], creds["client_secret"], creds["refresh_token"])
        access_token = resp["access_token"]
        new_expiry = (_utc_now() + timedelta(seconds=resp.get("expires_in", 7200))).isoformat()
        _set_setting("ebay_access_token", access_token)
        _set_setting("ebay_token_expiry", new_expiry)
        if resp.get("refresh_token"):
            _set_setting("ebay_refresh_token", resp["refresh_token"])
        return access_token

    return creds["access_token"]


def _ebay_request(method, path, body=None):
    """Make an authenticated eBay API request. Returns parsed JSON."""
    token = _get_valid_access_token()
    url   = _base_url() + path
    data  = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Accept":         "application/json",
        "Content-Language": "en-US",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
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
            msg = err_json.get("errors", [{}])[0].get("message", err_body)
        except Exception:
            msg = err_body[:300]
        raise RuntimeError(f"eBay API {e.code}: {msg}") from e


# ── Listing helpers ───────────────────────────────────────────────────────────

def ebay_list_item(item, price, condition="USED_EXCELLENT", quantity=1,
                   title=None, description=None, category_id="175672"):
    """
    List an item on eBay using the Inventory API.
    Returns {"listing_id": offerId, "listing_url": "..."}

    item       — dict from items table (needs id, name, serial, sku)
    price      — float, listing price in USD
    condition  — eBay condition enum (e.g. "NEW", "USED_EXCELLENT", "FOR_PARTS_OR_NOT_WORKING")
    category_id — eBay category ID (default 175672 = "Other Everything Else")
    """
    # Build a unique SKU for eBay (using internal ID + our SKU/serial)
    ebay_sku = f"CD-{item['id']}"
    if item.get("serial"):
        ebay_sku += f"-{item['serial'][:20]}"
    elif item.get("sku"):
        ebay_sku += f"-{item['sku'][:20]}"

    listing_title = (title or item.get("name") or "Item")[:80]
    listing_desc  = description or item.get("notes") or listing_title

    # Step 1: create/update inventory item
    _ebay_request("PUT", f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(ebay_sku)}", {
        "availability": {
            "shipToLocationAvailability": {
                "quantity": quantity
            }
        },
        "condition": condition,
        "product": {
            "title":       listing_title,
            "description": listing_desc,
        },
    })

    # Step 2: create offer
    offer_resp = _ebay_request("POST", "/sell/inventory/v1/offer", {
        "sku":            ebay_sku,
        "marketplaceId":  "EBAY_US",
        "format":         "FIXED_PRICE",
        "listingDescription": listing_desc,
        "pricingSummary": {
            "price": {"value": str(round(price, 2)), "currency": "USD"}
        },
        "categoryId":     str(category_id),
        "listingPolicies": {
            "fulfillmentPolicyId": _get_setting("ebay_fulfillment_policy_id", ""),
            "paymentPolicyId":     _get_setting("ebay_payment_policy_id", ""),
            "returnPolicyId":      _get_setting("ebay_return_policy_id", ""),
        },
    })
    offer_id = offer_resp.get("offerId")
    if not offer_id:
        raise RuntimeError(f"eBay offer creation failed: {offer_resp}")

    # Step 3: publish offer
    pub_resp = _ebay_request("POST", f"/sell/inventory/v1/offer/{offer_id}/publish", {})
    listing_id = pub_resp.get("listingId") or offer_id

    listing_url = f"https://www.ebay.com/itm/{listing_id}" if not _get_setting("ebay_sandbox", "1") == "1" \
        else f"https://sandbox.ebay.com/itm/{listing_id}"

    return {"offer_id": offer_id, "listing_id": listing_id, "listing_url": listing_url}


def ebay_end_listing(offer_id):
    """Withdraw (end) an active eBay listing."""
    _ebay_request("POST", f"/sell/inventory/v1/offer/{offer_id}/withdraw", {})


def ebay_get_offer(offer_id):
    """Get offer details including current status."""
    return _ebay_request("GET", f"/sell/inventory/v1/offer/{offer_id}")


def ebay_get_policies():
    """Fetch fulfillment, payment, and return policies from eBay."""
    ff = _ebay_request("GET", "/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US")
    pm = _ebay_request("GET", "/sell/account/v1/payment_policy?marketplace_id=EBAY_US")
    rt = _ebay_request("GET", "/sell/account/v1/return_policy?marketplace_id=EBAY_US")
    return {
        "fulfillment": ff.get("fulfillmentPolicies", []),
        "payment":     pm.get("paymentPolicies", []),
        "return":      rt.get("returnPolicies", []),
    }
