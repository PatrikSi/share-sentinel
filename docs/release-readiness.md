# Release readiness checklist

Use this checklist before publishing a public release of the current tree. It is split between items already present in the repository and maintainer-run checks that should happen for each tagged release.

## Already present in the current tree

- [x] Top-level project documentation: `README.md`, `docs/README.md`, and subsystem READMEs
- [x] Contributor and governance docs: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SUPPORT.md`, `SECURITY.md`, and `LICENSE`
- [x] Architecture and workflow docs covering API, UI, settings, and ingest behavior
- [x] Cookie-backed browser sessions with CSRF protection and session invalidation on logout and password resets
- [x] Project-scoped API tokens with hashed storage, scoped authorization, and expiry defaults
- [x] Worker retry scheduling, stale-run recovery, and Docker health checks backed by heartbeat files
- [x] Automated tests for API, worker, and collector components
- [x] Local Docker Compose deployment with bootstrap, API, worker, UI, Postgres, Redis, and gateway services
- [x] CI with dependency audits, UI production build, production/development Compose validation, all four application image builds, a live ingest, and a production-style security smoke test
- [x] GHCR publication for `latest`, exact commit, and exact release image tags with high/critical vulnerability scans, provenance, and SBOM attestations
- [x] Tracked synthetic ingest fixture at `examples/sample-artifact.json`
- [x] Tag-driven source archives and SHA-256 checksums

## Per-release validation

- [ ] Pick the release commit and tag name
- [ ] Run `./bootstrap.sh` and `docker compose up -d --build` from a clean checkout
- [ ] Run `./scripts/smoke-routes.sh http://localhost`
- [ ] Run `./scripts/smoke-ingest.sh http://localhost` with `SHARE_SENTINEL_SMOKE_PASSWORD` set
- [ ] Run API tests: `docker compose exec -T api bash -lc "pip install -q -r requirements-dev.txt && pytest -q"`
- [ ] Run worker tests: `cd worker && pip install -r requirements-dev.txt && pytest -q`
- [ ] Run collector tests: `cd collector && pip install -r requirements-dev.txt && pytest -q`
- [ ] Run UI validation: `cd ui && npm ci && npm run typecheck && npm run build`
- [ ] Run `python scripts/check-release.py --tag vX.Y.Z`
- [ ] Run Python and npm dependency audits
- [ ] Run `./scripts/smoke-production.sh` against the production-style hostname and gateway

## Security and deployment checks

- [ ] Replace all default secrets in `.env`
- [ ] Keep `ALLOW_NEVER_EXPIRING_API_TOKENS=false` unless you have a reviewed exception for a non-production environment
- [ ] Set `APP_ENV=production`-style values, `AUTH_COOKIE_SECURE=true`, and a TLS-terminating reverse proxy before internet exposure
- [ ] Set `TRUSTED_HOSTS` and narrow `CORS_ORIGINS` and `TRUSTED_PROXY_CIDRS` to the real deployment topology
- [ ] Verify deep health and metrics routes are reachable only by sysadmins
- [ ] Confirm the artifact volume is durable and shared consistently between API and worker
- [ ] Review audit logs after smoke testing to confirm auth, upload, and ingest events are recorded as expected

## Open source release checks

- [ ] Confirm the repository host has a private security reporting path enabled
- [ ] Draft release notes in `CHANGELOG.md`, including user-visible behavior changes, migrations, and new env vars
- [ ] Push the matching `vX.Y.Z` tag and verify the release workflow publishes source archives and checksums
- [ ] Confirm all four `vX.Y.Z` GHCR images map to the same tagged source release and commit
- [ ] Confirm all four GHCR packages have the intended visibility; public packages support anonymous Compose pulls
- [ ] Confirm issue tracker, default branch, and support expectations match `SUPPORT.md`
- [ ] Re-read `README.md` and `SECURITY.md` for any placeholders or environment-specific wording before publishing
