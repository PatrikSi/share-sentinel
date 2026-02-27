# Ingestion Worker

Consumes `ingest_jobs` from Redis Streams and ingests NDJSON artifacts from S3/MinIO into Postgres.

## Behavior

- Idempotent upserts for endpoints/resources/items
- Progress tracking in `scan_runs.ingest_progress`
- Status transitions: `INGESTING` -> `COMPLETE` / `FAILED`
- Writes audit events (`INGEST_STARTED`, `INGEST_COMPLETED`, `INGEST_FAILED`)
