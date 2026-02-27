import logging
import time

import redis
from fastapi import HTTPException, Request, status

from app.config import get_settings

logger = logging.getLogger("share_sentinel.ratelimit")


class RateLimiter:
    def __init__(self) -> None:
        settings = get_settings()
        self._redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)

    def check(self, request: Request, scope: str, limit: int, window_seconds: int) -> None:
        client_ip = request.client.host if request.client else "unknown"
        actor = request.headers.get("authorization", "anon")[:32]
        bucket = int(time.time() // window_seconds)
        key = f"ratelimit:{scope}:{client_ip}:{actor}:{bucket}"

        try:
            count = self._redis.incr(key)
            if count == 1:
                self._redis.expire(key, window_seconds + 1)
        except redis.RedisError:
            logger.warning("redis unavailable, skipping rate limit")
            return

        if count > limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")
