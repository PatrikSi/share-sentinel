# Auth and RBAC

This is the current access model used by the API and the UI.

## How authentication is resolved

Requests are checked in this order:

1. bearer access token
2. cookie session token
3. hashed API token

Cookie-authenticated unsafe methods also go through CSRF checks when CSRF protection is enabled.

## User state gates

For user-based access, the account must be both:

- active
- approved

That means self-registered users can exist in the system before they are allowed to sign in.

## Registration and password policy

Self-registration is optional and controlled by configuration.

Password rules are also configuration-driven and are enforced in the same way across:

- self-registration
- admin-created users
- password changes
- seeded admin bootstrap

If the seeded admin password does not meet the configured policy, bootstrap fails with a clear error instead of continuing with a broken setup.

## Project roles

Project access is based on three roles:

- `viewer`
- `operator`
- `admin`

In practice:

- `viewer` can read dashboard, inventory, and run data
- `operator` can create runs and upload artifacts
- `admin` can manage project membership and create self-service project tokens

## Sysadmin role

Sysadmin is separate from project roles. It unlocks the global settings surface and user administration APIs.

Typical sysadmin-only actions:

- user lifecycle management
- approvals
- global token administration
- global audit access
- cross-project membership management
- project creation

## API token scopes

API tokens are project-scoped and scope-checked.

Important rules:

- token scopes must satisfy the endpoint's required scopes
- token role cannot exceed the owner's project role
- non-sysadmin scope choices are constrained to the defaults allowed for that project role
- token creation, rotation, and revocation are all audited

## Safety rails

The API blocks several easy-to-make lockout mistakes.

### User admin safety

- you cannot disable your own account
- you cannot unapprove your own account
- you cannot remove your own sysadmin access
- the last active approved sysadmin cannot be removed or demoted

### Project admin safety

- the last project admin cannot be removed or demoted

### Token safety

- self-service token creation requires a real user login
- revoked tokens cannot be updated or rotated
- token role cannot exceed the owner's current membership role

### Session safety

- password changes revoke active refresh sessions
- disabling or unapproving a user revokes active refresh sessions

## Rate limiting and guardrails

There are a few extra protections around the auth surface:

- login throttling with lockout
- rate limits on login, refresh, registration, token creation, and artifact upload
- request IDs attached to responses for tracing

## Where the UI surfaces this

- the login page reads `/auth/registration-settings`
- the settings overview reads `/auth/security-settings`
- the `Access` settings page reflects approval and role guardrails through dialogs and server error handling
- the `Tokens` settings page exposes role and scope rules through the global token workflow
