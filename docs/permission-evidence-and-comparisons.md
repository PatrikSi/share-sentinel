# Permission evidence and run comparisons

Share Sentinel treats access as evidence, not as one optimistic label. A run can contain three related but independent dimensions:

- **direct permission evidence**: provider-declared ACL or sharing entries collected from a specific object
- **capability observations**: bounded operations that the collector was allowed, denied, or unable to assess with one session identity
- **exposure evidence**: positive evidence for anonymous, organization-wide, external, or assessed-user visibility

The legacy `access_level` remains for artifact and API compatibility. It is not an effective-permissions calculation and must not replace the normalized evidence model.

## Evidence contract

Collectors enabling direct permissions declare artifact schema version 2 and the `direct_permissions_v1` feature before emitting evidence records. Every assessment contains:

- a provider and versioned semantics identifier
- the assessed resource or item subject
- collection method and permission surface
- selection, retrieval, semantic, visibility, and principal-resolution coverage
- assessment state (`complete`, `partial`, `failed`, or `not_assessed`)
- observed, persisted, omitted, and unknown entry counts
- limitations and bounded error codes
- a consumer-derived evidence hash and, only for an exact entry set, an entry-set hash
- whether the evidence supports a negative conclusion for its declared scope

Permission entries retain the provider entry ID when available, normalized rights, effect, inheritance state, expiration, provider-specific facts, and a normalized principal. Principal display names and aliases are presentation data; stable identity is derived from provider, identifier namespace, canonical authority scope, native ID, and kind.

Principal display names, email addresses, login names, aliases, and native identifiers can be personal or security-sensitive directory data. Protect the database and backups accordingly and grant evidence access only to project viewers who need it. Evidence follows its source run/project retention and deletion lifecycle in this release; there is no independent field-level principal retention policy.

The worker owns stored identity keys and hashes. Producer-supplied keys and hashes are transport hints and cannot make two different normalized principals or entries compare equal. SharePoint Entra principals are tenant-scoped, while SharePoint site-user and site-group identifiers are site-scoped. SMB path identity preserves case because Samba-backed namespaces can be case-sensitive.

The worker reconciles each completed run after ingest. A permission assessment cannot claim an exact negative conclusion unless its state and coverage are complete, its declared and persisted entry counts agree, no entries were omitted or unknown, and its evidence hashes are present. Resource and item summaries use persisted entry counts rather than trusting producer totals. The worker also derives bounded resource-level evidence and quality fingerprints once at ingest; scalable comparisons read those fingerprints instead of rescanning every item assessment in a large library.

## Provider behavior

### SMB

`--smb-permissions root` retrieves the filesystem security descriptor for the share root with `READ_CONTROL`. It records owner, group, DACL state, ordered ACEs, access masks, inheritance flags, and parsing limitations under `smb_windows_acl_v1`.

This descriptor is not the server's separate SMB share-level ACL. It also does not expand domain groups or compute the effective rights of the scan identity. The ordinary non-mutating probes remain a separate evidence stream: they open existing handles with narrowly requested rights and never create, modify, or delete content. A denial is evidence about that attempted operation and sample, not proof that every object is inaccessible.

The capability metadata records the effective session kind and a non-secret identity fingerprint. Comparisons refuse to treat capability changes as equivalent when the effective session identity differs or is unknown.

### SharePoint Online

`--permissions library_roots` assesses document-library roots. `--permissions all_items` additionally assesses every selected materialized drive item. Calls are GET-only and bounded by object, HTTP-attempt, entry, and concurrency limits.

The collector normalizes Microsoft Graph permission objects, identities, link scopes, roles, expiration, and the limited inheritance information returned by Graph. Permission visibility remains caller-dependent. Group membership is not expanded and effective access is not computed. SharePoint document libraries commonly omit `inheritedFrom`, so missing inheritance data remains `unknown`.

Authentication, authorization, throttling, not-found/visibility, protocol, and budget failures are classified separately. A failure before the first permission page is `failed`; a failure after at least one page is `partial`; a run-wide circuit or exhausted budget prevents new requests and emits `not_assessed` summaries. Successful content enumeration is retained when permission assessment fails, but the run declares partial permission coverage.

Only positive anonymous or organization link evidence strengthens exposure. An empty, partial, denied, or caller-trimmed permission response never becomes `RESTRICTED` and never proves that no sharing exists.

## Access evidence UI

Open **Access evidence** from a resource row in project inventory or the run explorer. The side panel separates:

1. the overall evidence state and compatibility access label
2. direct provider permission assessments and principals
3. observed protocol capabilities and visibility
4. assessment coverage, limitations, errors, identity, and run provenance

Large evidence sets are paginated. Each response is bounded to 25 assessments and 100 permission entries; the client exhausts entry pages for the current assessment page before advancing the assessment cursor, merges entries by stable ID, and preserves already loaded evidence when a continuation request fails. “No rows shown,” “not assessed,” and an authoritative empty assessment are intentionally different states.

Reading detailed evidence requires project viewer access plus both `read:runs` and `read:inventory` token scopes. Evidence views are recorded in the project audit log.

## Materialized run comparisons

The bounded `GET .../runs/{run_id}/diff` endpoint remains useful for exact item-path previews below its configured item ceiling. The materialized comparison workflow is designed for larger resource inventories:

1. open a complete current run and choose a complete baseline on its **Diff** tab
2. start a scalable resource comparison
3. the API validates both runs, records dimension-specific compatibility, and queues a durable comparison row
4. a worker derives stable resource identities, processes resources in bounded batches, stores result rows, and heartbeats progress
5. the comparison workspace polls the job and then provides server-side filters and keyset pagination

Comparison creation requires project `operator` or `admin` access plus `write:runs` and `read:inventory`; project viewers need both `read:runs` and `read:inventory` to read an existing result. Repeating the same baseline/current/algorithm/options request is idempotent. A failed comparison can be explicitly submitted again, which resets its terminal error and requeues it. Redis handoff is an optimization: database recovery can claim a committed queued comparison if enqueueing fails.

### Identity and match quality

Stable provider resource IDs are preferred and scoped to provider and SharePoint tenant. Fallback identity uses provider, endpoint, resource type, and a provider-appropriate normalized name. SMB and SharePoint fallback names are case-insensitive; NFS export names preserve case. Ambiguous duplicate identities fail the job rather than pairing arbitrary resources.

SMB resource IDs incorporate the server identity observed during negotiation. Before publishing structural or content conclusions, the worker verifies that every case-normalized SMB endpoint has the same valid provider endpoint ID, identity source, and strength in both runs. A server-GUID/fallback transition, a changed GUID, missing identity provenance, or an ambiguous endpoint makes those dimensions indeterminate rather than producing false appeared/disappeared rows. Case-only SMB share or network-host spelling changes are not treated as moves.

A stable advertised-name or scan-target fallback supports a bounded comparison of observations at the same requested network location; it does **not** prove that the physical server behind that name or address is unchanged. The comparison UI labels this as **location-bound**, records `identity_scope_exact=false`, and does not include it in an exact resource-summary claim. Only stable server-GUID evidence in both runs is presented as strong server identity.

Result rows expose the match basis and quality:

- **strong**: a provider-backed stable resource identity
- **moderate**: a provider-specific fallback with endpoint context
- **weak**: a legacy fallback with limited provider identity

### Interpretation dimensions

The API compares collection contexts before work begins. Structural appearance or disappearance is definitive only when both runs represent the same known tenant/provider, authentication or assessed identity, requested scope, discovery coverage, and materialized snapshot semantics. Network host/CIDR scope and SMB include/exclude-share filters are validated; SharePoint target scope and discovery mode must be internally consistent. Content interpretation additionally requires file enumeration, complete content coverage, and equivalent depth/path/extension selection. SMB capability comparison also requires equivalent probe depth, entry, path, and sample limits. Contradictory or missing producer metadata fails only the affected dimension closed.

Collectors declare provider-bound comparison contracts for structural identity/inventory, content inventory, and—where implemented—SMB capability semantics. Both runs must declare the same contract version recognized for that provider. Missing, unknown, cross-provider, or changed contracts make the corresponding dimension indeterminate, allowing collector behavior to evolve without silently reinterpreting historical runs. Direct permission evidence remains independently versioned and gated by normalized assessment semantics and consumer-derived quality hashes. Capability comparison is currently applicable only to SMB-only runs; the UI presents it as **not applicable** for SharePoint rather than as a failed check.

A row can therefore be:

- `appeared` or `disappeared` when equivalent authoritative structural scope supports absence
- `changed` when comparable observations prove a structural or location change, a bounded capability or provider permission-evidence change, or an aggregate item-count change; this does not claim effective-access or item-level churn
- `indeterminate` when a possible difference exists but scope, identity, collection coverage, or evidence integrity cannot support a definitive claim

The result keeps structural, access, and content states separate. Equal failed/partial permission evidence is not silently treated as “no change”; it remains indeterminate. Location changes are not promoted to definitive moves when structural collection is incomparable.

Resource summary counts are exact only for the materialized resource result set and explicitly expose `resource_summary_exact`. Overall `exact` remains false because per-item churn is not computed. Item count snapshots are useful aggregate evidence, but null added/removed/moved values mean **not computed**, never zero.

## Failure and recovery behavior

- queued and stale-running comparisons are recoverable from Postgres
- workers claim a comparison under a PostgreSQL advisory lock
- result materialization is idempotent and an unfinished attempt does not publish a terminal complete state
- progress and heartbeats are persisted between batches
- retryable infrastructure errors return the job to recoverable work with bounded backoff; deterministic identity or evidence violations fail it with an operator-facing code and message
- replacing an artifact deletes every materialized comparison involving that run before its normalized rows are cleared, preventing stale results
- concurrent comparison admission is serialized per project and bounded by `API_COMPARISON_MAX_ACTIVE_PER_PROJECT`; create requests are rate limited

Comparison result rows can be large when many resources change. Monitor `run_comparisons` and `comparison_resource_changes` growth alongside ordinary inventory retention. The current release ties comparison lifetime to its project and source runs; deleting either cascades the comparison. It does not yet provide a separate comparison-retention policy or item-level materialization.

## API map

- `GET /projects/{project_id}/runs/{run_id}/resources/{resource_id}/access-evidence`
- `POST /projects/{project_id}/comparisons`
- `GET /projects/{project_id}/comparisons/{comparison_id}`
- `GET /projects/{project_id}/comparisons/{comparison_id}/resource-changes`

Resource changes accept `change_type`, `provider`, `category`, `q`, `limit`, and opaque `cursor` filters. A comparison returns `409` until it is complete. Clients should preserve the returned comparison ID and use exponential backoff while the state is `queued` or `running`.
