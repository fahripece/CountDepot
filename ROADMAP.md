# CountDepot Roadmap

## Where We Stand

CountDepot is already a real multi-tenant inventory platform, not a blank-slate MVP.
Today the codebase includes:

- Multi-tenant tenant isolation with one SQLite database per tenant
- Platform admin workspace creation and management
- Self-serve signup, onboarding, email verification, password reset
- Session auth, role/permission checks, API keys, Google login, SAML SSO, 2FA options
- Category -> Product -> Item inventory structure
- Checkout/check-in, scan-required flows, print-and-scan labels
- Multi-location support, departments, reservations, kits, maintenance/service logs
- Import/export workflows, invoice import, low-stock logic, procurement and purchase orders
- Billing and subscription gating scaffolding
- Webhooks, audit logging, support tooling, backups, and deployment scripts

The biggest need now is not adding random features. It is making the existing surface area
more reliable, more testable, and easier to operate.

---

## Phase 1: Stabilize the Product

### 1. Automated testing foundation
This is the top priority.

Goals:
- Add repeatable integration tests for tenant bootstrap, login, onboarding, product add, item add, and inventory fetch
- Run tests automatically in GitHub Actions before deployment
- Keep tests isolated with temporary databases so they are safe and deterministic

Definition of done:
- `pytest` runs locally and in CI
- Main branch deploys only after tests pass
- Core smoke tests cover the flows we touch most often during debugging

### 2. Tenant bootstrap and migration hardening
We have a lot of schema evolution already, so upgrades need to be boring and safe.

Goals:
- Make every tenant self-heal forward through migrations
- Prevent new tenants from being created with outdated schema
- Add more explicit migration/version visibility
- Add admin diagnostics for tenant bootstrap state

Definition of done:
- Fresh tenants, old tenants, and restored tenants all pass the same smoke tests
- No manual DB patching is needed for normal upgrades

### 3. UI copy and workflow consistency pass
There are still rough edges from fast iteration.

Goals:
- Remove client-specific or confusing sample text
- Standardize placeholder examples, labels, empty states, and validation messages
- Review item, product, procurement, and importer flows for terminology consistency

Definition of done:
- UI text is generic, professional, and internally consistent
- Common error states tell the user exactly what to do next

---

## Phase 2: Expand Test Coverage

### 4. Auth and account lifecycle coverage
Add tests for:

- Email login
- Legacy username fallback
- Logout
- Forced password change
- Forgot/reset password
- Email verification
- 2FA challenge flow
- Permission enforcement by role

### 5. Inventory lifecycle coverage
Add tests for:

- Product add/edit/delete
- Item add/edit/clone/delete
- Duplicate serial and SKU validation
- Quantity-tracked vs serial-tracked items
- Low-stock calculations
- Label/scan-required save paths

### 6. Operational and tenant controls coverage
Add tests for:

- Platform tenant creation
- Tenant onboarding redirect behavior
- Subscription expired/overdue gating
- Site restrictions
- Department restrictions
- API key access

Definition of done:
- The highest-risk flows are covered by tests before we expand feature scope further

---

## Phase 3: Reliability and Operations

### 7. Deployment safety
Goals:
- Add test gating to deployment
- Add clear rollback and restore steps
- Document test commands and local troubleshooting

### 8. Monitoring and recovery
Goals:
- Improve health checks and diagnostics
- Add tenant repair tooling for common failure cases
- Make backup restore a tested operational workflow, not just a script on disk

### 9. Codebase maintainability
Goals:
- Start breaking up very large files like `app/blueprints/api.py`
- Group routes by domain area
- Reduce hidden coupling between tenant bootstrap, schema, and request lifecycle

---

## Phase 4: Product Growth After Stability

These come after the platform is consistently testable and stable.

### 10. Procurement and receiving polish
- Better receiving workflow from purchase orders
- Better vendor catalog linking and suggested ordering
- Better import reconciliation

### 11. Attachments and richer asset records
- Item photos
- Documents/manuals/warranty attachments
- Better item history presentation

### 12. Notification improvements
- Email digests
- Better low-stock notifications
- More actionable in-app alerts

### 13. Mobile-first workflow improvements
- Smoother scanner-heavy flows on mobile
- Better PWA behavior for warehouse/site use

---

## Testing Priorities

### Must-have coverage
- Tenant creation to first successful login
- Login to onboarding to inventory
- Product creation
- Item creation
- Inventory listing/search
- Permission denial for restricted users

### Next coverage
- Checkout/check-in
- Imports
- Procurement
- Billing/subscription gates
- API keys and webhooks
- Backup/restore verification

---

## What We Should Not Do Yet

- Do not add large new feature families before the core flows have automated coverage
- Do not rely on manual QA for every release
- Do not assume old tenant databases will always match current code without explicit tests

---

## Immediate Next Steps

1. Land the automated test harness and CI workflow
2. Grow coverage around auth, onboarding, and inventory lifecycle
3. Do a focused bug bash on imports, checkout, and permissions
4. Refactor the largest route modules once tests are protecting behavior
