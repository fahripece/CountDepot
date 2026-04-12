"""
Amazon Business / SP-API client for CountDepot.

Authentication:
  Amazon Selling Partner API uses a two-layer auth:
    1. LWA (Login with Amazon) OAuth — exchange refresh_token for access_token
    2. AWS Signature V4 — sign requests with IAM credentials

  Some SP-API endpoints (catalog, pricing) also accept just the LWA token,
  but signing is required for most. We implement both.

Configuration (stored in tenant settings table):
  amz_lwa_client_id       — LWA application client ID
  amz_lwa_client_secret   — LWA application client secret
  amz_lwa_refresh_token   — obtained via OAuth consent flow
  amz_aws_access_key      — AWS IAM access key (if required)
  amz_aws_secret_key      — AWS IAM secret key (if required)
  amz_marketplace_id      — e.g. ATVPDKIKX0DER (US), A2EUQ1WTGCTBG2 (CA)
  amz_seller_id           — merchant ID (for seller-context endpoints)
  amz_business_account_id — Amazon Business buyer account ID

cXML Punch-out:
  Amazon Business supports cXML PunchOut 1.2 via
  https://www.amazon.com/business/api/punchout
  We build the PunchOutSetupRequest, receive the redirect URL,
  and handle the PunchOutOrderMessage on return.
"""

import hashlib
import hmac
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


# ── LWA Token exchange ────────────────────────────────────────────────────────

LWA_TOKEN_URL  = "https://api.amazon.com/auth/o2/token"
LWA_AUTH_URL   = "https://sellercentral.amazon.com/apps/authorize/consent"
SP_API_REGION  = "us-east-1"
SP_API_SERVICE = "execute-api"
SP_API_HOST    = "sellingpartnerapi-na.amazon.com"


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """
    Exchange LWA refresh_token for a short-lived access_token.
    Returns: {"access_token": "...", "expires_in": 3600} or raises ValueError.
    """
    import urllib.request
    payload = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        LWA_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ── AWS Signature V4 ──────────────────────────────────────────────────────────

def _sign_v4(method, url, headers, payload_bytes, access_key, secret_key,
             region=SP_API_REGION, service=SP_API_SERVICE):
    """
    Add AWS Signature V4 headers to the headers dict in-place.
    Returns the signed headers dict.
    """
    parsed   = urllib.parse.urlparse(url)
    host     = parsed.netloc
    path     = parsed.path or "/"
    query    = parsed.query

    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    headers["Host"]         = host
    headers["x-amz-date"]  = amz_date

    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    headers["x-amz-content-sha256"] = payload_hash

    # Canonical headers
    signed_header_keys = sorted(k.lower() for k in headers)
    canonical_headers  = "".join(
        f"{k}:{headers[next(x for x in headers if x.lower()==k)]}\n"
        for k in signed_header_keys
    )
    signed_headers = ";".join(signed_header_keys)

    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(urllib.parse.parse_qsl(query))
    )

    canonical_req = "\n".join([
        method.upper(), path, canonical_qs,
        canonical_headers, signed_headers, payload_hash,
    ])

    cred_scope   = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, cred_scope,
        hashlib.sha256(canonical_req.encode()).hexdigest(),
    ])

    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    signing_key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date_stamp), region), service),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{cred_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


# ── SP-API helpers ────────────────────────────────────────────────────────────

def _sp_get(path, params, access_token, aws_access_key=None, aws_secret_key=None):
    """Perform a signed GET against the SP-API."""
    import urllib.request
    qs  = urllib.parse.urlencode(params)
    url = f"https://{SP_API_HOST}{path}?{qs}" if qs else f"https://{SP_API_HOST}{path}"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type":       "application/json",
    }
    if aws_access_key and aws_secret_key:
        _sign_v4("GET", url, headers, b"", aws_access_key, aws_secret_key)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ── Public API ────────────────────────────────────────────────────────────────

def lookup_prices(keywords_or_skus: list, marketplace_id: str,
                  client_id: str, client_secret: str, refresh_token: str,
                  aws_access_key=None, aws_secret_key=None) -> list:
    """
    Look up catalog items and pricing for a list of keywords/SKUs.
    Returns list of dicts: {query, asin, title, brand, price, currency, url}
    """
    token_data   = get_access_token(client_id, client_secret, refresh_token)
    access_token = token_data["access_token"]
    results = []
    for kw in keywords_or_skus:
        try:
            data = _sp_get(
                "/catalog/2022-04-01/items",
                {"keywords": kw, "marketplaceIds": marketplace_id,
                 "includedData": "summaries,offers"},
                access_token, aws_access_key, aws_secret_key,
            )
            items = data.get("items", [])
            for item in items[:2]:  # top 2 results per query
                summaries = item.get("summaries", [{}])[0]
                offers    = item.get("offers", [{}])[0]
                price     = None
                if offers:
                    listing = offers.get("buyingPrice", {})
                    price   = listing.get("listingPrice", {}).get("amount")
                results.append({
                    "query":    kw,
                    "asin":     item.get("asin"),
                    "title":    summaries.get("itemName"),
                    "brand":    summaries.get("brand"),
                    "price":    float(price) if price else None,
                    "currency": offers.get("buyingPrice", {}).get("listingPrice", {}).get("currencyCode", "USD"),
                    "url":      f"https://www.amazon.com/dp/{item.get('asin')}",
                })
                break  # only first result per query
        except Exception as e:
            results.append({"query": kw, "error": str(e)})
    return results


def sync_orders(marketplace_id: str, client_id: str, client_secret: str,
                refresh_token: str, aws_access_key=None, aws_secret_key=None,
                days_back: int = 90) -> list:
    """
    Fetch recent Amazon Business orders.
    Returns list of order dicts.
    """
    from datetime import timedelta
    token_data   = get_access_token(client_id, client_secret, refresh_token)
    access_token = token_data["access_token"]

    created_after = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    data = _sp_get(
        "/orders/v0/orders",
        {
            "MarketplaceIds": marketplace_id,
            "CreatedAfter":   created_after,
            "OrderStatuses":  "Shipped,Delivered,Unshipped,PartiallyShipped",
        },
        access_token, aws_access_key, aws_secret_key,
    )
    orders = data.get("payload", {}).get("Orders", [])
    result = []
    for o in orders:
        result.append({
            "amazon_order_id":   o.get("AmazonOrderId"),
            "purchase_date":     o.get("PurchaseDate", "")[:10],
            "status":            o.get("OrderStatus"),
            "total_amount":      float(o["OrderTotal"]["Amount"]) if o.get("OrderTotal") else None,
            "currency":          o.get("OrderTotal", {}).get("CurrencyCode"),
            "buyer_email":       o.get("BuyerEmail"),
            "ship_to_city":      o.get("ShippingAddress", {}).get("City"),
            "item_count":        o.get("NumberOfItemsShipped", 0),
        })
    return result


def get_order_items(amazon_order_id: str, client_id: str, client_secret: str,
                    refresh_token: str, aws_access_key=None, aws_secret_key=None) -> list:
    """Fetch line items for a specific Amazon order."""
    token_data   = get_access_token(client_id, client_secret, refresh_token)
    access_token = token_data["access_token"]
    data = _sp_get(
        f"/orders/v0/orders/{amazon_order_id}/orderItems",
        {}, access_token, aws_access_key, aws_secret_key,
    )
    items = data.get("payload", {}).get("OrderItems", [])
    return [{
        "asin":        i.get("ASIN"),
        "title":       i.get("Title"),
        "seller_sku":  i.get("SellerSKU"),
        "qty_ordered": i.get("QuantityOrdered", 0),
        "qty_shipped": i.get("QuantityShipped", 0),
        "unit_price":  float(i["ItemPrice"]["Amount"]) / max(i.get("QuantityOrdered",1),1)
                       if i.get("ItemPrice") else None,
    } for i in items]


# ── cXML Punch-out ────────────────────────────────────────────────────────────

AMAZON_PUNCHOUT_URL = "https://www.amazon.com/business/api/punchout"


def build_punchout_setup_request(return_url: str, buyer_cookie: str,
                                  identity: str, secret: str, from_domain: str,
                                  buyer_id: str, order_id: str) -> str:
    """
    Build a cXML PunchOutSetupRequest XML string.
    """
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ts   = str(int(time.time()))
    shared_secret = hmac.new(
        secret.encode(), f"{identity}{ts}".encode(), hashlib.sha1
    ).hexdigest()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE cXML SYSTEM "http://xml.cXML.org/schemas/cXML/1.2.014/cXML.dtd">
<cXML payloadID="{ts}@{from_domain}" timestamp="{now}">
  <Header>
    <From>
      <Credential domain="{from_domain}">
        <Identity>{identity}</Identity>
      </Credential>
    </From>
    <To>
      <Credential domain="AmazonBusiness">
        <Identity>{buyer_id}</Identity>
      </Credential>
    </To>
    <Sender>
      <Credential domain="{from_domain}">
        <Identity>{identity}</Identity>
        <SharedSecret>{shared_secret}</SharedSecret>
      </Credential>
      <UserAgent>CountDepot/1.0</UserAgent>
    </Sender>
  </Header>
  <Request>
    <PunchOutSetupRequest operation="create">
      <BuyerCookie>{buyer_cookie}</BuyerCookie>
      <Extrinsic name="UserEmail">{buyer_id}</Extrinsic>
      <BrowserFormPost>
        <URL>{return_url}</URL>
      </BrowserFormPost>
    </PunchOutSetupRequest>
  </Request>
</cXML>"""
    return xml


def send_punchout_setup(return_url: str, identity: str, secret: str,
                         from_domain: str, buyer_id: str, order_id: str) -> dict:
    """
    POST a PunchOutSetupRequest to Amazon Business.
    Returns: {"ok": True, "punchout_url": "..."} or {"ok": False, "error": "..."}
    """
    import urllib.request as ureq
    buyer_cookie = f"CountDepot-{order_id}-{int(time.time())}"
    xml_body     = build_punchout_setup_request(
        return_url, buyer_cookie, identity, secret, from_domain, buyer_id, order_id
    )
    try:
        req = ureq.Request(
            AMAZON_PUNCHOUT_URL,
            data=xml_body.encode(),
            headers={
                "Content-Type": "text/xml",
                "Accept":       "text/xml",
            },
        )
        with ureq.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
        root = ET.fromstring(body)
        # PunchOutSetupResponse/StartPage/URL
        url_el = root.find(".//StartPage/URL")
        if url_el is not None and url_el.text:
            return {"ok": True, "punchout_url": url_el.text.strip(),
                    "buyer_cookie": buyer_cookie}
        status = root.find(".//Response/Status")
        code   = status.attrib.get("code", "?") if status is not None else "?"
        text   = (status.text or "").strip() if status is not None else "Unknown error"
        return {"ok": False, "error": f"cXML error {code}: {text}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def parse_punchout_order_message(xml_body: str) -> dict:
    """
    Parse a PunchOutOrderMessage (the cart returned by Amazon on checkout).
    Returns: {"items": [...], "buyer_cookie": "..."}
    """
    root  = ET.fromstring(xml_body)
    bc    = root.findtext(".//BuyerCookie", "")
    items = []
    for item in root.findall(".//ItemIn"):
        qty   = int(item.attrib.get("quantity", 1))
        uom   = item.attrib.get("unit", "EA")
        desc  = item.findtext(".//Description") or item.findtext(".//ShortName", "")
        sku   = item.findtext(".//SupplierPartID", "")
        price = item.findtext(".//UnitPrice/Money")
        curr  = item.find(".//UnitPrice/Money")
        items.append({
            "vendor_sku":  sku,
            "description": desc,
            "qty":         qty,
            "unit_price":  float(price) if price else 0,
            "currency":    curr.attrib.get("currency", "USD") if curr is not None else "USD",
        })
    return {"items": items, "buyer_cookie": bc}
