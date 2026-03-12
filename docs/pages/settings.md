# Settings

The settings area is the sysadmin control surface for Share Sentinel. It is where global administration happens once projects and users are already in motion.

## Who can use it

- UI access is sysadmin-only.
- The backing API routes are also sysadmin-only.
- API tokens still need the right scopes even when the owner is a sysadmin.

## Navigation

Current tabs:

- `Overview`
- `Access`
- `Tokens`
- `Audit`

Older links such as `/settings/users` and `/settings/rbac` still redirect into the current `Access` area.

## Overview

The overview page is a live snapshot, not a checklist page.

It shows:

- whether self-registration is open or approval-only
- how many users exist, how many are active, and how many are still pending approval
- active versus revoked token counts
- project count
- the current password policy
- login guardrails such as lockout windows
- token hygiene details like default expiry and never-expiring token count
- recent global audit events

This page is meant to answer "what is the current posture right now?" without forcing an admin to click through every tab.

## Access

`Access` combines the older user and RBAC views into one workflow.

What admins can do here:

- search and filter the user directory
- create users
- approve or unapprove accounts
- enable or disable accounts
- grant or remove sysadmin status
- set passwords
- assign project roles
- apply one role across all projects
- review the global membership directory

Important guardrails enforced by the API:

- an admin cannot disable or unapprove themselves
- an admin cannot remove their own sysadmin status
- the last active approved sysadmin cannot be removed
- the last project admin on a project cannot be removed

## Tokens

`Tokens` is the global API token admin page.

Main workflows:

- search the full token inventory
- create a token for an approved project member
- adjust name, role, scopes, and expiry
- rotate a token secret
- revoke a token

The UI now treats secrets as sensitive values:

- newly created or rotated secrets are shown in a dedicated reveal component
- destructive actions use real dialogs instead of browser prompts

Key policy rules:

- token role cannot exceed the user's project membership role
- the target user must already be a project member
- the target user must be active and approved

## Audit

`Audit` is the global event stream for the whole system.

It supports:

- text search
- cursor-based pagination
- CSV export
- JSON export

This is the right place to review changes across users, tokens, memberships, runs, and other admin actions without dropping into the database.

## Related APIs

The settings area depends mostly on these routes:

- `GET /auth/security-settings`
- `GET /users`
- `POST /users`
- `PATCH /users/{user_id}`
- `PATCH /users/{user_id}/status`
- `PATCH /users/{user_id}/approval`
- `POST /users/{user_id}/assign-all-projects`
- `GET /settings/projects`
- `GET /settings/api-token-scopes`
- `GET /settings/api-tokens`
- `POST /settings/api-tokens`
- `PATCH /settings/api-tokens/{token_id}`
- `POST /settings/api-tokens/{token_id}/rotate`
- `DELETE /settings/api-tokens/{token_id}`
- `GET /settings/audit`
- `GET /settings/audit/export`
- `GET /settings/rbac/project-memberships`
- `POST /settings/rbac/project-memberships`
- `DELETE /settings/rbac/project-memberships/{project_id}/{user_id}`
- `POST /settings/rbac/users/{user_id}/assign-all-projects`
