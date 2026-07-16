# Deployment and operations

## Support boundary

The checked-in Compose stack is the supported reference topology for a single-host, initial deployment. It is suitable for local evaluation and can sit behind a separately managed TLS reverse proxy. It is not a turnkey HA design, Kubernetes manifest, or unattended internet-facing appliance.

The application containers build from source. Postgres, Redis, Traefik, Python, Node, and Nginx inputs are pinned by digest so a rebuild uses reviewed base images until an explicit dependency update changes them.

## Prerequisites

- Docker Engine with Compose v2
- enough durable disk for Postgres plus retained raw artifacts
- a TLS-terminating reverse proxy for any non-local deployment
- backups for Postgres and the artifact volume

The API and worker must mount the same durable POSIX artifact volume at the same path. Local or ephemeral per-container filesystems are not interchangeable with shared storage.

## Local deployment

```bash
cp .env.example .env
```

Replace the four placeholder secrets in `.env`, then run:

```bash
docker compose up -d --build
docker compose ps
./scripts/smoke-routes.sh http://localhost
```

The gateway listens on `127.0.0.1:80` by default. The `bootstrap` service applies migrations and creates the initial admin before the API and worker start.

## Production configuration contract

Do not change `APP_ENV` to `production` until HTTPS is in place. Secure browser cookies will not work over plain HTTP.

At minimum, set:

```dotenv
APP_ENV=production
APP_HOST=sentinel.example.com
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

`APP_HOST` controls the Traefik host router. `TRUSTED_HOSTS` is the API-level host allowlist. They normally contain the same public hostname, but `TRUSTED_HOSTS` may contain a comma-separated set when the service has intentional aliases.

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
docker compose up -d --build
docker compose ps
./scripts/smoke-routes.sh https://sentinel.example.com
```

Then sign in and upload [`examples/sample-artifact.json`](../examples/sample-artifact.json). A successful verification reaches `COMPLETE`, shows two endpoints and two resources, and records the synthetic warning under run Issues.

Production-style API startup fails fast when secure cookies, proxy CIDRs, hostnames, secrets, or seed-admin settings are missing. Interactive API docs are disabled outside development/test environments.

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
4. Run `docker compose config --quiet` and `docker compose build`.
5. Run `docker compose up -d` and watch the one-shot `bootstrap` service.
6. Confirm all long-running services are healthy, run the route smoke test, and verify a synthetic ingest.

Alembic migrations are applied forward by bootstrap. Automated downgrade or zero-downtime multi-version compatibility is not promised in the current release line.

## Capacity and scaling limits

- One worker process handles jobs serially; add replicas only with shared artifact storage and after testing database/Redis pressure.
- Run diff responses are not paginated and can grow with large churn.
- The default 10 GiB upload ceiling is enforced by the API and matched by the bundled Nginx configuration.
- The Compose deployment has single Postgres, Redis, and gateway instances and therefore no HA guarantee.
- API metrics do not include a full worker backlog or per-stage ingest telemetry surface.

For these reasons, capacity testing and a deployment-specific recovery plan are required before treating the current release as a critical production service.
