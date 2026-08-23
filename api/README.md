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
- Redis-backed fixed-window rate limits (auth + upload)
- Atomic rate-limit counter expiry and bounded Redis connect/read timeouts
- Deep health endpoint for database and Redis readiness (`/healthz/deep`)
- Async ingestion queueing via Redis Streams
- Trusted-host enforcement and explicit production configuration validation
- Raw streaming artifact uploads with filename-aware JSON/NDJSON/gzip classification

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

New Postgres connections use a five-second connect timeout. Database statement and transaction timeout policy remains a deployment-level control.

First-party artifact clients send the file itself as the request body to `POST /projects/{project_id}/runs/{run_id}/artifact` with:

- `Content-Type: application/json` for `.json`
- `Content-Type: application/x-ndjson` for `.ndjson` or `.jsonl`
- `Content-Type: application/gzip` for gzip variants
- `X-Artifact-Filename` set to a basename ending in `.json`, `.json.gz`, `.ndjson`, `.ndjson.gz`, `.jsonl`, or `.jsonl.gz`

The filename header is limited to 255 characters and cannot contain path separators, controls, or leading/trailing whitespace. Multipart file upload remains available for compatibility. The API ends its preflight database transaction before reading the body, stores each attempt under an immutable key, then reacquires the run lock and rechecks its authoritative state before selecting the artifact.

`UPLOAD_MAX_BYTES`, `UPLOAD_CHUNK_BYTES`, `REDIS_STREAM_RETRIES`, `REDIS_STREAM_MAXLEN`, token lifetimes, and login-throttle counts/windows must be positive. Upload chunks must not exceed the upload limit or 128 MiB. Invalid settings fail application startup instead of silently disabling the relevant control.

Production-style `APP_ENV` values also require secure cookies, explicit `TRUSTED_HOSTS`, and valid `TRUSTED_PROXY_CIDRS`. Interactive API docs are disabled in those environments. See the repository [deployment guide](../docs/deployment.md) before exposing the service.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q -p no:cacheprovider
```
