import gzip
import io
import json
from pathlib import Path
import pytest
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


def test_is_json_artifact_supports_json_and_json_gz() -> None:
    assert main._is_json_artifact("artifact.json", "application/json") is True
    assert main._is_json_artifact("artifact.json.gz", "application/gzip") is True
    assert main._is_json_artifact("artifact.ndjson", "application/x-ndjson") is False


def test_read_json_compat_bytes_rejects_payloads_over_limit() -> None:
    with pytest.raises(ValueError, match="compatibility limit"):
        main._read_json_compat_bytes(io.BytesIO(b"x" * 11), gzip_input=False, max_bytes=10)


def test_public_ingest_error_redacts_internal_failure_types() -> None:
    assert main._public_ingest_error(ValueError("unsupported JSON artifact format")) == "unsupported JSON artifact format"
    assert main._public_ingest_error(TypeError("bad shape")) == "artifact contained an unexpected record shape"
    assert main._public_ingest_error(RuntimeError("oops")) == "unexpected ingest failure"


def test_update_run_status_persists_heartbeat_timestamp() -> None:
    captured: dict[str, object] = {}

    class _Conn:
        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

    main.update_run_status(_Conn(), "run-1", "INGESTING", 12, {"items": 3}, last_error="broken")

    ingest_progress = json.loads(captured["params"][1])
    assert ingest_progress["line_offset"] == 12
    assert ingest_progress["last_error"] == "broken"
    assert "heartbeat_at" in ingest_progress


def test_read_json_compat_bytes_rejects_gzip_payloads_over_limit() -> None:
    payload = gzip.compress(b'{"hello":"' + (b"a" * 32) + b'"}')

    with pytest.raises(ValueError, match="JSON artifact exceeds non-streamable compatibility limit"):
        main._read_json_compat_bytes(io.BytesIO(payload), gzip_input=True, max_bytes=16)


def test_parse_offset_returns_zero_for_non_numeric_values() -> None:
    assert main.parse_offset({"line_offset": "12"}) == 12
    assert main.parse_offset({"line_offset": -7}) == 0
    assert main.parse_offset({"line_offset": "nan"}) == 0
    assert main.parse_offset({"line_offset": None}) == 0


def test_validate_record_requires_numeric_schema_version_for_run_meta() -> None:
    invalid, invalid_reason = main.validate_record(
        {
            "type": "run_meta",
            "schema_version": "x",
            "tool": "collector",
            "tool_version": "1.0",
            "run_id": "run-1",
            "started_at": "2026-01-01T00:00:00Z",
        }
    )
    assert invalid is False
    assert invalid_reason == "invalid schema_version"

    unsupported, unsupported_reason = main.validate_record(
        {
            "type": "run_meta",
            "schema_version": 2,
            "tool": "collector",
            "tool_version": "1.0",
            "run_id": "run-1",
            "started_at": "2026-01-01T00:00:00Z",
        }
    )
    assert unsupported is False
    assert unsupported_reason == "unsupported schema_version"


def test_validate_record_normalizes_share_type_and_resource_type() -> None:
    resource_record = {
        "type": "resource",
        "run_id": "run-1",
        "endpoint_key": "host:2049",
        "name": "/srv/public",
        "share_type": "NFS",
    }
    resource_ok, resource_reason = main.validate_record(resource_record)
    assert resource_ok is True
    assert resource_reason is None
    assert resource_record["share_type"] == "nfs"
    assert resource_record["resource_type"] == "nfs_share"

    item_record = {
        "type": "item",
        "run_id": "run-1",
        "endpoint_key": "host:2049",
        "resource_name": "/srv/public",
        "path": "/srv/public/file.txt",
        "resource_type": "nfs_share",
    }
    item_ok, item_reason = main.validate_record(item_record)
    assert item_ok is True
    assert item_reason is None
    assert item_record["share_type"] == "nfs"
    assert item_record["resource_type"] == "nfs_share"


def test_validate_record_normalizes_access_level_aliases() -> None:
    resource_record = {
        "type": "resource",
        "run_id": "run-1",
        "endpoint_key": "host:445",
        "name": "Finance",
        "access_level": "read_write",
    }
    ok, reason = main.validate_record(resource_record)

    assert ok is True
    assert reason is None
    assert resource_record["access_level"] == "readable"
    assert main._normalize_access_level("list") == "list_only"
    assert main._normalize_access_level("unknown-value") == "no_access"


def test_bind_record_to_ingest_run_overrides_mismatched_run_id() -> None:
    original = {"type": "endpoint", "run_id": "old-run", "endpoint_key": "host:445"}
    rebound = main._bind_record_to_ingest_run(original, "new-run")

    assert rebound["run_id"] == "new-run"
    assert original["run_id"] == "old-run"


def test_bind_record_to_ingest_run_keeps_matching_record_identity() -> None:
    record = {"type": "endpoint", "run_id": "run-1", "endpoint_key": "host:445"}
    same = main._bind_record_to_ingest_run(record, "run-1")

    assert same is record


def test_records_from_nested_json_preserves_share_type() -> None:
    run_id = str(uuid.uuid4())
    records = main.records_from_json_document(
        {
            "endpoints": [
                {
                    "endpoint_key": "host:2049",
                    "shares": [
                        {
                            "name": "/srv/public",
                            "share_type": "nfs",
                            "entries": [{"name": "docs", "is_dir": True, "children": [{"name": "file.txt", "is_dir": False}]}],
                        }
                    ],
                }
            ]
        },
        run_id,
    )

    resource = next(record for record in records if record.get("type") == "resource")
    item = next(record for record in records if record.get("type") == "item")

    assert resource["share_type"] == "nfs"
    assert resource["resource_type"] == "nfs_share"
    assert item["share_type"] == "nfs"
    assert item["resource_type"] == "nfs_share"


def test_records_from_compact_json_includes_run_meta_issue_summary_and_run_end() -> None:
    run_id = str(uuid.uuid4())
    records = main.records_from_json_document(
        {
            "schema_version": 1,
            "meta": {
                "tool": "share-sentinel-collector",
                "tool_version": "0.2.0",
                "run_id": run_id,
                "started_at": "2026-03-10T10:00:00Z",
                "finished_at": "2026-03-10T10:05:00Z",
                "auth": {"method": "anonymous"},
            },
            "collection": {
                "command": "share_sentinel_collector.py",
                "arguments": ["--cidr", "10.0.0.0/24"],
            },
            "summary": {
                "endpoints": 1,
                "resources": 1,
                "items": 1,
                "errors": 3,
            },
            "issue_summary": [
                {
                    "severity": "warn",
                    "code": "LIST_SHARES_DENIED",
                    "count": 3,
                    "sample_message": "share enumeration denied",
                    "sample_hint": "use include-share",
                }
            ],
            "endpoints": [
                {
                    "endpoint_key": "host:445",
                    "ip": "10.0.0.5",
                    "auth": {"method": "anonymous", "success": True},
                    "shares": [
                        {
                            "name": "Public",
                            "share_type": "smb",
                            "entries": [{"name": "report.txt", "is_dir": False}],
                        }
                    ],
                }
            ],
        },
        run_id,
    )

    run_meta = next(record for record in records if record.get("type") == "run_meta")
    error = next(record for record in records if record.get("type") == "error")
    run_end = next(record for record in records if record.get("type") == "run_end")

    assert run_meta["tool_version"] == "0.2.0"
    assert run_meta["collection"]["command"] == "share_sentinel_collector.py"
    assert error["code"] == "LIST_SHARES_DENIED"
    assert run_end["stats"]["errors"] == 3


def test_read_json_compat_bytes_rejects_oversized_input() -> None:
    try:
        main._read_json_compat_bytes(io.BytesIO(b"abcdef"), gzip_input=False, max_bytes=5)
    except ValueError as exc:
        assert str(exc) == "JSON artifact exceeds non-streamable compatibility limit"
    else:
        raise AssertionError("expected compatibility limit failure")


def test_public_ingest_error_redacts_storage_and_database_details() -> None:
    assert main._public_ingest_error(main.psycopg.DataError("bad access level")) == "database operation failed during ingest"
    assert main._public_ingest_error(OSError("disk missing")) == "artifact storage read failed during ingest"


def test_iter_records_from_streamable_json_file_streams_compact_json() -> None:
    run_id = str(uuid.uuid4())
    payload = {
        "schema_version": 1,
        "meta": {
            "tool": "share-sentinel-collector",
            "tool_version": "0.2.0",
            "run_id": run_id,
            "started_at": "2026-03-10T10:00:00Z",
            "finished_at": "2026-03-10T10:05:00Z",
        },
        "summary": {
            "endpoints": 1,
            "resources": 1,
            "items": 1,
            "errors": 0,
        },
        "endpoints": [
            {
                "endpoint_key": "host:445",
                "shares": [
                    {
                        "name": "Public",
                        "share_type": "smb",
                        "entries": [{"name": "report.txt", "is_dir": False}],
                    }
                ],
            }
        ],
    }

    stream = io.BytesIO(json.dumps(payload).encode("utf-8"))
    records = list(main._iter_records_from_streamable_json_file(stream, run_id))

    assert records[0]["type"] == "run_meta"
    assert any(record.get("type") == "endpoint" for record in records)
    assert any(record.get("type") == "resource" for record in records)
    assert records[-1]["type"] == "run_end"


def test_discover_recoverable_runs_scans_uploaded_and_stale_ingesting(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Result:
        def fetchall(self):
            return [("run-1", "project-1", "artifact-1")]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params
            return _Result()

    monkeypatch.setattr(main.psycopg, "connect", lambda *_args, **_kwargs: _Conn())

    runs = main.discover_recoverable_runs(limit=3)

    assert runs == [{"run_id": "run-1", "project_id": "project-1", "artifact_key": "artifact-1"}]
    assert "status = 'UPLOADED'" in captured["query"]
    assert "status = 'INGESTING'" in captured["query"]
    assert captured["params"] == (main.STALE_INGESTING_SECONDS, 3)
