import gzip
import io
import json
import os
import subprocess
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


def test_read_int_env_strict_mode_rejects_invalid_or_unsafe_values(monkeypatch) -> None:
    monkeypatch.setenv("TEST_WORKER_INT", "invalid")
    with pytest.raises(ValueError, match="must be an integer"):
        main._read_int_env("TEST_WORKER_INT", default=5, min_value=1, max_value=10, strict=True)

    monkeypatch.setenv("TEST_WORKER_INT", "0")
    with pytest.raises(ValueError, match="must be at least 1"):
        main._read_int_env("TEST_WORKER_INT", default=5, min_value=1, max_value=10, strict=True)

    monkeypatch.setenv("TEST_WORKER_INT", "11")
    with pytest.raises(ValueError, match="must be at most 10"):
        main._read_int_env("TEST_WORKER_INT", default=5, min_value=1, max_value=10, strict=True)

    monkeypatch.setenv("TEST_WORKER_INT", "10")
    assert main._read_int_env("TEST_WORKER_INT", default=5, min_value=1, max_value=10, strict=True) == 10


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


def test_read_float_env_strict_mode_rejects_non_finite_and_out_of_range(monkeypatch) -> None:
    for value in ("invalid", "nan", "-0.1", "1.1"):
        monkeypatch.setenv("TEST_WORKER_FLOAT", value)
        with pytest.raises(ValueError):
            main._read_float_env(
                "TEST_WORKER_FLOAT",
                default=0.2,
                min_value=0.0,
                max_value=1.0,
                strict=True,
            )


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("INGEST_BATCH_SIZE", str(main.MAX_INGEST_BATCH_SIZE + 1), "must be at most"),
        ("INGEST_BATCH_SIZE", "not-an-integer", "must be an integer"),
        ("INGEST_MAX_RECORD_BYTES", str(main.MAX_INGEST_RECORD_BYTES + 1), "must be at most"),
        ("INGEST_JSON_COMPAT_MAX_BYTES", str(main.MAX_INGEST_JSON_COMPAT_BYTES + 1), "must be at most"),
        ("INGEST_GZIP_MAX_BYTES", str(main.MAX_INGEST_GZIP_DECOMPRESSED_BYTES + 1), "must be at most"),
        ("INGEST_GZIP_MAX_EXPANSION_RATIO", str(main.MAX_INGEST_GZIP_EXPANSION_RATIO + 1), "must be at most"),
        ("INGEST_IDENTITY_CACHE_SIZE", str(main.MAX_INGEST_IDENTITY_CACHE_SIZE + 1), "must be at most"),
        ("INGEST_MAX_RETRIES", str(main.MAX_INGEST_RETRIES + 1), "must be at most"),
        ("INGEST_RETRY_JITTER_RATIO", "1.01", "must be at most"),
    ],
)
def test_bounded_worker_settings_fail_fast_at_startup(name: str, value: str, expected: str) -> None:
    worker_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env[name] = value
    env["PYTHONPATH"] = str(worker_root)

    result = subprocess.run(
        [sys.executable, "-c", "from worker import main"],
        cwd=worker_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert name in result.stderr
    assert expected in result.stderr


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
    assert (
        main._public_ingest_error(ValueError("unsupported JSON artifact format")) == "unsupported JSON artifact format"
    )
    assert (
        main._public_ingest_error(ValueError(main.GZIP_DECOMPRESSED_LIMIT_ERROR)) == main.GZIP_DECOMPRESSED_LIMIT_ERROR
    )
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


def test_clear_persisted_ingest_inventory_removes_all_resumable_rows() -> None:
    statements: list[tuple[str, tuple[str, ...]]] = []

    class _Conn:
        def execute(self, query, params):
            statements.append((" ".join(query.split()), params))

    main.clear_persisted_ingest_inventory(_Conn(), "run-1")

    assert [statement for statement, _params in statements] == [
        "DELETE FROM items WHERE run_id = %s",
        "DELETE FROM resources WHERE run_id = %s",
        "DELETE FROM endpoints WHERE run_id = %s",
        "DELETE FROM ingest_errors WHERE run_id = %s",
        "UPDATE scan_runs SET collection_context = '{}'::jsonb WHERE id = %s",
    ]
    assert all(params == ("run-1",) for _statement, params in statements)


def test_read_json_compat_bytes_rejects_gzip_payloads_over_limit() -> None:
    payload = gzip.compress(b'{"hello":"' + (b"a" * 32) + b'"}')

    with pytest.raises(ValueError, match="JSON artifact exceeds non-streamable compatibility limit"):
        main._read_json_compat_bytes(io.BytesIO(payload), gzip_input=True, max_bytes=16)


def test_limited_reader_rejects_gzip_payloads_over_limit() -> None:
    payload = gzip.compress(b"a" * 33)

    with (
        gzip.GzipFile(fileobj=io.BytesIO(payload)) as gzip_reader,
        main._LimitedReader(
            gzip_reader,
            max_bytes=32,
            error_message=main.GZIP_DECOMPRESSED_LIMIT_ERROR,
        ) as limited_reader,
    ):
        with pytest.raises(ValueError, match=main.GZIP_DECOMPRESSED_LIMIT_ERROR):
            limited_reader.read()


def test_limited_reader_does_not_double_count_repeated_streamable_json_passes() -> None:
    raw = b'{"meta":{"tool":"collector"},"endpoints":[]}'
    payload = gzip.compress(raw)

    with (
        gzip.GzipFile(fileobj=io.BytesIO(payload)) as gzip_reader,
        main._LimitedReader(
            gzip_reader,
            max_bytes=len(raw),
            error_message=main.GZIP_DECOMPRESSED_LIMIT_ERROR,
        ) as limited_reader,
    ):
        assert limited_reader.read() == raw
        limited_reader.seek(0)
        assert limited_reader.read() == raw


def test_bounded_ndjson_reader_rejects_limit_plus_one_without_unbounded_read() -> None:
    reader = io.BytesIO(b"x" * 9)

    with pytest.raises(ValueError, match=main.NDJSON_RECORD_TOO_LARGE_ERROR):
        list(main._iter_bounded_ndjson_lines(reader, max_record_bytes=8))

    assert reader.tell() == 9


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "run_end"}],
        [{"type": "run_meta"}],
        [{"type": "run_meta"}, {"type": "run_meta"}, {"type": "run_end"}],
        [{"type": "run_meta"}, {"type": "run_end"}, {"type": "endpoint"}],
        [{"type": "run_meta"}, {"type": "run_end"}, {"type": "run_end"}],
    ],
)
def test_ndjson_framing_requires_one_header_and_one_terminal_footer(records) -> None:
    payload = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)

    with pytest.raises(main.ArtifactFramingError, match="exactly one run_meta"):
        main._validate_ndjson_framing(io.BytesIO(payload))


def test_ndjson_framing_allows_blank_and_malformed_middle_records() -> None:
    payload = b"\n".join(
        [
            b'{"type":"run_meta"}',
            b"{malformed}",
            b'{"type":"run_end"}',
            b"",
        ]
    )

    progress_calls = 0

    def report_progress() -> None:
        nonlocal progress_calls
        progress_calls += 1

    main._validate_ndjson_framing(
        io.BytesIO(payload),
        progress_callback=report_progress,
    )

    assert progress_calls >= 4


def test_gzip_decompressed_limit_scales_from_artifact_size() -> None:
    assert main._gzip_decompressed_limit(1024) == max(
        main.JSON_COMPAT_MAX_BYTES, 1024 * main.GZIP_DECOMPRESSED_MAX_RATIO
    )


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
    assert main._retry_backoff_seconds(10**1000) == main.INGEST_RETRY_MAX_SECONDS


def test_retry_backoff_jitter_is_stable_bounded_and_spreads_runs(monkeypatch) -> None:
    monkeypatch.setattr(main, "INGEST_RETRY_JITTER_RATIO", 0.2)
    base = main._retry_backoff_seconds(2)
    first = main._retry_backoff_seconds(2, jitter_key="run-a")
    repeated = main._retry_backoff_seconds(2, jitter_key="run-a")
    second = main._retry_backoff_seconds(2, jitter_key="run-b")

    assert first == repeated
    assert round(base * 0.8) <= first <= base
    assert round(base * 0.8) <= second <= base
    assert first != second


def test_runtime_error_is_terminal_but_dependency_errors_are_retryable() -> None:
    assert main._is_retryable_ingest_error(RuntimeError("poison")) is False
    assert main._is_retryable_ingest_error(main.psycopg.OperationalError("database unavailable")) is True
    assert main._is_retryable_ingest_error(OSError("artifact unavailable")) is True


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


def test_ingest_error_row_bounds_untrusted_diagnostics() -> None:
    row = main.build_ingest_error_row(
        "run-1",
        "unexpected",
        "C" * 500,
        "M" * 10000,
        "E" * 500,
        "R" * 500,
        "P" * 10000,
    )

    assert row[1] == "error"
    assert len(row[2]) == main.INGEST_ERROR_CODE_MAX_LENGTH
    assert len(row[3]) == main.INGEST_ERROR_MESSAGE_MAX_LENGTH
    assert len(row[4]) == main.ENDPOINT_KEY_MAX_LENGTH
    assert len(row[5]) == main.RESOURCE_NAME_MAX_LENGTH
    assert len(row[6]) == main.INGEST_ERROR_PATH_MAX_LENGTH


def test_validate_record_rejects_database_poison_fields() -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    item = {
        "type": "item",
        "run_id": run_id,
        "endpoint_key": "host:445",
        "resource_name": "Finance",
        "path": "\\" + ("x" * (main.ITEM_PATH_MAX_BYTES + 1)),
        "name": "report.txt",
    }
    valid, reason = main.validate_record(item)
    assert valid is False
    assert reason == f"field path exceeds {main.ITEM_PATH_MAX_BYTES} UTF-8 bytes"

    endpoint = {
        "type": "endpoint",
        "run_id": run_id,
        "endpoint_key": "host:445",
        "auth": ["not", "an", "object"],
    }
    valid, reason = main.validate_record(endpoint)
    assert valid is False
    assert reason == "field auth must be an object"


def test_bounded_identity_cache_evicts_least_recently_used_entry() -> None:
    cache = main._BoundedLRUCache[str](2)
    cache["a"] = 1
    cache["b"] = 2
    assert cache.get("a") == 1
    cache["c"] = 3

    assert dict(cache) == {"a": 1, "c": 3}


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
            assert "LIMIT %s" in query
            assert params == ("run-1", main.INGEST_IDENTITY_CACHE_SIZE)
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


def test_validate_record_normalizes_first_class_sharepoint_metadata() -> None:
    resource_record = {
        "type": "resource",
        "run_id": "run-1",
        "endpoint_key": "sharepoint:site-1",
        "name": "Documents",
        "share_type": "sharepoint",
        "provider_resource_id": "drive-1",
        "web_url": "https://contoso.sharepoint.com/sites/Finance/Documents",
        "exposure": "user visible",
        "exposure_evidence": {"basis": "delegated_visibility"},
        "metadata": {"tenant_id": "tenant-1", "site_id": "site-1", "drive_id": "drive-1"},
    }
    item_record = {
        "type": "item",
        "run_id": "run-1",
        "endpoint_key": "sharepoint:site-1",
        "resource_name": "Documents",
        "share_type": "sharepoint",
        "provider_resource_id": "drive-1",
        "provider_item_id": "item-1",
        "provider_parent_id": "parent-1",
        "path": "/Budgets/FY26.xlsx",
        "name": "FY26.xlsx",
        "is_dir": False,
        "size": 184933,
        "modified_at": "2026-08-23T12:34:56Z",
        "web_url": "https://contoso.sharepoint.com/sites/Finance/Documents/Budgets/FY26.xlsx",
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "etag": '"etag-1"',
        "ctag": '"ctag-1"',
        "exposure": "USER_VISIBLE",
        "metadata": {"site_id": "site-1", "drive_id": "drive-1"},
    }

    assert main.validate_record(resource_record) == (True, None)
    assert main.validate_record(item_record) == (True, None)
    assert resource_record["resource_type"] == "sharepoint_library"
    assert resource_record["provider"] == "sharepoint"
    assert resource_record["exposure"] == "USER_VISIBLE"
    assert item_record["resource_type"] == "sharepoint_library"
    assert item_record["size_bytes"] == 184933
    assert item_record["mtime"].isoformat() == "2026-08-23T12:34:56+00:00"
    assert item_record["provider_metadata"]["etag"] == '"etag-1"'
    assert item_record["provider_metadata"]["ctag"] == '"ctag-1"'


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            {
                "type": "resource",
                "run_id": "run-1",
                "endpoint_key": "sharepoint:site-1",
                "name": "Documents",
                "share_type": "sharepoint",
            },
            "provider_resource_id",
        ),
        (
            {
                "type": "item",
                "run_id": "run-1",
                "endpoint_key": "sharepoint:site-1",
                "resource_name": "Documents",
                "share_type": "sharepoint",
                "provider_item_id": "item-1",
                "path": "/file.txt",
            },
            "provider_resource_id",
        ),
        (
            {
                "type": "item",
                "run_id": "run-1",
                "endpoint_key": "sharepoint:site-1",
                "resource_name": "Documents",
                "share_type": "sharepoint",
                "provider_resource_id": "drive-1",
                "path": "/file.txt",
            },
            "provider_item_id",
        ),
    ],
)
def test_validate_record_requires_sharepoint_stable_ids(record, message) -> None:
    valid, reason = main.validate_record(record)

    assert valid is False
    assert message in str(reason)


def test_validate_record_rejects_explicit_unknown_resource_types_instead_of_smb_fallback() -> None:
    record = {
        "type": "resource",
        "run_id": "run-1",
        "endpoint_key": "host",
        "name": "resource",
        "share_type": "future-provider",
    }

    valid, reason = main.validate_record(record)

    assert valid is False
    assert reason == "unsupported share_type: future-provider"
    assert main._normalize_share_type(None, None) == "smb"


@pytest.mark.parametrize(
    "web_url",
    [
        "javascript:alert(1)",
        "http://contoso.sharepoint.com/sites/Finance",
        "https://user:secret@contoso.sharepoint.com/sites/Finance",
    ],
)
def test_validate_record_rejects_unsafe_sharepoint_urls(web_url: str) -> None:
    record = {
        "type": "resource",
        "run_id": "run-1",
        "endpoint_key": "sharepoint:site-1",
        "name": "Documents",
        "share_type": "sharepoint",
        "provider_resource_id": "drive-1",
        "web_url": web_url,
    }

    valid, reason = main.validate_record(record)

    assert valid is False
    assert "web_url" in str(reason)


@pytest.mark.parametrize(
    "secret_key",
    ["accessToken", "clientSecret", "authorization_header", "refresh-token", "privateKey"],
)
def test_validate_record_rejects_secret_key_variants(secret_key: str) -> None:
    record = {
        "type": "endpoint",
        "run_id": "run-1",
        "endpoint_key": "sharepoint:site-1",
        "provider": "sharepoint",
        "metadata": {"nested": {secret_key: "must-not-survive"}},
    }

    valid, reason = main.validate_record(record)

    assert valid is False
    assert "secret or sensitive operational state" in str(reason)


def test_validate_record_bounds_unicode_sharepoint_names_and_paths_without_truncation() -> None:
    boundary_name = "é" * 255
    boundary_path = "/" + ("😀" * 397) + "/x"
    record = {
        "type": "item",
        "run_id": "run-1",
        "endpoint_key": "sharepoint:site-1",
        "resource_name": "Documents",
        "share_type": "sharepoint",
        "provider_resource_id": "drive-1",
        "provider_item_id": "item-1",
        "path": boundary_path,
        "name": boundary_name,
    }

    assert main.validate_record(record) == (True, None)
    assert record["name"] == boundary_name
    assert record["path"] == boundary_path

    record["path"] += "x"
    valid, reason = main.validate_record(record)
    assert valid is False
    assert "400 characters" in str(reason)


def test_validate_record_normalizes_canonical_collection_context() -> None:
    record = {
        "type": "run_meta",
        "schema_version": 1,
        "tool": "share-sentinel-sharepoint-collector",
        "tool_version": "1.0.0",
        "run_id": "run-1",
        "started_at": "2026-08-24T00:00:00Z",
        "collection_context": {
            "source": "sharepoint",
            "collection_mode": "delegated_user_view",
            "assessed_identity": "alice@contoso.example",
            "partial": False,
            "auth_context": {
                "auth_mode": "interactive",
                "auth_type": "delegated",
                "tenant_id": "tenant-1",
                "user_principal_name": "alice@contoso.example",
                "scopes": ["Sites.Read.All", "Sites.Read.All"],
                "jwt_inspection": "unverified_metadata_only",
            },
            "metadata": {
                "snapshot_materialized": True,
                "discovery_authoritative": False,
            },
        },
    }

    assert main.validate_record(record) == (True, None)
    context = record["collection_context"]
    assert context["source"] == "sharepoint"
    assert context["provider"] == "sharepoint"
    assert context["auth_type"] == "delegated"
    assert context["tenant_id"] == "tenant-1"
    assert context["scopes"] == ["Sites.Read.All"]
    assert context["materialized_snapshot"] is True
    assert context["discovery_completeness"] == "non_authoritative"


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
    assert main._normalize_access_level("write") == "unknown"
    assert main._normalize_access_level("modify") == "unknown"
    assert main._normalize_access_level("read_write") == "readable"
    assert main._normalize_access_level("full_control") == "readable"
    assert main._normalize_access_level("unknown") == "unknown"
    assert main._normalize_access_level("unknown-value") == "unknown"


def test_validate_record_bounds_access_capabilities_and_preserves_probe_metadata() -> None:
    resource_record = {
        "type": "resource",
        "run_id": "run-1",
        "endpoint_key": "host:445",
        "name": "Finance",
        "access_level": "unknown",
        "access_capabilities": {
            "read_file": {
                "status": "allowed",
                "attempted": "1",
                "allowed": 1,
                "denied": -2,
                "inconclusive": False,
                "reason_code": "granted",
                "protocol_status": "0x00000000",
                "method": "non_mutating_handle_open",
                "scope": "file",
            },
            "_metadata": {
                "probe_method": "non_mutating_handle_open",
                "coverage": "bounded_sample",
                "assessment_summary": "read_observed",
                "assessment_reason": "bounded_observation",
                "probe_abort_reason": "tree_session_invalid",
                "share_presence": "confirmed",
                "probe_limit": 3,
                "partial": True,
                "complete": True,
                "finalized": True,
                "degraded": False,
                "transport_failed": False,
                "probes_aborted": True,
                "listing_truncated": False,
                "directory_candidates_seen": 7,
                "file_candidates_seen": 11,
                "ignored_nested": {"unbounded": ["data"]},
            },
            "bad": {"status": "not-a-status"},
            "x" * 65: {"status": "allowed"},
        },
    }

    ok, reason = main.validate_record(resource_record)

    assert ok is True
    assert reason is None
    assert resource_record["access_level"] == "unknown"
    assert resource_record["access_capabilities"] == {
        "read_file": {
            "status": "allowed",
            "attempted": 1,
            "allowed": 1,
            "denied": 0,
            "inconclusive": 0,
            "reason_code": "granted",
            "protocol_status": "0x00000000",
            "method": "non_mutating_handle_open",
            "scope": "file",
        },
        "_metadata": {
            "probe_method": "non_mutating_handle_open",
            "coverage": "bounded_sample",
            "assessment_summary": "read_observed",
            "assessment_reason": "bounded_observation",
            "probe_abort_reason": "tree_session_invalid",
            "share_presence": "confirmed",
            "probe_limit": 3,
            "directory_candidates_seen": 7,
            "file_candidates_seen": 11,
            "partial": True,
            "complete": True,
            "listing_truncated": False,
            "finalized": True,
            "degraded": False,
            "transport_failed": False,
            "probes_aborted": True,
        },
        "bad": {
            "status": "not_tested",
            "attempted": 0,
            "allowed": 0,
            "denied": 0,
            "inconclusive": 0,
        },
    }


def test_merge_access_capabilities_is_monotonic_and_idempotent() -> None:
    final = {
        "read_file": {"status": "allowed", "attempted": 1, "allowed": 1, "denied": 0, "inconclusive": 0},
        "_metadata": {
            "probe_method": "non_mutating_handle_open",
            "coverage": "bounded_sample",
            "probe_limit": 3,
            "complete": True,
            "partial": True,
            "file_candidates_seen": 4,
        },
    }
    stale_provisional = {
        "read_file": {"status": "not_tested", "attempted": 0, "allowed": 0, "denied": 0, "inconclusive": 0},
        "_metadata": {"coverage": "disabled", "probe_limit": 3, "complete": False, "partial": True},
    }

    merged = main._merge_access_capabilities(final, stale_provisional)

    assert merged["read_file"] == final["read_file"]
    assert merged["_metadata"]["complete"] is True
    assert merged["_metadata"]["coverage"] == "bounded_sample"
    assert main._merge_access_capabilities(merged, final) == merged

    mixed = main._merge_access_capabilities(
        {
            "read_file": {
                "status": "allowed",
                "attempted": 1,
                "allowed": 1,
                "reason_code": "granted",
                "protocol_status": "0x00000000",
            }
        },
        {
            "read_file": {
                "status": "denied",
                "attempted": 1,
                "denied": 1,
                "reason_code": "access_denied",
                "protocol_status": "0xC0000022",
            }
        },
    )
    assert mixed["read_file"]["status"] == "mixed"
    assert mixed["read_file"]["attempted"] == 2
    assert mixed["read_file"]["reason_code"] == "multiple_outcomes"
    assert mixed["read_file"]["protocol_status"] == "multiple"

    observed_after_no_sample = main._merge_access_capabilities(
        {"read_file": {"status": "not_tested", "not_tested_reason": "no_visible_file_candidate"}},
        {"read_file": {"status": "allowed", "attempted": 1, "allowed": 1, "reason_code": "granted"}},
    )
    assert observed_after_no_sample["read_file"]["status"] == "allowed"
    assert "not_tested_reason" not in observed_after_no_sample["read_file"]


def test_completed_capability_metadata_wins_regardless_of_replay_order() -> None:
    final = {
        "_metadata": {
            "probe_method": "non_mutating_handle_open",
            "coverage": "bounded_sample",
            "complete": True,
            "partial": True,
            "listing_truncated": False,
        }
    }
    stale_provisional = {
        "_metadata": {
            "probe_method": "legacy_probe",
            "coverage": "disabled",
            "complete": False,
            "partial": False,
            "listing_truncated": True,
        }
    }

    forward = main._merge_access_capabilities(stale_provisional, final)
    reverse = main._merge_access_capabilities(final, stale_provisional)

    assert forward == reverse
    assert forward["_metadata"] == final["_metadata"]


def test_finalized_degraded_metadata_and_reason_fields_survive_stale_replay() -> None:
    final = {
        "read_file": {
            "status": "not_tested",
            "not_tested_reason": "transport_aborted",
            "method": "non_mutating_handle_open",
            "scope": "file",
        },
        "create_file": {
            "status": "inconclusive",
            "attempted": 1,
            "inconclusive": 1,
            "reason_code": "transport_failure",
            "protocol_status": "0xC00000C9",
        },
        "_metadata": {
            "probe_method": "non_mutating_handle_open",
            "coverage": "bounded_sample",
            "assessment_summary": "connected_only",
            "assessment_reason": "partial_transport_failure",
            "share_presence": "confirmed",
            "complete": False,
            "finalized": True,
            "degraded": True,
            "transport_failed": True,
            "probes_aborted": True,
            "probe_abort_reason": "tree_session_invalid",
        },
    }
    stale_provisional = {
        "read_file": {"status": "not_tested"},
        "_metadata": {
            "coverage": "disabled",
            "assessment_summary": "not_assessed",
            "assessment_reason": "pending",
            "share_presence": "unverified",
            "complete": False,
            "finalized": False,
            "degraded": False,
            "transport_failed": False,
            "probes_aborted": False,
        },
    }

    forward = main._merge_access_capabilities(stale_provisional, final)
    reverse = main._merge_access_capabilities(final, stale_provisional)

    assert forward == reverse
    assert forward["read_file"]["not_tested_reason"] == "transport_aborted"
    assert forward["create_file"]["reason_code"] == "transport_failure"
    assert forward["create_file"]["protocol_status"] == "0xC00000C9"
    assert forward["_metadata"] == final["_metadata"]


def test_capability_key_bounds_prioritize_contract_fields_during_normalize_and_merge() -> None:
    extras = {f"extra_{index:02d}": {"status": "allowed"} for index in range(40)}
    document = {
        **extras,
        "read_file": {"status": "allowed", "attempted": 1, "allowed": 1},
        "_metadata": {"complete": True, "coverage": "bounded_sample"},
    }

    normalized = main._normalize_access_capabilities(document)
    assert len(normalized) == main.ACCESS_CAPABILITY_MAX_KEYS
    assert "_metadata" in normalized
    assert "read_file" in normalized

    left = {f"left_{index:02d}": {"status": "allowed"} for index in range(31)}
    right = {f"right_{index:02d}": {"status": "denied"} for index in range(31)}
    merged = main._merge_access_capabilities(
        {**left, "read_file": {"status": "allowed"}},
        {**right, "_metadata": {"complete": True}},
    )
    assert len(merged) == main.ACCESS_CAPABILITY_MAX_KEYS
    assert "_metadata" in merged
    assert "read_file" in merged


def test_capability_counter_saturation_preserves_attempted_invariant_and_evidence_classes() -> None:
    maximum = main.ACCESS_CAPABILITY_MAX_COUNT
    normalized = main._normalize_access_capabilities(
        {
            "read_file": {
                "attempted": maximum,
                "allowed": maximum,
                "denied": maximum,
                "inconclusive": maximum,
            }
        }
    )["read_file"]

    outcome_total = normalized["allowed"] + normalized["denied"] + normalized["inconclusive"]
    assert outcome_total == maximum
    assert normalized["attempted"] >= outcome_total
    assert normalized["allowed"] > 0
    assert normalized["denied"] > 0
    assert normalized["inconclusive"] > 0
    assert normalized["status"] == "mixed"


def test_saturated_capability_merge_is_idempotent_and_replay_order_independent() -> None:
    maximum = main.ACCESS_CAPABILITY_MAX_COUNT
    allowed = {"read_file": {"allowed": maximum}}
    denied = {"read_file": {"denied": maximum}}
    inconclusive = {"read_file": {"inconclusive": maximum}}

    merged = main._merge_access_capabilities(main._merge_access_capabilities(allowed, denied), inconclusive)
    replayed = main._merge_access_capabilities(merged, allowed)
    permuted = main._merge_access_capabilities(
        main._merge_access_capabilities(inconclusive, allowed),
        denied,
    )

    assert replayed == merged
    assert permuted == merged
    evidence = merged["read_file"]
    assert evidence["attempted"] >= evidence["allowed"] + evidence["denied"] + evidence["inconclusive"]
    assert evidence["status"] == "mixed"


def test_upsert_resource_does_not_downgrade_existing_access_or_capability_evidence() -> None:
    captured: dict[str, object] = {}
    existing_capabilities = {
        "read_file": {"status": "allowed", "attempted": 1, "allowed": 1, "denied": 0, "inconclusive": 0},
        "_metadata": {"coverage": "bounded_sample", "complete": True, "partial": True},
    }

    class _Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class _Conn:
        def execute(self, query, params):
            if "SELECT id, access_level::text" in query:
                return _Result((9, "readable", existing_capabilities))
            if "UPDATE resources" in query:
                captured["query"] = query
                captured["params"] = params
                return _Result((9,))
            raise AssertionError(f"unexpected query: {query}")

    resource_id = main.upsert_resource(
        _Conn(),
        "run-1",
        7,
        {
            "resource_type": "smb_share",
            "name": "Finance",
            "access_level": "unknown",
            "access_capabilities": {
                "read_file": {"status": "not_tested"},
                "_metadata": {"coverage": "disabled", "complete": False, "partial": True},
            },
        },
    )

    assert resource_id == 9
    assert captured["params"][2] == "readable"
    persisted_capabilities = json.loads(captured["params"][3])
    assert persisted_capabilities["read_file"]["status"] == "allowed"
    assert persisted_capabilities["_metadata"]["coverage"] == "bounded_sample"


def test_upsert_resource_renames_same_provider_resource_instead_of_duplicating() -> None:
    captured: dict[str, object] = {}

    class _Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class _Conn:
        def execute(self, query, params):
            if "provider_resource_id = %s" in query:
                captured["select_params"] = params
                return _Result((9, "list_only", {}, {"drive_id": "drive-1"}, "UNKNOWN", {}))
            if "UPDATE resources" in query:
                captured["update_params"] = params
                return _Result((9,))
            raise AssertionError(f"unexpected query: {query}")

    resource_id = main.upsert_resource(
        _Conn(),
        "run-1",
        7,
        {
            "resource_type": "sharepoint_library",
            "name": "Renamed Documents",
            "provider": "sharepoint",
            "provider_resource_id": "drive-1",
            "access_level": "list_only",
            "provider_metadata": {"site_id": "site-1"},
            "exposure": "USER_VISIBLE",
            "exposure_evidence": {"basis": "delegated_visibility"},
        },
    )

    assert resource_id == 9
    assert captured["select_params"][-1] == "drive-1"
    assert captured["update_params"][0] == "Renamed Documents"
    assert captured["update_params"][5] == "drive-1"


def test_update_run_collection_context_persists_only_normalized_public_context() -> None:
    captured: dict[str, object] = {}

    class _Conn:
        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

    context = {
        "source": "sharepoint",
        "collection_mode": "delegated_user_view",
        "assessed_identity": "alice@contoso.example",
    }

    main.update_run_collection_context(_Conn(), "run-1", context)

    assert "collection_context" in str(captured["query"])
    assert json.loads(captured["params"][0]) == context
    assert captured["params"][1] == "run-1"


def test_write_only_capability_evidence_corrects_stale_no_access_summary() -> None:
    capabilities = {
        "list": {"status": "denied", "attempted": 1, "denied": 1},
        "create_file": {"status": "allowed", "attempted": 1, "allowed": 1},
    }

    assert main._reconcile_access_level_with_capabilities("no_access", capabilities) == "unknown"
    assert main._reconcile_access_level_with_capabilities("list_only", capabilities) == "list_only"
    assert main._reconcile_access_level_with_capabilities("readable", capabilities) == "readable"

    connected_but_not_listable = {
        "tree_connect": {"status": "allowed", "attempted": 1, "allowed": 1},
        "list": {"status": "denied", "attempted": 1, "denied": 1},
    }
    assert main._reconcile_access_level_with_capabilities("no_access", connected_but_not_listable) == "unknown"


def test_capability_evidence_upgrades_legacy_access_summary() -> None:
    assert (
        main._reconcile_access_level_with_capabilities(
            "no_access",
            {"read_file": {"status": "allowed"}},
        )
        == "readable"
    )
    assert (
        main._reconcile_access_level_with_capabilities(
            "unknown",
            {"list": {"status": "mixed"}},
        )
        == "list_only"
    )


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
        "allocation_size_bytes": 4096,
        "mtime": "2026-08-23T12:34:56Z",
        "created_at": "2026-01-01T00:00:00Z",
        "accessed_at": "2026-08-24T12:34:56Z",
        "changed_at": "2026-08-23T13:00:00Z",
        "file_attributes": ["Archive", "READ-ONLY", "Archive", 123, "x" * 65],
    }

    ok, reason = main.validate_record(item_record)

    assert ok is True
    assert reason is None
    assert item_record["size_bytes"] == 1024
    assert item_record["allocation_size_bytes"] == 4096
    assert item_record["mtime"].isoformat() == "2026-08-23T12:34:56+00:00"
    assert item_record["created_at"].isoformat() == "2026-01-01T00:00:00+00:00"
    assert item_record["accessed_at"].isoformat() == "2026-08-24T12:34:56+00:00"
    assert item_record["changed_at"].isoformat() == "2026-08-23T13:00:00+00:00"
    assert item_record["file_attributes"] == ["archive", "read_only"]

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

    rows = [
        (
            "run-1",
            7,
            "\\report.xlsx",
            "report.xlsx",
            False,
            1024,
            4096,
            "2026-08-23T12:34:56Z",
            "2026-01-01T00:00:00Z",
            "2026-08-24T12:34:56Z",
            "2026-08-23T13:00:00Z",
            '["archive"]',
        )
    ]
    main.flush_item_batch(_Conn(), rows)

    assert "size_bytes" in str(captured["query"])
    assert "mtime" in str(captured["query"])
    assert "DO UPDATE SET" in str(captured["query"])
    assert "allocation_size_bytes" in str(captured["query"])
    assert "file_attributes" in str(captured["query"])
    assert len(captured["params"][0]) == 21
    assert rows == []


def test_flush_item_batch_uses_stable_provider_identity_for_move_and_deletion_updates() -> None:
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

    rows = [
        (
            "run-1",
            7,
            "/Renamed/report.xlsx",
            "report.xlsx",
            False,
            1024,
            None,
            None,
            None,
            None,
            None,
            "[]",
            "sharepoint",
            "item-1",
            "parent-2",
            "https://contoso.sharepoint.com/report.xlsx",
            "application/vnd.test",
            False,
            '{"drive_id":"drive-1"}',
            "USER_VISIBLE",
            '{"basis":"delegated_visibility"}',
        )
    ]

    main.flush_item_batch(_Conn(), rows)

    query = str(captured["query"])
    assert "ON CONFLICT (run_id, resource_id, provider_item_id)" in query
    assert "path = CASE WHEN EXCLUDED.deleted" in query
    assert "is_dir = CASE WHEN EXCLUDED.deleted" in query
    assert captured["params"][0][13] == "item-1"
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
                            "entries": [
                                {"name": "docs", "is_dir": True, "children": [{"name": "file.txt", "is_dir": False}]}
                            ],
                        }
                    ],
                }
            ]
        },
        run_id,
    )

    resource = next(record for record in records if record.get("type") == "resource")
    item = next(record for record in records if record.get("type") == "item")

    assert records[0]["type"] == "run_meta"
    assert records[-1]["type"] == "run_end"
    assert records[-1]["run_id"] == run_id
    assert records[-1]["stats"] == {}
    assert resource["share_type"] == "nfs"
    assert resource["resource_type"] == "nfs_share"
    assert resource["access_level"] == "unknown"
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
                            "access_level": "readable",
                            "access_capabilities": {
                                "read_file": {"status": "allowed", "attempted": 1, "allowed": 1},
                            },
                            "entries": [
                                {
                                    "name": "report.xlsx",
                                    "is_dir": False,
                                    "size_bytes": 4096,
                                    "allocation_size_bytes": 8192,
                                    "mtime": "2026-08-23T12:34:56Z",
                                    "created_at": "2026-01-01T00:00:00Z",
                                    "accessed_at": "2026-08-24T12:34:56Z",
                                    "changed_at": "2026-08-23T13:00:00Z",
                                    "file_attributes": ["archive"],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        run_id,
    )

    resource = next(record for record in records if record.get("type") == "resource")
    item = next(record for record in records if record.get("type") == "item")
    assert resource["access_level"] == "readable"
    assert resource["access_capabilities"]["read_file"]["status"] == "allowed"
    assert item["size_bytes"] == 4096
    assert item["allocation_size_bytes"] == 8192
    assert item["mtime"] == "2026-08-23T12:34:56Z"
    assert item["created_at"] == "2026-01-01T00:00:00Z"
    assert item["accessed_at"] == "2026-08-24T12:34:56Z"
    assert item["changed_at"] == "2026-08-23T13:00:00Z"
    assert item["file_attributes"] == ["archive"]


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
    assert (
        main._public_ingest_error(main.psycopg.DataError("bad access level"))
        == "database operation failed during ingest"
    )
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
    assert "FOR UPDATE SKIP LOCKED" in captured["query"]
    assert "recovery_claimed_by" in captured["query"]
    assert captured["params"] == (main.STALE_INGESTING_SECONDS, 3, main.CONSUMER_NAME)


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
        "kwargs": {
            "connect_timeout": main.WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS,
            "options": (
                f"-c statement_timeout={main.WORKER_DATABASE_STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={main.WORKER_DATABASE_LOCK_TIMEOUT_MS}"
            ),
        },
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

    assert recovery_limits == [1]


def test_main_survives_transient_database_recovery_failure(monkeypatch) -> None:
    recovery_attempts = 0
    heartbeat_statuses: list[str] = []
    monkeypatch.setattr(main, "RECOVERY_SCAN_SECONDS", 0)
    monkeypatch.setattr(main, "try_ensure_group", lambda last_logged_at: (False, last_logged_at))
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        main,
        "_write_worker_heartbeat",
        lambda status, **_kwargs: heartbeat_statuses.append(status),
    )

    def _discover(limit: int):
        nonlocal recovery_attempts
        assert limit == 1
        recovery_attempts += 1
        if recovery_attempts == 1:
            raise main.psycopg.OperationalError("database unavailable")
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "discover_recoverable_runs", _discover)

    with pytest.raises(KeyboardInterrupt):
        main.main()

    assert recovery_attempts == 2
    assert "database_recovery_retry" in heartbeat_statuses


def test_shutdown_signal_sets_cooperative_stop_event() -> None:
    main._shutdown_event.clear()
    try:
        main._handle_shutdown_signal(main.signal.SIGTERM, None)
        assert main._shutdown_event.is_set()
    finally:
        main._shutdown_event.clear()
