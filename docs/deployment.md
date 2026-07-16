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
./scripts/bootstrap-env.sh
docker compose up -d --build
docker compose ps
./scripts/smoke-routes.sh http://localhost
export SHARE_SENTINEL_SMOKE_PASSWORD='<the SEED_ADMIN_PASSWORD value>'
./scripts/smoke-ingest.sh http://localhost admin@example.com
unset SHARE_SENTINEL_SMOKE_PASSWORD
```

The script creates `.env` with random database, JWT, token-pepper, and administrator secrets, prints the initial administrator password once, and sets `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml`. The gateway listens on `127.0.0.1:80`; the `bootstrap` service applies migrations and creates the initial admin before the API and worker start.

To regenerate intentionally, pass `--force`. Do not use `--force` on an environment whose generated secrets are already protecting persistent data.

## Production configuration contract

Do not change `APP_ENV` to `production` until HTTPS is in place. Secure browser cookies will not work over plain HTTP.

At minimum, set:

```dotenv
COMPOSE_FILE=docker-compose.yml
APP_ENV=production
APP_HOST=sentinel.example.com
SHARE_SENTINEL_STACK=production
SHARE_SENTINEL_IMAGE_TAG=v0.2.0
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
./scripts/bootstrap-env.sh \
  --production sentinel.example.com \
  --admin-email admin@example.com \
  --image-tag v0.2.0
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

The production smoke verifies the public health route, hidden API docs, UI routing, secure browser cookies, response headers, and exact configured CORS origin. The ingest smoke creates and removes its own temporary project; a successful run reaches `COMPLETE`, shows two endpoints and two resources, and records the synthetic warning under run Issues.

Production-style API startup fails fast when secure cookies, proxy CIDRs, hostnames, secrets, or seed-admin settings are missing. Interactive API docs are disabled outside development/test environments.

The prebuilt UI writes `/runtime-config.js` when its container starts, using `VITE_API_BASE_URL`, `VITE_CSRF_COOKIE_NAME`, and `VITE_CSRF_HEADER_NAME` from Compose. This keeps one published UI image compatible with deployment-specific API paths and CSRF names. Unsafe values fail container startup instead of being injected into JavaScript; keep the VITE CSRF values identical to the corresponding `AUTH_CSRF_*` API values.

## Published images and tags

The workflows build and publish these images:

- `ghcr.io/patriksi/share-sentinel-api` — API runtime plus bootstrap/migrations
- `ghcr.io/patriksi/share-sentinel-worker` — Redis ingest worker
- `ghcr.io/patriksi/share-sentinel-ui` — static UI served by Nginx
- `ghcr.io/patriksi/share-sentinel-collector` — standalone SMB/NFS collector CLI

Verified `main` commits receive `latest` and `sha-<full-commit>`. Verified release tags receive those tags plus the exact `vX.Y.Z`. The release workflow publishes images only after tests, dependency audits, high/critical image vulnerability scans, local image builds, the full ingest smoke, and the production-style security smoke succeed. Published images include OCI source/revision labels, provenance, and an SBOM attestation.

## Persistence and backups

Durable state is split across:

- `pgdata`: authoritative users, projects, runs, inventory, tokens, and audit data
- `artifacts`: raw uploaded artifacts required for ingest and retained run data
- `redisdata`: stream and rate-limit state; Postgres-backed recovery can rediscover some queued runs, but Redis should still be treated as operational state

Back up Postgres and the artifact volume as one operational recovery point. A database restore without matching artifacts can leave stored run metadata pointing to missing files. Encrypt backups when scan paths and host data are sensitive, and test restoration before relying on it.

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

- One worker process handles jobs serially; add replicas only with shared artifact storage and after testing database/Redis pressure.
- Run diff responses are not paginated and can grow with large churn.
- The default 10 GiB upload ceiling is enforced by the API and matched by the bundled Nginx configuration.
- The Compose deployment has single Postgres, Redis, and gateway instances and therefore no HA guarantee.
- API metrics do not include a full worker backlog or per-stage ingest telemetry surface.

For these reasons, capacity testing and a deployment-specific recovery plan are required before treating the current release as a critical production service.
