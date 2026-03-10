# Collector

One-off collector that writes compact JSON or JSON.GZ artifacts and can upload them to Share Sentinel.

## Example

```bash
python share_sentinel_collector.py \
  --cidr 10.0.0.0/24 \
  --domain CONTOSO \
  --username alice \
  --password '***' \
  --workers 200 \
  --timeout 3 \
  --max-depth 1 \
  --output out.json.gz \
  --gzip
```

## Upload mode

```bash
python share_sentinel_collector.py \
  --hosts hosts.txt \
  --domain CONTOSO \
  --username alice \
  --password '***' \
  --output out.json.gz --gzip \
  --upload \
  --api-base https://api.example.com \
  --project-id <project-uuid> \
  --api-token <token>
```
