# Frontend reference

The UI is a single-page app built with React and Vite. It keeps most work centered around one active project so people can move between the dashboard, inventory, import flow, and run review without reselecting context on every page.

## Route map

- `/` login and optional self-registration
- `/projects` dashboard
- `/projects/:projectId/import` run creation and artifact upload
- `/projects/:projectId/inventory` project inventory
- `/projects/:projectId/runs/:runId` run explorer
- `/account` current-user account settings
- `/settings/overview`
- `/settings/iam`
- `/settings/iam/users/:userId`
- `/settings/api-tokens`
- `/settings/audit-logs`

Legacy redirects that still exist:

- `/settings/users` -> `/settings/iam`
- `/settings/rbac` -> `/settings/iam`
- `/admin` -> `/settings/overview`

## Navigation model

- The brand in the top nav returns to the dashboard.
- Project context stays visible across `/projects/*`.
- Project creation and project switching live in the top nav.
- Settings uses a dedicated tab bar with `Overview`, `Access`, `Tokens`, and `Audit`.

## Login and account entry

- sign-in form first
- optional registration toggle when self-registration is enabled
- password policy hints shown during registration
- successful login lands on the dashboard
- deep links are preserved through the `next` query parameter

## Dashboard

The dashboard is the project landing page after login.

Key pieces:

- project-scoped stat tiles
- latest run summary
- next-action card
- run queue with search and status filters
- file-type chips
- quick links into inventory, import, run review, and issue review

## Import flow

The import page is a three-step workflow:

1. enter run details
2. attach and validate the artifact
3. create the run and upload

Current behavior:

- drag-and-drop upload area
- basic file preflight with detected type and size
- status messaging while upload and ingest begin
- redirect into the run explorer after upload starts

## Inventory

Inventory is designed to stay dense and practical.

Tabs:

- files and folders
- shares
- endpoints

Main UX patterns:

- compact guided filters first
- clear active filter state
- extension chips for the item view
- collapsible free-text DSL editor with examples and apply/clear actions
- collapsible run scope selector
- column picker for the result table

Project collaboration happens here through shared investigations:

- save the current inventory state as a project investigation
- update an existing shared investigation
- apply a saved investigation back into the page state
- delete no-longer-useful saved investigations

## Run explorer

The run page is split into focused tabs instead of one long mixed screen:

- `Overview`
- `Issues`
- `Diff`
- `Explore`
- `Search`

What each tab is for:

- `Overview` gives quick run context and baseline hints
- `Issues` reviews ingest warnings and errors and links back into search
- `Diff` compares the run with a baseline run
- `Explore` walks endpoints -> shares -> items
- `Search` finds items inside the run without browsing the hierarchy

Run-scoped saved search presets are browser-local and separate from project-shared inventory investigations.

## Settings

Settings is sysadmin-only and split into four sections.

### Overview

Shows live counts and posture data:

- registration state
- user counts
- token counts
- project count
- password policy
- login lockout settings
- token hygiene
- recent audit activity

### Access

The IAM workflow is two-step:

- the main `Access` page is a directory and creation surface
- the per-user detail page handles approvals, activation, sysadmin state, password resets, membership edits, and bulk all-project assignment

### Tokens

Global API token administration with:

- free-text search
- pagination
- create, update, rotate, and revoke flows
- one-time secret reveal on create or rotate

### Audit

Global audit search and export.

## Shared UI patterns

- `StatePanel` for loading, empty, and error states
- `StatusBanner` for inline workflow feedback
- `Dialog` for destructive or sensitive admin actions
- `SecretReveal` for one-time token display
