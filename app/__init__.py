import hmac
import secrets

from flask import Flask, request, jsonify, redirect, url_for, g, session, abort
from config import Config
from app.platform import init_platform_db
from app.tenant import resolve_tenant
from app.db import close_db
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
        """Make csrf_token available in every template."""
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_hex(32)
        return {"csrf_token": session["_csrf_token"]}

    @app.before_request
    def before():
        # CSRF validation — all state-changing requests except the health probe
        if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                and request.path != "/_health"):
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
                or request.path.startswith("/signup")):
            return

        # Bare domain (countdepot.com with no subdomain) → landing page
        host = request.host.split(":")[0]
        parts = host.split(".")
        import logging
        logging.getLogger(__name__).warning(f"DEBUG host={host} parts={parts} path={request.path}")
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

        # Routes that don't need the intercept checks
        skip = ("/onboarding", "/login", "/logout", "/change-password",
                "/forgot-password", "/reset-password", "/verify-email",
                "/static", "/signup")
        if any(request.path.startswith(s) for s in skip):
            return

        if not session.get("user_id"):
            return  # login_required on the actual route handles this

        # Session expiry
        from app.blueprints.auth import check_session_expiry
        if not check_session_expiry():
            return redirect(url_for("auth.login_page"))

        # Force password change on first login
        if session.get("must_change_password"):
            return redirect(url_for("auth.change_password"))

        # Sector onboarding — must pick business type before using app
        if not g.tenant.get("onboarded"):
            return redirect(url_for("onboarding.index"))

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
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(platform_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(signup_bp)

    return app
