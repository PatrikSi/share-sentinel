# API Service

FastAPI service for auth, project management, run lifecycle, artifact upload, and query endpoints.

## Key properties

- JWT access + refresh tokens
- Hashed API tokens (project scoped)
- Project-scoped RBAC (admin/operator/viewer)
- Request IDs and request logging
- Redis-backed fixed-window rate limits (auth + upload)
- Async ingestion queueing via Redis Streams

## Local run (without Docker)

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```
