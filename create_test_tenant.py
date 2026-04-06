"""
Quick script to create (or reset) the test tenant.
Usage:  python create_test_tenant.py [slug] [password]
Defaults: slug=test, password=test123

The test tenant will be available at:
  http://test.localhost:5000  (or test.<yourdomain>)
"""
import sys
from app import create_app
from app.platform import create_tenant, get_tenant_by_slug
from app.blueprints.platform import _bootstrap_tenant_db

SLUG     = sys.argv[1] if len(sys.argv) > 1 else "test"
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "test123"

app = create_app()
with app.app_context():
    existing = get_tenant_by_slug(SLUG)
    if existing:
        print(f"Tenant '{SLUG}' already exists — resetting admin password via DB.")
        import sqlite3, os
        from config import Config
        db_path = os.path.join(Config.TENANTS_DIR, SLUG, "inventory.db")
        from app.helpers import hash_pw
        con = sqlite3.connect(db_path)
        con.execute("UPDATE users SET password=?, must_change_password=0 WHERE username='admin'",
                    [hash_pw(PASSWORD)])
        con.commit(); con.close()
        print(f"Admin password reset to: {PASSWORD}")
    else:
        create_tenant(SLUG, f"{SLUG.title()} Test Tenant", "standard")
        _bootstrap_tenant_db(SLUG, PASSWORD)
        print(f"Created tenant '{SLUG}'.")

    print(f"\n✓ Test tenant ready:")
    print(f"  URL:      http://{SLUG}.localhost:5000")
    print(f"  Username: admin")
    print(f"  Password: {PASSWORD}")
    print(f"\nThen go to Admin → Workspace Sector to switch between sectors for testing.")
