import gzip
import io
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


def test_should_ack_stream_result_skips_busy_only() -> None:
    assert main.should_ack_stream_result("complete") is True
    assert main.should_ack_stream_result("failed") is True
    assert main.should_ack_stream_result("retry_scheduled") is True
    assert main.should_ack_stream_result("deferred") is True
    assert main.should_ack_stream_result("ignored") is True
    assert main.should_ack_stream_result("busy") is False


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


def test_read_float_env_validates_and_enforces_minimum(monkeypatch) -> None:
    monkeypatch.setenv("TEST_WORKER_FLOAT", "2.5")
    assert main._read_float_env("TEST_WORKER_FLOAT", default=5.0) == 2.5

    monkeypatch.setenv("TEST_WORKER_FLOAT", "invalid")
    assert main._read_float_env("TEST_WORKER_FLOAT", default=5.0) == 5.0

    monkeypatch.setenv("TEST_WORKER_FLOAT", "0")
    assert main._read_float_env("TEST_WORKER_FLOAT", default=5.0, min_value=0.25) == 0.25

    for non_finite in ("nan", "inf", "-inf"):
        monkeypatch.setenv("TEST_WORKER_FLOAT", non_finite)
        assert main._read_float_env("TEST_WORKER_FLOAT", default=5.0) == 5.0


def test_is_json_artifact_supports_json_and_json_gz() -> None:
    assert main._is_json_artifact("artifact.json", "application/json") is True
    assert main._is_json_artifact("artifact.json.gz", "application/gzip") is True
    assert main._is_json_artifact("artifact.ndjson", "application/x-ndjson") is False


def test_read_json_compat_bytes_rejects_payloads_over_limit() -> None:
    with pytest.raises(ValueError, match="compatibility limit"):
        main._read_json_compat_bytes(io.BytesIO(b"x" * 11), gzip_input=False, max_bytes=10)


def test_load_json_records_rejects_invalid_utf8_without_replacement() -> None:
    with pytest.raises(ValueError, match=main.INVALID_UTF8_ARTIFACT_ERROR):
        main._load_json_records_from_bytes(b'{"hostname":"bad\xff"}', "run-1")


def test_public_ingest_error_redacts_internal_failure_types() -> None:
    assert main._public_ingest_error(ValueError("unsupported JSON artifact format")) == "unsupported JSON artifact format"
    assert main._public_ingest_error(ValueError(main.GZIP_DECOMPRESSED_LIMIT_ERROR)) == main.GZIP_DECOMPRESSED_LIMIT_ERROR
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


def test_limited_reader_rejects_gzip_payloads_over_limit() -> None:
    payload = gzip.compress(b"a" * 33)

    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as gzip_reader, main._LimitedReader(
        gzip_reader,
        max_bytes=32,
        error_message=main.GZIP_DECOMPRESSED_LIMIT_ERROR,
    ) as limited_reader:
        with pytest.raises(ValueError, match=main.GZIP_DECOMPRESSED_LIMIT_ERROR):
            limited_reader.read()


def test_limited_reader_does_not_double_count_repeated_streamable_json_passes() -> None:
    raw = b'{"meta":{"tool":"collector"},"endpoints":[]}'
    payload = gzip.compress(raw)

    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as gzip_reader, main._LimitedReader(
        gzip_reader,
        max_bytes=len(raw),
        error_message=main.GZIP_DECOMPRESSED_LIMIT_ERROR,
    ) as limited_reader:
        assert limited_reader.read() == raw
        limited_reader.seek(0)
        assert limited_reader.read() == raw


def test_bounded_ndjson_reader_rejects_limit_plus_one_without_unbounded_read() -> None:
    reader = io.BytesIO(b"x" * 9)

    with pytest.raises(ValueError, match=main.NDJSON_RECORD_TOO_LARGE_ERROR):
        list(main._iter_bounded_ndjson_lines(reader, max_record_bytes=8))

    assert reader.tell() == 9


def test_gzip_decompressed_limit_scales_from_artifact_size() -> None:
    assert main._gzip_decompressed_limit(1024) == max(main.JSON_COMPAT_MAX_BYTES, 1024 * main.GZIP_DECOMPRESSED_MAX_RATIO)


def test_parse_offset_returns_zero_for_non_numeric_values() -> None:
    assert main.parse_offset({"line_offset": "12"}) == 12
    assert main.parse_offset({"line_offset": -7}) == 0
    assert main.parse_offset({"line_offset": "nan"}) == 0
    assert main.parse_offset({"line_offset": None}) == 0


def test_parse_attempt_count_returns_zero_for_non_numeric_values() -> None:
    assert main.parse_attempt_count({"attempt_count": "3"}) == 3
    assert main.parse_attempt_count({"attempt_count": -1}) == 0
    assert main.parse_attempt_count({"attempt_count": "nan"}) == 0
    assert main.parse_attempt_count({"attempt_count": None}) == 0


def test_parse_next_retry_at_normalizes_valid_values_and_rejects_invalid_values() -> None:
    expected = datetime(2026, 8, 23, 12, 34, 56, tzinfo=UTC)
    assert main.parse_next_retry_at({"next_retry_at": "2026-08-23T12:34:56Z"}) == expected
    assert main.parse_next_retry_at({"next_retry_at": "2026-08-23T12:34:56"}) == expected
    assert main.parse_next_retry_at({"next_retry_at": "not-a-date"}) is None
    assert main.parse_next_retry_at({}) is None


def test_retry_backoff_seconds_grows_and_caps() -> None:
    assert main._retry_backoff_seconds(1) == main.INGEST_RETRY_BASE_SECONDS
    assert main._retry_backoff_seconds(2) == min(main.INGEST_RETRY_MAX_SECONDS, main.INGEST_RETRY_BASE_SECONDS * 2)
    assert main._retry_backoff_seconds(99) == main.INGEST_RETRY_MAX_SECONDS


def test_write_worker_heartbeat_persists_status_payload(tmp_path: Path, monkeypatch) -> None:
    heartbeat_path = tmp_path / "worker-heartbeat.json"
    monkeypatch.setattr(main, "WORKER_HEARTBEAT_PATH", str(heartbeat_path))

    main._write_worker_heartbeat("processing", run_id="run-1", line_offset=42)

    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert payload["status"] == "processing"
    assert payload["run_id"] == "run-1"
    assert payload["line_offset"] == 42
    assert "ts" in payload


def test_ingest_error_fingerprint_is_stable_for_same_payload() -> None:
    row_a = main.build_ingest_error_row("run-1", "error", "SCHEMA_INVALID", "bad record", "host:445", "share", "/a")
    row_b = main.build_ingest_error_row("run-1", "error", "SCHEMA_INVALID", "bad record", "host:445", "share", "/a")
    row_c = main.build_ingest_error_row("run-1", "error", "SCHEMA_INVALID", "different", "host:445", "share", "/a")

    assert row_a == row_b
    assert row_a[-1] != row_c[-1]
    assert len(row_a[-1]) == 32


def test_flush_error_batch_uses_fingerprint_deduplication() -> None:
    captured: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def executemany(self, query, params):
            captured["query"] = query
            captured["params"] = list(params)

    class _Conn:
        def cursor(self):
            return _Cursor()

    rows = [main.build_ingest_error_row("run-1", "error", "SCHEMA_INVALID", "bad record", "host:445", "share", "/a")]

    main.flush_error_batch(_Conn(), rows)

    assert "fingerprint" in str(captured["query"])
    assert "ON CONFLICT (run_id, fingerprint) DO NOTHING" in str(captured["query"])
    assert len(captured["params"][0]) == 8
    assert rows == []


def test_load_resume_caches_preserves_existing_resource_identity() -> None:
    class _Result:
        def fetchall(self):
            return [
                (7, "host:445", 8, "smb_share", "Finance"),
                (9, "host:2049", None, None, None),
            ]

    class _Conn:
        def execute(self, query, params):
            assert "LEFT JOIN resources" in query
            assert params == ("run-1",)
            return _Result()

    endpoints, resources = main.load_resume_caches(_Conn(), "run-1")

    assert endpoints == {"host:445": 7, "host:2049": 9}
    assert resources == {("host:445", "Finance", "smb_share"): 8}


def test_load_persisted_summary_counts_authoritative_rows() -> None:
    class _Result:
        def fetchone(self):
            return (2, 3, 5, 7)

    class _Conn:
        def execute(self, query, params):
            assert "SELECT COUNT(*) FROM endpoints" in query
            assert params == ("run-1", "run-1", "run-1", "run-1")
            return _Result()

    assert main.load_persisted_summary(_Conn(), "run-1") == {
        "endpoints": 2,
        "resources": 3,
        "items": 5,
        "errors": 7,
    }


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


def test_normalize_smb_signing_accepts_canonical_and_legacy_fields() -> None:
    assert main._normalize_smb_signing({"signing": "required"}) == "required"
    assert main._normalize_smb_signing({"signing": "not_required"}) == "not_required"
    assert main._normalize_smb_signing({"signing_required": True}) == "required"
    assert main._normalize_smb_signing({"signing_required": False}) == "not_required"
    assert main._normalize_smb_signing({}) is None


def test_validate_record_normalizes_optional_item_metadata() -> None:
    item_record = {
        "type": "item",
        "run_id": "run-1",
        "endpoint_key": "host:445",
        "resource_name": "Finance",
        "path": "\\report.xlsx",
        "name": "report.xlsx",
        "size_bytes": "1024",
        "mtime": "2026-08-23T12:34:56Z",
    }

    ok, reason = main.validate_record(item_record)

    assert ok is True
    assert reason is None
    assert item_record["size_bytes"] == 1024
    assert item_record["mtime"].isoformat() == "2026-08-23T12:34:56+00:00"

    item_record.update(size_bytes=-1, mtime="not-a-date")
    main.validate_record(item_record)
    assert item_record["size_bytes"] is None
    assert item_record["mtime"] is None


def test_validate_record_derives_item_name_from_windows_path() -> None:
    item_record = {
        "type": "item",
        "run_id": "run-1",
        "endpoint_key": "host:445",
        "resource_name": "Finance",
        "path": r"\reports\quarterly.xlsx",
    }

    ok, reason = main.validate_record(item_record)

    assert ok is True
    assert reason is None
    assert item_record["name"] == "quarterly.xlsx"


def test_flush_item_batch_upserts_optional_metadata() -> None:
    captured: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def executemany(self, query, params):
            captured["query"] = query
            captured["params"] = list(params)

    class _Conn:
        def cursor(self):
            return _Cursor()

    rows = [("run-1", 7, "\\report.xlsx", "report.xlsx", False, 1024, "2026-08-23T12:34:56Z")]
    main.flush_item_batch(_Conn(), rows)

    assert "size_bytes" in str(captured["query"])
    assert "mtime" in str(captured["query"])
    assert "DO UPDATE SET" in str(captured["query"])
    assert len(captured["params"][0]) == 7
    assert rows == []


def test_bind_record_to_ingest_run_overrides_mismatched_run_id() -> None:
    original = {"type": "endpoint", "run_id": "old-run", "endpoint_key": "host:445"}
    rebound = main._bind_record_to_ingest_run(original, "new-run")

    assert rebound["run_id"] == "new-run"
    assert original["run_id"] == "old-run"


def test_bind_record_to_ingest_run_keeps_matching_record_identity() -> None:
    record = {"type": "endpoint", "run_id": "run-1", "endpoint_key": "host:445"}
    same = main._bind_record_to_ingest_run(record, "run-1")

    assert same is record


def test_artifact_filename_suffix_is_authoritative_over_mislabeled_content_type() -> None:
    assert main._is_json_artifact("artifact.ndjson", "application/json") is False
    assert main._is_json_artifact("artifact.jsonl.gz", "application/json") is False
    assert main._is_json_artifact("artifact.json", "application/x-ndjson") is True
    assert main._is_json_artifact("legacy-artifact", "application/json") is True


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


def test_records_from_nested_json_preserves_item_metadata() -> None:
    run_id = str(uuid.uuid4())
    records = main.records_from_json_document(
        {
            "endpoints": [
                {
                    "endpoint_key": "host:445",
                    "shares": [
                        {
                            "name": "Finance",
                            "entries": [
                                {
                                    "name": "report.xlsx",
                                    "is_dir": False,
                                    "size_bytes": 4096,
                                    "mtime": "2026-08-23T12:34:56Z",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        run_id,
    )

    item = next(record for record in records if record.get("type") == "item")
    assert item["size_bytes"] == 4096
    assert item["mtime"] == "2026-08-23T12:34:56Z"


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


def test_corrupt_gzip_is_a_terminal_validation_failure() -> None:
    error = gzip.BadGzipFile("not a gzip stream")

    assert main._is_retryable_ingest_error(error) is False
    assert main._public_ingest_error(error) == main.INVALID_GZIP_ARTIFACT_ERROR


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
    assert str(captured["query"]).count("pg_input_is_valid") == 2
    assert captured["params"] == (main.STALE_INGESTING_SECONDS, 3)


def test_connect_database_uses_bounded_connect_timeout(monkeypatch) -> None:
    sentinel = object()
    captured: dict[str, object] = {}

    def _connect(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(main.psycopg, "connect", _connect)

    assert main.connect_database() is sentinel
    assert captured == {
        "url": main.DATABASE_URL,
        "kwargs": {"connect_timeout": main.DATABASE_CONNECT_TIMEOUT_SECONDS},
    }


def test_try_ensure_group_keeps_database_recovery_available_when_redis_is_down(monkeypatch) -> None:
    heartbeat_statuses: list[str] = []
    monkeypatch.setattr(main, "ensure_group", lambda: (_ for _ in ()).throw(main.redis.ConnectionError("down")))
    monkeypatch.setattr(main.time, "time", lambda: 100.0)
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda status, **_kwargs: heartbeat_statuses.append(status))

    ready, last_logged_at = main.try_ensure_group(0.0)

    assert ready is False
    assert last_logged_at == 100.0
    assert heartbeat_statuses == ["waiting_for_redis"]


def test_main_runs_database_recovery_when_redis_is_unavailable_at_startup(monkeypatch) -> None:
    recovery_limits: list[int] = []
    monkeypatch.setattr(main, "try_ensure_group", lambda last_logged_at: (False, last_logged_at))
    monkeypatch.setattr(main, "_write_worker_heartbeat", lambda *_args, **_kwargs: None)

    def _discover(limit: int):
        recovery_limits.append(limit)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "discover_recoverable_runs", _discover)

    with pytest.raises(KeyboardInterrupt):
        main.main()

    assert recovery_limits == [main.RECOVERY_SCAN_LIMIT]
