import hmac
import secrets

from flask import Flask, request, jsonify, redirect, url_for, g, session, abort
from config import Config
from app.platform import init_platform_db
from app.tenant import resolve_tenant
from app.db import close_db, query
from app.schema import init_db

# ── Google OAuth2 ─────────────────────────────────────────────────────────────
try:
    from authlib.integrations.flask_client import OAuth as _OAuth
    _oauth_available = True
except ImportError:
    _oauth_available = False

oauth = None  # set in create_app()


def create_app():
    global oauth

    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.from_object(Config)
    app.secret_key = Config.SECRET_KEY

    # ── Google OAuth2 client ──────────────────────────────────────────────────
    if _oauth_available and Config.GOOGLE_CLIENT_ID:
        oauth = _OAuth(app)
        oauth.register(
            name="google",
            client_id=Config.GOOGLE_CLIENT_ID,
            client_secret=Config.GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    # ── Sentry error monitoring ───────────────────────────────────────────────
    _sentry_dsn = Config.SENTRY_DSN
    if _sentry_dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.flask import FlaskIntegration
            sentry_sdk.init(
                dsn=_sentry_dsn,
                integrations=[FlaskIntegration()],
                traces_sample_rate=float(Config.SENTRY_TRACES_SAMPLE_RATE),
                send_default_pii=False,   # never send passwords/tokens to Sentry
                environment=Config.ENV,
            )
        except ImportError:
            pass  # sentry-sdk not installed; harmless

    # ── Gzip compression for HTML/JSON/CSS responses ──────────────────────────
    try:
        from flask_compress import Compress
        Compress(app)
    except ImportError:
        pass  # flask-compress not installed yet; harmless

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
        host = request.host.split(":")[0]
        if host == "www.countdepot.com":
            target = request.url.replace("//www.countdepot.com", "//countdepot.com", 1)
            return redirect(target, code=301 if request.method in ("GET", "HEAD") else 308)

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
                       or request.path == "/api/demo-request"
                       or request.path == "/_cross-login"
                       or request.path == "/_cross-forgot-password"
                       or bool(auth_header.startswith("Bearer sk_live_")))
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and not csrf_exempt):
            token = (request.form.get("csrf_token")
                     or request.headers.get("X-CSRF-Token"))
            expected = session.get("_csrf_token", "")
            if not token or not expected or not hmac.compare_digest(token, expected):
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"ok": False, "msg": "CSRF validation failed"}), 403
                abort(403)

        from app.seo_pages import seo_page_for_slug
        path_slug = request.path.strip("/")

        # These routes bypass tenant resolution entirely
        if (request.path.startswith("/_platform")
                or request.path == "/_health"
                or request.path == "/_stripe/webhook"
                or request.path == "/api/demo-request"
                or request.path == "/manifest.webmanifest"
                or request.path == "/sw.js"
                or request.path == "/robots.txt"
                or request.path == "/sitemap.xml"
                or request.path == "/sitemap"
                or seo_page_for_slug(path_slug)
                or request.path.startswith("/static/")
                or request.path.startswith("/signup")
                or request.path.startswith("/verify-signup")
                or request.path == "/resend-signup-verify"
                or request.path == "/_cross-login"
                or request.path == "/_cross-forgot-password"
                or request.path.startswith("/auth/google")):
            return

        # Bare domain (countdepot.com with no subdomain) → landing page
        parts = host.split(".")
        is_bare_domain = (
            host not in ("localhost", "127.0.0.1")
            and not host.endswith(".localhost")
            and len(parts) < 3
        )
        if is_bare_domain and not request.path.startswith("/auth/google"):
            from flask import render_template
            return render_template("landing.html")

        resolve_tenant()

        # Full schema init (CREATE TABLE etc.) runs once per tenant per process.
        # Migrations run on every request so new columns are never missed when
        # the server stays up across deploys.
        from app.db import _initialized_tenants, _migrated_tenants
        from app.schema import run_migrations_only
        if g.tenant_slug not in _initialized_tenants:
            init_db()
            _initialized_tenants.add(g.tenant_slug)
            _migrated_tenants.add(g.tenant_slug)
        else:
            run_migrations_only()
            _migrated_tenants.add(g.tenant_slug)

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

        # ── Load brand settings (single query for all keys) ───────────────────
        if getattr(g, "tenant_slug", None):
            try:
                from app.db import query as _bq
                _settings_rows = _bq(
                    "SELECT key, value FROM settings WHERE key IN ('brand_name','brand_color','banner_message')"
                )
                _settings = {r["key"]: r["value"] for r in _settings_rows}
                g.brand_name     = _settings.get("brand_name") or g.tenant.get("name")
                g.brand_color    = _settings.get("brand_color") or "#0f172a"
                g.banner_message = _settings.get("banner_message")
                # Trial countdown banner (overrides manual banner when trial is expiring soon)
                if not g.banner_message:
                    sub_st = g.tenant.get("subscription_status", "") if g.tenant else ""
                    trial_e = g.tenant.get("trial_ends_at", "") if g.tenant else ""
                    if sub_st == "trial" and trial_e:
                        try:
                            from datetime import datetime as _dtb, timedelta as _tdb
                            days_left = (_dtb.fromisoformat(trial_e[:19]) - _dtb.utcnow()).days
                            if days_left <= 7:
                                g.banner_message = (
                                    f"Your free trial expires in {max(0, days_left)} day{'s' if days_left != 1 else ''}. "
                                    "Upgrade at /billing to keep access."
                                )
                        except Exception:
                            pass
            except Exception:
                g.brand_name     = g.tenant.get("name") if hasattr(g, "tenant") and g.tenant else None
                g.brand_color    = "#0f172a"
                g.banner_message = None

        # Routes that don't need the intercept checks
        skip = ("/onboarding", "/login", "/logout", "/change-password",
                "/forgot-password", "/reset-password", "/verify-email",
                "/resend-verification", "/billing", "/static", "/signup",
                "/verify-signup", "/resend-signup-verify", "/auto-login",
                "/auth/google", "/sso/")
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
        db_user = query(
            "SELECT id, username, role, permissions, must_change_password, session_token "
            "FROM users WHERE id=?",
            [session["user_id"]],
            one=True,
        )
        if not db_user or db_user["session_token"] != session.get("session_token"):
            session.clear()
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"ok": False, "msg": "Session invalidated"}), 401
            return redirect(url_for("auth.login_page"))

        # Keep active sessions in sync with admin permission/site changes.
        from app.helpers import get_user_perms, get_user_location_ids
        perms = get_user_perms(
            db_user["id"],
            db_user["role"],
            db_user["permissions"] if "permissions" in db_user.keys() else "",
        )
        session["username"] = db_user["username"]
        session["role"] = db_user["role"]
        session["permissions"] = ",".join(perms)
        session["must_change_password"] = bool(db_user["must_change_password"])
        session["location_ids"] = get_user_location_ids(db_user["id"], db_user["role"])

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
        from datetime import datetime as _dt, timedelta as _td
        now_iso = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        # Fallback: if trial has no end date, derive one from created_at + 30 days
        if sub_status == "trial" and not trial_ends:
            created_at = g.tenant.get("created_at") or ""
            if created_at:
                try:
                    trial_ends = (_dt.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S") + _td(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
        if sub_status == "trial" and trial_ends and now_iso > trial_ends:
            # Trial expired — update DB
            from app.platform import get_platform_db as _get_pdb
            _pdb = _get_pdb()
            try:
                trial_expired_at = _dt.strptime(trial_ends[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                trial_expired_at = _dt.strptime(trial_ends[:10], "%Y-%m-%d")
            delete_at = (trial_expired_at + _td(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            _pdb.execute(
                "UPDATE tenants SET subscription_status='expired', trial_expired_at=?, "
                "scheduled_delete_at=? WHERE slug=?",
                [trial_expired_at.strftime("%Y-%m-%d %H:%M:%S"), delete_at, g.tenant_slug],
            )
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
        # Permissions-Policy: disable unused browser APIs
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=(self)"
        )
        # Content-Security-Policy
        # Notes:
        #  - unsafe-inline required for script/style due to extensive inline JS/CSS
        #  - cdnjs: JsBarcode, Chart.js; fonts.googleapis/gstatic: IBM Plex
        #  - googletagmanager/google-analytics: Google Analytics tag + collection
        #  - js.stripe.com: Stripe checkout frames
        #  - data: blob: needed for item photo uploads / label rendering
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://js.stripe.com https://www.googletagmanager.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https: https://www.google-analytics.com https://stats.g.doubleclick.net; "
            "connect-src 'self' https://www.google-analytics.com https://analytics.google.com https://region1.google-analytics.com https://stats.g.doubleclick.net; "
            "frame-src https://js.stripe.com; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'"
        )
        # Long-lived cache for static assets (versioned by Flask's url_for ?v=...)
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.errorhandler(404)
    def err_404(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "Not found"}), 404
        return redirect(url_for("auth.login_page"))

    @app.errorhandler(500)
    def err_500(e):
        import traceback, threading
        tenant = getattr(g, "tenant_slug", "unknown")
        tb     = traceback.format_exc()
        def _alert():
            try:
                from app.mailer import send_error_alert
                send_error_alert(request.method, request.path, tenant, tb)
            except Exception:
                pass
        threading.Thread(target=_alert, daemon=True).start()
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
