import logging
import hashlib
import time

import redis
from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.deps import resolve_client_ip

logger = logging.getLogger("share_sentinel.ratelimit")


class RateLimiter:
    def __init__(self) -> None:
        settings = get_settings()
        self._redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)

    @staticmethod
    def _hash_identity(value: str) -> str:
        digest = hashlib.sha256()
        digest.update(value.encode("utf-8"))
        return digest.hexdigest()[:20]

    def check(
        self,
        request: Request,
        scope: str,
        limit: int,
        window_seconds: int,
        actor_key: str | None = None,
        fail_open: bool | None = None,
    ) -> None:
        client_ip = _resolve_rate_limit_ip(request)
        actor = actor_key or "anon"
        identity_hash = self._hash_identity(f"{client_ip}:{actor}")
        bucket = int(time.time() // window_seconds)
        key = f"ratelimit:{scope}:{identity_hash}:{bucket}"
        should_fail_open = get_settings().rate_limit_fail_open if fail_open is None else fail_open

        try:
            count = self._redis.incr(key)
            if count == 1:
                self._redis.expire(key, window_seconds + 1)
        except redis.RedisError:
            logger.warning("redis unavailable for rate limiting scope=%s", scope)
            if should_fail_open:
                return
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="rate limit backend unavailable")

        if count > limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")


def _resolve_rate_limit_ip(request: Request) -> str:
    try:
        return resolve_client_ip(request)
    except Exception:  # noqa: BLE001
        return request.client.host if request.client else "unknown"
