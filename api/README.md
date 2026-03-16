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

## Local run (without Docker)

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

If you also run the worker outside Docker, point both processes at the same `ARTIFACT_STORAGE_PATH` so uploaded artifacts and ingestion reads use the same shared directory.

Password policy is controlled through environment variables:

- `PASSWORD_MIN_LENGTH`
- `PASSWORD_REQUIRE_LOWERCASE`
- `PASSWORD_REQUIRE_UPPERCASE`
- `PASSWORD_REQUIRE_NUMBER`
- `PASSWORD_REQUIRE_SPECIAL`

If `SEED_ADMIN_PASSWORD` is configured and does not match the active policy, startup fails with a configuration error so the container logs point directly at the invalid env values.
