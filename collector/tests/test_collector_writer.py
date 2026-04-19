import importlib.util
import io
import json
import sys
from pathlib import Path


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

    payload = json.loads(fake_stdout.getvalue())
    assert payload["meta"]["run_id"] == "abc"
    assert payload["endpoints"][0]["shares"][0]["name"] == "Public"


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
