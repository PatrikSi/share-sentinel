import importlib.util
import json
import struct
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


def _sid(*subauthorities: int, authority: int = 5) -> bytes:
    return (
        bytes((1, len(subauthorities)))
        + authority.to_bytes(6, "big")
        + b"".join(struct.pack("<L", value) for value in subauthorities)
    )


def _ace(ace_type: int, mask: int, sid: bytes, *, flags: int = 0, application_data: bytes = b"") -> bytes:
    body = struct.pack("<L", mask) + sid + application_data
    return struct.pack("<BBH", ace_type, flags, 4 + len(body)) + body


def _descriptor(*aces: bytes, null_dacl: bool = False, include_dacl: bool = True) -> bytes:
    owner = _sid(18)
    group = _sid(32, 545)
    owner_offset = 20
    group_offset = owner_offset + len(owner)
    control = 0x8000 | (0x0004 if include_dacl else 0)
    if include_dacl and not null_dacl:
        acl_body = b"".join(aces)
        acl = struct.pack("<BBHHH", 2, 0, 8 + len(acl_body), len(aces), 0) + acl_body
        dacl_offset = group_offset + len(group)
    else:
        acl = b""
        dacl_offset = 0
    header = struct.pack(
        "<BBHLLLL",
        1,
        0,
        control,
        owner_offset,
        group_offset,
        0,
        dacl_offset,
    )
    return header + owner + group + acl


def _assessment_kwargs(collector):
    return {
        "run_id": "run-1",
        "endpoint_key": "server:445",
        "resource_name": "Data",
        "provider_resource_id": "smb-share:v1:abc",
        "subject_path": "",
        "is_directory": True,
        "assessed_identity": {
            "assessed_identity_fingerprint": "smb-session-identity:v1:abc",
            "session_kind": "ntlm",
            "identity_source": "requested_identity",
        },
        "selection_scope": "share_root",
        "entry_budget": collector.SMB_PERMISSION_MAX_ENTRIES_PER_SHARE,
        "root_path_hint": None,
        "cancel_event": threading.Event(),
    }


def test_security_descriptor_parser_preserves_acl_order_flags_masks_and_sids() -> None:
    collector = _load_collector_module()
    descriptor = _descriptor(
        _ace(0x01, 0x00000002, _sid(21, 1000), flags=0x10),
        _ace(0x09, 0x80000001, _sid(1, 0), application_data=b"condition-not-retained"),
    )

    parsed = collector._parse_smb_security_descriptor(descriptor)

    assert parsed["provider_details"]["dacl_state"] == "present"
    assert parsed["provider_details"]["owner"]["native_id"] == "S-1-5-18"
    assert [entry["effect"] for entry in parsed["entries"]] == ["deny", "allow"]
    assert [entry["ordinal"] for entry in parsed["entries"]] == [0, 1]
    assert parsed["entries"][0]["inherited_state"] == "inherited"
    assert parsed["entries"][0]["principal"]["native_id"] == "S-1-5-21-1000"
    assert "write_data_or_add_file" in parsed["entries"][0]["normalized_rights"]
    assert parsed["entries"][1]["provider_details"]["application_data_present"] is True
    assert parsed["entries"][1]["provider_details"]["application_data_retained"] is False
    serialized = json.dumps(parsed)
    assert "condition-not-retained" not in serialized
    assert descriptor.hex() not in serialized


def test_security_descriptor_parser_distinguishes_absent_null_and_empty_dacl() -> None:
    collector = _load_collector_module()

    absent = collector._parse_smb_security_descriptor(_descriptor(include_dacl=False))
    null = collector._parse_smb_security_descriptor(_descriptor(null_dacl=True))
    empty = collector._parse_smb_security_descriptor(_descriptor())

    assert absent["provider_details"]["dacl_state"] == "absent"
    assert null["provider_details"]["dacl_state"] == "null"
    assert empty["provider_details"]["dacl_state"] == "empty"
    assert all(result["entries_emitted"] == 0 for result in (absent, null, empty))
    assert null["permission_summary"]["dacl_state"] == "null"
    assert len({result["entry_set_hash"] for result in (absent, null, empty)}) == 3


def test_security_descriptor_set_hash_includes_owner_and_dacl_control_state() -> None:
    collector = _load_collector_module()
    base = _descriptor(_ace(0x00, 0x1, _sid(11)))
    owner_changed = bytearray(base)
    owner_changed[28] = 19
    protected = bytearray(base)
    control = struct.unpack_from("<H", protected, 2)[0]
    struct.pack_into("<H", protected, 2, control | collector.SMB_SECURITY_DESCRIPTOR_DACL_PROTECTED)

    hashes = {
        collector._parse_smb_security_descriptor(payload)["entry_set_hash"]
        for payload in (base, bytes(owner_changed), bytes(protected))
    }

    assert len(hashes) == 3


def test_security_descriptor_parser_rejects_oversized_or_non_self_relative_data() -> None:
    collector = _load_collector_module()
    oversized = b"x" * (collector.SMB_PERMISSION_MAX_DESCRIPTOR_BYTES + 1)
    non_relative = struct.pack("<BBHLLLL", 1, 0, 0x0004, 0, 0, 0, 0)

    for payload in (oversized, non_relative):
        try:
            collector._parse_smb_security_descriptor(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed descriptor was accepted")


def test_security_descriptor_parser_caps_aces_and_reports_truthful_counts() -> None:
    collector = _load_collector_module()
    descriptor = _descriptor(
        *(_ace(0x00, 0x1, _sid(21, ordinal)) for ordinal in range(collector.SMB_PERMISSION_MAX_ACES_PER_DESCRIPTOR + 1))
    )

    parsed = collector._parse_smb_security_descriptor(descriptor)

    assert parsed["entries_observed"] == collector.SMB_PERMISSION_MAX_ACES_PER_DESCRIPTOR + 1
    assert parsed["entries_emitted"] == collector.SMB_PERMISSION_MAX_ACES_PER_DESCRIPTOR
    assert parsed["entries_omitted"] == 1
    assert parsed["truncated"] is True
    assert "ace_emission_limit_reached" in parsed["limitations"]


def test_smb2_permission_query_uses_read_control_owner_group_dacl_and_closes_handle() -> None:
    collector = _load_collector_module()
    descriptor = _descriptor(_ace(0x00, 0x1, _sid(11)))

    class _LowLevel:
        def __init__(self):
            self.calls = []

        def queryInfo(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return descriptor

    class _Connection:
        def __init__(self):
            self.low = _LowLevel()
            self.opens = []
            self.closes = []

        def openFile(self, tree_id, path, **kwargs):
            self.opens.append((tree_id, path, kwargs))
            return b"file-id"

        def getSMBServer(self):
            return self.low

        def closeFile(self, tree_id, file_id):
            self.closes.append((tree_id, file_id))

    conn = _Connection()
    assessment, entries, fatal = collector._build_smb_permission_records(conn, 7, **_assessment_kwargs(collector))

    assert fatal is False
    assert assessment["assessment_state"] == "complete"
    assert assessment["semantics"] == "smb_windows_acl_v1"
    assert assessment["effective_access_status"] == "not_computed"
    assert assessment["negative_conclusion_supported"] is True
    assert assessment["provider_details"]["assessed_identity_fingerprint"] == ("smb-session-identity:v1:abc")
    assert len(entries) == 1
    assert conn.opens[0][2]["desiredAccess"] == collector.READ_CONTROL
    assert conn.opens[0][2]["creationDisposition"] == collector.FILE_OPEN
    query_kwargs = conn.low.calls[0][1]
    assert query_kwargs["infoType"] == collector.SMB2_0_INFO_SECURITY
    assert query_kwargs["additionalInformation"] == (
        collector.OWNER_SECURITY_INFORMATION
        | collector.GROUP_SECURITY_INFORMATION
        | collector.DACL_SECURITY_INFORMATION
    )
    assert query_kwargs["additionalInformation"] & 0x8 == 0
    assert conn.closes == [(7, b"file-id")]


def test_smb1_permission_query_uses_query_sec_info_and_closes_handle() -> None:
    collector = _load_collector_module()
    descriptor = _descriptor(_ace(0x01, 0x2, _sid(11)))

    class _LowLevel:
        def __init__(self):
            self.calls = []

        def query_sec_info(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return descriptor

    class _Connection:
        def __init__(self):
            self.low = _LowLevel()
            self.closed = 0

        def openFile(self, *_args, **_kwargs):
            return 99

        def getSMBServer(self):
            return self.low

        def closeFile(self, *_args):
            self.closed += 1

    conn = _Connection()
    assessment, entries, fatal = collector._build_smb_permission_records(conn, 4, **_assessment_kwargs(collector))

    assert fatal is False
    assert assessment["provider_details"]["query_protocol"] == "smb1_nt_trans_query_security_desc"
    assert len(entries) == 1
    assert conn.low.calls == [
        (
            (4, 99),
            {"additional_information": collector.SMB_PERMISSION_REQUESTED_INFORMATION},
        )
    ]
    assert conn.closed == 1


def test_denied_security_query_emits_failed_evidence_and_still_closes_handle(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Denied(Exception):
        def getErrorCode(self):
            return 0xC0000022

    monkeypatch.setattr(collector, "SessionError", _Denied)

    class _LowLevel:
        def queryInfo(self, *_args, **_kwargs):
            raise _Denied("STATUS_ACCESS_DENIED")

    class _Connection:
        def __init__(self):
            self.closed = 0

        def openFile(self, *_args, **_kwargs):
            return b"file-id"

        def getSMBServer(self):
            return _LowLevel()

        def closeFile(self, *_args):
            self.closed += 1

    conn = _Connection()
    assessment, entries, fatal = collector._build_smb_permission_records(conn, 7, **_assessment_kwargs(collector))

    assert fatal is False
    assert entries == []
    assert assessment["outcome"] == "denied"
    assert assessment["assessment_state"] == "failed"
    assert assessment["error_code"] == "permission_read_denied"
    assert assessment["negative_conclusion_supported"] is False
    assert conn.closed == 1


def test_failed_security_query_preserves_transport_fatal_close_error(monkeypatch) -> None:
    collector = _load_collector_module()

    class _Denied(Exception):
        def getErrorCode(self):
            return 0xC0000022

    monkeypatch.setattr(collector, "SessionError", _Denied)

    class _LowLevel:
        def queryInfo(self, *_args, **_kwargs):
            raise _Denied("STATUS_ACCESS_DENIED")

    class _Connection:
        def openFile(self, *_args, **_kwargs):
            return b"file-id"

        def getSMBServer(self):
            return _LowLevel()

        def closeFile(self, *_args):
            raise OSError("connection lost while closing handle")

    assessment, entries, fatal = collector._build_smb_permission_records(
        _Connection(), 7, **_assessment_kwargs(collector)
    )

    assert entries == []
    assert assessment["outcome"] == "denied"
    assert assessment["error_code"] == "permission_read_denied"
    assert [error["code"] for error in assessment["errors"]] == [
        "permission_read_denied",
        "handle_close_failed",
    ]
    assert "handle_cleanup_failed_session_will_be_closed" in assessment["limitations"]
    assert fatal is True


def test_handle_close_transport_failure_marks_assessment_partial_and_aborts_more_samples() -> None:
    collector = _load_collector_module()
    descriptor = _descriptor(_ace(0x00, 0x1, _sid(11)))

    class _LowLevel:
        def queryInfo(self, *_args, **_kwargs):
            return descriptor

    class _Connection:
        def openFile(self, *_args, **_kwargs):
            return b"file-id"

        def getSMBServer(self):
            return _LowLevel()

        def closeFile(self, *_args):
            raise OSError("connection lost while closing handle")

    assessment, entries, fatal = collector._build_smb_permission_records(
        _Connection(), 7, **_assessment_kwargs(collector)
    )

    assert len(entries) == 1
    assert assessment["assessment_state"] == "partial"
    assert assessment["error_code"] == "handle_close_failed"
    assert fatal is True


def test_permission_candidate_selection_is_deterministic_and_bounded() -> None:
    collector = _load_collector_module()
    paths = [("\\z", True), ("\\a", True), ("\\b.txt", False), ("\\x.txt", False)]
    first = collector._SMBPermissionCandidateSelector("resource", 1)
    second = collector._SMBPermissionCandidateSelector("resource", 1)
    for path, is_directory in paths:
        first.consider(path, is_directory)
    for path, is_directory in reversed(paths):
        second.consider(path, is_directory)

    assert first.selected() == second.selected()
    assert len(first.selected()) == 2
    assert {is_directory for _, is_directory in first.selected()} == {True, False}


def test_scan_emits_assessment_before_entries_and_keeps_access_evidence_separate(monkeypatch) -> None:
    collector = _load_collector_module()
    descriptor = _descriptor(_ace(0x00, 0x1, _sid(11)))

    class _LowLevel:
        def queryInfo(self, *_args, **_kwargs):
            return descriptor

    class _Connection:
        def login(self, *_args, **_kwargs):
            return None

        def getDialect(self):
            return "785"

        def isSigningRequired(self):
            return True

        def listShares(self):
            return [{"shi1_netname": "Data\x00", "shi1_remark": "\x00"}]

        def connectTree(self, _share_name):
            return 7

        def listPath(self, *_args):
            return []

        def openFile(self, *_args, **_kwargs):
            return b"file-id"

        def closeFile(self, *_args):
            return None

        def getSMBServer(self):
            return _LowLevel()

        def disconnectTree(self, _tree_id):
            return None

        def logoff(self):
            return None

    monkeypatch.setattr(collector, "SMBConnection", lambda *_args, **_kwargs: _Connection())
    records = []
    writer = SimpleNamespace(records=records, emit=records.append, write_failed=False)
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
        smb_permissions="root",
        smb_permission_sample_limit=2,
        cancel_event=threading.Event(),
    )
    stats = collector.Stats()

    assert collector.scan_host_smb("10.0.0.8", args, "run-1", writer, stats, threading.Lock()) is True

    record_types = [record["type"] for record in records]
    assessment_index = record_types.index("permission_assessment")
    entry_index = record_types.index("permission_entry")
    assert assessment_index < entry_index
    assessment = records[assessment_index]
    assert assessment["subject_kind"] == "share_root"
    assert assessment["subject_path"] == "\\"
    assert assessment["assessment_state"] == "complete"
    final_resource = [record for record in records if record["type"] == "resource"][-1]
    assert final_resource["permission_summary"]["assessment_state"] == "complete"
    assert final_resource["permission_summary"]["entries"] == 1
    assert final_resource["access_capabilities"]["_metadata"]["assessment_summary"] == "list_observed"
    assert stats.permission_assessments == 1
    assert stats.permission_entries == 1


def test_compact_writer_keeps_normalized_permission_assessment_and_entries(tmp_path) -> None:
    collector = _load_collector_module()
    output = tmp_path / "artifact.json"
    writer = collector.NDJSONWriter(str(output), gzip_output=False)
    writer.emit(
        {
            "type": "run_meta",
            "schema_version": 2,
            "artifact_features": ["direct_permissions_v1"],
            "collection_context": {
                "provider": "smb",
                "assessed_identity": "identity-fingerprint",
                "materialized_snapshot": True,
            },
            "run_id": "run-1",
        }
    )
    writer.emit(
        {
            "type": "endpoint",
            "run_id": "run-1",
            "endpoint_key": "server:445",
            "provider": "smb",
            "provider_endpoint_id": "server-id",
        }
    )
    writer.emit(
        {
            "type": "resource",
            "run_id": "run-1",
            "endpoint_key": "server:445",
            "share_type": "smb",
            "resource_type": "smb_share",
            "name": "Data",
            "provider_resource_id": "resource-id",
        }
    )
    writer.emit(
        {
            "type": "permission_assessment",
            "run_id": "run-1",
            "endpoint_key": "server:445",
            "share_type": "smb",
            "resource_type": "smb_share",
            "resource_name": "Data",
            "assessment_key": "assessment-id",
            "semantics": "smb_windows_acl_v1",
            "provider_resource_id": "resource-id",
        }
    )
    writer.emit(
        {
            "type": "permission_entry",
            "run_id": "run-1",
            "endpoint_key": "server:445",
            "share_type": "smb",
            "resource_type": "smb_share",
            "resource_name": "Data",
            "assessment_key": "assessment-id",
            "entry_key": "entry-id",
            "ordinal": 0,
            "effect": "allow",
        }
    )
    writer.emit({"type": "run_end", "run_id": "run-1", "stats": {"resources": 1}})
    writer.close()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["artifact_features"] == ["direct_permissions_v1"]
    assert payload["collection_context"] == {
        "provider": "smb",
        "assessed_identity": "identity-fingerprint",
        "materialized_snapshot": True,
    }
    share = payload["endpoints"][0]["shares"][0]
    assessment = share["permission_assessments"][0]
    assert assessment["assessment_key"] == "assessment-id"
    assert assessment["semantics"] == "smb_windows_acl_v1"
    assert assessment["entries"] == [
        {
            "assessment_key": "assessment-id",
            "entry_key": "entry-id",
            "ordinal": 0,
            "effect": "allow",
        }
    ]


def test_smb_permissions_cli_defaults_to_root_and_can_be_disabled() -> None:
    collector = _load_collector_module()
    default_args = collector.parse_args(["--cidr", "10.0.0.0/30", "--smb-anonymous"])
    disabled_args = collector.parse_args(["--cidr", "10.0.0.0/30", "--smb-anonymous", "--smb-permissions", "none"])

    assert default_args.smb_permissions == "root"
    assert default_args.smb_permission_sample_limit == collector.SMB_PERMISSION_DEFAULT_SAMPLE_LIMIT
    assert disabled_args.smb_permissions == "none"


def test_main_announces_schema_v2_from_first_record_when_smb_permissions_are_enabled(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    output = tmp_path / "artifact.ndjson"
    args = collector.parse_args(
        [
            "--cidr",
            "10.0.0.5/32",
            "--smb-anonymous",
            "--smb-permissions",
            "root",
            "--output",
            str(output),
            "--quiet",
        ]
    )
    monkeypatch.setattr(collector, "parse_args", lambda: args)
    monkeypatch.setattr(collector, "SMBConnection", object())

    def _scan_targets(_targets, _args, run_id, writer, stats, lock):
        writer.emit(
            {
                "type": "endpoint",
                "run_id": run_id,
                "endpoint_key": "10.0.0.5:445",
            }
        )
        with lock:
            stats.endpoints += 1
        return collector.ScanOutcome(1, 1, 0)

    monkeypatch.setattr(collector, "_scan_targets", _scan_targets)

    assert collector.main() == collector.EXIT_SUCCESS
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records[0]["type"] == "run_meta"
    assert records[0]["schema_version"] == 2
    assert records[0]["artifact_features"] == ["direct_permissions_v1"]
    assert records[0]["collection_context"]["provider"] == "smb"
    assert records[0]["collection_context"]["materialized_snapshot"] is True
    assert records[0]["collection_context"]["discovery_completeness"] == "authoritative"
    assert records[0]["collection_context"]["metadata"]["structural_complete"] is True
    assert records[0]["collection_context"]["metadata"]["comparison_contracts"] == {
        "structural": "network_share_inventory_v1",
        "content": "smb_tree_inventory_v1",
        "capability": "smb_nonmutating_capability_v1",
    }
    assert records[0]["collection_context"]["assessed_identity"]
    assert records[0]["collection"]["enumeration"]["smb_permissions"] == "root"
    assert records[-1]["stats"]["permission_assessments"] == 0
