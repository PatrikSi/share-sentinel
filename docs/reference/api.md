# API reference

This is a practical map of the API surface in `main`. It is grouped by workflow rather than by file so it is easier to line up with the UI and automation use cases.

## Health and diagnostics

### `GET /healthz`

Lightweight liveness check.

### `GET /healthz/deep`

Readiness-style check for Postgres and Redis.

### `GET /metrics`

Prometheus-compatible metrics output. This endpoint is not shown in the OpenAPI schema.

## Authentication and session management

### `GET /auth/registration-settings`

Returns whether self-registration is enabled and the active password policy. The login page uses this to decide whether to show the registration flow and which password hints to show.

### `GET /auth/security-settings`

Sysadmin-only snapshot of security-related settings used by the settings overview.

### `POST /auth/register`

Creates a new unapproved user when self-registration is enabled.

### `POST /auth/login`

Starts a browser session by setting auth, refresh, and CSRF cookies, then returns the current user payload.

### `POST /auth/refresh`

Rotates the refresh cookie and renews the access cookie. Browser clients use the refresh cookie; request-body refresh tokens are legacy compatibility input only.

### `POST /auth/logout`

Clears auth cookies and revokes the active refresh session when a refresh cookie or refresh token is present.

### `POST /auth/logout-all`

Revokes all active refresh tokens for the current user.

### `GET /auth/me`

Returns the current authenticated user.

### `PATCH /auth/me/theme`

Updates the current user's UI theme.

### `POST /auth/change-password`

Changes the current user's password and revokes active sessions.

## Self-service API tokens

These routes are for a signed-in user managing their own project token inventory.

### `POST /auth/api-tokens`

Creates a project-scoped token for the current user.

Rules:

- user login is required
- current user must be `admin` on the target project
- token role cannot exceed the user's project role

### `GET /auth/api-tokens`

Lists the current user's tokens.

### `DELETE /auth/api-tokens/{token_id}`

Revokes one of the current user's tokens.

## Projects and memberships

### `POST /projects`

Creates a project. Sysadmin only.

### `GET /projects`

Lists projects visible to the current user or token.

### `GET /projects/{project_id}`

Returns one project. Requires project access.

### `GET /projects/{project_id}/my-role`

Returns the resolved project role for the current user or token.

### `POST /projects/{project_id}/members`

Adds or updates a project member by user id. Project admin required.

### `POST /projects/{project_id}/members/by-email`

Adds or updates a project member by email. Project admin required.

### `GET /projects/{project_id}/members`

Lists project members. Project admin required.

### `DELETE /projects/{project_id}/members/{user_id}`

Removes a project member. Project admin required.

### `GET /projects/{project_id}/audit`

Project-scoped audit log. Project admin required.

## Runs and ingest

### `POST /projects/{project_id}/runs`

Creates a run record. Requires `operator` or `admin`.

### `GET /projects/{project_id}/runs`

Lists runs for a project with keyset pagination.

### `GET /projects/{project_id}/runs/{run_id}`

Returns one run.

### `DELETE /projects/{project_id}/runs/{run_id}`

Deletes a run. Project admin only.

### `POST /projects/{project_id}/runs/{run_id}/artifact`

Uploads the artifact for a run. The API accepts JSON, NDJSON, JSONL, and gzip variants, validates headers and payload signatures, stores the raw artifact, and queues ingestion.

### `GET /projects/{project_id}/runs/{run_id}/diff`

Compares a run with a baseline run. If no `baseline_run_id` is supplied, the API uses the nearest earlier complete run when possible.

### `GET /projects/{project_id}/runs/{run_id}/endpoints`

Lists endpoints discovered in a run.

### `GET /projects/{project_id}/runs/{run_id}/endpoints/{endpoint_id}/resources`

Lists shares for one endpoint within the run.

### `GET /projects/{project_id}/runs/{run_id}/resources/{resource_id}/items`

Lists items for one share within the run.

### `GET /projects/{project_id}/runs/{run_id}/search/items`

Searches items within a run.

## Inventory

Inventory routes work across runs in the current project. The UI uses them for guided filters, extension chips, and the optional query DSL.

### `GET /projects/{project_id}/inventory/stats`

Returns project-level counts for runs, endpoints, shares, files, directories, file types, and latest run time.

### `GET /projects/{project_id}/inventory/extensions`

Returns the most common file extensions in scope.

### `GET /projects/{project_id}/inventory/items`

Item-level inventory view. Supports:

- `q`
- `query_dsl`
- `ext`
- `endpoint`
- `share`
- `path_prefix`
- `run_ids`
- `is_dir`
- `limit`
- `cursor`

### `GET /projects/{project_id}/inventory/resources`

Share-level inventory view. Supports:

- `q`
- `query_dsl`
- `endpoint`
- `access_level`
- `run_ids`
- `limit`
- `cursor`

### `GET /projects/{project_id}/inventory/endpoints`

Endpoint-level inventory view. Supports:

- `q`
- `query_dsl`
- `endpoint`
- `run_ids`
- `limit`
- `cursor`

### `GET /projects/{project_id}/inventory/investigations`

Lists saved investigations for the project.

### `POST /projects/{project_id}/inventory/investigations`

Creates a saved investigation definition.

### `DELETE /projects/{project_id}/inventory/investigations/{investigation_id}`

Deletes a saved investigation.

## User administration

These routes back the `Access` settings area and are sysadmin-only.

### `GET /users`

Lists users with filters:

- `search`
- `include_pending_only`
- `is_active`
- `is_approved`
- `is_sysadmin`
- `limit`
- `cursor`

### `POST /users`

Creates a user.

Payload supports:

- `email`
- `password`
- `is_active`
- `is_sysadmin`
- `is_approved`
- `add_to_all_projects`
- `all_projects_role`

### `PATCH /users/{user_id}`

Updates email, password, and admin-controlled flags in one request.

### `PATCH /users/{user_id}/status`

Enables or disables a user.

### `PATCH /users/{user_id}/approval`

Approves or unapproves a user.

### `POST /users/{user_id}/assign-all-projects`

Adds or updates the user across every project with one role.

## System settings and governance

These routes back the sysadmin settings area.

### `GET /settings/projects`

Returns all projects for global admin workflows.

### `GET /settings/api-token-scopes`

Returns the allowed token scope catalog and the default scopes for each project role.

### `GET /settings/api-tokens`

Lists the global token inventory with search and keyset pagination.

### `POST /settings/api-tokens`

Creates a token for a specific user and project.

Rules:

- target user must be active and approved
- target user must already be a project member
- token role cannot exceed the user's project role

### `PATCH /settings/api-tokens/{token_id}`

Updates token metadata, role, scopes, and expiry settings.

### `POST /settings/api-tokens/{token_id}/rotate`

Rotates a token secret and returns the new secret once.

### `DELETE /settings/api-tokens/{token_id}`

Revokes a token.

### `GET /settings/audit`

Lists the global audit stream.

### `GET /settings/audit/export`

Exports the global audit stream as CSV or JSON.

### `GET /settings/rbac/project-memberships`

Lists global project membership entries.

### `POST /settings/rbac/project-memberships`

Creates or updates one membership entry.

### `DELETE /settings/rbac/project-memberships/{project_id}/{user_id}`

Removes one membership entry.

### `POST /settings/rbac/users/{user_id}/assign-all-projects`

Bulk-assigns a user across all projects.

## A few important behavior notes

- Password policy is driven by environment variables and enforced for registration, admin-created users, password changes, and seeded admin validation.
- Login and upload paths are rate-limited.
- Audit events are written for both changes and many read operations.
- Inventory and run listings use keyset pagination rather than offset pagination.
