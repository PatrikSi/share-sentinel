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
- Redis queue handoff is preferred, but the worker also scans Postgres for recoverable runs as a fallback path.
- Gzip input is protected by decompression limits before parsing.

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

In Docker, those are already wired in `docker-compose.yml`.
