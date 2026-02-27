import json

import redis

from app.config import get_settings

_redis = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
STREAM_NAME = "ingest_jobs"


def enqueue_ingest_job(payload: dict) -> str:
    serializable = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in payload.items()}
    return _redis.xadd(STREAM_NAME, serializable, maxlen=100000, approximate=True)
