import copy
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import main


def _smb_assessment(**overrides):
    record = {
        "type": "permission_assessment",
        "run_id": "11111111-1111-1111-1111-111111111111",
        "assessment_key": "producer-assessment-key",
        "subject_key": "producer-subject-key",
        "provider": "smb",
        "semantics": "smb_windows_acl_v1",
        "permission_surface": "smb_filesystem_dacl",
        "endpoint_key": "server:445",
        "resource_name": "Finance",
        "provider_resource_id": "stable-share-id",
        "subject_kind": "share_root",
        "subject_path": "\\",
        "method": "smb_query_security_info_read_control",
        "assessment_state": "complete",
        "selection_scope": "share_root",
        "selection_coverage": "exhaustive_for_scope",
        "retrieval_coverage": "complete",
        "provider_visibility": "provider_visible",
        "semantic_coverage": "acl_structure_only",
        "principal_resolution": "well_known_only",
        "effective_access_status": "not_computed",
        "negative_conclusion_supported": True,
        "entries_observed": 0,
        "entries_emitted": 0,
        "entries_omitted": 0,
        "unknown_entries": 0,
        "entry_set_hash": "a" * 64,
        "provider_details": {
            "descriptor_revision": 1,
            "descriptor_control_retained": 32772,
            "descriptor_control_flags": ["self_relative", "dacl_present"],
            "descriptor_size": 128,
            "owner_state": "observed",
            "owner": {"native_id": "S-1-5-32-544", "display_name": "Administrators"},
            "group_state": "observed",
            "group": {"native_id": "S-1-5-18", "display_name": "SYSTEM"},
            "dacl_state": "empty",
            "dacl_revision": 2,
            "dacl_ace_count": 0,
            "query_protocol": "smb3",
            "handle_path_variant": "root_marker",
            "assessed_identity_fingerprint": "identity-a",
        },
    }
    record.update(overrides)
    return record


def _smb_entry(**overrides):
    record = {
        "type": "permission_entry",
        "run_id": "11111111-1111-1111-1111-111111111111",
        "assessment_key": "producer-assessment-key",
        "entry_key": "producer-entry-key",
        "provider": "smb",
        "semantics": "smb_windows_acl_v1",
        "permission_surface": "smb_filesystem_dacl",
        "entry_kind": "ace",
        "effect": "allow",
        "normalized_rights": ["read_data"],
        "inherited_state": "not_inherited",
        "ordinal": 0,
        "principal": {
            "provider": "smb",
            "principal_key": "producer-principal-key",
            "identifier_namespace": "windows_sid",
            "authority": "windows",
            "native_id": "S-1-5-32-544",
            "kind": "group",
            "display_name": "Administrators",
            "resolution": "well_known",
        },
        "provider_details": {
            "ace_type": "access_allowed",
            "ace_type_code": 0,
            "ace_flags": 0,
            "ace_flag_names": [],
            "ace_size": 20,
            "access_mask": "0x00000001",
        },
    }
    record.update(overrides)
    return record


def test_smb_assessment_hash_excludes_transport_and_presentation_provenance() -> None:
    smb3 = _smb_assessment()
    smb1 = copy.deepcopy(smb3)
    smb1["provider_details"].update(
        {
            "query_protocol": "smb1",
            "handle_path_variant": "relative",
            "descriptor_size": 256,
            "descriptor_control_flags": ["owner_defaulted"],
        }
    )

    main._normalize_permission_assessment(smb3)
    main._normalize_permission_assessment(smb1)

    assert smb3["evidence_hash"] == smb1["evidence_hash"]
    assert smb3["provider_details"]["query_protocol"] == "smb3"
    assert smb1["provider_details"]["query_protocol"] == "smb1"


def test_consumer_owned_principal_and_entry_hashes_ignore_forged_keys_and_labels() -> None:
    baseline = _smb_entry()
    same = copy.deepcopy(baseline)
    same["principal"]["principal_key"] = "reused-attacker-key"
    same["principal"]["display_name"] = "Localized administrators"
    same["provider_details"]["ace_type"] = "localized label"
    changed = copy.deepcopy(same)
    changed["normalized_rights"] = ["write_data"]

    for record in (baseline, same, changed):
        main._normalize_permission_entry(record)

    assert baseline["principal_key"] == same["principal_key"]
    assert baseline["evidence_hash"] == same["evidence_hash"]
    assert baseline["evidence_hash"] != changed["evidence_hash"]


@pytest.mark.parametrize("missing_field", ["native_id", "authority"])
def test_permission_entry_rejects_principals_without_stable_provider_identity(missing_field: str) -> None:
    record = _smb_entry()
    record["principal"].pop(missing_field)

    with pytest.raises(ValueError, match="requires stable native_id and authority"):
        main._normalize_permission_entry(record)


def test_display_only_principals_cannot_collapse_to_the_same_evidence_identity() -> None:
    alice = _smb_entry()
    bob = copy.deepcopy(alice)
    for record, display_name in ((alice, "Alice"), (bob, "Bob")):
        record["principal"].pop("native_id")
        record["principal"].pop("authority")
        record["principal"]["display_name"] = display_name

        with pytest.raises(ValueError, match="requires stable native_id and authority"):
            main._normalize_permission_entry(record)


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "acl_payload_v2",
        "dacl_copy",
        "descriptor_blob_backup",
        "descriptor_backup",
        "invitation_redeem_uri_v2",
        "link_backup",
        "raw_dacl_v3",
        "raw_descriptor_v2",
        "sacl_bytes",
        "security_descriptor_copy",
        "security_descriptor_bytes_backup",
        "securitydescriptorvalue",
        "shareIdV2",
        "sharing_url_backup",
        "sddl_v2",
        "webHtmlV2",
        "web_url_backup",
    ],
)
def test_permission_payload_rejects_raw_acl_and_share_link_aliases(sensitive_key: str) -> None:
    with pytest.raises(ValueError, match="sensitive permission material"):
        main._normalize_permission_details(
            {"nested": {sensitive_key: "must-not-survive"}},
            field="provider_details",
        )


def test_permission_payload_preserves_reviewed_semantic_telemetry() -> None:
    details = {
        "descriptor_revision": 1,
        "descriptor_control_retained": 32772,
        "descriptor_control_flags": ["self_relative", "dacl_present"],
        "descriptor_size": 128,
        "dacl_state": "present",
        "dacl_revision": 2,
        "dacl_reserved_byte": 0,
        "dacl_reserved_word": 0,
        "dacl_size": 64,
        "dacl_ace_count": 2,
        "sacl_requested": False,
        "sacl_retained": False,
        "link_scope": "organization",
        "link_type": "view",
        "has_password": True,
        "invitation_sign_in_required": True,
    }

    assert main._normalize_permission_details(details, field="provider_details") == details


@pytest.mark.parametrize(
    "details",
    [
        {"descriptor_size": "QUJDREVGR0g=" * 100},
        {"descriptor_revision": "RAW_DESCRIPTOR_BYTES"},
        {"dacl_state": {"raw_bytes": "QUJD"}},
        {"link_scope": "https://tenant.example/bearer"},
    ],
)
def test_permission_payload_rejects_invalid_safe_telemetry_values(details: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid permission telemetry"):
        main._normalize_permission_details(details, field="provider_details")


@pytest.mark.parametrize(
    "details",
    [
        {"descriptor_control_retained": True},
        {"dacl_ace_count": "2"},
        {"dacl_reserved_byte": 256},
        {"dacl_reserved_word": -1},
        {"dacl_revision": 256},
        {"dacl_size": 65_536},
        {"has_password": "false"},
        {"invitation_sign_in_required": {}},
        {"sacl_requested": 0},
        {"sacl_retained": []},
        {"link_type": "download"},
        {"descriptor_control_flags": ["self_relative", "raw_descriptor"]},
        {"descriptor_control_flags": ["self_relative", "self_relative"]},
    ],
)
def test_permission_payload_rejects_malformed_reviewed_telemetry(details: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid permission telemetry"):
        main._normalize_permission_details(details, field="provider_details")


@pytest.mark.parametrize(
    "sensitive_key",
    ["url", "uri", "href", "html", "redeem_url", "anonymous_url", "raw", "raw_bytes", "binary", "blob", "payload"],
)
def test_permission_payload_rejects_unreviewed_locator_and_raw_aliases(sensitive_key: str) -> None:
    with pytest.raises(ValueError, match="sensitive permission material"):
        main._normalize_permission_details({sensitive_key: "must-not-survive"}, field="provider_details")


@pytest.mark.parametrize("sensitive_key", ["raw_descriptor_v2", "share_link_href_backup", "web_url_v3"])
def test_permission_errors_apply_sensitive_payload_policy(sensitive_key: str) -> None:
    assessment = _smb_assessment(errors=[{"code": "provider_failure", sensitive_key: "must-not-survive"}])

    with pytest.raises(ValueError, match="sensitive permission material"):
        main._normalize_permission_assessment(assessment)


def test_consumer_principal_key_preserves_provider_authority_scope() -> None:
    def sharepoint_entry(authority: str) -> dict:
        return {
            "type": "permission_entry",
            "run_id": "11111111-1111-1111-1111-111111111111",
            "assessment_key": "assessment",
            "entry_key": "entry",
            "provider": "sharepoint",
            "semantics": "sharepoint_graph_permission_v1",
            "permission_surface": "sharepoint_graph_permissions",
            "entry_kind": "identity_grant",
            "effect": "allow",
            "normalized_rights": ["read"],
            "inherited_state": "unknown",
            "principal": {
                "provider": "sharepoint",
                "identifier_namespace": "sharepoint_principal_id",
                "authority": authority,
                "native_id": "5",
                "kind": "site_user",
                "display_name": "Site Owners",
                "resolution": "provider_identifiers_no_group_expansion",
            },
            "provider_details": {"permission_kind": "identity_grant"},
        }

    site_a = sharepoint_entry("sharepoint:v1:" + "a" * 64)
    same_site_new_label = copy.deepcopy(site_a)
    same_site_new_label["principal"]["display_name"] = "Localized Owners"
    site_b = sharepoint_entry("sharepoint:v1:" + "b" * 64)

    for record in (site_a, same_site_new_label, site_b):
        main._normalize_permission_entry(record)

    assert site_a["principal_key"] == same_site_new_label["principal_key"]
    assert site_a["principal_key"] != site_b["principal_key"]


def test_principal_upsert_enforces_collision_contract_in_one_statement() -> None:
    class _Result:
        @staticmethod
        def fetchone():
            return (42,)

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((query, params))
            return _Result()

    entry = _smb_entry()
    main._normalize_permission_entry(entry)
    conn = _Conn()

    principal_id = main.upsert_permission_principal(conn, entry["run_id"], entry["principal"])

    sql = " ".join(conn.calls[0][0].split())
    assert principal_id == 42
    assert len(conn.calls) == 1
    assert "permission_principals.identifier_namespace = EXCLUDED.identifier_namespace" in sql
    assert "permission_principals.authority = EXCLUDED.authority" in sql


def test_successful_permission_entry_batch_releases_buffered_rows() -> None:
    class _Result:
        @staticmethod
        def fetchone():
            return None

    class _Conn:
        def __init__(self):
            self.call_count = 0

        def execute(self, _query, _params=None):
            self.call_count += 1
            return _Result()

    entry = _smb_entry()
    main._normalize_permission_entry(entry)
    rows = [(7, None, entry)]
    conn = _Conn()

    main.flush_permission_entry_batch(conn, entry["run_id"], rows)

    assert rows == []
    assert conn.call_count == 2


def test_consumer_owned_subject_key_ignores_producer_key_and_preserves_smb_path_case() -> None:
    root_a = _smb_assessment(subject_key="attacker-a")
    root_b = _smb_assessment(subject_key="attacker-b")
    directory_upper = _smb_assessment(subject_kind="directory", subject_path="\\Data")
    directory_lower = _smb_assessment(subject_kind="directory", subject_path="\\data")

    for record in (root_a, root_b, directory_upper, directory_lower):
        main._normalize_permission_assessment(record)

    assert root_a["subject_key"] == root_b["subject_key"]
    assert directory_upper["subject_key"] != directory_lower["subject_key"]


class _CaptureResult:
    def fetchall(self):
        return []


class _CaptureConn:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return _CaptureResult()


def test_integrity_reconciliation_requires_exact_hash_and_smb_contract_for_negative_claims() -> None:
    conn = _CaptureConn()
    main.reconcile_permission_evidence_integrity(conn, "run-1")
    sql = " ".join(conn.calls[0][0].split())

    assert "assessment.entry_set_hash IS NOT NULL" in sql
    assert "assessment.evidence_hash IS NOT NULL" in sql
    assert "assessment.semantics = 'smb_windows_acl_v1'" in sql
    assert "assessment.method = 'smb_query_security_info_read_control'" in sql
    assert "entry.principal_id IS NULL" in sql
    assert "entry.entry_kind IN ('link', 'invitation')" in sql
    assert "entry.entry_kind IN ('identity_grant', 'link', 'invitation')" in sql
    assert "entry.entry_kind = 'ace'" in sql
    assert "persisted.noncomparable_entries = 0" in sql
    assert "unknown_entries = GREATEST" in sql
    assert "INGESTED_ENTRY_PRINCIPAL_UNRESOLVED" in sql
    assert "INGESTED_ENTRY_KIND_INVALID" in sql
    assert "invalid_entry_kind_entries" in sql
    assert "noncomparable_entries" in sql


def test_integrity_reconciliation_allows_only_reviewed_principal_less_sharepoint_kinds() -> None:
    conn = _CaptureConn()
    main.reconcile_permission_evidence_integrity(conn, "run-1")
    sql = " ".join(conn.calls[0][0].split())

    principal_exception = (
        "assessment.provider = 'sharepoint' "
        "AND assessment.semantics = 'sharepoint_graph_permission_v1' "
        "AND assessment.permission_surface = 'sharepoint_graph_permissions' "
        "AND entry.entry_kind IN ('link', 'invitation')"
    )
    supported_sharepoint_kinds = (
        "assessment.provider = 'sharepoint' "
        "AND assessment.semantics = 'sharepoint_graph_permission_v1' "
        "AND assessment.permission_surface = 'sharepoint_graph_permissions' "
        "AND entry.entry_kind IN ('identity_grant', 'link', 'invitation')"
    )

    assert principal_exception in sql
    assert supported_sharepoint_kinds in sql
    assert "'unknown'" not in supported_sharepoint_kinds
    assert "'mixed'" not in supported_sharepoint_kinds


def test_permission_summaries_use_verified_persisted_entry_counts() -> None:
    conn = _CaptureConn()
    main.reconcile_permission_summaries(conn, "run-1")
    sql = " ".join(" ".join(query.split()) for query, _params in conn.calls)

    assert "FROM permission_entries AS entry" in sql
    assert "COALESCE(SUM(persisted.entry_count), 0)" in sql
    assert "COUNT(*) FILTER (WHERE unresolved_principal)" in sql
    assert "COUNT(*) FILTER (WHERE invalid_entry_kind)" in sql
    assert "COALESCE(persisted.noncomparable_entry_count, 0) = 0" in sql
    assert "unresolved_principal_entry_count" in sql
    assert "invalid_entry_kind_count" in sql
    assert "noncomparable_entry_count" in sql
    assert "declared_entry_count" in sql


def test_comparison_sql_marks_present_invalid_evidence_indeterminate() -> None:
    conn = _CaptureConn()
    emitted = main._materialize_comparison_batch(
        conn,
        "comparison-id",
        "baseline-run",
        "current-run",
        ["a" * 64],
        {
            "structural_interpretable": True,
            "content_interpretable": True,
            "access_interpretable": True,
            "capability_interpretable": True,
        },
    )
    sql = " ".join(conn.calls[0][0].split())

    assert emitted == 0
    assert "permission_evidence_present AND NOT direct_access_comparable" in sql
    assert "%(direct_permissions)s AND provider IN ('smb', 'sharepoint')" in sql
    assert "%(direct_permissions)s AND COALESCE(permission_rollup.directly_comparable" in sql
    assert "before_capability_present OR after_capability_present" in sql
    assert "before_capability_identity IS DISTINCT FROM after_capability_identity" in sql
    assert "before_items IS DISTINCT FROM after_items AND ( NOT %(content)s" in sql
    assert "WHEN NOT access_evidence_present THEN 'not_assessed'" in sql
    assert "WHEN capability_access_comparable THEN 'bounded capability observations were unchanged" in sql
    assert "resource.permission_summary->>'comparison_evidence_hash'" in sql
    assert "resource.permission_summary->>'comparison_quality_hash'" in sql
    assert "FROM permission_assessments AS assessment" not in sql
    assert "NOT %(structural)s" in sql
    assert "WHEN provider = 'smb' THEN lower(before_name) IS DISTINCT FROM lower(after_name)" in sql
    assert "WHEN provider IN ('smb', 'nfs') THEN lower(before_endpoint)" in sql
    assert sql.count("endpoint.provider_metadata->>'identity_strength' AS identity_strength") == 2
    assert "resource.provider_metadata->>'server_identity_strength'" not in sql
    assert "AND %(identity_scope_exact)s AND identity_strength = 'strong' THEN 'strong'" in sql
    assert sql.count("= 'sharepoint' THEN TRUE ELSE FALSE END AS content_observation_complete") == 2


def test_smb_identity_plane_requires_stable_valid_endpoint_identity() -> None:
    guid_a = "smb-server-guid:v1:" + ("a" * 64)
    guid_b = "smb-server-guid:v1:" + ("b" * 64)
    name_a = "smb-server-name:v1:" + ("a" * 64)
    stable_rows = [
        ("baseline", "FILES:445", guid_a, "server_guid", "strong"),
        ("current", "files:445", guid_a, "server_guid", "strong"),
    ]

    assert main._smb_identity_rows_stable(stable_rows, "baseline", "current") is True

    source_transition = [
        stable_rows[0],
        ("current", "files:445", name_a, "advertised_name", "moderate"),
    ]
    assert main._smb_identity_rows_stable(source_transition, "baseline", "current") is False

    replaced_server = [
        stable_rows[0],
        ("current", "files:445", guid_b, "server_guid", "strong"),
    ]
    assert main._smb_identity_rows_stable(replaced_server, "baseline", "current") is False
    assert main._smb_identity_rows_stable(stable_rows[:1], "baseline", "current") is False
    invalid_shape = [
        ("baseline", "files:445", "forged", "server_guid", "strong"),
        ("current", "files:445", "forged", "server_guid", "strong"),
    ]
    assert main._smb_identity_rows_stable(invalid_shape, "baseline", "current") is False


def test_smb_identity_plane_rejects_duplicate_casefolded_endpoint_keys() -> None:
    guid = "smb-server-guid:v1:" + ("a" * 64)
    rows = [
        ("baseline", "FILES:445", guid, "server_guid", "strong"),
        ("baseline", "files:445", guid, "server_guid", "strong"),
        ("current", "files:445", guid, "server_guid", "strong"),
    ]

    assert main._smb_identity_rows_stable(rows, "baseline", "current") is False


def test_smb_identity_status_is_constant_memory_and_distinguishes_strong_scope() -> None:
    class _StatusResult:
        @staticmethod
        def fetchone():
            return (True, False)

        @staticmethod
        def fetchall():
            raise AssertionError("identity preflight must not materialize endpoint rows")

    class _StatusConn:
        def __init__(self):
            self.query = ""
            self.params = None

        def execute(self, query, params=None):
            self.query = query
            self.params = params
            return _StatusResult()

    conn = _StatusConn()

    assert main._comparison_smb_identity_status(conn, "baseline", "current") == (
        True,
        False,
    )
    sql = " ".join(conn.query.split())
    assert "BOOL_AND(COALESCE(" in sql
    assert "COUNT(*) = COUNT(DISTINCT (run_id, endpoint_key))" in sql
    assert "baseline_minus_current" in sql
    assert "current_minus_baseline" in sql
    assert "smb-server-guid:v1:[0-9a-f]{64}" in sql
    assert "strong_identity_complete" in sql


def test_location_bound_smb_identity_prevents_exact_resource_summary() -> None:
    summary = main._build_comparison_summary(
        [],
        {
            "structural_interpretable": True,
            "content_interpretable": True,
            "identity_applicable": True,
            "identity_scope_exact": False,
            "access_interpretable": True,
            "capability_interpretable": True,
            "direct_permissions_interpretable": True,
            "direct_permissions_scope_exact": True,
        },
    )

    assert summary["dimensions"]["identity_scope"] is False
    assert summary["dimensions"]["identity_applicable"] is True
    assert summary["resource_summary_exact"] is False


def test_consumer_permission_integrity_requires_persisted_coverage() -> None:
    producer_context = {
        "metadata": {
            "permissions_assessed": True,
            "permissions_complete": True,
            "permission_assessment": {"candidate_objects": 2},
        }
    }

    verified, diagnostics = main._permission_collection_integrity(
        producer_context,
        assessment_count=2,
        assessed_resource_count=1,
        incomplete_assessment_count=0,
        relevant_resource_count=1,
        rejected_record_count=0,
    )

    assert verified["metadata"]["permissions_assessed"] is True
    assert verified["metadata"]["permissions_complete"] is True
    assert diagnostics["status"] == "verified_complete"
    assert diagnostics["expected_objects"] == 2


def test_invalid_permission_records_use_the_consumer_integrity_error_code() -> None:
    assert main._record_validation_error_code({"type": "permission_assessment"}) == ("PERMISSION_EVIDENCE_INVALID")
    assert main._record_validation_error_code({"type": "permission_entry"}) == "PERMISSION_EVIDENCE_INVALID"
    assert main._record_validation_error_code({"type": "endpoint"}) == main.CONSUMER_STRUCTURAL_RECORD_ERROR
    assert main._record_validation_error_code({"type": "resource"}) == main.CONSUMER_STRUCTURAL_RECORD_ERROR
    assert main._record_validation_error_code({"type": "item"}) == main.CONSUMER_CONTENT_RECORD_ERROR
    assert main._record_validation_error_code({"type": "unexpected"}) == main.CONSUMER_UNCLASSIFIED_RECORD_ERROR


@pytest.mark.parametrize(
    ("metrics", "reason"),
    [
        (
            {
                "assessment_count": 0,
                "assessed_resource_count": 0,
                "incomplete_assessment_count": 0,
                "relevant_resource_count": 1,
                "rejected_record_count": 0,
            },
            "resource_coverage_incomplete",
        ),
        (
            {
                "assessment_count": 2,
                "assessed_resource_count": 1,
                "incomplete_assessment_count": 0,
                "relevant_resource_count": 1,
                "rejected_record_count": 1,
            },
            "permission_records_rejected",
        ),
        (
            {
                "assessment_count": 1,
                "assessed_resource_count": 1,
                "incomplete_assessment_count": 0,
                "relevant_resource_count": 1,
                "rejected_record_count": 0,
            },
            "declared_object_coverage_incomplete",
        ),
        (
            {
                "assessment_count": 3,
                "assessed_resource_count": 1,
                "incomplete_assessment_count": 0,
                "relevant_resource_count": 1,
                "rejected_record_count": 0,
            },
            "declared_object_coverage_incomplete",
        ),
    ],
)
def test_consumer_permission_integrity_downgrades_untrusted_completeness(metrics, reason) -> None:
    context, diagnostics = main._permission_collection_integrity(
        {
            "metadata": {
                "permissions_assessed": True,
                "permissions_complete": True,
                "permission_assessment": {"candidate_objects": 2},
            }
        },
        **metrics,
    )

    assert context["metadata"]["permissions_complete"] is False
    assert diagnostics["status"] == "incomplete"
    assert reason in diagnostics["reasons"]


@pytest.mark.parametrize(
    ("producer_counts", "persisted_counts", "rejections", "structural", "content", "reason"),
    [
        (
            {"endpoints": 1, "resources": 1, "items": 1},
            {"endpoints": 1, "resources": 1, "items": 1},
            (1, 0, 0),
            False,
            False,
            "structural_records_rejected",
        ),
        (
            {"endpoints": 1, "resources": 1, "items": 1},
            {"endpoints": 1, "resources": 1, "items": 1},
            (0, 1, 0),
            True,
            False,
            "content_records_rejected",
        ),
        (
            {"endpoints": 1, "resources": 1, "items": 1},
            {"endpoints": 1, "resources": 1, "items": 1},
            (0, 0, 1),
            False,
            False,
            "unclassified_artifact_records_rejected",
        ),
        (
            {"endpoints": 1, "resources": 2, "items": 1},
            {"endpoints": 1, "resources": 1, "items": 1},
            (0, 0, 0),
            False,
            False,
            "resources_count_mismatch",
        ),
        (
            {"endpoints": 1, "resources": 1, "items": 2},
            {"endpoints": 1, "resources": 1, "items": 1},
            (0, 0, 0),
            True,
            False,
            "items_count_mismatch",
        ),
        (
            None,
            {"endpoints": 1, "resources": 1, "items": 1},
            (0, 0, 0),
            False,
            False,
            "producer_inventory_counts_missing_or_invalid",
        ),
    ],
)
def test_consumer_inventory_integrity_downgrades_only_affected_dimensions(
    producer_counts,
    persisted_counts,
    rejections,
    structural,
    content,
    reason,
) -> None:
    context, diagnostics = main._inventory_collection_integrity(
        {
            "metadata": {
                "structural_complete": True,
                "content_complete": True,
            }
        },
        producer_counts=producer_counts,
        persisted_counts=persisted_counts,
        structural_rejected_records=rejections[0],
        content_rejected_records=rejections[1],
        unclassified_rejected_records=rejections[2],
    )

    assert context["metadata"]["structural_complete"] is structural
    assert context["metadata"]["content_complete"] is content
    assert context["metadata"]["inventory_ingest"] == diagnostics
    assert reason in diagnostics["reasons"]


def test_consumer_inventory_integrity_never_promotes_producer_completeness() -> None:
    context, diagnostics = main._inventory_collection_integrity(
        {
            "metadata": {
                "structural_complete": False,
                "content_complete": False,
            }
        },
        producer_counts={"endpoints": 1, "resources": 1, "items": 1},
        persisted_counts={"endpoints": 1, "resources": 1, "items": 1},
        structural_rejected_records=0,
        content_rejected_records=0,
        unclassified_rejected_records=0,
    )

    assert diagnostics["status"] == "verified"
    assert context["metadata"]["structural_complete"] is False
    assert context["metadata"]["content_complete"] is False


@pytest.mark.parametrize(
    ("rejected_counts", "expected_structural", "expected_content"),
    [((1, 0, 0), False, False), ((0, 1, 0), True, False)],
)
def test_inventory_reconciliation_persists_consumer_dimension_downgrade(
    rejected_counts,
    expected_structural,
    expected_content,
) -> None:
    class _IntegrityConn:
        def __init__(self):
            self.persisted_context = None

        def execute(self, query, params=None):
            class _Result:
                def __init__(self, row):
                    self.row = row

                def fetchone(self):
                    return self.row

            if "structural_rejected_records" in query:
                return _Result(
                    (
                        {
                            "metadata": {
                                "structural_complete": True,
                                "content_complete": True,
                            }
                        },
                        *rejected_counts,
                    )
                )
            if "UPDATE scan_runs SET collection_context" in query:
                self.persisted_context = json.loads(params[0])
                return _Result(None)
            raise AssertionError(query)

    conn = _IntegrityConn()
    diagnostics = main.reconcile_inventory_collection_context(
        conn,
        "run-1",
        producer_counts={"endpoints": 1, "resources": 1, "items": 1},
        persisted_counts={"endpoints": 1, "resources": 1, "items": 1, "errors": 1},
    )

    assert diagnostics["status"] == "incomplete"
    assert conn.persisted_context["metadata"]["structural_complete"] is expected_structural
    assert conn.persisted_context["metadata"]["content_complete"] is expected_content
    assert conn.persisted_context["metadata"]["inventory_ingest"] == diagnostics


@pytest.mark.parametrize(
    "stats",
    [
        None,
        {},
        {"endpoints": True, "resources": 0, "items": 0},
        {"endpoints": 0, "resources": -1, "items": 0},
        {"endpoints": 0, "resources": 0, "items": "1"},
    ],
)
def test_producer_inventory_counts_require_complete_nonnegative_integer_stats(stats) -> None:
    assert main._validated_producer_inventory_counts(stats) is None


def test_identity_preparation_is_tenant_aware_and_preserves_nfs_case() -> None:
    conn = _CaptureConn()
    main.prepare_run_identity_keys(conn, "run-1")
    resource_sql = " ".join(conn.calls[0][0].split())

    assert "run.collection_context->>'tenant_id'" in resource_sql
    assert "IN ('smb', 'sharepoint') THEN lower(resource.name) ELSE resource.name" in resource_sql
    assert "resource.identity_key IS NULL" not in resource_sql


def test_assessment_collision_rejects_immutable_subject_reuse() -> None:
    class _CollisionConn:
        def execute(self, _query, _params=None):
            class _Result:
                @staticmethod
                def fetchone():
                    return (
                        "a" * 64,
                        7,
                        None,
                        "smb",
                        "smb_windows_acl_v1",
                        "smb_filesystem_dacl",
                        "different-subject",
                        "share_root",
                        "stable-share-id",
                        "\\",
                        "smb_query_security_info_read_control",
                    )

            return _Result()

    record = _smb_assessment()
    main._normalize_permission_assessment(record)
    with pytest.raises(ValueError, match="identity collides"):
        main.upsert_permission_assessment(_CollisionConn(), record["run_id"], 7, None, record)


def test_comparison_public_errors_do_not_disclose_database_details() -> None:
    error = psycopg.OperationalError("connection to host secret-db.internal failed")

    message = main._public_comparison_error(error)

    assert message == "database operation failed while comparing runs"
    assert "secret-db" not in message


def test_comparison_summary_is_not_exact_when_permission_evidence_was_not_assessed() -> None:
    compatibility = {
        "structural_interpretable": True,
        "content_interpretable": True,
        "access_interpretable": True,
        "capability_interpretable": True,
        "direct_permissions_interpretable": False,
        "direct_permissions_scope_exact": False,
    }

    summary = main._build_comparison_summary([("appeared", 2)], compatibility)

    assert summary["resource_summary_exact"] is False
    assert summary["dimensions"]["direct_permissions"] is False
    assert summary["exact"] is False
    assert summary["item_churn_computed"] is False


def test_comparison_summary_can_be_resource_exact_but_never_claims_item_exactness() -> None:
    compatibility = {
        "structural_interpretable": True,
        "content_interpretable": True,
        "access_interpretable": True,
        "capability_interpretable": True,
        "direct_permissions_interpretable": True,
        "direct_permissions_scope_exact": True,
    }

    summary = main._build_comparison_summary([("changed", 3)], compatibility)

    assert summary["resource_summary_exact"] is True
    assert summary["exact"] is False


def test_comparison_summary_treats_non_applicable_capabilities_as_satisfied() -> None:
    compatibility = {
        "structural_interpretable": True,
        "content_interpretable": True,
        "access_interpretable": True,
        "capability_applicable": False,
        "capability_interpretable": False,
        "direct_permissions_interpretable": True,
        "direct_permissions_scope_exact": True,
    }

    summary = main._build_comparison_summary([], compatibility)

    assert summary["resource_summary_exact"] is True
    assert summary["dimensions"]["capabilities"] is True
    assert summary["dimensions"]["capabilities_applicable"] is False


def test_comparison_summary_is_not_exact_for_bounded_permission_scope() -> None:
    summary = main._build_comparison_summary(
        [],
        {
            "structural_interpretable": True,
            "content_interpretable": True,
            "access_interpretable": True,
            "capability_interpretable": True,
            "direct_permissions_interpretable": True,
            "direct_permissions_scope_exact": False,
        },
    )

    assert summary["resource_summary_exact"] is False
    assert summary["dimensions"]["direct_permission_scope"] is False


def test_comparison_scope_exact_requires_both_runs_and_negative_support() -> None:
    class _ScopeResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _ScopeConn:
        def __init__(self, rows):
            self.rows = rows
            self.query = ""

        def execute(self, query, _params=None):
            self.query = query
            return _ScopeResult(self.rows)

    exact = _ScopeConn([("baseline", True), ("current", True)])
    bounded = _ScopeConn([("baseline", True), ("current", False)])

    assert main._comparison_direct_permission_scope_exact(exact, "baseline", "current") is True
    assert main._comparison_direct_permission_scope_exact(bounded, "baseline", "current") is False
    assert "resource.permission_summary->>'scope_exact'" in exact.query
    assert "resource.permission_summary->>'comparison_evidence_hash' IS NOT NULL" in exact.query


def test_comparison_retry_backoff_cannot_be_bypassed_by_stream_redelivery() -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

    assert main._comparison_retry_is_deferred("queued", now + timedelta(seconds=30), now=now) is True
    assert main._comparison_retry_is_deferred("queued", now - timedelta(seconds=1), now=now) is False
    assert main._comparison_retry_is_deferred("running", now + timedelta(seconds=30), now=now) is False
