# Settings

The settings area is the sysadmin control surface for Share Sentinel. It is where global administration happens once projects and users are already active.

## Who can use it

- UI access is sysadmin-only.
- The backing API routes are also sysadmin-only.
- API tokens still need the right scopes even when the owner is a sysadmin.

## Navigation

Current tabs:

- `General`
- `Users`
- `Projects`
- `API Tokens`
- `Audit Log`

Older links such as `/settings/iam` and `/settings/rbac` still redirect into the current `Users` area.

## General

The overview page is a live posture snapshot, not a checklist page.

It shows:

- whether self-registration is open or approval-only
- how many users exist, how many are active, and how many are still pending approval
- active versus revoked token counts
- project count
- the current password policy
- login guardrails such as lockout windows
- token hygiene details like default expiry, whether never-expiring issuance is allowed, and how many legacy never-expiring tokens still exist
- recent global audit events

## Users

`Users` is a two-screen workflow.

### Directory page

The main page supports:

- searching and filtering the user directory
- creating users
- browsing summary membership tags
- opening a specific user detail page

### Per-user detail page

The detail page handles the sensitive lifecycle work:

- approve or unapprove
- enable or disable
- grant or remove sysadmin
- reset password
- assign or remove project roles
- apply one role across all projects

Important guardrails enforced by the API:

- an admin cannot disable or unapprove themselves
- an admin cannot remove their own sysadmin status
- the last active approved sysadmin cannot be removed
- the last project admin on a project cannot be removed

## Projects

`Projects` provides global project lifecycle administration:

- search the complete project catalog
- inspect run, membership, and storage counts
- review and change memberships through the existing user workflow
- rename a project while preserving its id and related data
- delete a project only after typing its exact name

Project deletion is irreversible. Both the UI and API require the exact current project name; the DELETE request body is `{"confirm_name":"Exact project name"}`. The API deletes related database rows transactionally, then removes associated raw artifacts. Its response reports artifact cleanup failures so operators can resolve residual files.

## API Tokens

`Tokens` is the global API token admin page.

Main workflows:

- search the full token inventory
- create a token for an approved project member
- adjust name, role, scopes, and expiry
- rotate a token secret
- revoke a token

The UI shows secrets only at create or rotate time through a dedicated reveal component.
Never-expiring token issuance is disabled by default and requires explicit configuration.

## Audit Log

`Audit` is the global event stream for the whole system.

It supports:

- text search
- cursor-based pagination
- CSV export
- JSON export

## Related APIs

The settings area depends mostly on these routes:

- `GET /auth/security-settings`
- `GET /users`
- `POST /users`
- `GET /users/{user_id}`
- `PATCH /users/{user_id}`
- `GET /settings/overview`
- `GET /settings/projects`
- `GET /settings/projects/catalog`
- `GET /settings/projects/{project_id}`
- `PATCH /settings/projects/{project_id}`
- `DELETE /settings/projects/{project_id}`
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
