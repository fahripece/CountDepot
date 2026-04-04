#!/usr/bin/env python3
"""
Create a new CountDepot tenant from the command line.

Usage:
    python scripts/create_tenant.py --slug acme --name "Acme Corp" --password "TempPass1!" [--plan standard]

Useful for:
  - Initial setup before the platform web UI is accessible
  - Scripting bulk tenant creation
  - Dev/staging environment setup
"""
import sys
import os
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.platform import create_tenant, get_tenant_by_slug
from app.blueprints.platform import _bootstrap_tenant_db


def main():
    parser = argparse.ArgumentParser(description="Create a CountDepot tenant")
    parser.add_argument("--slug",     required=True, help="Tenant slug (becomes subdomain)")
    parser.add_argument("--name",     required=True, help="Company display name")
    parser.add_argument("--password", required=True, help="Admin account temporary password")
    parser.add_argument("--plan",     default="standard", choices=["standard","pro","healthcare"],
                        help="Subscription plan (default: standard)")
    args = parser.parse_args()

    # Validate slug
    import re
    if not re.match(r'^[a-z0-9][a-z0-9\-]{1,31}$', args.slug):
        print(f"Error: slug '{args.slug}' is invalid. Use lowercase letters, numbers, hyphens (2-32 chars).")
        sys.exit(1)

    if len(args.password) < 8:
        print("Error: password must be at least 8 characters.")
        sys.exit(1)

    app = create_app()
    with app.app_context():
        # Check if slug already exists
        existing = get_tenant_by_slug(args.slug)
        if existing:
            print(f"Error: tenant '{args.slug}' already exists.")
            sys.exit(1)

        print(f"Creating tenant '{args.slug}' ({args.name})...")
        create_tenant(args.slug, args.name, args.plan)
        _bootstrap_tenant_db(args.slug, args.password)

        print(f"""
✓ Tenant created successfully

  Slug:     {args.slug}
  Name:     {args.name}
  Plan:     {args.plan}
  URL:      https://{args.slug}.yourdomain.com
  Login:    admin / {args.password}

The admin will be forced to change their password on first login.
They'll then choose their business sector to configure categories.
""")


if __name__ == "__main__":
    main()
