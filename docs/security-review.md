# Security review

Review date: 2026-08-30
Target: Share Sentinel `dev` continuous-monitoring expansion
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

### SS-SEC-012 — Monitoring audit metadata growth and secret exposure

- Severity: Medium
- Status: Resolved
- Affected: audit event service, finding/source mutations, worker automation, and artifact reconciliation
- Resolution: recursive sensitive-key redaction now covers credentials, connection strings, assertions, keys, tokens, and token hashes; metadata depth, field count, collection length, text length, and serialized size are bounded before persistence. Atomic bulk finding changes retain a shared batch identifier and an event for every affected finding.
- Resolution: immutable non-FK project, user, and API-token references plus event-time labels preserve supported forensic attribution for new events when mutable parents are renamed or deleted. Concurrent lookup indexes precede restart-safe bounded legacy backfill and parent-deletion triggers; legacy live-parent rows receive upgrade-time labels, while prior orphans remain unrecoverable. Token secrets and hashes are never snapshotted.
- Remaining responsibility: the application does not choose an audit-retention period and Postgres is not an append-only/WORM sink. Audit backup, export, privacy review, external immutable forwarding, access review, and deletion policy remain deployment controls. Attribution orphaned before the snapshot schema existed cannot be reconstructed.

### SS-SEC-013 — SharePoint certificate and national-cloud boundary

- Severity: Medium
- Status: Resolved
- Affected: SharePoint collector authentication and Graph client configuration
- Resolution: supported cloud profiles bind authority, Graph audience/host, and SharePoint hostname suffix together; state is partitioned by cloud. Certificate files must be bounded regular files and, on Unix, owner-readable only, with symlink traversal rejected. Secrets, certificate material, imported tokens, and delta links are excluded from artifacts and logs.
- Remaining responsibility: protect collector state and process environment, use least-privileged application consent, and treat additional directory/group permissions as a separate reviewed opt-in.

### SS-SEC-014 — Effective-access overstatement

- Severity: High
- Status: Resolved within the current evidence boundary
- Affected: collector completeness, normalized permission evidence, comparison findings, and effective-access UI/API
- Resolution: direct provider grants, assessed-identity capability probes, and provider-computed decisions are separate evidence planes. Group membership, inheritance, partial retrieval, unresolved identity, NFS authentication, DFS targets, and incompatible snapshots remain explicitly unknown or indeterminate. Finding resolution requires authoritative coverage.
- Remaining risk: Share Sentinel is not a directory-service authorization simulator. A collector identity's observed access and an ACL entry do not by themselves prove every user's effective access.

## Accepted product limitations

- No MFA, SSO, SCIM, or external identity-provider integration.
- No turnkey TLS or HA topology; TLS termination and proxy policy are deployment responsibilities.
- No malware/content scanner for uploaded artifacts.
- No formal compatibility guarantee for multiple application versions running during migration.
- No automatic notification connector, custom policy authoring, or retention scheduler.
- No complete group-membership/inheritance expansion or cross-provider effective-entitlement engine.
- No dedicated worker HTTP metrics endpoint or comprehensive per-stage tracing dashboard; the sysadmin API metrics endpoint exposes bounded durable backlog/age and dependency signals.

These limitations do not block an initial source release, but they should be revisited before high-assurance or large multi-tenant use.
