# Settings Section

## Purpose

Centralized enterprise administration for users, RBAC, API tokens, and global audit visibility.

## Navigation Items

- Overview
- Users
- RBAC
- API Tokens
- Audit Logs

## Overview

Provides a quick controls matrix:

- Implemented controls
- Operational defaults
- Enterprise gaps still to plan

## Users

Primary workflows:

- Create user with:
  - approval state
  - sysadmin state
  - optional add-to-all-projects assignment
- Search and filter user directory by:
  - email
  - active flag
  - approval flag
  - sysadmin flag
- Lifecycle operations:
  - enable / disable
  - approve / unapprove
  - promote / demote sysadmin
  - reset password
- Bulk onboarding:
  - assign existing user to all projects
  - optional overwrite of existing memberships

Safety controls:

- User cannot disable self
- User cannot unapprove self
- User cannot remove own sysadmin role
- Last active approved sysadmin cannot be removed

API calls:

- `GET /users`
- `POST /users`
- `PATCH /users/{id}`
- `POST /users/{id}/assign-all-projects`

## RBAC

Project membership operations:

- Upsert one membership (project, user, role)
- Remove one membership
- Assign user role across all projects

Safety controls:

- Prevent demotion/removal of the last admin in a project

API calls:

- `GET /settings/projects`
- `GET /settings/rbac/project-memberships`
- `POST /settings/rbac/project-memberships`
- `DELETE /settings/rbac/project-memberships/{project_id}/{user_id}`
- `POST /settings/rbac/users/{user_id}/assign-all-projects`

## API Tokens

Enterprise token administration:

- Global search and pagination
- Create token for any eligible project member
- One-time secret display on create and rotate
- Update token metadata:
  - name
  - role
  - scopes
  - expiry
- Rotate token secret
- Revoke token

Scope controls:

- Scope catalog endpoint for allowed scopes and per-role defaults
- Non-sysadmin token scopes are constrained to role defaults

API calls:

- `GET /settings/api-token-scopes`
- `GET /settings/api-tokens`
- `POST /settings/api-tokens`
- `PATCH /settings/api-tokens/{token_id}`
- `POST /settings/api-tokens/{token_id}/rotate`
- `DELETE /settings/api-tokens/{token_id}`

## Audit Logs

Global audit workflow:

- Search by action/object/actor/project
- Cursor pagination
- System-wide event timeline

API calls:

- `GET /settings/audit`

## Access Rules

- Settings routes require authenticated user context.
- Settings pages and APIs are sysadmin-only.
- API token access still requires matching scopes for read/write settings operations.
