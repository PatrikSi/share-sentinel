# Operations, scale, and recovery

This guide defines the operating contract for Share Sentinel beyond a successful `docker compose up`. It is deliberately explicit about what the application protects, what an operator must monitor, and where the reference Compose topology stops.

## Reference workload model

The following is the engineering envelope used to choose query limits, connection budgets, ingest bounds, and recovery behavior for this release:

- up to 100 active project scopes
- a largest retained project on the order of 10 million inventory items, with item-history volume measured separately because a high-churn comparison can approach inventory scale
- 100 concurrent paginated UI reads during an investigation burst
- five simultaneous uploads, each independently bounded by `UPLOAD_MAX_BYTES`
- bursty ingest handled by up to four worker replicas against shared artifact storage
- collectors producing streaming NDJSON, with individual records bounded by `INGEST_MAX_RECORD_BYTES`

These are validation targets, not a performance warranty. Storage latency, Postgres sizing, filter selectivity, artifact shape, retention, and deployment topology materially change capacity. A deployment expecting a larger order of magnitude should run the workload test below with representative filters and artifacts before rollout.

The bundled Compose deployment remains a single-host, single-Postgres, single-Redis reference. It is suitable for evaluation and controlled internal use but does not itself provide high availability.

## Service invariants

Operators should be able to rely on these properties:

1. Postgres is authoritative for run state. A Redis delivery failure cannot erase a successfully committed upload.
2. Artifact selection is immutable per committed upload attempt. A stale queue entry cannot cause a superseded object to be ingested.
3. At most one worker processes a run at a time. Worker replicas coordinate through Postgres and may safely rediscover recoverable work.
4. Progress checkpoints are committed in bounded batches. A terminated worker can replay from the last checkpoint; upserts and uniqueness constraints make replay idempotent.
5. Parser or contract failures terminate the affected run with actionable diagnostics. Dependency failures are retried with bounded backoff and jitter rather than busy-looping.
6. API collection routes are bounded and use stable pagination. Malformed cursors, filters, and identifiers return a client error with a request ID instead of becoming an internal error.
7. A readiness failure removes an API instance from service without claiming that the process is dead. The public liveness route intentionally does not perform dependency traffic.
8. Automatic finding resolution requires an authoritative replacement observation. Failed, partial, or identity-incompatible scans retain the earlier finding.
9. Direct grants, observed capabilities, and computed effective access remain separate evidence planes; missing membership or inheritance evidence stays unknown.

Do not deploy API or worker replicas with container-local artifact filesystems. Every replica must see the same durable artifact key at the same path.

## Capacity acceptance targets

Set targets before load testing. A reasonable starting contract for the reference workload is:

- paginated inventory reads: p95 below 1 second and p99 below 3 seconds
- ordinary control-plane writes: p95 below 1 second
- API error rate below 1% excluding deliberate `4xx` responses
- run progress or worker heartbeat freshness below 60 seconds while processing
- no run left `INGESTING` beyond the configured stale threshold without either fresh progress or recovery
- no sustained connection-pool wait, lock timeout, or queue-age growth when arrival rate is within tested capacity
- artifact and database volumes retain at least 20% free space and enough absolute headroom for the largest accepted upload plus ingest/index growth

These are recommended acceptance criteria, not metrics claimed by an untested host. Record the measured host class, Postgres settings, artifact storage, dataset, filters, concurrency, and results alongside every production capacity decision.

## Scaling each stateful path

### API

Scale API containers horizontally behind the gateway. Keep one Uvicorn process per container so health and in-process metrics remain understandable. API database pool size, overflow, wait budget, connection recycle, and database statement/lock budgets are bounded independently; size total possible connections across all replicas below the Postgres connection budget.

Pagination limits protect response memory, but expensive low-selectivity substring filters still consume database CPU. Watch latency by route, database statement duration, timeouts, and connection-pool saturation. Add capacity by improving selectivity/indexes and sizing Postgres before multiplying API pools.

Run diff is a guarded synchronous exception to ordinary collection pagination: exact item-path totals and bounded detail are available only while both runs together remain under `API_RUN_DIFF_MAX_ITEMS` (250000 by default). Larger resource and item comparisons are queued, processed in bounded identity batches, rate/concurrency limited at creation, and read with keyset pagination. Monitor queued/running age, retry timestamps, both comparison result tables, and the selectivity of search/category/provider filters rather than increasing the synchronous ceiling without measurements.

### Worker

Scale worker containers only when Postgres and artifact storage have measured headroom. Workers claim different recoverable runs and use a per-run advisory lock as the final duplicate-execution guard. A bounded identity cache prevents a single huge artifact from consuming memory proportional to every endpoint/share identity.

More workers reduce queue and comparison age only while database writes, index maintenance, and artifact reads are not saturated. Permission entries use bounded set-based inserts and a bounded principal cache, while comparison work persists cursors between batches so recoverable attempts do not replay the full result. Permission reconciliation and comparison materialization still consume Postgres CPU and WAL. Stop adding workers when throughput flattens, database latency rises, lock timeouts grow, or interactive API latency breaches its target.

### Postgres

Postgres holds the normalized inventory and is normally the first shared bottleneck. Use a managed or separately operated HA Postgres service for critical deployments. Monitor:

- active, idle, and waiting connections versus the server limit
- statement latency and timeout counts
- lock waits and deadlocks
- table/index growth, vacuum lag, transaction age, and WAL growth
- cache hit rate and storage latency
- slow query plans for representative inventory filters

Schema migrations run in the one-shot bootstrap service. Back up first, test the migration against production-sized data, and keep the previous application images available. Do not start mixed versions unless the release notes explicitly declare them compatible.

### Redis

Redis accelerates delivery and provides distributed rate-limit state; it is not the only record of already accepted work. Monitor availability, memory, evictions, stream length, pending entries, oldest pending age, and AOF persistence health. If Redis becomes unavailable, runs already committed as `UPLOADED` remain recoverable from Postgres and workers continue database recovery scans. With the secure default `RATE_LIMIT_FAIL_OPEN=false`, new upload requests receive `503` before artifact storage because their distributed admission check cannot run; enabling fail-open accepts that availability-versus-abuse-control tradeoff explicitly.

### Artifact storage

Artifact storage is durable application state, not scratch space. Capacity must cover concurrent uploads, retained artifacts, `.multipart` remnants after process loss, and filesystem overhead. Back it up consistently with Postgres. For multiple hosts, use shared POSIX storage with tested link, rename, fsync, durability, permissions, and failure semantics.

`ARTIFACT_STORAGE_MIN_FREE_BYTES` and `ARTIFACT_STORAGE_MIN_FREE_PERCENT` gate readiness, multipart creation, and each streamed upload part through the shared storage checks. Raw uploads with a valid `Content-Length` also receive a projected-capacity precheck. Part admission and allocation are serialized across API replicas with a bounded POSIX `flock`; the shared filesystem must implement advisory locks consistently. External writers do not participate in that lock, so choose reserves larger than the largest concurrent part plus measured non-application growth. The sysadmin deep-health route performs a bounded create, fsync, no-overwrite hard-link, rename, directory-fsync, and cleanup probe, but routine readiness deliberately avoids mutating storage. The defaults are guardrails, not capacity planning.

## Overload and degraded behavior

Share Sentinel should fail narrowly:

- oversized uploads are rejected before acceptance rather than partially ingested
- capacity exhaustion returns `507` with `Retry-After`; transient artifact I/O or lock contention returns `503` with `Retry-After`, while request cancellation waits for the active part/publication step and performs bounded cleanup before returning
- database pool exhaustion or bounded statement/lock timeouts produce a retryable service error and request ID
- Redis handoff failure leaves the run durably `UPLOADED` for database recovery
- malformed records become bounded ingest issues when safe; a structurally unsafe artifact fails only its run
- retryable worker failures use capped exponential backoff with jitter and persist the next retry time
- poison failures become terminal instead of cycling forever
- a missing, unreadable, permission-unwritable, or below-threshold artifact root makes readiness fail; an upload cannot replace an earlier immutable artifact pointer

Clients should retry only idempotent reads and explicitly retryable responses. Use exponential backoff with jitter, honor `Retry-After` when present, and preserve the returned `X-Request-ID` when escalating a failure.

## Routine diagnostics

Run the read-only doctor from the checkout:

```bash
./scripts/doctor.sh --url http://localhost
```

For a production-style host router reached through a loopback port:

```bash
./scripts/doctor.sh \
  --url http://127.0.0.1:8080 \
  --host sentinel.example.com
```

The doctor continues after failures and reports API liveness and dependency readiness separately, UI reachability, request IDs, Compose validity, every service replica's state/health, database size, Redis stream length, and artifact filesystem usage. Use `--no-compose` for a remote route-only check. It does not mutate application data and does not replace authenticated deep health, metrics, logs, free-space alerts, or backup-restore testing.

At minimum, centralize API and worker logs and retain these fields: timestamp, level, component, request or run ID, operation/status, latency or progress, retry classification, and error type. Alert on sustained readiness failures, worker heartbeat staleness, repeated retries, terminal ingest spikes, database timeouts, queue-age growth, stale enabled sources, repeated partial coverage, expired-risk reopen events, and storage headroom.

The sysadmin Prometheus endpoint includes durable run/comparison counts, oldest active job age, Redis retained/pending/lag state, artifact capacity/headroom, and a per-dependency metric-collection success flag. A missing metric family together with `share_sentinel_operational_metrics_collection_success{component=...} 0` is degraded telemetry, not a zero-value dependency.

### Audit coverage and retention

Source configuration, monitoring retries, comparison creation/retry, finding lifecycle changes, accepted-risk expiry, automatic observation/resolution, and applied artifact cleanup all write audit events. Bulk finding changes retain one event per affected finding plus a shared batch identifier. Detailed finding evidence, occurrences, finding activity, effective-access explanations, and materialized resource/item evidence reads are audited; high-frequency queue/source/status listing is not, so use centralized request logs and metrics for polling telemetry.

Viewer-facing run/finding activity is a deliberately reduced workflow projection. It never returns request IP, user-agent, request correlation fields, or API-token identifiers; the sysadmin/project-admin audit surfaces retain the bounded, recursively redacted source events. Define export, access-review, backup, and deletion periods for audit records. The application supplies no default retention scheduler and an application audit trail is not a substitute for immutable external log archival.

New events retain immutable non-FK user, API-token, and project references plus event-time email/token/project labels. Normal parent rename or deletion after the trigger is installed therefore does not remove historical filtering or exact token attribution. The upgrade backfills legacy events with still-live parents in bounded committed batches, but those rows receive the parent's label at upgrade time—not the unknowable event-time label. Attribution already orphaned before the upgrade cannot be reconstructed and remains null. These snapshots can contain personal or operational identifiers after the source row is deleted, so include them explicitly in privacy and retention procedures. They do not make the Postgres table tamper-evident.

An OS-level filesystem call such as `fsync` cannot be forcibly timed out safely from Python once the kernel has accepted it. Alert on sustained upload latency and API thread-pool pressure, and remove a replica from service at the infrastructure layer when shared storage is wedged; do not rely only on request cancellation to interrupt a blocked kernel operation.

## Artifact reconciliation

Reconciliation compares the shared filesystem with authoritative `scan_runs.artifact_key` references. It reports missing referenced objects, old unreferenced objects, and stale multipart files. The command is dry-run by default and refuses unbounded deletion.

Run a machine-readable dry run against a live stack:

```bash
docker compose run --rm --no-deps api \
  python -m app.maintenance.reconcile_artifacts \
  --min-age-hours 24 --max-delete 1000 --json
```

After backing up and reviewing the exact candidate set, repeat with `--apply`. Each candidate is rechecked immediately before deletion and cleanup actions are written to the audit log. Run small batches, investigate missing referenced objects before deleting anything, and never substitute a blind filesystem age job.

## Recovery playbooks

### API is alive but not ready

1. Run `doctor.sh` and inspect the API readiness response from inside the trusted network.
2. Identify whether Postgres, Redis, or artifact storage is failing; do not restart every dependency at once.
3. Restore the dependency or route traffic to a healthy replica.
4. Confirm readiness, then verify a paginated inventory read and a small synthetic ingest.

### Redis is unavailable

1. Keep Postgres and artifact storage online.
2. Confirm already accepted uploads are still `UPLOADED`; do not repeatedly resubmit them. Expect new uploads to fail with `503` while the default fail-closed rate limiter cannot reach Redis.
3. Restore Redis and verify AOF health and the consumer group.
4. Watch worker recovery logs and run state transitions until queue age returns to normal.

### A run is stuck or repeatedly retrying

1. Inspect its progress, attempt count, last heartbeat, next retry time, and request/run-correlated logs.
2. Check database lock/statement timeouts and artifact readability from a worker replica.
3. If progress is fresh, let the current owner continue. If stale, recovery will claim it after the configured threshold.
4. Treat repeated deterministic parser/contract errors as poison input; preserve the artifact and run diagnostics for analysis rather than forcing infinite retries.

### Disk pressure

1. Stop new collection uploads at the ingress layer if headroom is below the largest allowed request.
2. Back up before deleting anything.
3. Apply the documented retention policy to whole runs/projects; do not manually delete referenced artifact keys.
4. Run the artifact reconciliation dry run, review missing references and candidates, then apply a bounded batch if appropriate.
5. Confirm Postgres vacuum/WAL health and run a synthetic ingest after capacity is restored.

### Worker termination during ingest

Allow the container its configured graceful-stop budget. On `SIGTERM` or `SIGINT`, the worker stops taking new work, commits a checkpoint, returns the run to `UPLOADED`, and records an `INGEST_PAUSED` audit event before exiting. If it is forcibly killed before that checkpoint, the last prior commit remains authoritative and another worker can recover the run after the stale threshold. Verify final persisted counts rather than assuming the producer's terminal summary proves database completion.

## Repeatable capacity validation

Generate a streaming artifact without holding its inventory in memory:

```bash
python3 scripts/generate-capacity-artifact.py \
  --output /tmp/share-sentinel-capacity.ndjson.gz \
  --endpoints 10 \
  --shares-per-endpoint 10 \
  --items-per-share 1000
```

Then exercise the same authenticated upload, ingest, verification, and cleanup path as the release smoke:

```bash
export SHARE_SENTINEL_SMOKE_PASSWORD='<seed admin password>'
export SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS=900
./scripts/smoke-ingest.sh \
  http://localhost \
  admin@example.com \
  /tmp/share-sentinel-capacity.ndjson.gz
unset SHARE_SENTINEL_SMOKE_PASSWORD SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS
```

For a custom artifact, the smoke first runs the standard-library structural validator. Each record is limited to 8 MiB and the decompressed stream to 10 GiB by default, with a 200x gzip expansion-ratio guard; framing, run identity, and terminal counts must agree before any API data is created. The worker still applies the complete record schema during ingestion.

Increase one dimension at a time: retained rows, concurrent readers, concurrent uploads, worker replicas, then mixed traffic. Capture first-page and subsequent-page latency, error/timeout rates, Postgres resource use, pool wait, ingest records/second, queue age, worker memory, artifact throughput, and recovery time after killing one worker. The earliest sustained threshold breach is the deployment's capacity boundary; keep normal peak below it with explicit headroom.

## Known boundaries

The reference release still requires deployment-specific work for:

- HA/failover of gateway, Postgres, Redis, and shared storage
- automated backup scheduling and restore orchestration
- organization SSO, MFA, SCIM, and centralized policy integration
- automated run, finding, audit, and materialized-comparison retention scheduling
- automatic notification delivery and custom finding-policy authoring
- multi-region operation and disaster-recovery replication
- a supported Kubernetes/operator distribution

Treat these as architecture inputs for a critical deployment, not checkboxes that the single-host Compose file silently satisfies.
