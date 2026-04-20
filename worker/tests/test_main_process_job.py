from pathlib import Path
import sys
from typing import Any

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import main


def test_process_job_returns_early_for_invalid_run_id(monkeypatch) -> None:
    calls: list[str] = []

    def _unexpected_connect(*_args, **_kwargs):
        calls.append("connect")
        raise AssertionError("process_job should not connect for invalid run_id")

    monkeypatch.setattr(main.psycopg, "connect", _unexpected_connect)
    result = main.process_job({"run_id": "not-a-uuid", "project_id": "also-not-a-uuid"})
    assert calls == []
    assert result == "ignored"


class _FakeResult:
    def __init__(self, row: Any):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, run_row):
        self._run_row = run_row
        self._unlocked = False
        self.rollback_calls = 0
        self.commit_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        if "pg_try_advisory_lock" in query:
            return _FakeResult((True,))
        if "FROM scan_runs" in query:
            return _FakeResult(self._run_row)
        if "pg_advisory_unlock" in query:
            self._unlocked = True
            return _FakeResult((True,))
        raise AssertionError(f"unexpected query: {query}")

    def rollback(self):
        self.rollback_calls += 1

    def commit(self):
        self.commit_calls += 1


def test_process_job_skips_failed_runs_without_touching_s3(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact-key", "FAILED", {}, {"line_offset": 0}, "application/x-ndjson", 1)
    fake_conn = _FakeConn(run_row)

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "open_artifact_stream", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("failed runs must not fetch artifacts")))

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact-key"})
    assert fake_conn._unlocked is True
    assert result == "ignored"


class _FakeBody:
    def __init__(self, lines: list[str]):
        self._lines = [line.encode("utf-8") for line in lines]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        for line in self._lines:
            yield line

    def close(self):
        return None


def test_process_job_rolls_back_before_marking_failed(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 64)
    fake_conn = _FakeConn(run_row)
    status_updates: list[str] = []
    failed_errors: list[str | None] = []
    audit_actions: list[str] = []
    audit_metadata: list[dict[str, Any]] = []

    def _fake_update_run_status(conn, _run_id, status, *_args, **kwargs):
        if status == "FAILED":
            assert conn.rollback_calls >= 1
            failed_errors.append(kwargs.get("last_error"))
        status_updates.append(status)

    def _fake_write_audit(_conn, _project_id, action, *_args, **kwargs):
        audit_actions.append(action)
        audit_metadata.append(kwargs.get("metadata", {}))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "update_run_status", _fake_update_run_status)
    monkeypatch.setattr(main, "write_audit", _fake_write_audit)
    monkeypatch.setattr(main, "upsert_endpoint", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(main, "upsert_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg.DataError("bad access level")))
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: _FakeBody(
            [
                '{"type":"resource","run_id":"11111111-1111-1111-1111-111111111111","endpoint_key":"host:445","name":"Finance","access_level":"read_write"}'
            ]
        ),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "failed"
    assert fake_conn.rollback_calls == 1
    assert fake_conn._unlocked is True
    assert status_updates == ["INGESTING", "FAILED"]
    assert "INGEST_FAILED" in audit_actions


def test_process_job_persists_public_error_message_when_ingest_fails(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 64)
    fake_conn = _FakeConn(run_row)
    captured: dict[str, str] = {}

    def _fake_update_run_status(_conn, _run_id, status, *_args, last_error=None, **_kwargs):
        if status == "FAILED":
            captured["last_error"] = last_error

    def _fake_write_audit(_conn, _project_id, action, *_args, **kwargs):
        if action == "INGEST_FAILED":
            metadata = kwargs.get("metadata")
            if metadata is None and _args:
                metadata = _args[-1]
            captured["audit_error"] = metadata["error"]

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "update_run_status", _fake_update_run_status)
    monkeypatch.setattr(main, "write_audit", _fake_write_audit)
    monkeypatch.setattr(main, "upsert_endpoint", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(main, "upsert_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg.DataError("bad access level")))
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: _FakeBody(
            [
                '{"type":"resource","run_id":"11111111-1111-1111-1111-111111111111","endpoint_key":"host:445","name":"Finance","access_level":"read_write"}'
            ]
        ),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "failed"
    assert captured["last_error"] == "database operation failed during ingest"
    assert captured["audit_error"] == "database operation failed during ingest"


def test_process_job_schedules_retry_for_retryable_ingest_error(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 64)
    fake_conn = _FakeConn(run_row)
    status_updates: list[str] = []
    retry_metadata: dict[str, Any] = {}
    audit_actions: list[str] = []

    def _fake_update_run_status(conn, _run_id, status, *_args, **kwargs):
        status_updates.append(status)
        if status == "UPLOADED":
            assert conn.rollback_calls >= 1
            retry_metadata["last_error"] = kwargs.get("last_error")
            retry_metadata["extra_progress"] = kwargs.get("extra_progress", {})

    def _fake_write_audit(_conn, _project_id, action, *_args, **_kwargs):
        audit_actions.append(action)

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "update_run_status", _fake_update_run_status)
    monkeypatch.setattr(main, "write_audit", _fake_write_audit)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "upsert_endpoint", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(main, "upsert_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("artifact read failed")))
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: _FakeBody(
            [
                '{"type":"resource","run_id":"11111111-1111-1111-1111-111111111111","endpoint_key":"host:445","name":"Finance","access_level":"read_write"}'
            ]
        ),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "retry_scheduled"
    assert fake_conn.rollback_calls == 1
    assert fake_conn.commit_calls == 2
    assert fake_conn._unlocked is True
    assert status_updates == ["INGESTING", "UPLOADED"]
    assert retry_metadata["last_error"] == "artifact storage read failed during ingest"
    assert retry_metadata["extra_progress"]["attempt_count"] == 1
    assert "next_retry_at" in retry_metadata["extra_progress"]
    assert "INGEST_RETRY_SCHEDULED" in audit_actions
