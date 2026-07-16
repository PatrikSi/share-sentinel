# share-sentinel

[![CI](https://github.com/PatrikSi/share-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/PatrikSi/share-sentinel/actions/workflows/ci.yml)

Share Sentinel is a self-hostable workspace for ingesting SMB and NFS collection artifacts, tracking project-scoped inventory, and reviewing run-to-run changes without losing analyst context.

Version 0.2.0 is an initial publication candidate: the core local workflow is tested end to end, while HA operation, MFA, SSO, and turnkey internet-facing deployment are intentionally outside the current support boundary.

It is built around one loop:

1. collect data with the bundled collector or another compatible producer
2. upload the artifact into a project
3. let the worker ingest it into Postgres
4. review inventory, issues, diffs, and saved investigations in the UI

## What is included

- `api/` FastAPI control plane for auth, RBAC, projects, runs, inventory, settings, and audit
- `worker/` background ingestion worker fed by Redis Streams
- `ui/` React + Vite single-page app
- `collector/` Python CLI for SMB and NFS collection plus optional direct upload
- `docker-compose.yml` for a local all-in-one stack with Traefik, Postgres, Redis, API, worker, and UI

## Quick start

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Edit `.env` and replace the required placeholder values before starting the stack:

- `POSTGRES_PASSWORD`
- `JWT_SECRET`
- `TOKEN_PEPPER`
- `SEED_ADMIN_PASSWORD`

The example file intentionally uses placeholder values that fail fast until you replace them.

3. Build and start the stack:

```bash
docker compose up --build
```

4. Open the app:

- `http://localhost`

5. Optional local routing smoke test:

```bash
./scripts/smoke-routes.sh http://localhost
```

To exercise ingest without real infrastructure data, upload [`examples/sample-artifact.json`](./examples/sample-artifact.json) through the Import page.

The bundled Compose file keeps the gateway on `127.0.0.1:80` by default. That is intentional. If you expose the stack on a real network, put it behind TLS and review [SECURITY.md](./SECURITY.md) first.

The checked-in Compose stack is for local evaluation and development. Do not expose it as-is with placeholder secrets, default admin credentials, or plain HTTP.

The default gateway also mounts the host Docker socket read-only so Traefik can discover the API and UI containers. Treat that as a trust boundary in its own right and review whether that deployment model fits your environment before publishing the stack.

## Main workflows

### Dashboard

The dashboard stays project-scoped. It surfaces recent runs, project inventory totals, and shortcuts into inventory, import, and run review.

### Import

Operators and admins can create a run, upload a JSON, NDJSON, JSONL, or gzip-compressed artifact, and land directly in the run explorer while ingest starts.

### Inventory

Project inventory supports three working views:

- files and folders
- shares
- endpoints

The page supports guided filters, an optional query DSL, run scoping, and project-shared saved investigations.

### Run explorer

Each run is split into five focused tabs:

- `Overview`
- `Issues`
- `Diff`
- `Explore`
- `Search`

Run-scoped saved searches remain browser-local. Project-wide shared investigations live on the project inventory page.

### Settings

Sysadmins get five settings areas:

- `General` for security posture, token hygiene, and recent audit activity
- `Users` for approvals, password resets, and project memberships
- `Projects` for project ownership, rename, membership review, and guarded deletion
- `API Tokens` for global machine credential administration
- `Audit Log` for global event review and export

## Architecture at a glance

- The API stores uploaded artifacts on the shared `/artifacts` volume.
- The API enqueues ingest work into the `ingest_jobs` Redis stream.
- The worker reads the artifact from the same shared storage and writes normalized inventory into Postgres.
- The UI reads only through the API.
- The bootstrap container applies Alembic migrations and seeds the initial admin account.

For a fuller component and trust-boundary walkthrough, see [docs/architecture.md](./docs/architecture.md).

## Security and deployment notes

- Browser auth uses cookie-backed JWT sessions with CSRF protection.
- API automation uses hashed, project-scoped API tokens with role and scope checks.
- `GET /api/healthz` is public, while deep health and Prometheus metrics are sysadmin-only routes.
- The bundled gateway relies on a read-only host Docker socket mount for Traefik service discovery.
- OpenAPI and Swagger docs are intended for development-style environments and are hidden in production-style `APP_ENV` values.
- The default Docker deployment is local-first, not internet-ready. Replace secrets, enable TLS, and review the reverse-proxy posture before exposing it.

See the [deployment guide](./docs/deployment.md) for the production configuration contract, backups, upgrades, and known topology limits.

## Release and support policy

- Source is released from this repository and is the primary supported distribution format.
- Before the first tag, support is best-effort on `main`; matching `vX.Y.Z` tags publish verified source archives and checksums.
- When tagged releases exist, expect support to focus on `main` plus the latest tagged release unless a future policy says otherwise.
- Docker images may be published for convenience, but they should be treated as secondary artifacts to the tagged source release.

## Current limitations

- Ingest is asynchronous. Runs can stay in `UPLOADED` or `INGESTING` while the worker is catching up.
- Retryable ingest failures are rescheduled with backoff, but terminal parser or data-shape failures still land the run in `FAILED`.
- MFA, SSO, and SCIM are not implemented.
- The project is best treated as actively evolving rather than as a locked compatibility surface.

## Documentation

- [Docs index](./docs/README.md)
- [Architecture overview](./docs/architecture.md)
- [Deployment and operations](./docs/deployment.md)
- [Security review](./docs/security-review.md)
- [API reference](./docs/reference/api.md)
- [Auth and RBAC reference](./docs/reference/auth-rbac.md)
- [Frontend reference](./docs/reference/frontend.md)
- [Release readiness checklist](./docs/release-readiness.md)
- [Settings guide](./docs/pages/settings.md)
- [API service README](./api/README.md)
- [Collector README](./collector/README.md)
- [Worker README](./worker/README.md)
- [Security policy](./SECURITY.md)
- [Contributing guide](./CONTRIBUTING.md)
- [Support guide](./SUPPORT.md)
- [Code of conduct](./CODE_OF_CONDUCT.md)
- [Changelog](./CHANGELOG.md)
