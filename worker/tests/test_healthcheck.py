from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import healthcheck


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
