# ADR 0009: Materialize item history asynchronously with stable identities

- Status: Accepted
- Date: 2026-08-30

## Context

Request-time diffs cannot safely compare millions of files, and path-only matching cannot distinguish a move or rename from removal plus addition. Recurring enterprise collections need durable, paginated history without loading complete inventories into API memory.

## Decision

Resource and item history are worker jobs whose durable state lives in Postgres. The worker processes ordered identity batches, commits results with progress checkpoints, and exposes rows only after the comparison reaches a terminal complete state. Recovery continues from committed checkpoints; an explicit operator retry may start a failed comparison again without allowing duplicate results.

Provider item IDs are strong identities. Consumer-derived path identities are a bounded fallback and are labeled weak. Strong matches can classify addition, removal, move, rename, metadata change, and comparable permission-evidence change. Ambiguous identities or incompatible content and permission scopes produce explicit limitations or indeterminate rows instead of guessed changes.

Results use keyset pagination and indexed filters. Summary counts state whether item history was computed and whether the result is exact, bounded, or indeterminate.

## Consequences

- Comparison work scales with bounded database batches rather than API process memory.
- SharePoint moves and renames can be distinguished when stable provider IDs are present.
- SMB or external artifacts that provide only paths retain useful history but cannot claim exact move semantics.
- Comparison-result retention must be planned alongside run retention because item history can approach inventory scale.
