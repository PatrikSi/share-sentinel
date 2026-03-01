# Settings Section

## Purpose

Centralized enterprise administration for IAM, API tokens, and global audit governance.

## Navigation Items

- Overview
- IAM
- API Tokens
- Audit Logs

## Overview

Provides a quick controls matrix:

- implemented controls
- operational defaults
- enterprise gaps still to plan

## IAM

IAM consolidates identity lifecycle and project access control in one place.

### Identity Management

Primary workflows:

- create identity with:
  - approval state
  - system role (`System Admin` or `Standard User`)
  - optional add-to-all-projects assignment
- search and filter identity directory by:
  - email
  - active flag
  - approval flag
  - system role flag
- lifecycle operations:
  - enable / disable
  - approve / unapprove
  - promote / demote system admin
  - reset password

Safety controls:

- identity cannot disable self
- identity cannot unapprove self
- identity cannot remove own sysadmin role
- last active approved sysadmin cannot be removed

### Access Management

Project membership operations:

- upsert one membership (project, user, role)
- remove one membership
- apply all-project role policy for one identity

Project-safety controls:

- prevent demotion/removal of the last admin in a project

API calls:

- `GET /users`
- `POST /users`
- `PATCH /users/{id}`
- `POST /users/{id}/assign-all-projects`
- `GET /settings/projects`
- `GET /settings/rbac/project-memberships`
- `POST /settings/rbac/project-memberships`
- `DELETE /settings/rbac/project-memberships/{project_id}/{user_id}`
- `POST /settings/rbac/users/{user_id}/assign-all-projects`

## API Tokens

Enterprise token administration:

- global search and pagination
- create token for any eligible project member
- one-time secret display on create and rotate
- update token metadata:
  - name
  - role
  - scopes
  - expiry
- rotate token secret
- revoke token

Scope controls:

- scope catalog endpoint for allowed scopes and per-role defaults
- non-sysadmin token scopes are constrained to role defaults

API calls:

- `GET /settings/api-token-scopes`
- `GET /settings/api-tokens`
- `POST /settings/api-tokens`
- `PATCH /settings/api-tokens/{token_id}`
- `POST /settings/api-tokens/{token_id}/rotate`
- `DELETE /settings/api-tokens/{token_id}`

## Audit Logs

Global audit workflow:

- search by action/object/actor/project
- cursor pagination
- system-wide event timeline

API calls:

- `GET /settings/audit`

## Access Rules

- Settings routes require authenticated user context.
- Settings pages and APIs are sysadmin-only.
- API token access still requires matching scopes for read/write settings operations.
