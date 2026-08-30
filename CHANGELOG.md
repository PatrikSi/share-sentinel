# Changelog

All notable user-visible changes should be recorded in this file.

The project follows a simple release-first workflow:

- stage changes under `## Unreleased` while they are on `main`
- promote the relevant entries into a versioned section when cutting a tag
- keep migrations, env var changes, and operator-facing caveats explicit

## Unreleased

### Added

- Project monitoring sources now connect recurring runs into a durable timeline with source health, expected collection cadence, compatible automatic baselines, and an explicit enable/disable control.
- A project findings queue turns comparison and permission evidence into deduplicated, severity-ranked work. Findings support assignment, status/revision safeguards, accepted-risk expiry, evidence snapshots, occurrence history, activity, and bounded bulk updates.
- Materialized comparisons now retain keyset-paginated item additions, removals, moves, and permission-evidence changes, with explicit indeterminate rows when identity or collection coverage cannot support a definitive conclusion.
- Effective-access views separate provider-reported grants, the collector's observed capabilities for its assessed identity, and conservative computed conclusions. Incomplete group, inheritance, or collection evidence remains unknown rather than being inferred.
- SharePoint collection supports Microsoft national-cloud endpoint profiles, certificate credentials, bounded governance metadata, and safer state partitioning/backfill. SMB collection reports bounded DFS detection, and NFS discovery distinguishes RPC/export evidence from unassessed authentication.
- Operational metrics now expose source/finding/comparison age and backlog signals, Redis delivery state, per-component collection outcomes, and artifact-volume capacity. A bounded artifact reconciliation command audits stale multipart files, missing references, and safe orphan cleanup.
- New audit history retains immutable project, user, and exact API-token references with event-time labels across supported rename and deletion workflows. Legacy rows whose parents still exist receive the parent's label at upgrade time in bounded restart-safe batches; a historical label changed or deleted before upgrade cannot be recovered.
- Audit exports stream bounded CSV or JSON from an ID high-watermark, expose row-limit metadata, and warn administrators after a downloaded file was capped.

### Fixed

- SMB timestamps now distinguish last-write from metadata-change time, and incomplete network discovery can no longer claim complete permission coverage.
- NFS reachability no longer appears as authenticated access; DFS observations no longer imply referral enumeration or credential forwarding.
- Finding auto-resolution requires authoritative structural or capability coverage, accepted-risk expiry is reopened by a bounded worker sweep, and disabled sources continue recording observations without silently running automation.
- Artifact publication is immutable and atomic, storage checks fail closed at configured headroom with explicit retryable errors, cancelled requests cannot race an in-flight part/publication, and deep health verifies the write/fsync/rename contract used by API and worker.
- Finding and run workflow activity now withholds request/network and token audit context from ordinary viewers; finding queue polling exposes only evidence state and the full evidence read remains separately audited.
- Monitoring mutations use revision checks and bounded, redacted audit metadata; bulk workflows retain per-finding accountability.
- Source configuration uses explicit value preconditions for name, enabled state, and cadence, so cadence-only edits and nullable cadence clears remain safe while background health updates continue.
- Monitoring recovery now commits terminal run state, current-source coverage, and audit evidence atomically; superseded runs cannot republish stale coverage, and missing derived-evaluation state remains degraded rather than failing open to healthy.
- SharePoint count guards no longer overload `0` as an implicit unlimited value. Existing automation using `--max-sites 0`, `--max-libraries 0`, or `--max-items 0` must migrate to the corresponding explicit `--unlimited-*` switch.
- Audit export batching now records distinct completed, failed, and interrupted terminal outcomes; CI and tag releases exercise the attribution DDL, triggers, rename/delete retention, and restart-safe backfill against PostgreSQL 16.

### Operator notes

- Back up Postgres and apply migrations `0014` through `0018` before starting the updated API and worker. `0014` adds monitoring, findings, and item-history state using online-safe foreign-key staging; `0015` validates those constraints and builds large-table indexes concurrently. `0016` adds nullable audit snapshots and the new-event trigger, `0017` builds audit lookup indexes concurrently, and restart-safe `0018` installs idempotent parent triggers before backfilling live-parent legacy rows in committed batches of at most 5,000. Pause user, token, and project rename/deletion during the upgrade; a legacy label already renamed or orphaned is not historically reconstructable.
- Existing narrowly scoped API tokens do not gain the new `read:findings`, `write:findings`, `read:sources`, or `write:sources` scopes automatically. Add only the scopes the integration needs; newly created role-default tokens include the appropriate monitoring scopes.
- Findings and effective-access output are evidence, not a directory-service entitlement simulation. Group expansion and inherited-permission resolution remain unknown unless a future collector supplies complete, ordered, and provenance-backed membership/inheritance snapshots.
- Recurring comparisons are time-sliced and recovered from Postgres. Size worker replicas, database capacity, retention, and collection cadence from representative tenant skew; a schedule must not create an unbounded backlog.
- Raw artifacts, item history, permission evidence, findings, and audit records can contain sensitive infrastructure and identity metadata. Define deployment-specific access, backup, retention, and deletion policies.
- Materialized item history uses comparison algorithm `resource-evidence-v3`. Existing v2 rows are retained for audit history but are potentially incomplete and non-authoritative; the API and UI mark them as legacy. Queued or running v2 work and legacy finding-evaluation retries fail with recreate guidance instead of being mislabeled. Cached automatic-baseline coverage now includes the algorithm version and fails closed in source health, so important baseline/current pairs must be recreated after upgrade to materialize the corrected v3 result.

## [1.3.0] - 2026-08-27

### Added

- SMB and SharePoint collection now emits normalized, provider-aware permission evidence with principals, grants, capabilities, provenance, completeness, and explicit collection diagnostics.
- Projects can create durable asynchronous comparisons between compatible runs, review exact added, removed, changed, and unchanged totals, and page through large result sets without loading the full diff into the browser.
- Inventory and run views expose concise permission summaries with drill-down evidence, while the comparison workspace keeps scope, compatibility, partial-result, and truncation states visible to operators.
- Permission evidence and comparison storage use provider-aware identity reconciliation, bounded ingestion, replay-safe job handling, and online indexes for large existing installations.

### Fixed

- Evidence ingestion now fails closed for malformed or ambiguous principal data while preserving valid principal-less SharePoint link and invitation grants.
- Collector telemetry validation accepts only explicitly supported bounded byte-count fields and rejects raw byte payloads or misleading aliases.
- Incomplete collection and incompatible run contracts can no longer produce authoritative-looking absence or permission-change conclusions.
- Runtime builds now install available OS security updates, and registry publication bypasses stale layer caches so published images do not retain fixed vulnerabilities from an older base-image snapshot.

### Operator notes

- Apply migrations `0012` and `0013` before starting the 1.3.0 API and worker. The second migration builds comparison indexes concurrently and must run outside a transaction.
- Permission evidence can contain sensitive identity and authorization metadata. Apply the same access, retention, backup, and export controls used for collected resource paths.

## [1.2.0] - 2026-08-25

### Added

- Project inventory now exports filtered file, resource, or endpoint results as formula-safe, bounded-memory streaming CSV without the previous 20,000-row ceiling, with documented live high-watermark consistency and export admission controls.
- Inventory tables now expose numbered cursor-page navigation, file/directory filtering with inline include/exclude shortcuts, and provider-aware connection actions for SMB, NFS, and SharePoint resources.
- SharePoint collection now records evidence-backed target availability and archive lifecycle, emits failed targeted-site assessments, and distinguishes populated, confirmed-empty, not-requested, and failed library enumeration with file, folder, item, and observed-size totals.
- SharePoint file inventory now preserves Microsoft 365 Archive states returned by Graph and summarizes archived, reactivating, not-archived, and unknown file counts per library; the local state upgrade forces one safe metadata backfill before delta collection resumes.
- SharePoint inventory rows now provide a compact assessment summary, detailed provider evidence, optional lifecycle/content/count columns, and a one-click pivot into the exact collected files and folders.

### Fixed

- Browser sessions can now use their refresh cookie after the short-lived access cookie expires because the double-submit CSRF cookie remains available for the refresh lifetime.
- The web shell now prevents stale deployment caching, returns a real 404 for missing hashed assets, validates every startup asset in deployment smokes, and shows an actionable browser-side error instead of an empty page when JavaScript cannot start.
- SMB access results now explain connected-but-not-listable, connection-only, disabled, sample-limited, unavailable, and transport-inconclusive outcomes while retaining the existing compatibility access level and strictly non-mutating probes.
- SharePoint lifecycle and library checks now keep only bounded request windows in flight, deduplicate site-collection roots, report progress, and mark unresolved lifecycle assessments partial; malformed item snapshots no longer emit a valid-looking subset.
- SMB output failures now abort collection instead of being relabeled as per-share protocol failures, and SMB1 errors retain true NTSTATUS or legacy class/code evidence with clearer missing-object and missing-share reasons.
- Rejected SharePoint libraries no longer consume the run-wide item budget, escaped invalid-target evidence stays ingestible, and folder-only libraries retain their item drill-down.
- SMB enumeration now distinguishes authorization denial from failed sessions or protocol errors, preserves aborted-probe evidence through ingestion, and retries legacy-compatible share-root syntax safely.
- Production UI smoke checks now verify that the visible footer version matches the repository release version.

## [1.1.0] - 2026-08-24

### Added

- Metadata-only SharePoint Online collection through Microsoft Graph, with unattended application inventory and delegated user quick-check perspectives, bounded paging/retries, and optional direct upload.
- Durable per-library delta checkpoints and local SQLite metadata snapshots that materialize a complete artifact on every successful run while preserving stable site, library, and drive-item identities across moves and renames.
- First-class SharePoint provider metadata, collection context, resource types, exposure evidence, inventory filters, and run-explorer presentation. Delegated `USER_VISIBLE` evidence is explicitly distinct from public, external, or anonymous exposure.
- Collector-shaped full/delta SharePoint fixtures, an end-to-end stable-ID diff smoke test, and Windows unit/contract coverage for SharePoint collector authentication paths.
- A small application-version footer so operators can identify the running web build during support and incident triage.

### Fixed

- Project deletion now serializes run creation and artifact-pointer commits, preventing a concurrent upload from leaving sensitive scan artifacts outside the deletion snapshot.
- Release publication can be rerun safely and replaces generated source assets on an existing GitHub release.
- Release verification now isolates unit tests from the production-style environment used by its deployment smoke tests.
- Gzip collector artifacts now use a Windows-compatible durability flush before their atomic rename.

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
