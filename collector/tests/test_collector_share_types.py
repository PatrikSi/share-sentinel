import importlib.util
import os
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


def test_selected_share_types_supports_both_and_defaults() -> None:
    collector = _load_collector_module()

    assert collector._selected_share_types("smb") == {"smb"}
    assert collector._selected_share_types("nfs") == {"nfs"}
    assert collector._selected_share_types("both") == {"smb", "nfs"}
    assert collector._selected_share_types("unknown") == {"smb"}


def test_iter_targets_streams_without_sorting_and_deduplicates() -> None:
    collector = _load_collector_module()

    targets = list(collector.iter_targets(["10.0.0.0/30"], ["10.0.0.2", "host-a", "host-a"]))

    assert targets == ["10.0.0.1", "10.0.0.2", "host-a"]


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


def test_showmount_export_parser_bounds_count_and_path_size() -> None:
    collector = _load_collector_module()
    output = "/one host\n/two host\n/three host\n/" + ("x" * 32) + " host\n"

    result = collector._parse_showmount_exports_bounded(output, max_exports=2, max_path_bytes=16)

    assert result.exports == ("/one", "/two")
    assert result.observed_export_lines == 4
    assert result.truncated is True
    assert set(result.limitations) == {"nfs_export_count_limit_reached", "nfs_export_path_limit_reached"}


def test_showmount_export_parser_accepts_255_unicode_characters_and_rejects_256() -> None:
    collector = _load_collector_module()
    accepted = "/" + ("é" * 254)
    rejected = "/" + ("é" * 255)

    result = collector._parse_showmount_exports_bounded(f"{accepted} host\n{rejected} host\n")

    assert result.exports == (accepted,)
    assert result.observed_export_lines == 2
    assert result.truncated is True
    assert result.limitations == ("nfs_export_path_character_limit_reached",)


@pytest.mark.parametrize(
    ("raw", "expected", "discarded"),
    [
        (b"/complete host\n/partial", b"/complete host\n", True),
        (b"/complete host\n", b"/complete host\n", False),
        (b"/complete host\n/partial-\xe2\x82", b"/complete host\n", True),
    ],
)
def test_truncated_showmount_stdout_retains_only_complete_utf8_safe_lines(raw, expected, discarded) -> None:
    collector = _load_collector_module()

    retained, was_discarded = collector._complete_showmount_stdout(raw, truncated=True)

    assert retained == expected
    assert was_discarded is discarded
    retained.decode("utf-8", errors="strict")


def test_showmount_truncated_mid_line_does_not_emit_partial_export(monkeypatch) -> None:
    collector = _load_collector_module()
    monkeypatch.setattr(
        collector,
        "_run_bounded_process",
        lambda *_args, **_kwargs: collector._BoundedProcessResult(
            0,
            b"/srv/complete host\n/srv/partial-\xe2\x82",
            b"",
            True,
            False,
        ),
    )

    discovery = collector._discover_nfs_exports("nfs.example", 1.0)

    assert discovery.exports == ("/srv/complete",)
    assert discovery.status == "truncated"
    assert "showmount_unterminated_trailing_line_discarded" in discovery.limitations


def test_bounded_process_drains_stdout_and_stderr_without_unbounded_retention() -> None:
    collector = _load_collector_module()

    result = collector._run_bounded_process(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'x' * 200000); os.write(2, b'y' * 200000)",
        ],
        timeout=5,
        stdout_limit=1024,
        stderr_limit=2048,
    )

    assert result.returncode == 0
    assert len(result.stdout) == 1024
    assert len(result.stderr) == 2048
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_bounded_process_closes_inherited_output_pipes_without_hanging(monkeypatch) -> None:
    collector = _load_collector_module()

    class BlockingPipe:
        def __init__(self) -> None:
            self.released = threading.Event()
            self.close_calls = 0

        def read(self, _size):
            self.released.wait(5)
            return b""

        def close(self) -> None:
            self.close_calls += 1
            self.released.set()

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BlockingPipe()
            self.stderr = BlockingPipe()
            self.kill_calls = 0

        def wait(self, *, timeout):  # noqa: ARG002
            return 0

        def kill(self) -> None:
            self.kill_calls += 1

    process = FakeProcess()
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(collector, "NFS_SHOWMOUNT_PIPE_DRAIN_GRACE_SECONDS", 0.02)

    result = collector._run_bounded_process(
        ["showmount", "-e", "nfs.example"],
        timeout=1,
        stdout_limit=1024,
        stderr_limit=1024,
    )

    assert result.returncode == 0
    assert process.kill_calls == 1
    assert process.stdout.close_calls >= 1
    assert process.stderr.close_calls >= 1


def test_bounded_process_fails_closed_when_output_pipe_cannot_be_released(monkeypatch) -> None:
    collector = _load_collector_module()

    class StubbornPipe:
        def __init__(self) -> None:
            self.released = threading.Event()
            self.close_calls = 0

        def read(self, _size):
            self.released.wait(5)
            return b""

        def close(self) -> None:
            self.close_calls += 1

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = StubbornPipe()
            self.stderr = StubbornPipe()
            self.kill_calls = 0

        def wait(self, *, timeout):  # noqa: ARG002
            return 0

        def kill(self) -> None:
            self.kill_calls += 1

    process = FakeProcess()
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(collector, "NFS_SHOWMOUNT_PIPE_DRAIN_GRACE_SECONDS", 0.02)
    try:
        with pytest.raises(collector._BoundedProcessDrainError, match="safety bound"):
            collector._run_bounded_process(
                ["showmount", "-e", "nfs.example"],
                timeout=1,
                stdout_limit=1024,
                stderr_limit=1024,
            )
    finally:
        process.stdout.released.set()
        process.stderr.released.set()

    assert process.stdout.close_calls >= 1
    assert process.stderr.close_calls >= 1
    assert process.kill_calls == 1


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "fork"), reason="requires POSIX process groups")
def test_bounded_process_reaps_pipe_inheriting_process_group(tmp_path) -> None:
    collector = _load_collector_module()
    pid_path = tmp_path / "descendant.pid"
    existing_drain_threads = {
        thread.ident for thread in threading.enumerate() if thread.name in {"showmount-stdout", "showmount-stderr"}
    }
    fd_directory = Path("/proc/self/fd")
    initial_fd_count = len(list(fd_directory.iterdir())) if fd_directory.is_dir() else None
    script = (
        "import os, pathlib, time; "
        "child=os.fork(); "
        f"path={str(pid_path)!r}; "
        "(pathlib.Path(path).write_text(str(os.getpid()), encoding='ascii'), time.sleep(60), os._exit(0)) "
        "if child == 0 else os._exit(0)"
    )
    descendant_pid = None
    started = time.monotonic()
    try:
        result = collector._run_bounded_process(
            [sys.executable, "-c", script],
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
        )
        for _ in range(100):
            if pid_path.exists():
                descendant_pid = int(pid_path.read_text(encoding="ascii"))
                break
            time.sleep(0.01)
        assert descendant_pid is not None

        def live_process(pid: int) -> bool:
            proc_stat = Path(f"/proc/{pid}/stat")
            if proc_stat.exists():
                fields = proc_stat.read_text(encoding="ascii", errors="replace").split()
                return len(fields) < 3 or fields[2] != "Z"
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        for _ in range(200):
            if not live_process(descendant_pid):
                break
            time.sleep(0.01)

        assert result.returncode == 0
        assert not live_process(descendant_pid)
        assert time.monotonic() - started < 8
        assert not any(
            thread.ident not in existing_drain_threads
            for thread in threading.enumerate()
            if thread.name in {"showmount-stdout", "showmount-stderr"}
        )
        if initial_fd_count is not None:
            assert len(list(fd_directory.iterdir())) <= initial_fd_count
    finally:
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "fork"), reason="requires POSIX process groups")
def test_bounded_process_timeout_kills_entire_process_group(tmp_path) -> None:
    collector = _load_collector_module()
    pid_path = tmp_path / "timeout-descendant.pid"
    script = f"""
import os
import pathlib
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = os.fork()
if child == 0:
    pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="ascii")
    time.sleep(60)
    os._exit(0)
deadline = time.monotonic() + 2
while not pathlib.Path({str(pid_path)!r}).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(60)
"""
    descendant_pid = None
    started = time.monotonic()
    try:
        with pytest.raises(collector.subprocess.TimeoutExpired):
            collector._run_bounded_process(
                [sys.executable, "-c", script],
                timeout=0.1,
                stdout_limit=1024,
                stderr_limit=1024,
            )
        descendant_pid = int(pid_path.read_text(encoding="ascii"))

        def live_process(pid: int) -> bool:
            proc_stat = Path(f"/proc/{pid}/stat")
            if proc_stat.exists():
                fields = proc_stat.read_text(encoding="ascii", errors="replace").split()
                return len(fields) < 3 or fields[2] != "Z"
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        for _ in range(200):
            if not live_process(descendant_pid):
                break
            time.sleep(0.01)

        assert not live_process(descendant_pid)
        assert time.monotonic() - started < 8
    finally:
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, 9)
            except ProcessLookupError:
                pass


def test_showmount_output_drain_failure_is_explicit_partial_evidence(monkeypatch) -> None:
    collector = _load_collector_module()
    monkeypatch.setattr(
        collector,
        "_run_bounded_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(collector._BoundedProcessDrainError("sensitive")),
    )

    discovery = collector._discover_nfs_exports("nfs.example", 1.0)

    assert discovery.exports == ()
    assert discovery.status == "output_drain_failed"
    assert discovery.limitations == ("showmount_output_drain_failed",)
    assert "sensitive" not in (discovery.detail or "")


def test_showmount_output_truncation_is_explicit_partial_evidence(monkeypatch) -> None:
    collector = _load_collector_module()
    monkeypatch.setattr(
        collector,
        "_run_bounded_process",
        lambda *_args, **_kwargs: collector._BoundedProcessResult(0, b"/srv/public host\n", b"", True, False),
    )

    discovery = collector._discover_nfs_exports("nfs.example", 1.0)

    assert discovery.exports == ("/srv/public",)
    assert discovery.status == "truncated"
    assert discovery.stdout_truncated is True
    assert "showmount_stdout_limit_reached" in discovery.limitations


def test_nfs_v4_null_reply_confirms_protocol_without_claiming_authentication() -> None:
    collector = _load_collector_module()
    xid = 0x12345678
    payload = struct.pack("!6I", xid, 1, 0, 0, 0, 0)

    result = collector._parse_nfs_v4_null_reply(payload, expected_xid=xid)

    assert result.transport_status == "reachable"
    assert result.service_status == "nfs_v4_confirmed"
    assert result.status == "supported"
    assert result.public_metadata()["credential_flavor"] == "AUTH_NONE"
    assert result.public_metadata()["mutating"] is False


def test_nfs_v4_null_reply_preserves_supported_version_range() -> None:
    collector = _load_collector_module()
    xid = 42
    payload = struct.pack("!8I", xid, 1, 0, 0, 0, 2, 2, 3)

    result = collector._parse_nfs_v4_null_reply(payload, expected_xid=xid)

    assert result.status == "version_not_supported"
    assert result.supported_version_min == 2
    assert result.supported_version_max == 3


def test_nfs_v4_null_reply_rejects_mismatched_transaction() -> None:
    collector = _load_collector_module()
    payload = struct.pack("!6I", 99, 1, 0, 0, 0, 0)

    with pytest.raises(RuntimeError, match="rpc_xid_mismatch"):
        collector._parse_nfs_v4_null_reply(payload, expected_xid=100)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("RPC: Program not registered", "mount_protocol_unavailable"),
        ("mount clntudp_create: RPC: Timed out", "timed_out"),
        ("clnt_create: RPC: Authentication error", "permission_denied"),
        ("No route to host", "transport_unreachable"),
    ],
)
def test_showmount_failures_are_classified(detail, expected) -> None:
    collector = _load_collector_module()

    assert collector._classify_showmount_failure(detail) == expected


def test_list_share_entries_emits_limit_callback_when_truncated() -> None:
    collector = _load_collector_module()
    callbacks: list[tuple[int, int]] = []

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
            on_limit_reached=lambda inspected, emitted: callbacks.append((inspected, emitted)),
        )
    )

    assert len(rows) == 1
    assert callbacks == [(1, 1)]


def test_list_share_entries_caps_inspection_before_extension_filtering() -> None:
    collector = _load_collector_module()
    callbacks: list[tuple[int, int]] = []
    inspected_names: list[str] = []

    class _Entry:
        def __init__(self, name: str):
            self._name = name

        def get_longname(self):
            inspected_names.append(self._name)
            return self._name

        def is_directory(self):
            return False

    class _Conn:
        def listPath(self, *_args, **_kwargs):
            return [_Entry(f"file-{index}.txt") for index in range(1000)]

    rows = list(
        collector.list_share_entries(
            _Conn(),
            "General",
            max_depth=1,
            max_entries=3,
            exclude_path_regex=None,
            extensions={".pdf"},
            on_limit_reached=lambda inspected, emitted: callbacks.append((inspected, emitted)),
        )
    )

    assert rows == []
    assert inspected_names == ["file-0.txt", "file-1.txt", "file-2.txt"]
    assert callbacks == [(3, 0)]


def test_list_share_entries_does_not_report_limit_at_exact_end_of_share() -> None:
    collector = _load_collector_module()
    callbacks: list[tuple[int, int]] = []

    class _Entry:
        def __init__(self, name: str):
            self._name = name

        def get_longname(self):
            return self._name

        def is_directory(self):
            return False

    class _Conn:
        def listPath(self, *_args, **_kwargs):
            return [_Entry("one.txt"), _Entry("two.txt"), _Entry("."), _Entry("..")]

    rows = list(
        collector.list_share_entries(
            _Conn(),
            "General",
            max_depth=1,
            max_entries=2,
            exclude_path_regex=None,
            extensions=None,
            on_limit_reached=lambda inspected, emitted: callbacks.append((inspected, emitted)),
        )
    )

    assert [row["name"] for row in rows] == ["one.txt", "two.txt"]
    assert callbacks == []


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
    stats = collector.Stats()
    ok = collector.scan_host("10.0.0.5", args, "run-1", SimpleNamespace(emit=lambda *_: None), stats, threading.Lock())

    assert ok is True
    assert calls == ["smb", "nfs"]
    assert stats.structural_coverage_gaps == 1


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


@pytest.mark.parametrize(
    ("raw_username", "expected_username", "expected_domain", "expected_local_auth"),
    [
        (r"CONTOSO\alice", "alice", "CONTOSO", False),
        ("CONTOSO/alice", "alice", "CONTOSO", False),
        ("alice@contoso.example", "alice", "contoso.example", False),
        (r".\alice", "alice", "", True),
        ("./alice", "alice", "", True),
        ("alice", "alice", "", False),
    ],
)
def test_normalize_smb_identity_supports_established_ad_forms(
    raw_username, expected_username, expected_domain, expected_local_auth
) -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(username=raw_username, domain="", local_auth=False)

    collector._normalize_smb_identity(args)

    assert args.username == expected_username
    assert args.domain == expected_domain
    assert args.local_auth is expected_local_auth


def test_normalize_smb_identity_allows_matching_explicit_domain() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(username=r"contoso\alice", domain="CONTOSO", local_auth=False)

    collector._normalize_smb_identity(args)

    assert args.username == "alice"
    assert args.domain == "contoso"


@pytest.mark.parametrize(
    ("username", "domain", "local_auth", "message"),
    [
        (r"CONTOSO\alice", "OTHER", False, "conflicting SMB domains"),
        (r"CONTOSO\alice", "", True, "--local-auth cannot be combined"),
        (r"CONTOSO\alice/extra", "", False, "ambiguous --username"),
        (r"CONTOSO\alice\extra", "", False, "exactly one identity separator"),
        ("CONTOSO\\", "", False, "components must be non-empty"),
        ("@contoso.example", "", False, "components must be non-empty"),
    ],
)
def test_normalize_smb_identity_rejects_ambiguous_or_conflicting_forms(username, domain, local_auth, message) -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(username=username, domain=domain, local_auth=local_auth)

    with pytest.raises(SystemExit, match=message):
        collector._normalize_smb_identity(args)


def test_validate_args_normalizes_domain_username_before_authentication() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        cidr=["10.0.0.0/24"],
        hosts=None,
        share_types="smb",
        kerberos=False,
        smb_anonymous=False,
        use_session_creds=False,
        username=r"CONTOSO\svc_scan",
        password="secret",
        domain="",
        local_auth=False,
        hashes=None,
        ccache=None,
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
    )

    collector._validate_args(args)

    assert args.username == "svc_scan"
    assert args.domain == "CONTOSO"


def test_validate_args_rejects_kerberos_without_domain() -> None:
    collector = _load_collector_module()
    args = SimpleNamespace(
        cidr=["10.0.0.0/24"],
        hosts=None,
        share_types="smb",
        kerberos=True,
        smb_anonymous=False,
        use_session_creds=False,
        username="svc_scan",
        password="secret",
        domain="",
        local_auth=False,
        hashes=None,
        ccache=None,
        upload=False,
        api_base=None,
        project_id=None,
        api_token=None,
    )

    with pytest.raises(SystemExit, match="--kerberos requires --domain"):
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


@pytest.mark.parametrize(
    ("filename", "gzip_output", "message"),
    [
        ("scan.txt", False, "--output must end"),
        ("scan.ndjson", True, "filename ending in .gz"),
        ("scan.json.gz", False, "requires --gzip"),
    ],
)
def test_validate_args_rejects_ambiguous_artifact_suffixes(tmp_path, filename, gzip_output, message) -> None:
    collector = _load_collector_module()
    args = collector.parse_args(
        [
            "--cidr",
            "10.0.0.0/30",
            "--smb-anonymous",
            "--output",
            str(tmp_path / filename),
            *(["--gzip"] if gzip_output else []),
        ]
    )

    with pytest.raises(SystemExit, match=message):
        collector._validate_args(args)


@pytest.mark.parametrize("filename", ["scan.json", "scan.ndjson", "scan.jsonl.gz"])
def test_validate_args_accepts_supported_artifact_suffixes(tmp_path, filename) -> None:
    collector = _load_collector_module()
    args = collector.parse_args(
        [
            "--cidr",
            "10.0.0.0/30",
            "--smb-anonymous",
            "--output",
            str(tmp_path / filename),
            *(["--gzip"] if filename.endswith(".gz") else []),
        ]
    )

    collector._validate_args(args)


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

    assert redacted == [
        "--username",
        "svc",
        "--password",
        "<redacted>",
        "--hashes=<redacted>",
        "--api-token",
        "<redacted>",
    ]


@pytest.mark.parametrize("abbreviated_flag", ["--pass", "--hash", "--api-t"])
def test_parse_args_rejects_abbreviated_secret_flags(abbreviated_flag) -> None:
    collector = _load_collector_module()

    with pytest.raises(SystemExit):
        collector.parse_args(["--hosts", "hosts.txt", abbreviated_flag, "secret"])


def test_parse_args_reads_secrets_from_environment(monkeypatch) -> None:
    collector = _load_collector_module()
    monkeypatch.setenv(collector.SMB_PASSWORD_ENV, "password-from-env")
    monkeypatch.setenv(collector.API_TOKEN_ENV, "token-from-env")

    args = collector.parse_args(
        [
            "--hosts",
            "hosts.txt",
            "--username",
            "svc",
            "--upload",
            "--api-base",
            "https://sentinel.example.test/api",
            "--project-id",
            "00000000-0000-4000-8000-000000000001",
        ]
    )

    assert args.password == "password-from-env"
    assert args.api_token == "token-from-env"


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


def test_scan_host_smb_passes_normalized_domain_identity_to_impacket(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Conn:
        def __init__(self):
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
        def emit(self, _record):
            return None

    args = SimpleNamespace(
        timeout=1.0,
        kerberos=False,
        smb_anonymous=False,
        username=r"CONTOSO\svc_scan",
        password="secret",
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
    collector._normalize_smb_identity(args)

    ok = collector.scan_host_smb("10.0.0.5", args, "run-domain-1", _Writer(), collector.Stats(), threading.Lock())

    assert ok is True
    assert fake_conn.login_args == ("svc_scan", "secret", "CONTOSO", "", "")


def test_scan_host_smb_reports_authenticated_guest_fallback(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Conn:
        def login(self, *_args, **_kwargs):
            return None

        def isGuestSession(self):
            return True

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
    records = []
    writer = SimpleNamespace(records=records, emit=records.append, write_failed=False)
    args = SimpleNamespace(
        timeout=1.0,
        kerberos=False,
        smb_anonymous=False,
        username="svc_scan",
        password="secret",
        domain="CONTOSO",
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
    stats = collector.Stats()

    ok = collector.scan_host_smb("10.0.0.5", args, "run-guest-1", writer, stats, threading.Lock())

    assert ok is True
    assert stats.structural_coverage_gaps == 1
    assert stats.error_codes["SMB_AUTH_GUEST_FALLBACK"] == 1
    assert any(record.get("code") == "SMB_AUTH_GUEST_FALLBACK" for record in records)
    endpoint = next(record for record in records if record.get("type") == "endpoint")
    assert endpoint["metadata"]["session_kind"] == "guest"
    assert endpoint["metadata"]["session_identity_source"] == "server_session"


def test_scan_host_smb_kerberos_does_not_mutate_ccache_env_per_call(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            self.use_cache = None
            self.login_args = None

        def kerberosLogin(self, *args, **kwargs):
            self.use_cache = kwargs.get("useCache")
            self.login_args = (args, kwargs)
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
        password="",
        domain="EXAMPLE",
        ccache_env_value="FILE:/tmp/alternate-cache",
        hashes="lmhash:nthash",
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
    assert fake_conn.login_args[0] == ("svc", "", "EXAMPLE")
    assert fake_conn.login_args[1]["lmhash"] == "lmhash"
    assert fake_conn.login_args[1]["nthash"] == "nthash"
    assert os.environ.get("KRB5CCNAME") == "FILE:/tmp/original-cache"


def test_help_text_contains_common_and_examples_sections() -> None:
    collector = _load_collector_module()
    help_text = collector._build_parser().format_help()

    assert "Common Options" in help_text
    assert "SMB Authentication" in help_text
    assert "Examples:" in help_text
    assert "--use-session-creds" in help_text
    assert "DOMAIN/USER" in help_text
    assert "USER@REALM" in help_text
    assert "unquoted single backslash is removed by the shell" in help_text


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
    assert stats.endpoints == 1
    assert stats.resources == 0
    assert stats.errors == 1
    assert stats.structural_coverage_gaps == 1
    endpoint_record = next(row for row in writer.records if row.get("type") == "endpoint")
    assert endpoint_record["endpoint_key"] == "10.0.0.6:445"
    error_record = next(row for row in writer.records if row.get("type") == "error")
    assert error_record["code"] == "LIST_SHARES_DENIED"
    assert "--include-share" in error_record["hint"]


@pytest.mark.parametrize(
    ("status_code", "reason_code"),
    [
        (0xC000035C, "transport_failure"),  # STATUS_NETWORK_SESSION_EXPIRED
        (0xC00000BB, "unsupported_request"),  # STATUS_NOT_SUPPORTED
        (0xC00000A2, "write_protected"),  # not an enumeration authorization denial
    ],
)
def test_scan_host_smb_treats_share_enumeration_session_failures_as_host_failures(
    monkeypatch, status_code, reason_code
) -> None:
    collector = _load_collector_module()

    class _SessionFailure(Exception):
        def getErrorCode(self):
            return status_code

    monkeypatch.setattr(collector, "SessionError", _SessionFailure)

    class _Conn:
        def __init__(self, *_args, **_kwargs):
            self.logged_off = False

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "768"

        def isSigningRequired(self):
            return False

        def listShares(self):
            raise _SessionFailure()

        def logoff(self):
            self.logged_off = True

    connection = _Conn()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)

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

    ok = collector.scan_host_smb("10.0.0.7", args, "run-1", writer, stats, threading.Lock())

    assert ok is False
    assert connection.logged_off is True
    assert stats.endpoints == 1
    assert stats.resources == 0
    assert stats.error_codes["LIST_SHARES_FAILED"] == 1
    error_record = next(row for row in writer.records if row.get("type") == "error")
    assert error_record["severity"] == "error"
    assert error_record["code"] == "LIST_SHARES_FAILED"
    assert reason_code in error_record["message"]
    assert f"0x{status_code:08X}" in error_record["message"]


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


def test_scan_host_smb_preserves_empty_endpoint_when_shares_are_filtered_out(monkeypatch) -> None:
    collector = _load_collector_module()

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
            return [{"shi1_netname": "Hidden$\x00", "shi1_remark": "admin\x00"}]

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
        smb_anonymous=True,
        username="",
        password="",
        domain="",
        ccache=None,
        hashes=None,
        local_auth=False,
        include_share=[],
        exclude_share=["Hidden$"],
        exclude_path_regex=None,
        exclude_path_pattern=None,
        extensions_only=None,
        max_depth=1,
        max_entries_per_share=10,
    )
    writer = _Writer()
    stats = collector.Stats()

    ok = collector.scan_host_smb("10.0.0.9", args, "run-empty", writer, stats, threading.Lock())

    assert ok is True
    assert stats.endpoints == 1
    assert stats.resources == 0
    assert [row["type"] for row in writer.records] == ["endpoint"]


def test_scan_host_nfs_preserves_empty_endpoint_when_no_exports_found(monkeypatch) -> None:
    collector = _load_collector_module()

    class _SocketConn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Writer:
        def __init__(self):
            self.records = []

        def emit(self, record):
            self.records.append(record)

    monkeypatch.setattr(
        collector,
        "_probe_nfs_v4_null",
        lambda *_args, **_kwargs: collector.NFSV4NullProbe(
            "reachable", "nfs_service_confirmed", "version_not_supported"
        ),
    )
    monkeypatch.setattr(
        collector,
        "_discover_nfs_exports",
        lambda *_args, **_kwargs: collector.NFSExportDiscovery((), "complete"),
    )

    args = SimpleNamespace(timeout=1.0, domain="")
    writer = _Writer()
    stats = collector.Stats()

    ok = collector.scan_host_nfs("10.0.0.10", args, "run-nfs-empty", writer, stats, threading.Lock())

    assert ok is True
    assert stats.endpoints == 1
    assert stats.resources == 0
    assert [row["type"] for row in writer.records] == ["endpoint"]
    assert writer.records[0]["auth"] == {
        "method": "not_assessed",
        "success": None,
        "reason": "NFS NULL and export-discovery calls do not authenticate filesystem access",
    }


def test_scan_host_nfs_marks_export_enumeration_failure_as_structural_gap(monkeypatch) -> None:
    collector = _load_collector_module()

    class _SocketConn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    records = []
    writer = SimpleNamespace(records=records, emit=records.append)
    monkeypatch.setattr(
        collector,
        "_probe_nfs_v4_null",
        lambda *_args, **_kwargs: collector.NFSV4NullProbe(
            "reachable", "nfs_service_confirmed", "version_not_supported"
        ),
    )
    monkeypatch.setattr(
        collector,
        "_discover_nfs_exports",
        lambda *_args, **_kwargs: ([], "rpc mount service denied the request"),
    )
    stats = collector.Stats()

    ok = collector.scan_host_nfs(
        "10.0.0.10",
        SimpleNamespace(timeout=1.0, domain=""),
        "run-nfs-denied",
        writer,
        stats,
        threading.Lock(),
    )

    assert ok is True
    assert stats.structural_coverage_gaps == 1
    assert stats.error_codes["NFS_EXPORT_ENUM_FAILED"] == 1
    assert any(record.get("code") == "NFS_EXPORT_ENUM_FAILED" for record in records)


def test_scan_host_nfs_reports_v4_only_namespace_as_partial_without_overclaim(monkeypatch) -> None:
    collector = _load_collector_module()
    records = []
    monkeypatch.setattr(
        collector,
        "_probe_nfs_v4_null",
        lambda *_args, **_kwargs: collector.NFSV4NullProbe("reachable", "nfs_v4_confirmed", "supported"),
    )
    monkeypatch.setattr(
        collector,
        "_discover_nfs_exports",
        lambda *_args, **_kwargs: collector.NFSExportDiscovery(
            (), "mount_protocol_unavailable", "RPC: Program not registered"
        ),
    )
    stats = collector.Stats()

    assert collector.scan_host_nfs(
        "nfs-v4.example",
        SimpleNamespace(timeout=1.0, domain=""),
        "run-nfs-v4",
        SimpleNamespace(emit=records.append),
        stats,
        threading.Lock(),
    )

    endpoint = next(record for record in records if record["type"] == "endpoint")
    assert endpoint["nfs"]["service_status"] == "nfs_v4_confirmed"
    assert endpoint["nfs"]["structural_coverage"] == "partial"
    assert endpoint["auth"]["success"] is None
    assert stats.structural_coverage_gaps == 1
    assert stats.error_codes["NFS_V4_NAMESPACE_NOT_ENUMERATED"] == 1


def test_scan_host_nfs_keeps_indeterminate_v4_probe_structurally_partial(monkeypatch) -> None:
    collector = _load_collector_module()
    records = []
    monkeypatch.setattr(
        collector,
        "_probe_nfs_v4_null",
        lambda *_args, **_kwargs: collector.NFSV4NullProbe("reachable", "indeterminate", "response_timeout"),
    )
    monkeypatch.setattr(
        collector,
        "_discover_nfs_exports",
        lambda *_args, **_kwargs: collector.NFSExportDiscovery(("/legacy",), "complete", observed_export_lines=1),
    )
    stats = collector.Stats()

    assert collector.scan_host_nfs(
        "nfs-unknown.example",
        SimpleNamespace(timeout=1.0, domain=""),
        "run-nfs-unknown",
        SimpleNamespace(emit=records.append),
        stats,
        threading.Lock(),
    )

    endpoint = next(record for record in records if record["type"] == "endpoint")
    assert endpoint["nfs"]["structural_coverage"] == "partial"
    assert endpoint["auth"]["success"] is None
    assert stats.structural_coverage_gaps == 1
    assert stats.error_codes["NFS_V4_PROBE_INDETERMINATE"] == 1
    assert stats.error_codes["NFS_V4_NAMESPACE_NOT_ENUMERATED"] == 1


def test_scan_host_nfs_marks_bounded_export_truncation_as_structural_gap(monkeypatch) -> None:
    collector = _load_collector_module()
    records = []
    monkeypatch.setattr(
        collector,
        "_probe_nfs_v4_null",
        lambda *_args, **_kwargs: collector.NFSV4NullProbe(
            "reachable", "nfs_service_confirmed", "version_not_supported"
        ),
    )
    monkeypatch.setattr(
        collector,
        "_discover_nfs_exports",
        lambda *_args, **_kwargs: collector.NFSExportDiscovery(
            ("/retained",),
            "truncated",
            "bounded",
            observed_export_lines=2,
            exports_truncated=True,
            limitations=("nfs_export_path_character_limit_reached",),
        ),
    )
    stats = collector.Stats()

    assert collector.scan_host_nfs(
        "nfs-large.example",
        SimpleNamespace(timeout=1.0, domain=""),
        "run-nfs-large",
        SimpleNamespace(emit=records.append),
        stats,
        threading.Lock(),
    )

    endpoint = next(record for record in records if record["type"] == "endpoint")
    assert endpoint["nfs"]["export_discovery"]["exports_truncated"] is True
    assert endpoint["nfs"]["export_discovery"]["limits"]["export_path_characters"] == 255
    assert "nfs_export_path_character_limit_reached" in endpoint["nfs"]["limitations"]
    assert endpoint["nfs"]["structural_coverage"] == "partial"
    assert stats.structural_coverage_gaps == 1
    assert stats.error_codes["NFS_EXPORT_ENUM_TRUNCATED"] == 1


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
