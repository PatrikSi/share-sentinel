# Ingestion worker

The worker consumes ingest jobs, reads raw artifacts from shared storage, and normalizes them into Postgres-backed inventory.

## Responsibilities

- consume `ingest_jobs` from Redis Streams
- reopen raw artifacts from `ARTIFACT_STORAGE_PATH`
- upsert endpoints, resources, items, normalized permission evidence, and ingest errors
- materialize durable resource comparisons between complete runs
- register recurring collection sources, choose compatible automatic baselines, and evaluate built-in finding policies
- materialize durable item additions, removals, moves, metadata changes, permission-evidence changes, and indeterminate correlations
- checkpoint `scan_runs.ingest_progress`
- emit ingest, source, comparison, finding, and accepted-risk-expiry audit events
- recover some stranded `UPLOADED` or stale `INGESTING` runs

## Hard deployment invariant

The worker must see the same shared artifact storage as the API, at the same filesystem path.

That is not just a convenience setting. The API writes uploads to local filesystem-backed storage and the worker later reopens those files directly. A multi-node or ephemeral-disk deployment will fail unless both services share the same durable POSIX volume mounted at the same `ARTIFACT_STORAGE_PATH`.

## Behavior

- Status flow is typically `PENDING_UPLOAD -> UPLOADED -> INGESTING -> COMPLETE` or `FAILED`.
- Ingest progress is checkpointed so interrupted work can resume from a saved line offset.
- Redis queue handoff is preferred, but the worker also scans Postgres for recoverable runs as a fallback path. If Redis is unavailable at startup, stream setup is retried without blocking that database recovery scan.
- Recovery candidates are claimed one at a time with `FOR UPDATE SKIP LOCKED`, so multiple replicas can make progress without a serial worker pre-claiming an entire batch.
- Postgres is authoritative for the current project and artifact key, so delayed or duplicate Redis messages cannot ingest a superseded upload.
- Upload attempts use immutable keys; committing a new database pointer precedes best-effort cleanup of the superseded file.
- The framing and normalization passes independently stream-check the stored artifact against the size and SHA-256 accepted by the API, and the worker reopens it for one final bounded check immediately before `COMPLETE`. Missing provenance or changed bytes terminalize the run, discard all checkpointed inventory and permission evidence, and require a fresh upload; a resumed run can never combine old rows with a changed artifact and publish `COMPLETE`.
- Before publishing `COMPLETE`, the worker reconciles valid persisted endpoint, resource, and item counts with the terminal producer counts. Rejected structural records fail closed for both inventory and content comparisons; rejected item records downgrade content only; undecodable or unclassified records downgrade both. The consumer-owned result is stored in `collection_context.metadata.inventory_ingest` and can only narrow producer completeness claims.
- Gzip input is protected by decompression limits before parsing.
- NDJSON reads are capped per record so a newline-free artifact cannot force an unbounded allocation.
- Compact JSON is a compatibility format capped at 50 MiB decompressed; use NDJSON for large inventories so records remain independently bounded.
- Invalid UTF-8 NDJSON records become explicit ingest errors and are never persisted with replacement-corrupted names or paths.
- Resumed ingestion hydrates existing endpoint/resource identities before processing later item records, preserving checkpointed access metadata.
- Final run counts are derived from normalized database rows rather than trusting producer-declared summary totals.
- Optional item size and modification timestamps are normalized and retained when collectors provide them.
- SharePoint sites, document libraries, and drive items retain bounded provider metadata and stable provider IDs separately from mutable names and paths. This lets resource renames and item moves update one normalized identity rather than creating false duplicates.
- SharePoint collection context records the authentication perspective, tenant, assessed identity, discovery completeness, and materialized snapshot semantics. Secret-bearing keys, opaque delta links, and unsafe provider URLs are rejected at the ingest boundary.
- Deleted provider records are retained as tombstones only when an artifact explicitly supplies them; current-inventory queries exclude tombstones by default.
- SMB signing uses the canonical `smb.signing` string (`required` or `not_required`); legacy boolean `smb.signing_required` artifacts remain accepted.
- Unexpected poison-record failures are terminalized with a redacted operator-facing error instead of being replayed forever.
- Duplicate stream messages honor a future `next_retry_at`; they are acknowledged while the database recovery scan owns the due retry.
- Retry backoff includes deterministic per-run jitter to avoid synchronized retries after a shared dependency recovers.
- Endpoint and resource identity caches are bounded LRU maps rather than inventory-sized dictionaries.
- Permission entries use bounded set-based inserts and a bounded principal cache; malformed key collisions fall back to isolated row validation so unrelated evidence can still ingest.
- Comparison retries persist a due timestamp and use delayed deterministic jitter rather than consuming their retry budget in a tight loop.
- Comparison phase and keyset cursors are durable. Recovery preserves committed resource/item batches, while only an explicit failed-comparison retry resets result state.
- Long comparisons yield after the configured time or batch quantum and return to the Postgres recovery queue so one job cannot occupy a serial worker indefinitely.
- Disabled sources still record successful/failed observations but skip automatic comparison and policy evaluation; incomplete scans never auto-resolve state findings.
- Accepted-risk findings are reopened by a bounded expiry sweep and every automatic lifecycle transition is audited.
- `SIGTERM` and `SIGINT` stop new claims and cooperatively checkpoint active work. A paused run returns to `UPLOADED`, records `INGEST_PAUSED`, and can be resumed by another worker.

## Important caveats

### Upload accepted does not always mean worker started

The API can accept an artifact, mark the run `UPLOADED`, and return `queued: false` when Redis queue handoff falls back. In that case the worker will discover the run during its periodic recovery scan instead of through the primary stream path.

Operators should watch run status or activity rather than assuming that a successful upload response means the worker has already started ingesting.

### Artifact acceptance is broader than ingest success

The API can accept large uploads, but the worker still enforces parser and decompression limits later. Large gzip or non-streamable JSON artifacts can therefore be accepted at upload time and still fail during ingest.

### Serial worker model

One worker process handles jobs serially. Comparison time slicing improves fairness but does not create parallelism inside a process. If you need more throughput, scale by adding worker replicas and verify that Postgres, Redis, and shared artifact storage can support the concurrent read/write pattern.

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
- `WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS` (default `5`)
- `WORKER_DATABASE_STATEMENT_TIMEOUT_MS` (default `120000`)
- `WORKER_DATABASE_LOCK_TIMEOUT_MS` (default `15000`)
- `INGEST_BATCH_SIZE` (default `5000`, maximum `10000`)
- `INGEST_MAX_RECORD_BYTES` (default `8388608`, maximum `16777216`)
- `INGEST_JSON_COMPAT_MAX_BYTES` (default `52428800`, maximum `134217728`; compact JSON only)
- `INGEST_GZIP_MAX_BYTES` (default `10737418240`, maximum `107374182400`; decompressed bytes)
- `INGEST_GZIP_MAX_EXPANSION_RATIO` (default `200`, maximum `1000`)
- `INGEST_RETRY_JITTER_RATIO` (default `0.2`; range `0` through `1`)
- `INGEST_MAX_RETRIES` (default `4`, maximum `100`)
- `INGEST_IDENTITY_CACHE_SIZE` (default `10000`, maximum `100000` entries per identity map)
- `INGEST_PERMISSION_ENTRY_BATCH_SIZE` (default `500`, maximum `5000` normalized permission entries per set-based insert)
- `INGEST_PERMISSION_ENTRY_BATCH_MAX_BYTES` (default `8388608`, maximum `67108864`; flushes entry batches by serialized size as well as count)
- `COMPARISON_WORK_QUANTUM_SECONDS` (default `30`; range `5` through `300`)
- `COMPARISON_WORK_QUANTUM_BATCHES` (default `20`; range `1` through `1000`)
- `AUTOMATIC_COMPARISON_MAX_ACTIVE_PER_PROJECT` (default `3`; range `1` through `100`)
- `FINDING_EVALUATION_BATCH_SIZE` (default `500`; range `50` through `5000`)
- `FINDING_RESOLUTION_BATCH_SIZE` (default `250`; range `25` through `1000`)

The Compose file exposes the routinely tuned settings above and uses worker defaults for the others. Invalid values and values above the documented memory-safety ceilings stop worker startup with a configuration error instead of being silently clamped.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q -p no:cacheprovider
```
