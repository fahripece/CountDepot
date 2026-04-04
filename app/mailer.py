"""
Email sending helper. All outbound email goes through send_email().

If SMTP_HOST is blank the email is printed to the log — useful for dev
and for setups that don't need email yet.
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from config import Config

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, html: str, text: str = "") -> bool:
    """Send an email. Returns True on success, False on failure."""
    if not Config.SMTP_HOST:
        log.warning(f"[MAIL no-SMTP] To: {to} | Subject: {subject}")
        log.warning(f"[MAIL body] {text or html[:300]}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = Config.SMTP_FROM
        msg["To"]      = to
        if text:
            msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
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


def send_welcome_email(to: str, name: str, slug: str, temp_password: str,
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
    <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">YOUR URL</div>
    <a href="{url}" style="font-size:16px;font-weight:700;color:#1d4ed8;text-decoration:none">{url}</a>
    <div style="display:flex;gap:32px;margin-top:14px">
      <div><div style="font-size:11px;color:#94a3b8;margin-bottom:2px">USERNAME</div><strong>admin</strong></div>
      <div><div style="font-size:11px;color:#94a3b8;margin-bottom:2px">PASSWORD</div><strong style="font-family:monospace">{temp_password}</strong></div>
    </div>
  </div>
  <p style="font-size:13px;color:#64748b;margin-bottom:20px">After verifying, choose your business type to set up the right categories.</p>
  <a href="{url}" style="display:inline-block;padding:12px 24px;background:#1d4ed8;color:#fff;border-radius:7px;text-decoration:none;font-weight:600;font-size:14px">Go to my workspace →</a>
  <p style="margin-top:28px;font-size:11px;color:#94a3b8">If you didn't sign up for CountDepot, ignore this email.</p>
</div>"""
    text = (f"Welcome to CountDepot\n\nURL: {url}\nUsername: admin\nPassword: {temp_password}\n"
            f"{verify_block_text}\n")
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
