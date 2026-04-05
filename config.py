import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Config:
    SECRET_KEY        = os.environ.get("SECRET_KEY", "change-me-in-production")
    BASE_DIR          = BASE_DIR
    DATA_DIR          = os.path.join(BASE_DIR, "data")
    TENANTS_DIR       = os.path.join(BASE_DIR, "data", "tenants")
    PLATFORM_DB_PATH  = os.path.join(BASE_DIR, "data", "platform.db")
    # The local dev slug — used when there's no subdomain (localhost)
    DEV_TENANT_SLUG   = os.environ.get("DEV_TENANT_SLUG", "dev")
    # Root domain — used to build tenant URLs like acme.yourdomain.com
    APP_DOMAIN        = os.environ.get("APP_DOMAIN", "localhost:5000")
    # Email (SMTP) — optional. If SMTP_HOST is blank, emails are logged instead.
    SMTP_HOST         = os.environ.get("SMTP_HOST", "")
    SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER         = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD     = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM         = os.environ.get("SMTP_FROM", "noreply@yourdomain.com")
    SMTP_USE_TLS      = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    # Set to "false" to disable self-serve signup (require platform admin to create tenants)
    SIGNUP_ENABLED    = os.environ.get("SIGNUP_ENABLED", "true").lower() == "true"
    # Platform admin MFA — set this to enable email OTP after password login
    PLATFORM_ADMIN_EMAIL = os.environ.get("PLATFORM_ADMIN_EMAIL", "")
    # Stripe billing
    STRIPE_SECRET_KEY          = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY     = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET      = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICE_STARTER_MONTHLY    = os.environ.get("STRIPE_PRICE_STARTER_MONTHLY", "")
    STRIPE_PRICE_STARTER_YEARLY     = os.environ.get("STRIPE_PRICE_STARTER_YEARLY", "")
    STRIPE_PRICE_PRO_MONTHLY        = os.environ.get("STRIPE_PRICE_PRO_MONTHLY", "")
    STRIPE_PRICE_PRO_YEARLY         = os.environ.get("STRIPE_PRICE_PRO_YEARLY", "")
    STRIPE_PRICE_ENTERPRISE_MONTHLY = os.environ.get("STRIPE_PRICE_ENTERPRISE_MONTHLY", "")
    STRIPE_PRICE_ENTERPRISE_YEARLY  = os.environ.get("STRIPE_PRICE_ENTERPRISE_YEARLY", "")
