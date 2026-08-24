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
cd api && pip install -r requirements-dev.txt && ruff check . && pytest -q -p no:cacheprovider
cd ../worker && pip install -r requirements-dev.txt && ruff check . && pytest -q -p no:cacheprovider
cd ../collector && pip install -r requirements-dev.txt && ruff check . && pytest -q -p no:cacheprovider
cd ../ui && npm ci && npm audit --audit-level=high && npm run build
cd .. && ruff check scripts && python scripts/validate-sample.py && python scripts/check-release.py
```

CI runs those checks independently, builds and vulnerability-scans API, worker, UI, and collector images, and exercises both development and production-style live stacks. Successful `main` runs publish staging candidates, pull and scan those exact registry artifacts, smoke-test a production stack from them, and promote an immutable commit tag. A run advances `latest` only while its commit remains the live branch head. Tag workflows reuse and re-verify that commit set before release promotion.

## Smoke validation

End-to-end smoke checks verify:

- UI project route returns app shell
- UI settings route returns app shell
- API health endpoint returns `ok=true`
- Settings API route is correctly routed (not proxy 404)
- Auth login route is wired to API (not proxy 404)
- A tracked artifact reaches `COMPLETE` through Redis and the worker
- Normalized endpoint, resource, item, and warning counts match the fixture
- Project deletion requires an exact-name confirmation and cleans up its artifact
- Production hides API docs, emits security headers, uses secure cookies, and allows only the configured CORS origin

Run:

```bash
./scripts/smoke-routes.sh http://localhost
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
./scripts/smoke-ingest.sh http://localhost admin@example.com
unset SHARE_SENTINEL_SMOKE_PASSWORD
```

Against a production-style deployment, pass both the reachable gateway URL and the configured host router name:

```bash
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
./scripts/smoke-production.sh https://sentinel.example.com sentinel.example.com admin@example.com
unset SHARE_SENTINEL_SMOKE_PASSWORD
```

For a larger streaming ingest, generate a synthetic NDJSON artifact and pass it as the optional third argument. The smoke performs a dependency-free bounded structural/count validation before upload, waits for completion, verifies persisted inventory, and removes its temporary project:

```bash
python3 scripts/generate-capacity-artifact.py \
  --output /tmp/share-sentinel-capacity.ndjson.gz \
  --endpoints 10 \
  --shares-per-endpoint 10 \
  --items-per-share 1000
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
export SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS=900
./scripts/smoke-ingest.sh http://localhost admin@example.com /tmp/share-sentinel-capacity.ndjson.gz
unset SHARE_SENTINEL_SMOKE_PASSWORD SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS
```

See [Operations, scale, and recovery](../operations.md) for the workload model, acceptance signals, and failure-injection sequence. Run `./scripts/doctor.sh` for non-mutating route and local Compose diagnostics.

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
docker compose build api worker ui
docker compose up -d api ui gateway
```

Then re-run smoke validation.
