import importlib.util
import io
import json
import stat
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


def _load_collector_module():
    module_path = Path(__file__).resolve().parents[1] / "share_sentinel_collector.py"
    spec = importlib.util.spec_from_file_location("share_sentinel_collector", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _base_args(output_path: str | None):
    return SimpleNamespace(
        hosts=None,
        cidr=["10.0.0.0/30"],
        share_types="smb",
        output=output_path,
        gzip=False,
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
        run_name="test-run",
        upload_timeout=20.0,
        upload_attempts=2,
        workers=1,
        timeout=1.0,
        max_targets=65536,
        operator_label=None,
        quiet=True,
        verbose=0,
        progress_interval=0.0,
        smb_anonymous=True,
        username="",
        password="",
        hashes=None,
        domain="",
        local_auth=False,
        kerberos=False,
        ccache=None,
        use_session_creds=False,
        max_depth=1,
        max_entries_per_share=10,
        include_share=[],
        exclude_share=[],
        exclude_path_regex=None,
        extensions_only=None,
    )


@pytest.mark.parametrize(
    ("cidrs", "hosts"),
    [
        (["10.0.0.0/30", "10.0.0.1/32"], ["10.0.0.2", "server-a", "SERVER-A"]),
        (["10.0.0.0/31", "10.0.0.2/31"], []),
        (["2001:db8::/127", "2001:db8::2/127"], ["2001:0db8::1", "server-b"]),
    ],
)
def test_count_targets_matches_streamed_deduplicated_targets(cidrs, hosts) -> None:
    collector = _load_collector_module()

    assert collector.count_targets(cidrs, hosts) == len(list(collector.iter_targets(cidrs, hosts)))


def test_progress_reporter_emits_line_oriented_counts_and_terminal_status() -> None:
    collector = _load_collector_module()
    stream = io.StringIO()
    stats = collector.Stats(endpoints=1, resources=2, items=3)
    lock = threading.Lock()
    reporter = collector.ProgressReporter(
        total_targets=3,
        stats=stats,
        stats_lock=lock,
        interval_seconds=0,
        stream=stream,
    )

    reporter.start(workers=2, share_types=["smb"])
    reporter.target_submitted()
    reporter.target_submitted()
    reporter.target_started("host-a")
    reporter.target_completed("host-a", succeeded=True)
    reporter.finish(status="partial", artifact="scan.json", upload_status="not requested")

    output = stream.getvalue()
    assert "scan started: targets=3 workers=2 protocols=smb" in output
    assert "collector finished (partial)" in output
    assert "discovered=3 submitted=2 processed=1 active=0 pending=1 remaining=2" in output
    assert "endpoints=1 resources=2 items=3 issues=0" in output
    assert "result: artifact=scan.json upload=not requested" in output
    assert "\r" not in output


def test_progress_reporter_quiet_mode_suppresses_routine_output() -> None:
    collector = _load_collector_module()
    stream = io.StringIO()
    reporter = collector.ProgressReporter(
        total_targets=1,
        stats=collector.Stats(),
        stats_lock=threading.Lock(),
        quiet=True,
        interval_seconds=0,
        stream=stream,
    )

    reporter.start(workers=1, share_types=["smb"])
    reporter.target_submitted()
    reporter.target_completed("host-a", succeeded=False)
    reporter.finish(status="failure")

    output = stream.getvalue()
    assert "collector finished (failure)" in output
    assert "failed_targets=1" in output


def test_progress_reporter_quiet_mode_suppresses_success_output() -> None:
    collector = _load_collector_module()
    stream = io.StringIO()
    reporter = collector.ProgressReporter(
        total_targets=1,
        stats=collector.Stats(),
        stats_lock=threading.Lock(),
        quiet=True,
        interval_seconds=0,
        stream=stream,
    )

    reporter.target_submitted()
    reporter.target_completed("host-a", succeeded=True)
    reporter.finish(status="success")

    assert stream.getvalue() == ""


def test_progress_reporter_quiet_failure_lists_issue_codes() -> None:
    collector = _load_collector_module()
    stream = io.StringIO()
    stats = collector.Stats(errors=2)
    stats.error_codes["AUTH_FAILED"] = 2
    reporter = collector.ProgressReporter(
        total_targets=1,
        stats=stats,
        stats_lock=threading.Lock(),
        quiet=True,
        interval_seconds=0,
        stream=stream,
    )

    reporter.finish(status="partial")

    assert "issues=AUTH_FAILED=2" in stream.getvalue()


def test_progress_reporter_continues_when_periodic_thread_cannot_start(monkeypatch) -> None:
    collector = _load_collector_module()
    stream = io.StringIO()

    def _fail_start(_self):
        raise RuntimeError("thread quota exhausted")

    monkeypatch.setattr(collector.threading.Thread, "start", _fail_start)
    reporter = collector.ProgressReporter(
        total_targets=1,
        stats=collector.Stats(),
        stats_lock=threading.Lock(),
        interval_seconds=1,
        stream=stream,
    )

    reporter.start(workers=1, share_types=["smb"])
    reporter.finish(status="success")

    assert "progress warning" in stream.getvalue()
    assert "collector finished (success)" in stream.getvalue()


def test_main_rejects_oversized_scope_before_starting_workers(monkeypatch, tmp_path, capsys) -> None:
    collector = _load_collector_module()
    args = _base_args(str(tmp_path / "out.json"))
    args.max_targets = 1
    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "SMBConnection", object())
    scan_called = False

    def _scan(*_args, **_kwargs):
        nonlocal scan_called
        scan_called = True
        return True

    monkeypatch.setattr(collector, "scan_host", _scan)

    assert collector.main() == collector.EXIT_FAILURE
    assert scan_called is False
    assert "resolved 2 unique targets" in capsys.readouterr().err


def test_validation_rejects_ignored_anonymous_password_and_malformed_hashes() -> None:
    collector = _load_collector_module()
    args = _base_args("scan.json")
    args.password = "should-not-be-ignored"

    with pytest.raises(SystemExit, match="--smb-anonymous cannot be combined"):
        collector._validate_args(args)

    args.smb_anonymous = False
    args.password = ""
    args.username = "alice"
    args.hashes = "not-hex:not-hex"
    with pytest.raises(SystemExit, match="32-character hexadecimal"):
        collector._validate_args(args)


@pytest.mark.parametrize("field", ["timeout", "progress_interval", "upload_timeout"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validation_rejects_non_finite_timeouts(field, value) -> None:
    collector = _load_collector_module()
    args = _base_args(None)
    setattr(args, field, value)

    with pytest.raises(SystemExit, match="must be finite"):
        collector._validate_args(args)


def test_main_writes_partial_artifact_and_skips_upload_on_interrupt(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "interrupted.json"
    args = _base_args(str(output_path))
    args.upload = True
    args.api_base = "https://sentinel.example.test/api"
    args.project_id = "project-id"
    args.api_token = "token"
    upload_called = False

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(
        collector,
        "_scan_targets",
        lambda *_args, **_kwargs: collector.ScanOutcome(
            1, 0, 0, interrupted=True, targets_cancelled=1
        ),
    )

    def _upload(*_args, **_kwargs):
        nonlocal upload_called
        upload_called = True

    monkeypatch.setattr(collector, "upload_artifact", _upload)

    assert collector.main() == collector.EXIT_INTERRUPTED
    assert upload_called is False
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["interrupted"] is True
    assert payload["summary"]["targets_cancelled"] == 1
    assert payload["summary"]["targets_remaining"] == 2
    assert payload["issue_summary"][0]["code"] == "SCAN_INTERRUPTED"


def test_successful_temp_upload_terminal_summary_does_not_claim_deleted_path(monkeypatch) -> None:
    collector = _load_collector_module()
    args = _base_args(None)
    args.upload = True
    args.api_base = "https://sentinel.example.test/api"
    args.project_id = "project-id"
    args.api_token = "token"
    args.quiet = False
    stderr = io.StringIO()

    def _scan_host(host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": f"{host}:445"})
        with lock:
            stats.endpoints += 1
        return True

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "upload_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector.sys, "stderr", stderr)

    assert collector.main() == collector.EXIT_SUCCESS
    assert "artifact=uploaded (temporary copy removed) upload=accepted" in stderr.getvalue()


def test_scan_targets_handles_keyboard_interrupt_and_drains_submitted_work() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        workers=1,
        share_types="smb",
        disabled_share_types=set(),
        cancel_event=threading.Event(),
    )

    def _targets():
        yield "host-a"
        raise KeyboardInterrupt

    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
    stats = collector.Stats()
    outcome = collector._scan_targets(
        _targets(),
        args,
        "run-1",
        writer,
        stats,
        threading.Lock(),
    )

    assert outcome.interrupted is True
    assert outcome.targets_submitted == 1
    assert outcome.targets_completed + outcome.targets_cancelled == 1
    assert args.cancel_event.is_set()


def test_scan_targets_counts_cooperatively_cancelled_submitted_future_as_cancelled() -> None:
    collector = _load_collector_module()
    stream = io.StringIO()
    stats = collector.Stats()
    stats_lock = threading.Lock()
    args = SimpleNamespace(
        workers=1,
        share_types="smb",
        disabled_share_types=set(),
        cancel_event=threading.Event(),
    )
    args.progress_reporter = collector.ProgressReporter(
        total_targets=1,
        stats=stats,
        stats_lock=stats_lock,
        interval_seconds=0,
        stream=stream,
    )
    args.cancel_event.set()
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))

    outcome = collector._scan_targets(
        ["host-a"],
        args,
        "run-1",
        writer,
        stats,
        stats_lock,
    )
    args.progress_reporter.finish(status="interrupted")

    assert outcome.targets_submitted == 1
    assert outcome.targets_completed == 0
    assert outcome.targets_cancelled == 1
    assert outcome.host_failures == 0
    assert writer.records == []
    assert "processed=0 active=0 pending=0 remaining=1" in stream.getvalue()
    assert "cancelled=1" in stream.getvalue()


def test_scan_targets_handles_executor_construction_failure(monkeypatch) -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        workers=1,
        share_types="smb",
        disabled_share_types=set(),
        cancel_event=threading.Event(),
    )
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
    stats = collector.Stats()
    monkeypatch.setattr(
        collector.concurrent.futures,
        "ThreadPoolExecutor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("thread quota exhausted")),
    )

    outcome = collector._scan_targets(
        ["host-a"],
        args,
        "run-1",
        writer,
        stats,
        threading.Lock(),
    )

    assert outcome.targets_submitted == 0
    assert outcome.targets_completed == 0
    assert outcome.host_failures == 1
    assert stats.error_codes["SCAN_ORCHESTRATION_FAILED"] == 1
    assert writer.records[0]["code"] == "SCAN_ORCHESTRATION_FAILED"


def test_atomic_writer_preserves_existing_artifact_when_final_write_fails(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "scan.json"
    output_path.write_text("previous-good-artifact", encoding="utf-8")
    writer = collector.NDJSONWriter(str(output_path), gzip_output=False)

    def _fail_write(_target_fp):
        raise OSError("disk full")

    monkeypatch.setattr(writer, "_write_document", _fail_write)

    with pytest.raises(OSError, match="disk full"):
        writer.close()

    assert output_path.read_text(encoding="utf-8") == "previous-good-artifact"
    assert list(tmp_path.glob(".scan.json.*.tmp")) == []


def test_atomic_writer_uses_private_permissions_for_new_artifact(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "scan.json"
    writer = collector.NDJSONWriter(str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "run-1"})
    writer.emit({"type": "run_end", "stats": {}})

    writer.close()

    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600


def test_atomic_writer_can_retry_after_finalization_interrupt(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "scan.ndjson"
    output_path.write_text("previous-good-artifact", encoding="utf-8")
    writer = collector.NDJSONWriter(str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "run-1"})
    writer.emit({"type": "run_end", "run_id": "run-1", "stats": {}})
    original_write_payload = writer._write_payload
    calls = 0

    def _interrupt_once(target_fp):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt()
        return original_write_payload(target_fp)

    monkeypatch.setattr(writer, "_write_payload", _interrupt_once)

    with pytest.raises(KeyboardInterrupt):
        writer.close()
    assert output_path.read_text(encoding="utf-8") == "previous-good-artifact"

    writer.close()

    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert [record["type"] for record in records] == ["run_meta", "run_end"]


def test_output_directory_write_failure_is_detected_before_scan(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    args = _base_args(str(tmp_path / "scan.json"))

    def _deny_preflight(*_args, **_kwargs):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(collector.tempfile, "mkstemp", _deny_preflight)

    with pytest.raises(SystemExit, match="output directory is not writable"):
        collector._validate_args(args)


def test_list_share_entries_emits_safe_file_metadata_and_normalizes_extension() -> None:
    collector = _load_collector_module()
    mtime_epoch = 1_700_000_000

    class _Entry:
        def get_longname(self):
            return "report.pdf"

        def is_directory(self):
            return False

        def get_filesize(self):
            return 1234

        def get_mtime_epoch(self):
            return mtime_epoch

    class _Connection:
        def listPath(self, *_args, **_kwargs):
            return [_Entry()]

    records = list(
        collector.list_share_entries(
            _Connection(),
            "Reports",
            max_depth=1,
            max_entries=10,
            exclude_path_regex=None,
            extensions=collector._normalized_extensions("PDF"),
        )
    )

    assert records == [
        {
            "path": "\\report.pdf",
            "name": "report.pdf",
            "is_dir": False,
            "size_bytes": 1234,
            "mtime": datetime.fromtimestamp(mtime_epoch, tz=UTC).isoformat(),
        }
    ]


def test_writer_preserves_optional_item_metadata_in_compact_tree(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "scan.json"
    writer = collector.NDJSONWriter(str(output_path), gzip_output=False)
    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "run-1"})
    writer.emit({"type": "endpoint", "endpoint_key": "host:445"})
    writer.emit({"type": "resource", "endpoint_key": "host:445", "name": "Reports", "share_type": "smb"})
    writer.emit(
        {
            "type": "item",
            "endpoint_key": "host:445",
            "resource_name": "Reports",
            "share_type": "smb",
            "path": "\\report.pdf",
            "name": "report.pdf",
            "is_dir": False,
            "size_bytes": 1234,
            "mtime": "2023-11-14T22:13:20+00:00",
        }
    )
    writer.emit({"type": "run_end", "stats": {}})

    writer.close()

    item = json.loads(output_path.read_text(encoding="utf-8"))["endpoints"][0]["shares"][0]["entries"][0]
    assert item["size_bytes"] == 1234
    assert item["mtime"] == "2023-11-14T22:13:20+00:00"


def test_smb_connection_is_closed_when_authentication_fails(monkeypatch) -> None:
    collector = _load_collector_module()
    fake_session_error = type("FakeSessionError", (Exception,), {})
    monkeypatch.setattr(collector, "SessionError", fake_session_error)

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.closed = False

        def login(self, *_args, **_kwargs):
            raise fake_session_error("STATUS_LOGON_FAILURE")

        def close(self):
            self.closed = True

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    args = SimpleNamespace(
        timeout=1,
        kerberos=False,
        smb_anonymous=False,
        username="alice",
        password="bad",
        domain="CONTOSO",
        hashes=None,
        local_auth=False,
    )
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))

    assert collector.scan_host_smb(
        "10.0.0.5", args, "run-1", writer, collector.Stats(), threading.Lock()
    ) is False
    assert connection.closed is True


def test_nfs_advertised_export_does_not_overstate_access(monkeypatch) -> None:
    collector = _load_collector_module()

    class _SocketConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    records = []
    monkeypatch.setattr(collector.socket, "create_connection", lambda *_args, **_kwargs: _SocketConnection())
    monkeypatch.setattr(collector, "_discover_nfs_exports", lambda *_args, **_kwargs: (["/srv/public"], None))
    args = SimpleNamespace(timeout=1.0, domain="")

    assert collector.scan_host_nfs(
        "10.0.0.5",
        args,
        "run-1",
        SimpleNamespace(emit=records.append),
        collector.Stats(),
        threading.Lock(),
    ) is True

    resource = next(record for record in records if record["type"] == "resource")
    assert resource["access_level"] == "no_access"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(514, "2.0.2"), (528, "2.1"), (768, "3.0"), (770, "3.0.2"), (785, "3.1.1")],
)
def test_smb_dialect_labels_match_impacket_constants(raw, expected) -> None:
    collector = _load_collector_module()

    assert collector._dialect_label(raw) == expected


@pytest.mark.parametrize("required", [True, False])
def test_smb_signing_label_only_reports_requirement(required) -> None:
    collector = _load_collector_module()
    connection = SimpleNamespace(isSigningRequired=lambda: required)

    assert collector._signing_label(connection) == ("required" if required else "not_required")


def test_upload_uses_bounded_connect_and_configured_read_timeouts(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "scan.json"
    artifact.write_text("{}", encoding="utf-8")
    observed_timeouts = []

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "queued": True,
                "artifact_sha256": collector._sha256_file(str(artifact)),
            }

    def _post(*_args, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        return _Response()

    monkeypatch.setattr(collector.requests, "post", _post)
    args = SimpleNamespace(
        upload=True,
        api_base="https://sentinel.example.test/api",
        project_id="project-id",
        api_token="token",
        run_name="run",
        cidr=[],
        upload_timeout=120.0,
        upload_attempts=1,
        quiet=True,
    )

    collector.upload_artifact(args, "run-id", str(artifact), [])

    assert observed_timeouts == [(10.0, 120.0), (10.0, 120.0)]


def test_retry_loop_does_not_retry_terminal_request_configuration_error() -> None:
    collector = _load_collector_module()
    calls = 0

    def _request():
        nonlocal calls
        calls += 1
        raise requests.exceptions.InvalidURL("bad URL")

    with pytest.raises(requests.exceptions.InvalidURL):
        collector._post_with_retries(_request, max_attempts=3)

    assert calls == 1


def test_retry_loop_honors_bounded_retry_after(monkeypatch) -> None:
    collector = _load_collector_module()
    sleeps = []

    class _Response:
        def __init__(self, status_code, retry_after="0"):
            self.status_code = status_code
            self.headers = {"Retry-After": retry_after}

        def close(self):
            return None

    responses = [_Response(429, "120"), _Response(200)]
    monkeypatch.setattr(collector.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)

    result = collector._post_with_retries(lambda: responses.pop(0), max_attempts=2)

    assert result.status_code == 200
    assert sleeps == [30.0]


@pytest.mark.parametrize("retry_after", ["nan", "inf", "-1"])
def test_retry_loop_rejects_nonfinite_or_negative_retry_after(monkeypatch, retry_after) -> None:
    collector = _load_collector_module()
    sleeps = []

    class _Response:
        def __init__(self, status_code, header="0"):
            self.status_code = status_code
            self.headers = {"Retry-After": header}

        def close(self):
            return None

    responses = [_Response(429, retry_after), _Response(200)]
    monkeypatch.setattr(collector.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)

    collector._post_with_retries(lambda: responses.pop(0), max_attempts=2)

    assert sleeps == [0.5]
