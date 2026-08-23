import hashlib
import logging
import time

import redis
from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.deps import resolve_client_ip
from app.redis_client import create_redis_client, increment_keys_with_ttl

logger = logging.getLogger("share_sentinel.ratelimit")


class RateLimiter:
    def __init__(self) -> None:
        self._redis = create_redis_client()

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
            count = increment_keys_with_ttl(self._redis, [key], window_seconds + 1)[0]
        except redis.RedisError:
            logger.warning("redis unavailable for rate limiting scope=%s", scope)
            if should_fail_open:
                return
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="rate limit backend unavailable")

        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers={"Retry-After": str(window_seconds)},
            )


def _resolve_rate_limit_ip(request: Request) -> str:
    try:
        return resolve_client_ip(request)
    except Exception:  # noqa: BLE001
        return request.client.host if request.client else "unknown"
