"""
Email sending helper. All outbound email goes through send_email().

If SMTP_HOST is blank the email is printed to the log — useful for dev
and for setups that don't need email yet.
"""

import smtplib
import logging
from html import escape
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.mime.base      import MIMEBase
from email               import encoders
from config import Config

log = logging.getLogger(__name__)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def send_email(to: str, subject: str, html: str, text: str = "",
               attachments: list = None) -> bool:
    """Send an email. Returns True on success, False on failure.
    attachments: list of (filename, bytes, mimetype) tuples."""
    if not Config.SMTP_HOST:
        log.warning(f"[MAIL no-SMTP] To: {to} | Subject: {subject}")
        log.warning(f"[MAIL body] {text or html[:300]}")
        return False
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = Config.SMTP_FROM
        msg["To"]      = to
        alt = MIMEMultipart("alternative")
        if text:
            alt.attach(MIMEText(text, "plain"))
        alt.attach(MIMEText(html, "html"))
        msg.attach(alt)
        for fname, data, mime in (attachments or []):
            maintype, subtype = mime.split("/", 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(part)
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=10) as smtp:
            if Config.SMTP_USE_TLS:
                smtp.starttls()
            if Config.SMTP_USER and Config.SMTP_PASSWORD:
                smtp.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            smtp.sendmail(Config.SMTP_FROM, [to], msg.as_string())
        log.info(f"Email sent → {to}: {subject}")
        return True
    except Exception as e:
        log.error(f"Email failed → {to}: {e}")
        return False


def send_welcome_email(to: str, name: str, slug: str, temp_password: str = None,
                       verify_token: str = None) -> bool:
    domain  = Config.APP_DOMAIN
    url     = f"https://{slug}.{domain}"
    subject = "Welcome to CountDepot — your workspace is ready"

    verify_block_html = ""
    verify_block_text = ""
    if verify_token:
        verify_url = f"https://{slug}.{domain}/verify-email/{verify_token}"
        verify_block_html = f"""
  <div style="background:#fffbeb;border:1px solid #fbbf24;border-radius:8px;padding:14px 18px;margin-bottom:20px">
    <p style="margin:0 0 10px;font-size:13px;color:#92400e;font-weight:600">One more step — verify your email</p>
    <p style="margin:0 0 12px;font-size:13px;color:#78350f">Click the button below to confirm your address and activate your account.</p>
    <a href="{verify_url}" style="display:inline-block;padding:10px 20px;background:#d97706;color:#fff;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px">Verify my email →</a>
  </div>"""
        verify_block_text = f"\n⚠ Verify your email before logging in:\n{verify_url}\n"

    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h1 style="font-size:22px;font-weight:700;margin-bottom:6px">Welcome to CountDepot</h1>
  <p style="color:#64748b;margin-bottom:24px">Your inventory workspace for <strong>{name}</strong> is ready.</p>
  {verify_block_html}
  <div style="background:#f8f7f4;border-radius:10px;padding:18px 20px;margin-bottom:24px">
    <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">YOUR WORKSPACE URL</div>
    <a href="{url}" style="font-size:16px;font-weight:700;color:#1d4ed8;text-decoration:none">{url}</a>
    <div style="margin-top:14px">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:2px">LOGIN EMAIL</div>
      <strong style="font-family:monospace">{to}</strong>
    </div>
    {f'<div style="margin-top:10px"><div style="font-size:11px;color:#94a3b8;margin-bottom:2px">TEMPORARY PASSWORD</div><strong style="font-family:monospace">{temp_password}</strong></div>' if temp_password else ''}
  </div>
  {f'<p style="font-size:13px;color:#64748b;margin-bottom:20px">After verifying your email, choose your business type to finish setup.</p>' if verify_token else ''}
  <a href="{url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Go to my workspace →</a>
  <p style="margin-top:28px;font-size:11px;color:#94a3b8">If you didn't sign up for CountDepot, ignore this email.</p>
</div>"""
    pw_line = f"Temporary password: {temp_password}\n" if temp_password else ""
    text = (f"Welcome to CountDepot\n\nURL: {url}\nLogin email: {to}\n{pw_line}"
            f"{verify_block_text}\n")
    return send_email(to, subject, html, text)


def send_signup_verification_email(to: str, name: str, token: str) -> bool:
    """Sent before a tenant is created — link goes to the bare domain /verify-signup/<token>."""
    domain  = Config.APP_DOMAIN
    url     = f"https://{domain}/verify-signup/{token}"
    subject = "Verify your email to activate CountDepot"
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h1 style="font-size:22px;font-weight:700;margin-bottom:6px">One more step</h1>
  <p style="color:#64748b;margin-bottom:24px">Hi <strong>{name}</strong> — click below to verify your email and create your CountDepot workspace.</p>
  <a href="{url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Verify email &amp; create workspace →</a>
  <p style="margin-top:20px;font-size:12px;color:#64748b">This link expires in <strong>24 hours</strong>. Or paste into your browser:<br><span style="font-family:monospace;color:#1d4ed8">{url}</span></p>
  <p style="margin-top:28px;font-size:11px;color:#94a3b8">If you didn't sign up for CountDepot, you can safely ignore this email.</p>
</div>"""
    text = (f"Verify your CountDepot email\n\n"
            f"Hi {name},\n\nClick here to verify and create your workspace (expires 24h):\n{url}\n\n"
            f"If you didn't sign up, ignore this email.\n")
    return send_email(to, subject, html, text)


def send_platform_signup_alert(name: str, email: str, slug: str, selected_plan: str,
                               selected_period: str, stage: str, created: bool = False) -> bool:
    to = Config.PLATFORM_ADMIN_EMAIL or Config.SUPPORT_EMAIL
    if not to:
        return False
    safe_name = escape(name or "Unknown")
    safe_email = escape(email or "Unknown")
    safe_slug = escape(slug or "unknown")
    safe_plan = escape((selected_plan or "free").title())
    safe_period = escape((selected_period or "monthly").title())
    safe_stage = "Workspace created" if created else "Signup submitted"
    subject = f"[CountDepot] {safe_stage}: {name or email or slug}"
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <div style="display:inline-block;background:#eff6ff;color:#1d4ed8;font-size:11px;font-weight:700;letter-spacing:.5px;padding:3px 10px;border-radius:4px;margin-bottom:16px">{safe_stage.upper()}</div>
  <h2 style="font-size:18px;font-weight:700;margin-bottom:8px">New CountDepot signup activity</h2>
  <p style="color:#64748b;margin-bottom:20px;font-size:13px">A new self-serve account event just happened.</p>
  <div style="background:#f8f7f4;border-radius:8px;padding:14px 18px;margin-bottom:20px">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">COMPANY</div>
    <div style="font-weight:700">{safe_name}</div>
    <div style="margin-top:12px;font-size:11px;color:#94a3b8;margin-bottom:4px">EMAIL</div>
    <div style="font-family:monospace">{safe_email}</div>
    <div style="margin-top:12px;font-size:11px;color:#94a3b8;margin-bottom:4px">WORKSPACE</div>
    <div style="font-family:monospace">{safe_slug}</div>
    <div style="margin-top:12px;font-size:11px;color:#94a3b8;margin-bottom:4px">PLAN INTENT</div>
    <div>{safe_plan} · {safe_period}</div>
    <div style="margin-top:12px;font-size:11px;color:#94a3b8;margin-bottom:4px">STAGE</div>
    <div>{escape(stage or safe_stage)}</div>
  </div>
  <p style="font-size:12px;color:#94a3b8">Check the platform dashboard for recent signups, verification status, and workspace health.</p>
</div>"""
    text = (
        f"CountDepot signup activity\n\n"
        f"Stage: {stage or safe_stage}\n"
        f"Company: {name}\n"
        f"Email: {email}\n"
        f"Workspace: {slug}\n"
        f"Plan intent: {selected_plan} ({selected_period})\n"
    )
    return send_email(to, subject, html, text)


def send_free_inactive_warning_email(to: str, name: str, slug: str, delete_at: str, reason: str) -> bool:
    if not to:
        return False
    domain = Config.APP_DOMAIN
    workspace_url = f"https://{slug}.{domain}"
    reason_text = {
        "no_inventory_no_login": "No one has signed in and no inventory has been added in the last 7 days.",
        "no_activity": "Inventory exists, but no inventory activity has been recorded in the last 14 days.",
    }.get(reason, "This free workspace has been inactive long enough to trigger cleanup.")
    subject = "Your CountDepot free workspace is scheduled for cleanup"
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <div style="display:inline-block;background:#fffbeb;color:#92400e;font-size:11px;font-weight:700;letter-spacing:.5px;padding:3px 10px;border-radius:4px;margin-bottom:16px">INACTIVE FREE WORKSPACE</div>
  <h2 style="font-size:20px;font-weight:700;margin-bottom:8px">Your CountDepot free workspace needs activity</h2>
  <p style="color:#64748b;margin-bottom:18px;font-size:13px">{reason_text}</p>
  <div style="background:#f8f7f4;border-radius:8px;padding:14px 18px;margin-bottom:20px">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">WORKSPACE</div>
    <div style="font-weight:700">{escape(name)}</div>
    <div style="font-family:monospace;color:#1d4ed8;margin-top:4px">{escape(workspace_url)}</div>
    <div style="margin-top:12px;font-size:11px;color:#94a3b8;margin-bottom:4px">SCHEDULED CLEANUP DATE</div>
    <div style="font-weight:700">{escape(delete_at[:10] if delete_at else "")}</div>
  </div>
  <p style="color:#64748b;margin-bottom:20px;font-size:13px">Log in and add inventory or record inventory activity before that date to keep the workspace active.</p>
  <a href="{workspace_url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Open my workspace →</a>
</div>"""
    text = (
        f"Your CountDepot free workspace is scheduled for cleanup.\n\n"
        f"Workspace: {name}\n"
        f"URL: {workspace_url}\n"
        f"Reason: {reason_text}\n"
        f"Scheduled cleanup date: {delete_at[:10] if delete_at else ''}\n\n"
        f"Log in and add inventory or record inventory activity before that date to keep the workspace active.\n"
    )
    return send_email(to, subject, html, text)


def send_verification_email(to: str, slug: str, token: str) -> bool:
    """Standalone verification email — used when resending a verification link."""
    domain  = Config.APP_DOMAIN
    url     = f"https://{slug}.{domain}/verify-email/{token}"
    subject = "Verify your CountDepot email address"
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h1 style="font-size:22px;font-weight:700;margin-bottom:6px">Verify your email</h1>
  <p style="color:#64748b;margin-bottom:24px">Click below to confirm your email address and activate your CountDepot account.</p>
  <a href="{url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Verify my email →</a>
  <p style="margin-top:20px;font-size:12px;color:#64748b">Or paste into your browser:<br><span style="font-family:monospace;color:#1d4ed8">{url}</span></p>
  <p style="margin-top:28px;font-size:11px;color:#94a3b8">If you didn't sign up for CountDepot, ignore this email.</p>
</div>"""
    text = f"Verify your CountDepot email\n\nLink: {url}\n\nIf you didn't sign up, ignore this email.\n"
    return send_email(to, subject, html, text)


def send_invite_email(to: str, slug: str, token: str, inviter: str = "Your admin") -> bool:
    """Invitation email for new users added by an admin."""
    domain  = Config.APP_DOMAIN
    url     = f"https://{slug}.{domain}/reset-password/{token}"
    subject = f"You've been invited to {slug} on CountDepot"
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h1 style="font-size:22px;font-weight:700;margin-bottom:6px">You're invited to CountDepot</h1>
  <p style="color:#64748b;margin-bottom:24px"><strong>{inviter}</strong> has added you to the <strong>{slug}</strong> workspace. Click below to set your password and get started.</p>
  <a href="{url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Accept invite &amp; set password →</a>
  <p style="margin-top:16px;font-size:12px;color:#64748b">This link expires in 72 hours. Or paste into your browser:<br><span style="font-family:monospace;color:#1d4ed8">{url}</span></p>
  <p style="margin-top:28px;font-size:11px;color:#94a3b8">If you weren't expecting this invitation, you can ignore this email.</p>
</div>"""
    text = (f"You've been invited to CountDepot by {inviter}.\n\n"
            f"Set your password here: {url}\n\nLink expires in 72 hours.\n")
    return send_email(to, subject, html, text)


def send_support_message(subject: str, message: str, from_name: str,
                         from_email: str, tenant: str) -> bool:
    """Send an in-app support contact form message to the platform support inbox."""
    to = Config.SUPPORT_EMAIL or Config.PLATFORM_ADMIN_EMAIL
    if not to:
        log.warning(f"[SUPPORT no-dest] From: {from_name} ({tenant}) | {subject}")
        log.warning(f"[SUPPORT body] {message[:500]}")
        return False
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h2 style="font-size:18px;font-weight:700;margin-bottom:6px">Support message from CountDepot</h2>
  <div style="background:#f8f7f4;border-radius:8px;padding:14px 18px;margin-bottom:20px">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:2px">FROM</div>
    <div style="font-weight:600">{from_name}</div>
    <div style="font-size:13px;color:#64748b">{from_email or '(no email)'} &nbsp;·&nbsp; tenant: <strong>{tenant}</strong></div>
  </div>
  <div style="background:#f8f7f4;border-radius:8px;padding:14px 18px;margin-bottom:20px">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:6px">SUBJECT</div>
    <div style="font-weight:600">{subject}</div>
  </div>
  <div style="background:#fff;border:1px solid #e5e3de;border-radius:8px;padding:14px 18px">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:6px">MESSAGE</div>
    <div style="font-size:14px;line-height:1.6;white-space:pre-wrap">{message}</div>
  </div>
</div>"""
    text = (f"Support message from CountDepot\n\n"
            f"From: {from_name} ({from_email or 'no email'}) · tenant: {tenant}\n"
            f"Subject: {subject}\n\n{message}\n")
    return send_email(to, f"[CountDepot Support] {subject}", html, text)


def send_low_stock_alert(to: str, product_name: str, available: int, threshold: int,
                         location_name: str = None) -> bool:
    location_line = (f"<div style='font-size:12px;color:#7f1d1d;margin-bottom:8px'>"
                     f"Site: <strong>{location_name}</strong></div>") if location_name else ""
    text_location = f"Site: {location_name}\n" if location_name else ""
    subject = f"Low stock alert: {product_name}"
    if location_name:
        subject += f" ({location_name})"
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h2 style="font-size:18px;font-weight:700;margin-bottom:6px">Low stock alert</h2>
  <p style="color:#64748b;margin-bottom:20px">A product has dropped to or below its low stock threshold for this site.</p>
  <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <div style="font-size:13px;color:#991b1b;font-weight:600;margin-bottom:4px">{product_name}</div>
    {location_line}
    <div style="font-size:13px;color:#7f1d1d">Available: <strong>{available}</strong> &nbsp;/&nbsp; Threshold: <strong>{threshold}</strong></div>
  </div>
  <p style="font-size:12px;color:#94a3b8">You are receiving this because you are an admin on this CountDepot workspace. Alerts are sent at most once every 24 hours per product and site.</p>
</div>"""
    text = (f"Low stock alert: {product_name}\n\n"
            f"{text_location}"
            f"Available: {available} / Threshold: {threshold}\n\n"
            f"Log in to CountDepot to restock.")
    return send_email(to, subject, html, text)


def send_password_reset_email(to: str, slug: str, token: str) -> bool:
    domain  = Config.APP_DOMAIN
    url     = f"https://{slug}.{domain}/reset-password/{token}"
    subject = "Reset your CountDepot password"
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h1 style="font-size:22px;font-weight:700;margin-bottom:6px">Reset your password</h1>
  <p style="color:#64748b;margin-bottom:24px">Click below to set a new password. This link expires in <strong>1 hour</strong>.</p>
  <a href="{url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Reset password →</a>
  <p style="margin-top:20px;font-size:12px;color:#64748b">Or paste into your browser:<br><span style="font-family:monospace;color:#1d4ed8">{url}</span></p>
  <p style="margin-top:28px;font-size:11px;color:#94a3b8">If you didn't request this, ignore this email. Your password won't change.</p>
</div>"""
    text = f"Reset your CountDepot password\n\nLink (expires 1 hour): {url}\n\nIf you didn't request this, ignore this email.\n"
    return send_email(to, subject, html, text)


def send_overdue_reminder(to: str, item_name: str, serial: str, checked_out_by: str,
                          checkout_date: str, due_date: str, days_overdue: int,
                          reminder_num: int, workspace_url: str) -> bool:
    subject = f"Overdue item reminder: {item_name}"
    badge = "SECOND REMINDER" if reminder_num >= 2 else "REMINDER"
    serial_line = f'<div style="font-size:11px;color:#94a3b8;margin-top:4px">Serial: {serial}</div>' if serial else ""
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <div style="display:inline-block;background:#fef2f2;color:#991b1b;font-size:11px;font-weight:700;letter-spacing:.5px;padding:3px 8px;border-radius:4px;margin-bottom:16px">{badge}</div>
  <h1 style="font-size:20px;font-weight:700;margin-bottom:6px">You have an overdue item</h1>
  <p style="color:#64748b;margin-bottom:20px">This item was due back <strong>{days_overdue} day{'s' if days_overdue != 1 else ''} ago</strong>. Please return it as soon as possible.</p>
  <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <div style="font-size:15px;font-weight:700;color:#0f172a">{item_name}</div>
    {serial_line}
    <div style="margin-top:10px;display:flex;gap:24px;flex-wrap:wrap">
      <div><div style="font-size:10px;color:#94a3b8;margin-bottom:2px">CHECKED OUT BY</div><div style="font-size:13px;font-weight:600">{checked_out_by or '—'}</div></div>
      <div><div style="font-size:10px;color:#94a3b8;margin-bottom:2px">CHECKOUT DATE</div><div style="font-size:13px">{checkout_date or '—'}</div></div>
      <div><div style="font-size:10px;color:#94a3b8;margin-bottom:2px">DUE DATE</div><div style="font-size:13px;color:#991b1b;font-weight:600">{due_date or '—'}</div></div>
    </div>
  </div>
  <a href="{workspace_url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Go to CountDepot →</a>
  <p style="margin-top:24px;font-size:11px;color:#94a3b8">This reminder was sent automatically. Contact your administrator if you have questions.</p>
</div>"""
    text = (f"Overdue item reminder ({badge}): {item_name}\n\n"
            f"This item was due back {days_overdue} day(s) ago.\n\n"
            f"Checked out by: {checked_out_by or '—'}\n"
            f"Checkout date: {checkout_date or '—'}\n"
            f"Due date: {due_date or '—'}\n\n"
            f"Please return the item as soon as possible.\n{workspace_url}\n")
    return send_email(to, subject, html, text)


def send_error_alert(method: str, path: str, tenant: str, tb: str) -> bool:
    """Email the platform admin when a 500 error occurs.
    Only fires when PLATFORM_ADMIN_EMAIL is set."""
    to = Config.PLATFORM_ADMIN_EMAIL or Config.SUPPORT_EMAIL
    if not to:
        log.warning(f"[ERROR ALERT no-dest] {method} {path} — {tb[:200]}")
        return False
    ts = _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    tb_escaped = tb.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <div style="display:inline-block;background:#fef2f2;color:#991b1b;font-size:11px;font-weight:700;letter-spacing:.5px;padding:3px 10px;border-radius:4px;margin-bottom:16px">SERVER ERROR</div>
  <h2 style="font-size:18px;font-weight:700;margin-bottom:6px">500 Internal Server Error</h2>
  <div style="background:#f8f7f4;border-radius:8px;padding:14px 18px;margin-bottom:20px">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:2px">REQUEST</div>
    <div style="font-weight:600;font-family:monospace">{method} {path}</div>
    <div style="margin-top:8px;font-size:11px;color:#94a3b8">TENANT</div>
    <div style="font-family:monospace">{tenant}</div>
    <div style="margin-top:8px;font-size:11px;color:#94a3b8">TIME</div>
    <div style="font-size:13px">{ts}</div>
  </div>
  <div style="background:#0f172a;border-radius:8px;padding:16px 18px;margin-bottom:16px">
    <pre style="color:#e2e8f0;font-size:11px;margin:0;white-space:pre-wrap;word-break:break-all">{tb_escaped}</pre>
  </div>
  <p style="font-size:11px;color:#94a3b8">This alert was sent automatically by CountDepot. Check Sentry for full context.</p>
</div>"""
    text = f"CountDepot 500 Error\n\n{method} {path}\nTenant: {tenant}\nTime: {ts}\n\n{tb}"
    return send_email(to, f"[CountDepot Error] 500 on {method} {path} ({tenant})", html, text)


def send_daily_digest(to: str, stats: dict) -> bool:
    """Daily ops digest email for the platform admin."""
    ts = _utc_now().strftime("%Y-%m-%d")
    tenants     = stats.get("tenants", 0)
    errors_24h  = stats.get("errors_24h", 0)
    low_stock   = stats.get("low_stock_alerts", 0)
    overdue     = stats.get("overdue_checkouts", 0)
    warranty    = stats.get("warranty_expiring_30d", 0)
    backup_age  = stats.get("backup_age_hours", "unknown")
    disk_free   = stats.get("disk_free_pct", "unknown")

    def _status_badge(ok: bool):
        color = "#dcfce7" if ok else "#fef2f2"
        text_color = "#15803d" if ok else "#991b1b"
        label = "OK" if ok else "ALERT"
        return f'<span style="background:{color};color:{text_color};font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px">{label}</span>'

    backup_ok = isinstance(backup_age, (int, float)) and backup_age < 25
    disk_ok   = isinstance(disk_free, (int, float)) and disk_free > 15

    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h2 style="font-size:18px;font-weight:700;margin-bottom:4px">CountDepot Daily Digest</h2>
  <p style="color:#64748b;font-size:13px;margin-bottom:24px">{ts}</p>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px">
    <div style="background:#f8f7f4;border-radius:8px;padding:14px 16px">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">ACTIVE TENANTS</div>
      <div style="font-size:24px;font-weight:700">{tenants}</div>
    </div>
    <div style="background:{'#fef2f2' if errors_24h else '#f8f7f4'};border-radius:8px;padding:14px 16px">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">ERRORS (24H)</div>
      <div style="font-size:24px;font-weight:700;color:{'#b91c1c' if errors_24h else 'inherit'}">{errors_24h}</div>
    </div>
    <div style="background:{'#fef2f2' if low_stock else '#f8f7f4'};border-radius:8px;padding:14px 16px">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">LOW STOCK ALERTS</div>
      <div style="font-size:24px;font-weight:700;color:{'#b91c1c' if low_stock else 'inherit'}">{low_stock}</div>
    </div>
    <div style="background:{'#fef2f2' if overdue else '#f8f7f4'};border-radius:8px;padding:14px 16px">
      <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">OVERDUE CHECKOUTS</div>
      <div style="font-size:24px;font-weight:700;color:{'#b91c1c' if overdue else 'inherit'}">{overdue}</div>
    </div>
  </div>

  <div style="border:1px solid #e5e3de;border-radius:8px;overflow:hidden;margin-bottom:20px">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #e5e3de">
      <span style="font-size:13px">Backup age</span>
      <span style="display:flex;align-items:center;gap:8px;font-size:13px;font-family:monospace">
        {f'{backup_age}h' if isinstance(backup_age, (int,float)) else backup_age}
        {_status_badge(backup_ok)}
      </span>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #e5e3de">
      <span style="font-size:13px">Disk free</span>
      <span style="display:flex;align-items:center;gap:8px;font-size:13px;font-family:monospace">
        {f'{disk_free}%' if isinstance(disk_free, (int,float)) else disk_free}
        {_status_badge(disk_ok)}
      </span>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px">
      <span style="font-size:13px">Warranties expiring (30d)</span>
      <span style="font-size:13px;font-family:monospace">{warranty}</span>
    </div>
  </div>

  <p style="font-size:11px;color:#94a3b8">Sent automatically each morning by CountDepot ops cron.</p>
</div>"""
    text = (f"CountDepot Daily Digest — {ts}\n\n"
            f"Active tenants: {tenants}\n"
            f"Errors (24h): {errors_24h}\n"
            f"Low stock alerts: {low_stock}\n"
            f"Overdue checkouts: {overdue}\n"
            f"Warranties expiring (30d): {warranty}\n"
            f"Backup age: {backup_age}h\n"
            f"Disk free: {disk_free}%\n")
    return send_email(to, f"CountDepot Daily Digest — {ts}", html, text)


def send_po_email(to: str, po_number: str, vendor_name: str,
                  sender_name: str, notes: str, pdf_bytes: bytes) -> bool:
    subject = f"Purchase Order {po_number} from {sender_name}"
    notes_block = f'<p style="font-size:13px;color:#475569;margin-bottom:20px;line-height:1.6"><em>{notes}</em></p>' if notes else ""
    html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <h1 style="font-size:22px;font-weight:700;margin-bottom:6px">Purchase Order {po_number}</h1>
  <p style="color:#64748b;margin-bottom:20px">Hi {vendor_name} — please find our purchase order attached as a PDF.</p>
  {notes_block}
  <div style="background:#f8f7f4;border-radius:8px;padding:14px 18px;margin-bottom:20px">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">PO NUMBER</div>
    <div style="font-size:16px;font-weight:700;font-family:monospace">{po_number}</div>
    <div style="margin-top:10px;font-size:11px;color:#94a3b8">ISSUED BY</div>
    <div style="font-size:13px;font-weight:600">{sender_name}</div>
  </div>
  <p style="font-size:12px;color:#94a3b8;margin-top:24px">Please confirm receipt and expected delivery date by replying to this email.</p>
</div>"""
    text = (f"Purchase Order {po_number}\n\nHi {vendor_name},\n\n"
            f"Please find our purchase order {po_number} attached.\n\n"
            f"Issued by: {sender_name}\n"
            f"{('Notes: ' + notes + chr(10)) if notes else ''}"
            f"\nPlease confirm receipt by replying to this email.\n")
    return send_email(to, subject, html, text,
                      attachments=[(f"{po_number}.pdf", pdf_bytes, "application/pdf")])
