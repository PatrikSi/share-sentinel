import importlib.util
import json
import re
import socket
import sys
import threading
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


def test_capability_evidence_aggregates_allowed_denied_and_inconclusive() -> None:
    collector = _load_collector_module()
    capabilities = collector._new_access_capabilities()

    collector._record_capability(capabilities, "read_file", "allowed")
    collector._record_capability(capabilities, "read_file", "denied")
    collector._record_capability(capabilities, "read_file", "inconclusive")

    assert capabilities["read_file"] == {
        "status": "mixed",
        "attempted": 3,
        "allowed": 1,
        "denied": 1,
        "inconclusive": 1,
    }
    assert capabilities["modify_file"]["status"] == "not_tested"
    assert collector._legacy_access_level(capabilities) == "readable"


def test_legacy_access_distinguishes_explicit_denial_from_inconclusive_results() -> None:
    collector = _load_collector_module()

    denied = collector._new_access_capabilities()
    collector._record_capability(denied, "tree_connect", "denied")
    assert collector._legacy_access_level(denied) == "no_access"

    inconclusive = collector._new_access_capabilities()
    collector._record_capability(inconclusive, "list", "inconclusive")
    assert collector._legacy_access_level(inconclusive) == "unknown"

    listable = collector._new_access_capabilities()
    collector._record_capability(listable, "list", "allowed")
    assert collector._legacy_access_level(listable) == "list_only"

    connected_but_list_denied = collector._new_access_capabilities()
    collector._record_capability(connected_but_list_denied, "tree_connect", "allowed")
    collector._record_capability(connected_but_list_denied, "list", "denied")
    assert collector._legacy_access_level(connected_but_list_denied) == "unknown"


@pytest.mark.parametrize(("error_class", "error_code"), [(1, 5), (2, 4)])
def test_smb1_legacy_access_denials_use_exact_class_code_pair(error_class, error_code) -> None:
    collector = _load_collector_module()

    class _LegacyPacket:
        def __getitem__(self, field_name):
            return {"ErrorClass": error_class, "ErrorCode": error_code}[field_name]

    class _LegacySessionError(Exception):
        def getErrorCode(self):
            return error_code

        def getErrorPacket(self):
            return _LegacyPacket()

    assert collector._smb_probe_outcome(_LegacySessionError()) == "denied"


@pytest.mark.parametrize(("error_class", "error_code"), [(None, 4), (None, 5), (1, 4), (2, 5), (1, 12)])
def test_smb1_ambiguous_or_non_denial_codes_remain_inconclusive(
    error_class, error_code
) -> None:
    collector = _load_collector_module()

    class _LegacyPacket:
        def __getitem__(self, field_name):
            return {"ErrorClass": error_class, "ErrorCode": error_code}[field_name]

    class _LegacySessionError(Exception):
        def getErrorCode(self):
            return error_code

        def getErrorPacket(self):
            return _LegacyPacket() if error_class is not None else None

    assert collector._smb_probe_outcome(_LegacySessionError()) == "inconclusive"


def test_access_probe_limit_cli_supports_disable_and_rejects_unbounded_values(monkeypatch) -> None:
    monkeypatch.delenv("SHARE_SENTINEL_SMB_PASSWORD", raising=False)
    monkeypatch.delenv("SHARE_SENTINEL_SMB_HASHES", raising=False)
    collector = _load_collector_module()
    disabled = collector.parse_args(["--hosts", "hosts.txt", "--access-probe-limit", "0"])

    collector._validate_args(disabled)
    assert disabled.access_probe_limit == 0

    excessive = collector.parse_args(["--hosts", "hosts.txt", "--access-probe-limit", "101"])
    with pytest.raises(SystemExit, match="--access-probe-limit must be between 0 and 100"):
        collector._validate_args(excessive)


@pytest.mark.parametrize("dialect", ["NT LM 0.12", "2.1", "3.1.1"])
def test_root_probe_uses_dialect_independent_empty_path_and_non_mutating_open(dialect) -> None:
    collector = _load_collector_module()
    capabilities = collector._new_access_capabilities()

    class _Connection:
        def __init__(self):
            self.opens = []
            self.closed = []

        def getDialect(self):
            return dialect

        def openFile(self, tree_id, path, **kwargs):
            self.opens.append((tree_id, path, kwargs))
            return f"handle-{len(self.opens)}"

        def closeFile(self, tree_id, file_id):
            self.closed.append((tree_id, file_id))

    connection = _Connection()
    collector._probe_smb_handle_access(
        connection,
        7,
        "",
        is_directory=True,
        desired_access=collector.FILE_ADD_FILE,
        capability="create_file",
        capabilities=capabilities,
        cancel_event=None,
    )

    assert len(connection.opens) == 1
    tree_id, path, kwargs = connection.opens[0]
    assert tree_id == 7
    assert path == ""
    assert kwargs["desiredAccess"] == collector.FILE_ADD_FILE
    assert kwargs["creationDisposition"] == collector.FILE_OPEN
    assert kwargs["creationOption"] == collector.FILE_DIRECTORY_FILE
    assert kwargs["shareMode"] == (
        collector.FILE_SHARE_READ | collector.FILE_SHARE_WRITE | collector.FILE_SHARE_DELETE
    )
    assert connection.closed == [(7, "handle-1")]
    assert capabilities["create_file"]["status"] == "allowed"


def test_probe_candidates_are_captured_before_extension_output_filtering() -> None:
    collector = _load_collector_module()
    candidates = []

    class _Entry:
        def get_longname(self):
            return "secret.txt"

        def is_directory(self):
            return False

        def get_filesize(self):
            return 10

    class _Connection:
        def listPath(self, *_args):
            return [_Entry()]

    rows = list(
        collector.list_share_entries(
            _Connection(),
            "Reports",
            max_depth=1,
            max_entries=10,
            exclude_path_regex=None,
            extensions={".pdf"},
            on_probe_candidate=lambda path, is_dir: candidates.append((path, is_dir)),
        )
    )

    assert rows == []
    assert candidates == [("\\secret.txt", False)]


def test_nested_probe_discovery_honors_exclusion_and_entry_bounds() -> None:
    collector = _load_collector_module()

    class _Entry:
        def __init__(self, name):
            self.name = name

        def get_longname(self):
            return self.name

        def is_directory(self):
            return False

    class _Connection:
        def __init__(self):
            self.calls = []

        def listPath(self, _share_name, wildcard):
            self.calls.append(wildcard)
            return [_Entry("one.txt"), _Entry("two.txt")]

    connection = _Connection()
    directory_samples = []
    file_samples = []
    result = collector._discover_smb_probe_candidates(
        connection,
        "Data",
        directory_seeds=["\\Allowed", "\\Excluded"],
        directory_samples=directory_samples,
        file_samples=file_samples,
        probe_limit=3,
        max_entries=1,
        exclude_path_regex=re.compile(r"^\\Excluded(?:\\|$)"),
        already_listed_paths={""},
    )

    assert result == (0, 1, True, 1)
    assert connection.calls == ["Allowed\\*"]
    assert file_samples == ["\\Allowed\\one.txt"]


def test_nested_probe_discovery_honors_preexisting_cancellation() -> None:
    collector = _load_collector_module()
    cancel_event = threading.Event()
    cancel_event.set()

    class _Connection:
        def listPath(self, *_args):
            raise AssertionError("cancelled discovery must not list a directory")

    assert collector._discover_smb_probe_candidates(
        _Connection(),
        "Data",
        directory_seeds=["\\Folder"],
        directory_samples=[],
        file_samples=[],
        probe_limit=3,
        max_entries=10,
        exclude_path_regex=None,
        already_listed_paths={""},
        cancel_event=cancel_event,
    ) == (0, 0, False, 0)


def test_scan_smb_emits_bounded_non_mutating_capabilities_and_disconnects(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Denied(Exception):
        def getErrorCode(self):
            return 0xC0000022

    monkeypatch.setattr(collector, "SessionError", _Denied)

    class _Entry:
        def __init__(self, name, is_directory):
            self.name = name
            self.directory = is_directory

        def get_longname(self):
            return self.name

        def is_directory(self):
            return self.directory

        def get_filesize(self):
            return 42

        def get_allocsize(self):
            return 4096

        def get_wtime_epoch(self):
            return 1_700_000_000

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.opens = []
            self.closed = []
            self.disconnected = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return True

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "mixed access\x00"}]

        def connectTree(self, share_name):
            assert share_name == "Data"
            return 41

        def listPath(self, share_name, wildcard):
            assert (share_name, wildcard) == ("Data", "*")
            return [_Entry("Folder", True), _Entry("secret.txt", False)]

        def openFile(self, tree_id, path, **kwargs):
            assert tree_id == 41
            self.opens.append((path, kwargs))
            desired_access = kwargs["desiredAccess"]
            is_directory = kwargs["creationOption"] == collector.FILE_DIRECTORY_FILE
            if is_directory and desired_access in {
                collector.FILE_ADD_SUBDIRECTORY,
                collector.WRITE_DAC,
                collector.WRITE_OWNER,
            }:
                raise _Denied()
            if not is_directory and desired_access in {collector.FILE_WRITE_DATA, collector.DELETE}:
                raise _Denied()
            return f"file-{len(self.opens)}"

        def closeFile(self, tree_id, file_id):
            self.closed.append((tree_id, file_id))

        def disconnectTree(self, tree_id):
            self.disconnected.append(tree_id)

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        extensions_only=".pdf",
        max_depth=1,
        max_entries_per_share=10,
        access_probe_limit=1,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.8", args, "run-access", writer, collector.Stats(), threading.Lock()
    ) is True

    resources = [record for record in writer.records if record.get("type") == "resource"]
    assert resources[0]["access_level"] == "unknown"
    assert resources[0]["access_capabilities"]["_metadata"]["complete"] is False
    final = resources[-1]
    assert final["access_level"] == "readable"
    assert final["access_capabilities"]["tree_connect"]["status"] == "allowed"
    assert final["access_capabilities"]["list"]["status"] == "allowed"
    assert final["access_capabilities"]["read_file"]["status"] == "allowed"
    assert final["access_capabilities"]["create_file"]["status"] == "allowed"
    assert final["access_capabilities"]["create_directory"]["status"] == "denied"
    assert final["access_capabilities"]["modify_file"]["status"] == "denied"
    assert final["access_capabilities"]["delete"]["status"] == "mixed"
    assert final["access_capabilities"]["write_acl"]["status"] == "denied"
    assert final["access_capabilities"]["write_owner"]["status"] == "denied"
    metadata = final["access_capabilities"]["_metadata"]
    assert metadata == {
        "probe_method": "non_mutating_handle_open",
        "coverage": "bounded_sample",
        "probe_limit": 1,
        "partial": True,
        "complete": True,
        "directory_samples": 1,
        "file_samples": 1,
        "directory_candidates_seen": 1,
        "file_candidates_seen": 1,
        "listing_truncated": False,
    }
    assert any(
        path == "secret.txt" and kwargs["desiredAccess"] == collector.FILE_READ_DATA
        for path, kwargs in connection.opens
    )
    assert not any(
        record.get("type") == "item" and record.get("name") == "secret.txt"
        for record in writer.records
    )
    assert connection.disconnected == [41]
    assert len(connection.closed) == 5


def test_default_depth_discovers_nested_only_file_for_bounded_access_probes(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Entry:
        def __init__(self, name, is_directory):
            self.name = name
            self.directory = is_directory

        def get_longname(self):
            return self.name

        def is_directory(self):
            return self.directory

        def get_filesize(self):
            return 17

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.list_calls = []
            self.opens = []
            self.closed = []
            self.disconnected = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 17

        def listPath(self, share_name, wildcard):
            self.list_calls.append((share_name, wildcard))
            if wildcard == "*":
                return [_Entry("OnlyFolder", True)]
            if wildcard == "OnlyFolder\\*":
                return [_Entry("nested-only.txt", False)]
            raise AssertionError(f"unexpected wildcard: {wildcard}")

        def openFile(self, tree_id, path, **kwargs):
            self.opens.append((tree_id, path, kwargs))
            return f"handle-{len(self.opens)}"

        def closeFile(self, tree_id, file_id):
            self.closed.append((tree_id, file_id))

        def disconnectTree(self, tree_id):
            self.disconnected.append(tree_id)

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        max_entries_per_share=10,
        access_probe_limit=1,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.11", args, "run-nested", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    assert final["access_level"] == "readable"
    assert final["access_capabilities"]["list"]["attempted"] == 2
    assert final["access_capabilities"]["read_file"]["status"] == "allowed"
    assert final["access_capabilities"]["modify_file"]["status"] == "allowed"
    assert final["access_capabilities"]["delete"]["status"] == "allowed"
    metadata = final["access_capabilities"]["_metadata"]
    assert metadata["directory_candidates_seen"] == 1
    assert metadata["file_candidates_seen"] == 1
    assert metadata["file_samples"] == 1
    assert connection.list_calls == [("Data", "*"), ("Data", "OnlyFolder\\*")]
    assert any(
        path == "OnlyFolder\\nested-only.txt"
        and kwargs["desiredAccess"] == collector.FILE_READ_DATA
        for _tree_id, path, kwargs in connection.opens
    )
    assert not any(
        record.get("type") == "item" and record.get("name") == "nested-only.txt"
        for record in writer.records
    )
    assert len(connection.closed) == len(connection.opens)
    assert connection.disconnected == [17]


def test_probe_discovery_uses_unlisted_directory_at_configured_depth_boundary(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Entry:
        def __init__(self, name, is_directory):
            self.name = name
            self.directory = is_directory

        def get_longname(self):
            return self.name

        def is_directory(self):
            return self.directory

        def get_filesize(self):
            return 23

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.list_calls = []
            self.opens = []
            self.closed = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 27

        def listPath(self, _share_name, wildcard):
            self.list_calls.append(wildcard)
            if wildcard == "*":
                return [_Entry("A", True)]
            if wildcard == "A\\*":
                return [_Entry("Boundary", True)]
            if wildcard == "A\\Boundary\\*":
                return [_Entry("deep.txt", False)]
            raise AssertionError(f"unexpected wildcard: {wildcard}")

        def openFile(self, tree_id, path, **kwargs):
            self.opens.append((tree_id, path, kwargs))
            return f"handle-{len(self.opens)}"

        def closeFile(self, tree_id, file_id):
            self.closed.append((tree_id, file_id))

        def disconnectTree(self, _tree_id):
            return None

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        max_depth=2,
        max_entries_per_share=10,
        access_probe_limit=1,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.15", args, "run-boundary", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    assert final["access_level"] == "readable"
    assert final["access_capabilities"]["read_file"]["status"] == "allowed"
    assert final["access_capabilities"]["list"]["attempted"] == 3
    assert connection.list_calls == ["*", "A\\*", "A\\Boundary\\*"]
    assert any(
        path == "A\\Boundary\\deep.txt"
        and kwargs["desiredAccess"] == collector.FILE_READ_DATA
        for _tree_id, path, kwargs in connection.opens
    )
    assert not any(
        record.get("type") == "item" and record.get("name") == "deep.txt"
        for record in writer.records
    )
    assert len(connection.closed) == len(connection.opens)


def test_nested_listing_transport_failure_trips_share_probe_circuit(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Entry:
        def get_longname(self):
            return "OnlyFolder"

        def is_directory(self):
            return True

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.opens = []
            self.closed = []
            self.disconnected = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 18

        def listPath(self, _share_name, wildcard):
            if wildcard == "*":
                return [_Entry()]
            if wildcard == "OnlyFolder\\*":
                raise socket.timeout("nested listing timed out")
            raise AssertionError(f"unexpected wildcard: {wildcard}")

        def openFile(self, tree_id, path, **kwargs):
            self.opens.append((tree_id, path, kwargs))
            return f"handle-{len(self.opens)}"

        def closeFile(self, tree_id, file_id):
            self.closed.append((tree_id, file_id))

        def disconnectTree(self, tree_id):
            self.disconnected.append(tree_id)

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        max_entries_per_share=10,
        access_probe_limit=3,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.14", args, "run-nested-timeout", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    list_evidence = final["access_capabilities"]["list"]
    assert list_evidence["attempted"] == 2
    assert list_evidence["allowed"] == 1
    assert list_evidence["inconclusive"] == 1
    assert len(connection.opens) == 5
    assert all(path == "" for _tree_id, path, _kwargs in connection.opens)
    assert len(connection.closed) == len(connection.opens)
    assert final["access_capabilities"]["read_file"]["status"] == "not_tested"
    assert connection.disconnected == [18]


def test_inventory_transport_failure_stops_queued_lists_and_later_handle_probes(
    monkeypatch,
) -> None:
    collector = _load_collector_module()

    class _Entry:
        def __init__(self, name):
            self.name = name

        def get_longname(self):
            return self.name

        def is_directory(self):
            return True

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.list_calls = []
            self.opens = []
            self.closed = []
            self.disconnected = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 29

        def listPath(self, _share_name, wildcard):
            self.list_calls.append(wildcard)
            if wildcard == "*":
                return [_Entry("A"), _Entry("B")]
            if wildcard == "A\\*":
                raise socket.timeout("inventory listing timed out")
            raise AssertionError(f"listing continued after transport failure: {wildcard}")

        def openFile(self, tree_id, path, **kwargs):
            self.opens.append((tree_id, path, kwargs))
            return f"handle-{len(self.opens)}"

        def closeFile(self, tree_id, file_id):
            self.closed.append((tree_id, file_id))

        def disconnectTree(self, tree_id):
            self.disconnected.append(tree_id)

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        max_depth=2,
        max_entries_per_share=10,
        access_probe_limit=2,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.16", args, "run-list-circuit", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    list_evidence = final["access_capabilities"]["list"]
    assert list_evidence["attempted"] == 2
    assert list_evidence["allowed"] == 1
    assert list_evidence["inconclusive"] == 1
    assert connection.list_calls == ["*", "A\\*"]
    assert len(connection.opens) == 5
    assert all(path == "" for _tree_id, path, _kwargs in connection.opens)
    assert len(connection.closed) == len(connection.opens)
    assert final["access_capabilities"]["read_file"]["status"] == "not_tested"
    assert connection.disconnected == [29]


def test_handle_probe_transport_failure_trips_share_circuit_breaker(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Entry:
        def get_longname(self):
            return "visible.txt"

        def is_directory(self):
            return False

        def get_filesize(self):
            return 1

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.open_calls = 0
            self.disconnected = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 19

        def openFile(self, *_args, **_kwargs):
            self.open_calls += 1
            raise socket.timeout("probe timed out")

        def listPath(self, *_args):
            return [_Entry()]

        def disconnectTree(self, tree_id):
            self.disconnected.append(tree_id)

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        max_entries_per_share=10,
        access_probe_limit=100,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.12", args, "run-circuit", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    assert connection.open_calls == 1
    assert final["access_level"] == "list_only"
    assert final["access_capabilities"]["create_file"] == {
        "status": "inconclusive",
        "attempted": 1,
        "allowed": 0,
        "denied": 0,
        "inconclusive": 1,
    }
    assert final["access_capabilities"]["create_directory"]["status"] == "not_tested"
    assert final["access_capabilities"]["read_file"]["status"] == "not_tested"
    assert final["access_capabilities"]["_metadata"]["complete"] is True
    assert connection.disconnected == [19]


def test_disabled_probes_keep_tree_allowed_list_denied_access_unknown(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Denied(Exception):
        def getErrorCode(self):
            return 5

        def getErrorPacket(self):
            return {"ErrorClass": 1, "ErrorCode": 5}

    monkeypatch.setattr(collector, "SessionError", _Denied)

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.disconnected = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "NT LM 0.12"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 21

        def listPath(self, *_args):
            raise _Denied()

        def openFile(self, *_args, **_kwargs):
            raise AssertionError("explicit probes are disabled")

        def disconnectTree(self, tree_id):
            self.disconnected.append(tree_id)

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        max_entries_per_share=10,
        access_probe_limit=0,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.13", args, "run-no-probes", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    assert final["access_level"] == "unknown"
    assert final["access_capabilities"]["tree_connect"]["status"] == "allowed"
    assert final["access_capabilities"]["list"]["status"] == "denied"
    assert final["access_capabilities"]["_metadata"]["coverage"] == "disabled"
    assert connection.disconnected == [21]


def test_cancellation_closes_granted_handle_and_disconnects_tree(monkeypatch) -> None:
    collector = _load_collector_module()
    cancel_event = threading.Event()

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            self.closed = []
            self.disconnected = []

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 9

        def openFile(self, _tree_id, _path, **_kwargs):
            cancel_event.set()
            return "root-handle"

        def closeFile(self, tree_id, file_id):
            self.closed.append((tree_id, file_id))

        def disconnectTree(self, tree_id):
            self.disconnected.append(tree_id)

        def listPath(self, *_args):
            raise AssertionError("listing should stop after cancellation")

        def logoff(self):
            return None

    connection = _Connection()
    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        max_entries_per_share=10,
        access_probe_limit=1,
        cancel_event=cancel_event,
    )

    assert collector.scan_host_smb(
        "10.0.0.9", args, "run-cancel", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    assert final["access_capabilities"]["_metadata"]["complete"] is False
    assert connection.closed == [(9, "root-handle")]
    assert connection.disconnected == [9]


def test_scan_marks_listing_truncation_in_final_capability_metadata(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Entry:
        def __init__(self, name):
            self.name = name

        def get_longname(self):
            return self.name

        def is_directory(self):
            return False

        def get_filesize(self):
            return 1

    class _Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "768"

        def isSigningRequired(self):
            return False

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def listPath(self, *_args):
            return [_Entry("one.txt"), _Entry("two.txt")]

        def logoff(self):
            return None

    monkeypatch.setattr(collector, "SMBConnection", _Connection)
    writer = SimpleNamespace(records=[], emit=lambda record: writer.records.append(record))
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
        access_probe_limit=0,
        cancel_event=threading.Event(),
    )

    assert collector.scan_host_smb(
        "10.0.0.10", args, "run-limit", writer, collector.Stats(), threading.Lock()
    ) is True

    final = [record for record in writer.records if record.get("type") == "resource"][-1]
    metadata = final["access_capabilities"]["_metadata"]
    assert metadata["listing_truncated"] is True
    assert metadata["coverage"] == "disabled"
    assert metadata["complete"] is True


def test_compact_writer_retains_capabilities_and_extended_item_metadata(tmp_path) -> None:
    collector = _load_collector_module()
    output_path = tmp_path / "capabilities.json"
    writer = collector.NDJSONWriter(str(output_path), gzip_output=False)
    capabilities = collector._new_access_capabilities()
    collector._record_capability(capabilities, "read_file", "allowed")
    capability_document = collector._access_capability_snapshot(
        capabilities,
        probe_limit=1,
        partial=True,
        complete=True,
        file_samples=1,
        file_candidates_seen=1,
    )

    writer.emit({"type": "run_meta", "schema_version": 1, "run_id": "run-1"})
    writer.emit({"type": "endpoint", "endpoint_key": "host:445"})
    writer.emit(
        {
            "type": "resource",
            "endpoint_key": "host:445",
            "name": "Data",
            "share_type": "smb",
            "access_level": "readable",
            "access_capabilities": capability_document,
        }
    )
    writer.emit(
        {
            "type": "item",
            "endpoint_key": "host:445",
            "resource_name": "Data",
            "share_type": "smb",
            "path": "\\report.pdf",
            "name": "report.pdf",
            "is_dir": False,
            "size_bytes": 123,
            "allocation_size_bytes": 4096,
            "mtime": "2026-08-24T00:00:00+00:00",
            "created_at": "2026-08-20T00:00:00+00:00",
            "accessed_at": "2026-08-24T01:00:00+00:00",
            "changed_at": "2026-08-24T02:00:00+00:00",
            "file_attributes": ["archive", "read_only"],
        }
    )
    writer.emit({"type": "run_end", "stats": {}})
    writer.close()

    share = json.loads(output_path.read_text(encoding="utf-8"))["endpoints"][0]["shares"][0]
    assert share["access_capabilities"] == capability_document
    assert share["entries"][0]["allocation_size_bytes"] == 4096
    assert share["entries"][0]["created_at"] == "2026-08-20T00:00:00+00:00"
    assert share["entries"][0]["file_attributes"] == ["archive", "read_only"]
