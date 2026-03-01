# Frontend Reference

## Route Map

- `/projects`
- `/projects/:projectId/inventory`
- `/projects/:projectId/import`
- `/projects/:projectId/runs/:runId`
- `/account`
- `/settings/overview`
- `/settings/iam`
- `/settings/api-tokens`
- `/settings/audit-logs`

Legacy compatibility redirects:

- `/settings/users` -> `/settings/iam`
- `/settings/rbac` -> `/settings/iam`
- `/admin` -> `/settings/overview`

## Settings Layout

Settings is split into enterprise sections:

- Overview: capability model and enterprise-gap checklist
- IAM: identities and project access policies in one place
- API Tokens: create/update/rotate/revoke with scope management
- Audit Logs: global governance timeline

## IAM UX Model

IAM page combines:

- identity role model (system role vs project role)
- identity lifecycle management
- project access assignment and all-project policy tools
- access directory for membership visibility and cleanup

## User Experience Controls

- Error/success banners for each settings workflow
- Cursor-based pagination controls for large datasets
- Search and filtering across identities, access entries, tokens, and audit logs
- One-time display of token secrets after create/rotate
