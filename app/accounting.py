"""
Accounting integrations: QuickBooks Online, Xero, and Zoho Books.

Token storage uses the tenant's settings table (key/value pairs):
  qb_access_token, qb_refresh_token, qb_token_expiry, qb_realm_id
  xero_access_token, xero_refresh_token, xero_token_expiry, xero_tenant_id
  zoho_access_token, zoho_refresh_token, zoho_token_expiry, zoho_organization_id

OAuth callback URLs:
  /integrations/accounting/qb/callback
  /integrations/accounting/xero/callback
  /integrations/accounting/zoho/callback
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import g, session

from app.db import query, execute


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


# ── QuickBooks Online ─────────────────────────────────────────────────────────

QB_AUTH_URL   = "https://appcenter.intuit.com/connect/oauth2"
QB_TOKEN_URL  = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QB_REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
QB_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company"
QB_PROD_BASE    = "https://quickbooks.api.intuit.com/v3/company"


def qb_get_credentials():
    return {
        "client_id":     _get_setting("qb_client_id", ""),
        "client_secret": _get_setting("qb_client_secret", ""),
        "access_token":  _get_setting("qb_access_token", ""),
        "refresh_token": _get_setting("qb_refresh_token", ""),
        "token_expiry":  _get_setting("qb_token_expiry", ""),
        "realm_id":      _get_setting("qb_realm_id", ""),
        "sandbox":       _get_setting("qb_sandbox", "1"),
        "connected":     bool(_get_setting("qb_access_token")),
    }


def qb_auth_url(client_id, redirect_uri, state=""):
    params = urllib.parse.urlencode({
        "client_id":     client_id,
        "response_type": "code",
        "scope":         "com.intuit.quickbooks.accounting",
        "redirect_uri":  redirect_uri,
        "state":         state,
    })
    return f"{QB_AUTH_URL}?{params}"


def qb_exchange_code(client_id, client_secret, code, redirect_uri):
    """Exchange auth code for tokens. Returns dict with access_token, refresh_token, expires_in."""
    import base64
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data  = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(QB_TOKEN_URL, data=data, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/x-www-form-urlencoded",
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def qb_refresh_access_token(client_id, client_secret, refresh_token):
    import base64
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data  = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(QB_TOKEN_URL, data=data, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/x-www-form-urlencoded",
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def qb_ensure_fresh_token():
    """Refresh QB token if within 5 minutes of expiry. Returns current access token or raises."""
    creds  = qb_get_credentials()
    expiry = creds.get("token_expiry", "")
    now    = _utc_now()
    if expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry)
            if (exp_dt - now).total_seconds() < 300:
                resp = qb_refresh_access_token(
                    creds["client_id"], creds["client_secret"], creds["refresh_token"])
                new_expiry = (now + timedelta(seconds=resp["expires_in"])).isoformat()
                _set_setting("qb_access_token",  resp["access_token"])
                _set_setting("qb_refresh_token", resp.get("refresh_token", creds["refresh_token"]))
                _set_setting("qb_token_expiry",  new_expiry)
                return resp["access_token"]
        except Exception:
            pass
    return creds.get("access_token", "")


def _qb_request(method, path, body=None):
    """Make an authenticated QuickBooks API request."""
    token   = qb_ensure_fresh_token()
    creds   = qb_get_credentials()
    base    = QB_SANDBOX_BASE if creds.get("sandbox") == "1" else QB_PROD_BASE
    realm   = creds["realm_id"]
    url     = f"{base}/{realm}{path}?minorversion=65"
    data    = json.dumps(body).encode() if body else None
    req     = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"QB API {method} {path} → {e.code}: {e.read().decode()[:400]}")


def qb_sync_po(po_id):
    """Sync a purchase order to QuickBooks as a Bill. Returns QB Bill ID."""
    po   = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        raise ValueError(f"PO {po_id} not found")
    lines = query("SELECT * FROM po_lines WHERE po_id=?", [po_id])

    # Build Bill payload
    line_items = []
    for idx, line in enumerate(lines):
        line_items.append({
            "Id":          str(idx + 1),
            "LineNum":     idx + 1,
            "Amount":      float(line["unit_cost"] or 0) * int(line["qty_ordered"] or 0),
            "DetailType":  "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": "1"},  # default expense account
                "BillableStatus": "NotBillable",
                "Qty":       int(line["qty_ordered"] or 0),
                "UnitPrice": float(line["unit_cost"] or 0),
            },
            "Description": line["description"] or line["product_name"] or "",
        })

    bill = {
        "VendorRef": {"name": po["vendor"] or "Unknown Vendor"},
        "TxnDate":   (po["created_at"] or _utc_now().isoformat())[:10],
        "DueDate":   po["expected_date"] or (_utc_now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        "DocNumber": po["po_number"] or str(po_id),
        "Line":      line_items,
        "PrivateNote": f"Synced from CountDepot PO {po['po_number']}",
    }

    # Check if already synced (update) or new (create)
    existing_bill_id = po["qb_bill_id"] if "qb_bill_id" in po.keys() else None
    if existing_bill_id:
        # Sparse update
        existing = _qb_request("GET", f"/bill/{existing_bill_id}")
        bill["Id"]       = existing_bill_id
        bill["SyncToken"] = existing["Bill"]["SyncToken"]
        result = _qb_request("POST", "/bill", bill)
        bill_id = result["Bill"]["Id"]
    else:
        result  = _qb_request("POST", "/bill", bill)
        bill_id = result["Bill"]["Id"]

    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE purchase_orders SET qb_bill_id=?, synced_at=? WHERE id=?",
            [bill_id, now, po_id])
    execute(
        "INSERT INTO accounting_sync_log (provider,entity_type,entity_id,remote_id,status,synced_at) "
        "VALUES (?,?,?,?,?,?)",
        ["quickbooks", "purchase_order", po_id, bill_id, "ok", now])
    return bill_id


def qb_disconnect():
    for key in ("qb_access_token", "qb_refresh_token", "qb_token_expiry",
                "qb_realm_id"):
        _set_setting(key, "")


# ── Xero ──────────────────────────────────────────────────────────────────────

XERO_AUTH_URL  = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_API_BASE  = "https://api.xero.com/api.xro/2.0"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"


def xero_get_credentials():
    return {
        "client_id":     _get_setting("xero_client_id", ""),
        "client_secret": _get_setting("xero_client_secret", ""),
        "access_token":  _get_setting("xero_access_token", ""),
        "refresh_token": _get_setting("xero_refresh_token", ""),
        "token_expiry":  _get_setting("xero_token_expiry", ""),
        "tenant_id":     _get_setting("xero_tenant_id", ""),
        "connected":     bool(_get_setting("xero_access_token")),
    }


def xero_auth_url(client_id, redirect_uri, state=""):
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "scope":         "openid profile email accounting.transactions offline_access",
        "state":         state,
    })
    return f"{XERO_AUTH_URL}?{params}"


def _xero_token_request(data_dict, client_id, client_secret):
    import base64
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data  = urllib.parse.urlencode(data_dict).encode()
    req   = urllib.request.Request(XERO_TOKEN_URL, data=data, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def xero_exchange_code(client_id, client_secret, code, redirect_uri):
    return _xero_token_request({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": redirect_uri,
    }, client_id, client_secret)


def xero_refresh_access_token(client_id, client_secret, refresh_token):
    return _xero_token_request({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }, client_id, client_secret)


def xero_ensure_fresh_token():
    creds  = xero_get_credentials()
    expiry = creds.get("token_expiry", "")
    now    = _utc_now()
    if expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry)
            if (exp_dt - now).total_seconds() < 300:
                resp = xero_refresh_access_token(
                    creds["client_id"], creds["client_secret"], creds["refresh_token"])
                new_expiry = (now + timedelta(seconds=resp["expires_in"])).isoformat()
                _set_setting("xero_access_token",  resp["access_token"])
                _set_setting("xero_refresh_token", resp.get("refresh_token", creds["refresh_token"]))
                _set_setting("xero_token_expiry",  new_expiry)
                return resp["access_token"]
        except Exception:
            pass
    return creds.get("access_token", "")


def _xero_request(method, path, body=None):
    token     = xero_ensure_fresh_token()
    tenant_id = _get_setting("xero_tenant_id", "")
    url       = f"{XERO_API_BASE}/{path}"
    data      = json.dumps(body).encode() if body else None
    req       = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization":  f"Bearer {token}",
        "Xero-Tenant-Id": tenant_id,
        "Content-Type":   "application/json",
        "Accept":         "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Xero API {method} {path} → {e.code}: {e.read().decode()[:400]}")


def xero_get_tenants(access_token):
    """Fetch Xero tenant list using a fresh token."""
    req = urllib.request.Request(XERO_CONNECTIONS_URL, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def xero_sync_po(po_id):
    """Sync a PO to Xero as a PurchaseOrder. Returns Xero PO ID."""
    po    = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        raise ValueError(f"PO {po_id} not found")
    lines = query("SELECT * FROM po_lines WHERE po_id=?", [po_id])

    line_items = []
    for line in lines:
        line_items.append({
            "Description": line["description"] or line["product_name"] or "",
            "Quantity":    int(line["qty_ordered"] or 0),
            "UnitAmount":  float(line["unit_cost"] or 0),
            "AccountCode": "300",  # default Purchases account
            "LineAmount":  float(line["unit_cost"] or 0) * int(line["qty_ordered"] or 0),
        })

    xero_po = {
        "Type":           "PURCHASEORDER",
        "Contact":        {"Name": po["vendor"] or "Unknown Vendor"},
        "DateString":     (po["created_at"] or _utc_now().isoformat())[:10],
        "DeliveryDateString": po["expected_date"] or "",
        "PurchaseOrderNumber": po["po_number"] or str(po_id),
        "Reference":      f"CountDepot {po['po_number']}",
        "LineItems":      line_items,
        "Status":         "SUBMITTED",
    }

    existing_xero_id = po["xero_po_id"] if "xero_po_id" in po.keys() else None
    if existing_xero_id:
        xero_po["PurchaseOrderID"] = existing_xero_id
        result = _xero_request("POST", "PurchaseOrders", {"PurchaseOrders": [xero_po]})
    else:
        result = _xero_request("PUT", "PurchaseOrders", {"PurchaseOrders": [xero_po]})

    xero_id = result["PurchaseOrders"][0]["PurchaseOrderID"]
    now     = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE purchase_orders SET xero_po_id=?, synced_at=? WHERE id=?",
            [xero_id, now, po_id])
    execute(
        "INSERT INTO accounting_sync_log (provider,entity_type,entity_id,remote_id,status,synced_at) "
        "VALUES (?,?,?,?,?,?)",
        ["xero", "purchase_order", po_id, xero_id, "ok", now])
    return xero_id


def xero_disconnect():
    for key in ("xero_access_token", "xero_refresh_token", "xero_token_expiry", "xero_tenant_id"):
        _set_setting(key, "")


# Zoho Books

ZOHO_DEFAULT_ACCOUNTS_URL = "https://accounts.zoho.com"
ZOHO_DEFAULT_API_BASE = "https://www.zohoapis.com/books/v3"
ZOHO_SCOPES = (
    "ZohoBooks.settings.READ,"
    "ZohoBooks.purchaseorders.CREATE,"
    "ZohoBooks.purchaseorders.UPDATE,"
    "ZohoBooks.purchaseorders.READ"
)


def _clean_url(value, default):
    value = (value or default or "").strip().rstrip("/")
    if value and not value.startswith(("https://", "http://")):
        value = "https://" + value
    return value or default


def zoho_get_credentials():
    return {
        "client_id":         _get_setting("zoho_client_id", ""),
        "client_secret":     _get_setting("zoho_client_secret", ""),
        "access_token":      _get_setting("zoho_access_token", ""),
        "refresh_token":     _get_setting("zoho_refresh_token", ""),
        "token_expiry":      _get_setting("zoho_token_expiry", ""),
        "organization_id":   _get_setting("zoho_organization_id", ""),
        "organization_name": _get_setting("zoho_organization_name", ""),
        "accounts_url":      _clean_url(_get_setting("zoho_accounts_url", ""), ZOHO_DEFAULT_ACCOUNTS_URL),
        "api_base":          _clean_url(_get_setting("zoho_api_base", ""), ZOHO_DEFAULT_API_BASE),
        "vendor_id":         _get_setting("zoho_vendor_id", ""),
        "item_id":           _get_setting("zoho_item_id", ""),
        "account_id":        _get_setting("zoho_account_id", ""),
        "connected":         bool(_get_setting("zoho_access_token") and _get_setting("zoho_organization_id")),
    }


def zoho_auth_url(client_id, redirect_uri, state=""):
    accounts_url = zoho_get_credentials()["accounts_url"]
    params = urllib.parse.urlencode({
        "scope":         ZOHO_SCOPES,
        "client_id":     client_id,
        "response_type": "code",
        "access_type":   "offline",
        "redirect_uri":  redirect_uri,
        "state":         state,
        "prompt":        "consent",
    })
    return f"{accounts_url}/oauth/v2/auth?{params}"


def _zoho_token_request(data_dict):
    creds = zoho_get_credentials()
    data = urllib.parse.urlencode(data_dict).encode()
    req = urllib.request.Request(
        f"{creds['accounts_url']}/oauth/v2/token",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept":       "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def zoho_exchange_code(client_id, client_secret, code, redirect_uri):
    return _zoho_token_request({
        "grant_type":    "authorization_code",
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "code":          code,
    })


def zoho_refresh_access_token(client_id, client_secret, refresh_token):
    return _zoho_token_request({
        "grant_type":    "refresh_token",
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })


def zoho_ensure_fresh_token():
    creds = zoho_get_credentials()
    expiry = creds.get("token_expiry", "")
    now = _utc_now()
    if expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry)
            if (exp_dt - now).total_seconds() < 300:
                resp = zoho_refresh_access_token(
                    creds["client_id"], creds["client_secret"], creds["refresh_token"])
                new_expiry = (now + timedelta(seconds=int(resp.get("expires_in", 3600)))).isoformat()
                _set_setting("zoho_access_token", resp["access_token"])
                if resp.get("refresh_token"):
                    _set_setting("zoho_refresh_token", resp["refresh_token"])
                _set_setting("zoho_token_expiry", new_expiry)
                return resp["access_token"]
        except Exception:
            pass
    return creds.get("access_token", "")


def _zoho_request(method, path, body=None, include_org=True):
    token = zoho_ensure_fresh_token()
    creds = zoho_get_credentials()
    if not token:
        raise RuntimeError("Zoho Books is not connected")
    params = {}
    if include_org:
        if not creds["organization_id"]:
            raise RuntimeError("Zoho Books organization ID is required")
        params["organization_id"] = creds["organization_id"]
    qs = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{creds['api_base']}/{path.lstrip('/')}{qs}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Zoho API {method} {path} -> {e.code}: {e.read().decode()[:400]}")


def zoho_get_organizations(access_token=None):
    creds = zoho_get_credentials()
    token = access_token or zoho_ensure_fresh_token()
    req = urllib.request.Request(f"{creds['api_base']}/organizations", headers={
        "Authorization": f"Zoho-oauthtoken {token}",
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get("organizations", [])


def zoho_sync_po(po_id):
    """Sync a PO to Zoho Books as a Purchase Order. Returns Zoho purchaseorder_id."""
    po = query("SELECT * FROM purchase_orders WHERE id=?", [po_id], one=True)
    if not po:
        raise ValueError(f"PO {po_id} not found")
    lines = query("SELECT * FROM po_lines WHERE po_id=?", [po_id])
    if not lines:
        raise ValueError("PO has no line items to sync")

    creds = zoho_get_credentials()
    if not creds["vendor_id"]:
        raise RuntimeError("Zoho default vendor ID is required before syncing purchase orders")
    if not creds["item_id"] and not creds["account_id"]:
        raise RuntimeError("Zoho default item ID or expense account ID is required before syncing purchase orders")

    line_items = []
    for line in lines:
        item = {
            "name":        line["description"] or line["product_name"] or "CountDepot line item",
            "description": line["description"] or line["product_name"] or "",
            "quantity":    str(int(line["qty_ordered"] or 0)),
            "rate":        float(line["unit_cost"] or 0),
        }
        if creds["item_id"]:
            item["item_id"] = creds["item_id"]
        if creds["account_id"]:
            item["account_id"] = creds["account_id"]
        line_items.append(item)

    zoho_po = {
        "vendor_id":            creds["vendor_id"],
        "purchaseorder_number": po["po_number"] or str(po_id),
        "date":                 (po["created_at"] or _utc_now().isoformat())[:10],
        "line_items":           line_items,
        "notes":                po["notes"] or f"Synced from CountDepot PO {po['po_number']}",
    }
    if po["expected_date"]:
        zoho_po["delivery_date"] = po["expected_date"]

    existing_zoho_id = po["zoho_po_id"] if "zoho_po_id" in po.keys() else None
    if existing_zoho_id:
        result = _zoho_request("PUT", f"purchaseorders/{existing_zoho_id}", zoho_po)
    else:
        result = _zoho_request("POST", "purchaseorders", zoho_po)

    remote_po = result.get("purchaseorder") or result.get("purchase_order") or {}
    zoho_id = (
        remote_po.get("purchaseorder_id")
        or remote_po.get("purchase_order_id")
        or remote_po.get("id")
    )
    if not zoho_id:
        raise RuntimeError(f"Zoho sync completed but no purchase order ID was returned: {result}")

    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE purchase_orders SET zoho_po_id=?, synced_at=? WHERE id=?",
            [zoho_id, now, po_id])
    execute(
        "INSERT INTO accounting_sync_log (provider,entity_type,entity_id,remote_id,status,synced_at) "
        "VALUES (?,?,?,?,?,?)",
        ["zoho", "purchase_order", po_id, zoho_id, "ok", now])
    return zoho_id


def zoho_disconnect():
    for key in ("zoho_access_token", "zoho_refresh_token", "zoho_token_expiry",
                "zoho_organization_id", "zoho_organization_name"):
        _set_setting(key, "")
