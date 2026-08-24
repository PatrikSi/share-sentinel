# API Service

FastAPI service for auth, project management, run lifecycle, artifact upload, and query endpoints.

## Key properties

- JWT access + refresh tokens
- Hashed API tokens (project scoped)
- Project-scoped RBAC (admin/operator/viewer)
- API token scopes + configurable expiry
- Sysadmin user management endpoints (`/users`)
- Optional self-registration with admin approval workflow
- Login lockout protection with Redis + in-memory fallback
- Password policy and self-service password changes
- Optional cookie session support with CSRF protection for unsafe methods
- Request IDs and request logging
- Backward-compatible structured error envelopes with stable codes and request IDs
- Redis-backed fixed-window rate limits (auth + upload)
- Atomic rate-limit counter expiry and bounded Redis connect/read timeouts
- Deep health endpoint for database and Redis readiness (`/healthz/deep`)
- Async ingestion queueing via Redis Streams
- Trusted-host enforcement and explicit production configuration validation
- Raw streaming artifact uploads with filename-aware JSON/NDJSON/gzip classification
- Off-event-loop, short-lived database phases around long artifact streams
- Bounded keyset collections and matching tenant/run pagination indexes
- Provider-aware SharePoint inventory fields, stable resource/item identities, collection-context reporting, and evidence-based exposure filters

## Local run (without Docker)

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

The API only auto-loads `.env` from the current working directory. If your `.env` lives at the repo root, either:

- run the API from the repo root with `alembic -c api/alembic.ini upgrade head` and `uvicorn app.main:app --app-dir api --reload`
- or copy/link `.env` into `api/` before using the commands above

If you also run the worker outside Docker, point both processes at the same `ARTIFACT_STORAGE_PATH` so uploaded artifacts and ingestion reads use the same shared directory.

Password policy is controlled through environment variables:

- `PASSWORD_MIN_LENGTH`
- `PASSWORD_REQUIRE_LOWERCASE`
- `PASSWORD_REQUIRE_UPPERCASE`
- `PASSWORD_REQUIRE_NUMBER`
- `PASSWORD_REQUIRE_SPECIAL`

If `SEED_ADMIN_PASSWORD` is configured and does not match the active policy, startup fails with a configuration error so the container logs point directly at the invalid env values.

Redis dependency calls use `REDIS_CONNECT_TIMEOUT_SECONDS` (default `3`) and `REDIS_SOCKET_TIMEOUT_SECONDS` (default `5`). Upload queue handoff retries remain bounded; if all attempts fail, the run stays `UPLOADED` for worker recovery and the response reports `queued: false`.

Atomic rate-limit expiry uses Redis `EVAL`. The bundled Redis image supports it; a managed Redis ACL must permit `EVAL` or general API rate limits fail according to `RATE_LIMIT_FAIL_OPEN` while login protection uses its bounded process-local fallback.

API Postgres connections have explicit pool and query budgets. Defaults are a pool of `10`, overflow of `20`, a 10-second pool wait, five-second connect, 30-second statement timeout, and five-second lock timeout. Configure them with the `API_DATABASE_*` settings and keep the aggregate pool across all API replicas below the Postgres connection budget.

Inventory CSV exports are admitted at two levels before streaming: Redis limits each actor/client identity to `API_INVENTORY_EXPORT_RATE_LIMIT` starts per `API_INVENTORY_EXPORT_RATE_WINDOW_SECONDS` (defaults `12` per `60` seconds), and each API process permits `API_INVENTORY_EXPORT_MAX_CONCURRENT` active exports (default `4`). The process limit applies independently to every API replica; size the aggregate against database and egress capacity. Redis unavailability follows `RATE_LIMIT_FAIL_OPEN`, while local capacity exhaustion returns `503` with `Retry-After`.

Alembic bootstrap uses separate migration budgets: `MIGRATION_DATABASE_CONNECT_TIMEOUT_SECONDS` defaults to `10` and `MIGRATION_DATABASE_LOCK_TIMEOUT_MS` defaults to `60000`. Migration statements intentionally do not inherit the short API statement timeout because concurrent index builds may legitimately run for a long time after acquiring their locks.

Synchronous run diff is intentionally bounded. `API_RUN_DIFF_MAX_ITEMS` defaults to `250000` total items across both runs; larger comparisons return an actionable `422` instead of risking process exhaustion. Detail arrays default to 500 records each (request maximum 2000), preserve exact aggregate counts, and return explicit truncation metadata.

Provider-backed run diffs correlate items by stable provider identity within a resource. A path change is reported as a move/rename rather than one removal plus one addition. Diff responses also report whether collection perspectives are comparable; callers should treat a missing, partial, differently scoped, cross-tenant, or differently assessed identity context as a semantic warning even when structural differences are still returned.

First-party artifact clients send the file itself as the request body to `POST /projects/{project_id}/runs/{run_id}/artifact` with:

- `Content-Type: application/json` for `.json`
- `Content-Type: application/x-ndjson` for `.ndjson` or `.jsonl`
- `Content-Type: application/gzip` for gzip variants
- `X-Artifact-Filename` set to a basename ending in `.json`, `.json.gz`, `.ndjson`, `.ndjson.gz`, `.jsonl`, or `.jsonl.gz`

The filename header is limited to 255 characters and cannot contain path separators, controls, or leading/trailing whitespace. Multipart file upload remains available for compatibility. The API ends its authentication transaction before reading the body, stores each attempt under an immutable key, then uses short-lived database sessions off the async event loop to recheck authorization/state, acquire the run lock, select the artifact, and record queue outcome. Cancellation waits for each bounded durable database phase to finish, so a committed pointer or required cleanup is not abandoned halfway through.

`UPLOAD_MAX_BYTES`, `UPLOAD_CHUNK_BYTES`, `REDIS_STREAM_RETRIES`, `REDIS_STREAM_MAXLEN`, token lifetimes, and login-throttle counts/windows must be positive. Upload chunks must not exceed the upload limit or 128 MiB. Invalid settings fail application startup instead of silently disabling the relevant control.

Production-style `APP_ENV` values also require secure cookies, explicit `TRUSTED_HOSTS`, and valid `TRUSTED_PROXY_CIDRS`. Interactive API docs are disabled in those environments. See the repository [deployment guide](../docs/deployment.md) before exposing the service.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q -p no:cacheprovider
```
