# Automated Testing

## Goal

Make the core CountDepot flows verifiable without manual clicking every time.

The current test suite focuses on high-value integration coverage:

- tenant bootstrap
- login
- onboarding redirect behavior
- product creation
- item creation
- permission enforcement

These tests use temporary databases, so they do not touch real tenant data.

## Local Run

Install the test dependencies:

```bash
pip install -r requirements-test.txt
```

Run the suite:

```bash
pytest -q
```

Run one file:

```bash
pytest tests/test_app_flows.py -q
```

## CI

GitHub Actions runs the test suite automatically.

Current behavior:

- pull requests run tests
- pushes to `main` run tests first
- deployment only runs after the test job passes

## Expand Coverage Next

Recommended next test files:

- `tests/test_auth_flows.py`
- `tests/test_inventory_rules.py`
- `tests/test_checkout_flows.py`
- `tests/test_importer_flows.py`
- `tests/test_platform_tenants.py`

## Ground Rules

- Prefer integration tests over brittle UI-only tests for now
- Use temporary tenant databases per test run
- Keep each test focused on one business flow
- Add a regression test whenever we fix a real bug
