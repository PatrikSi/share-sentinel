# Changelog

All notable user-visible changes should be recorded in this file.

The project follows a simple release-first workflow:

- stage changes under `## Unreleased` while they are on `main`
- promote the relevant entries into a versioned section when cutting a tag
- keep migrations, env var changes, and operator-facing caveats explicit

## Unreleased

### Added

- Metadata-only SharePoint Online collection through Microsoft Graph, with unattended application inventory and delegated user quick-check perspectives, bounded paging/retries, and optional direct upload.
- Durable per-library delta checkpoints and local SQLite metadata snapshots that materialize a complete artifact on every successful run while preserving stable site, library, and drive-item identities across moves and renames.
- First-class SharePoint provider metadata, collection context, resource types, exposure evidence, inventory filters, and run-explorer presentation. Delegated `USER_VISIBLE` evidence is explicitly distinct from public, external, or anonymous exposure.
- Collector-shaped full/delta SharePoint fixtures, an end-to-end stable-ID diff smoke test, and Windows unit/contract coverage for SharePoint collector authentication paths.

### Operator notes

- Migrations `0010` and `0011` add SharePoint/provider inventory fields and resumable concurrent identity indexes. Apply migrations before starting the updated API and worker.
- SharePoint artifacts and collector state contain sensitive tenant metadata even though document contents and credentials are excluded. Protect and retain the collector state if efficient incremental collection is required.
- Opaque-token rotation intentionally creates isolated retained state scopes. Use a dedicated/disposable state database and periodic rotation or archival for imported opaque tokens; size collector storage for state, the final artifact, and its temporary spool.

## [1.0.0] - 2026-08-24

### Added

- GHCR publication for API, worker, UI, and collector images after full CI or release verification, including exact commit/release tags, vulnerability scans, provenance, and SBOM attestations.
- A containerized collector with NFS tooling and an unprivileged runtime.
- `docker-compose.dev.yml` for local source builds and a root-level `bootstrap.sh` that generates and validates development or production environment files.
- A compact enterprise inventory workspace with URL-backed filters, saved views, configurable columns and density, value include/exclude actions, keyboard shortcuts, clearer partial-ingest state, and optional file size/modified-time columns.
- Collector progress, verbosity and quiet modes, truthful processed/remaining counters, bounded upload retries, interruption-safe partial artifacts, and streaming schema-v1 NDJSON output for large scans.
- Bounded, non-mutating SMB access probes for tree connection, directory listing, file reads, file and directory creation rights, existing-file modification, deletion, ACL changes, and ownership changes, with explicit allowed, denied, mixed, inconclusive, and not-tested evidence.
- Optional SMB allocation size, creation time, last-access time, metadata-change time, and file-attribute inventory fields.
- Read-only deployment diagnostics plus streaming capacity-artifact generation and validation for repeatable scale tests.
- Bounded run-diff detail with exact summary totals and explicit truncation metadata, plus cursor pagination for endpoint shares.
- Explicit API database-pool/error metrics and a public dependency-readiness probe for load balancers and deployment diagnostics.

### Changed

- API database pools, connection waits, statements, and locks now have component-specific bounds; large run diffs fail safely and successful detail arrays disclose truncation while retaining exact totals.
- Inventory, run, issue, audit, and endpoint-share pagination now use matching composite indexes; malformed cursors, filters, and validation failures return structured request-correlated errors.
- UI collection loading is bounded by page, item, and time budgets; recent-run selectors preserve explicit older IDs, disclose partial catalogs, and cap explicit scopes, while detail routes cancel and ignore stale entity requests.
- Long-running Compose services use bounded local log rotation and explicit graceful-stop budgets.
- The base Compose stack pulls the canonical `ghcr.io/patriksi/share-sentinel-*` images directly; local development selects the source-build override through generated `COMPOSE_FILE` configuration.
- Container inputs now use readable exact version tags rather than digest pins.
- Upgraded React Router, PostCSS, and the transitive NanoID dependency to clear the current UI dependency audit without bundling unrelated framework major upgrades.
- Updated GitHub Actions runtimes to their Node 24-compatible releases so CI no longer relies on GitHub's deprecated Node 20 action shim.
- Main CI now pull-checks, vulnerability-scans, and production-smokes registry staging images before creating immutable `sha-<commit>` tags; ordered, branch-fresh promotion updates mutable `latest`. Releases reuse and re-verify the commit set, exact release tags refuse reassignment, registry inspection fails closed, and every promoted tag is checked against the tested image identity.
- Release tags now promote only their exact `vX.Y.Z` image set, preventing a tag workflow from racing main-branch ownership of the non-atomic `latest` convenience tags.
- Fixed SMB authentication identity handling for `DOMAIN\user`, `DOMAIN/user`, UPN, explicit-domain, and local-account forms; conflicting modes now fail early and Kerberos correctly receives configured NTLM hashes.
- Python application images now install available Debian security updates during builds so rebuilt releases do not retain fixed vulnerabilities from an older base-image snapshot.
- Redis calls now have bounded connect/read budgets, rate-limit increments attach expirations atomically, and long-running Compose services restart after unexpected exits.
- Ingest now treats Postgres as authoritative when stale queue messages reference replaced uploads, terminalizes unexpected poison failures, and retains collected file size and modification time metadata.
- Artifact upload now streams first-party raw bodies without holding a database transaction, runs bounded database phases off the async event loop, uses immutable per-attempt keys, rechecks run state before commit, finishes durable cleanup under cancellation, and preserves Postgres recovery when Redis handoff is unavailable.
- Worker retries now expose scheduled activity and jittered bounded backoff, resume without downgrading existing share access, reject oversized or invalidly encoded records, bound identity caches, claim recovery work safely across replicas, checkpoint graceful shutdowns, and derive final counts from persisted inventory.
- Database passwords are passed through libpq rather than embedded in URLs, so URL-reserved characters render safely in Compose.
- The UI runtime now runs as UID/GID `10001:10001`; Python linting is enforced in both main-branch and tag-release workflows.
- Share access now starts as unknown and upgrades monotonically from observed evidence; inventory and run-explorer views expose compact capability summaries and expandable probe evidence, while preserving the legacy access summary for compatible filters and artifacts.
- SMB `mtime` now uses the server's last-write timestamp instead of Impacket's metadata-change timestamp.

### Operator notes

- Migration `0008` adds the `unknown` access state, per-resource capability evidence, and optional item metadata columns. The bootstrap service applies it before the API and worker start.
- Migration `0009` creates large-table pagination indexes concurrently and can be resumed after an interrupted index build. Bootstrap uses separate migration connect/lock budgets; test this migration against production-sized data before rollout.
- New `API_DATABASE_*`, `MIGRATION_DATABASE_*`, `WORKER_DATABASE_*`, `API_RUN_DIFF_MAX_ITEMS`, `INGEST_JSON_COMPAT_MAX_BYTES`, `INGEST_GZIP_MAX_BYTES`, `INGEST_RETRY_JITTER_RATIO`, `INGEST_MAX_RETRIES`, and `INGEST_IDENTITY_CACHE_SIZE` settings are documented in `.env.example` and the deployment guide. Invalid or unsafe worker limits now fail startup.
- Access probes use `FILE_OPEN` on existing SMB objects and never create, modify, delete, take ownership of, or rewrite ACLs. They can still generate ordinary SMB and authorization audit telemetry and may update server-side last-access accounting.

## [0.2.0] - 2026-07-16

Initial open source publication candidate.

### Added

- Self-hosted FastAPI, Postgres, Redis Streams, React, and worker stack with Alembic bootstrap migrations.
- SMB and NFS collector with compact JSON/gzip output, optional direct upload, bounded traversal, and synthetic sample data.
- Project-scoped inventory, run explorer, diffs, issue review, saved investigations, audit history, and RBAC.
- Sysadmin administration for users, projects, memberships, API tokens, security posture, and audit export.
- CI for all Python suites, UI typechecking/builds, dependency audits, Compose validation, container builds, live ingest, and production-style security smoke tests.
- Tag-driven source release archives with checksums, plus automated dependency update configuration.

### Changed

- Upgraded vulnerable Python and JavaScript dependencies and moved JWT handling to PyJWT with required claims.
- Pinned container base images by digest and run API/worker images as an unprivileged user.
- Added explicit production host/proxy/CORS validation and secure response headers.
- Enforced exact-name project deletion confirmation at the API boundary and validated bootstrap admin email addresses before startup.
- Isolated Traefik discovery by deployment label so multiple Compose stacks can safely share one Docker host.

### Operator notes

- Production-style environments now require `TRUSTED_HOSTS`, `TRUSTED_PROXY_CIDRS`, secure auth cookies, non-placeholder secrets, and valid initial admin credentials.
- Give each Compose deployment a distinct `SHARE_SENTINEL_STACK` value when multiple stacks share a Docker host.
- Existing databases are upgraded through migrations `0001` through `0007`; the bootstrap service applies them before application startup.
- The bundled Compose topology remains local-first and requires an external TLS deployment design before network exposure.
