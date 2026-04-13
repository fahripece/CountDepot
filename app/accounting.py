"""
Accounting integrations: QuickBooks Online and Xero.

Token storage uses the tenant's settings table (key/value pairs):
  qb_access_token, qb_refresh_token, qb_token_expiry, qb_realm_id
  xero_access_token, xero_refresh_token, xero_token_expiry, xero_tenant_id

OAuth callback URLs:
  /integrations/accounting/qb/callback
  /integrations/accounting/xero/callback
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from flask import g, session

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
    now    = datetime.utcnow()
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
        "TxnDate":   (po["created_at"] or datetime.utcnow().isoformat())[:10],
        "DueDate":   po["expected_date"] or (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d"),
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

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
    now    = datetime.utcnow()
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
        "DateString":     (po["created_at"] or datetime.utcnow().isoformat())[:10],
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
    now     = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
