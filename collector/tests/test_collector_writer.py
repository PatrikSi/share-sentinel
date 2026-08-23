import gzip
import importlib.util
import io
import json
import sys
import tracemalloc
from pathlib import Path

import pytest


def _load_collector_module():
    module_path = Path(__file__).resolve().parents[1] / "share_sentinel_collector.py"
    spec = importlib.util.spec_from_file_location("share_sentinel_collector", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ndjson_writer_writes_stdout_on_close(monkeypatch) -> None:
    collector = _load_collector_module()
    fake_stdout = io.StringIO()
    monkeypatch.setattr(collector.sys, "stdout", fake_stdout)

    writer = collector.NDJSONWriter(path=None, gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "tool": "collector", "tool_version": "1.0", "run_id": "abc", "started_at": "2026-01-01T00:00:00Z"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "10.0.0.5:445", "ip": "10.0.0.5"})
    writer.emit({"type": "resource", "run_id": "abc", "endpoint_key": "10.0.0.5:445", "share_type": "smb", "resource_type": "smb_share", "name": "Public"})
    writer.emit({"type": "item", "run_id": "abc", "endpoint_key": "10.0.0.5:445", "resource_name": "Public", "share_type": "smb", "resource_type": "smb_share", "path": "\\report.txt", "name": "report.txt", "is_dir": False})
    writer.emit({"type": "run_end", "run_id": "abc", "finished_at": "2026-01-01T00:05:00Z", "stats": {"endpoints": 1, "resources": 1, "items": 1, "errors": 0}})

    assert fake_stdout.getvalue() == ""

    writer.close()

    records = [json.loads(line) for line in fake_stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == [
        "run_meta",
        "endpoint",
        "resource",
        "item",
        "run_end",
    ]
    assert records[0]["run_id"] == "abc"
    assert records[2]["name"] == "Public"


def test_ndjson_writer_discards_file_output_when_not_kept(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.json"

    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "endpoint", "run_id": "abc"})
    writer.close(keep_output=False)

    assert not output_path.exists()


def test_ndjson_writer_writes_file_output_when_kept(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.json"

    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "tool": "collector", "tool_version": "1.0", "run_id": "abc", "started_at": "2026-01-01T00:00:00Z"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "10.0.0.5:445", "ip": "10.0.0.5"})
    writer.emit({"type": "resource", "run_id": "abc", "endpoint_key": "10.0.0.5:445", "share_type": "smb", "resource_type": "smb_share", "name": "Public"})
    writer.emit({"type": "run_end", "run_id": "abc", "finished_at": "2026-01-01T00:05:00Z", "stats": {"endpoints": 1, "resources": 1, "items": 0, "errors": 0}})
    writer.close(keep_output=True)

    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["resources"] == 1
    assert payload["endpoints"][0]["endpoint_key"] == "10.0.0.5:445"


def test_ndjson_writer_preserves_empty_endpoints_and_compacts_issue_summary(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.json"

    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "tool": "collector", "tool_version": "1.0", "run_id": "abc", "started_at": "2026-01-01T00:00:00Z"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "10.0.0.8:445", "ip": "10.0.0.8"})
    writer.emit({"type": "error", "run_id": "abc", "severity": "warn", "code": "LIST_SHARES_DENIED", "message": "share enumeration denied", "hint": "use include-share"})
    writer.emit({"type": "run_end", "run_id": "abc", "finished_at": "2026-01-01T00:05:00Z", "stats": {"endpoints": 1, "resources": 0, "items": 0, "errors": 1}})
    writer.close(keep_output=True)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["endpoints"][0]["endpoint_key"] == "10.0.0.8:445"
    assert payload["endpoints"][0]["shares"] == []
    assert payload["issue_summary"][0]["code"] == "LIST_SHARES_DENIED"
    assert payload["issue_summary"][0]["count"] == 1


def test_ndjson_file_preserves_flat_record_order_and_full_issue_context(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.ndjson"
    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "abc"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "z-host:445"})
    writer.emit({"type": "resource", "run_id": "abc", "endpoint_key": "z-host:445", "name": "Z"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "a-host:445"})
    writer.emit({"type": "resource", "run_id": "abc", "endpoint_key": "a-host:445", "name": "A"})
    writer.emit(
        {
            "type": "error",
            "run_id": "abc",
            "severity": "warn",
            "code": "LIMIT",
            "message": "capped Z",
            "endpoint_key": "z-host:445",
            "resource_name": "Z",
            "path": "\\deep",
        }
    )
    writer.emit(
        {
            "type": "error",
            "run_id": "abc",
            "severity": "warn",
            "code": "LIMIT",
            "message": "capped A",
            "endpoint_key": "a-host:445",
            "resource_name": "A",
            "path": "\\other",
        }
    )
    writer.emit({"type": "run_end", "run_id": "abc", "stats": {"errors": 2}})

    writer.close()

    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert [record["type"] for record in records] == [
        "run_meta",
        "endpoint",
        "resource",
        "endpoint",
        "resource",
        "error",
        "error",
        "run_end",
    ]
    assert [records[1]["endpoint_key"], records[3]["endpoint_key"]] == ["z-host:445", "a-host:445"]
    assert records[-3] == {
        "type": "error",
        "run_id": "abc",
        "severity": "warn",
        "code": "LIMIT",
        "message": "capped Z",
        "endpoint_key": "z-host:445",
        "resource_name": "Z",
        "path": "\\deep",
    }
    assert records[-2]["message"] == "capped A"
    assert records[-2]["endpoint_key"] == "a-host:445"


def test_ndjson_gzip_stream_is_worker_compatible(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.ndjson.gz"
    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=True)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "abc"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "host:445"})
    writer.emit({"type": "run_end", "run_id": "abc", "stats": {"endpoints": 1}})

    writer.close()

    with gzip.open(output_path, "rt", encoding="utf-8") as artifact_fp:
        records = [json.loads(line) for line in artifact_fp]
    assert [record["type"] for record in records] == ["run_meta", "endpoint", "run_end"]


def test_ndjson_close_remains_bounded_for_large_single_endpoint(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "large.ndjson"
    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "abc"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "host:445"})
    writer.emit({"type": "resource", "run_id": "abc", "endpoint_key": "host:445", "name": "Data"})
    for index in range(20_000):
        writer.emit(
            {
                "type": "item",
                "run_id": "abc",
                "endpoint_key": "host:445",
                "resource_name": "Data",
                "path": f"\\file-{index}.txt",
                "name": f"file-{index}.txt",
                "is_dir": False,
            }
        )
    writer.emit({"type": "run_end", "run_id": "abc", "stats": {"items": 20_000}})
    writer._build_endpoint_document = lambda *_args: (_ for _ in ()).throw(AssertionError("compact tree used"))

    tracemalloc.start()
    writer.close()
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak_bytes < 4 * 1024 * 1024
    with output_path.open("r", encoding="utf-8") as artifact_fp:
        assert sum(1 for _line in artifact_fp) == 20_004


def test_compact_writer_rejects_oversized_endpoint_before_tree_assembly(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.json"
    output_path.write_text("previous-good-artifact", encoding="utf-8")
    monkeypatch.setattr(collector, "COMPACT_JSON_MAX_ENDPOINT_BUFFER_BYTES", 100)
    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "abc"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "host:445", "padding": "x" * 200})
    writer._build_endpoint_document = lambda *_args: (_ for _ in ()).throw(AssertionError("tree assembled"))

    with pytest.raises(collector.CompactArtifactTooLargeError, match=r"\.ndjson"):
        writer.close()

    assert output_path.read_text(encoding="utf-8") == "previous-good-artifact"


def test_compact_writer_rejects_oversized_total_buffers(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.json"
    monkeypatch.setattr(collector, "COMPACT_JSON_MAX_ENDPOINT_BUFFER_BYTES", 10_000)
    monkeypatch.setattr(collector, "COMPACT_JSON_MAX_BUFFER_BYTES", 150)
    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "abc"})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "a:445", "padding": "x" * 80})
    writer.emit({"type": "endpoint", "run_id": "abc", "endpoint_key": "b:445", "padding": "x" * 80})

    with pytest.raises(collector.CompactArtifactTooLargeError, match="compact JSON buffers"):
        writer.close()

    assert not output_path.exists()


def test_ndjson_spool_failure_is_sticky_and_reported_at_close(tmp_path) -> None:
    collector = _load_collector_module()
    writer = collector.NDJSONWriter(path=str(tmp_path / "collector.ndjson"), gzip_output=False)
    assert writer._ndjson_spool_fp is not None
    writer._ndjson_spool_fp.close()

    class _FailingSpool:
        def write(self, _value):
            raise OSError("disk full")

        def flush(self):
            return None

        def close(self):
            return None

    writer._ndjson_spool_fp = _FailingSpool()

    with pytest.raises(OSError, match="disk full"):
        writer.emit({"type": "endpoint", "endpoint_key": "host:445"})
    writer.emit({"type": "error", "code": "SECONDARY", "message": "must not recurse"})

    assert writer.write_failed is True
    with pytest.raises(OSError, match="collector buffer write failed: disk full"):
        writer.close()
    assert not (tmp_path / "collector.ndjson").exists()


def test_artifact_format_selection_uses_streaming_for_stdout_and_line_suffixes() -> None:
    collector = _load_collector_module()

    assert collector._artifact_format_for_path(None) == collector.ARTIFACT_FORMAT_NDJSON
    assert collector._artifact_format_for_path("scan.NDJSON.GZ") == collector.ARTIFACT_FORMAT_NDJSON
    assert collector._artifact_format_for_path("scan.jsonl") == collector.ARTIFACT_FORMAT_NDJSON
    assert collector._artifact_format_for_path("scan.json.gz") == collector.ARTIFACT_FORMAT_COMPACT_JSON
