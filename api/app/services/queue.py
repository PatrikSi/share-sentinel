import json

from app.config import get_settings
from app.redis_client import create_redis_client

_redis = create_redis_client()
STREAM_NAME = "ingest_jobs"


def enqueue_worker_job(payload: dict) -> str:
    settings = get_settings()
    serializable = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in payload.items()}
    return _redis.xadd(
        STREAM_NAME,
        serializable,
        maxlen=settings.redis_stream_maxlen,
        approximate=True,
    )


def enqueue_ingest_job(payload: dict) -> str:
    """Backward-compatible name used by the artifact upload path."""

    return enqueue_worker_job(payload)
