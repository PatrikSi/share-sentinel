import importlib.util
import io
import sys
from pathlib import Path


def _load_collector_module():
    module_path = Path(__file__).resolve().parents[1] / "smbguard_collector.py"
    spec = importlib.util.spec_from_file_location("smbguard_collector", module_path)
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
    writer.emit({"type": "run_meta", "run_id": "abc"})

    assert fake_stdout.getvalue() == ""

    writer.close()

    output = fake_stdout.getvalue()
    assert '"type": "run_meta"' in output
    assert '"run_id": "abc"' in output


def test_ndjson_writer_discards_file_output_when_not_kept(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.ndjson"

    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "endpoint", "run_id": "abc"})
    writer.close(keep_output=False)

    assert not output_path.exists()


def test_ndjson_writer_writes_file_output_when_kept(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "collector.ndjson"

    writer = collector.NDJSONWriter(path=str(output_path), gzip_output=False)
    writer.emit({"type": "endpoint", "run_id": "abc"})
    writer.close(keep_output=True)

    assert output_path.exists()
    payload = output_path.read_text(encoding="utf-8")
    assert '"type": "endpoint"' in payload
