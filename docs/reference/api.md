# API Reference

## Auth

### `GET /auth/me`

Returns current authenticated user.

### `POST /auth/change-password`

Changes current user password.

### `POST /auth/api-tokens`

Creates project-scoped token for current user (requires project admin on target project).

### `GET /auth/api-tokens`

Lists current user's API tokens.

### `DELETE /auth/api-tokens/{token_id}`

Revokes one current-user token.

## Users

### `GET /users`

Admin user directory with filters:

- `search`
- `include_pending_only`
- `is_active`
- `is_approved`
- `is_sysadmin`
- `limit`
- `cursor`

### `POST /users`

Creates user.

Payload supports:

- `email`, `password`
- `is_active`, `is_sysadmin`, `is_approved`
- `add_to_all_projects`
- `all_projects_role`

### `PATCH /users/{user_id}`

Updates user profile and status fields.

### `PATCH /users/{user_id}/status`

Toggles active status.

### `PATCH /users/{user_id}/approval`

Updates approval state.

### `POST /users/{user_id}/assign-all-projects`

Assigns user to every project with selected role.

Payload:

- `role`
- `overwrite_existing`

## Settings

### `GET /settings/projects`

Returns all projects.

### `GET /settings/api-token-scopes`

Returns:

- `allowed_scopes`
- `defaults_by_role`

### `GET /settings/api-tokens`

Global API token inventory.

Query:

- `q`
- `limit`
- `cursor`

### `POST /settings/api-tokens`

Creates token for a specific user and project.

Rules:

- target user must be active and approved
- target user must already be a project member
- token role cannot exceed membership role

### `PATCH /settings/api-tokens/{token_id}`

Updates token metadata.

Payload:

- `name`
- `role`
- `scopes`
- `expires_in_days`
- `never_expires`

### `POST /settings/api-tokens/{token_id}/rotate`

Rotates token secret and returns the new secret once.

### `DELETE /settings/api-tokens/{token_id}`

Revokes token.

### `GET /settings/audit`

Global audit listing.

Query:

- `q`
- `limit`
- `cursor`

### `GET /settings/rbac/project-memberships`

Lists global memberships.

### `POST /settings/rbac/project-memberships`

Creates/updates one membership.

### `DELETE /settings/rbac/project-memberships/{project_id}/{user_id}`

Deletes one membership.

Safety:

- cannot remove last admin from a project

### `POST /settings/rbac/users/{user_id}/assign-all-projects`

Adds or updates memberships for one user across all projects.

Payload:

- `role`
- `overwrite_existing`
