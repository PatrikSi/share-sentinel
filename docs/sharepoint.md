# SharePoint Online collection

Share Sentinel can inventory SharePoint Online sites, document libraries, folders, and files through Microsoft Graph. Collection is metadata-only: the collector does not download file contents, execute documents, or request write permissions.

SharePoint collection supports two different assessment perspectives. Keep them separate when interpreting or comparing results:

- **Application tenant inventory** is intended for scheduled monitoring. It runs unattended with an Entra application and enumerates the sites available to that application's granted `Sites.Read.All` permission.
- **Delegated user view** is a quick, security-trimmed assessment of which metadata one signed-in identity can enumerate. It is useful for access reviews but is not an authoritative tenant inventory or a content-read test.

The default collector writes the schema-v1 NDJSON format used by the rest of Share Sentinel. Enabling direct permission evidence declares schema v2 and the `direct_permissions_v1` artifact feature in the first record. A SharePoint site is an endpoint, a document library is a `sharepoint_library` resource, and folders/files are items. Stable Graph site, drive, and driveItem IDs are retained separately from display paths so moves and renames remain correlatable.

## Install and run

From the repository:

```bash
python3.11 -m venv collector/.venv
source collector/.venv/bin/activate
pip install -r collector/requirements.txt
python collector/share_sentinel_sharepoint.py --help
```

Use `.ndjson` or `.ndjson.gz` for normal scans. Filenames and paths can themselves contain sensitive business information, so protect artifacts and the local state database as assessment data.

### Scheduled application scan

Create an Entra application with Microsoft Graph application permission `Sites.Read.All` and grant tenant admin consent. The first implementation supports a client secret supplied only through the environment:

```bash
export SHARE_SENTINEL_GRAPH_TENANT_ID='<tenant-id>'
export SHARE_SENTINEL_GRAPH_CLIENT_ID='<application-client-id>'
read -rsp 'Graph client secret: ' SHARE_SENTINEL_GRAPH_CLIENT_SECRET && echo
export SHARE_SENTINEL_GRAPH_CLIENT_SECRET

python collector/share_sentinel_sharepoint.py \
  --auth app \
  --output sharepoint.ndjson.gz \
  --gzip

unset SHARE_SENTINEL_GRAPH_CLIENT_SECRET
```

Application mode uses the supported [`/sites/getAllSites`](https://learn.microsoft.com/graph/api/site-getallsites?view=graph-rest-1.0) inventory and follows every `@odata.nextLink`. It then lists every visible document library through [`/sites/{site-id}/drives`](https://learn.microsoft.com/graph/api/drive-list?view=graph-rest-1.0).

For a deliberately restricted application assessment, `Sites.Selected` is supported only with one or more explicit `--site` targets and matching site grants. It cannot be used for tenant discovery. When JWT roles are inspectable, the collector rejects that ambiguous combination before collection; for an opaque token, Graph enforces it when discovery begins.

Client secrets are acceptable for an initial deployment, but certificates or workload identity are preferable for long-lived automation. Certificate authentication is not implemented in this first collector version.

“Metadata-only” describes the calls Share Sentinel makes, not the maximum capability of the credential. Microsoft Graph `Sites.Read.All` and delegated `Files.Read.All` can authorize document-content reads even though this collector never requests content. Treat tokens and client secrets as high-impact credentials, prefer explicit `Sites.Selected` grants where tenant-wide discovery is unnecessary, store secrets in an approved secret manager, and rotate them after suspected exposure. See the [Microsoft Graph permissions reference](https://learn.microsoft.com/graph/permissions-reference).

### Interactive delegated scan

Interactive and WAM authentication use an Entra public-client application ID. No client secret is used and Share Sentinel never asks for the user's password:

```bash
export SHARE_SENTINEL_GRAPH_CLIENT_ID='<public-client-id>'

python collector/share_sentinel_sharepoint.py \
  --auth interactive \
  --tenant-id organizations \
  --output user-visible.ndjson.gz \
  --gzip
```

Interactive MSAL authentication opens the normal Microsoft sign-in experience and supports account selection, MFA, and Conditional Access subject to tenant policy. On supported Windows systems, `--auth wam` opts into the Web Account Manager broker; it still requires a correctly configured public-client ID and redirect URI. `--auth iwa` is a Windows-only compatibility path for supported hybrid/federated environments and is not recommended for MFA- or Conditional Access-dependent workflows.

On Windows PowerShell, install the broker-enabled dependency set and run WAM with an approved public-client registration:

```powershell
py -3.11 -m venv collector\.venv
.\collector\.venv\Scripts\Activate.ps1
python -m pip install -r collector\requirements.txt

$env:SHARE_SENTINEL_GRAPH_CLIENT_ID = '<public-client-id>'
python collector\share_sentinel_sharepoint.py `
  --auth wam --tenant-id organizations `
  --output user-visible.ndjson
```

The application registration must allow public-client flows and contain the WAM broker redirect URI required by MSAL. For the limited hybrid/federated compatibility path, use `--auth iwa --tenant-id '<tenant-id>' --login-hint 'user@contoso.example'`; it cannot complete MFA challenges. Interactive browser and WAM flows require a desktop/browser-capable user session. Use imported-token mode for headless or container quick checks rather than attempting an interactive login inside the container.

Default delegated discovery uses the Microsoft Graph Sites Search endpoint and is explicitly marked as potentially incomplete. `--discovery drive-search` instead uses Microsoft Search to enumerate visible document-library results and resolve their sites, which is useful for GraphRunner-style workflows. Both strategies run in the signed-in user's context, but indexing, query behavior, and tenant policy mean absence from either result is not proof that a site is inaccessible.

### Existing Graph token

Token mode supports GraphRunner-style workflows, enterprise token brokers, and testing without placing the bearer token in process arguments:

```bash
read -rsp 'Graph access token: ' GRAPH_ACCESS_TOKEN && echo
export GRAPH_ACCESS_TOKEN

python collector/share_sentinel_sharepoint.py \
  --auth token \
  --token-env GRAPH_ACCESS_TOKEN \
  --output user-visible.ndjson.gz \
  --gzip

unset GRAPH_ACCESS_TOKEN
```

The token can instead be read once from stdin or from a protected token file. On POSIX systems, token-file mode rejects group/world-readable permissions; on Windows, the operator must restrict the file ACL to the intended account. The collector locally inspects JWT metadata for the Graph audience, expiry, tenant, delegated scopes/application roles, and assessed identity. This inspection is not signature validation; Microsoft Graph remains authoritative. Tokens and authorization headers are never written to ordinary logs or artifacts.

Imported tokens are deliberately non-refreshable and must remain valid for the entire bounded collection. For large tenants, acquire a token with enough remaining lifetime, narrow the site scope, or use an MSAL-managed app/interactive mode that can renew a token without changing assessment identity. The collector fails rather than switching an imported credential mid-run.

Microsoft can also issue access tokens that clients must treat as opaque. For those tokens, supply the assessment context explicitly so Share Sentinel cannot mix state between identities:

```bash
python collector/share_sentinel_sharepoint.py \
  --auth token \
  --token-type delegated \
  --tenant-id '<actual-tenant-id>' \
  --assessed-identity 'analyst@contoso.example' \
  --output user-visible.ndjson.gz \
  --gzip
```

Opaque application tokens instead require `--token-type application`, a specific tenant ID, and `--client-id`. These values are operator assertions; Graph remains authoritative for the token and its permissions. Because an opaque token cannot expose a stable permission set locally, its token-derived state partition changes when the token rotates. The next run safely performs a full resynchronization rather than risking reuse of a checkpoint created under different permissions. Old opaque-token scopes are retained rather than guessed safe to delete, so use a dedicated/disposable state database for imported opaque tokens and periodically rotate or archive it to reclaim stale scopes. Do not mix that database with long-lived application-credential state.

## Limit the scope

Use a repeatable `--site` option to scan known site IDs or SharePoint URLs without tenant discovery:

```bash
python collector/share_sentinel_sharepoint.py \
  --auth token \
  --site 'https://contoso.sharepoint.com/sites/Finance' \
  --site 'contoso.sharepoint.com,site-collection-id,web-id' \
  --output finance.ndjson.gz \
  --gzip
```

Safety limits bound sites, libraries, items, pages, response sizes, request timeouts, retries, and Graph concurrency. A value of `0` means unlimited for the site, library, and item limits. Start with conservative concurrency for large or heavily used tenants.

Progress is written to stderr so NDJSON on stdout stays machine-readable. By default the collector prints a start line, periodic site/library/item counters, and one final status line. Use `-v` for per-library detail, repeat it for more request-level context, use `--progress-interval <seconds>` to tune periodic reporting, or set the interval to `0` to disable only periodic reports. `--quiet` suppresses progress output; terminal errors still remain actionable.

## Existence, lifecycle, and content evidence

Each discovered site is followed by a bounded, read-only Graph lookup of its site-collection root. Subsites in the same collection share one lookup, and only the configured number of requests are in flight, so discovery does not create an unbounded queue of lifecycle work. The endpoint record reports `existence_status`, `archive_status`, `lifecycle_state`, and structured evidence. Share Sentinel reports `recently_archived`, `fully_archived`, or `reactivating` only from Graph's authoritative `siteCollection.archivalDetails.archiveStatus` property; the evidence also makes clear that this lifecycle applies to the containing site collection, including when the discovered endpoint is a subsite. Absence of archival details after an explicit selection is shown as `not_archived` but explicitly marked as an inference rather than an authoritative provider assertion. If enrichment fails, the already discovered site remains `confirmed_from_discovery`, its lifecycle stays indeterminate, the run is partial, a site-collection-scoped warning is emitted, and library enumeration continues.

Explicit `--site` targets retain a synthetic endpoint record even when resolution fails, so the requested target and the observed failure are not lost. The statuses distinguish invalid syntax, authentication failure, permission denial, temporary unavailability, and an indeterminate failure. A Graph `404` is recorded as `not_found_or_not_visible`, not as proof of deletion: SharePoint security trimming can hide an existing site from the assessed identity. Confirm a suspected stale link with an independently authorized identity before treating it as deleted.

Document-library records describe how far content enumeration actually got. A successful complete delta traversal reports `content_state=populated` or `empty`, file/folder/item counts, observed file bytes, and `collection_complete=true`. It also preserves a file's Microsoft 365 Archive state when Graph supplies it and summarizes fully archived, reactivating, not-archived, and unknown file counts per library. This is deliberately best-effort on the production Graph v1.0 endpoint: Microsoft documents archive metadata in current developer guidance, but the v1.0 `file` facet does not yet make the property a universal contract, so omitted metadata remains unknown rather than being guessed active. The state schema upgrade performs one automatic full metadata sync before resuming delta collection so unchanged files from an older checkpoint do not remain permanently unassessed. `empty` is never inferred from a denied, interrupted, limited, or metadata-only request. When any file lacks usable size metadata, `total_size_bytes` remains the sum observed and `size_observation_complete=false`. A failed traversal instead reports its concrete state (for example `permission_denied` or `temporarily_unreachable`), leaves counts unknown, and preserves a resource-scoped error code. If even one item has invalid metadata, the library emits no apparently complete item subset and its checkpoint does not advance. `--no-files` deliberately reports `not_requested` / `not_assessed`; it does not claim the library is empty.

In the web inventory, enable the optional **File Archive State** item column to inspect the result and use its inline exact/exclude shortcuts. The advanced inventory query field is `file_archive_status`, for example `provider=sharepoint AND file_archive_status=fully_archived`; filtering remains server-side for large inventories and CSV exports.

## Direct permission and sharing evidence

Direct permission collection is opt-in because it adds at least one Graph request per assessed object. Choose the smallest scope that answers the review question:

```bash
# Assess the root of every discovered document library.
python collector/share_sentinel_sharepoint.py \
  --auth app --permissions library_roots \
  --output sharepoint-permissions.ndjson.gz --gzip

# Assess every library root and every item in the complete materialized snapshot.
python collector/share_sentinel_sharepoint.py \
  --auth app --permissions all_items \
  --max-permission-objects 10000 \
  --max-permission-http-attempts 25000 \
  --max-permission-entries 100000 \
  --permission-concurrency 2 \
  --output sharepoint-item-permissions.ndjson.gz --gzip
```

`--permissions none` is the default and preserves the schema-v1 artifact contract. `library_roots` checks only document-library roots. `all_items` checks roots plus every file and folder in the emitted snapshot, including unchanged items materialized from local delta state; it cannot be combined with `--no-files`. Permission requests are GET-only. Roots are assessed before item work, work is bounded by a small global concurrency limit, and HTTP attempt accounting includes permission pages, retries, and root-ID lookups. Object, request-attempt, and normalized-entry caps are run-wide. Hitting a cap, losing authorization, being throttled beyond the retry budget, or failing one object marks the affected evidence and the run partial without discarding successfully staged inventory or advancing a failed content checkpoint.

The defaults (10,000 permission objects and 100,000 normalized entries) are the supported starting envelope, not a claim that the CLI hard ceilings are validated throughput targets. The hard ceilings only reject pathological configuration. Load-test the complete collect/upload/ingest/compare path against representative data before raising defaults substantially; for very large tenants, prefer targeted-site runs with stable scope. Monitor artifact size, Graph throttling, ingest duration, PostgreSQL statement time, and permission table growth.

Each attempted object emits one `permission_assessment`, followed by bounded `permission_entry` records, under the `sharepoint_graph_permission_v1` semantics and `sharepoint_graph_permissions` surface. Assessments record selection, retrieval, provider-visibility, semantic, and principal-resolution coverage separately. Empty entries mean only that no entries were returned to this caller for this request; they never prove an object has no permissions. Stable assessment, subject, principal, entry, evidence, and complete entry-set hashes exclude mutable display paths and aliases. Display names and login names are retained only as bounded aliases. The collector preserves Graph's `inheritedFrom` source IDs if supplied, but Microsoft documents that SharePoint document libraries do not return this generic driveItem property, so an absent value remains inheritance `unknown` rather than being called direct. Group membership is not expanded and effective access is not computed.

The [Graph driveItem permissions endpoint](https://learn.microsoft.com/graph/api/driveitem-list-permissions?view=graph-rest-1.0) exposes effective sharing permission objects, including direct and inherited sharing entries, not a complete SharePoint ACL/effective-access evaluation. Its result depends on the caller and authorization mode; a non-owner caller can receive only permissions applicable to that caller. Share Sentinel therefore makes only two positive exposure classifications from authoritative link scope returned in the response:

- an `anonymous` sharing-link scope supports `ANONYMOUS`;
- an `organization` sharing-link scope supports `BROAD_INTERNAL`.

Specific-people links, invitations, principals, naming patterns such as `#EXT#`, empty results, and successful enumeration do not support `EXTERNAL` or `RESTRICTED`; those objects retain their prior `USER_VISIBLE` or `UNKNOWN` classification. Expired links remain evidence entries but do not support a current positive exposure label; Graph's `DateTime.MinValue` sentinel is treated as no expiration. Unknown future roles, permission facets, or link scopes make semantic coverage partial rather than being silently ignored. The normalized evidence never stores Graph `shareId`, sharing-link URLs, or embeddable HTML because those values may themselves grant or reveal access. See the [Graph permission resource](https://learn.microsoft.com/graph/api/resources/permission?view=graph-rest-1.0) for the provider contract.

## Incremental collection and local state

The collector uses the [driveItem delta API](https://learn.microsoft.com/graph/api/driveitem-delta?view=graph-rest-1.0) for both initial enumeration and later change tracking. Its SQLite state database stores metadata snapshots and opaque per-drive delta links; it never stores access tokens or document contents.

This is an important semantic boundary:

1. A subsequent run asks Graph only for changed, new, moved, renamed, or deleted items.
2. Changes are staged in SQLite under the current collection session.
3. The collector streams a complete materialized snapshot into the artifact, including unchanged items from local state.
4. Only after the artifact is durably finalized (and a requested direct upload is accepted) does it atomically advance the item snapshot and delta link.

As a result, every successful run remains a complete current inventory even though Graph work is incremental. Deleted items disappear from the materialized current snapshot, while stable IDs let run comparison identify moves and renames rather than reporting only unrelated path deletion/addition.

The default state location follows the platform state directory; override it with `--state-path` or `SHARE_SENTINEL_GRAPH_STATE_PATH`. Preserve this file between scheduled container runs by keeping it under the collector's `/data` volume. The collector creates its state database and SQLite sidecars with owner-only permissions on POSIX systems and restricts a newly created parent directory; operators remain responsible for permissions on an existing parent directory. State contains filenames, paths, URLs, and IDs and should be backed up and protected accordingly. File outputs are assembled in a protected sibling spool before atomic replacement; stdout and upload-only container runs use `/data` for temporary spooling. Size that persistent volume for the state database plus both the completed artifact and its in-progress spool.

`--full-sync` ignores a usable delta link for the current run but preserves the previous working snapshot until the replacement is complete. `--reset-delta` is for operator-directed recovery and follows the same replace-on-success rule. Graph `410 Gone` delta resets automatically trigger a safe full re-enumeration. One failed library records a scoped issue and does not discard successful libraries or advance the failed library's checkpoint.

Avoid running two collectors concurrently against the same state file and assessment identity. Optimistic state versions prevent silent checkpoint loss, but one overlapping run can fail its final checkpoint and must be repeated.

Standard output is useful for inspection and pipelines, but it is not a durable publication boundary. A stdout-only file inventory still emits a complete NDJSON snapshot, deliberately withholds any pending delta checkpoints, warns on stderr, and exits with status `1`. A `--no-files` metadata-only run has no item checkpoints to advance. Scheduled incremental file scans must use a file output or a confirmed direct upload.

## Capacity boundary

SharePoint load grows primarily with the number and metadata size of drive items, and a few very large libraries can dominate an otherwise small tenant. The first probable external bottleneck is Microsoft Graph throttling; locally it is `/data` capacity and SQLite I/O during materialization, followed by artifact ingest and Postgres index work. Delta reduces repeated Graph traffic, but every successful run intentionally emits a complete logical snapshot, so artifact and ingest volume remain linear in the current item count.

For a new or large tenant, begin with explicit `--site` targets or reviewed `--max-sites`, `--max-libraries`, and `--max-items` bounds. Record wall time, item count, artifact size, Graph retries, delta resets, partial failures, `/data` headroom, worker ingest duration, and Postgres growth before widening the scope. Keep concurrency conservative when retries rise; increasing workers cannot bypass a tenant or application Graph quota.

The synchronous run-diff API defaults to a 250,000-item combined ceiling and fails early above it rather than risking API memory exhaustion. Large recurring comparisons need a future asynchronous/materialized diff path; inventory browsing itself remains paginated. Use the repository capacity-artifact workflow to establish deployment-specific ingest throughput, then add a representative tenant dry run because mocked Graph tests cannot validate Microsoft-side quota and Conditional Access behavior.

## Exposure terminology

Filename inventory and exposure analysis are separate dimensions:

- `USER_VISIBLE` means the collector enumerated the object's metadata in the assessed delegated identity's Graph context. It does **not** prove content read access or mean public, organization-wide, external, or anonymous.
- `BROAD_INTERNAL` means evidence identifies a broad organization-wide principal or sharing scope.
- `EXTERNAL` means evidence identifies guest or external access.
- `ANONYMOUS` means evidence identifies an unauthenticated Anyone link.
- `RESTRICTED` means the available evidence found no broad, external, or anonymous exposure.
- `UNKNOWN` means the collector lacked enough evidence or permission to classify exposure.

Without opt-in permission evidence, the collector labels delegated visibility as `USER_VISIBLE` with evidence describing that assessment perspective, while application visibility alone remains `UNKNOWN`. With `--permissions library_roots` or `all_items`, an authoritative anonymous or organization link scope can strengthen the assessed object's label to `ANONYMOUS` or `BROAD_INTERNAL`. No label is strengthened merely because an application or user can enumerate metadata, and a partial or empty permission response never becomes a negative exposure conclusion.

## Upload to Share Sentinel

The SharePoint collector uses the existing run/artifact upload API. Create a project-scoped Share Sentinel token with `write:runs`, then keep it out of shell history:

```bash
read -rsp 'Share Sentinel API token: ' SHARE_SENTINEL_API_TOKEN && echo
export SHARE_SENTINEL_API_TOKEN

python collector/share_sentinel_sharepoint.py \
  --auth app \
  --output sharepoint.ndjson.gz \
  --gzip \
  --upload \
  --api-base http://localhost/api \
  --project-id '<project-uuid>'

unset SHARE_SENTINEL_API_TOKEN
```

The run UI records the provider, authentication type, tenant, assessed identity, discovery completeness, and initial/incremental snapshot mode. It does not receive the collector's opaque delta links or credentials.

When upload-only mode is used, the collector writes an owner-only temporary spool before uploading. A failed or ambiguous upload preserves that complete artifact and prints its recovery path. If the upload succeeds but a concurrent state update prevents checkpoint promotion, the collector returns `1` with a checkpoint warning; the accepted snapshot remains valid and the next run safely replays from the older checkpoint.

### Compose quick run

The repository bootstrap prepares the Compose stack and persistent collector volume. After running `./bootstrap.sh --development`, export the secret only for the collection process and use the collector profile:

```bash
read -rsp 'Graph client secret: ' SHARE_SENTINEL_GRAPH_CLIENT_SECRET && echo
export SHARE_SENTINEL_GRAPH_CLIENT_SECRET

docker compose --profile tools run --rm collector \
  sharepoint --auth app \
  --output /data/sharepoint.ndjson.gz --gzip

unset SHARE_SENTINEL_GRAPH_CLIENT_SECRET
```

Tenant and client IDs can be placed in the generated owner-only `.env`; the client secret should be supplied at run time. The named `collector_output` volume keeps both the artifact and `/data/sharepoint-state.sqlite3` across one-off collector containers. With the main stack running, set `SHARE_SENTINEL_PROJECT_ID` and `SHARE_SENTINEL_API_TOKEN` and add `--upload` to send the same durable artifact directly to Share Sentinel.

## Authentication and capability matrix

| Mode | Required setup | MFA / Conditional Access | Unattended | Assessment perspective |
|---|---|---:|---:|---|
| `app` | Tenant ID, confidential client ID, client secret, and `Sites.Read.All` or targeted `Sites.Selected` grants | Not applicable | Yes | Sites available to the application grant |
| `interactive` | Public-client ID and delegated consent | Yes | No | Selected user's security-trimmed view |
| `wam` | Windows, MSAL broker support, public-client redirect configuration | Yes | SSO-assisted | Current/selected user's security-trimmed view |
| `token` | Existing Microsoft Graph access token | Depends on issuer flow | Depends | Identity and permissions represented by the token |
| `iwa` | Windows and compatible hybrid/federated identity | Limited | Potentially | Windows user's security-trimmed view |

The collector requests read-only delegated `Sites.Read.All` and `Files.Read.All` scopes for interactive modes. It does not request `Sites.ReadWrite.All` or `Files.ReadWrite.All`. Microsoft Graph applies the user's existing SharePoint authorization in delegated mode.

## Current boundaries

- Microsoft Graph global cloud is the supported endpoint in this initial implementation; sovereign cloud selection is not yet exposed.
- App authentication currently supports client secrets, not certificates or managed/workload identity.
- The project does not ship a multi-tenant Share Sentinel public-client registration. Interactive and WAM users must provide a public-client ID; token mode works with an already authorized external session without a customer-created app.
- Delegated Search discovery is useful but not tenant-authoritative.
- Permission evidence is bounded, opt-in, caller-dependent sharing metadata rather than a complete SharePoint ACL, group-membership, inheritance, or effective-access evaluation. Only explicit anonymous and organization link scopes support positive exposure classification; external and restricted states are not guessed.
- `--site` accepts canonical SharePoint site URLs and Graph site IDs. It does not redeem or enumerate arbitrary `/:f:/`, `/:x:/`, `1drv.ms`, or other sharing URLs; Graph sharing-link redemption can change sharing state and requires a different permission model, so those links are not presented as stale/valid findings.
- Site pages, list rows outside document libraries, document contents, malware scanning, and content classification are outside this collector's scope.
- Collection is read-only but still produces normal Entra, Graph, and SharePoint audit activity.

Microsoft recommends honoring `Retry-After` when Graph throttles requests; the collector does so with bounded retries and treats an operator retry-delay budget as a partial resource failure rather than retrying earlier than instructed. See [Microsoft Graph throttling guidance](https://learn.microsoft.com/graph/throttling).
