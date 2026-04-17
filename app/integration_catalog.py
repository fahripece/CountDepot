import json
from copy import deepcopy

from app.db import execute, query


CATALOG = [
    {
        "key": "commerce",
        "name": "Commerce and marketplaces",
        "description": "Sales channels that need inventory listing, order import, and sold-status sync.",
        "connectors": [
            {
                "key": "woocommerce",
                "name": "WooCommerce",
                "summary": "WordPress store inventory and paid order sync.",
                "status": "connector_ready",
                "fields": [
                    {"key": "store_url", "label": "Store URL", "type": "url", "required": True},
                    {"key": "consumer_key", "label": "Consumer Key", "type": "password", "required": True},
                    {"key": "consumer_secret", "label": "Consumer Secret", "type": "password", "required": True},
                ],
            },
            {
                "key": "etsy",
                "name": "Etsy",
                "summary": "Marketplace listing and order workflow for Etsy shops.",
                "status": "connector_ready",
                "fields": [
                    {"key": "shop_id", "label": "Shop ID", "type": "text", "required": True},
                    {"key": "api_key", "label": "API Key", "type": "password", "required": True},
                    {"key": "access_token", "label": "Access Token", "type": "password", "required": True},
                ],
            },
            {
                "key": "amazon_seller",
                "name": "Amazon Seller",
                "summary": "Amazon Seller Central inventory and order connector.",
                "status": "connector_ready",
                "fields": [
                    {"key": "seller_id", "label": "Seller ID", "type": "text", "required": True},
                    {"key": "marketplace_id", "label": "Marketplace ID", "type": "text", "required": True},
                    {"key": "refresh_token", "label": "Refresh Token", "type": "password", "required": True},
                ],
            },
            {
                "key": "google_shopping",
                "name": "Google Shopping",
                "summary": "Product feed destination for Google Merchant Center.",
                "status": "connector_ready",
                "fields": [
                    {"key": "merchant_id", "label": "Merchant Center ID", "type": "text", "required": True},
                    {"key": "feed_label", "label": "Feed Label", "type": "text", "required": False},
                ],
            },
        ],
    },
    {
        "key": "shipping",
        "name": "Shipping and fulfillment",
        "description": "Carrier and shipping-platform connections for fulfillment workflows.",
        "connectors": [
            {
                "key": "shipping_carriers",
                "name": "Shipping carriers",
                "summary": "UPS, FedEx, USPS, and DHL account references.",
                "status": "connector_ready",
                "fields": [
                    {"key": "carrier", "label": "Primary Carrier", "type": "text", "required": True},
                    {"key": "account_number", "label": "Account Number", "type": "text", "required": True},
                    {"key": "api_key", "label": "API Key", "type": "password", "required": False},
                ],
            },
            {
                "key": "easypost",
                "name": "EasyPost",
                "summary": "Multi-carrier labels and tracking through EasyPost.",
                "status": "connector_ready",
                "fields": [
                    {"key": "api_key", "label": "API Key", "type": "password", "required": True},
                ],
            },
            {
                "key": "easyship",
                "name": "Easyship",
                "summary": "Shipping-rate and fulfillment platform connector.",
                "status": "connector_ready",
                "fields": [
                    {"key": "api_key", "label": "API Key", "type": "password", "required": True},
                    {"key": "team_id", "label": "Team ID", "type": "text", "required": False},
                ],
            },
        ],
    },
    {
        "key": "payments_tax",
        "name": "Payments and tax",
        "description": "Payment and tax systems commonly paired with inventory and ecommerce workflows.",
        "connectors": [
            {
                "key": "stripe",
                "name": "Stripe",
                "summary": "Payment metadata and product/customer references.",
                "status": "connector_ready",
                "fields": [
                    {"key": "secret_key", "label": "Secret Key", "type": "password", "required": True},
                    {"key": "webhook_secret", "label": "Webhook Secret", "type": "password", "required": False},
                ],
            },
            {
                "key": "paypal",
                "name": "PayPal",
                "summary": "Payment and customer reference connector.",
                "status": "connector_ready",
                "fields": [
                    {"key": "client_id", "label": "Client ID", "type": "text", "required": True},
                    {"key": "client_secret", "label": "Client Secret", "type": "password", "required": True},
                ],
            },
            {
                "key": "avalara",
                "name": "Avalara",
                "summary": "Tax-code and transaction-tax connector.",
                "status": "connector_ready",
                "fields": [
                    {"key": "account_id", "label": "Account ID", "type": "text", "required": True},
                    {"key": "license_key", "label": "License Key", "type": "password", "required": True},
                    {"key": "company_code", "label": "Company Code", "type": "text", "required": True},
                ],
            },
        ],
    },
    {
        "key": "automation",
        "name": "CRM, automation, and messaging",
        "description": "Lead, notification, and automation tools that extend inventory events.",
        "connectors": [
            {
                "key": "crm_sync",
                "name": "CRM sync",
                "summary": "HubSpot, Salesforce, Zoho CRM, or another CRM endpoint.",
                "status": "connector_ready",
                "fields": [
                    {"key": "crm_provider", "label": "CRM Provider", "type": "text", "required": True},
                    {"key": "api_base_url", "label": "API Base URL", "type": "url", "required": False},
                    {"key": "api_token", "label": "API Token", "type": "password", "required": True},
                ],
            },
            {
                "key": "zapier",
                "name": "Zapier",
                "summary": "No-code automation through Zapier catch hooks.",
                "status": "connector_ready",
                "fields": [
                    {"key": "webhook_url", "label": "Zapier Webhook URL", "type": "url", "required": True},
                ],
            },
            {
                "key": "make",
                "name": "Make",
                "summary": "No-code automation through Make webhooks.",
                "status": "connector_ready",
                "fields": [
                    {"key": "webhook_url", "label": "Make Webhook URL", "type": "url", "required": True},
                ],
            },
            {
                "key": "twilio",
                "name": "Twilio SMS",
                "summary": "SMS alerts for low stock, reservations, and overdue items.",
                "status": "connector_ready",
                "fields": [
                    {"key": "account_sid", "label": "Account SID", "type": "text", "required": True},
                    {"key": "auth_token", "label": "Auth Token", "type": "password", "required": True},
                    {"key": "from_number", "label": "From Number", "type": "text", "required": True},
                ],
            },
        ],
    },
]


def _settings_key(connector_key):
    return f"integration_connector:{connector_key}"


def _all_connectors():
    return {connector["key"]: connector for group in CATALOG for connector in group["connectors"]}


def connector_definition(connector_key):
    return _all_connectors().get(connector_key)


def _load_config(connector_key):
    row = query("SELECT value FROM settings WHERE key=?", [_settings_key(connector_key)], one=True)
    if not row or not row["value"]:
        return {"enabled": False, "fields": {}, "notes": ""}
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return {"enabled": False, "fields": {}, "notes": ""}
    return {
        "enabled": bool(data.get("enabled")),
        "fields": data.get("fields") if isinstance(data.get("fields"), dict) else {},
        "notes": data.get("notes") or "",
    }


def _masked_value(value):
    if not value:
        return ""
    raw = str(value)
    if len(raw) <= 4:
        return "****"
    return f"****{raw[-4:]}"


def catalog_with_status():
    groups = deepcopy(CATALOG)
    for group in groups:
        for connector in group["connectors"]:
            config = _load_config(connector["key"])
            fields = {}
            for field in connector["fields"]:
                value = config["fields"].get(field["key"], "")
                fields[field["key"]] = _masked_value(value) if field.get("type") == "password" else value
            required = [f["key"] for f in connector["fields"] if f.get("required")]
            missing = [f for f in required if not config["fields"].get(f)]
            connector["configured"] = not missing
            connector["enabled"] = bool(config["enabled"] and not missing)
            connector["saved_fields"] = fields
            connector["notes"] = config["notes"]
            connector["missing_fields"] = missing
    return groups


def save_connector(connector_key, enabled=False, fields=None, notes=""):
    definition = connector_definition(connector_key)
    if not definition:
        raise KeyError("Unknown connector")

    current = _load_config(connector_key)
    allowed = {field["key"]: field for field in definition["fields"]}
    saved_fields = dict(current["fields"])
    for key, value in (fields or {}).items():
        if key not in allowed:
            continue
        value = str(value or "").strip()
        if not value and allowed[key].get("type") == "password":
            continue
        saved_fields[key] = value

    payload = {
        "enabled": bool(enabled),
        "fields": saved_fields,
        "notes": str(notes or "").strip(),
    }
    execute(
        "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
        [_settings_key(connector_key), json.dumps(payload, sort_keys=True)],
    )
    return payload


def connector_status(connector_key):
    definition = connector_definition(connector_key)
    if not definition:
        raise KeyError("Unknown connector")
    config = _load_config(connector_key)
    missing = [
        field["key"]
        for field in definition["fields"]
        if field.get("required") and not config["fields"].get(field["key"])
    ]
    return {
        "configured": not missing,
        "enabled": bool(config["enabled"] and not missing),
        "missing_fields": missing,
    }
