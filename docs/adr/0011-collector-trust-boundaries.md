# ADR 0011: Bind collector evidence to explicit provider and credential boundaries

- Status: Accepted
- Date: 2026-08-30

## Context

SharePoint national clouds use different authority, Graph, and SharePoint hosts. DFS can redirect an authenticated SMB client to another server, while NFS protocol reachability says nothing about export authorization. Treating any of these observations as interchangeable would leak credentials or publish false access conclusions.

## Decision

SharePoint collection selects one supported cloud profile that binds the login authority, token audience, Graph host, and SharePoint hostname suffix. Tokens and discovered URLs are validated against that profile, and local delta/snapshot state is partitioned by cloud. Certificate authentication accepts only a bounded regular PEM file, rejects symlink traversal, and applies owner/mode checks where the platform supports them. Secrets, assertions, certificate material, opaque tokens, and delta links never enter artifacts or normal logs.

The SMB collector detects a DFS share through bounded protocol evidence and records that the namespace may redirect. It does not enumerate or follow referral targets and never forwards the supplied credentials to a discovered target. Physical target coverage therefore remains unresolved.

NFS collection separates transport, RPC, export discovery, namespace enumeration, and authentication evidence. A bounded NFSv4 NULL response proves protocol availability only. `showmount` results can describe NFSv3-style export discovery, but neither signal is converted into successful authentication or file access without an actual bounded namespace observation.

Permission completeness requires authoritative structural completion. Directory/group membership expansion is deferred until a collector can obtain the additional provider permissions and publish complete, provenance-backed, cycle-safe membership evidence; partial name matching is not accepted.

## Consequences

- Sovereign-cloud tokens and URLs cannot silently cross into a different Graph or SharePoint environment.
- DFS coverage is honest but logical-only; operators needing physical-target assessment must schedule those targets explicitly.
- NFSv4-only hosts are visible without being mislabeled as accessible exports.
- A SharePoint state-schema upgrade partitions cloud state and forces one bounded metadata backfill before incremental collection resumes.
- Group-derived effective access remains unknown rather than requiring silently broader Graph consent.
