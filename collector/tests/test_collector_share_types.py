import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


def _load_collector_module():
    module_path = Path(__file__).resolve().parents[1] / "smbguard_collector.py"
    spec = importlib.util.spec_from_file_location("smbguard_collector", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_selected_share_types_supports_both_and_defaults() -> None:
    collector = _load_collector_module()

    assert collector._selected_share_types("smb") == {"smb"}
    assert collector._selected_share_types("nfs") == {"nfs"}
    assert collector._selected_share_types("both") == {"smb", "nfs"}
    assert collector._selected_share_types("unknown") == {"smb"}


def test_parse_showmount_exports_extracts_unique_paths() -> None:
    collector = _load_collector_module()
    output = """
Exports list on host:
/srv/public 10.0.0.0/24
/srv/private 10.0.0.5
/srv/public 10.0.0.10
not-a-path value
"""

    exports = collector._parse_showmount_exports(output)

    assert exports == ["/srv/public", "/srv/private"]


def test_scan_host_dispatch_runs_only_selected_share_scanners(monkeypatch) -> None:
    collector = _load_collector_module()
    calls: list[str] = []

    monkeypatch.setattr(
        collector,
        "scan_host_smb",
        lambda *_args, **_kwargs: calls.append("smb") or True,
    )
    monkeypatch.setattr(
        collector,
        "scan_host_nfs",
        lambda *_args, **_kwargs: calls.append("nfs") or False,
    )

    args = SimpleNamespace(share_types="both")
    ok = collector.scan_host("10.0.0.5", args, "run-1", SimpleNamespace(emit=lambda *_: None), collector.Stats(), object())

    assert ok is True
    assert calls == ["smb", "nfs"]


def test_validate_args_allows_anonymous_smb_without_username() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        cidr=["10.0.0.0/24"],
        hosts=None,
        share_types="smb",
        kerberos=False,
        smb_anonymous=False,
        username="",
        password="",
        hashes=None,
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
    )

    collector._validate_args(args)


def test_validate_args_rejects_password_without_username_for_smb() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        cidr=["10.0.0.0/24"],
        hosts=None,
        share_types="smb",
        kerberos=False,
        smb_anonymous=False,
        username="",
        password="secret",
        hashes=None,
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
    )

    try:
        collector._validate_args(args)
    except SystemExit as exc:
        assert "--password requires --username" in str(exc)
        return
    raise AssertionError("expected SystemExit when password is provided without username")


def test_scan_host_smb_uses_anonymous_auth_when_username_missing(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            self.login_args = None

        def login(self, username, password, domain="", lmhash="", nthash=""):
            self.login_args = (username, password, domain, lmhash, nthash)

        def getDialect(self):
            return "768"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return []

        def logoff(self):
            return None

    fake_conn = _Conn()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: fake_conn)

    class _Writer:
        def __init__(self):
            self.records = []

        def emit(self, record):
            self.records.append(record)

    args = SimpleNamespace(
        timeout=1.0,
        kerberos=False,
        smb_anonymous=False,
        username="",
        password="",
        domain="",
        ccache=None,
        hashes=None,
        local_auth=False,
        exclude_share=[],
        exclude_path_regex=None,
        extensions_only=None,
        max_depth=1,
        max_entries_per_share=1,
    )
    writer = _Writer()
    stats = collector.Stats()
    ok = collector.scan_host_smb("10.0.0.5", args, "run-1", writer, stats, threading.Lock())

    assert ok is True
    assert fake_conn.login_args == ("", "", "", "", "")
    endpoint = next(row for row in writer.records if row.get("type") == "endpoint")
    assert endpoint["auth"]["method"] == "anonymous"


def test_help_text_contains_common_and_examples_sections() -> None:
    collector = _load_collector_module()
    help_text = collector._build_parser().format_help()

    assert "Common Options" in help_text
    assert "SMB Authentication" in help_text
    assert "Examples:" in help_text
