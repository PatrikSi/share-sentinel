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
- Deep health endpoint for database and Redis readiness (`/healthz/deep`)
- Async ingestion queueing via Redis Streams
- Trusted-host enforcement and explicit production configuration validation

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

Production-style `APP_ENV` values also require secure cookies, explicit `TRUSTED_HOSTS`, and valid `TRUSTED_PROXY_CIDRS`. Interactive API docs are disabled in those environments. See the repository [deployment guide](../docs/deployment.md) before exposing the service.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q -p no:cacheprovider
```
