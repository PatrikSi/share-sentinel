import gzip
import io
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import main


@pytest.fixture(autouse=True)
def _bypass_integrity_for_legacy_stream_fakes(monkeypatch):
    """Keep legacy process tests focused; integrity has dedicated real-byte tests."""

    monkeypatch.setattr(main, "_require_artifact_integrity", lambda *_args: (0, "0" * 64))

    @contextmanager
    def _open_unverified(key, _expected, **_kwargs):
        with main.open_artifact_stream(key) as body:
            yield body

    monkeypatch.setattr(main, "open_verified_artifact_stream", _open_unverified)
    monkeypatch.setattr(main, "verify_artifact_integrity", lambda *_args, **_kwargs: None)


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

    def fetchall(self):
        if self._row is None:
            return []
        if isinstance(self._row, list):
            return self._row
        return [self._row]


class _FakeConn:
    def __init__(self, run_row):
        self._run_row = run_row
        self._unlocked = False
        self.collection_context_updates: list[tuple[object, ...] | None] = []
        self.rollback_calls = 0
        self.commit_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        if "pg_try_advisory_lock" in query:
            return _FakeResult((True,))
        if "structural_rejected_records" in query:
            context = (
                self._run_row[7]
                if self._run_row is not None and len(self._run_row) > 7 and isinstance(self._run_row[7], dict)
                else {}
            )
            return _FakeResult((context, 0, 0, 0))
        if "FROM scan_runs" in query:
            return _FakeResult(self._run_row)
        if "SELECT COUNT(*) FROM endpoints" in query:
            return _FakeResult((0, 0, 0, 0))
        if "UPDATE scan_runs" in query and "collection_context" in query:
            self.collection_context_updates.append(_params)
            return _FakeResult(None)
        if "pg_advisory_unlock" in query:
            self._unlocked = True
            return _FakeResult((True,))
        raise AssertionError(f"unexpected query: {query}")

    def rollback(self):
        self.rollback_calls += 1

    def commit(self):
        self.commit_calls += 1


class _BusyConn(_FakeConn):
    def execute(self, query, _params=None):
        if "pg_try_advisory_lock" in query:
            return _FakeResult((False,))
        raise AssertionError(f"unexpected query: {query}")


def test_process_job_returns_busy_when_another_worker_holds_the_lock(monkeypatch) -> None:
    fake_conn = _BusyConn(run_row=None)
    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)

    result = main.process_job({"run_id": "11111111-1111-1111-1111-111111111111"})

    assert result == "busy"


def test_process_job_skips_failed_runs_without_touching_s3(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact-key", "FAILED", {}, {"line_offset": 0}, "application/x-ndjson", 1)
    fake_conn = _FakeConn(run_row)

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("failed runs must not fetch artifacts")),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact-key"})
    assert fake_conn._unlocked is True
    assert result == "ignored"


def test_process_job_defers_uploaded_run_until_scheduled_retry_is_due(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    next_retry_at = (datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()
    run_row = (
        project_id,
        "artifact.ndjson",
        "UPLOADED",
        {},
        {"line_offset": 12, "attempt_count": 1, "next_retry_at": next_retry_at},
        "application/x-ndjson",
        64,
    )
    fake_conn = _FakeConn(run_row)
    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deferred run must not read its artifact")),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "deferred"
    assert main.should_ack_stream_result(result) is True
    assert fake_conn.commit_calls == 0
    assert fake_conn._unlocked is True


class _FakeBody:
    def __init__(self, lines: list[str | bytes]):
        self._lines = [line if isinstance(line, bytes) else line.encode("utf-8") for line in lines]
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        for line in self._lines:
            yield line

    def readline(self, size: int = -1):
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        if size >= 0 and len(line) > size:
            head = line[:size]
            self._lines[self._index] = line[size:]
            return head
        self._index += 1
        return line

    def close(self):
        return None


def _framed_lines(
    run_id: str,
    *records: str | bytes,
    stats: dict[str, int] | None = None,
) -> list[str | bytes]:
    run_meta = (
        f'{{"type":"run_meta","schema_version":1,"tool":"test-collector",'
        f'"tool_version":"1.0.0","run_id":"{run_id}",'
        '"started_at":"2026-01-01T00:00:00Z"}\n'
    )
    summary = stats or {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}
    run_end = (
        f'{{"type":"run_end","run_id":"{run_id}","finished_at":"2026-01-01T00:00:01Z","stats":{json.dumps(summary)}}}\n'
    )
    return [run_meta, *records, run_end]


def test_process_job_rejects_invalid_framing_before_inventory_and_clears_resume_state(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (
        project_id,
        "artifact.ndjson",
        "INGESTING",
        {"endpoints": 1, "resources": 1, "items": 8, "errors": 0},
        {"line_offset": 10},
        "application/x-ndjson",
        256,
    )
    fake_conn = _FakeConn(run_row)
    events: list[object] = []
    trailing = f'{{"type":"endpoint","run_id":"{run_id}","endpoint_key":"host:445"}}\n'
    invalid_lines = [*_framed_lines(run_id), trailing]

    def _update(_conn, _run_id, status, offset, summary, last_error=None, **_kwargs):
        events.append(("status", status, offset, dict(summary), last_error))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "open_artifact_stream", lambda *_args: _FakeBody(invalid_lines))
    monkeypatch.setattr(main, "clear_persisted_ingest_inventory", lambda *_args: events.append("cleared"))
    monkeypatch.setattr(
        main,
        "upsert_endpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("preflight must run before inventory writes")),
    )
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "failed"
    assert events[0][:4] == (
        "status",
        "INGESTING",
        10,
        {"endpoints": 1, "resources": 1, "items": 8, "errors": 0},
    )
    assert events[1] == "cleared"
    assert events[2][0:4] == (
        "status",
        "FAILED",
        0,
        {"endpoints": 0, "resources": 0, "items": 0, "errors": 0},
    )
    assert "records follow run_end" in events[2][4]
    assert fake_conn.rollback_calls == 1


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
    monkeypatch.setattr(
        main, "upsert_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg.DataError("bad access level"))
    )
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: _FakeBody(
            _framed_lines(
                run_id,
                '{"type":"resource","run_id":"11111111-1111-1111-1111-111111111111","endpoint_key":"host:445","name":"Finance","access_level":"read_write"}',
            )
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
    monkeypatch.setattr(
        main, "upsert_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg.DataError("bad access level"))
    )
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: _FakeBody(
            _framed_lines(
                run_id,
                '{"type":"resource","run_id":"11111111-1111-1111-1111-111111111111","endpoint_key":"host:445","name":"Finance","access_level":"read_write"}',
            )
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
    monkeypatch.setattr(
        main, "upsert_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("artifact read failed"))
    )
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args, **_kwargs: _FakeBody(
            _framed_lines(
                run_id,
                '{"type":"resource","run_id":"11111111-1111-1111-1111-111111111111","endpoint_key":"host:445","name":"Finance","access_level":"read_write"}',
            )
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


def test_process_job_uses_database_artifact_over_stale_queue_payload(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    database_project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (
        database_project_id,
        "current-artifact.ndjson",
        "UPLOADED",
        {},
        {"line_offset": 0},
        "application/x-ndjson",
        0,
    )
    fake_conn = _FakeConn(run_row)
    opened_keys: list[str] = []
    audit_projects: list[str] = []

    def _open(key: str):
        opened_keys.append(key)
        return _FakeBody(_framed_lines(run_id))

    def _audit(_conn, project_id, *_args, **_kwargs):
        audit_projects.append(project_id)

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "open_artifact_stream", _open)
    monkeypatch.setattr(main, "update_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "write_audit", _audit)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job(
        {
            "run_id": run_id,
            "project_id": "33333333-3333-3333-3333-333333333333",
            "artifact_key": "superseded-artifact.json",
        }
    )

    assert result == "complete"
    assert opened_keys == ["current-artifact.ndjson", "current-artifact.ndjson"]
    assert audit_projects == [database_project_id, database_project_id]


def test_process_job_terminalizes_unexpected_poison_exception(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 64)
    fake_conn = _FakeConn(run_row)
    status_updates: list[tuple[str, str | None]] = []

    def _update(_conn, _run_id, status, *_args, **kwargs):
        status_updates.append((status, kwargs.get("last_error")))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(
        main, "open_artifact_stream", lambda *_args, **_kwargs: (_ for _ in ()).throw(AttributeError("poison"))
    )
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "failed"
    assert status_updates == [("INGESTING", None), ("FAILED", "unexpected ingest failure")]
    assert fake_conn.rollback_calls == 1


def test_process_job_terminalizes_corrupt_gzip_without_scheduling_retry(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson.gz", "UPLOADED", {}, {"line_offset": 0}, "application/gzip", 8)
    fake_conn = _FakeConn(run_row)
    status_updates: list[tuple[str, str | None]] = []

    def _update(_conn, _run_id, status, *_args, **kwargs):
        status_updates.append((status, kwargs.get("last_error")))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "open_artifact_stream", lambda *_args, **_kwargs: io.BytesIO(b"\x1f\x8bcorrupt"))
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson.gz"})

    assert result == "failed"
    assert status_updates == [("INGESTING", None), ("FAILED", main.INVALID_GZIP_ARTIFACT_ERROR)]


def test_process_job_terminalizes_oversized_ndjson_record(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 9)
    fake_conn = _FakeConn(run_row)
    status_updates: list[tuple[str, str | None]] = []

    def _update(_conn, _run_id, status, *_args, **kwargs):
        status_updates.append((status, kwargs.get("last_error")))

    monkeypatch.setattr(main, "INGEST_MAX_RECORD_BYTES", 8)
    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "open_artifact_stream", lambda *_args, **_kwargs: _FakeBody(["x" * 9]))
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "failed"
    assert status_updates == [("INGESTING", None), ("FAILED", main.NDJSON_RECORD_TOO_LARGE_ERROR)]


@pytest.mark.parametrize(
    ("artifact_key", "content_type", "payload"),
    [
        ("artifact.json", "application/json", b'{"x":"123456789"}'),
        ("artifact.json.gz", "application/gzip", gzip.compress(b'{"x":"123456789"}')),
    ],
)
def test_process_job_bounds_compact_json_materialization(monkeypatch, artifact_key, content_type, payload) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, artifact_key, "UPLOADED", {}, {"line_offset": 0}, content_type, len(payload))
    fake_conn = _FakeConn(run_row)
    status_updates: list[tuple[str, str | None]] = []

    def _update(_conn, _run_id, status, *_args, **kwargs):
        status_updates.append((status, kwargs.get("last_error")))

    monkeypatch.setattr(main, "JSON_COMPAT_MAX_BYTES", 8)
    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "open_artifact_stream", lambda *_args: io.BytesIO(payload))
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": artifact_key})

    assert result == "failed"
    assert status_updates == [("INGESTING", None), ("FAILED", main.JSON_COMPAT_LIMIT_ERROR)]


def test_process_job_resume_uses_existing_resource_without_downgrading_access(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    endpoint_key = "host:445"
    resource_key = (endpoint_key, "Finance", "smb_share")
    run_row = (
        project_id,
        "artifact.ndjson",
        "UPLOADED",
        {"endpoints": 1, "resources": 1, "items": 0, "errors": 0},
        {"line_offset": 3, "attempt_count": 1},
        "application/x-ndjson",
        256,
    )
    fake_conn = _FakeConn(run_row)
    inserted_item_rows: list[tuple] = []

    def _flush_items(_conn, rows):
        inserted_item_rows.extend(rows)
        rows.clear()

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "load_resume_caches", lambda *_args: ({endpoint_key: 7}, {resource_key: 8}))
    monkeypatch.setattr(
        main,
        "upsert_resource",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing readable resource must not be overwritten")
        ),
    )
    monkeypatch.setattr(main, "flush_item_batch", _flush_items)
    monkeypatch.setattr(
        main, "load_persisted_summary", lambda *_args: {"endpoints": 1, "resources": 1, "items": 1, "errors": 0}
    )
    monkeypatch.setattr(main, "update_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args: _FakeBody(
            _framed_lines(
                run_id,
                f'{{"type":"endpoint","run_id":"{run_id}","endpoint_key":"{endpoint_key}"}}\n',
                f'{{"type":"resource","run_id":"{run_id}","endpoint_key":"{endpoint_key}","name":"Finance","resource_type":"smb_share","access_level":"readable"}}\n',
                f'{{"type":"item","run_id":"{run_id}","endpoint_key":"{endpoint_key}","resource_name":"Finance","resource_type":"smb_share","path":"\\\\report.txt","name":"report.txt","is_dir":false}}\n',
            )
        ),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "complete"
    assert len(inserted_item_rows) == 1
    assert inserted_item_rows[0][1] == 8


def test_process_job_routes_out_of_order_sharepoint_item_by_drive_identity(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    endpoint_key = "sharepoint:site-1"
    run_row = (
        project_id,
        "artifact.ndjson",
        "UPLOADED",
        {},
        {"line_offset": 0},
        "application/x-ndjson",
        256,
    )
    fake_conn = _FakeConn(run_row)
    resource_calls: list[dict[str, Any]] = []
    item_rows: list[tuple] = []
    error_rows: list[tuple] = []

    def _upsert_resource(_conn, _run_id, _endpoint_id, record):
        resource_calls.append(dict(record))
        return 8

    def _flush_items(_conn, rows):
        item_rows.extend(rows)
        rows.clear()

    def _flush_errors(_conn, rows):
        error_rows.extend(rows)
        rows.clear()

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "upsert_endpoint", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(main, "upsert_resource", _upsert_resource)
    monkeypatch.setattr(main, "flush_item_batch", _flush_items)
    monkeypatch.setattr(main, "flush_error_batch", _flush_errors)
    monkeypatch.setattr(
        main,
        "load_persisted_summary",
        lambda *_args: {"endpoints": 1, "resources": 1, "items": 1, "errors": 0},
    )
    monkeypatch.setattr(main, "update_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args: _FakeBody(
            _framed_lines(
                run_id,
                (
                    f'{{"type":"item","run_id":"{run_id}",'
                    f'"endpoint_key":"{endpoint_key}","resource_name":"Documents",'
                    '"share_type":"sharepoint","provider":"sharepoint",'
                    '"provider_resource_id":"drive-1","provider_item_id":"item-1",'
                    '"path":"/report.txt","name":"report.txt","is_dir":false}\n'
                ),
                (
                    f'{{"type":"resource","run_id":"{run_id}",'
                    f'"endpoint_key":"{endpoint_key}","name":"Documents",'
                    '"share_type":"sharepoint","provider":"sharepoint",'
                    '"provider_resource_id":"drive-1","access_level":"list_only"}\n'
                ),
            )
        ),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "complete"
    assert error_rows == []
    assert [record["provider_resource_id"] for record in resource_calls] == ["drive-1", "drive-1"]
    assert len(item_rows) == 1
    assert item_rows[0][1] == 8
    assert item_rows[0][13] == "item-1"


def test_process_job_records_invalid_utf8_without_persisting_corrupted_inventory(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 32)
    fake_conn = _FakeConn(run_row)
    captured_errors: list[tuple] = []

    def _flush_errors(_conn, rows):
        captured_errors.extend(rows)
        rows.clear()

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args: _FakeBody(_framed_lines(run_id, b'{"type":"endpoint","hostname":"bad\xff"}\n')),
    )
    monkeypatch.setattr(
        main,
        "upsert_endpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid UTF-8 must not be persisted")),
    )
    monkeypatch.setattr(main, "flush_error_batch", _flush_errors)
    monkeypatch.setattr(
        main, "load_persisted_summary", lambda *_args: {"endpoints": 0, "resources": 0, "items": 0, "errors": 1}
    )
    monkeypatch.setattr(main, "update_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "complete"
    assert len(captured_errors) == 1
    assert captured_errors[0][2] == "UTF8_DECODE_ERROR"
    assert "byte offset" in captured_errors[0][3]


def test_process_job_checkpoints_and_heartbeats_for_every_consumed_ndjson_line(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 32)
    fake_conn = _FakeConn(run_row)
    captured_errors: list[tuple] = []
    status_updates: list[tuple[str, int, dict[str, int]]] = []
    heartbeats: list[tuple[str, int | None]] = []

    def _flush_errors(_conn, rows):
        captured_errors.extend(rows)
        rows.clear()

    def _update(_conn, _run_id, status, line_offset, summary, **_kwargs):
        status_updates.append((status, line_offset, dict(summary)))

    def _heartbeat(status, **kwargs):
        heartbeats.append((status, kwargs.get("line_offset")))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args: _FakeBody(
            _framed_lines(
                run_id,
                b'{"type":"endpoint","hostname":"bad\xff"}\n',
                b"   \n",
                b"{not-json}\n",
            )
        ),
    )
    monkeypatch.setattr(main, "PROGRESS_EVERY_LINES", 1)
    monkeypatch.setattr(main, "WORKER_HEARTBEAT_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main, "flush_error_batch", _flush_errors)
    monkeypatch.setattr(
        main,
        "load_persisted_summary",
        lambda *_args: {"endpoints": 0, "resources": 0, "items": 0, "errors": 2},
    )
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", _heartbeat)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "complete"
    assert [row[2] for row in captured_errors] == ["UTF8_DECODE_ERROR", "JSON_DECODE_ERROR"]
    assert [(status, offset) for status, offset, _summary in status_updates] == [
        ("INGESTING", 0),
        ("INGESTING", 1),
        ("INGESTING", 2),
        ("INGESTING", 3),
        ("INGESTING", 4),
        ("INGESTING", 5),
        ("COMPLETE", 5),
    ]
    # The framing preflight now emits additional offset-zero heartbeats. The
    # processing pass must still report every durable physical-line offset.
    assert len(heartbeats) > 7
    assert heartbeats[-7:] == [
        ("processing", 0),
        ("processing", 1),
        ("processing", 2),
        ("processing", 3),
        ("processing", 4),
        ("processing", 5),
        ("idle", None),
    ]


def test_process_job_records_non_object_ndjson_and_continues(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 64)
    fake_conn = _FakeConn(run_row)
    captured_errors: list[tuple] = []
    endpoint_records: list[dict[str, Any]] = []

    def _flush_errors(_conn, rows):
        captured_errors.extend(rows)
        rows.clear()

    def _upsert_endpoint(_conn, _run_id, record):
        endpoint_records.append(record)
        return 7

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args: _FakeBody(
            _framed_lines(
                run_id,
                "[1,2,3]\n",
                f'{{"type":"endpoint","run_id":"{run_id}","endpoint_key":"host:445"}}\n',
            )
        ),
    )
    monkeypatch.setattr(main, "upsert_endpoint", _upsert_endpoint)
    monkeypatch.setattr(main, "flush_error_batch", _flush_errors)
    monkeypatch.setattr(
        main,
        "load_persisted_summary",
        lambda *_args: {"endpoints": 1, "resources": 0, "items": 0, "errors": 1},
    )
    monkeypatch.setattr(main, "update_run_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "complete"
    assert len(endpoint_records) == 1
    assert len(captured_errors) == 1
    assert captured_errors[0][2] == main.CONSUMER_UNCLASSIFIED_RECORD_ERROR
    assert captured_errors[0][3] == "record must be a JSON object"


def test_process_job_uses_persisted_summary_instead_of_producer_claim(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 128)
    fake_conn = _FakeConn(run_row)
    complete_summaries: list[dict[str, int]] = []
    authoritative = {"endpoints": 1, "resources": 2, "items": 3, "errors": 4}

    def _update(_conn, _run_id, status, _offset, summary, **_kwargs):
        if status == "COMPLETE":
            complete_summaries.append(dict(summary))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(
        main,
        "open_artifact_stream",
        lambda *_args: _FakeBody(
            _framed_lines(
                run_id,
                stats={"endpoints": 999, "resources": 999, "items": 999, "errors": 999},
            )
        ),
    )
    monkeypatch.setattr(main, "load_persisted_summary", lambda *_args: authoritative)
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})

    assert result == "complete"
    assert complete_summaries == [authoritative]


def test_process_job_propagates_failure_before_authoritative_run_state_is_loaded(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"

    class _SelectFailConn(_FakeConn):
        def execute(self, query, params=None):
            if "FROM scan_runs" in query:
                raise psycopg.DataError("could not load run row")
            return super().execute(query, params)

    fake_conn = _SelectFailConn(None)
    status_updates: list[tuple[str, int, str | None]] = []

    def _update(_conn, _run_id, status, line_offset, _summary, last_error=None, **_kwargs):
        status_updates.append((status, line_offset, last_error))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    with pytest.raises(psycopg.DataError, match="could not load run row"):
        main.process_job({"run_id": run_id, "project_id": project_id})

    assert status_updates == []
    assert fake_conn.rollback_calls == 1
    assert fake_conn._unlocked is True


def test_process_job_propagates_connection_failure_for_stream_redelivery(monkeypatch) -> None:
    def _connect(*_args, **_kwargs):
        raise psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(main.psycopg, "connect", _connect)

    with pytest.raises(psycopg.OperationalError, match="database unavailable"):
        main.process_job({"run_id": "11111111-1111-1111-1111-111111111111"})


def test_process_job_checkpoints_without_failure_on_worker_shutdown(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    run_row = (project_id, "artifact.ndjson", "UPLOADED", {}, {"line_offset": 0}, "application/x-ndjson", 64)
    fake_conn = _FakeConn(run_row)
    status_updates: list[tuple[str, int, dict[str, Any]]] = []
    audit_actions: list[str] = []
    artifact_open_calls = 0

    def _update(_conn, _run_id, status, line_offset, _summary, **kwargs):
        status_updates.append((status, line_offset, kwargs.get("extra_progress", {})))

    def _audit(_conn, _project_id, action, *_args, **_kwargs):
        audit_actions.append(action)

    def _open_artifact(*_args):
        nonlocal artifact_open_calls
        artifact_open_calls += 1
        return _FakeBody(
            _framed_lines(run_id, f'{{"type":"endpoint","run_id":"{run_id}","endpoint_key":"host:445"}}\n')
        )

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setattr(main, "open_artifact_stream", _open_artifact)
    monkeypatch.setattr(
        main,
        "upsert_endpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("shutdown must happen before the next record")),
    )
    monkeypatch.setattr(main, "update_run_status", _update)
    monkeypatch.setattr(main, "write_audit", _audit)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)
    main._shutdown_event.set()
    try:
        result = main.process_job({"run_id": run_id, "project_id": project_id, "artifact_key": "artifact.ndjson"})
    finally:
        main._shutdown_event.clear()

    assert result == "shutdown"
    assert main.should_ack_stream_result(result) is True
    assert [(status, offset) for status, offset, _ in status_updates] == [("INGESTING", 0), ("UPLOADED", 0)]
    assert status_updates[-1][2]["pause_reason"] == "worker_shutdown"
    assert audit_actions == ["INGEST_STARTED", "INGEST_PAUSED"]
    assert artifact_open_calls == 1
    assert fake_conn.commit_calls == 2
    assert fake_conn.rollback_calls == 0
    assert fake_conn._unlocked is True
