"""
Onboarding blueprint — sector picker shown on a tenant's very first login.

Flow:
  1. User logs in as normal
  2. before_request sees tenant.onboarded == 0 → redirects here
  3. User picks their sector
  4. Categories seeded, tenant marked onboarded
  5. Redirect to inventory
"""

import sqlite3
import os

from flask import (Blueprint, render_template, request, session,
                   redirect, url_for, g)

from app.helpers  import login_required
from app.platform import set_tenant_sector
from app.sectors  import SECTORS, seed_sector_categories
from config       import Config

bp = Blueprint("onboarding", __name__)


@bp.route("/onboarding", methods=["GET"])
@login_required
def index():
    # If already onboarded, skip straight to inventory
    if g.tenant.get("onboarded"):
        return redirect(url_for("main.inventory"))
    return render_template("onboarding.html",
                           sectors=SECTORS,
                           tenant=g.tenant)


@bp.route("/onboarding/choose", methods=["POST"])
@login_required
def choose():
    sector_key = request.form.get("sector", "").strip()
    if sector_key not in SECTORS:
        return redirect(url_for("onboarding.index"))

    # 1. Open the tenant's DB directly (we're still in request context so get_db() works)
    from app.db import get_db
    db = get_db()

    # 2. Replace categories with sector-specific set
    seed_sector_categories(db, sector_key)

    # 3. Mark tenant as onboarded in platform.db
    set_tenant_sector(g.tenant_slug, sector_key)

    # 4. Update g.tenant so the intercept doesn't fire again this request
    g.tenant["onboarded"] = 1
    g.tenant["sector"]    = sector_key

    return redirect(url_for("main.inventory"))
