"""
Slack and Microsoft Teams webhook notifications.

Messages are sent via incoming webhooks — no OAuth, no SDK needed.
Webhook URLs are stored in the tenant settings table.

Usage:
    from app.messenger import notify
    notify("low_stock", title="Low Stock: ThinkPad X1",
           body="Only 2 available (threshold 5)", link="/inventory")
"""

import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from app.db import query


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Settings helpers ──────────────────────────────────────────────────────────

def _get_setting(key, default=""):
    row = query("SELECT value FROM settings WHERE key=?", [key], one=True)
    return row["value"] if row else default


# ── Slack ─────────────────────────────────────────────────────────────────────

SLACK_COLORS = {
    "low_stock":       "#b91c1c",
    "overdue_checkout":"#d97706",
    "po_submitted":    "#7c3aed",
    "po_approved":     "#15803d",
    "po_rejected":     "#b91c1c",
    "warranty_expiry": "#d97706",
    "default":         "#64748b",
}

SLACK_ICONS = {
    "low_stock":       ":package:",
    "overdue_checkout":":outbox_tray:",
    "po_submitted":    ":clipboard:",
    "po_approved":     ":white_check_mark:",
    "po_rejected":     ":x:",
    "warranty_expiry": ":shield:",
    "default":         ":bell:",
}


def send_slack(webhook_url, event_type, title, body=None, link=None):
    """Post a Slack Block Kit message to a webhook URL."""
    color = SLACK_COLORS.get(event_type, SLACK_COLORS["default"])
    icon  = SLACK_ICONS.get(event_type, SLACK_ICONS["default"])
    text  = title
    if body:
        text += f"\n{body}"

    payload = {
        "attachments": [{
            "color":  color,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{icon} *{title}*" + (f"\n{body}" if body else ""),
                    },
                    **({"accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View →"},
                        "url":  link,
                    }} if link else {}),
                },
                {
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": f"CountDepot · {_utc_now().strftime('%Y-%m-%d %H:%M')} UTC",
                    }],
                },
            ],
        }],
    }
    _post_webhook(webhook_url, payload)


# ── Teams ─────────────────────────────────────────────────────────────────────

TEAMS_COLORS = {
    "low_stock":       "FF0000",
    "overdue_checkout":"FF8C00",
    "po_submitted":    "7C3AED",
    "po_approved":     "15803D",
    "po_rejected":     "DC2626",
    "warranty_expiry": "D97706",
    "default":         "64748B",
}


def send_teams(webhook_url, event_type, title, body=None, link=None):
    """Post an Adaptive Card to a Teams incoming webhook."""
    color = TEAMS_COLORS.get(event_type, TEAMS_COLORS["default"])
    facts = []
    if body:
        facts.append({"title": "Details", "value": body})

    payload = {
        "@type":      "MessageCard",
        "@context":   "http://schema.org/extensions",
        "themeColor": color,
        "summary":    title,
        "sections":   [{
            "activityTitle":    title,
            "activitySubtitle": body or "",
            "facts":            facts,
            "markdown":         True,
        }],
        **({"potentialAction": [{
            "@type": "OpenUri",
            "name":  "View in CountDepot",
            "targets": [{"os": "default", "uri": link}],
        }]} if link else {}),
    }
    _post_webhook(webhook_url, payload)


# ── Shared ────────────────────────────────────────────────────────────────────

def _post_webhook(url, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Webhook {url[:40]}… → {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise RuntimeError(f"Webhook failed: {e}")


def send_twilio_sms(account_sid, auth_token, from_number, to_number, body):
    """Send an SMS through Twilio's Messages API."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": from_number,
        "To": to_number,
        "Body": body[:1500],
    }).encode()
    token = __import__("base64").b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twilio SMS failed: {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise RuntimeError(f"Twilio SMS failed: {e}")


def notify(event_type, title, body=None, link=None):
    """
    Send a notification to all configured channels for this event type.
    Call this from any route that triggers a notifiable event.
    Silently swallows errors so a webhook failure never breaks the main request.
    """
    # Load settings
    slack_enabled = _get_setting("slack_enabled", "0") == "1"
    slack_url     = _get_setting("slack_webhook_url", "")
    slack_events  = _get_setting("slack_events", "")   # comma-separated event types, empty = all

    teams_enabled = _get_setting("teams_enabled", "0") == "1"
    teams_url     = _get_setting("teams_webhook_url", "")
    teams_events  = _get_setting("teams_events", "")

    def _allowed(events_setting):
        if not events_setting:
            return True  # empty = all events
        return event_type in [e.strip() for e in events_setting.split(",")]

    if slack_enabled and slack_url and _allowed(slack_events):
        try:
            send_slack(slack_url, event_type, title, body, link)
        except Exception:
            pass

    if teams_enabled and teams_url and _allowed(teams_events):
        try:
            send_teams(teams_url, event_type, title, body, link)
        except Exception:
            pass

    try:
        from app.integration_catalog import connector_config, connector_status
        status = connector_status("twilio")
        config = connector_config("twilio", include_secrets=True)
        fields = (config or {}).get("fields", {})
        if status.get("enabled"):
            text = title
            if body:
                text += f"\n{body}"
            if link:
                text += f"\n{link}"
            send_twilio_sms(
                fields["account_sid"],
                fields["auth_token"],
                fields["from_number"],
                fields["alert_to_number"],
                text,
            )
    except Exception:
        pass
