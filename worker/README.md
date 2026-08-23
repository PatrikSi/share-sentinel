# Ingestion worker

The worker consumes ingest jobs, reads raw artifacts from shared storage, and normalizes them into Postgres-backed inventory.

## Responsibilities

- consume `ingest_jobs` from Redis Streams
- reopen raw artifacts from `ARTIFACT_STORAGE_PATH`
- upsert endpoints, resources, items, and ingest errors
- checkpoint `scan_runs.ingest_progress`
- emit ingest audit events
- recover some stranded `UPLOADED` or stale `INGESTING` runs

## Hard deployment invariant

The worker must see the same shared artifact storage as the API, at the same filesystem path.

That is not just a convenience setting. The API writes uploads to local filesystem-backed storage and the worker later reopens those files directly. A multi-node or ephemeral-disk deployment will fail unless both services share the same durable POSIX volume mounted at the same `ARTIFACT_STORAGE_PATH`.

## Behavior

- Status flow is typically `PENDING_UPLOAD -> UPLOADED -> INGESTING -> COMPLETE` or `FAILED`.
- Ingest progress is checkpointed so interrupted work can resume from a saved line offset.
- Redis queue handoff is preferred, but the worker also scans Postgres for recoverable runs as a fallback path. If Redis is unavailable at startup, stream setup is retried without blocking that database recovery scan.
- Postgres is authoritative for the current project and artifact key, so delayed or duplicate Redis messages cannot ingest a superseded upload.
- Upload attempts use immutable keys; committing a new database pointer precedes best-effort cleanup of the superseded file.
- Gzip input is protected by decompression limits before parsing.
- NDJSON reads are capped per record so a newline-free artifact cannot force an unbounded allocation.
- Compact JSON is a compatibility format capped at 50 MiB decompressed; use NDJSON for large inventories so records remain independently bounded.
- Invalid UTF-8 NDJSON records become explicit ingest errors and are never persisted with replacement-corrupted names or paths.
- Resumed ingestion hydrates existing endpoint/resource identities before processing later item records, preserving checkpointed access metadata.
- Final run counts are derived from normalized database rows rather than trusting producer-declared summary totals.
- Optional item size and modification timestamps are normalized and retained when collectors provide them.
- SMB signing uses the canonical `smb.signing` string (`required` or `not_required`); legacy boolean `smb.signing_required` artifacts remain accepted.
- Unexpected poison-record failures are terminalized with a redacted operator-facing error instead of being replayed forever.
- Duplicate stream messages honor a future `next_retry_at`; they are acknowledged while the database recovery scan owns the due retry.

## Important caveats

### Upload accepted does not always mean worker started

The API can accept an artifact, mark the run `UPLOADED`, and return `queued: false` when Redis queue handoff falls back. In that case the worker will discover the run during its periodic recovery scan instead of through the primary stream path.

Operators should watch run status or activity rather than assuming that a successful upload response means the worker has already started ingesting.

### Artifact acceptance is broader than ingest success

The API can accept large uploads, but the worker still enforces parser and decompression limits later. Large gzip or non-streamable JSON artifacts can therefore be accepted at upload time and still fail during ingest.

### Serial worker model

One worker process handles jobs serially. If you need more throughput, scale by adding worker replicas and verify your shared artifact storage can support the concurrent read pattern.

### Current operability limits

- retryable ingest failures back off and return runs to `UPLOADED` until the retry budget is exhausted
- the default Docker stack uses a heartbeat-file healthcheck rather than a dedicated worker HTTP endpoint
- the default stack exposes only API HTTP metrics, not worker-specific queue or backlog metrics

## Local configuration

At minimum, the worker needs:

- `DATABASE_URL`
- `REDIS_URL`
- `ARTIFACT_STORAGE_PATH`
- `WORKER_HEARTBEAT_PATH`
- `WORKER_HEALTH_TIMEOUT_SECONDS`
- `REDIS_CONNECT_TIMEOUT_SECONDS` (default `3`)
- `REDIS_SOCKET_TIMEOUT_SECONDS` (default `5`; keep this above the worker's 3-second blocking stream read)
- `DATABASE_CONNECT_TIMEOUT_SECONDS` (default `5`)
- `INGEST_MAX_RECORD_BYTES` (default `8388608`)
- `INGEST_JSON_COMPAT_MAX_BYTES` (default `52428800`; compact JSON only)

In Docker, those are already wired in `docker-compose.yml`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q -p no:cacheprovider
```
