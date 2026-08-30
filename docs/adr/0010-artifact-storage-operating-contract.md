# ADR 0010: Treat shared artifact storage as durable capacity-managed state

- Status: Accepted
- Date: 2026-08-30

## Context

The reference deployment uses a shared POSIX filesystem rather than object storage. Accepted uploads, database references, worker recovery, and multiple replicas depend on that filesystem. A mere directory-exists check cannot detect exhausted, unwritable, or non-durable storage, while unbounded orphan cleanup is unsafe.

## Decision

Artifact keys are immutable and multipart identifiers are validated. Publication fsyncs the completed object, uses an atomic no-overwrite hard-link operation, and syncs its parent directory. Readiness reports configured absolute and percentage free-space thresholds; upload creation and every streamed part recheck that headroom. Deep health additionally performs a bounded write, fsync, no-overwrite hard-link, rename, directory-fsync, and cleanup probe.

Reconciliation is an operator-run, database-authoritative maintenance command. It defaults to dry-run, applies minimum-age and maximum-delete bounds, rechecks each reference immediately before deletion, skips symlinks, reports missing referenced objects, and audits requested, deleted, and skipped actions. Storage, queue, active-job age, and dependency collection state are exported as operational metrics.

## Consequences

- Low capacity becomes visible before an accepted upload fails midway.
- Concurrent publication cannot silently replace an immutable artifact.
- Cleanup is recoverable and reviewable, but operators must still schedule it and back up Postgres and artifact storage consistently.
- Multi-host deployments require shared storage with tested POSIX link, rename, fsync, permissions, and failure behavior.
