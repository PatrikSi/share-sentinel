import importlib.util
import io
import json
import os
import requests
import sys
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


def _base_args(output_path: str):
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
        workers=1,
        timeout=1.0,
        operator_label=None,
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
        exclude_share=[],
        exclude_path_regex=None,
        extensions_only=None,
    )


def test_main_does_not_persist_output_when_run_has_no_data(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "failed.json"
    args = _base_args(str(output_path))

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(collector, "SMBConnection", object())

    rc = collector.main()

    assert rc == 2
    assert not output_path.exists()


def test_main_persists_output_when_run_has_endpoint_data(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "success.json"
    args = _base_args(str(output_path))

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445"})
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": "10.0.0.5:445",
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": "Public",
            }
        )
        with lock:
            stats.endpoints += 1
            stats.resources += 1
        return True

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())

    rc = collector.main()

    assert rc == 0
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["meta"]["tool"] == "share-sentinel-collector"
    assert payload["endpoints"][0]["shares"][0]["name"] == "Public"


def test_main_persists_output_when_run_has_only_empty_endpoint(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "endpoint-only.json"
    args = _base_args(str(output_path))

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445", "ip": "10.0.0.5"})
        with lock:
            stats.endpoints += 1
        return True

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())

    rc = collector.main()

    assert rc == 0
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["endpoints"] == 1
    assert payload["endpoints"][0]["endpoint_key"] == "10.0.0.5:445"
    assert payload["endpoints"][0]["shares"] == []


def test_main_reports_dependency_error_without_writing_output(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "missing-dep.json"
    args = _base_args(str(output_path))
    stderr_capture = io.StringIO()

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)
    monkeypatch.setattr(collector, "SMBConnection", None)

    rc = collector.main()

    assert rc == 2
    assert not output_path.exists()
    assert "impacket" in stderr_capture.getvalue().lower()


def test_main_reports_session_credential_configuration_error(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "session-auth.json"
    args = _base_args(str(output_path))
    args.use_session_creds = True
    args.smb_anonymous = False
    stderr_capture = io.StringIO()

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "_principal_from_ccache_env", lambda *_args, **_kwargs: (None, None, "cache missing principal"))
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == 2
    assert not output_path.exists()
    assert "cache missing principal" in stderr_capture.getvalue()


def test_main_reports_kerberos_preflight_error_without_username(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "kerberos-auth.json"
    args = _base_args(str(output_path))
    args.smb_anonymous = False
    args.kerberos = True
    args.ccache = "/tmp/bad.ccache"
    stderr_capture = io.StringIO()

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(
        collector,
        "_principal_from_ccache_env",
        lambda *_args, **_kwargs: (None, None, "unable to parse Kerberos cache FILE:/tmp/bad.ccache: bad format"),
    )
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == 2
    assert "unable to parse kerberos cache" in stderr_capture.getvalue().lower()
    assert "pass --username with --kerberos" in stderr_capture.getvalue()


def test_main_reports_output_write_errors_instead_of_traceback(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "ok.json"
    args = _base_args(str(output_path))
    stderr_capture = io.StringIO()

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445"})
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": "10.0.0.5:445",
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": "Public",
            }
        )
        with lock:
            stats.endpoints += 1
            stats.resources += 1
        return True

    def _raise_on_close(self, keep_output=True):  # noqa: ARG001
        raise FileNotFoundError("No such file or directory")

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(collector.NDJSONWriter, "close", _raise_on_close)
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == 2
    assert "output error: failed to write output" in stderr_capture.getvalue().lower()


def test_main_reports_upload_errors_without_traceback_returns_partial(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "upload.json"
    args = _base_args(str(output_path))
    args.upload = True
    args.api_base = "http://api"
    args.project_id = "project-id"
    args.api_token = "token-value"
    stderr_capture = io.StringIO()

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445"})
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": "10.0.0.5:445",
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": "Public",
            }
        )
        with lock:
            stats.endpoints += 1
            stats.resources += 1
        return True

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(
        collector,
        "upload_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.HTTPError("status 503")),
    )
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == collector.EXIT_PARTIAL
    assert "upload error: failed to send artifact" in stderr_capture.getvalue().lower()
    assert "artifact kept at" in stderr_capture.getvalue().lower()


def test_main_keeps_generated_temp_artifact_when_upload_fails_returns_partial(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    temp_output = tmp_path / "generated-upload.json"
    args = _base_args(None)
    args.upload = True
    args.api_base = "http://api"
    args.project_id = "project-id"
    args.api_token = "token-value"
    stderr_capture = io.StringIO()

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445"})
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": "10.0.0.5:445",
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": "Public",
            }
        )
        with lock:
            stats.endpoints += 1
            stats.resources += 1
        return True

    def _fake_mkstemp(*_args, **_kwargs):
        fd = os.open(temp_output, os.O_CREAT | os.O_RDWR)
        return fd, str(temp_output)

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(collector.tempfile, "mkstemp", _fake_mkstemp)
    monkeypatch.setattr(
        collector,
        "upload_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.HTTPError("status 503")),
    )
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == collector.EXIT_PARTIAL
    assert temp_output.exists()
    assert f"artifact kept at {temp_output}" in stderr_capture.getvalue()


def test_main_persists_output_when_run_has_only_errors(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "errors-only.json"
    args = _base_args(str(output_path))

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "LIST_TIMEOUT",
                "message": "Timed out reading host",
            }
        )
        with lock:
            stats.errors += 1
        return False

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())

    rc = collector.main()

    assert rc == 1
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["errors"] == 1
    assert payload["issue_summary"][0]["code"] == "LIST_TIMEOUT"


def test_main_returns_partial_success_when_artifact_is_kept(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "partial.json"
    args = _base_args(str(output_path))

    def _scan_host(host, _args, run_id, writer, stats, lock):
        if host == "10.0.0.5":
            writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445"})
            writer.emit(
                {
                    "type": "resource",
                    "run_id": run_id,
                    "endpoint_key": "10.0.0.5:445",
                    "share_type": "smb",
                    "resource_type": "smb_share",
                    "name": "Public",
                }
            )
            with lock:
                stats.endpoints += 1
                stats.resources += 1
            return True
        return False

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5", "10.0.0.6"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())

    rc = collector.main()

    assert rc == 1
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["resources"] == 1
    assert payload["summary"]["errors"] == 0


def test_main_reports_upload_errors_without_traceback_returns_failure(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "upload-failed.json"
    args = _base_args(str(output_path))
    args.upload = True
    args.api_base = "http://api"
    args.project_id = "project-id"
    args.api_token = "token-value"
    stderr_capture = io.StringIO()

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445"})
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": "10.0.0.5:445",
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": "Public",
            }
        )
        with lock:
            stats.endpoints += 1
            stats.resources += 1
        return True

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "upload_artifact", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.HTTPError("status 503")))
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == 1
    assert output_path.exists()
    stderr_value = stderr_capture.getvalue().lower()
    assert "upload error: failed to send artifact" in stderr_value
    assert "artifact kept at" in stderr_value


def test_main_keeps_generated_temp_artifact_on_upload_error_returns_partial(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "generated-upload-failed.json"
    args = _base_args(None)
    args.upload = True
    args.api_base = "http://api"
    args.project_id = "project-id"
    args.api_token = "token-value"
    stderr_capture = io.StringIO()

    def _scan_host(_host, _args, run_id, writer, stats, lock):
        writer.emit({"type": "endpoint", "run_id": run_id, "endpoint_key": "10.0.0.5:445"})
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": "10.0.0.5:445",
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": "Public",
            }
        )
        with lock:
            stats.endpoints += 1
            stats.resources += 1
        return True

    def _fake_mkstemp(*_args, **_kwargs):
        fd = collector.os.open(output_path, collector.os.O_RDWR | collector.os.O_CREAT | collector.os.O_TRUNC)
        return fd, str(output_path)

    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "iter_targets", lambda *_args, **_kwargs: iter(["10.0.0.5"]))
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "upload_artifact", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError("status 503")))
    monkeypatch.setattr(collector.tempfile, "mkstemp", _fake_mkstemp)
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == collector.EXIT_PARTIAL
    assert output_path.exists()
    stderr_value = stderr_capture.getvalue().lower()
    assert "upload error: failed to send artifact" in stderr_value
    assert "artifact kept at" in stderr_value
