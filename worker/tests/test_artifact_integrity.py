import gzip
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import main


class _Result:
    def __init__(self, row: Any):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, run_row: tuple[Any, ...]):
        self.run_row = run_row
        self.commit_calls = 0
        self.rollback_calls = 0
        self.unlocked = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        if "pg_try_advisory_lock" in query:
            return _Result((True,))
        if "structural_rejected_records" in query:
            context = self.run_row[7] if len(self.run_row) > 7 and isinstance(self.run_row[7], dict) else {}
            return _Result((context, 0, 0, 0))
        if "FROM scan_runs" in query:
            return _Result(self.run_row)
        if "UPDATE scan_runs" in query and "collection_context" in query:
            return _Result(None)
        if "pg_advisory_unlock" in query:
            self.unlocked = True
            return _Result((True,))
        raise AssertionError(f"unexpected query: {query}")

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


def _artifact(run_id: str, endpoint_key: str) -> bytes:
    records = [
        {
            "type": "run_meta",
            "schema_version": 1,
            "tool": "integrity-test",
            "tool_version": "1.0.0",
            "run_id": run_id,
            "started_at": "2026-01-01T00:00:00Z",
        },
        {"type": "endpoint", "run_id": run_id, "endpoint_key": endpoint_key},
        {
            "type": "run_end",
            "run_id": run_id,
            "finished_at": "2026-01-01T00:00:01Z",
            "stats": {"endpoints": 1, "resources": 0, "items": 0, "errors": 0},
        },
    ]
    return b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)


def _nested_json_artifact(run_id: str) -> bytes:
    return json.dumps(
        {
            "meta": {
                "run_id": run_id,
                "tool": "integrity-test",
                "tool_version": "1.0.0",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
            },
            "endpoints": [{"endpoint_key": "host-a:445", "shares": []}],
            "summary": {"endpoints": 1, "resources": 0, "items": 0, "errors": 0},
        },
        separators=(",", ":"),
    ).encode()


def _install_process_fakes(monkeypatch, connection: _Connection):
    statuses: list[tuple[str, int, dict[str, Any]]] = []
    audits: list[tuple[str, dict[str, Any]]] = []
    clears: list[str] = []

    def _status(_conn, _run_id, status, offset, _summary, **kwargs):
        statuses.append((status, offset, kwargs))

    def _audit(_conn, _project_id, action, _object_type, _object_id, metadata):
        audits.append((action, metadata))

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(main, "update_run_status", _status)
    monkeypatch.setattr(main, "write_audit", _audit)
    monkeypatch.setattr(main, "clear_persisted_ingest_inventory", lambda _conn, run_id: clears.append(run_id))
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)
    return statuses, audits, clears


@pytest.mark.parametrize(
    ("size", "digest"),
    [
        (None, "a" * 64),
        (-1, "a" * 64),
        (True, "a" * 64),
        (1, None),
        (1, "short"),
        (1, "z" * 64),
    ],
)
def test_artifact_integrity_metadata_is_required_and_well_formed(size, digest) -> None:
    with pytest.raises(main.ArtifactIntegrityError, match="integrity metadata"):
        main._require_artifact_integrity(size, digest)


def test_verified_stream_hashes_remaining_bytes_in_bounded_chunks(monkeypatch) -> None:
    payload = b"x" * (main.ARTIFACT_INTEGRITY_READ_CHUNK_BYTES * 2 + 17)

    class _RecordingBody(io.BytesIO):
        def __init__(self, value: bytes):
            super().__init__(value)
            self.requests: list[int] = []

        def read(self, size: int = -1):
            self.requests.append(size)
            return super().read(size)

    body = _RecordingBody(payload)
    monkeypatch.setattr(main, "open_artifact_stream", lambda _key: body)
    expected = (len(payload), hashlib.sha256(payload).hexdigest())

    with main.open_verified_artifact_stream("artifact.ndjson", expected) as stream:
        assert stream.read(3) == b"xxx"

    drain_requests = [size for size in body.requests[1:] if size > 0]
    assert drain_requests
    assert max(drain_requests) <= main.ARTIFACT_INTEGRITY_READ_CHUNK_BYTES


@pytest.mark.parametrize(
    "expected",
    [
        (6, hashlib.sha256(b"payload").hexdigest()),
        (7, "0" * 64),
    ],
)
def test_verified_stream_rejects_size_or_digest_mismatch(monkeypatch, expected) -> None:
    monkeypatch.setattr(main, "open_artifact_stream", lambda _key: io.BytesIO(b"payload"))

    with pytest.raises(main.ArtifactIntegrityError, match="stored bytes no longer match"):
        with main.open_verified_artifact_stream("artifact.ndjson", expected):
            pass


def test_gzip_framing_pass_authenticates_raw_compressed_bytes(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    payload = gzip.compress(_artifact(run_id, "host-a:445"))
    expected = (len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(main, "open_artifact_stream", lambda _key: io.BytesIO(payload))

    main._validate_artifact_framing(
        "artifact.ndjson.gz",
        "application/gzip",
        len(payload),
        run_id,
        expected,
    )


@pytest.mark.parametrize("gzip_input", [False, True])
def test_rewindable_json_framing_authenticates_each_raw_byte_once(monkeypatch, gzip_input) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    raw_json = _nested_json_artifact(run_id)
    payload = gzip.compress(raw_json) if gzip_input else raw_json
    suffix = ".json.gz" if gzip_input else ".json"
    content_type = "application/gzip" if gzip_input else "application/json"
    expected = (len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(main, "open_artifact_stream", lambda _key: io.BytesIO(payload))

    main._validate_artifact_framing(
        f"artifact{suffix}",
        content_type,
        len(payload),
        run_id,
        expected,
    )


def test_resume_rejects_changed_artifact_and_discards_checkpointed_rows(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    original = _artifact(run_id, "host-a:445")
    changed = _artifact(run_id, "host-b:445")
    assert len(changed) == len(original)
    run_row = (
        project_id,
        "artifact.ndjson",
        "UPLOADED",
        {"endpoints": 1, "resources": 0, "items": 0, "errors": 0},
        {"line_offset": 2, "attempt_count": 1},
        "application/x-ndjson",
        len(original),
        {},
        hashlib.sha256(original).hexdigest(),
    )
    connection = _Connection(run_row)
    statuses, audits, clears = _install_process_fakes(monkeypatch, connection)
    monkeypatch.setattr(main, "open_artifact_stream", lambda _key: io.BytesIO(changed))
    monkeypatch.setattr(
        main,
        "upsert_endpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("mismatch must fail before normalization")),
    )

    result = main.process_job({"run_id": run_id, "project_id": project_id})

    assert result == "failed"
    assert clears == [run_id]
    assert [status for status, _offset, _kwargs in statuses] == ["INGESTING", "FAILED"]
    assert statuses[-1][1] == 0
    assert statuses[-1][2]["last_error"] == main.ARTIFACT_INTEGRITY_MISMATCH_ERROR
    assert statuses[-1][2]["extra_progress"]["failure_code"] == "ARTIFACT_INTEGRITY_FAILED"
    assert [action for action, _metadata in audits] == ["INGEST_STARTED", "INGEST_FAILED"]
    assert audits[-1][1]["failure_code"] == "ARTIFACT_INTEGRITY_FAILED"
    assert connection.unlocked is True


def test_path_mutation_after_processing_cannot_publish_complete(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    original = _artifact(run_id, "host-a:445")
    changed = _artifact(run_id, "host-b:445")
    assert len(changed) == len(original)
    run_row = (
        project_id,
        "artifact.ndjson",
        "UPLOADED",
        {},
        {"line_offset": 0},
        "application/x-ndjson",
        len(original),
        {},
        hashlib.sha256(original).hexdigest(),
    )
    connection = _Connection(run_row)
    statuses, _audits, clears = _install_process_fakes(monkeypatch, connection)
    payloads = iter((original, original, changed))
    normalized_endpoints: list[str] = []
    monkeypatch.setattr(main, "open_artifact_stream", lambda _key: io.BytesIO(next(payloads)))
    monkeypatch.setattr(
        main,
        "upsert_endpoint",
        lambda _conn, _run_id, record: normalized_endpoints.append(record["endpoint_key"]) or 7,
    )
    monkeypatch.setattr(
        main,
        "load_persisted_summary",
        lambda _conn, _run_id: {"endpoints": 1, "resources": 0, "items": 0, "errors": 0},
    )
    monkeypatch.setattr(main, "PROGRESS_EVERY_LINES", 1)

    result = main.process_job({"run_id": run_id, "project_id": project_id})

    assert result == "failed"
    assert normalized_endpoints == ["host-a:445"]
    assert clears == [run_id]
    assert "COMPLETE" not in [status for status, _offset, _kwargs in statuses]
    assert statuses[-1][0] == "FAILED"
    assert statuses[-1][1] == 0
    assert connection.commit_calls > 1, "the regression must cover already-checkpointed normalized rows"


def test_mutation_that_causes_parse_failure_still_discards_checkpointed_rows(monkeypatch) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    first = _artifact(run_id, "host-a:445").splitlines(keepends=True)
    second_endpoint = (
        json.dumps(
            {"type": "endpoint", "run_id": run_id, "endpoint_key": "host-b:445"},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    original_lines = [first[0], first[1], second_endpoint, first[2]]
    original = b"".join(original_lines)
    changed = b"".join([*original_lines[:2], original_lines[2][:-1], b" ", original_lines[3]])
    assert len(changed) == len(original)
    record_limit = max(len(line) for line in original_lines)
    assert len(original_lines[2]) + len(original_lines[3]) - 1 > record_limit
    run_row = (
        project_id,
        "artifact.ndjson",
        "UPLOADED",
        {},
        {"line_offset": 0},
        "application/x-ndjson",
        len(original),
        {},
        hashlib.sha256(original).hexdigest(),
    )
    connection = _Connection(run_row)
    statuses, _audits, clears = _install_process_fakes(monkeypatch, connection)
    payloads = iter((original, changed))
    normalized_endpoints: list[str] = []
    monkeypatch.setattr(main, "open_artifact_stream", lambda _key: io.BytesIO(next(payloads)))
    monkeypatch.setattr(
        main,
        "upsert_endpoint",
        lambda _conn, _run_id, record: normalized_endpoints.append(record["endpoint_key"]) or 7,
    )
    monkeypatch.setattr(main, "INGEST_MAX_RECORD_BYTES", record_limit)
    monkeypatch.setattr(main, "PROGRESS_EVERY_LINES", 1)

    result = main.process_job({"run_id": run_id, "project_id": project_id})

    assert result == "failed"
    assert normalized_endpoints == ["host-a:445"]
    assert clears == [run_id]
    assert "COMPLETE" not in [status for status, _offset, _kwargs in statuses]
    assert statuses[-1][0:2] == ("FAILED", 0)
    assert statuses[-1][2]["last_error"] == main.ARTIFACT_INTEGRITY_MISMATCH_ERROR
    assert statuses[-1][2]["extra_progress"]["failure_code"] == "ARTIFACT_INTEGRITY_FAILED"
    assert connection.commit_calls > 1
