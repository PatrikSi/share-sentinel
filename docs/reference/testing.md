# Testing and Validation

## Automated tests

Backend tests include:

- auth and security helpers
- request context and dependency resolution
- token scope logic
- admin safety guards
- settings router coverage
- user all-project assignment coverage
- source registration, failed/disabled observations, freshness, and automatic-baseline idempotency
- finding deduplication, reopen/authoritative resolution, accepted-risk expiry, revision conflicts, and per-finding bulk audit
- durable comparison retry/recovery plus item add, remove, move, ambiguity, permission-change, and cursor-resume behavior
- bounded effective-access evidence and unknown-on-incomplete semantics

Collector tests include authentication identity parsing, read-only SMB capability evidence, DFS/NFS truthfulness, national-cloud endpoint isolation, certificate safety, bounded Graph retries/paging, governance-field compatibility fallbacks, and state upgrades. UI tests cover monitoring payload normalization, structured error propagation, bounded fetches, and finding-assignment behavior; typechecking and a production build remain required even when unit tests pass.

Run all component suites from a Python 3.11 environment:

```bash
cd api && pip install -r requirements-dev.txt && ruff check . && pytest -q -p no:cacheprovider
cd ../worker && pip install -r requirements-dev.txt && ruff check . && pytest -q -p no:cacheprovider
cd ../collector && pip install -r requirements-dev.txt && ruff check . && pytest -q -p no:cacheprovider
cd ../ui && npm ci && npm audit --audit-level=high && npm test && npm run typecheck && npm run build
cd .. && ruff check scripts && python scripts/validate-sample.py && python scripts/check-release.py
```

CI runs those checks independently, builds and vulnerability-scans API, worker, UI, and collector images, and exercises both development and production-style live stacks. A dedicated PostgreSQL 16 job runs the audit-attribution DDL, trigger, parent-rename/delete, and bounded-backfill tests with the integration-test URL set, so these tests cannot silently skip in either branch or tag gates. Successful `main` runs publish staging candidates, pull and scan those exact registry artifacts, smoke-test a production stack from them, and promote an immutable commit tag. A run advances `latest` only while its commit remains the live branch head. Tag workflows reuse and re-verify that commit set before release promotion.

To run that focused database contract against an existing disposable PostgreSQL database:

```bash
cd api
AUDIT_ATTRIBUTION_TEST_DATABASE_URL='postgresql+psycopg://user:password@127.0.0.1:5432/test_database' \
  pytest -q -p no:cacheprovider tests/test_audit_attribution_migration.py
```

The database principal must be able to create and drop temporary schemas. The tests isolate each run in a uniquely named schema and perform best-effort cleanup.

## Smoke validation

End-to-end smoke checks verify:

- UI project route returns app shell
- UI settings route returns app shell
- API health endpoint returns `ok=true`
- Settings API route is correctly routed (not proxy 404)
- Auth login route is wired to API (not proxy 404)
- A tracked artifact reaches `COMPLETE` through Redis and the worker
- Normalized endpoint, resource, item, and warning counts match the fixture
- Synthetic SharePoint provider IDs, library type, exposure evidence, canonical metadata, and provider filters survive end-to-end ingest
- Two collector-shaped SharePoint snapshots retain assessment context and correlate stable IDs across folder/file moves, renames, and deletion
- Project deletion requires an exact-name confirmation and cleans up its artifact
- Production hides API docs, emits security headers, uses secure cookies, and allows only the configured CORS origin

Run:

```bash
./scripts/smoke-routes.sh http://localhost
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
./scripts/smoke-ingest.sh http://localhost admin@example.com
./scripts/smoke-sharepoint-ingest.sh http://localhost admin@example.com
unset SHARE_SENTINEL_SMOKE_PASSWORD
```

The SharePoint smoke uploads the tracked full-sync and delta-shaped fixtures into one temporary project, verifies both runs reach `COMPLETE`, checks provider-filtered inventory, and asserts that run comparison reports two moves and one removal without treating the snapshot modes as different assessment perspectives.

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
