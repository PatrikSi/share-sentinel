# Frontend reference

The UI is a single-page app built with React and Vite. It keeps most work centered around one active project so people can move between the dashboard, inventory, import flow, and run explorer without reselecting context on every page.

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
- The dashboard project picker and project creation flow live in the top nav instead of inside the page body.
- Settings uses a dedicated tab bar with `Overview`, `Access`, `Tokens`, and `Audit`.

## Login and account entry

The login screen is intentionally minimal again:

- sign in form first
- optional registration toggle when self-registration is enabled
- password policy hints only when registration is active

Successful login lands the user on the dashboard. Deep links are preserved through the `next` query parameter.

## Dashboard

The dashboard is meant to answer three questions quickly:

1. Which project am I in?
2. What is the newest run worth looking at?
3. Where should I go next?

Key pieces:

- project-scoped stat tiles
- latest run summary
- next-action card
- run queue with search, status filters, and paging
- top file-type chips

From there, users can jump directly into inventory, import, or the latest run.

## Import flow

The import page is a three-step workflow:

1. enter run details
2. attach and validate the artifact
3. create the run and upload

Current behavior:

- drag and drop upload area
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
- optional advanced query builder for the DSL
- collapsible run scope selector
- column picker for the result table

The point is to make the common filters easy without removing the more expressive search path.

## Run explorer

The run page is split into focused tabs instead of one long mixed screen:

- `Overview`
- `Diff`
- `Explore`
- `Search`

What each tab is for:

- `Overview` gives quick run context and a baseline summary
- `Diff` compares the run with a baseline run
- `Explore` walks endpoints -> shares -> items
- `Search` finds items inside the run without browsing the hierarchy

## Settings

Settings is sysadmin-only and split into four sections.

### Overview

Shows live counts and posture data:

- registration state
- user counts
- token counts
- password policy
- token hygiene
- recent audit activity

### Access

Combines user administration and project access management:

- approvals
- activation
- sysadmin role changes
- password resets
- per-project access
- bulk all-project assignment

### Tokens

Global API token administration with:

- search and filters
- create, update, rotate, revoke
- one-time secret reveal on create or rotate
- safer confirmation dialogs instead of browser prompts

### Audit

Global audit search and export.

## Shared UI patterns

- `StatePanel` for loading, empty, and error states
- `StatusBanner` for inline workflow feedback
- `Dialog` for destructive or sensitive admin actions
- `SecretReveal` for one-time token display

These components keep admin workflows more consistent than the older mix of ad hoc messages and browser prompts.
