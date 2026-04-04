from flask import g, request, abort
from app.platform import get_tenant_by_slug
from config import Config


def resolve_tenant():
    """Resolve the current tenant from the request subdomain.

    Called before every request. Attaches tenant info to Flask's g object.
    All downstream code reads g.tenant_slug and g.tenant to know who
    this request belongs to.

    Subdomain logic:
      - clienta.yourdomain.com  → slug = 'clienta'
      - localhost / 127.0.0.1   → slug = DEV_TENANT_SLUG (for local dev)
      - Unknown slug             → 404
      - Suspended tenant         → 403
    """
    host = request.host.split(":")[0]  # strip port if present

    # Local development — no real subdomain
    if host in ("localhost", "127.0.0.1"):
        slug = Config.DEV_TENANT_SLUG
    else:
        # e.g. "clienta.storelax.com" → "clienta"
        parts = host.split(".")
        if len(parts) < 3:
            # bare domain with no subdomain — could be marketing site later
            abort(404)
        slug = parts[0]

    tenant = get_tenant_by_slug(slug)

    if tenant is None:
        abort(404)

    if not tenant["active"]:
        abort(403)

    # Attach to g so every route and helper can access it
    g.tenant_slug = tenant["slug"]
    g.tenant      = dict(tenant)
