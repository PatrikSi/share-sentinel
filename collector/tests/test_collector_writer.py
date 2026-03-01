import importlib.util
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


class _FakeStream:
    def __init__(self) -> None:
        self.buffer: list[str] = []
        self.flush_count = 0
        self.close_count = 0

    def write(self, value: str):
        self.buffer.append(value)

    def flush(self):
        self.flush_count += 1

    def close(self):
        self.close_count += 1


def test_ndjson_writer_flushes_stdout_emits() -> None:
    collector = _load_collector_module()
    fake_stdout = _FakeStream()
    collector.sys.stdout = fake_stdout

    writer = collector.NDJSONWriter(path=None, gzip_output=False)
    writer.emit({"type": "run_meta", "run_id": "abc"})
    writer.close()

    assert fake_stdout.flush_count == 1
    assert fake_stdout.close_count == 0
    assert fake_stdout.buffer


def test_ndjson_writer_buffers_file_output_without_per_line_flush() -> None:
    collector = _load_collector_module()
    fake_file = _FakeStream()

    collector.open = lambda *_args, **_kwargs: fake_file

    writer = collector.NDJSONWriter(path="/tmp/collector.ndjson", gzip_output=False)
    writer.emit({"type": "endpoint", "run_id": "abc"})
    writer.close()

    assert fake_file.flush_count == 0
    assert fake_file.close_count == 1
    assert fake_file.buffer
