from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, redirect, url_for

from app.db import query, execute
from app.helpers import (hash_pw, verify_pw, check_login_rate, clear_login_rate,
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
        ip = request.remote_addr or "unknown"
        allowed, reset_in = check_login_rate(ip)
        if not allowed:
            log_auth_event("RATE_LIMITED", username=username, ip=ip,
                           tenant=getattr(g, "tenant_slug", ""),
                           detail=f"Login rate limit hit; retry in {reset_in}s")
            return render_template(
                "login.html",
                error=f"Too many login attempts. Try again in {reset_in} seconds."
            ), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query("SELECT * FROM users WHERE username=?", [username], one=True)
        if user and verify_pw(password, user["password"]):
            # Auto-migrate legacy HMAC-SHA256 hashes to bcrypt on first successful login
            if not user["password"].startswith("$2"):
                execute("UPDATE users SET password=? WHERE id=?",
                        [hash_pw(password), user["id"]])
            # Block self-signup accounts that haven't verified their email yet
            email_verified = user["email_verified"] if "email_verified" in user.keys() else 1
            email = user["email"] if "email" in user.keys() else None
            if email and not email_verified:
                error = "Please verify your email address before logging in. Check your inbox for the verification link."
                return render_template("login.html", error=error)
            clear_login_rate(ip)
            perms = get_user_perms(
                user["id"], user["role"],
                user["permissions"] if "permissions" in user.keys() else "")
            now = datetime.utcnow()
            session.clear()
            session["user_id"]              = user["id"]
            session["username"]             = user["username"]
            session["role"]                 = user["role"]
            session["permissions"]          = ",".join(perms)
            session["must_change_password"] = bool(user["must_change_password"])
            session["expires_at"]           = (now + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
            execute("UPDATE users SET last_login=? WHERE id=?",
                    [now.strftime("%Y-%m-%d %H:%M:%S"), user["id"]])
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
    session.clear()
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
        if len(new_pw) < 8:
            error = "Password must be at least 8 characters."
        elif new_pw != confirm:
            error = "Passwords do not match."
        else:
            execute("UPDATE users SET password=?, must_change_password=0 WHERE id=?",
                    [hash_pw(new_pw), session["user_id"]])
            session["must_change_password"] = False
            log_auth_event("PW_CHANGE", username=session.get("username", ""),
                           ip=request.remote_addr or "",
                           tenant=getattr(g, "tenant_slug", ""))
            return redirect(url_for("main.inventory"))
    return render_template("change_password.html", error=error)


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Step 1 — user enters email, gets a reset link."""
    if session.get("user_id"):
        return redirect(url_for("main.inventory"))
    sent = False
    if request.method == "POST":
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
    return render_template("forgot_password.html", sent=sent)


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
        if len(new_pw) < 8:
            error = "Password must be at least 8 characters."
        elif new_pw != confirm:
            error = "Passwords do not match."
        else:
            execute("UPDATE users SET password=?, must_change_password=0 WHERE id=?",
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
