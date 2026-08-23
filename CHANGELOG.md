# Changelog

All notable user-visible changes should be recorded in this file.

The project follows a simple release-first workflow:

- stage changes under `## Unreleased` while they are on `main`
- promote the relevant entries into a versioned section when cutting a tag
- keep migrations, env var changes, and operator-facing caveats explicit

## Unreleased

### Added

- GHCR publication for API, worker, UI, and collector images after full CI or release verification, including exact commit/release tags, vulnerability scans, provenance, and SBOM attestations.
- A containerized collector with NFS tooling and an unprivileged runtime.
- `docker-compose.dev.yml` for local source builds and a root-level `bootstrap.sh` that generates and validates development or production environment files.

### Changed

- The base Compose stack pulls the canonical `ghcr.io/patriksi/share-sentinel-*` images directly; local development selects the source-build override through generated `COMPOSE_FILE` configuration.
- Container inputs now use readable exact version tags rather than digest pins.
- Upgraded React Router, PostCSS, and the transitive NanoID dependency to clear the current UI dependency audit without bundling unrelated framework major upgrades.
- Updated GitHub Actions runtimes to their Node 24-compatible releases so CI no longer relies on GitHub's deprecated Node 20 action shim.
- CI now pulls the promoted `latest` image set, verifies its revision labels, and runs a clean production Compose smoke test against the published artifacts.
- Fixed SMB authentication identity handling for `DOMAIN\user`, `DOMAIN/user`, UPN, explicit-domain, and local-account forms; conflicting modes now fail early and Kerberos correctly receives configured NTLM hashes.

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
