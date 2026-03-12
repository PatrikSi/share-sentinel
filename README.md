# share-sentinel

Share Sentinel ingests SMB collection artifacts and gives you a project-scoped workspace for review. It is built for a simple loop: upload a collector artifact, watch the ingest, compare runs, and drill into hosts, shares, and paths without losing project context.

## What it includes

- A FastAPI control plane with JWT sessions, cookie auth with CSRF protection, and scoped API tokens
- Project-scoped RBAC with `viewer`, `operator`, and `admin` roles
- Optional self-registration with admin approval
- Configurable password policy enforced at startup and at password change / registration time
- A React UI with a dashboard, import flow, compact inventory review, run diffing, and admin settings
- An async worker that ingests artifacts from object storage into Postgres
- A Python collector that can write artifacts locally or upload them directly

## Quick start

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Build and start the stack:

```bash
docker compose up --build
```

3. Open the app and API docs:

- `http://localhost`
- `http://localhost/api/docs`

4. If you want a quick routing check after startup:

```bash
./scripts/smoke-routes.sh
```

## Seed admin and password policy

The bootstrap container seeds the first admin account from these environment variables:

- `SEED_ADMIN_EMAIL`
- `SEED_ADMIN_PASSWORD`

Password rules are also driven from the environment:

- `PASSWORD_MIN_LENGTH`
- `PASSWORD_REQUIRE_LOWERCASE`
- `PASSWORD_REQUIRE_UPPERCASE`
- `PASSWORD_REQUIRE_NUMBER`
- `PASSWORD_REQUIRE_SPECIAL`

If `SEED_ADMIN_PASSWORD` does not satisfy the active policy, bootstrap fails immediately with a clear configuration error. That makes bad env combinations obvious in container logs instead of quietly skipping the seed account.

## Main workflows

### Dashboard

The dashboard is the landing area after login. Pick a project in the top bar, review recent runs, check high-level inventory counts, and jump straight into inventory or the latest run.

### Import

Operators and admins can create a run, attach a collector artifact, and upload it from the browser. The import page does basic preflight checks, shows the detected file type and size, and redirects into the run explorer once ingest starts.

### Inventory

Inventory stays scoped to the current project and supports three views:

- files and folders
- shares
- endpoints

The page is guided first. Most work can be done with compact filters and extension chips, while the query DSL is still available for more specific searches.

### Run explorer

Each run has four focused views:

- `Overview`
- `Diff`
- `Explore`
- `Search`

That split keeps baseline comparison, tree exploration, and item search from fighting for space on one screen.

### Settings

Sysadmins get four settings areas:

- `Overview` for live posture, password policy, token hygiene, and recent audit events
- `Access` for users, approvals, sysadmin status, and project membership management
- `Tokens` for global API token administration, including one-time secret reveal on create or rotate
- `Audit` for global event review and export

## Repo layout

- `api/` FastAPI service, auth, RBAC, project APIs, and settings APIs
- `worker/` ingestion worker for queued artifact processing
- `collector/` SMB collector CLI
- `ui/` React single-page app
- `docs/` product and reference documentation

## Documentation

- [Docs index](./docs/README.md)
- [API service notes](./api/README.md)
- [Collector notes](./collector/README.md)
- [Worker notes](./worker/README.md)

## Useful endpoints

- `GET /api/healthz`
- `GET /api/healthz/deep`
- `GET /api/metrics`

## Notes

- Ingestion is async and idempotent.
- The worker can resume interrupted ingestion and reconcile runs left in `UPLOADED`.
- Every API response includes an `X-Request-ID`.
- Auth and upload endpoints are rate-limited.
