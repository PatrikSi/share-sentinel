# Frontend reference

The UI is a single-page app built with React and Vite. It keeps most work centered around one active project so people can move between the dashboard, inventory, import flow, and run review without reselecting context on every page.

## Route map

- `/` login and optional self-registration
- `/projects` dashboard
- `/projects/:projectId/import` run creation and artifact upload
- `/projects/:projectId/inventory` project inventory
- `/projects/:projectId/runs/:runId` run explorer
- `/account` current-user account settings
- `/settings/general`
- `/settings/users`
- `/settings/users/:userId`
- `/settings/projects`
- `/settings/projects/:projectId`
- `/settings/tokens`
- `/settings/audit`

Legacy redirects that still exist:

- `/settings/overview` -> `/settings/general`
- `/settings/iam` -> `/settings/users`
- `/settings/rbac` -> `/settings/users`
- `/settings/api-tokens` -> `/settings/tokens`
- `/settings/audit-logs` -> `/settings/audit`
- `/admin` -> `/settings/users`

## Navigation model

- The compact top bar keeps Projects, authorized Settings, account, theme, and sign-out actions available.
- A condensed bottom navigation preserves the same primary destinations on narrow screens.
- Project context stays visible across `/projects/*`; project creation and switching live in the top bar.
- Switching projects preserves portable inventory filters but drops the old project's run IDs.
- Settings uses a dedicated sidebar with `General`, `Users`, `Projects`, `API Tokens`, and `Audit Log`.

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
- transfer progress and explicit upload cancellation
- timeout and network-failure messaging treats delivery as unknown and directs the operator to inspect the created run before retrying
- redirect into the run explorer after upload starts

## Inventory

Inventory is a dense, server-backed browsing workspace. Results remain in stable server order; the UI does not imply that a visible-page sort applies to the complete project.

Tabs:

- files and folders
- shares
- endpoints

Main UX patterns:

- a compact command bar with debounced search; press `/` outside an input to focus it
- progressive guided filters, run scope, and advanced query controls instead of a permanently expanded form
- active filter chips with individual removal and a clear-all action
- server-backed equals and exclude shortcuts on supported table values, plus copy-exact-value actions
- a free-text DSL editor with examples, validation, and apply/clear actions
- column pickers, compact/comfortable density, sticky headers, and optional Size and Modified columns
- explicit loading, no-match, request-error, and partial-ingest states; failed requests do not relabel old rows as current results
- complete filter, tab, query, and eligible run context in the URL for refreshes and shareable links

Only `COMPLETE` and `INGESTING` runs can scope inventory. `INGESTING` results are labelled partial, and unavailable run states are explained rather than producing silent empty results.

Project collaboration happens here through shared investigations:

- save the current inventory state as a project investigation
- update an existing shared investigation
- apply a saved investigation back into the page state
- delete no-longer-useful saved investigations

Saved investigations are project-scoped and opened from the command bar. Column and density preferences are browser-local.

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

Run artifact provenance includes content type, size, and a copyable SHA-256 when supplied by the API. Item views format collected size and modification timestamps when present.

## Settings

Settings is sysadmin-only and split into five sections.

### General

Shows live counts and posture data:

- registration state
- user counts
- token counts
- project count
- password policy
- login lockout settings
- token hygiene
- recent audit activity

### Users

The IAM workflow is two-step:

- the main `Access` page is a directory and creation surface
- the per-user detail page handles approvals, activation, sysadmin state, password resets, membership edits, and bulk all-project assignment

### Projects

Project administration supports search, project detail, membership review, rename, and guarded deletion. Deletion requires typing the project name and removes database state plus stored run artifacts.

### API Tokens

Global API token administration with:

- free-text search
- pagination
- create, update, rotate, and revoke flows
- one-time secret reveal on create or rotate

### Audit Log

Global audit search and export.

## Shared UI patterns

- `StatePanel` for loading, empty, and error states
- `StatusBanner` for inline workflow feedback
- `Dialog` for destructive or sensitive admin actions
- `SecretReveal` for one-time token display

API reads have finite request budgets that cover both headers and response bodies. A session-check outage is shown as retryable service failure rather than as a logout. Mutation timeouts and upload transport failures report an unknown outcome so operators inspect current state before retrying.

## Runtime configuration

The published Nginx image generates `/runtime-config.js` at container startup. The browser loads it before the Vite bundle, so the same image can use deployment-specific values without rebuilding:

- `VITE_API_BASE_URL` — relative API path, default `/api`
- `VITE_CSRF_COOKIE_NAME` — must match `AUTH_CSRF_COOKIE_NAME`
- `VITE_CSRF_HEADER_NAME` — must match `AUTH_CSRF_HEADER_NAME`

The startup hook accepts only a relative API path and conservative cookie/header characters. Invalid values stop the UI container before Nginx starts.
