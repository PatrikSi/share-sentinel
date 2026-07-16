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

The final artifact is a compact JSON document, optionally gzip-compressed when `--gzip` is used.

Internally the collector buffers intermediate per-endpoint data on local disk before assembling the final document, so local temp space and inode usage matter on large scans.

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
  --output out.json.gz \
  --gzip
```

Command-line secrets can be visible in shell history and process listings. Prefer the supported environment variables:

```bash
read -rsp "SMB password: " SHARE_SENTINEL_SMB_PASSWORD && echo
export SHARE_SENTINEL_SMB_PASSWORD
```

The collector also reads `SHARE_SENTINEL_SMB_HASHES` and `SHARE_SENTINEL_API_TOKEN`. Protect the collector host and unset secret variables after the run.

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
  --output out.json.gz \
  --gzip \
  --upload \
  --api-base http://localhost/api \
  --project-id <project-uuid>
```

Create a project-scoped token with `runs:read` and `runs:write`; the token role must be at least `operator`.

## Operational notes

### Capacity levers

The defaults are intentionally aggressive enough to move quickly in a lab, but they can put real pressure on targets and on the downstream ingest stack.

Treat these flags as capacity levers:

- `--workers`
- `--max-depth`
- `--max-entries-per-share`
- target count and host list size

For shared or fragile environments, start conservatively and scale up after you understand target behavior and ingest throughput.

Only scan systems for which you have explicit authorization. The collector performs concurrent authentication, share enumeration, and directory traversal and can create meaningful target load.

### Output and retry behavior

- If no endpoint, resource, item, or error data is collected, the collector does not keep an output file.
- If upload fails after the artifact has been written, the collector tries to keep the local artifact so you can retry without rescanning.
- If `--upload` is used without `--output`, the collector creates a temporary local artifact first and removes it only after a successful upload path.

### Upload semantics

Upload success means the artifact was accepted by the API. It does not always mean the Redis queue handoff succeeded immediately.

When the API falls back to database-based recovery, the collector will emit an upload warning and the run may stay `UPLOADED` briefly before the worker starts. Watch run status or activity in the UI/API instead of assuming that upload completion means ingest has already begun.

## Artifact compatibility

The API accepts raw JSON, NDJSON, JSONL, and gzip variants, but the bundled collector writes compact JSON or JSON.GZ output. If you build another producer, make sure it matches the API artifact contract described in the main project docs.

The tracked [`examples/sample-artifact.json`](../examples/sample-artifact.json) is synthetic and safe to use for a first ingest test.

## Exit codes

- `0`: collection completed without target failures
- `1`: partial result; an artifact may still have been written or uploaded
- `2`: configuration, input, output, or complete collection failure
