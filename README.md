# share-sentinel

Share Sentinel is a v1 SMB artifact ingestion platform with:

- Python collector that emits NDJSON / NDJSON.GZ
- FastAPI control plane with JWT + API token auth and project-scoped RBAC
- Enterprise auth controls: account approval workflow, optional self-registration, API token scopes/expiry, login lockout
- Async ingestion workers reading jobs from Redis Streams
- Postgres normalized schema + MinIO raw artifact storage
- React (Vite) UI for runs/endpoints/resources/items exploration

## Quick start

1. Copy environment values:

```bash
cp .env.example .env
```

2. Build and start services:

```bash
docker compose up --build
```

If you want a fast post-start verification (routing + API wiring):

```bash
./scripts/smoke-routes.sh
```

3. Open UI and API:

- `http://localhost`
- `http://localhost/api/docs`

4. Default seeded admin credentials come from `.env`:

- `SEED_ADMIN_EMAIL`
- `SEED_ADMIN_PASSWORD`

## Repo layout

- `api/`: FastAPI service + Alembic migrations
- `worker/`: Redis Streams ingestion worker
- `collector/`: one-off SMB collector CLI
- `ui/`: React SPA for operations

## Notes

- v1 ingestion is async and idempotent via unique DB constraints.
- Worker performs resume-aware ingestion (`line_offset`) and reconciliation for runs left in `UPLOADED`.
- API attaches `X-Request-ID` to every response.
- Upload and auth endpoints have Redis-backed fixed-window rate limiting.
- `/healthz/deep` provides dependency readiness checks for Postgres and Redis.
