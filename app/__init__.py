import hmac
import secrets

from flask import Flask, request, jsonify, redirect, url_for, g, session, abort
from config import Config
from app.platform import init_platform_db
from app.tenant import resolve_tenant
from app.db import close_db, query
from app.schema import init_db


def create_app():
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.from_object(Config)
    app.secret_key = Config.SECRET_KEY

    # Ensure platform.db (tenant registry) exists on startup
    init_platform_db()

    @app.context_processor
    def _inject_csrf():
        """Make csrf_token + brand settings available in every template."""
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_hex(32)
        brand_name     = getattr(g, "brand_name",     None)
        brand_color    = getattr(g, "brand_color",    None)
        banner_message = getattr(g, "banner_message", None)
        return {"csrf_token": session["_csrf_token"],
                "brand_name": brand_name, "brand_color": brand_color,
                "banner_message": banner_message}

    @app.before_request
    def before():
        # ── API key authentication ────────────────────────────────────────────
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer sk_live_"):
            raw_key = auth_header[len("Bearer "):]
            import hashlib as _hl
            key_hash = _hl.sha256(raw_key.encode()).hexdigest()
            # Tenant must be resolved first — fall through if no tenant yet
            # (tenant resolution happens below, so we do API key check after)
            g.pending_api_key = raw_key
            g.pending_api_key_hash = key_hash

        # CSRF validation — skip for API key requests and cross-login (uses JSON body token)
        csrf_exempt = (request.path == "/_health" or request.path == "/_stripe/webhook"
                       or request.path == "/_cross-login"
                       or bool(auth_header.startswith("Bearer sk_live_")))
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and not csrf_exempt):
            token = (request.form.get("csrf_token")
                     or request.headers.get("X-CSRF-Token"))
            expected = session.get("_csrf_token", "")
            if not token or not expected or not hmac.compare_digest(token, expected):
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"ok": False, "msg": "CSRF validation failed"}), 403
                abort(403)

        # These routes bypass tenant resolution entirely
        if (request.path.startswith("/_platform")
                or request.path == "/_health"
                or request.path == "/_stripe/webhook"
                or request.path.startswith("/signup")
                or request.path.startswith("/verify-signup")
                or request.path == "/resend-signup-verify"
                or request.path == "/_cross-login"):
            return

        # Bare domain (countdepot.com with no subdomain) → landing page
        host = request.host.split(":")[0]
        parts = host.split(".")
        is_bare_domain = (
            host not in ("localhost", "127.0.0.1")
            and len(parts) < 3
        )
        if is_bare_domain:
            from flask import render_template
            return render_template("landing.html")

        resolve_tenant()

        # Only run schema init once per tenant per process (not every request)
        from app.db import _initialized_tenants
        if g.tenant_slug not in _initialized_tenants:
            init_db()
            _initialized_tenants.add(g.tenant_slug)

        # ── Resolve API key after tenant is set ──────────────────────────────
        if getattr(g, "pending_api_key", None):
            import hashlib as _hl
            from app.db import query as _q
            row = _q("SELECT ak.*, u.role, u.permissions, u.email FROM api_keys ak "
                     "JOIN users u ON u.id=ak.user_id "
                     "WHERE ak.key_hash=? AND ak.active=1", [g.pending_api_key_hash], one=True)
            if row:
                g.api_user_id          = row["user_id"]
                g.api_user_role        = row["role"]
                g.api_user_permissions = row["permissions"] or ""
                g.api_key_auth         = True
                from app.db import execute as _ex
                from datetime import datetime as _dt2
                _ex("UPDATE api_keys SET last_used=? WHERE id=?",
                    [_dt2.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]])

        # ── Load brand settings ───────────────────────────────────────────────
        if getattr(g, "tenant_slug", None):
            try:
                from app.db import query as _bq
                brand_name_row  = _bq("SELECT value FROM settings WHERE key='brand_name'",  one=True)
                brand_color_row = _bq("SELECT value FROM settings WHERE key='brand_color'", one=True)
                g.brand_name  = (brand_name_row["value"]  if brand_name_row  else None) or g.tenant.get("name")
                g.brand_color = (brand_color_row["value"] if brand_color_row else None) or "#0f172a"
                banner_row    = _bq("SELECT value FROM settings WHERE key='banner_message'", one=True)
                g.banner_message = banner_row["value"] if banner_row else None
            except Exception:
                g.brand_name     = g.tenant.get("name") if hasattr(g, "tenant") and g.tenant else None
                g.brand_color    = "#0f172a"
                g.banner_message = None

        # Routes that don't need the intercept checks
        skip = ("/onboarding", "/login", "/logout", "/change-password",
                "/forgot-password", "/reset-password", "/verify-email",
                "/resend-verification", "/billing", "/static", "/signup",
                "/verify-signup", "/resend-signup-verify", "/auto-login")
        if any(request.path.startswith(s) for s in skip):
            return

        # API key requests skip session checks — auth is via the key itself
        if getattr(g, "api_key_auth", False):
            return

        if not session.get("user_id"):
            return  # login_required on the actual route handles this

        # Session expiry
        from app.blueprints.auth import check_session_expiry
        if not check_session_expiry():
            return redirect(url_for("auth.login_page"))

        # Verify session token — catches force-logged-out users
        db_tok = query("SELECT session_token FROM users WHERE id=?",
                       [session["user_id"]], one=True)
        if not db_tok or db_tok["session_token"] != session.get("session_token"):
            session.clear()
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"ok": False, "msg": "Session invalidated"}), 401
            return redirect(url_for("auth.login_page"))

        # Force password change on first login — block API too
        if session.get("must_change_password"):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"ok": False,
                                "msg": "Password change required. Please log in via the web interface."}), 403
            return redirect(url_for("auth.change_password"))

        # Sector onboarding — must pick business type before using app
        if not g.tenant.get("onboarded"):
            return redirect(url_for("onboarding.index"))

        # Subscription enforcement — block expired/overdue tenants
        sub_status  = g.tenant.get("subscription_status", "trial")
        trial_ends  = g.tenant.get("trial_ends_at") or ""
        from datetime import datetime as _dt
        now_iso = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if sub_status == "trial" and trial_ends and now_iso > trial_ends:
            # Trial expired — update DB
            from app.platform import get_platform_db as _get_pdb
            _pdb = _get_pdb()
            _pdb.execute("UPDATE tenants SET subscription_status='expired' WHERE slug=?",
                         [g.tenant_slug])
            _pdb.commit()
            _pdb.close()
            sub_status = "expired"
        if sub_status in ("expired", "overdue", "canceled"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False,
                                "msg": "Subscription required. Visit /billing to subscribe."}), 402
            return redirect(url_for("billing.billing_page"))

    app.teardown_appcontext(close_db)

    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]         = "SAMEORIGIN"
        response.headers["X-XSS-Protection"]        = "1; mode=block"
        response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
        return response

    @app.errorhandler(404)
    def err_404(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "Not found"}), 404
        return redirect(url_for("auth.login_page"))

    @app.errorhandler(500)
    def err_500(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "Internal server error"}), 500
        return redirect(url_for("auth.login_page"))

    # ── Register blueprints ───────────────────────────────────────────────────
    from app.blueprints.auth       import bp as auth_bp
    from app.blueprints.main       import bp as main_bp
    from app.blueprints.api        import bp as api_bp
    from app.blueprints.platform   import bp as platform_bp
    from app.blueprints.onboarding import bp as onboarding_bp
    from app.blueprints.signup     import bp as signup_bp
    from app.blueprints.billing    import bp as billing_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(platform_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(signup_bp)
    app.register_blueprint(billing_bp)

    return app
