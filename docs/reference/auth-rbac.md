# Auth and RBAC

## Identity Resolution

Request auth is resolved in this order:

1. Bearer JWT session token
2. Cookie session token (with CSRF checks for unsafe methods)
3. API token hash lookup

## User Status Gates

All authenticated operations require user to be:

- active
- approved

## Authorization Layers

### Sysadmin Gate

System administration routes require sysadmin user status.

### Token Scope Gate

Token-authenticated requests must satisfy required scopes.

### Project Role Gate

Project routes enforce minimum role:

- `viewer`
- `operator`
- `admin`

## User Management Safety

The platform blocks high-risk lockout patterns:

- self-disable
- self-unapprove
- self-sysadmin-removal
- removal of last active approved sysadmin

## Project Membership Safety

RBAC mutation routes prevent removal/demotion of the last `admin` member in a project.

## API Token Policy

### Creation Rules

- Token owner must be active and approved.
- Owner must be a member of target project.
- Token role cannot exceed owner's membership role.

### Scope Policy

- Non-sysadmin token scopes must match default scopes for selected token role.
- Sysadmin token owners can use full allowed scope catalog.

### Lifecycle Controls

- create
- update metadata
- rotate secret
- revoke
