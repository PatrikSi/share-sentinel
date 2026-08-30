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

Sysadmin-only dependency check for Postgres, Redis, and artifact storage. In
addition to the normal capacity/access check, it performs a bounded
create/write/fsync, no-overwrite hard-link, rename, directory-fsync, and cleanup
probe against the artifact volume.

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

Project-scoped audit log. Project admin required. Actor IDs and retained email/API-token labels remain available when a referenced user or token no longer exists. Labels on events created after migration `0016` are event-time snapshots; a live-parent legacy row backfilled by `0018` carries the label observed at upgrade time.

## Runs and ingest

### `POST /projects/{project_id}/runs`

Creates a run record. Requires `operator` or `admin`.

### `GET /projects/{project_id}/runs`

Lists runs for a project with keyset pagination. Optional `q`, `status`, and `source_id` filters support recurring-source investigations without loading an unbounded run catalog.

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

### `GET /projects/{project_id}/runs/{run_id}/resources/{resource_id}/access-evidence`

Returns normalized provider permission assessments, bounded capability observations, coverage, limitations, errors, principal identity, and run provenance for one resource. It requires project viewer access plus `read:runs` and `read:inventory` token scopes.

`assessment_limit`/`after_assessment_id` and `entry_limit`/`after_entry_id` form a two-level continuation contract: exhaust `next_entry_id` for the current assessment page before advancing `next_assessment_id`. An authoritative empty assessment, a response page with no entries, and an unassessed resource are separate states.

### `GET /projects/{project_id}/runs/{run_id}/resources/{resource_id}/effective-access`

Returns a conservative explanation of access evidence rather than an inferred directory entitlement. `principal_id` can select one observed principal; otherwise `limit` and `cursor` page through principals. Response planes keep direct provider entries, non-mutating capability observations for the collector's assessed identity, and any provider-computed result separate.

The resource-wide `decision` remains `unknown` unless the unfiltered response has exactly one complete, untruncated resource assessment that explicitly declares effective-access semantics, complete principal resolution, no omitted/unknown entries, and a recognized provider-computed decision. A provider denial additionally requires explicit negative-conclusion support. The response also returns page-scoped direct decisions, per-principal limitations and bounded entries, truncation flags, collection context, and the identity to which capability observations apply. Missing group expansion, unresolved principals, incomplete provider retrieval, and non-effective ACL semantics are explicit limitations, not denials.

### `GET /projects/{project_id}/runs/{run_id}/search/items`

Searches items within a run.

## Materialized comparisons

### `POST /projects/{project_id}/comparisons`

Creates or returns the idempotent materialized comparison for a complete baseline/current run pair and the active algorithm/options. Requires project operator or admin access plus `write:runs` and `read:inventory`. Creation is rate limited and active comparisons are capped per project. A failed comparison remains failed until the explicit retry endpoint is used; a completed or active comparison is returned unchanged.

### `GET /projects/{project_id}/comparisons`

Lists comparison history in stable keyset order. Optional `state`, `source_id`, `current_run_id`, and `baseline_run_id` filters support monitoring timelines and run-specific drill-down.

### `GET /projects/{project_id}/comparisons/{comparison_id}`

Returns queued/running progress, delayed retry time, dimension-specific compatibility, terminal summary, or a bounded public error. Requires project viewer access plus both `read:runs` and `read:inventory`.

### `POST /projects/{project_id}/comparisons/{comparison_id}/retry`

Explicitly resets a failed comparison for a fresh operator-authorized attempt. Active work is returned idempotently before mutation admission. A failed retry consumes the same rate-limit budget and per-project active-comparison capacity as creation; capacity exhaustion returns structured `429` detail with `Retry-After`. Mutation is serialized with the worker, source automation, project admission lane, and comparison row so concurrent retries cannot create duplicate work; a completed result returns `409`. Worker crash recovery, by contrast, preserves durable phase/cursor progress and does not erase already materialized rows.

### `GET /projects/{project_id}/comparisons/{comparison_id}/resource-changes`

Returns a complete comparison's materialized resource changes using stable keyset pagination. Filters include `change_type`, `provider`, `category`, and `q`. Rows separate structural, access, and content states and include match provenance plus before/after snapshots. `appeared`, `disappeared`, `changed`, and `indeterminate` are distinct; a missing resource is definitive only when structural collection scope is comparable. The endpoint returns `409` until work completes. It requires project viewer access plus both `read:runs` and `read:inventory`.

The summary's `resource_summary_exact` applies to the published resource-level evidence scope. Item counts on each resource state whether history was computed and exact; null means not computed, never zero.

### `GET /projects/{project_id}/comparisons/{comparison_id}/item-changes`

Returns durable item-level changes after a comparison completes. Filters include `change_type`, `resource_change_id`, and `q`; `limit`/`cursor` use stable keyset pagination. Change types distinguish additions, removals, moves/renames, metadata changes, permission-evidence changes, and indeterminate correlations. Each row contains bounded before/after snapshots (including collected URL and file-attribute metadata), match basis/quality, evidence state, limitations, and impact rank. A definitive permission change requires comparable non-null permission-quality hashes; evidence-shape drift without that contract is indeterminate. The endpoint returns `409` until the complete result is published and writes a bounded read-audit event.

## Continuous monitoring and findings

Collection sources are registered from normalized, non-secret run context. A source identity includes provider, target scope, tenant/perspective, and assessed identity where applicable; changing credentials or semantic scope must not silently reuse an incompatible baseline.

### `GET /projects/{project_id}/sources`

Lists sources with `provider`, `health_status`, `q`, `limit`, and `cursor` filters. A single provider component such as `smb` or `nfs` includes mixed sources such as `nfs+smb`; pass the full compound value to match only that collector scope. Health combines enabled state, last success/failure, expected interval, current age, and collection coverage. Staleness begins only after the larger of 15 minutes or twice the configured interval.

### `GET /projects/{project_id}/sources/{source_id}`

Returns one source, including last observed/success/failure runs, freshness, coverage, and automation state.

### `PATCH /projects/{project_id}/sources/{source_id}`

Project-admin operation for `display_name`, `enabled`, and nullable `expected_interval_seconds` (300 through 31,536,000). Every request supplies the configuration it was edited from as `expected_display_name`, `expected_enabled`, and `expected_current_interval_seconds`, then includes only mutable fields that changed. A concurrent configuration change returns structured `409 SOURCE_REVISION_CONFLICT`; clients must reload and let the operator reconcile the draft. Disabling a source does not reject later observations; it skips new automatic baseline comparison and finding-policy evaluation while leaving manual comparisons available. A work unit already claimed by a worker can finish, so use its audit/coverage state to verify the boundary after changing the switch. Credentials and Graph/SMB/NFS connection configuration are not accepted here.

### `POST /projects/{project_id}/runs/{run_id}/monitoring/retry`

Operator/admin recovery for the newest complete monitored run when its built-in finding evaluation reached a terminal `degraded` state. Active/queued evaluation is returned idempotently. A disabled source, superseded run, or non-retryable state returns structured `409` detail; rate limiting returns structured `429` detail and `Retry-After`. It requires `write:findings`.

### `POST /projects/{project_id}/comparisons/{comparison_id}/findings/retry`

Operator/admin recovery for finding evaluation attached to a complete comparison when the nested `findings_evaluation.state` is terminal `degraded`. This does not reset or recompute the comparison itself. Active/queued evaluation is returned idempotently; non-retryable state returns structured `409`, and rate limiting returns structured `429` with `Retry-After`. It requires `write:findings`.

### `GET /projects/{project_id}/finding-policies`

Returns the versioned built-in policy catalog. The current rules cover SharePoint anonymous links, broad internal grants, observed SMB write capability, resource appearance/disappearance, permission-evidence change, and comparison indeterminacy. Policy evidence states and limitations remain part of every result.

### `GET /projects/{project_id}/findings`

Lists the deduplicated analyst queue with `status`, `severity`, `policy_id`, `source_id`, `q`, `limit`, and `cursor`. The response includes status counts for the same non-status filter scope. Findings reopen when authoritative evidence observes the condition again; incomplete collection cannot silently resolve a prior finding.

### `GET /projects/{project_id}/findings/{finding_id}`

Returns current lifecycle state, bounded evidence snapshot, source/resource/run/comparison references, assignee, first/last seen timestamps, occurrence count, accepted-risk expiry, and an optimistic `revision`.

### `PATCH /projects/{project_id}/findings/{finding_id}`

Operator mutation for lifecycle status, project-member assignment, accepted-risk expiry, and an optional audit note. The caller must send the last observed `revision`; stale writes return structured `409 FINDING_REVISION_CONFLICT` detail. `accepted_risk` requires a future timezone-aware expiry, and the worker reopens expired risks with an audit event.

### `POST /projects/{project_id}/findings/bulk`

Atomically updates at most 100 finding IDs after locking them in stable order. The request supplies the expected revision for every ID; a missing, cross-project, or stale finding rejects the complete request, with bounded structured conflict detail for stale revisions. The server writes a shared batch event plus a bounded per-finding audit event so each finding's activity stream remains accountable.

### `GET /projects/{project_id}/findings/assignee-candidates`

Returns up to 100 active, approved project members for the operator assignment picker, optionally filtered by email.

### `GET /projects/{project_id}/findings/{finding_id}/occurrences`

Keyset-paginated immutable observations showing the run/comparison, policy version, evidence state, bounded evidence, and observation time.

### `GET /projects/{project_id}/findings/{finding_id}/activity`

Keyset-paginated finding audit activity. This is distinct from occurrence evidence: occurrences record what the scanner observed; activity records automated and human workflow transitions.

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

The shared `query_dsl` supports `item_type=file` and `item_type=directory` in addition to provider/resource fields. Resource and endpoint queries interpret item-type clauses through matching child items. Quoted values use doubled quote marks for a literal quote, for example `search~"Bob's ""quarterly"" report"`; backslashes remain literal.

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

Current SMB artifacts additionally preserve bounded evidence fields such as
`reason_code`, `protocol_status`, `not_tested_reason`, `method`, and `scope`.
The `_metadata` object exposes `assessment_summary`, `assessment_reason`,
`share_presence`, `finalized`, `degraded`, and `transport_failed`. Consumers
should prefer the assessment summary for display while retaining
`access_level` for compatibility. `finalized` means the final assessment record
was emitted; it does not claim exhaustive coverage or a healthy dependency.

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

### `GET /projects/{project_id}/inventory/export.csv`

Exports the filtered inventory scope as CSV. Supported parameters are:

- `tab` (`items`, `resources`, or `endpoints`)
- `query_dsl`
- `run_ids`
- `include_deleted` for item exports

The export uses the same project/run and query-DSL semantics as the paginated inventory views and streams matching rows in descending keyset batches. It has no fixed row-count ceiling and keeps API and browser memory bounded independently of the total result count. Text fields that spreadsheet software could interpret as formulas are neutralized before CSV encoding.

Consistency is explicitly `high-watermark-bounded-live-non-snapshot`: the first ordered page captures the highest matching row id, so later inserts are excluded, while updates, deletions, and `INGESTING` run-status changes can still affect pages fetched later. Responses expose this contract and frontier in `X-Share-Sentinel-Export-Consistency` and `X-Share-Sentinel-Export-High-Watermark`. A completed audit event means the server exhausted that live, high-watermark-bounded scope; it does not claim a point-in-time database snapshot or prove that a client durably saved every byte. Because a large export is streamed, a transport or database failure after response headers have been sent appears as an incomplete download and is recorded as failed or cancelled rather than being rewritten as a JSON error response.

Export starts are subject to the configured Redis rate limit (`429`) and per-process concurrent-export capacity (`503`). Both rejection modes include `Retry-After` and occur before streaming headers are sent.

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

Lists the global audit stream. Results expose retained `actor_user_id`, `actor_email`, `actor_token_id`, `actor_token_name`, `project_id`, and `project_name` attribution. The IDs are immutable audit references and can remain populated after the corresponding parent row is deleted.

### `GET /settings/audit/export`

Exports the filtered global audit stream as bounded-memory streaming CSV or JSON, including retained actor-token and project attribution. `max_rows` defaults to 5,000 and is capped at 20,000. The route captures an immutable audit-ID high-watermark before recording its own request event, then keyset-reads rows no newer than that watermark; it is stable against concurrent inserts but is not a database-wide repeatable-read snapshot. `X-Export-Row-Count`, `X-Export-Row-Limit`, `X-Export-Truncated`, and `X-Export-Snapshot-ID` describe the planned result. The administration UI reads those headers and displays a post-download warning when more matching events existed than the file could contain. CSV cells are neutralized against spreadsheet formula execution.

The request is audited as `SETTINGS_AUDIT_EXPORT_REQUESTED`; a fully consumed stream records `SETTINGS_AUDIT_EXPORTED`, while generator failure or disconnect records `SETTINGS_AUDIT_EXPORT_FAILED` or `SETTINGS_AUDIT_EXPORT_INTERRUPTED` on a best-effort retained-attribution path. A dependency failure after response headers produces an incomplete file rather than a second HTTP error, so consumers must validate JSON closure or the expected CSV row count. Responses are non-cacheable and proxy buffering is disabled. A larger historical export should be divided with selective project/search scope or taken from the operator's database/log archival pipeline.

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
- Audit events are written for changes and sensitive evidence reads. Metadata is recursively redacted and bounded by depth, field count, and serialized size before storage. The application does not silently promise an audit-retention period; operators must set retention, backup, and export policy for their deployment.
- Inventory and run listings use keyset pagination rather than offset pagination.
