import importlib.util
import io
import json
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
    monkeypatch.setattr(collector, "parse_targets", lambda *_args, **_kwargs: ["10.0.0.5"])
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
    monkeypatch.setattr(collector, "parse_targets", lambda *_args, **_kwargs: ["10.0.0.5"])
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())

    rc = collector.main()

    assert rc == 0
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["meta"]["tool"] == "share-sentinel-collector"
    assert payload["endpoints"][0]["shares"][0]["name"] == "Public"


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
    monkeypatch.setattr(collector, "parse_targets", lambda *_args, **_kwargs: ["10.0.0.5"])
    monkeypatch.setattr(collector, "parse_hosts_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "scan_host", _scan_host)
    monkeypatch.setattr(collector, "SMBConnection", object())
    monkeypatch.setattr(collector.NDJSONWriter, "close", _raise_on_close)
    monkeypatch.setattr(collector.sys, "stderr", stderr_capture)

    rc = collector.main()

    assert rc == 2
    assert "output error: failed to write output" in stderr_capture.getvalue().lower()
