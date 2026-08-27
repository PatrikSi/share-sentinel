# Security review

Review date: 2026-08-27
Target: Share Sentinel 1.3.0 open-source release
Scope: API, worker, collector, UI, dependency manifests, containers, and reference Compose topology

## Outcome

No known high- or critical-severity dependency advisories remain in the reviewed manifests. The core authentication, project authorization, upload, ingest, and administration paths have automated coverage. The repository is suitable for an initial open source publication with the deployment limitations below stated explicitly.

This is a code and configuration review, not a penetration test or certification.

## Findings

### SS-SEC-001 — Vulnerable dependency set

- Severity: High
- Status: Resolved
- Affected: `api/requirements.txt`, `worker/requirements.txt`, `collector/requirements.txt`, `ui/package.json`
- Resolution: upgraded the affected Python dependency chains and moved the UI to React Router 7.18.2, PostCSS 8.5.26, and NanoID 3.3.18. Current `pip-audit` checks for all Python development manifests and `npm audit --audit-level=high` report no known vulnerabilities.
- Regression control: `.github/workflows/ci.yml` and `.github/dependabot.yml`

### SS-SEC-002 — Unrestricted host and CORS configuration

- Severity: Medium
- Status: Resolved
- Affected: `api/app/config.py:109`, `api/app/config.py:162`, `api/app/main.py:29`
- Resolution: added trusted-host middleware, explicit CORS methods and headers, production rejection of wildcard CORS, validation of proxy CIDRs, and fail-fast production hostname requirements.
- Verification: configuration and request tests in `api/tests/test_config.py` and `api/tests/test_request_context.py`

### SS-SEC-003 — Mutable or contaminated container inputs

- Severity: Medium
- Status: Mitigated
- Affected: `api/Dockerfile:1`, `worker/Dockerfile:1`, `ui/Dockerfile:1`, and service `.dockerignore` files
- Resolution: version-pinned base images, service `.dockerignore` files so host dependencies cannot overwrite locked build output, no API compiler toolchain, and unprivileged API, worker, and collector processes.
- Remaining risk: version and application tags are registry-mutable by design. Published application images carry source/revision labels, provenance, and SBOM attestations, but deployments requiring immutable inputs must enforce resolved registry digests separately.
- Verification: clean container builds, high/critical Trivy scans, image-user inspection, Nginx config test, and non-root artifact write check

### SS-SEC-004 — JWT validation contract

- Severity: Medium
- Status: Resolved
- Affected: `api/app/security.py:59`, `api/app/deps.py:64`
- Resolution: replaced the vulnerable JWT dependency, pins `HS256`, verifies issuer, requires `exp`, `iat`, `iss`, and `sub`, and rejects non-access token types.
- Existing controls: server-side user/session-version checks invalidate cookies after logout-all, password changes, disable, and unapproval.

### SS-SEC-005 — Browser response hardening

- Severity: Low
- Status: Resolved
- Affected: `api/app/middleware.py:45`, `ui/nginx.conf:11`
- Resolution: API request IDs now accompany nosniff, frame, referrer, and permissions headers; auth and cookie responses are non-cacheable. The UI ships a restrictive CSP and equivalent browser headers.

### SS-SEC-006 — Secrets passed in collector arguments

- Severity: Medium
- Status: Mitigated
- Affected: `collector/share_sentinel_collector.py:44`, `collector/share_sentinel_collector.py:378`
- Resolution: collector CLI accepts SMB passwords, hashes, and API tokens through dedicated environment variables and redacts secret CLI values from artifact metadata.
- Remaining risk: environment variables are still sensitive process state. Protect the collector host, prefer Kerberos session credentials where available, and unset secrets after use.

### SS-SEC-007 — Docker socket trust boundary

- Severity: Medium
- Status: Accepted for the local reference topology
- Affected: `docker-compose.yml:2`, `docker-compose.yml:17`
- Risk: the gateway can read host container metadata through the Docker socket; compromise of a socket-aware component can increase impact.
- Control: the mount is read-only, the gateway is loopback-bound by default, and production guidance recommends static discovery when the socket is not acceptable.

### SS-SEC-008 — Sensitive raw artifact storage

- Severity: Medium
- Status: Deployment responsibility
- Affected: `docker-compose.yml:68`, `docker-compose.yml:130`, `docker-compose.yml:164`
- Risk: raw paths, hostnames, share names, and scan findings remain on filesystem storage; application-layer encryption and malware scanning are not provided.
- Control: keep storage private, use encrypted disks/backups where required, restrict volume access, and apply retention procedures outside the application.

### SS-SEC-009 — Cross-stack gateway discovery

- Severity: Medium
- Status: Resolved
- Affected: `docker-compose.yml:12`, `docker-compose.yml:137`, `docker-compose.yml:189`
- Risk: multiple Share Sentinel Compose projects on the same Docker host exposed identical Traefik router names and could be discovered by one another's gateways.
- Resolution: every gateway now constrains Docker discovery to an exact `SHARE_SENTINEL_STACK` label applied to its API and UI; the production guide requires a distinct value for each deployment.
- Verification: simultaneous development and production-style stacks routed only to their own API and UI services.

### SS-SEC-010 — Destructive confirmation enforced only by the client

- Severity: Low
- Status: Resolved
- Affected: `api/app/routers/settings.py:378`, `ui/src/pages/settings-project-detail-page.tsx:175`
- Risk: a sysadmin calling the API directly could delete a project without the exact-name confirmation presented by the UI.
- Resolution: the DELETE route requires a JSON body containing `confirm_name` and rejects anything other than the project's exact current name before deleting database rows or artifacts.
- Regression control: API router tests plus the live ingest-and-cleanup smoke test.

### SS-SEC-011 — Invalid bootstrap identity accepted until login

- Severity: Low
- Status: Resolved
- Affected: `api/app/config.py:71`
- Risk: bootstrap could create a seed administrator whose address failed the stricter login schema, leaving a fresh deployment without a usable administrator.
- Resolution: seed administrator addresses use the same validated email type during configuration loading, so startup fails before creating an unusable identity.
- Regression control: production configuration tests.

## Accepted product limitations

- No MFA, SSO, SCIM, or external identity-provider integration.
- No turnkey TLS or HA topology; TLS termination and proxy policy are deployment responsibilities.
- No malware/content scanner for uploaded artifacts.
- No formal compatibility guarantee for multiple application versions running during migration.
- No dedicated worker metrics endpoint or comprehensive queue-backlog dashboard.

These limitations do not block an initial source release, but they should be revisited before high-assurance or large multi-tenant use.
