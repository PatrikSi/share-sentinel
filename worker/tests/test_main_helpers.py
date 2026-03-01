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
