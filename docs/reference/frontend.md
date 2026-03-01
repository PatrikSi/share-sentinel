# Frontend Reference

## Route Map

- `/projects`
- `/projects/:projectId/inventory`
- `/projects/:projectId/import`
- `/projects/:projectId/runs/:runId`
- `/account`
- `/settings/overview`
- `/settings/users`
- `/settings/rbac`
- `/settings/api-tokens`
- `/settings/audit-logs`

`/admin` redirects to `/settings/overview`.

## Settings Layout

Settings is split into independent pages, not a monolithic admin screen:

- Overview: capabilities and enterprise-gap checklist
- Users: lifecycle and onboarding controls
- RBAC: project membership control and all-project assignment
- API Tokens: create/update/rotate/revoke with scope management
- Audit Logs: global governance timeline

## User Experience Controls

- Error/success banners for each settings workflow
- Cursor-based pagination controls for large datasets
- Search and filtering across users, RBAC, tokens, audit logs
- One-time display of token secrets after create/rotate
