# Continuous monitoring and findings

Share Sentinel's monitoring workflow connects recurring uploads without turning the server into a credential store or remote scanner. Collection remains an external operation; the application identifies compatible evidence, measures freshness, materializes history, and gives analysts an accountable findings queue.

## Source identity

A source is registered from normalized, credential-free run context:

- provider or provider set
- declared target scope
- collection and authentication perspective
- SharePoint cloud/environment profile
- a non-secret assessed-identity reference or fingerprint

The source key is stable for equivalent context and isolated when the target or assessment perspective changes. Passwords, tokens, hashes, Kerberos material, certificate keys, and collector command secrets are not part of the source record.

The `Sources` workspace shows:

- latest successful and failed ingest observations
- declared collection coverage and its reasons
- expected cadence and current freshness
- last run and last automatic comparison
- collector version, target scope, and assessed identity

Setting an expected interval does not schedule a collector. Use cron, a systemd timer, CI, an orchestration platform, or another approved scheduler to run the collector and upload its output. The minimum interval is five minutes. A source becomes stale after the greater of 15 minutes or twice its expected interval.

Disabling automatic monitoring preserves ordinary uploads and manual comparisons. It stops new automatic baseline comparisons and built-in policy evaluation until re-enabled. A bounded work unit already claimed by a worker may finish; the source coverage and audit trail show that transition rather than pretending the toggle cancels in-flight database work.

## Automatic baselines

After a complete ingest, an enabled source looks through the newest 20 earlier complete source runs for a compatible baseline. Compatibility includes target and identity context plus the relevant structural, content, capability, and direct-permission collection contracts. A recent run with different depth, provider coverage, identity, tenant, target, or evidence semantics is not silently used as a baseline. A first run—or a window with no compatible candidate—records baseline coverage as unavailable/degraded; it is not treated as an automation success or a security finding.

Automatic comparison creation is idempotent. Postgres is authoritative if Redis delivery fails, and a queued or stale-running job is recoverable by a worker. Comparison results remain unavailable through the API until the job is complete.

## Built-in policies

The first monitoring policy set is deliberately small and evidence-driven:

| Policy | Trigger | Default severity | Evidence boundary |
| --- | --- | --- | --- |
| Anonymous SharePoint access | explicit anonymous link evidence | critical | positive Graph permission evidence only |
| Organization-wide SharePoint access | explicit organization link evidence | medium | positive Graph permission evidence only |
| SMB write capability observed | bounded, non-mutating authorization observations for create/modify/delete/ACL/owner operations | high | assessed identity and inspected operations only |
| Resource appeared | authoritative structural comparison | info | compatible complete scope |
| Resource disappeared | authoritative structural comparison | low | compatible complete scope |
| Permission evidence changed | comparable capability or direct-permission evidence changed | high | no effective-access claim |
| Comparison indeterminate | suspected change lacks compatible scope or evidence | low | intentionally non-definitive |

Policies are versioned in each finding occurrence. This release does not provide custom policy authoring, per-policy configuration, notifications, or remediation automation.

## Finding lifecycle

A finding has a stable deduplication identity and one or more immutable occurrences. Current title, severity, evidence, and latest run/comparison references follow the newest occurrence. Analyst lifecycle state is separate:

- `open`
- `acknowledged`
- `accepted_risk`, which requires a future expiry
- `resolved`

Assignment is limited to active, approved project members. Single and bulk updates carry the revisions loaded by the browser so stale state cannot silently overwrite another analyst; any conflict rejects the whole mutation with a structured reload-and-review response. Bulk updates are atomic, capped at 100 findings, and limited by the UI to the selected page. Every analyst transition, assignment, note, accepted-risk expiry, automatic reopen, observation, and automatic resolution is represented in the audit trail.

A newer authoritative occurrence reopens a resolved finding. A late historical comparison can add an occurrence for chronology without overwriting or reopening a decision based on newer evidence. Accepted risk remains in force until its expiry; an expired acceptance is reopened by bounded worker maintenance and audited. State-based findings are automatically resolved only when a replacement run has authoritative coverage for the relevant policy. A partial, failed, truncated, identity-incompatible, or indeterminate run never proves absence.

## Evidence interpretation

Finding evidence is labeled `exact`, `bounded`, or `indeterminate` within the declared collection scope. The detail pane preserves structured summaries, limitations, and immutable run/comparison references. It does not contact the live resource.

An empty queue can mean no policy matched the stored authoritative evidence; it is not a universal claim that the environment is secure. Review source coverage and freshness before relying on absence.

## API map

- `GET /projects/{project_id}/sources`
- `GET /projects/{project_id}/sources/{source_id}`
- `PATCH /projects/{project_id}/sources/{source_id}`
- `GET /projects/{project_id}/finding-policies`
- `GET /projects/{project_id}/findings`
- `GET /projects/{project_id}/findings/{finding_id}`
- `PATCH /projects/{project_id}/findings/{finding_id}`
- `POST /projects/{project_id}/findings/bulk`
- `GET /projects/{project_id}/findings/assignee-candidates`
- `GET /projects/{project_id}/findings/{finding_id}/occurrences`
- `GET /projects/{project_id}/findings/{finding_id}/activity`
- `GET /projects/{project_id}/comparisons`
- `POST /projects/{project_id}/comparisons/{comparison_id}/retry`
- `POST /projects/{project_id}/runs/{run_id}/monitoring/retry`
- `POST /projects/{project_id}/comparisons/{comparison_id}/findings/retry`

The ordinary comparison retry resets failed comparison computation, uses run/inventory scopes, and shares creation's rate-limit and per-project active-capacity budget. The two monitoring retry routes recover only terminally degraded policy evaluation and require `write:findings`; they do not erase successfully materialized comparison rows. All routes are project-scoped. Read routes require viewer access and their documented token scopes. Source configuration requires project admin and uses configuration-value preconditions so background freshness updates do not conflict with an operator draft; finding lifecycle changes and retries require operator or admin. See the [API reference](./reference/api.md) and [auth/RBAC reference](./reference/auth-rbac.md) for the exact contract.
