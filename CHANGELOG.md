# Changelog

All notable user-visible changes should be recorded in this file.

The project follows a simple release-first workflow:

- stage changes under `## Unreleased` while they are on `main`
- promote the relevant entries into a versioned section when cutting a tag
- keep migrations, env var changes, and operator-facing caveats explicit

## Unreleased

- No unreleased changes yet.

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
