# Collector

The collector is a standalone Python CLI for scanning SMB and NFS targets, writing a Share Sentinel-compatible artifact, and optionally uploading that artifact into a project run.

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

## Common upload example

For the bundled Docker deployment, the API base is typically `http://localhost/api` rather than a separate `api.example.com` host.

```bash
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
  --project-id <project-uuid> \
  --api-token <token>
```

## Operational notes

### Capacity levers

The defaults are intentionally aggressive enough to move quickly in a lab, but they can put real pressure on targets and on the downstream ingest stack.

Treat these flags as capacity levers:

- `--workers`
- `--max-depth`
- `--max-entries-per-share`
- target count and host list size

For shared or fragile environments, start conservatively and scale up after you understand target behavior and ingest throughput.

### Output and retry behavior

- If no endpoint, resource, item, or error data is collected, the collector does not keep an output file.
- If upload fails after the artifact has been written, the collector tries to keep the local artifact so you can retry without rescanning.
- If `--upload` is used without `--output`, the collector creates a temporary local artifact first and removes it only after a successful upload path.

### Upload semantics

Upload success means the artifact was accepted by the API. It does not always mean the Redis queue handoff succeeded immediately.

When the API falls back to database-based recovery, the collector will emit an upload warning and the run may stay `UPLOADED` briefly before the worker starts. Watch run status or activity in the UI/API instead of assuming that upload completion means ingest has already begun.

## Artifact compatibility

The API accepts raw JSON, NDJSON, JSONL, and gzip variants, but the bundled collector writes compact JSON or JSON.GZ output. If you build another producer, make sure it matches the API artifact contract described in the main project docs.
