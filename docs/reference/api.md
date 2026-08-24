# API reference

This is a practical map of the API surface exposed by the current application. It is grouped by workflow so it lines up with the UI and collector behavior.

## Health and diagnostics

### `GET /healthz`

Lightweight liveness check.

### `GET /healthz/ready`

Unauthenticated load-balancer readiness check. It returns only generic Postgres,
Redis, and artifact-storage status and responds with `503` while any required
dependency is unavailable.

### `GET /healthz/deep`

Sysadmin-only readiness check for Postgres and Redis.

### `GET /metrics`

Sysadmin-only Prometheus output for API HTTP metrics. This route is intentionally excluded from OpenAPI.

## Authentication and session management

### `GET /auth/registration-settings`

Returns whether self-registration is enabled plus the active password policy.

### `GET /auth/security-settings`

Sysadmin-only snapshot of security settings used by the settings overview page.

Important note:

- includes the default API token expiry and whether never-expiring token issuance is enabled

### `POST /auth/register`

Creates a new unapproved user when self-registration is enabled.

### `POST /auth/login`

Starts a browser session by setting access, refresh, and CSRF cookies, then returns the current user payload.

### `POST /auth/refresh`

Rotates the refresh cookie and renews the access cookie. Browser clients normally use the refresh cookie; request-body refresh tokens are compatibility input only.

Important note:

- refresh tokens are single-use and replay is rejected after rotation

### `POST /auth/logout`

Clears auth cookies and revokes the active browser session when a refresh session is present.

### `POST /auth/logout-all`

Revokes all active browser sessions for the current user.

Important note:

- this route requires a browser session user and does not accept API-token-only auth

### `GET /auth/me`

Returns the current authenticated user.

### `PATCH /auth/me/theme`

Updates the current user's UI theme.

Important note:

- this route requires a browser session user and does not accept API-token-only auth

### `POST /auth/change-password`

Changes the current user's password and invalidates active browser sessions.

Important note:

- this route requires a browser session user and does not accept API-token-only auth

## Self-service API tokens

These routes are for a signed-in user managing their own project token inventory.

### `POST /auth/api-tokens`

Creates a project-scoped token for the current user.

Rules:

- user login is required
- current user must be `admin` on the target project
- token role cannot exceed the user's project role
- omitted `expires_in_days` falls back to the configured default expiry

## Browser auth request notes

- Unsafe requests authenticated by session cookie require a matching CSRF header.
- Bearer API tokens do not use the CSRF requirement.

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

Returns one run, including stored artifact size, content type, SHA-256 provenance, and bounded non-secret `collection_context` when an upload exists. SharePoint context distinguishes application tenant inventory from a delegated user's security-trimmed view and records completeness/snapshot semantics.

### `DELETE /projects/{project_id}/runs/{run_id}`

Deletes a run and its stored artifact. Project admin only.

### `POST /projects/{project_id}/runs/{run_id}/artifact`

Uploads the artifact for a run. The API accepts JSON, NDJSON, JSONL, and gzip variants, validates headers and basic payload structure, stores the raw artifact, and tries to queue ingestion.

Important note:

- first-party clients should stream a raw body with `X-Artifact-Filename` carrying the exact basename/suffix and use `application/json`, `application/x-ndjson`, or `application/gzip` as appropriate; multipart remains a compatibility input
- use `.ndjson`, `.jsonl`, or their gzip variants for large collections; compact JSON is a bounded compatibility format
- the API releases its preflight database transaction during the body transfer, then locks and rechecks the run before committing the immutable artifact pointer
- a successful upload response can still return `queued: false`
- when that happens, the worker will discover the run through its recovery path instead of the primary Redis stream handoff
- successful responses include the stored artifact SHA-256; clients resolving an ambiguous retry should compare it with `GET` run metadata before declaring success

### `GET /projects/{project_id}/runs/{run_id}/diff`

Compares a run with a baseline run. If no `baseline_run_id` is supplied, the API uses the nearest earlier complete run when possible.

Bounds:

- both current and baseline runs must be `COMPLETE`; partial or failed inputs return `409`
- `detail_limit` defaults to 500 and is capped at 2000 records per new-share, disappeared-share, and item-churn section
- aggregate summary counts remain exact and `truncation` identifies any bounded detail sections
- comparisons above `API_RUN_DIFF_MAX_ITEMS` total items across both runs return `422`; the default is 250000
- provider-backed items use stable IDs to report path changes in `moved_items` / `moved_examples` rather than double-counting them as removal plus addition
- a resource whose observed `access_level` changed is included in `item_churn` even when its item inventory did not change; `access_level_changed` and `previous_access_level` distinguish that transition from content churn
- `comparison_compatibility` warns when source, tenant, authentication perspective, assessed identity, discovery completeness, or materialization semantics make the runs unsafe to interpret as equivalent coverage

### `GET /projects/{project_id}/runs/{run_id}/errors`

Lists recorded ingest warnings and errors for a run with search, severity filtering, and keyset pagination.

### `GET /projects/{project_id}/runs/{run_id}/activity`

Lists run-scoped audit activity such as creation, upload, and ingest lifecycle events.

### `GET /projects/{project_id}/runs/{run_id}/endpoints`

Lists endpoints discovered in a run.

### `GET /projects/{project_id}/runs/{run_id}/endpoints/{endpoint_id}/resources`

Lists shares for one endpoint within the run with `limit`, opaque `cursor`, and `next_cursor` keyset pagination.

### `GET /projects/{project_id}/runs/{run_id}/resources/{resource_id}/items`

Lists items for one share within the run.

### `GET /projects/{project_id}/runs/{run_id}/search/items`

Searches items within a run.

## Inventory

Inventory routes work across runs in the current project.

Important note:

- inventory views can include data from `INGESTING` runs, so results may move until ingest completes
- explicit `run_ids` scopes accept at most 100 UUIDs; omit the parameter to query every eligible run in the project

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
- `provider`
- `resource_type`
- `exposure`
- `source`
- `run_ids`
- `is_dir`
- `include_deleted` (default `false`)
- `limit`
- `cursor`

Item rows include nullable `size_bytes`, `allocation_size_bytes`, ISO-8601 `mtime`, `created_at`, `accessed_at`, and `changed_at` values, plus a `file_attributes` array, when the source collector supplied usable metadata. Provider-backed rows can additionally include `provider`, stable item/parent IDs, an HTTPS `web_url`, MIME type, bounded provider metadata, deletion state, exposure classification, and evidence. For SMB artifacts produced by the bundled collector, `mtime` is the server's last-write time and `changed_at` is its metadata-change time.

Each item also inherits its resource's `access_level` and `access_capabilities`. Capability evidence is resource-level and sampled; it must not be interpreted as proof that the individual item row was tested.

Inventory pagination currently uses a stable server-defined order. Arbitrary column sorting is not part of the API contract yet; clients must not sort one fetched page and present it as a full-result sort.

### `GET /projects/{project_id}/inventory/resources`

Share-level inventory view. Supports:

- `q`
- `query_dsl`
- `endpoint`
- `access_level`
- `provider`
- `resource_type`
- `exposure`
- `source`
- `run_ids`
- `limit`
- `cursor`

Resource rows include the compatibility `access_level` (`unknown`, `no_access`, `list_only`, or `readable`) and an `access_capabilities` object. Known capabilities are `tree_connect`, `list`, `read_file`, `create_file`, `create_directory`, `modify_file`, `delete`, `write_acl`, and `write_owner`. Each contains a status (`allowed`, `denied`, `mixed`, `not_tested`, or `inconclusive`) and bounded evidence counts. A reserved `_metadata` object describes the non-mutating probe method and sample coverage. Its `complete` flag means a final per-share probe record was produced without cancellation, while `partial` describes limited or degraded coverage; both can truthfully be `true`.

SharePoint document-library rows use `resource_type=sharepoint_library`, retain a stable `provider_resource_id`, and carry separate exposure/evidence fields. Their compatibility `access_level` is conservative metadata-enumeration evidence; it is not proof that file content was opened or that a write would succeed.

### `GET /projects/{project_id}/inventory/endpoints`

Endpoint-level inventory view. Supports:

- `q`
- `query_dsl`
- `provider`
- `source`
- `endpoint`
- `run_ids`
- `limit`
- `cursor`

### `GET /projects/{project_id}/inventory/investigations`

Lists project-shared saved investigations.

### `POST /projects/{project_id}/inventory/investigations`

Creates a saved investigation definition.

Important note:

- create, update, and delete investigation routes require a browser session user
- API-token-only callers can read investigations but cannot mutate them

### `PATCH /projects/{project_id}/inventory/investigations/{investigation_id}`

Updates the name, description, target tab, query text, or saved definition for a project investigation.

Key fields:

- `name`
- `description`
- `target_tab`
- `query_text`
- `definition`

### `DELETE /projects/{project_id}/inventory/investigations/{investigation_id}`

Deletes a saved investigation.

## User administration

These routes back the sysadmin `Access` workflow.

### `GET /users`

Lists users with filters:

- `search`
- `include_pending_only`
- `project_id`
- `is_active`
- `is_approved`
- `is_sysadmin`
- `limit`
- `cursor`

Important note:

- `search` can match either user email or related project names

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

### `GET /users/{user_id}`

Returns one user for the per-user IAM detail page.

### `PATCH /users/{user_id}`

Updates email, password, approval state, active state, and sysadmin state in one request.

Important note:

- password reset, disable, and unapprove actions revoke active sessions

### `PATCH /users/{user_id}/status`

Enables or disables a user.

### `PATCH /users/{user_id}/approval`

Approves or unapproves a user.

### `POST /users/{user_id}/assign-all-projects`

Adds or updates the user across every project with one role.

Important note:

- this route supports `overwrite_existing`
- responses can include `partial` and `skipped_projects`

## System settings and governance

These routes back the sysadmin settings area.

### `GET /settings/overview`

Returns the live posture snapshot used by the settings overview page.

### `GET /settings/projects`

Returns all projects for global admin workflows.

### `GET /settings/projects/catalog`

Returns the searchable project catalog with aggregate counts for the project administration page.

### `GET /settings/projects/{project_id}`

Returns project detail, memberships, run counts, and artifact storage totals.

### `PATCH /settings/projects/{project_id}`

Renames a project. Project names remain unique.

### `DELETE /settings/projects/{project_id}`

Deletes a project only when the JSON body contains its exact current name:

```json
{"confirm_name": "Exact project name"}
```

The server enforces the confirmation independently of the UI. Related database state is removed transactionally; the response separately reports any raw artifact cleanup failures.

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

## Important behavior notes

- Password policy is driven by environment variables and enforced for registration, admin-created users, password changes, and seeded admin validation.
- Login and upload paths are rate-limited.
- Browser session tokens are invalidated on logout, logout-all, password change, admin password reset, disable, and unapprove.
- Audit events are written for both changes and many read operations.
- Inventory and run listings use keyset pagination rather than offset pagination.
