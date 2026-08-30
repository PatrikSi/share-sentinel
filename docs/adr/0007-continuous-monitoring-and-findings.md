# ADR 0007: Model continuous monitoring as credential-free sources and evidence-backed findings

- Status: Accepted
- Date: 2026-08-30

## Context

Uploaded runs previously had no durable identity linking recurring collections. Operators could compare runs manually, but could not tell whether a source was fresh, whether a compatible baseline existed, or which changes required action. Storing collector credentials in the server would create a substantially larger trust boundary and would not work for disconnected or externally scheduled collectors.

## Decision

The worker derives a project-scoped `collection_source` from canonical, credential-free collection context: provider, declared target scope, assessment perspective, and a non-secret assessed-identity reference. A source records freshness and collection coverage, but never authentication material.

After a complete ingest, enabled sources may create an idempotent comparison against the newest compatible complete run. Compatibility is conservative and includes provider, target, identity, collection mode, relevant enumeration settings, and evidence contracts. A comparison is not created when the worker cannot establish a compatible baseline.

Built-in, versioned policies turn explicit positive evidence and materialized comparison results into deduplicated findings. Findings retain immutable occurrences and mutable analyst lifecycle state. State-based findings are auto-resolved only after an authoritative replacement observation; partial, failed, or indeterminate collection never clears earlier evidence. Event findings remain available for analyst disposition.

Disabling monitoring stops new automatic comparison and policy evaluation work for that source while preserving ordinary ingest and manual comparison workflows. An already claimed bounded work unit may finish and remains visible in coverage/audit history. Scheduling and collector credentials remain outside the server and are owned by the operator's existing scheduler or automation platform.

## Consequences

- Recurring uploads become one observable source history without storing secrets.
- Source health can distinguish stale, degraded, partial, disabled, and never-collected states.
- Automatic comparisons and findings are explainable from stored run evidence.
- A changed target, identity, or collection contract can intentionally create a different source or prevent a misleading baseline.
- This release provides built-in policies rather than a general policy language. Custom policy authoring and server-side collector scheduling remain future work.
