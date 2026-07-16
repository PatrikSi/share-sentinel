# Security policy

Share Sentinel handles uploaded scan artifacts, browser sessions, API tokens, and administrative workflows. Treat it like a security-sensitive service, not like a static demo site.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability.

Use a private reporting path instead:

- Use the primary repository host's private security advisory or vulnerability-reporting feature when it is available.
- If you are working from a mirror or fork without that feature, use the private maintainer contact path published by that fork before public disclosure.

When reporting, include:

- affected commit or release
- deployment method
- whether `APP_ENV` was production-style
- reverse-proxy and TLS details
- sanitized reproduction steps
- any relevant `X-Request-ID`

Do not send:

- real collector artifacts
- SMB or NFS credentials
- API tokens
- browser cookies
- real hostnames, share names, or file paths unless they are fully sanitized

## Supported versions

At the moment, security fixes should be assumed to land on `main` first.

- Supported: the current `main` branch
- Supported when present: the latest tagged source release
- Not supported as a separate maintenance stream: older commits, forks, unpublished local variants, or convenience Docker images older than the latest tagged source release

## Secure deployment baseline

Before exposing Share Sentinel outside a local workstation:

1. Replace all default secrets in `.env`, especially `POSTGRES_PASSWORD`, `JWT_SECRET`, `TOKEN_PEPPER`, and `SEED_ADMIN_PASSWORD`.
2. Keep the gateway bound to localhost or put it behind a TLS-terminating reverse proxy with access controls.
3. Enable HTTPS and set `AUTH_COOKIE_SECURE=true` for real deployments.
4. Set a production-style `APP_ENV` so interactive API docs are not exposed by default.
5. Set `TRUSTED_HOSTS` to the deployed hostnames, narrow `CORS_ORIGINS`, and configure `TRUSTED_PROXY_CIDRS` for every real proxy hop so audit and rate-limit IP attribution is correct.
6. Keep Postgres, Redis, and the shared artifact volume on private network boundaries.
7. Restrict who can reach sysadmin routes, including deep health and metrics.
8. Prefer expiring API tokens over never-expiring tokens unless you have a strong operational reason.
9. Review audit events regularly and rotate credentials after personnel or environment changes.
10. Review the Traefik Docker-socket mount and replace that service-discovery model if your environment does not allow the gateway to read container metadata from the host.

## Current security posture

The project currently provides:

- cookie-backed browser sessions with CSRF protection
- project-scoped RBAC
- hashed API token storage
- login throttling and upload rate limiting
- audit logging for administrative and many read actions
- worker heartbeat files plus a Docker healthcheck in the default stack
- localhost-first Docker defaults
- trusted-host enforcement, explicit CORS methods/headers, and production configuration fail-fast checks
- digest-pinned base images with unprivileged API and worker processes

The project does not currently provide:

- MFA
- SSO
- SCIM
- a dedicated worker HTTP health endpoint
- a formal long-term support release process

## Important limitations

- `GET /healthz` is public; `/healthz/deep` and `/metrics` require sysadmin access.
- Artifact validation at upload time is structural, not malware-aware.
- Raw artifacts are stored on shared disk for worker access.
- Share Sentinel does not provide application-layer encryption for raw artifacts; use encrypted storage and backups when required.
- Upload throttling fails closed by default when Redis is unavailable. This can be changed with `RATE_LIMIT_FAIL_OPEN=true`, but the shipped default is `false`.

The current code-level review and accepted deployment risks are recorded in [docs/security-review.md](./docs/security-review.md).

## Disclosure expectations

Please allow time for confirmation, remediation, and coordinated disclosure before publishing details. If a report requires a breaking or user-visible mitigation, the project may prioritize a documented workaround before a full fix.

## Scope notes

When reporting a vulnerability, include:

- affected component or route
- deployment assumptions
- whether the issue needs auth, sysadmin access, or a crafted artifact
- impact on confidentiality, integrity, or availability
- reproduction steps and any proof-of-concept details

Good reports are usually the fastest way to get a fix shipped.
