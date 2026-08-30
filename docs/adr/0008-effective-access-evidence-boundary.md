# ADR 0008: Keep direct grants, observed capabilities, and effective access separate

- Status: Accepted
- Date: 2026-08-30

## Context

SMB ACLs and SharePoint sharing entries describe provider permission structures. Non-mutating SMB probes describe what one assessed identity could do at collection time. Neither source alone proves a principal's effective access after nested groups, inheritance, deny precedence, link expiration, conditional controls, or object-specific ACLs are considered.

Presenting any of these planes as a single allow or deny verdict would create dangerous false certainty.

## Decision

The normalized evidence model and UI expose three distinct planes:

1. direct provider evidence, including principal, rights, effect, inheritance state, scope, and collection coverage;
2. observed capabilities for the explicitly assessed identity and bounded operation set;
3. computed effective access, populated only when the stored evidence explicitly supports that computation.

The analysis endpoint is a bounded explanation over stored evidence. It reports provenance, completeness, truncation, and limitations and preserves `unknown` whenever identity resolution, group membership, inheritance, semantics, or retrieval coverage is incomplete. Direct allow or deny entries are labeled as evidence, not as a computed decision.

Run-scoped membership expansion is not inferred from directory names or partial membership responses. A future membership input must carry provider identity, snapshot time, completeness, provenance, and cycle-safe transitive edges before it can participate in computation.

## Consequences

- Analysts can see who was named in collected permissions and why an access conclusion is or is not possible.
- The product does not silently turn missing evidence into denial or a group grant into access for every possible member.
- Effective-access computation remains intentionally conservative until collectors can supply complete, ordered, and provenance-bearing identity data.
