# Architecture overview

Share Sentinel is a small multi-process system built around one durable workflow: receive a collection artifact, normalize it into project-scoped inventory, and expose that inventory through a browser UI and automation-friendly API.

## Scope

In scope:

- authentication and authorization
- project and membership management
- run creation and artifact upload
- asynchronous ingest into normalized tables
- inventory review, diffing, search, saved investigations, and audit

Out of scope:

- live endpoint collection from the server side
- object storage orchestration
- MFA, SSO, or SCIM
- high-availability orchestration beyond the local Docker deployment

## Runtime components

### Gateway

Traefik fronts the API and UI in the default Docker deployment. The bundled Compose file binds it to `127.0.0.1:80` by default.

In the bundled Compose stack, Traefik discovers services through a read-only mount of the host Docker socket. That is a real trust boundary and should be reviewed before using this deployment model outside a local workstation.

### API

The FastAPI service is the control plane. It owns:

- user auth and browser session cookies
- API token issuance and revocation
- sysadmin settings and audit APIs
- project, membership, run, and inventory APIs
- artifact upload validation and queue handoff

### Worker

The worker consumes `ingest_jobs` from Redis Streams, reads uploaded artifacts from shared storage, and writes normalized inventory into Postgres.

### Postgres

Postgres stores the durable application state:

- users and memberships
- API tokens and refresh tokens
- runs and ingest progress
- endpoints, resources, items, and ingest errors
- audit events
- saved investigations

### Redis

Redis is used for:

- ingest queueing through Streams
- login throttling and fixed-window rate limiting

### Shared artifact storage

The default deployment uses a shared `/artifacts` volume mounted into the API and worker. The API writes raw uploads there; the worker reads from the same location during ingest.

This is a hard deployment invariant: both services must share the same durable filesystem path, not just similar local directories.

### UI

The React UI is a browser client for the API. It does not talk directly to Postgres, Redis, or the worker.

### Collector

The bundled collector is an external producer. It can write a compatible artifact locally or create a run and upload the artifact directly to the API.

## Data flow

### 1. Collection

The collector scans SMB and NFS targets and writes schema-v1 NDJSON by default, optionally gzip-compressed based on the output suffix. NDJSON records are spooled incrementally so one endpoint tree does not need to be rebuilt in memory; after collection finishes, the finalized artifact is streamed from disk during upload. Explicit `.json` and `.json.gz` outputs remain available as bounded compact-format compatibility exports.

### 2. Upload

An operator or the collector creates a run, uploads the artifact, and the API:

- validates content type, filename, and basic payload structure
- streams the raw artifact to a unique immutable key on shared storage without holding a database transaction for the body transfer
- reacquires the run mutation lock, rechecks authoritative status, and records artifact metadata on the run
- enqueues the run id into Redis

### 3. Ingest

The worker consumes the queued job, opens the artifact, parses the records, and upserts:

- endpoints
- resources
- items
- ingest errors

The worker updates `scan_runs.summary` and `scan_runs.ingest_progress` as it goes.

If Redis queue handoff falls back, the run can remain `UPLOADED` until the worker discovers it through its recovery scan.

### 4. Query and review

The UI and API clients query Postgres-backed endpoints for:

- project inventory
- run detail and issues
- run-to-run diff
- audit history
- user and token administration

## Trust boundaries

The most important boundaries are:

### Browser and public HTTP boundary

User-controlled requests hit the API through the gateway. Browser auth uses cookies and CSRF checks for unsafe methods.

### Artifact ingestion boundary

Uploaded artifacts are treated as untrusted input. The API validates upload shape and the worker applies decompression and parser limits before normalization.

### Admin control boundary

Sysadmin routes can manage users, memberships, tokens, and global audit data. This is the highest-privilege application surface.

### Automation boundary

Project API tokens are separate from browser sessions and are scoped by role and explicit token scopes.

### Shared storage boundary

Artifacts live outside the database on a shared volume. Both the API and worker must be trusted to access that storage path.

## Authentication and authorization model

Share Sentinel splits auth into two paths:

### Browser sessions

- short-lived JWT access cookies
- refresh cookies
- CSRF cookie/header checks for unsafe methods
- session invalidation on logout, logout-all, password change, admin password reset, disable, and unapprove

### API tokens

- stored hashed in Postgres
- project-scoped
- constrained by membership role plus token scopes
- intended for collector and automation use cases

Authorization is project-scoped for most operational workflows and sysadmin-scoped for global settings and governance routes.

## Failure and recovery model

The worker is designed for asynchronous ingest and partial recovery:

- runs can be resumed from saved ingest progress
- stale pending Redis messages are reclaimed
- `UPLOADED` runs can be rediscovered if queue handoff falls back

Current caveats:

- retryable ingest failures are rescheduled with bounded backoff before the run is marked `FAILED`
- Redis stream loss or unavailability does not remove the durable `UPLOADED` state; the worker periodically discovers due runs from Postgres
- NDJSON records and compact JSON compatibility documents have explicit parser/materialization limits; large collections should use NDJSON
- the default deployment uses a worker heartbeat file and container healthcheck instead of an HTTP health endpoint
- the default Compose deployment is for local operation, not HA orchestration
- inventory views can include data from `INGESTING` runs until ingest settles

## ADR-style decisions

### ADR 1: Async ingest instead of request-time parsing

Artifacts are parsed by a worker, not inline with the upload request. This keeps uploads responsive and allows progress tracking and recovery.

### ADR 2: Project-scoped RBAC plus separate sysadmin control plane

Operational review stays inside project roles, while global user, token, and audit administration is restricted to sysadmins.

### ADR 3: Cookie-backed browser sessions plus project API tokens

Interactive browser use and automation use different credential types. This keeps browser CSRF handling separate from automation token scoping.

### ADR 4: Shared artifact volume instead of object storage in the default stack

The bundled deployment favors a simpler local-first topology. Raw artifacts are written to a shared volume so the API and worker can coordinate without introducing external object storage as a required dependency.
