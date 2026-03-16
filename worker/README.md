# Ingestion Worker

Consumes `ingest_jobs` from Redis Streams and ingests NDJSON artifacts from a shared filesystem into Postgres.

## Behavior

- Idempotent upserts for endpoints/resources/items
- Batched inserts for large item/error volumes
- Progress tracking in `scan_runs.ingest_progress` with resume support
- Status transitions: `INGESTING` -> `COMPLETE` / `FAILED`
- Writes audit events (`INGEST_STARTED`, `INGEST_COMPLETED`, `INGEST_FAILED`)
- Recovers stale Redis stream messages and scans `UPLOADED` runs as fallback

Set `ARTIFACT_STORAGE_PATH` to the same directory used by the API service so the worker can open uploaded artifacts directly.
