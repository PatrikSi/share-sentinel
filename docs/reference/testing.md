# Testing and Validation

## Automated tests

Backend tests include:

- auth and security helpers
- request context and dependency resolution
- token scope logic
- admin safety guards
- settings router coverage
- user all-project assignment coverage

Run all component suites from a Python 3.11 environment:

```bash
cd api && pip install -r requirements-dev.txt && pytest -q -p no:cacheprovider
cd ../worker && pip install -r requirements-dev.txt && pytest -q -p no:cacheprovider
cd ../collector && pip install -r requirements-dev.txt && pytest -q -p no:cacheprovider
cd ../ui && npm ci && npm audit --audit-level=high && npm run build
cd .. && python scripts/validate-sample.py && python scripts/check-release.py
```

CI runs those checks independently and then builds every production container through Compose.

## Smoke validation

End-to-end smoke checks verify:

- UI project route returns app shell
- UI settings route returns app shell
- API health endpoint returns `ok=true`
- Settings API route is correctly routed (not proxy 404)
- Auth login route is wired to API (not proxy 404)

Run:

```bash
./scripts/smoke-routes.sh http://localhost
```

## Dependency audit

```bash
pip install pip-audit
pip-audit -r api/requirements-dev.txt
pip-audit -r worker/requirements-dev.txt
pip-audit -r collector/requirements-dev.txt
cd ui && npm audit --audit-level=high
```

## Deployment checks

After API/UI changes:

```bash
docker compose build api ui
docker compose up -d api ui gateway
```

Then re-run smoke validation.
