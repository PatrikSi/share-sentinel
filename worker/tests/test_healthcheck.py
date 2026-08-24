import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import healthcheck


def test_read_timeout_falls_back_for_non_finite_values(monkeypatch) -> None:
    for non_finite in ("nan", "inf", "-inf"):
        monkeypatch.setenv("TEST_TIMEOUT", non_finite)
        assert healthcheck._read_timeout("TEST_TIMEOUT", 5.0) == 5.0


def test_check_redis_uses_bounded_socket_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Client:
        def ping(self):
            captured["ping"] = True

    def _from_url(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Client()

    monkeypatch.setattr(healthcheck.redis.Redis, "from_url", _from_url)

    healthcheck.check_redis("redis://cache:6379/0")

    assert captured["ping"] is True
    assert captured["url"] == "redis://cache:6379/0"
    assert captured["kwargs"] == {
        "decode_responses": True,
        "socket_connect_timeout": healthcheck.REDIS_CONNECT_TIMEOUT_SECONDS,
        "socket_timeout": healthcheck.REDIS_SOCKET_TIMEOUT_SECONDS,
    }


def test_check_database_uses_bounded_connect_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Result:
        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query: str):
            captured["query"] = query
            return _Result()

    def _connect(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Conn()

    monkeypatch.setattr(healthcheck.psycopg, "connect", _connect)

    healthcheck.check_database("postgresql+psycopg://db/share_sentinel")

    assert captured == {
        "url": "postgresql://db/share_sentinel",
        "kwargs": {
            "connect_timeout": healthcheck.WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS,
            "options": f"-c statement_timeout={healthcheck.WORKER_DATABASE_STATEMENT_TIMEOUT_MS}",
        },
        "query": "SELECT 1",
    }


def test_run_healthcheck_reports_all_checks_ok(tmp_path: Path, monkeypatch) -> None:
    heartbeat_path = tmp_path / "worker-heartbeat.json"
    heartbeat_path.write_text(json.dumps({"ts": datetime.now(tz=UTC).isoformat()}), encoding="utf-8")

    monkeypatch.setattr(healthcheck, "check_database", lambda _database_url: None)
    monkeypatch.setattr(healthcheck, "check_redis", lambda _redis_url: None)
    monkeypatch.setattr(healthcheck, "check_artifact_storage", lambda _artifact_storage_path: None)

    ok, checks = healthcheck.run_healthcheck("postgresql://db", "redis://redis", str(tmp_path), str(heartbeat_path), timeout_seconds=45)

    assert ok is True
    assert checks == {"heartbeat": "ok", "database": "ok", "redis": "ok", "artifact_storage": "ok"}


def test_run_healthcheck_reports_stale_heartbeat(tmp_path: Path, monkeypatch) -> None:
    heartbeat_path = tmp_path / "worker-heartbeat.json"
    heartbeat_path.write_text(json.dumps({"ts": (datetime.now(tz=UTC) - timedelta(seconds=60)).isoformat()}), encoding="utf-8")

    monkeypatch.setattr(healthcheck, "check_database", lambda _database_url: None)
    monkeypatch.setattr(healthcheck, "check_redis", lambda _redis_url: None)
    monkeypatch.setattr(healthcheck, "check_artifact_storage", lambda _artifact_storage_path: None)

    ok, checks = healthcheck.run_healthcheck("postgresql://db", "redis://redis", str(tmp_path), str(heartbeat_path), timeout_seconds=45)

    assert ok is False
    assert checks["heartbeat"] == "error"
    assert checks["database"] == "ok"
    assert checks["redis"] == "ok"
    assert checks["artifact_storage"] == "ok"


def test_run_healthcheck_reports_dependency_failures(tmp_path: Path, monkeypatch) -> None:
    heartbeat_path = tmp_path / "worker-heartbeat.json"
    heartbeat_path.write_text(json.dumps({"ts": datetime.now(tz=UTC).isoformat()}), encoding="utf-8")

    monkeypatch.setattr(healthcheck, "check_database", lambda _database_url: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(healthcheck, "check_redis", lambda _redis_url: (_ for _ in ()).throw(RuntimeError("redis down")))
    monkeypatch.setattr(healthcheck, "check_artifact_storage", lambda _artifact_storage_path: (_ for _ in ()).throw(RuntimeError("storage missing")))

    ok, checks = healthcheck.run_healthcheck("postgresql://db", "redis://redis", str(tmp_path), str(heartbeat_path), timeout_seconds=45)

    assert ok is False
    assert checks["heartbeat"] == "ok"
    assert checks["database"] == "error"
    assert checks["redis"] == "error"
    assert checks["artifact_storage"] == "error"


def test_main_uses_bounded_fallback_for_malformed_health_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _run_healthcheck(_database_url, _redis_url, _storage_path, _heartbeat_path, timeout_seconds):
        captured["timeout_seconds"] = timeout_seconds
        return True, {}

    monkeypatch.setenv("WORKER_HEALTH_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setattr(healthcheck, "run_healthcheck", _run_healthcheck)

    assert healthcheck.main() == 0
    assert captured["timeout_seconds"] == 45.0
