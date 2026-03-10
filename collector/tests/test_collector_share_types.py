import importlib.util
import os
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


def test_list_share_entries_emits_limit_callback_when_truncated() -> None:
    collector = _load_collector_module()
    callbacks: list[int] = []

    class _Entry:
        def __init__(self, name: str, is_dir: bool):
            self._name = name
            self._is_dir = is_dir

        def get_longname(self):
            return self._name

        def is_directory(self):
            return self._is_dir

    class _Conn:
        def listPath(self, _share_name, wildcard):
            if wildcard == "*":
                return [_Entry("folder", True), _Entry("file.txt", False)]
            return []

    rows = list(
        collector.list_share_entries(
            _Conn(),
            "General",
            max_depth=3,
            max_entries=1,
            exclude_path_regex=None,
            extensions=None,
            on_limit_reached=lambda emitted: callbacks.append(emitted),
        )
    )

    assert len(rows) == 1
    assert callbacks == [1]


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


def test_validate_args_rejects_invalid_hash_format() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        cidr=["10.0.0.0/24"],
        hosts=None,
        share_types="smb",
        kerberos=False,
        smb_anonymous=False,
        use_session_creds=False,
        username="svc",
        password="",
        hashes="not-valid",
        ccache=None,
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
    )

    try:
        collector._validate_args(args)
    except SystemExit as exc:
        assert "--hashes must be in LMHASH:NTHASH format" in str(exc)
        return
    raise AssertionError("expected SystemExit for invalid --hashes format")


def test_validate_args_rejects_ccache_without_kerberos() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        cidr=["10.0.0.0/24"],
        hosts=None,
        share_types="smb",
        kerberos=False,
        smb_anonymous=False,
        use_session_creds=False,
        username="svc",
        password="secret",
        hashes=None,
        ccache="/tmp/krb5cc",
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
    )

    try:
        collector._validate_args(args)
    except SystemExit as exc:
        assert "--ccache requires --kerberos" in str(exc)
        return
    raise AssertionError("expected SystemExit for --ccache without --kerberos")


def test_validate_args_rejects_output_path_with_missing_parent(tmp_path) -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        cidr=["10.0.0.0/24"],
        hosts=None,
        share_types="smb",
        kerberos=False,
        smb_anonymous=True,
        use_session_creds=False,
        username="",
        password="",
        hashes=None,
        ccache=None,
        output=str(tmp_path / "missing-parent" / "out.ndjson"),
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
    )

    try:
        collector._validate_args(args)
    except SystemExit as exc:
        assert "--output directory does not exist" in str(exc)
        return
    raise AssertionError("expected SystemExit for missing output parent directory")


def test_resolve_smb_auth_method_prefers_session_creds() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(smb_anonymous=False, use_session_creds=True, kerberos=False, username="")
    assert collector._resolve_smb_auth_method(args) == "kerberos"


def test_resolve_ccache_env_value_normalizes_paths(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    monkeypatch.delenv("KRB5CCNAME", raising=False)

    file_path = tmp_path / "krb5cc_test"
    file_path.write_text("dummy", encoding="utf-8")
    resolved = collector._resolve_ccache_env_value(str(file_path))

    assert resolved == str(file_path)


def test_resolve_ccache_env_value_falls_back_to_environment(monkeypatch) -> None:
    collector = _load_collector_module()
    monkeypatch.setenv("KRB5CCNAME", "FILE:/tmp/from-env")

    assert collector._resolve_ccache_env_value(None) == "/tmp/from-env"


def test_redact_cli_arguments_hides_sensitive_values() -> None:
    collector = _load_collector_module()

    redacted = collector._redact_cli_arguments(
        [
            "--username",
            "svc",
            "--password",
            "secret",
            "--hashes=LMHASH:NTHASH",
            "--api-token",
            "token-value",
        ]
    )

    assert redacted == ["--username", "svc", "--password", "<redacted>", "--hashes=<redacted>", "--api-token", "<redacted>"]


def test_session_error_hint_includes_share_name_guidance() -> None:
    collector = _load_collector_module()

    hint = collector._session_error_hint("STATUS_BAD_NETWORK_NAME", "anonymous")
    assert hint is not None
    assert "share name" in hint.lower()


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
            return [{"shi1_netname": "Public\x00", "shi1_remark": "open\x00"}]

        def listPath(self, *_args, **_kwargs):
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
        include_share=[],
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


def test_scan_host_smb_kerberos_does_not_mutate_ccache_env_per_call(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            self.use_cache = None

        def kerberosLogin(self, *_args, **kwargs):
            self.use_cache = kwargs.get("useCache")
            return None

        def getDialect(self):
            return "768"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Public\x00", "shi1_remark": "open\x00"}]

        def listPath(self, *_args, **_kwargs):
            return []

        def logoff(self):
            return None

    fake_conn = _Conn()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: fake_conn)
    monkeypatch.setenv("KRB5CCNAME", "FILE:/tmp/original-cache")

    class _Writer:
        def __init__(self):
            self.records = []

        def emit(self, record):
            self.records.append(record)

    args = SimpleNamespace(
        timeout=1.0,
        kerberos=True,
        smb_anonymous=False,
        username="svc",
        password="secret",
        domain="EXAMPLE",
        ccache_env_value="FILE:/tmp/alternate-cache",
        hashes=None,
        local_auth=False,
        include_share=[],
        exclude_share=[],
        exclude_path_regex=None,
        extensions_only=None,
        max_depth=1,
        max_entries_per_share=1,
    )
    writer = _Writer()
    stats = collector.Stats()
    ok = collector.scan_host_smb("10.0.0.5", args, "run-kerb-1", writer, stats, threading.Lock())

    assert ok is True
    assert fake_conn.use_cache is True
    assert os.environ.get("KRB5CCNAME") == "FILE:/tmp/original-cache"


def test_help_text_contains_common_and_examples_sections() -> None:
    collector = _load_collector_module()
    help_text = collector._build_parser().format_help()

    assert "Common Options" in help_text
    assert "SMB Authentication" in help_text
    assert "Examples:" in help_text
    assert "--use-session-creds" in help_text


def test_scan_host_smb_reports_share_enumeration_denied_with_anonymous_hint(monkeypatch) -> None:
    collector = _load_collector_module()
    fake_session_error = type("FakeSessionError", (Exception,), {})
    monkeypatch.setattr(collector, "SessionError", fake_session_error)

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "768"

        def isSigningRequired(self):
            return False

        def listShares(self):
            raise fake_session_error("STATUS_ACCESS_DENIED")

        def logoff(self):
            return None

    monkeypatch.setattr(collector, "SMBConnection", _Conn)

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
        include_share=[],
        exclude_share=[],
        exclude_path_regex=None,
        exclude_path_pattern=None,
        extensions_only=None,
        max_depth=1,
        max_entries_per_share=1,
    )
    writer = _Writer()
    stats = collector.Stats()

    ok = collector.scan_host_smb("10.0.0.6", args, "run-1", writer, stats, threading.Lock())

    assert ok is True
    assert stats.endpoints == 0
    assert stats.resources == 0
    assert stats.errors == 1
    error_record = next(row for row in writer.records if row.get("type") == "error")
    assert error_record["code"] == "LIST_SHARES_DENIED"
    assert "--include-share" in error_record["hint"]


def test_scan_host_smb_scans_user_specified_shares_without_enumeration(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Entry:
        def __init__(self, name: str, is_dir: bool):
            self._name = name
            self._is_dir = is_dir

        def get_longname(self):
            return self._name

        def is_directory(self):
            return self._is_dir

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            self.paths = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "768"

        def isSigningRequired(self):
            return False

        def listShares(self):
            raise AssertionError("listShares should not be called when include_share is provided")

        def listPath(self, share_name, wildcard):
            self.paths.append((share_name, wildcard))
            return [_Entry("report.txt", False)]

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
        smb_anonymous=True,
        username="",
        password="",
        domain="",
        ccache=None,
        hashes=None,
        local_auth=False,
        include_share=["Public"],
        exclude_share=[],
        exclude_path_regex=None,
        exclude_path_pattern=None,
        extensions_only=None,
        max_depth=1,
        max_entries_per_share=10,
    )
    writer = _Writer()
    stats = collector.Stats()

    ok = collector.scan_host_smb("10.0.0.7", args, "run-2", writer, stats, threading.Lock())

    assert ok is True
    assert ("Public", "*") in fake_conn.paths
    resource_record = next(row for row in writer.records if row.get("type") == "resource")
    item_record = next(row for row in writer.records if row.get("type") == "item")
    assert resource_record["name"] == "Public"
    assert item_record["resource_name"] == "Public"


def test_scan_host_smb_handles_share_info_objects_without_get(monkeypatch) -> None:
    collector = _load_collector_module()

    class _ShareInfo:
        def __init__(self, name: str, remark: str):
            self._data = {"shi1_netname": f"{name}\x00", "shi1_remark": f"{remark}\x00"}

        def __getitem__(self, key):
            return self._data[key]

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "768"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [_ShareInfo("General", "open-share")]

        def listPath(self, *_args, **_kwargs):
            return []

        def logoff(self):
            return None

    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: _Conn())

    class _Writer:
        def __init__(self):
            self.records = []

        def emit(self, record):
            self.records.append(record)

    args = SimpleNamespace(
        timeout=1.0,
        kerberos=False,
        smb_anonymous=False,
        username="tester",
        password="tester",
        domain="",
        ccache=None,
        use_session_creds=False,
        hashes=None,
        local_auth=False,
        include_share=[],
        exclude_share=[],
        exclude_path_regex=None,
        exclude_path_pattern=None,
        extensions_only=None,
        max_depth=1,
        max_entries_per_share=10,
    )
    writer = _Writer()
    stats = collector.Stats()

    ok = collector.scan_host_smb("10.0.0.8", args, "run-obj", writer, stats, threading.Lock())

    assert ok is True
    resource = next(row for row in writer.records if row.get("type") == "resource")
    assert resource["name"] == "General"
    assert resource["remark"] == "open-share"


def test_scan_host_smb_auth_failure_includes_actionable_hint(monkeypatch) -> None:
    collector = _load_collector_module()
    fake_session_error = type("FakeSessionError", (Exception,), {})
    monkeypatch.setattr(collector, "SessionError", fake_session_error)

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            raise fake_session_error("STATUS_LOGON_FAILURE")

    monkeypatch.setattr(collector, "SMBConnection", _Conn)

    class _Writer:
        def __init__(self):
            self.records = []

        def emit(self, record):
            self.records.append(record)

    args = SimpleNamespace(
        timeout=1.0,
        kerberos=False,
        smb_anonymous=False,
        username="svc-scan",
        password="bad-pass",
        domain="",
        ccache=None,
        hashes=None,
        local_auth=False,
        include_share=[],
        exclude_share=[],
        exclude_path_regex=None,
        exclude_path_pattern=None,
        extensions_only=None,
        max_depth=1,
        max_entries_per_share=1,
    )
    writer = _Writer()
    stats = collector.Stats()

    ok = collector.scan_host_smb("10.0.0.6", args, "run-1", writer, stats, threading.Lock())

    assert ok is False
    assert stats.errors == 1
    error_record = writer.records[0]
    assert error_record["code"] == "SMB_AUTH_FAILED"
    assert "check smb username/password" in error_record["hint"].lower()
