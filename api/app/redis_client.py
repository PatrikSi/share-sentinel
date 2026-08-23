from __future__ import annotations

from collections.abc import Sequence

import redis

from app.config import get_settings

_INCREMENT_WITH_TTL_SCRIPT = """
local counts = {}
for index, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    if count == 1 or redis.call('TTL', key) < 0 then
        redis.call('EXPIRE', key, ARGV[1])
    end
    counts[index] = count
end
return counts
"""


def create_redis_client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )


def increment_keys_with_ttl(client: redis.Redis, keys: Sequence[str], ttl_seconds: int) -> list[int]:
    if not keys:
        return []
    counts = client.eval(
        _INCREMENT_WITH_TTL_SCRIPT,
        len(keys),
        *keys,
        max(1, int(ttl_seconds)),
    )
    return [int(count) for count in counts]
