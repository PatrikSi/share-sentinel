# ADR 0012: Preserve audit attribution independently of mutable parents

- Status: Accepted
- Date: 2026-08-30

## Context

Audit events reference users, API tokens, and projects with foreign keys that use `ON DELETE SET NULL`. That protects deletion workflows from dangling foreign keys, but a live identifier and label alone are not durable forensic attribution: deleting a project or token can otherwise turn earlier activity into an unfilterable global event, and later renames can change how old activity is interpreted.

## Decision

Each new audit event stores immutable, non-foreign-key references for its actor user, actor API token, and project, plus bounded event-time snapshots of the user email, token name, and project name. No token secret, hash, collector credential, or session material is copied. The live foreign keys remain for relational integrity while the parent exists.

A database trigger applies the same contract to API and direct worker writes and prevents later updates from replacing an established attribution snapshot. Parent rename/deletion triggers preserve any still-legacy rows before a foreign key is cleared. Lookup indexes are built concurrently before those parent triggers are installed. Existing rows with live parents are migrated in restart-safe, autocommitted batches and receive the parent's label as it exists at upgrade time; a previous historical label is unknowable if the parent was renamed before the trigger existed. An event whose parent was already deleted before this schema existed cannot be reconstructed and remains explicitly unattributed.

Global and project audit APIs prefer the retained references and snapshots, so supported parent deletion does not silently erase filtering or actor identity. New rows expose event-time labels; legacy backfilled rows expose the explicitly documented upgrade-time label. Viewer-facing workflow timelines continue to expose only their reduced actor/metadata projection.

## Consequences

- Historical project, user, and exact API-token attribution survives normal rename and deletion workflows.
- Email addresses and administrative labels may intentionally outlive their source rows. Operators must include those snapshots in privacy, retention, backup, and deletion policy.
- The database audit table is accountable application state, not a tamper-evident or append-only/WORM log. High-assurance deployments should forward events to separately controlled immutable storage.
- Pre-upgrade orphaned attribution is not guessed from object IDs, display names, or metadata.
- Legacy rows cannot prove the label that existed when the event occurred; their backfilled label is the value observed during upgrade.
