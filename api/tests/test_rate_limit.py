import pytest
from app import rate_limit
from fastapi import HTTPException
from starlette.requests import Request


class _FakeRedis:
    def __init__(self, count: int = 1, fail: bool = False):
        self.count = count
        self.fail = fail
        self.eval_calls: list[tuple] = []

    def eval(self, *args):
        if self.fail:
            raise rate_limit.redis.RedisError("redis down")
        self.eval_calls.append(args)
        return [self.count]


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [],
        "query_string": b"",
        "client": ("10.0.0.1", 5000),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_rate_limiter_uses_resolved_client_ip(monkeypatch) -> None:
    fake_redis = _FakeRedis(count=1, fail=False)
    monkeypatch.setattr(rate_limit.redis.Redis, "from_url", lambda *_args, **_kwargs: fake_redis)
    monkeypatch.setattr(rate_limit, "resolve_client_ip", lambda _request: "198.51.100.10")
    monkeypatch.setattr(rate_limit.time, "time", lambda: 120.0)

    limiter = rate_limit.RateLimiter()
    limiter.check(_request(), "auth_login", limit=20, window_seconds=60, actor_key="user:abc", fail_open=False)

    identity_hash = rate_limit.RateLimiter._hash_identity("198.51.100.10:user:abc")
    assert len(fake_redis.eval_calls) == 1
    _script, key_count, key, ttl = fake_redis.eval_calls[0]
    assert key_count == 1
    assert key == f"ratelimit:auth_login:{identity_hash}:2"
    assert ttl == 61


def test_rate_limiter_fail_closed_when_redis_unavailable(monkeypatch) -> None:
    fake_redis = _FakeRedis(fail=True)
    monkeypatch.setattr(rate_limit.redis.Redis, "from_url", lambda *_args, **_kwargs: fake_redis)
    limiter = rate_limit.RateLimiter()

    with pytest.raises(HTTPException) as exc:
        limiter.check(_request(), "auth_login", limit=20, window_seconds=60, actor_key="u", fail_open=False)
    assert exc.value.status_code == 503


def test_rate_limiter_fail_open_when_configured(monkeypatch) -> None:
    fake_redis = _FakeRedis(fail=True)
    monkeypatch.setattr(rate_limit.redis.Redis, "from_url", lambda *_args, **_kwargs: fake_redis)
    limiter = rate_limit.RateLimiter()

    limiter.check(_request(), "auth_login", limit=20, window_seconds=60, actor_key="u", fail_open=True)


def test_rate_limiter_rejects_when_count_exceeds_limit(monkeypatch) -> None:
    fake_redis = _FakeRedis(count=11, fail=False)
    monkeypatch.setattr(rate_limit.redis.Redis, "from_url", lambda *_args, **_kwargs: fake_redis)
    limiter = rate_limit.RateLimiter()

    with pytest.raises(HTTPException) as exc:
        limiter.check(_request(), "auth_login", limit=10, window_seconds=60, actor_key="u", fail_open=False)
    assert exc.value.status_code == 429
    assert exc.value.headers == {"Retry-After": "60"}
