# share-sentinel

Share Sentinel is a v1 SMB artifact ingestion platform with:

- Python collector that emits NDJSON / NDJSON.GZ
- FastAPI control plane with JWT + API token auth and project-scoped RBAC
- Async ingestion workers reading jobs from Redis Streams
- Postgres normalized schema + MinIO raw artifact storage
- Next.js UI for runs/endpoints/resources/items exploration

## Quick start

1. Copy environment values:

```bash
cp .env.example .env
```

2. Build and start services:

```bash
docker compose up --build
```

3. Open UI and API:

- `http://ui.localhost`
- `http://api.localhost/docs`

4. Default seeded admin credentials come from `.env`:

- `SEED_ADMIN_EMAIL`
- `SEED_ADMIN_PASSWORD`

## Repo layout

- `api/`: FastAPI service + Alembic migrations
- `worker/`: Redis Streams ingestion worker
- `collector/`: one-off SMB collector CLI
- `ui/`: Next.js app for operations

## Notes

- v1 ingestion is async and idempotent via unique DB constraints.
- API attaches `X-Request-ID` to every response.
- Upload and auth endpoints have Redis-backed fixed-window rate limiting.
