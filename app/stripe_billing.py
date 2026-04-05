"""
Stripe billing helpers for CountDepot.

All Stripe API calls go through here. If STRIPE_SECRET_KEY is not set,
functions return None gracefully so the app works without Stripe configured.
"""

from config import Config

# ── Plan definitions ──────────────────────────────────────────────────────────

PLANS = {
    "starter": {
        "name":       "Starter",
        "max_users":  5,
        "max_items":  500,
        "monthly":    20,
        "yearly":     200,
        "excel":      False,
        "api_access": False,
        "features":   [
            "5 users",
            "500 items",
            "All core features",
            "Barcode scanning",
            "Financial reports",
            "Email support",
        ],
    },
    "pro": {
        "name":       "Pro",
        "max_users":  15,
        "max_items":  None,   # unlimited
        "monthly":    50,
        "yearly":     500,
        "excel":      True,
        "api_access": False,
        "features":   [
            "15 users",
            "Unlimited items",
            "Everything in Starter",
            "Priority support",
            "Advanced reporting",
            "Excel import / export",
        ],
    },
    "enterprise": {
        "name":       "Enterprise",
        "max_users":  None,   # unlimited
        "max_items":  None,   # unlimited
        "monthly":    80,
        "yearly":     800,
        "excel":      True,
        "api_access": True,
        "features":   [
            "Unlimited users",
            "Unlimited items",
            "Everything in Pro",
            "Dedicated support",
            "Custom onboarding",
            "REST API access",
        ],
    },
}

PLAN_ORDER = ["starter", "pro", "enterprise"]


def _price_id(plan: str, period: str) -> str:
    key = f"STRIPE_PRICE_{plan.upper()}_{period.upper()}"
    return getattr(Config, key, "") or ""


def price_id_for(plan: str, period: str) -> str:
    return _price_id(plan, period)


def plan_for_price_id(price_id: str):
    """Reverse lookup: (plan_key, period) from a Stripe Price ID, or (None, None)."""
    for plan_key in PLANS:
        if price_id and price_id == _price_id(plan_key, "monthly"):
            return plan_key, "monthly"
        if price_id and price_id == _price_id(plan_key, "yearly"):
            return plan_key, "yearly"
    return None, None


# ── Stripe API wrappers ───────────────────────────────────────────────────────

def _stripe():
    if not Config.STRIPE_SECRET_KEY:
        return None
    import stripe
    stripe.api_key = Config.STRIPE_SECRET_KEY
    return stripe


def create_customer(email: str, name: str, slug: str):
    """Create a Stripe customer. Returns customer ID or None."""
    s = _stripe()
    if not s:
        return None
    try:
        c = s.Customer.create(email=email, name=name,
                              metadata={"slug": slug})
        return c.id
    except Exception:
        return None


def create_checkout_session(customer_id: str, price_id: str, slug: str,
                             success_url: str, cancel_url: str):
    """Create a Stripe Checkout Session. Returns the session or None."""
    s = _stripe()
    if not s:
        return None
    try:
        return s.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            metadata={"slug": slug},
        )
    except Exception:
        return None


def create_portal_session(customer_id: str, return_url: str):
    """Create a Stripe Customer Portal session. Returns the session or None."""
    s = _stripe()
    if not s:
        return None
    try:
        return s.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
    except Exception:
        return None


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify and parse a Stripe webhook payload. Returns event or None."""
    s = _stripe()
    if not s or not Config.STRIPE_WEBHOOK_SECRET:
        return None
    try:
        return s.Webhook.construct_event(
            payload, sig_header, Config.STRIPE_WEBHOOK_SECRET)
    except Exception:
        return None
