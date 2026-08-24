# Collector

The collector is a standalone Python CLI for scanning SMB and NFS targets, writing a Share Sentinel-compatible artifact, and optionally uploading that artifact into a project run.

## Prerequisites and installation

- Python 3.11
- Network access to the explicitly authorized SMB/NFS targets
- `showmount` for NFS discovery (`nfs-common` on Debian/Ubuntu or `nfs-utils` on Fedora/RHEL)

From this directory:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python share_sentinel_collector.py --help
```

The published container includes `showmount` and runs as an unprivileged user:

```bash
docker run --rm ghcr.io/patriksi/share-sentinel-collector:latest --help
```

For repeatable use, replace `latest` with an exact `vX.Y.Z` or `sha-<full-commit>` tag. The root Compose file also exposes the collector through the optional `tools` profile and a persistent output volume. In a generated development environment, build it from the checkout on first use:

```bash
docker compose --profile tools run --rm --build collector --help
```

Production environments omit `--build`; Compose pulls the configured GHCR tag.

## What it produces

Use `.ndjson` or `.jsonl` output for normal collection. These schema-v1 records
are spooled once and copied to the final artifact as a bounded stream; this is
also the format written to stdout and used for upload-only temporary artifacts.
Add `.gz` and `--gzip` for streaming compression.

`.json` remains available as a compact, nested compatibility format. Compact
assembly is deliberately limited to 8 MiB of flat records per endpoint and
40 MiB in total because it must reconstruct one endpoint tree in memory. A
larger compact run fails safely before assembly and tells you to rerun with
`.ndjson`; it never replaces an existing destination with partial output.

The filename and compression flag must agree: `.ndjson.gz`, `.jsonl.gz`, and
`.json.gz` require `--gzip`, while a non-`.gz` filename rejects `--gzip`.
Other output suffixes are rejected so local files and API uploads cannot be
misclassified.

## Common local example

```bash
python share_sentinel_collector.py \
  --cidr 10.0.0.0/24 \
  --share-types smb \
  --domain CONTOSO \
  --username alice \
  --password '***' \
  --workers 50 \
  --timeout 3 \
  --max-depth 1 \
  --access-probe-limit 3 \
  --progress-interval 5 \
  --output out.ndjson.gz \
  --gzip
```

The collector accepts the common AD identity forms used by Windows, Samba,
Impacket, and NetExec:

```bash
# Least ambiguous; recommended for scripts.
--domain CONTOSO --username alice

# Windows down-level logon name. Quote it in POSIX shells.
--username 'CONTOSO\alice'

# Impacket-style form; safe without quoting in POSIX shells.
--username CONTOSO/alice

# User principal name (UPN).
--username alice@contoso.example

# Local SAM account.
--username '.\alice'
# Equivalent: --local-auth --username alice
```

Do not enter a single backslash unquoted in a POSIX shell: `CONTOSO\alice`
is passed to the program as `CONTOSOalice` because the shell consumes the
backslash before the collector starts. Quote the value, escape the backslash,
use `CONTOSO/alice`, or use separate `--domain` and `--username` flags.

If an identity embeds a domain and `--domain` is also supplied, both values
must match (case-insensitively). Conflicting domain/local/Kerberos modes fail
before any network authentication is attempted.

Command-line secrets can be visible in shell history and process listings. Prefer the supported environment variables:

```bash
read -rsp "SMB password: " SHARE_SENTINEL_SMB_PASSWORD && echo
export SHARE_SENTINEL_SMB_PASSWORD
```

The collector also reads `SHARE_SENTINEL_SMB_HASHES` and `SHARE_SENTINEL_API_TOKEN`. Protect the collector host and unset secret variables after the run.
Hash authentication uses `LMHASH:NTHASH`; the LM component may be empty, but
the NT component must be a 32-character hexadecimal hash. Password and hash
sources are mutually exclusive, and anonymous mode rejects all supplied SMB
credentials instead of silently ignoring them.

## Common upload example

For the bundled Docker deployment, the API base is typically `http://localhost/api` rather than a separate `api.example.com` host.

```bash
read -rsp "Share Sentinel API token: " SHARE_SENTINEL_API_TOKEN && echo
export SHARE_SENTINEL_API_TOKEN

python share_sentinel_collector.py \
  --hosts hosts.txt \
  --share-types smb \
  --domain CONTOSO \
  --username alice \
  --password '***' \
  --output out.ndjson.gz \
  --gzip \
  --upload \
  --api-base http://localhost/api \
  --project-id <project-uuid>
```

Create a project-scoped token with `write:runs`; the token role must be at least `operator`. The API treats `write:runs` as including run-read access. An explicit `read:runs` scope is also compatible, and ambiguous upload responses are reconciled through the run read endpoint before the collector reports success.

## Operational notes

### Progress, verbosity, and shell output

Routine status is line-oriented and written to stderr, including an initial
scope summary, periodic progress, and one terminal result. Progress reports
distinguish targets discovered, submitted, actively running, queued, processed,
cancelled, and remaining. They also include successful/failed hosts, endpoint,
resource, item and issue counts, elapsed time, and the current host rate.

```bash
# Default: start, periodic progress every five seconds, and final summary.
python share_sentinel_collector.py --hosts hosts.txt --output scan.ndjson

# One completion line per target in addition to periodic progress.
python share_sentinel_collector.py --hosts hosts.txt --output scan.ndjson -v

# Also report protocol and SMB share activity. Share names can be sensitive.
python share_sentinel_collector.py --hosts hosts.txt --output scan.ndjson -vv

# No routine status. Failures still emit a concise stderr issue summary.
python share_sentinel_collector.py --hosts hosts.txt --output scan.ndjson --quiet

# Disable periodic reports but retain the start and final summaries.
python share_sentinel_collector.py --hosts hosts.txt --output scan.ndjson --progress-interval 0
```

When `--output` is omitted, NDJSON is the only content written to stdout;
status remains on stderr. This makes shell redirection safe:

```bash
python share_sentinel_collector.py --hosts hosts.txt --quiet > scan.ndjson
```

Passwords, hashes, and API tokens are redacted from artifact command metadata
and are never included in progress messages. Use the documented environment
variables to keep them out of process listings as well. Long-option
abbreviations are rejected, so an abbreviated secret flag cannot bypass this
metadata redaction contract.

### Bounded scope and interruption

Before opening network connections, the collector computes the exact unique
target count without expanding large CIDRs in memory. The default
`--max-targets 65536` guard prevents an accidental oversized scan. Narrow the
scope or explicitly raise the limit after review; `--max-targets 0` disables
the guard.

Pressing Ctrl-C stops new submissions, cancels queued targets, and asks active
SMB/NFS tasks to stop between bounded network operations. The collector drains
those operations before finalizing so the artifact cannot race with worker
threads. It writes a `SCAN_INTERRUPTED` issue, keeps the partial artifact, skips
automatic upload, prints an interrupted terminal summary, and exits `130`.
If Ctrl-C arrives during upload or its retry backoff, the collector also exits
`130`, retains the local artifact, and reports the delivery outcome as unknown
so operators can inspect the run before retrying.

If Ctrl-C arrives while a file artifact is being finalized, the collector
keeps the disk spool, makes one atomic finalization retry, skips upload, and
exits `130`; a prior destination is never replaced by a partial file. Stdout
cannot be rewound safely, so an interrupted stdout finalization is not replayed.

### Capacity levers

The defaults are intentionally aggressive enough to move quickly in a lab, but they can put real pressure on targets and on the downstream ingest stack.

Treat these flags as capacity levers:

- `--workers`
- `--max-depth`
- `--max-entries-per-share`
- `--access-probe-limit`
- `--max-targets`
- target count and host list size

For shared or fragile environments, start conservatively and scale up after you understand target behavior and ingest throughput.

`--max-entries-per-share` caps entries inspected, excluding the synthetic `.`
and `..` directory rows, before path or extension filters are applied. This
keeps a directory containing many non-matching files bounded. A truncation
issue reports both entries inspected and records emitted; reaching the cap on
the exact final entry does not create a false truncation warning.

Prefer `.ndjson`/`.ndjson.gz` for any inventory whose size is not already
known to be small. The collector keeps one NDJSON spool open and writes each
flat record once, so a large share does not require a directory tree in RAM or
one filesystem open per item. Compact `.json` is a bounded compatibility path,
not the large-scan format.

### Observed SMB access

SMB resource records retain the compatibility `access_level` and add independent
`access_capabilities` evidence for share tree connection, directory listing,
file reading, file creation, directory creation, existing-file modification,
deletion, ACL changes, and ownership changes. Each capability is `allowed`,
`denied`, `mixed`, `not_tested`, or `inconclusive` and includes bounded attempt
counts. Unknown, transient, sharing-violation, and disappeared-object outcomes
are not converted into authorization denials.

Compatibility summaries are conservative: `readable` requires an observed
file-read right, `list_only` requires an observed listing but no read, and
`no_access` is reserved for an explicit share tree-connect denial with no
stronger positive evidence. Connected-but-not-listable and write-only cases use
`unknown` in the compatibility field while their capability evidence shows the
useful result.

The reserved `_metadata.complete` flag means the per-share probe workflow
reached its final record without cancellation; it does not mean coverage was
exhaustive. Read it together with `_metadata.partial`, sample counts, and
`listing_truncated`. A completed bounded scan can correctly be partial.

The default `--access-probe-limit 3` checks the share root plus up to three
discovered directories and three discovered files. Candidate selection happens
before `--extensions-only` output filtering, so an inventory display filter does
not accidentally suppress access evidence. Set the limit to `0` to disable
explicit handle probes; tree-connect and directory-listing observations are
still recorded.

Access candidate discovery is separate from inventory output depth. When the
configured traversal sees folders but no files, the collector may inspect a
bounded number of those folders to find probe candidates without emitting
out-of-depth items. This prevents the default root-only inventory view from
silently turning a common nested share into `read_file: not_tested`.

These checks are non-mutating. They open existing objects with `FILE_OPEN`, ask
the SMB server to authorize one narrow access mask, and immediately close the
handle. They never create a probe file or directory, write bytes, mark an object
for deletion, replace an ACL, or take ownership. A positive result proves that
the scan identity was granted that right on a sampled object at scan time; it
does not guarantee that a later operation will succeed under quotas, read-only
storage, endpoint security controls, object-specific ACLs, or changed state.
Handle opens can still produce ordinary SMB/authorization audit telemetry and
may affect server-side last-access accounting.

Only scan systems for which you have explicit authorization. The collector performs concurrent authentication, share enumeration, and directory traversal and can create meaningful target load.

SMB authentication is attempted once per target. The collector deliberately
does not retry failed logons because retries across many hosts can amplify an
incorrect credential into an Active Directory account lockout. Correct the
credential or connectivity issue and rerun explicitly.

### Output and retry behavior

- File artifacts are assembled into a private (`0600`) sibling temporary file, flushed, and atomically replaced. A failed final write does not truncate a previously good artifact at the destination.
- Collector buffering is always removed on normal completion, partial completion, write failure, and handled interruption.
- If no endpoint, resource, item, or error data is collected, the collector does not keep an output file.
- If upload fails after the artifact has been written, the collector tries to keep the local artifact so you can retry without rescanning.
- If `--upload` is used without `--output`, the collector creates a temporary local artifact first and removes it only after a successful upload path.
- An interrupted upload keeps that temporary artifact and is reported as an unknown delivery outcome rather than as success or ordinary failure.
- API calls retry only transient connection/timeouts and HTTP 408/429/5xx responses. Backoff has jitter, honors numeric `Retry-After` up to 30 seconds, and is bounded by `--upload-attempts` (default 3).
- `--upload-timeout` (default 600 seconds) is the response/read budget for each attempt; API connection establishment is capped at 10 seconds per attempt.
- A timeout after an artifact POST is ambiguous because the API may already have stored and queued it. If a retry receives an ingest/state conflict, the collector reads the run and reports recovery only when its status is `UPLOADED`, `INGESTING`, or `COMPLETE` and the server artifact SHA-256 exactly matches the local file. Missing read permission, a mismatched digest, or any other state fails the upload and retains the local artifact.

### Upload semantics

Upload success means the artifact was accepted by the API. It does not always mean the Redis queue handoff succeeded immediately.

When the API falls back to database-based recovery, the collector will emit an upload warning and the run may stay `UPLOADED` briefly before the worker starts. Watch run status or activity in the UI/API instead of assuming that upload completion means ingest has already begun.

## Artifact compatibility

The collector emits flat schema-v1 NDJSON/JSONL records for its streaming path
and the nested `share_sentinel_compact_json` document for `.json` compatibility.
The API and worker accept both forms, including their gzip variants. Compact
`.json.gz` uploads remain raw streams and send a canonical ASCII basename in
`X-Artifact-Filename` so the API can distinguish compact gzip from NDJSON gzip
without multipart pre-spooling; the artifact is never loaded wholesale for
upload.

The tracked [`examples/sample-artifact.json`](../examples/sample-artifact.json) is synthetic and safe to use for a first ingest test.

SMB entries include nullable UTC ISO-8601 `mtime` (last write), `created_at`,
`accessed_at`, and `changed_at` (metadata change) values when the server returns
valid metadata. Files can additionally include `size_bytes`,
`allocation_size_bytes`, and common `file_attributes`. Older entries and NFS
export-only records remain valid without these fields.

SMB dialects use Impacket's negotiated protocol constants (`2.0.2`, `2.1`,
`3.0`, `3.0.2`, or `3.1.1`). The `smb.signing` value is deliberately limited
to `required`, `not_required`, or `unknown`: Impacket's
`isSigningRequired()` signal does not prove the broader server capability
implied by labels such as `enabled`.

NFS collection currently checks tcp/2049 and enumerates advertised exports via
`showmount -e`. It does not mount exports or traverse their contents. A missing,
timed-out, or denied `showmount` command is recorded as a partial-coverage issue
rather than silently treated as an empty server. Discovered exports are marked
`unknown`; an advertised export name does not prove that the scanner can mount
or list it.

## Exit codes

- `0`: collection completed without target failures
- `1`: partial result, including target/protocol failures, truncation, dependency warnings, or other recorded coverage issues; an artifact may still have been written or uploaded
- `2`: configuration, input, output, or complete collection failure
- `130`: interrupted by Ctrl-C during collection or upload; the local artifact is preserved and an upload interruption is treated as an unknown delivery outcome
