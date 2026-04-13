"""
Outbound webhook delivery.

Webhooks are stored per-tenant in the webhooks table.
Each webhook has a URL, a comma-separated list of subscribed events,
an optional HMAC secret, and an enabled flag.

Signed delivery:
  If a secret is set, we include an X-CountDepot-Signature header:
    X-CountDepot-Signature: sha256=<hmac_hex>
  Computed as HMAC-SHA256(secret, raw_body).

Supported event types:
  item.added, item.updated, item.deleted, item.checked_out, item.checked_in,
  item.sold, item.retired, low_stock, po.created, po.approved, po.rejected,
  po.received, warranty.expiring

Usage:
    from app.webhooks import fire
    fire("item.added", {"id": 42, "name": "ThinkPad X1"})
"""

import json
import hmac
import hashlib
import threading
import urllib.request
import urllib.error
from datetime import datetime

from app.db import query, execute


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _deliver(webhook_id: int, url: str, secret: str, event_type: str, payload: dict):
    """Deliver a single webhook. Runs in a background thread."""
    now      = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    envelope = {
        "event":      event_type,
        "created_at": now,
        "data":       payload,
    }
    body        = json.dumps(envelope, separators=(",", ":")).encode()
    headers     = {
        "Content-Type":          "application/json",
        "User-Agent":            "CountDepot-Webhooks/1.0",
        "X-CountDepot-Event":    event_type,
        "X-CountDepot-Delivery": f"{webhook_id}-{event_type}-{int(datetime.utcnow().timestamp())}",
    }
    if secret:
        headers["X-CountDepot-Signature"] = _sign(secret, body)

    status_code = None
    error_msg   = None
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_code = resp.status
    except urllib.error.HTTPError as e:
        status_code = e.code
        error_msg   = e.read().decode()[:300]
    except Exception as e:
        error_msg = str(e)[:300]

    try:
        execute(
            "INSERT INTO webhook_log (webhook_id,event_type,payload,status_code,error,delivered_at) "
            "VALUES (?,?,?,?,?,?)",
            [webhook_id, event_type, json.dumps(payload)[:2000],
             status_code, error_msg, now])
    except Exception:
        pass


def fire(event_type: str, payload: dict):
    """
    Fire an event to all matching enabled webhooks for the current tenant.
    Delivery is async — each webhook gets its own thread.
    Silently no-ops if there are no matching webhooks or if called outside a request context.
    """
    try:
        rows = query(
            "SELECT id, url, secret FROM webhooks "
            "WHERE enabled=1 AND (events='' OR events LIKE ? OR events LIKE ? OR events LIKE ? OR events LIKE ?)",
            [event_type, f"{event_type},%", f"%,{event_type}", f"%,{event_type},%"])
        for row in rows:
            t = threading.Thread(
                target=_deliver,
                args=(row["id"], row["url"], row["secret"] or "", event_type, payload),
                daemon=True)
            t.start()
    except Exception:
        pass
