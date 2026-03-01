import redis

from app.services import auth_rate_limit


class _FailingRedis:
    def ttl(self, _key):
        raise redis.RedisError("down")

    def incr(self, _key):
        raise redis.RedisError("down")

    def delete(self, *_keys):
        raise redis.RedisError("down")


def _reset_fallback_state() -> None:
    auth_rate_limit._fallback_failures.clear()
    auth_rate_limit._fallback_locks.clear()


def test_login_lockout_uses_fallback_when_redis_unavailable(monkeypatch) -> None:
    _reset_fallback_state()
    monkeypatch.setattr(auth_rate_limit, "redis_client", _FailingRedis())
    monkeypatch.setattr(auth_rate_limit.settings, "auth_login_max_attempts", 2)
    monkeypatch.setattr(auth_rate_limit.settings, "auth_login_window_seconds", 60)
    monkeypatch.setattr(auth_rate_limit.settings, "auth_login_lockout_seconds", 30)

    state_before = auth_rate_limit.check_login_throttle("user@example.com", "10.0.0.1")
    assert state_before.blocked is False

    auth_rate_limit.record_login_failure("user@example.com", "10.0.0.1")
    auth_rate_limit.record_login_failure("user@example.com", "10.0.0.1")

    state_after = auth_rate_limit.check_login_throttle("user@example.com", "10.0.0.1")
    assert state_after.blocked is True
    assert state_after.retry_after_seconds is not None


def test_clear_login_failures_unlocks_fallback_state(monkeypatch) -> None:
    _reset_fallback_state()
    monkeypatch.setattr(auth_rate_limit, "redis_client", _FailingRedis())
    monkeypatch.setattr(auth_rate_limit.settings, "auth_login_max_attempts", 1)
    monkeypatch.setattr(auth_rate_limit.settings, "auth_login_window_seconds", 60)
    monkeypatch.setattr(auth_rate_limit.settings, "auth_login_lockout_seconds", 30)

    auth_rate_limit.record_login_failure("user@example.com", "10.0.0.1")
    locked = auth_rate_limit.check_login_throttle("user@example.com", "10.0.0.1")
    assert locked.blocked is True

    auth_rate_limit.clear_login_failures("user@example.com", "10.0.0.1")
    unlocked = auth_rate_limit.check_login_throttle("user@example.com", "10.0.0.1")
    assert unlocked.blocked is False
