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
    main.process_job({"run_id": "not-a-uuid", "project_id": "also-not-a-uuid"})
    assert calls == []


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
    monkeypatch.setattr(
        main.s3,
        "get_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("failed runs must not fetch artifacts")),
    )

    main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact-key"})
    assert fake_conn._unlocked is True


class _FakeBody:
    def __init__(self, lines: list[str]):
        self._lines = [line.encode("utf-8") for line in lines]

    def iter_lines(self):
        for line in self._lines:
            yield line


def test_process_job_rolls_back_before_marking_failed(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 64)
    fake_conn = _FakeConn(run_row)
    status_updates: list[str] = []
    audit_actions: list[str] = []

    def _fake_update_run_status(conn, _run_id, status, *_args, **_kwargs):
        if status == "FAILED":
            assert conn.rollback_calls >= 1
        status_updates.append(status)

    def _fake_write_audit(_conn, _project_id, action, *_args, **_kwargs):
        audit_actions.append(action)

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "update_run_status", _fake_update_run_status)
    monkeypatch.setattr(main, "write_audit", _fake_write_audit)
    monkeypatch.setattr(main, "upsert_endpoint", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(main, "upsert_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg.DataError("bad access level")))
    monkeypatch.setattr(
        main.s3,
        "get_object",
        lambda *_args, **_kwargs: {
            "Body": _FakeBody(
                [
                    '{"type":"resource","run_id":"11111111-1111-1111-1111-111111111111","endpoint_key":"host:445","name":"Finance","access_level":"read_write"}'
                ]
            )
        },
    )

    with pytest.raises(psycopg.DataError):
        main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert fake_conn.rollback_calls == 1
    assert fake_conn._unlocked is True
    assert status_updates == ["INGESTING", "FAILED"]
    assert "INGEST_FAILED" in audit_actions
