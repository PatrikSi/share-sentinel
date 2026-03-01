# Testing and Validation

## Automated Tests

Backend tests include:

- auth and security helpers
- request context and dependency resolution
- token scope logic
- admin safety guards
- settings router coverage
- user all-project assignment coverage

Run in container:

```bash
docker compose run --rm api bash -lc "pip install -q -r requirements-dev.txt && pytest -q"
```

## Smoke Validation

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

## Deployment Checks

After API/UI changes:

```bash
docker compose build api ui
docker compose up -d api ui gateway
```

Then re-run smoke validation.
