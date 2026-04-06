from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, redirect, url_for, g, jsonify

import secrets as _secrets

from app.db import query, execute
from app.helpers import (hash_pw, verify_pw, validate_password,
                         check_login_rate, clear_login_rate, check_rate_limit,
                         get_user_perms, log_auth_event)

bp = Blueprint("auth", __name__)

SESSION_LIFETIME_HOURS = 8


def check_session_expiry():
    """Returns True if the current session is still valid, False if expired."""
    expires = session.get("expires_at")
    if not expires:
        return False
    if datetime.utcnow().isoformat() > expires:
        session.clear()
        return False
    return True


@bp.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("user_id") and check_session_expiry():
        return redirect(url_for("main.inventory"))
    error = None
    if request.method == "POST":
        ip       = request.remote_addr or "unknown"
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        allowed, reset_in = check_login_rate(ip)
        if not allowed:
            log_auth_event("RATE_LIMITED", username=username, ip=ip,
                           tenant=getattr(g, "tenant_slug", ""),
                           detail=f"Login rate limit hit; retry in {reset_in}s")
            return render_template(
                "login.html",
                error=f"Too many login attempts. Try again in {reset_in} seconds."
            ), 429
        # Email-only login
        user = query("SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?",
                     [username.lower()], one=True)
        if user and verify_pw(password, user["password"]):
            # Auto-migrate legacy HMAC-SHA256 hashes to bcrypt on first successful login
            if not user["password"].startswith("$2"):
                execute("UPDATE users SET password=? WHERE id=?",
                        [hash_pw(password), user["id"]])
            # Block accounts that haven't verified their email yet
            email_verified = user["email_verified"] if "email_verified" in user.keys() else 1
            email = user["email"] if "email" in user.keys() else None
            if not email_verified:
                error = "Please verify your email address before logging in. Check your inbox for the verification link."
                return render_template("login.html", error=error,
                                       unverified=True, unverified_email=email)
            # 2FA check
            two_fa = user["two_fa_enabled"] if "two_fa_enabled" in user.keys() else 0
            from config import Config as _Cfg
            if two_fa and email and _Cfg.SMTP_HOST:
                import random
                from app.mailer import send_email as _send_email
                otp = f"{random.randint(0, 999999):06d}"
                expires_at = (datetime.utcnow() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
                execute("UPDATE login_otp SET used=1 WHERE user_id=? AND used=0", [user["id"]])
                execute("INSERT INTO login_otp (user_id,otp,expires_at) VALUES (?,?,?)",
                        [user["id"], otp, expires_at])
                _send_email(email, "Your CountDepot login code",
                    f"<p style='font-family:sans-serif'>Your one-time login code is:</p>"
                    f"<p style='font-family:monospace;font-size:32px;font-weight:700;letter-spacing:8px'>{otp}</p>"
                    f"<p style='font-family:sans-serif;font-size:12px;color:#888'>Expires in 10 minutes. If you didn't request this, ignore it.</p>",
                    f"Your CountDepot login code: {otp}\nExpires in 10 minutes.")
                session["pending_2fa_user_id"] = user["id"]
                log_auth_event("2FA_SENT", username=user["username"], ip=ip,
                               tenant=getattr(g, "tenant_slug", ""))
                return render_template("verify_2fa.html")
            clear_login_rate(ip)
            perms = get_user_perms(
                user["id"], user["role"],
                user["permissions"] if "permissions" in user.keys() else "")
            now = datetime.utcnow()
            tok = _secrets.token_hex(32)
            session.clear()
            session["user_id"]              = user["id"]
            session["username"]             = user["username"]
            session["role"]                 = user["role"]
            session["permissions"]          = ",".join(perms)
            session["must_change_password"] = bool(user["must_change_password"])
            session["expires_at"]           = (now + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
            session["session_token"]        = tok
            execute("UPDATE users SET last_login=?, session_token=? WHERE id=?",
                    [now.strftime("%Y-%m-%d %H:%M:%S"), tok, user["id"]])
            log_auth_event("LOGIN_OK", username=user["username"], ip=ip,
                           tenant=getattr(g, "tenant_slug", ""))
            return redirect(url_for("main.inventory"))
        error = "Invalid username or password."
        log_auth_event("LOGIN_FAIL", username=username, ip=ip,
                       tenant=getattr(g, "tenant_slug", ""))
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    log_auth_event("LOGOUT", username=session.get("username", ""),
                   ip=request.remote_addr or "",
                   tenant=getattr(g, "tenant_slug", ""))
    uid = session.get("user_id")
    session.clear()
    if uid:
        try:
            execute("UPDATE users SET session_token=NULL WHERE id=?", [uid])
        except Exception:
            pass
    return redirect(url_for("auth.login_page"))


@bp.route("/change-password", methods=["GET", "POST"])
def change_password():
    """Forced password change — shown when must_change_password=1."""
    if not session.get("user_id"):
        return redirect(url_for("auth.login_page"))
    error = None
    if request.method == "POST":
        new_pw  = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        error = validate_password(new_pw)
        if not error and new_pw != confirm:
            error = "Passwords do not match."
        if not error:
            execute("UPDATE users SET password=?, must_change_password=0 WHERE id=?",
                    [hash_pw(new_pw), session["user_id"]])
            session["must_change_password"] = False
            log_auth_event("PW_CHANGE", username=session.get("username", ""),
                           ip=request.remote_addr or "",
                           tenant=getattr(g, "tenant_slug", ""))
            return redirect(url_for("main.inventory"))
    return render_template("change_password.html", error=error)


@bp.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    uid = session.get("pending_2fa_user_id")
    if not uid:
        return redirect(url_for("auth.login_page"))
    error = None
    if request.method == "POST":
        ip  = request.remote_addr or "unknown"
        otp = request.form.get("otp", "").strip()
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        row = query("SELECT * FROM login_otp WHERE user_id=? AND used=0 AND expires_at > ? ORDER BY id DESC LIMIT 1",
                    [uid, now], one=True)
        if row and row["otp"] == otp:
            execute("UPDATE login_otp SET used=1 WHERE id=?", [row["id"]])
            user = query("SELECT * FROM users WHERE id=?", [uid], one=True)
            if not user:
                session.pop("pending_2fa_user_id", None)
                return redirect(url_for("auth.login_page"))
            clear_login_rate(ip)
            perms = get_user_perms(user["id"], user["role"],
                                   user["permissions"] if "permissions" in user.keys() else "")
            now_dt = datetime.utcnow()
            tok = _secrets.token_hex(32)
            session.clear()
            session["user_id"]              = user["id"]
            session["username"]             = user["username"]
            session["role"]                 = user["role"]
            session["permissions"]          = ",".join(perms)
            session["must_change_password"] = bool(user["must_change_password"])
            session["expires_at"]           = (now_dt + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
            session["session_token"]        = tok
            execute("UPDATE users SET last_login=?, session_token=? WHERE id=?",
                    [now_dt.strftime("%Y-%m-%d %H:%M:%S"), tok, user["id"]])
            log_auth_event("LOGIN_OK_2FA", username=user["username"], ip=ip,
                           tenant=getattr(g, "tenant_slug", ""))
            return redirect(url_for("main.inventory"))
        error = "Invalid or expired code. Please try again."
        log_auth_event("2FA_FAIL", username=str(uid), ip=ip,
                       tenant=getattr(g, "tenant_slug", ""))
    return render_template("verify_2fa.html", error=error)


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Step 1 — user enters email, gets a reset link."""
    if session.get("user_id"):
        return redirect(url_for("main.inventory"))
    sent = False
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        allowed, reset_in = check_rate_limit(ip, "forgot-password", max_attempts=5, window=900)
        if not allowed:
            return render_template("forgot_password.html", sent=False,
                                   error=f"Too many requests. Try again in {reset_in // 60 + 1} minutes.")
        email = request.form.get("email", "").strip().lower()
        if email:
            user = query("SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?",
                         [email], one=True)
            if user:
                import secrets as _secrets
                from datetime import timedelta
                from app.mailer import send_password_reset_email
                from flask import g
                token      = _secrets.token_urlsafe(32)
                expires_at = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                execute("UPDATE password_reset_tokens SET used=1 WHERE user_id=? AND used=0",
                        [user["id"]])
                execute("INSERT INTO password_reset_tokens (user_id,token,expires_at) VALUES (?,?,?)",
                        [user["id"], token, expires_at])
                send_password_reset_email(email, g.tenant_slug, token)
        sent = True   # always show "sent" — don't reveal whether email exists
    return render_template("forgot_password.html", sent=sent, error=None)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Step 2 — user clicks link, sets new password."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = query(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        [token, now], one=True)
    if not row:
        return render_template("reset_password.html", invalid=True, token=token)
    error = None
    if request.method == "POST":
        new_pw  = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        error = validate_password(new_pw)
        if not error and new_pw != confirm:
            error = "Passwords do not match."
        if not error:
            execute("UPDATE users SET password=?, must_change_password=0, email_verified=1 WHERE id=?",
                    [hash_pw(new_pw), row["user_id"]])
            execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", [row["id"]])
            return render_template("reset_password.html", success=True, token=token)
    return render_template("reset_password.html", token=token, error=error, invalid=False)


@bp.route("/verify-email/<token>")
def verify_email(token):
    """Email verification — user clicks link from their welcome email."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = query(
        "SELECT * FROM email_verification_tokens WHERE token=? AND used=0 AND expires_at > ?",
        [token, now], one=True)
    if not row:
        return render_template("verify_email.html", invalid=True)
    execute("UPDATE users SET email_verified=1 WHERE id=?", [row["user_id"]])
    execute("UPDATE email_verification_tokens SET used=1 WHERE id=?", [row["id"]])
    return render_template("verify_email.html", success=True)


@bp.route("/_cross-login", methods=["POST"])
def cross_login():
    """Find which tenant an email belongs to, validate password, return one-time auto-login URL."""
    data     = request.get_json(silent=True) or {}
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"ok": False, "msg": "Email and password are required."}), 400

    ip = request.remote_addr or "unknown"
    allowed, reset_in = check_login_rate(ip)
    if not allowed:
        return jsonify({"ok": False, "msg": f"Too many attempts. Try again in {reset_in}s."}), 429

    import os, sqlite3
    from config import Config as _Cfg
    from app.platform import get_platform_db as _get_pdb

    pdb      = _get_pdb()
    tenants  = pdb.execute("SELECT slug FROM tenants WHERE active=1").fetchall()
    pdb.close()

    found_slug = None
    found_user = None
    for row in tenants:
        slug    = row[0]
        db_path = os.path.join(_Cfg.TENANTS_DIR, slug, "inventory.db")
        if not os.path.exists(db_path):
            continue
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        user = conn.execute(
            "SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?", [email]
        ).fetchone()
        conn.close()
        if user and verify_pw(password, user["password"]):
            found_slug = slug
            found_user = dict(user)
            break

    if not found_slug:
        log_auth_event("LOGIN_FAIL", username=email, ip=ip, tenant="(cross-login)",
                       detail="No matching tenant/password")
        return jsonify({"ok": False, "msg": "Invalid email or password."}), 401

    # Check email verification
    if not found_user.get("email_verified", 1):
        return jsonify({"ok": False, "msg": "Please verify your email address before logging in."}), 403

    # Generate one-time cross-login token (5 min TTL)
    import secrets as _sec
    from app.platform import get_platform_db as _get_pdb2
    token      = _sec.token_urlsafe(32)
    now        = datetime.utcnow()
    expires_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    pdb2 = _get_pdb2()
    pdb2.execute(
        "INSERT INTO cross_login_tokens (tenant_slug,user_id,token,expires_at,created_at) VALUES (?,?,?,?,?)",
        [found_slug, found_user["id"], token, expires_at, created_at]
    )
    pdb2.commit()
    pdb2.close()

    clear_login_rate(ip)
    log_auth_event("LOGIN_OK", username=found_user["username"], ip=ip, tenant=found_slug,
                   detail="cross-login token issued")

    # Build redirect URL — preserve scheme and base domain, swap subdomain
    host   = request.host  # e.g. "countdepot.com" or "localhost:5000"
    scheme = "https"
    if "localhost" in host or "127.0.0.1" in host:
        scheme = "http"
        redirect_url = f"{scheme}://{host}/auto-login?token={token}"
    else:
        host_no_port = host.split(":")[0]
        parts        = host_no_port.split(".")
        base_domain  = ".".join(parts[-2:]) if len(parts) >= 2 else host_no_port
        redirect_url = f"{scheme}://{found_slug}.{base_domain}/auto-login?token={token}"

    return jsonify({"ok": True, "redirect": redirect_url})


@bp.route("/auto-login")
def auto_login():
    """Consume a cross-login token issued by /_cross-login and start a session."""
    token = request.args.get("token", "").strip()
    if not token:
        return redirect(url_for("auth.login_page"))

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    from app.platform import get_platform_db as _get_pdb
    pdb = _get_pdb()
    row = pdb.execute(
        "SELECT * FROM cross_login_tokens WHERE token=? AND used=0 AND expires_at > ?",
        [token, now]
    ).fetchone()
    if not row:
        pdb.close()
        return redirect(url_for("auth.login_page"))

    tenant_slug = row["tenant_slug"]
    user_id     = row["user_id"]

    # Ensure this token is for the current tenant
    current_slug = getattr(g, "tenant_slug", None)
    if current_slug and current_slug != tenant_slug:
        pdb.close()
        return redirect(url_for("auth.login_page"))

    pdb.execute("UPDATE cross_login_tokens SET used=1 WHERE token=?", [token])
    pdb.commit()
    pdb.close()

    user = query("SELECT * FROM users WHERE id=?", [user_id], one=True)
    if not user:
        return redirect(url_for("auth.login_page"))

    perms  = get_user_perms(user["id"], user["role"],
                            user["permissions"] if "permissions" in user.keys() else "")
    now_dt = datetime.utcnow()
    tok    = _secrets.token_hex(32)
    session.clear()
    session["user_id"]              = user["id"]
    session["username"]             = user["username"]
    session["role"]                 = user["role"]
    session["permissions"]          = ",".join(perms)
    session["must_change_password"] = bool(user["must_change_password"])
    session["expires_at"]           = (now_dt + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
    session["session_token"]        = tok
    execute("UPDATE users SET last_login=?, session_token=? WHERE id=?",
            [now_dt.strftime("%Y-%m-%d %H:%M:%S"), tok, user["id"]])
    log_auth_event("LOGIN_OK", username=user["username"],
                   ip=request.remote_addr or "", tenant=tenant_slug,
                   detail="via cross-login token")
    return redirect(url_for("main.inventory"))


@bp.route("/resend-verification", methods=["POST"])
def resend_verification():
    """Resend email verification link. Rate limited to 3 per 15 min per IP."""
    ip = request.remote_addr or "unknown"
    allowed, reset_in = check_rate_limit(ip, "resend-verification", max_attempts=3, window=900)
    if not allowed:
        return render_template("login.html",
                               error=f"Too many requests. Try again in {reset_in // 60 + 1} minutes.",
                               unverified=False)

    email_addr = (request.form.get("email") or request.form.get("username") or "").strip().lower()
    if email_addr:
        user = query("SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?", [email_addr], one=True)
        email_verified = user["email_verified"] if user and "email_verified" in user.keys() else 1
        email = user["email"] if user and "email" in user.keys() else None
        if user and not email_verified:
            from app.mailer import send_verification_email
            token      = _secrets.token_urlsafe(32)
            expires_at = (datetime.utcnow() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
            execute("UPDATE email_verification_tokens SET used=1 WHERE user_id=? AND used=0",
                    [user["id"]])
            execute("INSERT INTO email_verification_tokens (user_id, token, expires_at) VALUES (?,?,?)",
                    [user["id"], token, expires_at])
            send_verification_email(email, g.tenant_slug, token)

    # Always show the same message — don't reveal whether account exists or is unverified
    return render_template("login.html",
                           error="If your account exists and is unverified, a new link has been sent. Check your inbox.",
                           unverified=False)
