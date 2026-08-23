from types import SimpleNamespace

from app import redis_client


class _FakeRedis:
    def __init__(self, counts: list[int] | None = None) -> None:
        self.counts = counts or [1]
        self.eval_calls: list[tuple] = []

    def eval(self, *args):
        self.eval_calls.append(args)
        return self.counts


def test_create_redis_client_applies_bounded_socket_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake = _FakeRedis()

    def _from_url(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(
        redis_client,
        "get_settings",
        lambda: SimpleNamespace(
            redis_url="redis://cache:6379/2",
            redis_connect_timeout_seconds=2.5,
            redis_socket_timeout_seconds=4.5,
        ),
    )
    monkeypatch.setattr(redis_client.redis.Redis, "from_url", _from_url)

    assert redis_client.create_redis_client() is fake
    assert captured == {
        "url": "redis://cache:6379/2",
        "kwargs": {
            "decode_responses": True,
            "socket_connect_timeout": 2.5,
            "socket_timeout": 4.5,
        },
    }


def test_increment_keys_with_ttl_is_one_atomic_redis_operation() -> None:
    fake = _FakeRedis(counts=[2, 5])

    counts = redis_client.increment_keys_with_ttl(fake, ["email", "ip"], 60)

    assert counts == [2, 5]
    assert len(fake.eval_calls) == 1
    script, key_count, email_key, ip_key, ttl = fake.eval_calls[0]
    assert "INCR" in script
    assert "EXPIRE" in script
    assert "TTL" in script
    assert "< 0" in script
    assert key_count == 2
    assert (email_key, ip_key, ttl) == ("email", "ip", 60)


def test_increment_keys_with_ttl_skips_redis_for_empty_key_set() -> None:
    fake = _FakeRedis()

    assert redis_client.increment_keys_with_ttl(fake, [], 60) == []
    assert fake.eval_calls == []
