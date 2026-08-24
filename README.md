# share-sentinel

[![CI](https://github.com/PatrikSi/share-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/PatrikSi/share-sentinel/actions/workflows/ci.yml)

Share Sentinel is a self-hostable workspace for ingesting SMB and NFS collection artifacts, tracking project-scoped inventory, and reviewing run-to-run changes without losing analyst context.

Version 1.0.0 is the initial open-source release: the core local workflow is tested end to end, while HA operation, MFA, SSO, and turnkey internet-facing deployment are intentionally outside the current support boundary.

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
- `docker-compose.yml` for a GHCR-backed single-host stack and `docker-compose.dev.yml` for local source builds

## Quick start

1. Generate a development environment with random secrets:

```bash
./bootstrap.sh
```

The script creates a mode-`600` `.env`, validates the rendered Compose stack, prints the one-time seed administrator password and exact start command, and refuses to overwrite an existing file unless `--force` is supplied. You can choose the initial login through environment overrides:

```bash
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='use-a-long-unique-password' ./bootstrap.sh
```

2. Build the application images from the current checkout and start the stack:

```bash
docker compose up -d --build
```

The generated development environment selects both Compose files automatically through `COMPOSE_FILE`.

3. Open the app and sign in with the generated administrator credentials:

- `http://localhost`

4. Optional local routing smoke test:

```bash
./scripts/smoke-routes.sh http://localhost
```

To verify the complete project, upload, worker-ingest, result, and cleanup lifecycle without real infrastructure data, provide the seeded admin password and run:

```bash
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
./scripts/smoke-ingest.sh http://localhost admin@example.com
unset SHARE_SENTINEL_SMOKE_PASSWORD
```

The same tracked fixture is available at [`examples/sample-artifact.json`](./examples/sample-artifact.json) for manual testing through the Import page.

The bundled Compose file keeps the gateway on `127.0.0.1:80` by default. That is intentional. If you expose the stack on a real network, put it behind TLS and review [SECURITY.md](./SECURITY.md) first.

The base Compose stack pulls versioned application images from `ghcr.io/patriksi`; the development override replaces them with local builds. Neither topology is a turnkey internet-facing appliance. Use the production bootstrap mode, exact release image tags, TLS, backups, and the deployment guide before network exposure.

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

The page supports guided filters, an optional query DSL, run scoping, and project-shared saved investigations. SMB results distinguish observed listing, file-read, create-file, create-directory, modify, delete, ACL-change, and ownership-change capabilities instead of treating every listable share as equally readable.

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
- `GET /api/healthz` and the generic dependency readiness route `/api/healthz/ready` are public for load balancers; deep health and Prometheus metrics are sysadmin-only routes.
- The bundled gateway relies on a read-only host Docker socket mount for Traefik service discovery.
- Each gateway discovers only containers with its `SHARE_SENTINEL_STACK` label, preventing multiple Share Sentinel deployments on one Docker host from routing into one another.
- OpenAPI and Swagger docs are intended for development-style environments and are hidden in production-style `APP_ENV` values.
- The default Docker deployment is local-first, not internet-ready. Replace secrets, enable TLS, and review the reverse-proxy posture before exposing it.

See the [deployment guide](./docs/deployment.md) for the production configuration contract, backups, upgrades, and known topology limits.

## Release and support policy

- Source is released from this repository and is the primary supported distribution format.
- Verified API, worker, UI, and collector images are published under `ghcr.io/patriksi/share-sentinel-*`.
- Every verified `main` commit publishes an immutable `sha-<full-commit>` tag. The run still matching the branch head also advances `latest`; superseded runs deliberately skip that mutable tag.
- Matching `vX.Y.Z` tags reuse the already verified `sha-<full-commit>` image set, publish `vX.Y.Z`, and add source archives and checksums.
- When tagged releases exist, expect support to focus on `main` plus the latest tagged release unless a future policy says otherwise.
- Production deployments should select an exact `vX.Y.Z` or `sha-<full-commit>` tag instead of tracking `latest`.

`latest` is a convenience pointer updated by verified `main` builds. The workflows treat existing `sha-<full-commit>` and `vX.Y.Z` tags as immutable and refuse to replace them with a different runnable image. Because the four components are separate packages, registry tag updates are sequential rather than atomic; use one exact tag across all services as the deployment unit for repeatable or production rollouts.

## Current limitations

- Ingest is asynchronous. Runs can stay in `UPLOADED` or `INGESTING` while the worker is catching up.
- Retryable ingest failures are rescheduled with backoff, but terminal parser or data-shape failures still land the run in `FAILED`.
- MFA, SSO, and SCIM are not implemented.
- SMB capability checks are bounded observations made with the scan identity, not a guarantee for every object or for future writes. They request rights on existing handles without creating or modifying content; quotas, read-only storage, security products, and object-specific ACLs can still affect a later operation.
- NFS collection currently discovers advertised exports but does not mount them, so NFS access remains `unknown` unless a richer external artifact supplies evidence.
- The project is best treated as actively evolving rather than as a locked compatibility surface.

## Documentation

- [Docs index](./docs/README.md)
- [Architecture overview](./docs/architecture.md)
- [Deployment and operations](./docs/deployment.md)
- [Operations, scale, and recovery](./docs/operations.md)
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
