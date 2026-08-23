import concurrent.futures
import importlib.util
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


def _load_collector_module():
    module_path = Path(__file__).resolve().parents[1] / "share_sentinel_collector.py"
    spec = importlib.util.spec_from_file_location("share_sentinel_collector", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Writer:
    def __init__(self) -> None:
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _done_future(result=None, exc: Exception | None = None) -> concurrent.futures.Future:
    future: concurrent.futures.Future = concurrent.futures.Future()
    if exc is not None:
        future.set_exception(exc)
    else:
        future.set_result(result)
    return future


def test_collect_scan_results_counts_false_results_without_extra_errors() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    run_id = "run-1"
    futures = {
        _done_future(True): "10.0.0.1",
        _done_future(False): "10.0.0.2",
    }

    host_failures = collector._collect_scan_results(futures, run_id, writer, stats, lock)

    assert host_failures == 1
    assert stats.errors == 0
    assert writer.records == []


def test_collect_scan_results_emits_error_for_exception_future() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    run_id = "run-2"
    futures = {
        _done_future(exc=RuntimeError("boom")): "10.0.0.10",
    }

    host_failures = collector._collect_scan_results(futures, run_id, writer, stats, lock)

    assert host_failures == 1
    assert stats.errors == 1
    assert len(writer.records) == 1
    record = writer.records[0]
    assert record["type"] == "error"
    assert record["run_id"] == run_id
    assert record["code"] == "SCAN_THREAD_FAILED"
    assert record["endpoint_key"] == "10.0.0.10:445"
    assert "boom" in record["message"]


def test_collect_scan_results_maps_timeout_exception_to_specific_code() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    run_id = "run-3"
    futures = {
        _done_future(exc=socket.timeout("timeout")): "10.0.0.11",
    }

    host_failures = collector._collect_scan_results(futures, run_id, writer, stats, lock)

    assert host_failures == 1
    assert stats.errors == 1
    assert writer.records[0]["code"] == "SCAN_TIMEOUT"


def test_collect_scan_results_maps_netbios_timeout_to_scan_timeout() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    run_id = "run-3b"
    futures = {
        _done_future(exc=collector.NetBIOSTimeout("nb-timeout")): "10.0.0.15",
    }

    host_failures = collector._collect_scan_results(futures, run_id, writer, stats, lock)

    assert host_failures == 1
    assert stats.errors == 1
    assert writer.records[0]["code"] == "SCAN_TIMEOUT"


def test_collect_scan_results_treats_cancelled_future_as_expected_cancellation() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    run_id = "run-4"
    futures = {
        _done_future(exc=concurrent.futures.CancelledError()): "10.0.0.12",
    }

    host_failures = collector._collect_scan_results(futures, run_id, writer, stats, lock)

    assert host_failures == 0
    assert stats.errors == 0
    assert writer.records == []


def test_collect_scan_results_treats_cooperative_cancel_sentinel_as_expected_cancellation() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    futures = {_done_future(collector.SCAN_CANCELLED): "10.0.0.13"}

    host_failures = collector._collect_scan_results(futures, "run-4b", writer, stats, lock)

    assert host_failures == 0
    assert stats.errors == 0
    assert writer.records == []


def test_collect_scan_results_uses_nfs_endpoint_key_when_nfs_only() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    run_id = "run-5"
    futures = {
        _done_future(exc=RuntimeError("boom")): "10.0.0.21",
    }
    args = SimpleNamespace(share_types="nfs", disabled_share_types=set())

    host_failures = collector._collect_scan_results(futures, run_id, writer, stats, lock, args=args)

    assert host_failures == 1
    assert writer.records[0]["endpoint_key"] == "10.0.0.21:2049"


def test_collect_scan_results_omits_endpoint_key_when_share_types_mixed() -> None:
    collector = _load_collector_module()
    writer = _Writer()
    stats = collector.Stats()
    lock = threading.Lock()
    run_id = "run-6"
    futures = {
        _done_future(exc=RuntimeError("boom")): "10.0.0.22",
    }
    args = SimpleNamespace(share_types="both", disabled_share_types=set())

    host_failures = collector._collect_scan_results(futures, run_id, writer, stats, lock, args=args)

    assert host_failures == 1
    assert "endpoint_key" not in writer.records[0]
