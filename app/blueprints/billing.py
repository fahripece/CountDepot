"""
Billing routes for CountDepot.

/billing          — tenant billing page (current plan, upgrade)
/billing/checkout — create Stripe Checkout session
/billing/success  — post-payment success landing
/billing/portal   — Stripe Customer Portal
/_stripe/webhook  — Stripe webhook endpoint (no CSRF, signature-verified)
"""

from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, session,
                   redirect, url_for, jsonify, g)

from app.helpers        import login_required, admin_required
from app.platform       import get_platform_db
from app.stripe_billing import (PLANS, PLAN_ORDER, price_id_for,
                                plan_for_price_id, create_customer,
                                create_checkout_session, create_portal_session,
                                construct_webhook_event)
from config import Config

bp = Blueprint("billing", __name__)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _update_tenant(slug: str, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    db = get_platform_db()
    try:
        db.execute(f"UPDATE tenants SET {sets} WHERE slug=?",
                   list(kwargs.values()) + [slug])
        db.commit()
    finally:
        db.close()


def _tenant_row(slug: str):
    db = get_platform_db()
    try:
        return db.execute("SELECT * FROM tenants WHERE slug=?", [slug]).fetchone()
    finally:
        db.close()


def _ensure_stripe_customer(tenant) -> str | None:
    """Return existing Stripe customer ID or create one now."""
    cid = tenant.get("stripe_customer_id") if hasattr(tenant, "get") else tenant["stripe_customer_id"]
    if cid:
        return cid
    slug  = tenant["slug"]
    name  = tenant["name"]
    # Try to get admin email from tenant DB
    from app.db import query
    row = query("SELECT email FROM users WHERE role='admin' AND email IS NOT NULL LIMIT 1", one=True)
    email = row["email"] if row else f"admin@{slug}.countdepot.com"
    cid = create_customer(email, name, slug)
    if cid:
        _update_tenant(slug, stripe_customer_id=cid)
    return cid


def _trial_days_remaining(tenant) -> int | None:
    trial_ends = tenant.get("trial_ends_at") if hasattr(tenant, "get") else tenant["trial_ends_at"]
    if not trial_ends:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            delta = datetime.strptime(trial_ends[:len(fmt)], fmt) - _utc_now()
            return max(0, delta.days)
        except ValueError:
            continue
    return None


# ── Billing page ──────────────────────────────────────────────────────────────

@bp.route("/billing")
@login_required
def billing_page():
    tenant   = dict(_tenant_row(g.tenant_slug))
    plan_key = tenant.get("plan") or "starter"
    if plan_key not in PLANS:
        plan_key = "starter"
    status       = tenant.get("subscription_status", "trial")
    trial_days   = _trial_days_remaining(tenant) if status == "trial" else None
    current_plan = PLANS.get(plan_key, PLANS["starter"])
    stripe_ok    = bool(Config.STRIPE_SECRET_KEY)

    from app.db import query
    user_count = query("SELECT COUNT(*) FROM users", one=True)[0]
    item_count = query("SELECT COUNT(*) FROM items WHERE active=1 AND sold=0", one=True)[0]
    max_users  = current_plan["max_users"]
    max_items  = current_plan["max_items"]

    return render_template("billing.html",
        tenant=tenant, status=status, plan_key=plan_key,
        current_plan=current_plan, trial_days=trial_days,
        plans=PLANS, plan_order=PLAN_ORDER,
        user_count=user_count, max_users=max_users,
        item_count=item_count, max_items=max_items,
        stripe_ok=stripe_ok,
        stripe_pub_key=Config.STRIPE_PUBLISHABLE_KEY)


# ── Checkout ──────────────────────────────────────────────────────────────────

@bp.route("/billing/checkout", methods=["POST"])
@login_required
@admin_required
def billing_checkout():
    if not Config.STRIPE_SECRET_KEY:
        return jsonify({"ok": False, "msg": "Stripe is not configured."})

    d      = request.json or {}
    plan   = d.get("plan", "")
    period = d.get("period", "monthly")
    if plan not in PLANS or period not in ("monthly", "yearly"):
        return jsonify({"ok": False, "msg": "Invalid plan or period."})

    pid = price_id_for(plan, period)
    if not pid:
        return jsonify({"ok": False, "msg": f"Stripe price ID not set for {plan}/{period}. "
                        f"Add STRIPE_PRICE_{plan.upper()}_{period.upper()} to your server environment."})

    tenant = dict(_tenant_row(g.tenant_slug))
    cid    = _ensure_stripe_customer(tenant)
    if not cid:
        return jsonify({"ok": False, "msg": "Could not create Stripe customer. Check STRIPE_SECRET_KEY in your server environment."})

    domain  = Config.APP_DOMAIN
    base    = f"https://{g.tenant_slug}.{domain}"
    session_obj, err = create_checkout_session(
        cid, pid, g.tenant_slug,
        success_url=f"{base}/billing/success",
        cancel_url=f"{base}/billing")

    if not session_obj:
        return jsonify({"ok": False, "msg": f"Stripe error: {err}"})

    return jsonify({"ok": True, "url": session_obj.url})


# ── Success landing ───────────────────────────────────────────────────────────

@bp.route("/billing/success")
@login_required
def billing_success():
    return render_template("billing_success.html")


# ── Customer portal ───────────────────────────────────────────────────────────

@bp.route("/billing/portal")
@login_required
@admin_required
def billing_portal():
    if not Config.STRIPE_SECRET_KEY:
        return redirect(url_for("billing.billing_page"))

    tenant = dict(_tenant_row(g.tenant_slug))
    cid    = tenant.get("stripe_customer_id")
    if not cid:
        return redirect(url_for("billing.billing_page"))

    domain     = Config.APP_DOMAIN
    return_url = f"https://{g.tenant_slug}.{domain}/billing"
    portal     = create_portal_session(cid, return_url)
    if not portal:
        return redirect(url_for("billing.billing_page"))
    return redirect(portal.url)


# ── Billing status (polled by success page) ───────────────────────────────────

@bp.route("/api/billing/status")
@login_required
def billing_status():
    tenant = dict(_tenant_row(g.tenant_slug))
    return jsonify({
        "status":     tenant.get("subscription_status", "trial"),
        "plan":       tenant.get("plan", "starter"),
        "trial_days": _trial_days_remaining(tenant),
    })


# ── Stripe webhook ────────────────────────────────────────────────────────────

@bp.route("/_stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    event      = construct_webhook_event(payload, sig_header)
    if event is None:
        return jsonify({"ok": False}), 400

    etype = event["type"]
    data  = event["data"]["object"]

    if etype == "checkout.session.completed":
        _handle_checkout_complete(data)

    elif etype in ("invoice.payment_succeeded", "invoice.paid"):
        _handle_payment_succeeded(data)

    elif etype == "invoice.payment_failed":
        _handle_payment_failed(data)

    elif etype in ("customer.subscription.deleted", "customer.subscription.canceled"):
        _handle_subscription_canceled(data)

    elif etype == "customer.subscription.updated":
        _handle_subscription_updated(data)

    return jsonify({"ok": True})


def _handle_checkout_complete(session_obj):
    import logging
    log = logging.getLogger(__name__)
    slug = session_obj.get("metadata", {}).get("slug")
    if not slug:
        return
    sub_id   = session_obj.get("subscription")
    price_id = None
    plan_key = None
    # Retrieve subscription to get the price
    try:
        import stripe
        stripe.api_key = Config.STRIPE_SECRET_KEY
        sub      = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
        price_id = sub["items"]["data"][0]["price"]["id"]
        plan_key, _period = plan_for_price_id(price_id)
    except Exception as e:
        log.error(f"Stripe: could not retrieve subscription {sub_id} for {slug}: {e}")

    updates = dict(subscription_status="active", stripe_subscription_id=sub_id, trial_ends_at=None)
    if plan_key:
        updates["plan"] = plan_key
    # If plan lookup failed, leave the existing plan unchanged rather than downgrading to starter
    _update_tenant(slug, **updates)


def _handle_payment_succeeded(invoice):
    sub_id = invoice.get("subscription")
    if not sub_id:
        return
    db = get_platform_db()
    try:
        row = db.execute("SELECT slug FROM tenants WHERE stripe_subscription_id=?",
                         [sub_id]).fetchone()
        if row:
            _update_tenant(row["slug"], subscription_status="active")
    finally:
        db.close()


def _handle_payment_failed(invoice):
    sub_id = invoice.get("subscription")
    if not sub_id:
        return
    db = get_platform_db()
    try:
        row = db.execute("SELECT slug FROM tenants WHERE stripe_subscription_id=?",
                         [sub_id]).fetchone()
        if row:
            _update_tenant(row["slug"], subscription_status="overdue")
    finally:
        db.close()


def _handle_subscription_canceled(subscription):
    sub_id = subscription.get("id")
    if not sub_id:
        return
    db = get_platform_db()
    try:
        row = db.execute("SELECT slug FROM tenants WHERE stripe_subscription_id=?",
                         [sub_id]).fetchone()
        if row:
            _update_tenant(row["slug"], subscription_status="canceled")
    finally:
        db.close()


def _handle_subscription_updated(subscription):
    sub_id = subscription.get("id")
    if not sub_id:
        return
    try:
        price_id = subscription["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError):
        return
    plan_key, period = plan_for_price_id(price_id)
    if not plan_key:
        return
    db = get_platform_db()
    try:
        row = db.execute("SELECT slug FROM tenants WHERE stripe_subscription_id=?",
                         [sub_id]).fetchone()
        if row:
            _update_tenant(row["slug"], plan=plan_key)
    finally:
        db.close()
