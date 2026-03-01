from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import main


def test_safe_run_id_handles_missing_and_invalid_values() -> None:
    value = uuid.uuid4()
    assert main._safe_run_id(None) is None
    assert main._safe_run_id({}) is None
    assert main._safe_run_id({"run_id": str(value)}) == str(value)
    assert main._safe_run_id({"run_id": 123}) is None
    assert main._safe_run_id({"run_id": "abc"}) is None


def test_should_log_redis_error_uses_interval_threshold() -> None:
    assert main._should_log_redis_error(last_logged_at=0.0, now=30.0) is True
    assert main._should_log_redis_error(last_logged_at=10.0, now=39.0) is False
    assert main._should_log_redis_error(last_logged_at=10.0, now=40.0) is True
    assert main._should_log_redis_error(last_logged_at=100.0, now=109.9, interval_seconds=10.0) is False
    assert main._should_log_redis_error(last_logged_at=100.0, now=110.0, interval_seconds=10.0) is True


def test_normalize_uuid_str_returns_canonical_uuid_or_none() -> None:
    value = uuid.uuid4()
    assert main._normalize_uuid_str(str(value)) == str(value)
    assert main._normalize_uuid_str(value) == str(value)
    assert main._normalize_uuid_str("not-a-uuid") is None
    assert main._normalize_uuid_str(None) is None


def test_read_int_env_validates_and_enforces_minimum(monkeypatch) -> None:
    monkeypatch.setenv("TEST_WORKER_INT", "42")
    assert main._read_int_env("TEST_WORKER_INT", default=7, min_value=1) == 42

    monkeypatch.setenv("TEST_WORKER_INT", "0")
    assert main._read_int_env("TEST_WORKER_INT", default=7, min_value=1) == 1

    monkeypatch.setenv("TEST_WORKER_INT", "-5")
    assert main._read_int_env("TEST_WORKER_INT", default=7, min_value=3) == 3


def test_read_int_env_uses_default_for_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("TEST_WORKER_INT", "not-an-int")
    assert main._read_int_env("TEST_WORKER_INT", default=9, min_value=1) == 9
