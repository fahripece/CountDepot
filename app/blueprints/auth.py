from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from flask import Blueprint, render_template, request, session, redirect, url_for, g, jsonify

import secrets as _secrets

from app.db import query, execute
from app.helpers import (hash_pw, verify_pw, validate_password,
                         check_login_rate, clear_login_rate, check_rate_limit,
                         check_account_lockout,
                         get_user_perms, get_user_location_ids, log_auth_event)

bp = Blueprint("auth", __name__)

SESSION_LIFETIME_HOURS = 8


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _workspace_email_2fa_enabled() -> bool:
    row = query("SELECT value FROM settings WHERE key='workspace_email_2fa_enabled'", one=True)
    if row and str(row["value"]).strip() != "":
        return str(row["value"]).strip() in {"1", "true", "True", "yes", "on"}
    legacy = query(
        "SELECT 1 FROM users WHERE role='admin' AND COALESCE(two_fa_enabled,0)=1 LIMIT 1",
        one=True,
    )
    return bool(legacy)


def _email_2fa_required(user) -> bool:
    per_user = user["two_fa_enabled"] if "two_fa_enabled" in user.keys() else 0
    return _workspace_email_2fa_enabled() or bool(per_user)


def _resolved_tour_status(user) -> str:
    raw = ""
    if "tour_status" in user.keys():
        raw = (user["tour_status"] or "").strip()
    if raw in {"pending", "remind_later", "skipped", "completed"}:
        return raw
    legacy_done = bool(user["intro_tour_completed_at"] if "intro_tour_completed_at" in user.keys() else None)
    return "completed" if legacy_done else "pending"


def _build_session(user, perms):
    """Return a dict of session keys for a logged-in user."""
    loc_ids = get_user_location_ids(user["id"], user["role"])
    tour_status = _resolved_tour_status(user)
    sess = {
        "user_id":              user["id"],
        "username":             user["username"],
        "role":                 user["role"],
        "permissions":          ",".join(perms),
        "must_change_password": bool(user["must_change_password"]),
        "expires_at":           (_utc_now() + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat(),
        "session_token":        _secrets.token_hex(32),
        "location_ids":         loc_ids,
        "tour_status":          tour_status,
        "tour_prompt_suppressed": False,
    }
    return sess


def _finalize_session(user, perms, *, next_target=None, track_login=True, login_result="ok", auth_detail=None, impersonation=False):
    sess = _build_session(user, perms)
    session.clear()
    session.update(sess)
    if next_target:
        session["post_login_redirect"] = next_target
    if impersonation:
        session["platform_impersonation"] = True
    if track_login:
        now_str = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
        execute("UPDATE users SET last_login=?, session_token=? WHERE id=?",
                [now_str, sess["session_token"], user["id"]])
        execute("INSERT INTO login_log (user_id,username,ip_address,user_agent,result,ts) VALUES (?,?,?,?,?,?)",
                [user["id"], user["username"], request.remote_addr or "", request.user_agent.string, login_result, now_str])
    if auth_detail:
        log_auth_event(auth_detail, username=user["username"], ip=request.remote_addr or "", tenant=getattr(g, "tenant_slug", ""))
    return redirect(
        _safe_next_target(session.pop("post_login_redirect", None)) or url_for("main.inventory"),
        code=303 if not track_login else 302,
    )


def _safe_next_target(raw_target):
    target = (raw_target or "").strip()
    if not target.startswith("/"):
        return None
    if target.startswith("//"):
        return None
    return target


def _request_next_target():
    raw_target = request.args.get("next", "")
    raw_query = request.query_string.decode("utf-8", errors="ignore")
    if "next=" in raw_query:
        raw_target = unquote(raw_query.split("next=", 1)[1])
    return _safe_next_target(raw_target)


def check_session_expiry():
    """Returns True if the current session is still valid, False if expired."""
    expires = session.get("expires_at")
    if not expires:
        return False
    if _utc_now().isoformat() > expires:
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

        # Per-account lockout — 5 consecutive failures from any IP → 15-min lockout
        acct_allowed, locked_until = check_account_lockout(username)
        if not acct_allowed:
            log_auth_event("ACCOUNT_LOCKED", username=username, ip=ip,
                           tenant=getattr(g, "tenant_slug", ""),
                           detail=f"Account locked until {locked_until}")
            return render_template(
                "login.html",
                error="This account is temporarily locked due to too many failed attempts. Try again in 15 minutes."
            ), 429

        # Prefer email login, but fall back to username for legacy/test tenants
        # that were created before email-only auth became the default.
        user = query("SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?",
                     [username.lower()], one=True)
        if not user:
            user = query("SELECT * FROM users WHERE LOWER(username)=?",
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
            # 2FA check — TOTP takes priority over email OTP if both are enabled
            totp_enabled = user["totp_enabled"] if "totp_enabled" in user.keys() else 0
            two_fa       = _email_2fa_required(user)
            from config import Config as _Cfg
            if totp_enabled and user.get("totp_secret"):
                session["pending_2fa_user_id"] = user["id"]
                session["pending_2fa_method"]  = "totp"
                log_auth_event("2FA_TOTP_CHALLENGE", username=user["username"], ip=ip,
                               tenant=getattr(g, "tenant_slug", ""))
                return render_template("verify_2fa.html", method="totp")
            if two_fa:
                if not email:
                    log_auth_event("2FA_BLOCKED_NO_EMAIL", username=user["username"], ip=ip,
                                   tenant=getattr(g, "tenant_slug", ""))
                    return render_template(
                        "login.html",
                        error="Two-factor authentication is enabled for this account, but no email address is set."
                    ), 403
                if not _Cfg.SMTP_HOST:
                    log_auth_event("2FA_BLOCKED_NO_SMTP", username=user["username"], ip=ip,
                                   tenant=getattr(g, "tenant_slug", ""))
                    return render_template(
                        "login.html",
                        error="Two-factor authentication is enabled for this account, but email delivery is not configured on this server. Contact your admin."
                    ), 503
                import random
                from app.mailer import send_email as _send_email
                otp = f"{random.randint(0, 999999):06d}"
                expires_at = (_utc_now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
                execute("UPDATE login_otp SET used=1 WHERE user_id=? AND used=0", [user["id"]])
                execute("INSERT INTO login_otp (user_id,otp,expires_at) VALUES (?,?,?)",
                        [user["id"], otp, expires_at])
                _send_email(email, "Your CountDepot login code",
                    f"<p style='font-family:sans-serif'>Your one-time login code is:</p>"
                    f"<p style='font-family:monospace;font-size:32px;font-weight:700;letter-spacing:8px'>{otp}</p>"
                    f"<p style='font-family:sans-serif;font-size:12px;color:#888'>Expires in 10 minutes. If you didn't request this, ignore it.</p>",
                    f"Your CountDepot login code: {otp}\nExpires in 10 minutes.")
                session["pending_2fa_user_id"] = user["id"]
                session["pending_2fa_method"]  = "email"
                log_auth_event("2FA_SENT", username=user["username"], ip=ip,
                               tenant=getattr(g, "tenant_slug", ""))
                return render_template("verify_2fa.html", method="email")
            clear_login_rate(ip)
            perms = get_user_perms(
                user["id"], user["role"],
                user["permissions"] if "permissions" in user.keys() else "")
            sess = _build_session(user, perms)
            redirect_target = _safe_next_target(session.get("post_login_redirect"))
            session.clear()
            session.update(sess)
            now_str = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
            execute("UPDATE users SET last_login=?, session_token=? WHERE id=?",
                    [now_str, sess["session_token"], user["id"]])
            execute("INSERT INTO login_log (user_id,username,ip_address,user_agent,result,ts) VALUES (?,?,?,?,?,?)",
                    [user["id"], user["username"], ip, request.user_agent.string, "ok", now_str])
            log_auth_event("LOGIN_OK", username=user["username"], ip=ip,
                           tenant=getattr(g, "tenant_slug", ""))
            return redirect(redirect_target or url_for("main.inventory"))
        error = "Invalid username or password."
        execute("INSERT INTO login_log (user_id,username,ip_address,user_agent,result,ts) VALUES (?,?,?,?,?,?)",
                [None, username, ip, request.user_agent.string, "fail",
                 _utc_now().strftime("%Y-%m-%d %H:%M:%S")])
        log_auth_event("LOGIN_FAIL", username=username, ip=ip,
                       tenant=getattr(g, "tenant_slug", ""))
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    username = session.get("username", "")
    uid      = session.get("user_id")
    ip       = request.remote_addr or ""
    impersonating = bool(session.get("platform_impersonation"))
    log_auth_event("LOGOUT", username=username, ip=ip, tenant=getattr(g, "tenant_slug", ""))
    if not impersonating:
        try:
            execute("INSERT INTO login_log (user_id,username,ip_address,user_agent,result,ts) VALUES (?,?,?,?,?,?)",
                    [uid, username, ip, request.user_agent.string, "logout",
                     _utc_now().strftime("%Y-%m-%d %H:%M:%S")])
        except Exception:
            pass
    session.clear()
    if uid and not impersonating:
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
        current = query("SELECT password FROM users WHERE id=?", [session["user_id"]], one=True)
        error = validate_password(new_pw, current["password"] if current else None)
        if not error and new_pw != confirm:
            error = "Passwords do not match."
        if not error:
            execute("UPDATE users SET password=?, must_change_password=0 WHERE id=?",
                    [hash_pw(new_pw), session["user_id"]])
            session["must_change_password"] = False
            log_auth_event("PW_CHANGE", username=session.get("username", ""),
                           ip=request.remote_addr or "",
                           tenant=getattr(g, "tenant_slug", ""))
            return redirect(_safe_next_target(session.pop("post_login_redirect", None)) or url_for("main.inventory"))
    return render_template("change_password.html", error=error)


@bp.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    uid    = session.get("pending_2fa_user_id")
    method = session.get("pending_2fa_method", "email")
    if not uid:
        return redirect(url_for("auth.login_page"))
    error = None
    if request.method == "POST":
        ip  = request.remote_addr or "unknown"
        allowed, retry_in = check_rate_limit(ip, "2fa", max_attempts=5, window=300)
        if not allowed:
            session.pop("pending_2fa_user_id", None)
            session.pop("pending_2fa_method", None)
            log_auth_event("RATE_LIMITED", username=str(uid), ip=ip,
                           tenant=getattr(g, "tenant_slug", ""),
                           detail="2FA rate limit hit")
            return render_template("verify_2fa.html", method=method,
                                   error=f"Too many attempts. Try again in {retry_in} seconds."), 429
        otp = request.form.get("otp", "").strip()
        verified = False

        if method == "totp":
            try:
                import pyotp
                user_row = query("SELECT totp_secret FROM users WHERE id=?", [uid], one=True)
                if user_row and user_row["totp_secret"]:
                    verified = pyotp.TOTP(user_row["totp_secret"]).verify(otp, valid_window=1)
            except Exception:
                pass
        else:
            now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
            row = query("SELECT * FROM login_otp WHERE user_id=? AND used=0 AND expires_at > ? ORDER BY id DESC LIMIT 1",
                        [uid, now], one=True)
            if row and row["otp"] == otp:
                execute("UPDATE login_otp SET used=1 WHERE id=?", [row["id"]])
                verified = True

        if verified:
            user = query("SELECT * FROM users WHERE id=?", [uid], one=True)
            if not user:
                session.pop("pending_2fa_user_id", None)
                session.pop("pending_2fa_method", None)
                return redirect(url_for("auth.login_page"))
            clear_login_rate(ip)
            perms = get_user_perms(user["id"], user["role"],
                                   user["permissions"] if "permissions" in user.keys() else "")
            redirect_target = _safe_next_target(session.get("post_login_redirect"))
            pending_impersonation = bool(session.get("pending_platform_impersonation"))
            if pending_impersonation:
                return _finalize_session(
                    user,
                    perms,
                    next_target=redirect_target,
                    track_login=False,
                    impersonation=True,
                )
            return _finalize_session(
                user,
                perms,
                next_target=redirect_target,
                track_login=True,
                login_result="ok",
                auth_detail="LOGIN_OK_2FA",
            )

        error = "Invalid or expired code. Please try again."
        execute("INSERT INTO login_log (user_id,username,ip_address,user_agent,result,ts) VALUES (?,?,?,?,?,?)",
                [uid, str(uid), ip, request.user_agent.string, "2fa_fail",
                 _utc_now().strftime("%Y-%m-%d %H:%M:%S")])
        log_auth_event("2FA_FAIL", username=str(uid), ip=ip,
                       tenant=getattr(g, "tenant_slug", ""))
    return render_template("verify_2fa.html", method=method, error=error)


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
                expires_at = (_utc_now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
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
    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    row = query(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        [token, now], one=True)
    if not row:
        return render_template("reset_password.html", invalid=True, token=token)
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        allowed, reset_in = check_rate_limit(
            ip,
            f"reset-password:{token[:16]}",
            max_attempts=8,
            window=900,
        )
        if not allowed:
            return render_template(
                "reset_password.html",
                token=token,
                error=f"Too many reset attempts. Try again in {reset_in // 60 + 1} minutes.",
                invalid=False,
            ), 429
        new_pw  = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        current = query("SELECT password FROM users WHERE id=?", [row["user_id"]], one=True)
        error = validate_password(new_pw, current["password"] if current else None)
        if not error and new_pw != confirm:
            error = "Passwords do not match."
        if not error:
            execute("UPDATE users SET password=?, must_change_password=0, email_verified=1 WHERE id=?",
                    [hash_pw(new_pw), row["user_id"]])
            execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", [row["id"]])
            user = query("SELECT username FROM users WHERE id=?", [row["user_id"]], one=True)
            log_auth_event("PW_RESET", username=user["username"] if user else "",
                           ip=ip, tenant=getattr(g, "tenant_slug", ""),
                           detail="password reset completed")
            return render_template("reset_password.html", success=True, token=token)
    return render_template("reset_password.html", token=token, error=error, invalid=False)


@bp.route("/verify-email/<token>")
def verify_email(token):
    """Email verification — user clicks link from their welcome email."""
    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    row = query(
        "SELECT * FROM email_verification_tokens WHERE token=? AND used=0 AND expires_at > ?",
        [token, now], one=True)
    if not row:
        # Check if this token was already used (e.g. link clicked twice or after resend)
        used_row = query(
            "SELECT u.email_verified FROM email_verification_tokens t "
            "JOIN users u ON u.id=t.user_id WHERE t.token=?",
            [token], one=True)
        if used_row and used_row["email_verified"]:
            # Already verified — treat as success so the user isn't confused
            return render_template("verify_email.html", success=True)
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
    now        = _utc_now()
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


@bp.route("/_cross-forgot-password", methods=["POST"])
def cross_forgot_password():
    """Find which tenant an email belongs to and send a password reset link.
    Always returns ok=True to avoid revealing whether the email exists."""
    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    if not email:
        return jsonify({"ok": True})  # silent no-op

    ip = request.remote_addr or "unknown"
    allowed, reset_in = check_rate_limit(ip, "forgot-password", max_attempts=5, window=900)
    if not allowed:
        return jsonify({"ok": False, "msg": f"Too many requests. Try again in {reset_in // 60 + 1} minutes."}), 429

    import os, sqlite3 as _sql
    from config import Config as _Cfg
    from app.platform import get_platform_db as _get_pdb

    pdb     = _get_pdb()
    tenants = pdb.execute("SELECT slug FROM tenants WHERE active=1").fetchall()
    pdb.close()

    found_slug = None
    found_user = None
    for row in tenants:
        slug    = row[0]
        db_path = os.path.join(_Cfg.TENANTS_DIR, slug, "inventory.db")
        if not os.path.exists(db_path):
            continue
        conn = _sql.connect(db_path)
        conn.row_factory = _sql.Row
        user = conn.execute(
            "SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?", [email]
        ).fetchone()
        conn.close()
        if user:
            found_slug = slug
            found_user = dict(user)
            break

    if found_slug and found_user:
        import secrets as _sec
        conn = _sql.connect(os.path.join(_Cfg.TENANTS_DIR, found_slug, "inventory.db"))
        expires_at = (_utc_now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        token = _sec.token_urlsafe(32)
        conn.execute("UPDATE password_reset_tokens SET used=1 WHERE user_id=? AND used=0",
                     [found_user["id"]])
        conn.execute("INSERT INTO password_reset_tokens (user_id,token,expires_at) VALUES (?,?,?)",
                     [found_user["id"], token, expires_at])
        conn.commit()
        conn.close()
        from app.mailer import send_password_reset_email
        send_password_reset_email(email, found_slug, token)
        log_auth_event("PW_RESET", username=found_user.get("username", email),
                       ip=ip, tenant=found_slug, detail="cross-domain reset link sent")

    return jsonify({"ok": True})


@bp.route("/auto-login")
def auto_login():
    """Consume a cross-login token issued by /_cross-login and start a session."""
    token = request.args.get("token", "").strip()
    next_target = _request_next_target()
    if not token:
        return redirect(url_for("auth.login_page"))

    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
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
    impersonation = bool(row["impersonation"]) if "impersonation" in row.keys() else False

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

    # Enforce the same 2FA gate used by the normal tenant login flow.
    totp_enabled = user["totp_enabled"] if "totp_enabled" in user.keys() else 0
    two_fa = _email_2fa_required(user)
    email = user["email"] if "email" in user.keys() else None
    ip = request.remote_addr or "unknown"
    from config import Config as _Cfg
    if totp_enabled and user.get("totp_secret"):
        session.clear()
        session["pending_2fa_user_id"] = user["id"]
        session["pending_2fa_method"] = "totp"
        if impersonation:
            session["pending_platform_impersonation"] = True
        if next_target:
            session["post_login_redirect"] = next_target
        log_auth_event("2FA_TOTP_CHALLENGE", username=user["username"], ip=ip,
                       tenant=tenant_slug, detail="via cross-login token")
        return render_template("verify_2fa.html", method="totp")
    if two_fa:
        if not email:
            log_auth_event("2FA_BLOCKED_NO_EMAIL", username=user["username"], ip=ip,
                           tenant=tenant_slug, detail="via cross-login token")
            return render_template(
                "login.html",
                error="Two-factor authentication is enabled for this account, but no email address is set."
            ), 403
        if not _Cfg.SMTP_HOST:
            log_auth_event("2FA_BLOCKED_NO_SMTP", username=user["username"], ip=ip,
                           tenant=tenant_slug, detail="via cross-login token")
            return render_template(
                "login.html",
                error="Two-factor authentication is enabled for this account, but email delivery is not configured on this server. Contact your admin."
            ), 503
        import random
        from app.mailer import send_email as _send_email
        otp = f"{random.randint(0, 999999):06d}"
        expires_at = (_utc_now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        execute("UPDATE login_otp SET used=1 WHERE user_id=? AND used=0", [user["id"]])
        execute("INSERT INTO login_otp (user_id,otp,expires_at) VALUES (?,?,?)",
                [user["id"], otp, expires_at])
        _send_email(
            email,
            "Your CountDepot login code",
            f"<p style='font-family:sans-serif'>Your one-time login code is:</p>"
            f"<p style='font-family:monospace;font-size:32px;font-weight:700;letter-spacing:8px'>{otp}</p>"
            f"<p style='font-family:sans-serif;font-size:12px;color:#888'>Expires in 10 minutes. If you didn't request this, ignore it.</p>",
            f"Your CountDepot login code: {otp}\nExpires in 10 minutes."
        )
        session.clear()
        session["pending_2fa_user_id"] = user["id"]
        session["pending_2fa_method"] = "email"
        if impersonation:
            session["pending_platform_impersonation"] = True
        if next_target:
            session["post_login_redirect"] = next_target
        log_auth_event("2FA_SENT", username=user["username"], ip=ip,
                       tenant=tenant_slug, detail="via cross-login token")
        return render_template("verify_2fa.html", method="email")

    perms  = get_user_perms(user["id"], user["role"],
                            user["permissions"] if "permissions" in user.keys() else "")
    if impersonation:
        return _finalize_session(
            user,
            perms,
            next_target=next_target,
            track_login=False,
            impersonation=True,
        )
    return _finalize_session(
        user,
        perms,
        next_target=next_target,
        track_login=True,
        login_result="ok",
        auth_detail="LOGIN_OK",
    )


@bp.route("/auth/google")
def auth_google():
    """Redirect to Google for OAuth2 sign-in."""
    from app import oauth
    if oauth is None:
        return render_template("login.html",
                               error="Google sign-in is not configured. Contact your administrator.")
    redirect_uri = url_for("auth.auth_google_callback", _external=True)
    # Store mode (signin vs signup) and whether this is a bare-domain request
    mode = request.args.get("mode", "signin")
    return oauth.google.authorize_redirect(redirect_uri, state=mode)


@bp.route("/auth/google/callback")
def auth_google_callback():
    """Google OAuth2 callback — handles both bare-domain (platform) and tenant contexts."""
    from app import oauth
    if oauth is None:
        return redirect(url_for("auth.login_page"))

    ip   = request.remote_addr or "unknown"
    mode = request.args.get("state", "signin")

    try:
        token     = oauth.google.authorize_access_token()
        user_info = token.get("userinfo") or {}
    except Exception as e:
        log_auth_event("GOOGLE_AUTH_ERROR", username="(google)", ip=ip,
                       tenant=getattr(g, "tenant_slug", ""), detail=str(e))
        return render_template("login.html", error="Google sign-in failed. Please try again.")

    email = (user_info.get("email") or "").strip().lower()
    if not email:
        return render_template("login.html",
                               error="Google did not return an email address. Please try again.")
    if not user_info.get("email_verified", True):
        return render_template("login.html",
                               error="Your Google account email is not verified.")

    given  = user_info.get("given_name", "")
    family = user_info.get("family_name", "")
    name   = (f"{given} {family}".strip() or email.split("@")[0])[:50]

    tenant_slug = getattr(g, "tenant_slug", None)

    # ── Bare-domain path (countdepot.com) ────────────────────────────────────
    # Find which tenant this email belongs to; issue a cross-login token.
    if not tenant_slug:
        import os as _os, sqlite3 as _sql
        from config import Config as _Cfg
        from app.platform import get_platform_db as _get_pdb

        pdb     = _get_pdb()
        tenants = pdb.execute("SELECT slug FROM tenants WHERE active=1").fetchall()
        pdb.close()

        found_slug = None
        found_user = None
        for row in tenants:
            slug    = row[0]
            db_path = _os.path.join(_Cfg.TENANTS_DIR, slug, "inventory.db")
            if not _os.path.exists(db_path):
                continue
            conn = _sql.connect(db_path)
            conn.row_factory = _sql.Row
            u = conn.execute(
                "SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?", [email]
            ).fetchone()
            conn.close()
            if u:
                found_slug = slug
                found_user = dict(u)
                break

        if found_slug:
            # Existing user — generate cross-login token and redirect to their subdomain
            import secrets as _sec
            from app.platform import get_platform_db as _get_pdb2
            token_val  = _sec.token_urlsafe(32)
            now        = _utc_now()
            expires_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            created_at = now.strftime("%Y-%m-%d %H:%M:%S")
            pdb2 = _get_pdb2()
            pdb2.execute(
                "INSERT INTO cross_login_tokens (tenant_slug,user_id,token,expires_at,created_at) "
                "VALUES (?,?,?,?,?)",
                [found_slug, found_user["id"], token_val, expires_at, created_at])
            pdb2.commit()
            pdb2.close()
            log_auth_event("GOOGLE_OK", username=found_user.get("username", email),
                           ip=ip, tenant=found_slug, detail="via Google, bare-domain")
            host     = request.host.split(":")[0]
            parts    = host.split(".")
            base     = ".".join(parts[-2:]) if len(parts) >= 2 else host
            scheme   = "https" if not ("localhost" in host or "127.0.0.1" in host) else "http"
            return redirect(f"{scheme}://{found_slug}.{base}/auto-login?token={token_val}")

        # No existing account — redirect to signup with email pre-filled
        from urllib.parse import urlencode
        params = urlencode({"google_email": email, "google_name": name})
        return redirect(f"/signup?{params}")

    # ── Tenant path (subdomain) ──────────────────────────────────────────────
    user = query("SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?", [email], one=True)

    if not user:
        sso_cfg = query("SELECT jit_enabled, jit_default_role, jit_default_permissions "
                        "FROM sso_config LIMIT 1", one=True)
        jit_ok = sso_cfg["jit_enabled"] if sso_cfg else False
        if jit_ok:
            role  = (sso_cfg["jit_default_role"] or "worker") if sso_cfg else "worker"
            perms = (sso_cfg["jit_default_permissions"] or "") if sso_cfg else ""
            import secrets as _sec
            uid = execute(
                "INSERT INTO users (username, password, role, permissions, email, "
                "email_verified, must_change_password) VALUES (?,?,?,?,?,1,0)",
                [name, _sec.token_hex(32), role, perms, email])
            user = query("SELECT * FROM users WHERE id=?", [uid], one=True)
            log_auth_event("GOOGLE_JIT", username=name, ip=ip, tenant=tenant_slug,
                           detail=f"JIT-provisioned via Google; email={email}")
        else:
            return render_template(
                "login.html",
                error="No account found for this Google email. "
                      "Ask your administrator to add you to this workspace.")

    totp_enabled = user["totp_enabled"] if "totp_enabled" in user.keys() else 0
    two_fa = _email_2fa_required(user)
    if totp_enabled and user.get("totp_secret"):
        session.clear()
        session["pending_2fa_user_id"] = user["id"]
        session["pending_2fa_method"] = "totp"
        log_auth_event("2FA_TOTP_CHALLENGE", username=user["username"], ip=ip,
                       tenant=tenant_slug, detail="via Google sign-in")
        return render_template("verify_2fa.html", method="totp")
    if two_fa:
        if not email:
            log_auth_event("2FA_BLOCKED_NO_EMAIL", username=user["username"], ip=ip,
                           tenant=tenant_slug, detail="via Google sign-in")
            return render_template(
                "login.html",
                error="Two-factor authentication is enabled for this account, but no email address is set."
            ), 403
        from config import Config as _Cfg
        if not _Cfg.SMTP_HOST:
            log_auth_event("2FA_BLOCKED_NO_SMTP", username=user["username"], ip=ip,
                           tenant=tenant_slug, detail="via Google sign-in")
            return render_template(
                "login.html",
                error="Two-factor authentication is enabled for this account, but email delivery is not configured on this server. Contact your admin."
            ), 503
        import random
        from app.mailer import send_email as _send_email
        otp = f"{random.randint(0, 999999):06d}"
        expires_at = (_utc_now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        execute("UPDATE login_otp SET used=1 WHERE user_id=? AND used=0", [user["id"]])
        execute("INSERT INTO login_otp (user_id,otp,expires_at) VALUES (?,?,?)",
                [user["id"], otp, expires_at])
        _send_email(
            email,
            "Your CountDepot login code",
            f"<p style='font-family:sans-serif'>Your one-time login code is:</p>"
            f"<p style='font-family:monospace;font-size:32px;font-weight:700;letter-spacing:8px'>{otp}</p>"
            f"<p style='font-family:sans-serif;font-size:12px;color:#888'>Expires in 10 minutes. If you didn't request this, ignore it.</p>",
            f"Your CountDepot login code: {otp}\nExpires in 10 minutes."
        )
        session.clear()
        session["pending_2fa_user_id"] = user["id"]
        session["pending_2fa_method"] = "email"
        log_auth_event("2FA_SENT", username=user["username"], ip=ip,
                       tenant=tenant_slug, detail="via Google sign-in")
        return render_template("verify_2fa.html", method="email")

    perms = get_user_perms(user["id"], user["role"],
                           user["permissions"] if "permissions" in user.keys() else "")
    sess = _build_session(user, perms)
    session.clear()
    session.update(sess)
    now_str = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE users SET last_login=?, session_token=? WHERE id=?",
            [now_str, sess["session_token"], user["id"]])
    execute("INSERT INTO login_log (user_id,username,ip_address,user_agent,result,ts) VALUES (?,?,?,?,?,?)",
            [user["id"], user["username"], ip, request.user_agent.string, "ok_google", now_str])
    log_auth_event("GOOGLE_OK", username=user["username"], ip=ip, tenant=tenant_slug)
    return redirect(url_for("main.inventory"))


@bp.route("/sso/metadata")
def sso_metadata():
    """Return SAML SP metadata XML — share this URL with your Identity Provider."""
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
    except ImportError:
        return "SAML library not installed on server", 503

    settings_dict, err = _build_saml_settings()
    if err:
        return err, 400

    try:
        saml_settings = OneLogin_Saml2_Settings(settings=settings_dict, sp_validation_only=True)
        metadata = saml_settings.get_sp_metadata()
        errors   = saml_settings.validate_metadata(metadata)
        if errors:
            return f"Metadata validation errors: {', '.join(errors)}", 400
        return metadata, 200, {"Content-Type": "text/xml"}
    except Exception as e:
        return f"Could not generate metadata: {e}", 500


@bp.route("/sso/login")
def sso_login():
    """Redirect the browser to the IdP for SAML login."""
    if session.get("user_id") and check_session_expiry():
        return redirect(url_for("main.inventory"))

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError:
        return "SAML library not installed on this server. Contact your administrator.", 503

    settings_dict, err = _build_saml_settings()
    if err:
        return render_template("login.html", error=f"SSO is not configured: {err}")

    try:
        req  = _prepare_saml_request(request)
        auth = OneLogin_Saml2_Auth(req, old_settings=settings_dict)
        return redirect(auth.login())
    except Exception as e:
        return render_template("login.html", error=f"SSO login failed: {e}")


@bp.route("/sso/acs", methods=["POST"])
def sso_acs():
    """SAML Assertion Consumer Service — receives and validates IdP response."""
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError:
        return "SAML library not installed", 503

    settings_dict, err = _build_saml_settings()
    if err:
        return render_template("login.html", error=f"SSO not configured: {err}")

    cfg = query("SELECT * FROM sso_config LIMIT 1", one=True)
    ip  = request.remote_addr or "unknown"

    try:
        req  = _prepare_saml_request(request)
        auth = OneLogin_Saml2_Auth(req, old_settings=settings_dict)
        auth.process_response()
        errors = auth.get_errors()
        if errors:
            log_auth_event("SSO_FAIL", username="(saml)", ip=ip,
                           tenant=getattr(g, "tenant_slug", ""),
                           detail="; ".join(errors))
            return render_template("login.html",
                                   error="SSO authentication failed. Please contact your administrator.")

        if not auth.is_authenticated():
            return render_template("login.html", error="SSO: Not authenticated.")

        # Extract attributes
        attrs      = auth.get_attributes()
        name_id    = auth.get_nameid() or ""
        attr_email = cfg["attr_email"] if cfg else "email"
        attr_user  = cfg["attr_username"] if cfg else "username"
        attr_fn    = cfg["attr_firstname"] if cfg else "firstName"
        attr_ln    = cfg["attr_lastname"] if cfg else "lastName"

        email    = _saml_attr(attrs, attr_email) or name_id
        username = _saml_attr(attrs, attr_user) or email.split("@")[0]
        fname    = _saml_attr(attrs, attr_fn) or ""
        lname    = _saml_attr(attrs, attr_ln) or ""

        if not email:
            return render_template("login.html",
                                   error="SSO: Could not retrieve email address from IdP.")

        # Look up user by email
        user = query("SELECT * FROM users WHERE LOWER(COALESCE(email,''))=?",
                     [email.lower()], one=True)

        # JIT provisioning — create user on first login if enabled
        if not user and cfg and cfg["jit_enabled"]:
            role        = cfg["jit_default_role"] or "worker"
            perms_str   = cfg["jit_default_permissions"] or ""
            display_name = (f"{fname} {lname}".strip() or username)[:50]
            import secrets as _sec
            uid = execute(
                "INSERT INTO users (username, password, role, permissions, email, email_verified, must_change_password) "
                "VALUES (?, ?, ?, ?, ?, 1, 0)",
                [display_name, _sec.token_hex(32), role, perms_str, email.lower()])
            user = query("SELECT * FROM users WHERE id=?", [uid], one=True)
            log_auth_event("SSO_JIT", username=display_name, ip=ip,
                           tenant=getattr(g, "tenant_slug", ""),
                           detail=f"JIT-provisioned via SSO; email={email}")

        if not user:
            return render_template("login.html",
                                   error="Your account is not provisioned in this workspace. "
                                         "Contact your administrator.")

        perms = get_user_perms(user["id"], user["role"],
                               user["permissions"] if "permissions" in user.keys() else "")
        sess = _build_session(user, perms)
        session.clear()
        session.update(sess)
        now_str = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
        execute("UPDATE users SET last_login=?, session_token=? WHERE id=?",
                [now_str, sess["session_token"], user["id"]])
        execute("INSERT INTO login_log (user_id,username,ip_address,user_agent,result,ts) VALUES (?,?,?,?,?,?)",
                [user["id"], user["username"], ip, request.user_agent.string, "ok_sso", now_str])
        log_auth_event("SSO_OK", username=user["username"], ip=ip,
                       tenant=getattr(g, "tenant_slug", ""))

        relay = auth.get_last_request_id()
        return redirect(url_for("main.inventory"))

    except Exception as e:
        log_auth_event("SSO_ERROR", username="(saml)", ip=ip,
                       tenant=getattr(g, "tenant_slug", ""), detail=str(e))
        return render_template("login.html", error=f"SSO error: {e}")


# ── SSO helpers ───────────────────────────────────────────────────────────────

def _prepare_saml_request(req):
    """Convert Flask request to the dict that python3-saml expects."""
    return {
        "https":       "on" if req.is_secure or req.headers.get("X-Forwarded-Proto") == "https" else "off",
        "http_host":   req.host,
        "script_name": req.path,
        "get_data":    req.args.copy(),
        "post_data":   req.form.copy(),
        "server_port": req.environ.get("SERVER_PORT", "443"),
    }


def _build_saml_settings():
    """Return (settings_dict, None) or (None, error_string)."""
    cfg = query("SELECT * FROM sso_config LIMIT 1", one=True)
    if not cfg or not cfg["enabled"]:
        return None, "SSO is not enabled for this workspace"
    if not cfg["idp_entity_id"] or not cfg["idp_sso_url"] or not cfg["idp_cert"]:
        return None, "SSO configuration is incomplete (missing IdP Entity ID, SSO URL, or certificate)"

    scheme = "https"
    from flask import current_app
    if current_app.debug:
        scheme = "http"

    from flask import request as _req
    host = _req.host if _req else "localhost"

    sp_base = f"{scheme}://{host}"
    settings = {
        "strict": True,
        "debug":  False,
        "sp": {
            "entityId": f"{sp_base}/sso/metadata",
            "assertionConsumerService": {
                "url":     f"{sp_base}/sso/acs",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "singleLogoutService": {
                "url":     f"{sp_base}/sso/slo",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            "x509cert":  "",
            "privateKey": "",
        },
        "idp": {
            "entityId": cfg["idp_entity_id"],
            "singleSignOnService": {
                "url":     cfg["idp_sso_url"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "singleLogoutService": {
                "url":     cfg["idp_slo_url"] or "",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": cfg["idp_cert"],
        },
    }
    return settings, None


def _saml_attr(attrs, key):
    """Safely extract the first value of a SAML attribute."""
    val = attrs.get(key, [])
    return val[0].strip() if val else None


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
            expires_at = (_utc_now() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
            execute("UPDATE email_verification_tokens SET used=1 WHERE user_id=? AND used=0",
                    [user["id"]])
            execute("INSERT INTO email_verification_tokens (user_id, token, expires_at) VALUES (?,?,?)",
                    [user["id"], token, expires_at])
            send_verification_email(email, g.tenant_slug, token)

    # Always show the same message — don't reveal whether account exists or is unverified
    return render_template("login.html",
                           error="If your account exists and is unverified, a new link has been sent. Check your inbox.",
                           unverified=False)
