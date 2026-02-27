# Collector

One-off SMB collector that writes NDJSON or NDJSON.GZ and can upload artifacts to Share Sentinel.

## Example

```bash
python smbguard_collector.py \
  --cidr 10.0.0.0/24 \
  --domain CONTOSO \
  --username alice \
  --password '***' \
  --workers 200 \
  --timeout 3 \
  --max-depth 1 \
  --output out.ndjson.gz \
  --gzip
```

## Upload mode

```bash
python smbguard_collector.py \
  --hosts hosts.txt \
  --domain CONTOSO \
  --username alice \
  --password '***' \
  --output out.ndjson.gz --gzip \
  --upload \
  --api-base https://api.example.com \
  --project-id <project-uuid> \
  --api-token <token>
```
