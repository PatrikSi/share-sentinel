# Deployment and operations

## Support boundary

The checked-in Compose stack is the supported reference topology for a single-host, initial deployment. It pulls application images from GitHub Container Registry (GHCR), can sit behind a separately managed TLS reverse proxy, and is not a turnkey HA design, Kubernetes manifest, or unattended internet-facing appliance.

`docker-compose.dev.yml` is the separate local-development override. It builds API, worker, UI, and collector images from the checkout while preserving the production service topology. Upstream images and Dockerfile bases use explicit version tags rather than digest pins; deployments that require immutable inputs should resolve and enforce registry digests outside this reference file.

## Prerequisites

- Docker Engine with Compose v2
- enough durable disk for Postgres plus retained raw artifacts
- a TLS-terminating reverse proxy for any non-local deployment
- backups for Postgres and the artifact volume

The API and worker must mount the same durable POSIX artifact volume at the same path. Local or ephemeral per-container filesystems are not interchangeable with shared storage.

## Local deployment

Generate a complete local environment and start source-built services:

```bash
./bootstrap.sh
docker compose up -d --build
docker compose ps
./scripts/smoke-routes.sh http://localhost
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
./scripts/smoke-ingest.sh http://localhost admin@example.com
./scripts/smoke-sharepoint-ingest.sh http://localhost admin@example.com
unset SHARE_SENTINEL_SMOKE_PASSWORD
```

The script creates `.env` with random database, JWT, token-pepper, and administrator secrets, validates the rendered Compose configuration, prints the initial administrator password and exact start command, and sets `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml`. The gateway listens on `127.0.0.1:80`; the `bootstrap` service applies migrations and creates the initial admin before the API and worker start.

To regenerate intentionally, pass `--force`. Do not use `--force` on an environment whose generated secrets are already protecting persistent data.

## Production configuration contract

Do not change `APP_ENV` to `production` until HTTPS is in place. Secure browser cookies will not work over plain HTTP.

At minimum, set:

```dotenv
COMPOSE_FILE=docker-compose.yml
APP_ENV=production
APP_HOST=sentinel.example.com
SHARE_SENTINEL_STACK=production
SHARE_SENTINEL_IMAGE_TAG=v1.1.0
GATEWAY_BIND_ADDRESS=127.0.0.1
GATEWAY_HTTP_PORT=8080
AUTH_COOKIE_SECURE=true
TRUSTED_HOSTS=sentinel.example.com
CORS_ORIGINS=https://sentinel.example.com
TRUSTED_PROXY_CIDRS=172.16.0.0/12,192.0.2.10/32
POSTGRES_PASSWORD=<unique-random-secret>
JWT_SECRET=<at-least-32-random-characters>
TOKEN_PEPPER=<different-at-least-32-character-secret>
SEED_ADMIN_EMAIL=<initial-admin-address>
SEED_ADMIN_PASSWORD=<strong-unique-password>
```

The bootstrap script can create this contract without placing secrets in shell history:

```bash
./bootstrap.sh \
  --production sentinel.example.com \
  --admin-email admin@example.com \
  --image-tag v1.1.0
```

It defaults to `latest` when `--image-tag` is omitted. Use an exact `vX.Y.Z` or `sha-<full-commit>` tag for a repeatable production rollout. If the GHCR packages are private, authenticate the deployment host with a read-only package token before pulling:

```bash
docker login ghcr.io
```

GitHub controls package visibility separately from repository visibility. Maintainers publishing a public deployment should mark all four `share-sentinel-*` packages public in the repository package settings; otherwise every deployment host must authenticate before `docker compose pull`.

`APP_HOST` controls the Traefik host router. `TRUSTED_HOSTS` is the API-level host allowlist. They normally contain the same public hostname, but `TRUSTED_HOSTS` may contain a comma-separated set when the service has intentional aliases.

`SHARE_SENTINEL_STACK` scopes Traefik discovery to this deployment. Give every Share Sentinel Compose project on the same Docker host a distinct value so gateways cannot discover one another's API/UI containers.

`TRUSTED_PROXY_CIDRS` must cover the API's immediate Traefik peer and every trusted proxy hop represented in `X-Forwarded-For`. Do not add client networks or broad public ranges. If the chain is incomplete, audit and rate-limit attribution stops at the first untrusted proxy rather than trusting spoofable headers.

Terminate TLS in a separately managed proxy and forward the public hostname to the loopback gateway port. Configure that proxy to:

- accept only the intended hostname
- redirect HTTP to HTTPS
- preserve the `Host` header
- set or append standard `X-Forwarded-For` and `X-Forwarded-Proto` headers
- enforce request and idle timeouts appropriate for large uploads
- add HSTS after HTTPS is confirmed stable

The bundled gateway reads the Docker socket for service discovery. If that violates the host security policy, replace Traefik discovery with a static upstream configuration; do not make the socket writable.

## Startup and verification

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
./scripts/smoke-routes.sh https://sentinel.example.com
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
./scripts/smoke-production.sh https://sentinel.example.com sentinel.example.com '<the SEED_ADMIN_EMAIL value>'
unset SHARE_SENTINEL_SMOKE_PASSWORD
```

The production smoke verifies the public health route, hidden API docs, UI routing, secure browser cookies, response headers, and exact configured CORS origin. The generic ingest smoke creates and removes its own temporary project; a successful run reaches `COMPLETE`, shows three endpoints and three resources across SMB, NFS, and synthetic SharePoint data, and records the synthetic warning under run Issues. The SharePoint smoke imports two collector-shaped snapshots and verifies persisted assessment context plus stable-ID move, rename, and deletion comparison.

Production-style API startup fails fast when secure cookies, proxy CIDRs, hostnames, secrets, or seed-admin settings are missing. Interactive API docs are disabled outside development/test environments.

API and worker Redis operations use bounded connect and response budgets. `REDIS_CONNECT_TIMEOUT_SECONDS` defaults to `3` and `REDIS_SOCKET_TIMEOUT_SECONDS` defaults to `5`. Keep the socket budget above the worker's three-second blocking stream read. Queue handoff timeouts are treated as ambiguous: the upload remains durably `UPLOADED`, and Postgres-backed recovery can discover it without relying on a second client submission.

Rate-limit counter expiry is attached atomically with Redis `EVAL`. The bundled Redis configuration supports this command. If a managed Redis service uses ACLs, allow `EVAL`; otherwise general request rate limiting follows `RATE_LIMIT_FAIL_OPEN` and login throttling falls back to bounded per-process state rather than distributed enforcement.

API, worker, and worker-health Postgres connection attempts have a five-second default budget. The worker is not startup-gated on Redis and continues its Postgres recovery scan while Redis stream setup is unavailable. API connections default to a 30-second statement and five-second lock budget; worker connections default to a 120-second statement and 15-second lock budget. The `API_DATABASE_*` and `WORKER_DATABASE_*` settings keep these different workloads independently tunable. Size `API_DATABASE_POOL_SIZE + API_DATABASE_MAX_OVERFLOW` across every API replica below the server connection budget.

Inventory CSV exports stream without a fixed row ceiling, but admission is bounded. `API_INVENTORY_EXPORT_MAX_CONCURRENT` defaults to four active exports per API process; multiply it by the replica count when budgeting Postgres queries and outbound bandwidth. Redis permits `API_INVENTORY_EXPORT_RATE_LIMIT` starts (default `12`) per actor/client identity in each `API_INVENTORY_EXPORT_RATE_WINDOW_SECONDS` window (default `60`). Capacity exhaustion returns `503`; rate exhaustion returns `429`; both include `Retry-After`. Redis failure follows the global `RATE_LIMIT_FAIL_OPEN` posture.

Synchronous run comparison is capped by `API_RUN_DIFF_MAX_ITEMS` (default `250000` total items across both runs). Above that envelope the API returns `422` with guidance instead of risking unbounded process memory. Successful comparisons preserve exact totals while bounding each detail array and reporting which sections were truncated. Raise the limit only after measuring API memory and database latency; large recurring comparisons should move to a future asynchronous/materialized workflow.

Compose uses bounded local JSON logs (five 20 MiB files per service), a three-minute worker stop budget, and a one-minute API stop budget. Those budgets leave checkpoint/rollback margin beyond the default database statement timeouts; UI, Postgres, and Redis have separate shorter or service-appropriate budgets. Override the logging block when forwarding to a managed log driver, but retain an explicit disk/retention policy.

Compose passes the database password through libpq's `PGPASSWORD` environment input and keeps it out of the URL, so generated or operator-supplied passwords may contain URL-reserved characters without manual percent encoding. Treat the rendered container environment as secret-bearing operational state.

Raw NDJSON ingestion limits each physical record to `INGEST_MAX_RECORD_BYTES` (default 8 MiB, maximum 16 MiB) before allocation. Compact JSON remains capped at 50 MiB by default and 128 MiB maximum; larger inventories must use streaming NDJSON. `INGEST_GZIP_MAX_BYTES` defaults to 10 GiB decompressed so the worker matches the collector and API's default 10 GiB artifact envelope, while the 200x expansion-ratio guard still rejects compressed bombs. Batch size and identity cache settings also have fail-fast upper bounds; see the worker configuration reference for exact ceilings. Oversized artifacts terminalize the run with a validation error, while unsafe worker configuration prevents startup.

The collector independently caps its uncompressed NDJSON spool at 10 GiB with `--max-artifact-bytes`. Keep that value at or below the reviewed API upload and artifact-storage envelope; setting it to `0` disables the collector-side guard and should be an explicit operator decision. File outputs spool beside their destination before atomic replacement, while container stdout and upload-only collection use the persistent `/data` volume. Budget collector storage for the state database plus the final artifact and its in-progress spool.

Compact `.json` and `.json.gz` are SMB/NFS collector compatibility formats; the SharePoint collector accepts only streaming `.ndjson`, `.jsonl`, or their gzip variants so provider metadata is preserved. The bundled SMB/NFS collector caps compact reconstruction at 8 MiB per endpoint and 40 MiB total, while the worker caps compact JSON materialization at 50 MiB by default. Use NDJSON/JSONL for normal and large collections.

The prebuilt UI writes `/runtime-config.js` when its container starts, using `VITE_API_BASE_URL`, `VITE_CSRF_COOKIE_NAME`, and `VITE_CSRF_HEADER_NAME` from Compose. This keeps one published UI image compatible with deployment-specific API paths and CSRF names. Unsafe values fail container startup instead of being injected into JavaScript; keep the VITE CSRF values identical to the corresponding `AUTH_CSRF_*` API values.

## Published images and tags

The workflows build and publish these images:

- `ghcr.io/patriksi/share-sentinel-api` — API runtime plus bootstrap/migrations
- `ghcr.io/patriksi/share-sentinel-worker` — Redis ingest worker
- `ghcr.io/patriksi/share-sentinel-ui` — static UI served by Nginx
- `ghcr.io/patriksi/share-sentinel-collector` — standalone SMB/NFS and SharePoint Online collector CLIs

Verified `main` commits receive an immutable `sha-<full-commit>` tag. The run that still matches the live branch head also advances `latest`; a superseded run deliberately skips that mutable tag. Verified release tags reuse the commit image set and add the exact `vX.Y.Z`; release jobs do not rebuild the SHA set or rewrite `latest`. Before publication, main runs tests, dependency audits, local image scans, local builds, a full ingest smoke, and a production-style security smoke. It then publishes staging candidates, pulls and revision-checks them, scans the registry artifacts, exercises a clean production stack from those candidates, and only then creates the commit tags. Main publication runs share one ordered lane and recheck the live branch head immediately before moving `latest`, so an older run cannot finish after a newer one and roll the tag backward. Existing commit and version tags are never overwritten: a rerun must resolve to the same runnable image identity and revision or fail, and registry inspection errors fail closed rather than being treated as absence. Release verification pulls, scans, and production-smokes the canonical commit set again before promoting `vX.Y.Z`. Final jobs verify every promoted tag resolves to the tested image identity. Published images include OCI source/revision labels, provenance, and an SBOM attestation.

The four component images live in separate registry packages, so promotion of either `latest` or `vX.Y.Z` updates their tags sequentially rather than atomically. CI verifies all four identities after promotion, but an observer could still pull during that short window. Treat one exact `vX.Y.Z` or `sha-<full-commit>` value across all services as the deployable image set; use `latest` only as a development convenience.

## Persistence and backups

Durable state is split across:

- `pgdata`: authoritative users, projects, runs, inventory, tokens, and audit data
- `artifacts`: raw uploaded artifacts required for ingest and retained run data
- `redisdata`: stream and rate-limit state; Postgres-backed recovery can rediscover some queued runs, but Redis should still be treated as operational state
- `collector_output`: optional collector artifacts and SharePoint delta/snapshot state when the `tools` profile is used

Back up Postgres and the artifact volume as one operational recovery point. A database restore without matching artifacts can leave stored run metadata pointing to missing files. Encrypt backups when scan paths and host data are sensitive, and test restoration before relying on it.

SharePoint delta state is an optimization as well as a checkpoint. Losing it does not corrupt server inventory, but the next collection must perform a bounded full Graph enumeration before it can publish a new complete snapshot. Do not share one state file between concurrent assessment identities or tenants. Opaque imported tokens intentionally use token-derived isolation scopes; token rotation retains the old scope and starts a safe full sync, so use a separate disposable state database and a documented rotation/archive policy for that mode.

Before widening a SharePoint rollout, capture per-run item/artifact growth, Graph retry and reset counts, partial-library failures, collector volume headroom, worker ingest age, API/database latency, and Postgres storage growth. A few oversized libraries can dominate the workload; test tenant skew rather than relying only on average library size. Graph throttling should reduce throughput through bounded retries, safety limits should stop collection with an explicit partial result, and the worker queue should absorb only a measured bounded ingest burst—not an unlimited schedule backlog.

Each upload attempt is stored under a new immutable artifact key before Postgres selects it for the run. A process or database failure before that pointer commit can leave an unreferenced file, but it cannot overwrite the previously committed artifact. Run and project deletion, plus cleanup of superseded uploads, remove files on a best-effort basis after the database change. Explicit request cancellation cleans up its in-progress upload, but a process kill or power loss can leave stale files under `.multipart`; failed post-commit deletions can also leave immutable-object orphans. This release does not include an automatic reconciler or retention job. Include periodic artifact-volume review of both referenced objects and `.multipart` files in the operator runbook.

## Upgrades

1. Read `CHANGELOG.md` for migrations, environment changes, and caveats.
2. Back up Postgres and artifacts.
3. Pull or check out the intended tag.
4. Set `SHARE_SENTINEL_IMAGE_TAG` to the intended exact release tag.
5. Run `docker compose config --quiet` and `docker compose pull`.
6. Run `docker compose up -d` and watch the one-shot `bootstrap` service.
7. Confirm all long-running services are healthy, run the route smoke test, and verify a synthetic ingest.

Alembic migrations are applied forward by bootstrap. Automated downgrade or zero-downtime multi-version compatibility is not promised in the current release line.

## Capacity and scaling limits

- One worker process handles one job at a time. Multiple replicas use database claims and per-run advisory locks, but should be added only with shared artifact storage and measured database/Redis headroom.
- Run-diff detail arrays are bounded and disclose truncation; comparisons above the synchronous item envelope are rejected. Large comparison workloads still require an asynchronous/materialized design.
- The default 10 GiB ceiling is an application-level streaming limit. First-party clients use raw request bodies so the API can enforce it incrementally; multipart remains a compatibility path and may be pre-spooled by the HTTP framework. Configure every external proxy with an intentional body-size limit and enough temporary storage rather than assuming the UI Nginx setting protects the direct Traefik-to-API route.
- The Compose deployment has single Postgres, Redis, and gateway instances and therefore no HA guarantee.
- API metrics do not include a full worker backlog or per-stage ingest telemetry surface.
- Inventory collections use stable keyset order but do not yet expose arbitrary server-side column sorting; the UI intentionally avoids page-local sorting that would misrepresent the full result set.

Use [Operations, scale, and recovery](./operations.md) for the reference workload, capacity smoke, diagnostics, alerts, and failure playbooks. Capacity testing and a deployment-specific recovery plan remain required before treating the current release as a critical production service.
