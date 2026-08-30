import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.deps import AuthContext
from app.enums import ProjectRole
from app.routers import comparisons
from app.routers.comparisons import build_comparison_compatibility
from app.routers.comparisons import router as comparison_router
from app.routers.runs import router as runs_router
from fastapi import HTTPException


def _run(
    *,
    tenant_id: str = "tenant-a",
    partial: bool = False,
    permissions_assessed: bool = True,
    permissions_complete: bool = True,
    provider: str = "sharepoint",
    auth_mode: str = "client_secret",
    jwt_inspection: str = "unverified_metadata_only",
    discovery_authoritative: bool = True,
):
    content_supported = "nfs" not in provider.split("+")
    providers = set(provider.split("+"))
    target_scope = {
        "provider": "sharepoint",
        "targeted_sites": [],
        "max_sites": 10_000,
        "max_libraries": 100_000,
        "max_items": 1_000_000,
    }
    comparison_contracts: dict[str, str]
    if providers == {"sharepoint"}:
        comparison_contracts = {
            "structural": "sharepoint_resource_inventory_v1",
            "content": "sharepoint_drive_inventory_v1",
        }
    else:
        comparison_contracts = {"structural": "network_share_inventory_v1"}
        if "smb" in providers:
            comparison_contracts.update(
                {
                    "content": "smb_tree_inventory_v1",
                    "capability": "smb_nonmutating_capability_v1",
                }
            )
    enumeration: dict[str, object] = {}
    if "sharepoint" in providers:
        enumeration.update({"max_pages": 1000, "include_files": content_supported})
    if providers & {"smb", "nfs"}:
        target_scope = {
            "hosts": ["files.example.test"],
            "cidrs": [],
            "share_types": sorted(providers),
            "disabled_share_types": [],
            "target_count": 1,
        }
    if "smb" in providers:
        enumeration.update(
            {
                "max_depth": 5,
                "max_entries_per_share": 10_000,
                "access_probe_limit": 3,
                "include_share": [],
                "exclude_share": [],
                "exclude_path_regex": None,
                "extensions_only": None,
            }
        )
    return SimpleNamespace(
        collection_context={
            "source": provider,
            "provider": provider,
            "collection_mode": "tenant_inventory",
            "auth_mode": auth_mode,
            "discovery_completeness": "complete_for_granted_scope",
            "materialized_snapshot": True,
            "sync_mode": "full",
            "partial": partial,
            "auth_type": "application",
            "tenant_id": tenant_id,
            "client_id": "collector-app",
            "assessed_identity": None,
            "scopes": [],
            "roles": ["Sites.Read.All"],
            "jwt_inspection": jwt_inspection,
            "metadata": {
                "comparison_contracts": comparison_contracts,
                "structural_complete": not partial,
                "content_complete": not partial and content_supported,
                "files_included": content_supported,
                "permissions_assessed": permissions_assessed,
                "permissions_complete": permissions_complete,
                "discovery_strategy": "graph_sites_list",
                "discovery_authoritative": discovery_authoritative,
                "collection": {"target_scope": target_scope, "enumeration": enumeration},
            },
        }
    )


def test_detailed_comparison_and_legacy_diff_require_inventory_scope() -> None:
    restricted = AuthContext(
        user_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_project_id=uuid.uuid4(),
        token_role=ProjectRole.VIEWER,
        token_scopes=["read:runs"],
    )
    allowed = AuthContext(
        user_id=restricted.user_id,
        token_id=restricted.token_id,
        token_project_id=restricted.token_project_id,
        token_role=restricted.token_role,
        token_scopes=["read:runs", "read:inventory"],
    )
    routes = (
        next(
            route
            for route in comparison_router.routes
            if getattr(route, "path", "").endswith("/{comparison_id}/resource-changes")
        ),
        next(route for route in runs_router.routes if getattr(route, "path", "").endswith("/{run_id}/diff")),
    )
    for route in routes:
        scope_dependency = next(
            dependency.call
            for dependency in route.dependant.dependencies
            if getattr(dependency.call, "__name__", "") == "_checker"
        )
        with pytest.raises(HTTPException) as exc:
            scope_dependency(restricted)
        assert exc.value.status_code == 403
        assert scope_dependency(allowed) is allowed


def test_comparison_creation_requires_inventory_read_scope_for_tokens() -> None:
    project_id = uuid.uuid4()
    write_only = AuthContext(
        user_id=uuid.uuid4(),
        token_id=uuid.uuid4(),
        token_project_id=project_id,
        token_role=ProjectRole.OPERATOR,
        token_scopes=["write:runs"],
    )
    allowed_token = AuthContext(
        user_id=write_only.user_id,
        token_id=write_only.token_id,
        token_project_id=project_id,
        token_role=write_only.token_role,
        token_scopes=["write:runs", "read:inventory"],
    )
    default_operator = AuthContext(
        user_id=uuid.uuid4(),
        token_id=None,
        token_project_id=None,
        token_role=None,
        token_scopes=[],
    )
    create_route = next(
        route
        for route in comparison_router.routes
        if getattr(route, "path", "").endswith("/comparisons") and "POST" in getattr(route, "methods", set())
    )
    scope_dependency = next(
        dependency.call
        for dependency in create_route.dependant.dependencies
        if getattr(dependency.call, "__name__", "") == "_checker"
    )

    with pytest.raises(HTTPException) as exc:
        scope_dependency(write_only)
    assert exc.value.status_code == 403
    assert scope_dependency(allowed_token) is allowed_token
    assert scope_dependency(default_operator) is default_operator


def test_comparison_algorithm_version_advances_for_durable_item_history() -> None:
    assert comparisons.ALGORITHM_VERSION == "resource-evidence-v3"
    create_source = inspect.getsource(comparisons.create_comparison)
    assert "RunComparison.algorithm_version == ALGORITHM_VERSION" in create_source
    assert "RunComparison.options_hash == DEFAULT_OPTIONS_HASH" in create_source


def test_legacy_comparison_is_explicitly_non_authoritative_and_not_retryable() -> None:
    legacy = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_id=None,
        baseline_run_id=uuid.uuid4(),
        current_run_id=uuid.uuid4(),
        algorithm_version="resource-evidence-v2",
        trigger="manual",
        state="complete",
        compatibility={},
        progress={"phase": "complete"},
        summary={"total": 0},
        error_code=None,
        error_message=None,
        attempt_count=1,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=None,
        completed_at=None,
        heartbeat_at=None,
        next_retry_at=None,
    )

    payload = comparisons._comparison_out(legacy)

    assert payload.algorithm_current is False
    assert "may be incomplete" in (payload.algorithm_warning or "")
    with pytest.raises(HTTPException) as exc:
        comparisons._require_current_comparison_algorithm(legacy)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "COMPARISON_ALGORITHM_OBSOLETE"


def test_comparison_compatibility_accepts_same_complete_observation_plane() -> None:
    compatibility = build_comparison_compatibility(_run(provider="smb"), _run(provider="smb"))

    assert compatibility["status"] == "compatible"
    assert compatibility["structural_interpretable"] is True
    assert compatibility["direct_permissions_interpretable"] is True
    assert compatibility["direct_permissions_scope_exact"] is False
    assert compatibility["reasons"] == []


def test_missing_comparison_contract_fails_only_affected_dimensions() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    del current.collection_context["metadata"]["comparison_contracts"]["capability"]

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is True
    assert compatibility["direct_permissions_interpretable"] is True
    assert compatibility["capability_interpretable"] is False
    assert any("capability comparison contract" in reason for reason in compatibility["reasons"])


def test_unknown_structural_contract_fails_closed() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    current.collection_context["metadata"]["comparison_contracts"]["structural"] = "network_share_inventory_v999"

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert compatibility["direct_permissions_interpretable"] is True
    assert any("structural comparison contract" in reason for reason in compatibility["reasons"])


def test_known_contract_from_wrong_provider_fails_closed() -> None:
    current = _run()
    baseline = _run()
    for run in (current, baseline):
        run.collection_context["metadata"]["comparison_contracts"]["structural"] = "network_share_inventory_v1"

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert any("structural comparison contract" in reason for reason in compatibility["reasons"])


def test_comparison_compatibility_rejects_cross_tenant_structural_conclusions() -> None:
    compatibility = build_comparison_compatibility(_run(tenant_id="tenant-b"), _run())

    assert compatibility["structural_interpretable"] is False
    assert compatibility["status"] != "compatible"
    assert any("tenant_id" in reason for reason in compatibility["reasons"])


def test_comparison_compatibility_marks_incomplete_permissions_explicitly() -> None:
    compatibility = build_comparison_compatibility(
        _run(permissions_complete=False),
        _run(),
    )

    assert compatibility["direct_permissions_assessed"] is True
    assert compatibility["direct_permissions_complete"] is False
    assert compatibility["direct_permissions_interpretable"] is False
    assert any("incomplete direct-permission" in reason for reason in compatibility["reasons"])


def test_matching_access_context_does_not_claim_unassessed_evidence_is_interpretable() -> None:
    compatibility = build_comparison_compatibility(
        _run(permissions_assessed=False, permissions_complete=False),
        _run(permissions_assessed=False, permissions_complete=False),
    )

    assert compatibility["access_context_comparable"] is True
    assert compatibility["access_interpretable"] is False
    assert compatibility["direct_permissions_interpretable"] is False
    assert compatibility["status"] == "partial"


def test_mixed_nfs_run_does_not_claim_complete_access_coverage() -> None:
    compatibility = build_comparison_compatibility(
        _run(provider="nfs+smb"),
        _run(provider="nfs+smb"),
    )

    assert compatibility["direct_permissions_interpretable"] is True
    assert compatibility["access_interpretable"] is False
    assert compatibility["access_provider_coverage_complete"] is False
    assert compatibility["unsupported_access_providers"] == ["nfs"]
    assert compatibility["content_interpretable"] is False
    assert any("not implemented" in reason for reason in compatibility["reasons"])
    assert any("File enumeration was not confirmed" in reason for reason in compatibility["reasons"])


def test_content_scope_changes_are_not_reported_as_item_count_changes() -> None:
    current = _run()
    baseline = _run()
    current.collection_context["metadata"]["collection"]["enumeration"] = {
        "max_pages": 500,
        "include_files": True,
    }
    baseline.collection_context["metadata"]["collection"]["enumeration"] = {
        "max_pages": 1000,
        "include_files": True,
    }

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is False
    assert any("max_pages" in reason for reason in compatibility["reasons"])


def test_smb_share_filters_use_case_insensitive_set_semantics() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    current_enumeration = current.collection_context["metadata"]["collection"]["enumeration"]
    baseline_enumeration = baseline.collection_context["metadata"]["collection"]["enumeration"]
    current_enumeration["include_share"] = [" Finance ", "HR", "finance"]
    baseline_enumeration["include_share"] = ["hr", "FINANCE"]
    current_enumeration["exclude_share"] = ["ADMIN$", "admin$"]
    baseline_enumeration["exclude_share"] = ["Admin$"]

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is True
    assert compatibility["capability_interpretable"] is True


def test_smb_share_filter_change_blocks_appearance_and_disappearance_claims() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    current.collection_context["metadata"]["collection"]["enumeration"]["exclude_share"] = ["Secret"]

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert any("exclude_share" in reason for reason in compatibility["reasons"])


def test_smb_path_filter_change_blocks_content_and_capability_conclusions() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    current.collection_context["metadata"]["collection"]["enumeration"]["exclude_path_regex"] = r"(?i)\\archive(?:\\|$)"

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is False
    assert compatibility["capability_interpretable"] is False
    assert any("exclude_path_regex" in reason for reason in compatibility["reasons"])


def test_smb_extension_filter_is_content_only_and_normalized() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    current_enumeration = current.collection_context["metadata"]["collection"]["enumeration"]
    baseline_enumeration = baseline.collection_context["metadata"]["collection"]["enumeration"]
    current_enumeration["extensions_only"] = "PDF, .docx,pdf"
    baseline_enumeration["extensions_only"] = ".DOCX,.pdf"

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is True
    assert compatibility["capability_interpretable"] is True

    baseline_enumeration["extensions_only"] = ".pdf"
    compatibility = build_comparison_compatibility(current, baseline)
    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is False
    assert compatibility["capability_interpretable"] is True


def test_missing_smb_filter_scope_fails_closed_with_actionable_reason() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    del current.collection_context["metadata"]["collection"]["enumeration"]["include_share"]

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert any(
        "structural enumeration scope is unknown" in reason and "include_share" in reason
        for reason in compatibility["reasons"]
    )


def test_missing_network_target_scope_cannot_support_resource_absence() -> None:
    current = _run(provider="smb")
    baseline = _run(provider="smb")
    current.collection_context["metadata"]["collection"].pop("target_scope")
    baseline.collection_context["metadata"]["collection"].pop("target_scope")

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert any("collection target scope" in reason for reason in compatibility["reasons"])


def test_network_target_scope_uses_normalized_set_semantics() -> None:
    current = _run(provider="nfs+smb")
    baseline = _run(provider="nfs+smb")
    current_scope = current.collection_context["metadata"]["collection"]["target_scope"]
    baseline_scope = baseline.collection_context["metadata"]["collection"]["target_scope"]
    current_scope.update(
        {
            "hosts": ["FILE01.example.test", "192.0.2.1", "file01.EXAMPLE.test"],
            "cidrs": ["198.51.100.7/24"],
            "share_types": ["SMB", "nfs", "smb"],
            "target_count": 256,
        }
    )
    baseline_scope.update(
        {
            "hosts": ["192.0.2.1", "file01.example.test"],
            "cidrs": ["198.51.100.0/24"],
            "share_types": ["nfs", "smb"],
            "target_count": 256,
        }
    )

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True


def test_malformed_nfs_target_scope_fails_closed() -> None:
    current = _run(provider="nfs")
    baseline = _run(provider="nfs")
    current.collection_context["metadata"]["collection"]["target_scope"]["target_count"] = True

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert any("collection target scope" in reason for reason in compatibility["reasons"])


def test_auth_mode_changes_invalidate_structural_and_access_conclusions() -> None:
    compatibility = build_comparison_compatibility(
        _run(auth_mode="local"),
        _run(auth_mode="domain"),
    )

    assert compatibility["structural_interpretable"] is False
    assert compatibility["access_context_comparable"] is False
    assert any("auth_mode" in reason for reason in compatibility["reasons"])


def test_opaque_operator_token_context_cannot_support_exact_comparison() -> None:
    compatibility = build_comparison_compatibility(
        _run(jwt_inspection="opaque_token_context_supplied_by_operator"),
        _run(jwt_inspection="opaque_token_context_supplied_by_operator"),
    )

    assert compatibility["structural_interpretable"] is False
    assert compatibility["access_context_comparable"] is False
    assert compatibility["direct_permissions_interpretable"] is False
    assert any("opaque token" in reason.lower() for reason in compatibility["reasons"])


def test_non_authoritative_sharepoint_discovery_cannot_support_absence_claims() -> None:
    compatibility = build_comparison_compatibility(
        _run(discovery_authoritative=False),
        _run(discovery_authoritative=False),
    )

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert compatibility["status"] != "compatible"
    assert any("does not declare authoritative discovery" in reason for reason in compatibility["reasons"])


def test_unknown_required_sharepoint_context_fails_closed() -> None:
    current = _run()
    baseline = _run()
    current.collection_context.pop("sync_mode")
    baseline.collection_context.pop("sync_mode")

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert compatibility["access_context_comparable"] is True
    assert compatibility["direct_permissions_interpretable"] is True
    assert compatibility["capability_interpretable"] is False
    assert compatibility["status"] != "compatible"
    assert any("sync_mode" in reason for reason in compatibility["reasons"])


def test_complete_matching_targeted_scope_supports_bounded_absence_claims() -> None:
    current = _run(discovery_authoritative=False)
    baseline = _run(discovery_authoritative=False)
    for run in (current, baseline):
        context = run.collection_context
        context["discovery_completeness"] = "targeted_scope"
        context["metadata"]["discovery_strategy"] = "targeted"
        context["metadata"]["collection"]["target_scope"] = {
            "provider": "sharepoint",
            "targeted_sites": ["https://contoso.sharepoint.com/sites/finance"],
            "max_sites": 10_000,
            "max_libraries": 100_000,
            "max_items": 1_000_000,
        }

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is True


def test_malformed_targeted_scope_cannot_support_absence_claims() -> None:
    current = _run(discovery_authoritative=False)
    baseline = _run(discovery_authoritative=False)
    for run in (current, baseline):
        context = run.collection_context
        context["discovery_completeness"] = "targeted_scope"
        context["metadata"]["discovery_strategy"] = "targeted"
        context["metadata"]["collection"]["target_scope"] = {
            "provider": "sharepoint",
            "targeted_sites": ["https://contoso.sharepoint.com/sites/finance", 123],
            "max_sites": 10_000,
            "max_libraries": 100_000,
            "max_items": 1_000_000,
        }

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert compatibility["status"] != "compatible"


def test_missing_full_sharepoint_target_scope_fails_closed() -> None:
    current = _run()
    baseline = _run()
    current.collection_context["metadata"]["collection"].pop("target_scope")
    baseline.collection_context["metadata"]["collection"].pop("target_scope")

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert any("collection target scope" in reason for reason in compatibility["reasons"])


def test_sharepoint_file_declaration_contradiction_blocks_only_content() -> None:
    current = _run()
    baseline = _run()
    current.collection_context["metadata"]["collection"]["enumeration"]["include_files"] = False

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is False
    assert any("file-enumeration declarations" in reason for reason in compatibility["reasons"])


def test_sharepoint_targeted_sites_must_match_discovery_mode() -> None:
    current = _run()
    baseline = _run()
    for run in (current, baseline):
        run.collection_context["metadata"]["collection"]["target_scope"]["targeted_sites"] = [
            "https://contoso.sharepoint.com/sites/finance"
        ]

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert any("contradicts the declared discovery mode" in reason for reason in compatibility["reasons"])


def test_missing_max_items_blocks_content_but_not_structure() -> None:
    current = _run()
    baseline = _run()
    current.collection_context["metadata"]["collection"]["target_scope"].pop("max_items")

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is False
    assert any("target_scope.max_items" in reason for reason in compatibility["reasons"])


def test_known_contract_requires_explicit_dimension_completeness() -> None:
    current = _run()
    baseline = _run()
    current.collection_context["metadata"].pop("structural_complete")
    current.collection_context["metadata"].pop("content_complete")
    current.collection_context["partial"] = False

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert any("incomplete structural" in reason for reason in compatibility["reasons"])


def test_materialized_full_and_delta_snapshots_remain_structurally_comparable() -> None:
    current = _run()
    baseline = _run()
    current.collection_context["sync_mode"] = "delta"
    baseline.collection_context["sync_mode"] = "full"

    compatibility = build_comparison_compatibility(current, baseline)

    assert compatibility["structural_interpretable"] is True
    assert compatibility["content_interpretable"] is True


def test_partial_context_without_dimension_flags_fails_closed() -> None:
    current = _run(partial=True)
    current.collection_context["metadata"].pop("structural_complete")
    current.collection_context["metadata"].pop("content_complete")

    compatibility = build_comparison_compatibility(current, _run())

    assert compatibility["structural_interpretable"] is False
    assert compatibility["content_interpretable"] is False
    assert compatibility["status"] != "compatible"
    assert any("incomplete structural" in reason for reason in compatibility["reasons"])
