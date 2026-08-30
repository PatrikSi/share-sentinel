import hashlib
import ipaddress
import logging
import unicodedata
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.comparison_contract import (
    COMPARISON_ALGORITHM_VERSION,
    LEGACY_COMPARISON_ALGORITHM_WARNING,
    comparison_algorithm_is_current,
)
from app.config import get_settings
from app.db import escape_like, get_db
from app.deps import AuthContext, get_auth_context, request_meta, require_project_role, require_token_scopes
from app.enums import ProjectRole, RunStatus
from app.locking import lock_monitoring_source, lock_worker_job
from app.models import CollectionSource, ComparisonItemChange, ComparisonResourceChange, RunComparison, ScanRun
from app.pagination import (
    KeysetColumn,
    apply_keyset_pagination,
    paginate_rows,
    parse_datetime_cursor_value,
    parse_int_cursor_value,
    parse_uuid_cursor_value,
)
from app.rate_limit import RateLimiter
from app.routers.runs import _run_diff_compatibility
from app.schemas import ComparisonCreateIn, ComparisonItemChangeOut, ComparisonOut, ComparisonResourceChangeOut
from app.services.audit import write_audit_event
from app.services.monitoring import AutomaticSourceDisabledError, publish_automatic_baseline_recovery
from app.services.queue import enqueue_worker_job
from app.token_scopes import SCOPE_READ_INVENTORY, SCOPE_READ_RUNS, SCOPE_WRITE_RUNS

router = APIRouter(prefix="/projects/{project_id}/comparisons", tags=["comparisons"])
logger = logging.getLogger("share_sentinel.comparisons")
rate_limiter = RateLimiter()
ALGORITHM_VERSION = COMPARISON_ALGORITHM_VERSION
DEFAULT_OPTIONS_HASH = hashlib.sha256(b"{}").hexdigest()
LEGACY_ALGORITHM_WARNING = LEGACY_COMPARISON_ALGORITHM_WARNING
CHANGE_CURSOR = (
    KeysetColumn("impact_rank", ComparisonResourceChange.impact_rank, direction="desc", parser=parse_int_cursor_value),
    KeysetColumn("id", ComparisonResourceChange.id, direction="desc", parser=parse_int_cursor_value),
)
COMPARISON_CURSOR = (
    KeysetColumn("created_at", RunComparison.created_at, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", RunComparison.id, direction="desc", parser=parse_uuid_cursor_value),
)
ITEM_CHANGE_CURSOR = (
    KeysetColumn("impact_rank", ComparisonItemChange.impact_rank, direction="desc", parser=parse_int_cursor_value),
    KeysetColumn("id", ComparisonItemChange.id, direction="desc", parser=parse_int_cursor_value),
)
CHANGE_TYPES = {"appeared", "disappeared", "changed", "indeterminate"}
ITEM_CHANGE_TYPES = {
    "added",
    "removed",
    "moved",
    "renamed",
    "metadata_changed",
    "permission_changed",
    "indeterminate",
}
COMPARISON_STATES = {"queued", "running", "complete", "failed"}
KNOWN_COMPARISON_CONTRACTS = {
    "structural": frozenset({"network_share_inventory_v1", "sharepoint_resource_inventory_v1"}),
    "content": frozenset({"smb_tree_inventory_v1", "sharepoint_drive_inventory_v1"}),
    "capability": frozenset({"smb_nonmutating_capability_v1"}),
}


def _get_complete_run(db: Session, project_id: uuid.UUID, run_id: uuid.UUID, label: str) -> ScanRun:
    run = db.execute(select(ScanRun).where(ScanRun.id == run_id, ScanRun.project_id == project_id)).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"{label} run was not found in this project"},
        )
    if run.status != RunStatus.COMPLETE:
        state = run.status.value if hasattr(run.status, "value") else str(run.status)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RUN_NOT_COMPLETE",
                "message": f"{label} run must be COMPLETE before comparison; current state is {state}",
            },
        )
    return run


def _context_value(context: dict[str, Any], key: str) -> Any:
    value = context.get(key)
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, list):
        return sorted(str(item).strip().casefold() for item in value)
    return value


def _context_providers(context: dict[str, Any]) -> set[str]:
    """Return the declared provider set for coverage-level compatibility.

    The network collector uses a stable ``nfs+smb`` value for mixed runs.  Do
    not let complete SMB ACL evidence silently imply that the NFS resources in
    the same run also had an access-evidence plane.
    """

    raw_value = context.get("provider") or context.get("source")
    raw_values = raw_value if isinstance(raw_value, list) else [raw_value]
    providers: set[str] = set()
    for raw_provider in raw_values:
        if not isinstance(raw_provider, str):
            continue
        providers.update(part.strip().casefold() for part in raw_provider.replace(",", "+").split("+") if part.strip())
    return providers


def _expected_comparison_contract(providers: set[str], dimension: str) -> str | None:
    if providers == {"sharepoint"}:
        return {
            "structural": "sharepoint_resource_inventory_v1",
            "content": "sharepoint_drive_inventory_v1",
        }.get(dimension)
    if providers and providers.issubset({"smb", "nfs"}):
        if dimension == "structural":
            return "network_share_inventory_v1"
        if dimension == "content" and "smb" in providers:
            return "smb_tree_inventory_v1"
        if dimension == "capability" and providers == {"smb"}:
            return "smb_nonmutating_capability_v1"
    return None


def _comparison_contract_value(metadata: dict[str, Any], dimension: str, providers: set[str]) -> str | None:
    contracts = metadata.get("comparison_contracts")
    if not isinstance(contracts, dict):
        return None
    value = contracts.get(dimension)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    expected = _expected_comparison_contract(providers, dimension)
    return normalized if normalized == expected else None


def _enumeration_scope_value(enumeration: dict[str, Any], field: str) -> tuple[bool, Any]:
    """Return a validated, semantics-aware collection-scope value.

    Collector arguments are persisted as entered.  Normalize only where the
    collector itself treats values as sets; regexes remain byte-for-byte
    significant because changing a pattern can change the observed tree.
    The boolean result distinguishes a known empty/default value from missing
    or malformed metadata so older/third-party artifacts fail closed.
    """

    if field not in enumeration:
        return False, None
    value = enumeration[field]
    if field == "include_share":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return False, None
        return True, tuple(sorted({item.strip().casefold() for item in value if item.strip()}))
    if field == "exclude_share":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return False, None
        # Exclusion matching in the collector is case-insensitive, but unlike
        # explicit includes it does not strip meaningful whitespace.
        return True, tuple(sorted({item.upper() for item in value if item}))
    if field == "extensions_only":
        if value is None:
            return True, ()
        if not isinstance(value, str):
            return False, None
        normalized = {
            extension if extension.startswith(".") else f".{extension}"
            for part in value.split(",")
            if (extension := part.strip().lower())
        }
        return True, tuple(sorted(normalized))
    if field == "exclude_path_regex":
        return (True, value) if value is None or isinstance(value, str) else (False, None)
    if field == "include_files":
        return (True, value) if isinstance(value, bool) else (False, None)
    if field in {"max_depth", "max_entries_per_share", "max_pages"}:
        return (True, value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else (False, None)
    if field == "access_probe_limit":
        return (True, value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else (False, None)
    return True, value


def _scope_field_differences(
    current: dict[str, Any],
    baseline: dict[str, Any],
    fields: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    mismatched: list[str] = []
    unknown: list[str] = []
    for field in fields:
        current_known, current_value = _enumeration_scope_value(current, field)
        baseline_known, baseline_value = _enumeration_scope_value(baseline, field)
        if not current_known or not baseline_known:
            unknown.append(field)
        elif current_value != baseline_value:
            mismatched.append(field)
    return mismatched, unknown


def _network_target_scope_value(collection: dict[str, Any], declared_providers: set[str]) -> tuple[bool, Any]:
    """Validate and normalize the declared network target plane.

    Resource absence is meaningful only when both runs identify the same host
    set and requested providers.  Keep list ordering and duplicate CLI inputs
    out of the comparison while rejecting incomplete or malformed producer
    metadata instead of silently treating two unknown scopes as equal.
    """

    scope = collection.get("target_scope")
    if not isinstance(scope, dict):
        return False, None
    list_fields = ("hosts", "cidrs", "share_types", "disabled_share_types")
    values: dict[str, list[str]] = {}
    for field in list_fields:
        raw_value = scope.get(field)
        if not isinstance(raw_value, list) or any(not isinstance(item, str) or not item.strip() for item in raw_value):
            return False, None
        values[field] = raw_value
    target_count = scope.get("target_count")
    if not isinstance(target_count, int) or isinstance(target_count, bool) or target_count <= 0:
        return False, None
    if not values["hosts"] and not values["cidrs"]:
        return False, None

    normalized_hosts: set[str] = set()
    for raw_host in values["hosts"]:
        host = raw_host.strip()
        try:
            normalized_hosts.add(str(ipaddress.ip_address(host)))
        except ValueError:
            normalized_hosts.add(host.casefold())
    normalized_cidrs: set[str] = set()
    for raw_cidr in values["cidrs"]:
        try:
            normalized_cidrs.add(str(ipaddress.ip_network(raw_cidr.strip(), strict=False)))
        except ValueError:
            return False, None
    share_types = {share_type.strip().casefold() for share_type in values["share_types"]}
    disabled_share_types = {share_type.strip().casefold() for share_type in values["disabled_share_types"]}
    required_network_providers = declared_providers & {"smb", "nfs"}
    if (
        not required_network_providers
        or required_network_providers != share_types
        or not disabled_share_types.issubset(share_types)
    ):
        return False, None
    return True, (
        tuple(sorted(normalized_hosts)),
        tuple(sorted(normalized_cidrs)),
        tuple(sorted(share_types)),
        tuple(sorted(disabled_share_types)),
        target_count,
    )


def _sharepoint_target_scope_value(collection: dict[str, Any]) -> tuple[bool, Any]:
    scope = collection.get("target_scope")
    if not isinstance(scope, dict):
        return False, None
    if str(scope.get("provider") or "").strip().casefold() != "sharepoint":
        return False, None
    raw_sites = scope.get("targeted_sites")
    if not isinstance(raw_sites, list) or any(not isinstance(site, str) or not site.strip() for site in raw_sites):
        return False, None
    for field in ("max_sites", "max_libraries"):
        value = scope.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False, None
    normalized_sites: set[str] = set()
    for raw_site in raw_sites:
        site = unicodedata.normalize("NFC", raw_site).strip()
        if site != "/":
            site = site.rstrip("/")
        normalized_sites.add(site.casefold())
    return True, tuple(sorted(normalized_sites))


def _sharepoint_content_scope_known(collection: dict[str, Any]) -> bool:
    scope = collection.get("target_scope")
    if not isinstance(scope, dict):
        return False
    max_items = scope.get("max_items")
    return isinstance(max_items, int) and not isinstance(max_items, bool) and max_items >= 0


def build_comparison_compatibility(current: ScanRun, baseline: ScanRun) -> dict[str, Any]:
    """Return explicit per-dimension interpretability, never a single lossy flag."""

    legacy = _run_diff_compatibility(current, baseline)
    current_context = current.collection_context or {}
    baseline_context = baseline.collection_context or {}
    reasons: list[str] = []
    legacy_mismatched_fields = [str(field) for field in (legacy.get("mismatched_fields") or [])]
    legacy_unknown_fields = [field for field in legacy_mismatched_fields if field.startswith("unknown:")]
    legacy_structural_unknown_fields = [
        field
        for field in legacy_unknown_fields
        if not field.endswith(".metadata.files_included") and not field.endswith(".permissions")
    ]
    access_context_suffixes = (
        ".source",
        ".provider",
        ".auth_type",
        ".auth_mode",
        ".tenant_id",
        ".client_id",
        ".assessed_identity",
        ".scopes",
        ".roles",
        ".permissions",
    )
    legacy_access_unknown_fields = [field for field in legacy_unknown_fields if field.endswith(access_context_suffixes)]
    declared_providers = _context_providers(current_context) | _context_providers(baseline_context)
    sharepoint_selected = "sharepoint" in declared_providers
    smb_selected = "smb" in declared_providers
    network_selected = bool(declared_providers & {"smb", "nfs"})

    if not current_context or not baseline_context:
        reasons.append("Collection context is missing for at least one run.")

    # Inventory visibility is security-trimmed for both Graph and SMB. A
    # different tenant, application, or assessed identity is therefore a
    # different structural observation plane, not evidence of appearance or
    # disappearance.
    structural_fields = (
        "source",
        "provider",
        "collection_mode",
        "discovery_completeness",
        "auth_type",
        "auth_mode",
        "tenant_id",
        "client_id",
        "assessed_identity",
        "scopes",
        "roles",
    )
    structural_mismatches = [
        field
        for field in structural_fields
        if _context_value(current_context, field) != _context_value(baseline_context, field)
    ]
    current_meta = current_context.get("metadata") if isinstance(current_context.get("metadata"), dict) else {}
    baseline_meta = baseline_context.get("metadata") if isinstance(baseline_context.get("metadata"), dict) else {}
    current_providers = _context_providers(current_context)
    baseline_providers = _context_providers(baseline_context)
    current_contracts = {
        dimension: _comparison_contract_value(current_meta, dimension, current_providers)
        for dimension in KNOWN_COMPARISON_CONTRACTS
    }
    baseline_contracts = {
        dimension: _comparison_contract_value(baseline_meta, dimension, baseline_providers)
        for dimension in KNOWN_COMPARISON_CONTRACTS
    }
    structural_contract_known = all(
        contracts["structural"] is not None for contracts in (current_contracts, baseline_contracts)
    )
    structural_contract_matches = (
        structural_contract_known and current_contracts["structural"] == baseline_contracts["structural"]
    )
    for field in ("discovery_strategy", "discovery_authoritative"):
        if _context_value(current_meta, field) != _context_value(baseline_meta, field):
            structural_mismatches.append(f"metadata.{field}")
    current_collection = current_meta.get("collection") if isinstance(current_meta.get("collection"), dict) else {}
    baseline_collection = baseline_meta.get("collection") if isinstance(baseline_meta.get("collection"), dict) else {}
    target_scope_mismatch = False
    target_scope_unknown = False
    sharepoint_scope_mode_consistent = True
    if sharepoint_selected:
        current_target_known, current_target_scope = _sharepoint_target_scope_value(current_collection)
        baseline_target_known, baseline_target_scope = _sharepoint_target_scope_value(baseline_collection)
        target_scope_unknown = not current_target_known or not baseline_target_known
        target_scope_mismatch = (
            current_target_known and baseline_target_known and current_target_scope != baseline_target_scope
        )
        if current_target_known and baseline_target_known:
            for context, meta, targeted_sites in (
                (current_context, current_meta, current_target_scope),
                (baseline_context, baseline_meta, baseline_target_scope),
            ):
                completeness_targeted = (
                    str(context.get("discovery_completeness") or "").strip().casefold() == "targeted_scope"
                )
                strategy_targeted = str(meta.get("discovery_strategy") or "").strip().casefold() == "targeted"
                if completeness_targeted or strategy_targeted:
                    sharepoint_scope_mode_consistent = (
                        sharepoint_scope_mode_consistent
                        and completeness_targeted
                        and strategy_targeted
                        and bool(targeted_sites)
                    )
                elif targeted_sites:
                    sharepoint_scope_mode_consistent = False
    if network_selected:
        current_target_known, current_target_scope = _network_target_scope_value(
            current_collection, _context_providers(current_context)
        )
        baseline_target_known, baseline_target_scope = _network_target_scope_value(
            baseline_collection, _context_providers(baseline_context)
        )
        target_scope_unknown = target_scope_unknown or not current_target_known or not baseline_target_known
        target_scope_mismatch = target_scope_mismatch or (
            current_target_known and baseline_target_known and current_target_scope != baseline_target_scope
        )
    elif not sharepoint_selected:
        target_scope_mismatch = current_collection.get("target_scope") != baseline_collection.get("target_scope")
    if target_scope_mismatch:
        structural_mismatches.append("target_scope")
    current_enumeration = (
        current_collection.get("enumeration") if isinstance(current_collection.get("enumeration"), dict) else {}
    )
    baseline_enumeration = (
        baseline_collection.get("enumeration") if isinstance(baseline_collection.get("enumeration"), dict) else {}
    )
    structural_scope_mismatches: list[str] = []
    structural_scope_unknown: list[str] = []
    content_scope_fields: list[str] = []
    if smb_selected:
        structural_scope_mismatches, structural_scope_unknown = _scope_field_differences(
            current_enumeration,
            baseline_enumeration,
            ("include_share", "exclude_share"),
        )
        content_scope_fields.extend(("max_depth", "max_entries_per_share", "exclude_path_regex", "extensions_only"))
    if sharepoint_selected:
        content_scope_fields.extend(("max_pages", "include_files"))
    content_scope_mismatches, content_scope_unknown = _scope_field_differences(
        current_enumeration,
        baseline_enumeration,
        tuple(content_scope_fields),
    )
    if sharepoint_selected and not all(
        _sharepoint_content_scope_known(collection) for collection in (current_collection, baseline_collection)
    ):
        content_scope_unknown.append("target_scope.max_items")
    structural_mismatches.extend(f"enumeration.{field}" for field in structural_scope_mismatches)

    authoritative_values = {
        "authoritative",
        "complete",
        "complete_for_declared_scope",
        "complete_for_granted_scope",
        "full",
        "targeted_scope",
    }
    discovery_known = all(
        str(context.get("discovery_completeness") or "").strip().casefold() in authoritative_values
        for context in (current_context, baseline_context)
    )
    targeted_scope_authoritative = sharepoint_selected and all(
        str(context.get("discovery_completeness") or "").strip().casefold() == "targeted_scope"
        and str(meta.get("discovery_strategy") or "").strip().casefold() == "targeted"
        and isinstance(collection.get("target_scope"), dict)
        and str(collection["target_scope"].get("provider") or "").strip().casefold() == "sharepoint"
        and isinstance(collection["target_scope"].get("targeted_sites"), list)
        and bool(collection["target_scope"]["targeted_sites"])
        and all(isinstance(site, str) and bool(site.strip()) for site in collection["target_scope"]["targeted_sites"])
        for context, meta, collection in (
            (current_context, current_meta, current_collection),
            (baseline_context, baseline_meta, baseline_collection),
        )
    )
    discovery_authoritative = (
        not sharepoint_selected
        or targeted_scope_authoritative
        or all(meta.get("discovery_authoritative") is True for meta in (current_meta, baseline_meta))
    )
    materialized = all(context.get("materialized_snapshot") is True for context in (current_context, baseline_context))
    structural_complete = all(meta.get("structural_complete") is True for meta in (current_meta, baseline_meta))
    opaque_access_context = any(
        str(context.get("jwt_inspection") or "").strip().casefold() == "opaque_token_context_supplied_by_operator"
        for context in (current_context, baseline_context)
    )
    structural_interpretable = bool(
        current_context
        and baseline_context
        and not structural_mismatches
        and discovery_known
        and materialized
        and structural_complete
        and discovery_authoritative
        and not legacy_structural_unknown_fields
        and not structural_scope_unknown
        and not target_scope_unknown
        and sharepoint_scope_mode_consistent
        and structural_contract_matches
        and not opaque_access_context
    )
    if structural_mismatches:
        reasons.append("Structural collection perspectives differ: " + ", ".join(structural_mismatches) + ".")
    if not discovery_known:
        reasons.append("At least one run does not declare authoritative discovery coverage.")
    if not discovery_authoritative:
        reasons.append(
            "At least one SharePoint run does not declare authoritative discovery for its granted scope or an explicit complete targeted scope."
        )
    if legacy_structural_unknown_fields:
        reasons.append(
            "Required structural comparison context is unknown: "
            + ", ".join(field.removeprefix("unknown:") for field in legacy_structural_unknown_fields)
            + "."
        )
    if structural_scope_unknown:
        reasons.append("Required structural enumeration scope is unknown: " + ", ".join(structural_scope_unknown) + ".")
    if not materialized:
        reasons.append("At least one run is not a materialized point-in-time snapshot.")
    if not structural_complete:
        reasons.append("At least one run reports incomplete structural discovery coverage.")
    if opaque_access_context:
        reasons.append(
            "At least one run used opaque token context supplied by an operator; its identity and Graph grants cannot be verified for structural or access conclusions."
        )
    if target_scope_unknown:
        reasons.append(
            "Required collection target scope is missing or malformed; declared resource coverage cannot be compared."
        )
    if not sharepoint_scope_mode_consistent:
        reasons.append("SharePoint targeted-site scope contradicts the declared discovery mode.")
    if not structural_contract_known:
        reasons.append("The structural comparison contract is missing or unsupported for at least one run.")
    elif not structural_contract_matches:
        reasons.append("Structural comparison contracts differ between the two runs.")

    files_included = all(meta.get("files_included") is True for meta in (current_meta, baseline_meta))
    files_declaration_consistent = True
    if sharepoint_selected:
        for meta, enumeration in (
            (current_meta, current_enumeration),
            (baseline_meta, baseline_enumeration),
        ):
            include_files_known, include_files = _enumeration_scope_value(enumeration, "include_files")
            if (
                not isinstance(meta.get("files_included"), bool)
                or not include_files_known
                or meta.get("files_included") != include_files
            ):
                files_declaration_consistent = False
    content_complete = all(meta.get("content_complete") is True for meta in (current_meta, baseline_meta))
    content_contract_known = all(
        contracts["content"] is not None for contracts in (current_contracts, baseline_contracts)
    )
    content_contract_matches = content_contract_known and current_contracts["content"] == baseline_contracts["content"]
    content_interpretable = (
        structural_interpretable
        and files_included
        and content_complete
        and not content_scope_mismatches
        and not content_scope_unknown
        and content_contract_matches
        and files_declaration_consistent
    )
    if not files_included:
        reasons.append("File enumeration was not confirmed for both runs; item changes are not computed.")
    elif not content_complete:
        reasons.append("At least one run reports incomplete content enumeration coverage.")
    if not files_declaration_consistent:
        reasons.append("SharePoint file-enumeration declarations are missing or contradictory.")
    if content_scope_mismatches:
        reasons.append("Content enumeration scopes differ: " + ", ".join(content_scope_mismatches) + ".")
    if content_scope_unknown:
        reasons.append("Required content enumeration scope is unknown: " + ", ".join(content_scope_unknown) + ".")
    if not content_contract_known:
        reasons.append("The content comparison contract is missing or unsupported for at least one run.")
    elif not content_contract_matches:
        reasons.append("Content comparison contracts differ between the two runs.")

    access_fields = (
        "provider",
        "auth_type",
        "auth_mode",
        "tenant_id",
        "client_id",
        "assessed_identity",
        "scopes",
        "roles",
    )
    access_mismatches = [
        field
        for field in access_fields
        if _context_value(current_context, field) != _context_value(baseline_context, field)
    ]
    permissions_assessed = all(meta.get("permissions_assessed") is True for meta in (current_meta, baseline_meta))
    permissions_complete = permissions_assessed and all(
        meta.get("permissions_complete") is True for meta in (current_meta, baseline_meta)
    )
    access_context_known = (
        all(
            bool(str(context.get("provider") or context.get("source") or "").strip())
            and (
                bool(str(context.get("assessed_identity") or "").strip())
                or (
                    bool(str(context.get("tenant_id") or "").strip())
                    and bool(str(context.get("client_id") or "").strip())
                )
            )
            for context in (current_context, baseline_context)
        )
        and not opaque_access_context
        and not legacy_access_unknown_fields
    )
    access_context_comparable = not access_mismatches and access_context_known
    capability_scope_fields = (
        ("access_probe_limit", "max_depth", "max_entries_per_share", "exclude_path_regex") if smb_selected else ()
    )
    capability_scope_mismatches, capability_scope_unknown = _scope_field_differences(
        current_enumeration,
        baseline_enumeration,
        capability_scope_fields,
    )
    declared_access_providers = declared_providers
    unsupported_access_providers = sorted(declared_access_providers - {"smb", "sharepoint"})
    access_provider_coverage_complete = bool(declared_access_providers) and not (unsupported_access_providers)
    capability_applicable = "smb" in declared_access_providers
    capability_provider_coverage_complete = capability_applicable and declared_access_providers == {"smb"}
    capability_contract_known = all(
        contracts["capability"] is not None for contracts in (current_contracts, baseline_contracts)
    )
    capability_contract_matches = (
        capability_contract_known and current_contracts["capability"] == baseline_contracts["capability"]
    )
    # Matching credentials/scope make observations comparable, but do not by
    # themselves prove that an access evidence plane was actually assessed.
    access_interpretable = access_context_comparable and permissions_assessed and access_provider_coverage_complete
    capability_interpretable = (
        access_context_comparable
        and not capability_scope_mismatches
        and not capability_scope_unknown
        and capability_provider_coverage_complete
        and capability_contract_matches
    )
    if access_mismatches:
        reasons.append("Access assessment perspectives differ: " + ", ".join(access_mismatches) + ".")
    if not access_context_known:
        reasons.append("The assessed identity or application context is not known for both runs.")
    if not permissions_assessed:
        reasons.append(
            "Direct permission assessment was not confirmed for both runs; access comparison is limited to capability observations."
        )
    elif not permissions_complete:
        reasons.append(
            "At least one run reports incomplete direct-permission coverage; affected resources are marked indeterminate."
        )
    if unsupported_access_providers:
        reasons.append(
            "Access evidence is not implemented for every declared provider: "
            + ", ".join(unsupported_access_providers)
            + ". Supported-provider evidence remains comparable, but the overall access dimension is not exact."
        )
    if capability_applicable and not capability_provider_coverage_complete:
        reasons.append("Capability comparison is currently supported only for SMB-only runs.")
    if capability_applicable and capability_provider_coverage_complete and not capability_contract_known:
        reasons.append("The capability comparison contract is missing or unsupported for at least one run.")
    elif capability_applicable and capability_provider_coverage_complete and not capability_contract_matches:
        reasons.append("Capability comparison contracts differ between the two runs.")
    if capability_applicable and not capability_interpretable:
        if capability_scope_mismatches:
            reasons.append("Capability assessment scopes differ: " + ", ".join(capability_scope_mismatches) + ".")
        elif capability_scope_unknown:
            reasons.append(
                "Required capability assessment scope is unknown: " + ", ".join(capability_scope_unknown) + "."
            )
        elif capability_provider_coverage_complete and capability_contract_matches:
            reasons.append(
                "Capability observations are not comparable because the access perspective differs or is unknown."
            )

    legacy_warning = legacy.get("warning")
    if legacy_warning and legacy_warning not in reasons:
        reasons.append(str(legacy_warning))
    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    dimensions = (
        structural_interpretable,
        content_interpretable,
        access_interpretable,
        not capability_applicable or capability_interpretable,
    )
    compatibility_status = (
        "compatible" if all(dimensions) and permissions_complete else ("partial" if any(dimensions) else "incompatible")
    )
    return {
        "status": compatibility_status,
        "structural_interpretable": structural_interpretable,
        "content_interpretable": content_interpretable,
        "access_context_comparable": access_context_comparable,
        "access_provider_coverage_complete": access_provider_coverage_complete,
        "capability_applicable": capability_applicable,
        "capability_provider_coverage_complete": capability_provider_coverage_complete,
        "smb_identity_required": smb_selected,
        # The worker replaces this preliminary value after checking normalized
        # endpoint evidence in both runs. Non-SMB comparisons do not need a
        # physical-server identity plane.
        "identity_applicable": smb_selected,
        "identity_scope_exact": not smb_selected,
        "unsupported_access_providers": unsupported_access_providers,
        "access_interpretable": access_interpretable,
        "capability_interpretable": capability_interpretable,
        "direct_permissions_assessed": permissions_assessed,
        "direct_permissions_complete": permissions_complete,
        "direct_permissions_interpretable": access_context_comparable and permissions_complete,
        # A worker derives the stronger scope-exact flag from normalized,
        # integrity-checked assessments before publishing a completed result.
        "direct_permissions_scope_exact": False,
        "reasons": reasons,
    }


def _run_label(run: ScanRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "name": run.name,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
    }


def _comparison_out(
    comparison: RunComparison,
    baseline: ScanRun | None = None,
    current: ScanRun | None = None,
) -> ComparisonOut:
    error = None
    if comparison.error_code or comparison.error_message:
        error = {
            "code": comparison.error_code or "COMPARISON_FAILED",
            "message": comparison.error_message or "Comparison failed without an error message.",
        }
    return ComparisonOut(
        id=comparison.id,
        project_id=comparison.project_id,
        source_id=getattr(comparison, "source_id", None),
        baseline_run_id=comparison.baseline_run_id,
        current_run_id=comparison.current_run_id,
        baseline_run=_run_label(baseline),
        current_run=_run_label(current),
        algorithm_version=comparison.algorithm_version,
        algorithm_current=comparison_algorithm_is_current(comparison.algorithm_version),
        algorithm_warning=(
            None if comparison_algorithm_is_current(comparison.algorithm_version) else LEGACY_ALGORITHM_WARNING
        ),
        trigger=getattr(comparison, "trigger", "manual"),
        state=comparison.state,
        compatibility=comparison.compatibility or {},
        progress=comparison.progress or {},
        summary=comparison.summary or {},
        error=error,
        attempt_count=comparison.attempt_count,
        created_at=comparison.created_at,
        started_at=comparison.started_at,
        completed_at=comparison.completed_at,
        heartbeat_at=comparison.heartbeat_at,
        next_retry_at=comparison.next_retry_at,
    )


def _require_current_comparison_algorithm(comparison: RunComparison) -> None:
    if comparison_algorithm_is_current(comparison.algorithm_version):
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "COMPARISON_ALGORITHM_OBSOLETE",
            "message": (
                f"Comparison algorithm {comparison.algorithm_version} cannot be retried in place. "
                "Create the same baseline/current comparison again to use the current algorithm."
            ),
        },
    )


def _get_comparison(db: Session, project_id: uuid.UUID, comparison_id: uuid.UUID) -> RunComparison:
    comparison = db.execute(
        select(RunComparison).where(
            RunComparison.id == comparison_id,
            RunComparison.project_id == project_id,
        )
    ).scalar_one_or_none()
    if comparison is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="comparison not found")
    return comparison


@router.post("", response_model=ComparisonOut, status_code=status.HTTP_202_ACCEPTED)
def create_comparison(
    project_id: uuid.UUID,
    payload: ComparisonCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_RUNS, SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    if payload.baseline_run_id == payload.current_run_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "COMPARISON_RUNS_MUST_DIFFER",
                "message": "Baseline and current runs must be different.",
            },
        )
    baseline = _get_complete_run(db, project_id, payload.baseline_run_id, "baseline")
    current = _get_complete_run(db, project_id, payload.current_run_id, "current")

    identity_filter = (
        RunComparison.project_id == project_id,
        RunComparison.baseline_run_id == baseline.id,
        RunComparison.current_run_id == current.id,
        RunComparison.algorithm_version == ALGORITHM_VERSION,
        RunComparison.options_hash == DEFAULT_OPTIONS_HASH,
    )
    existing = db.execute(select(RunComparison).where(*identity_filter)).scalar_one_or_none()
    if existing is not None:
        if existing.state == "failed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "COMPARISON_RETRY_REQUIRED",
                    "message": "This comparison failed. Use the dedicated retry action to reset it safely.",
                },
            )
        return _comparison_out(existing, baseline, current)

    settings = get_settings()
    rate_limiter.check(
        request,
        "comparison_create",
        limit=settings.api_comparison_rate_limit,
        window_seconds=settings.api_comparison_rate_window_seconds,
        actor_key=f"{auth.user_id or auth.token_id}:{project_id}",
    )

    # Serialize admission within a project so concurrent requests cannot all
    # observe the same free slot and exceed the active-comparison budget.
    comparison_lock_key = project_id.int % (2**63 - 1)
    db.execute(select(func.pg_advisory_xact_lock(comparison_lock_key)))
    existing = db.execute(select(RunComparison).where(*identity_filter)).scalar_one_or_none()
    if existing is not None:
        if existing.state == "failed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "COMPARISON_RETRY_REQUIRED",
                    "message": "This comparison failed. Use the dedicated retry action to reset it safely.",
                },
            )
        return _comparison_out(existing, baseline, current)

    active_count = int(
        db.execute(
            select(func.count(RunComparison.id)).where(
                RunComparison.project_id == project_id,
                RunComparison.state.in_(("queued", "running")),
            )
        ).scalar()
        or 0
    )
    if active_count >= settings.api_comparison_max_active_per_project:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "COMPARISON_CAPACITY_REACHED",
                "message": "This project already has the maximum number of active comparisons. Retry shortly.",
            },
            headers={"Retry-After": "5"},
        )

    initial_summary = {
        "appeared": 0,
        "disappeared": 0,
        "changed": 0,
        "indeterminate": 0,
        "total": 0,
        "exact": False,
        "resource_summary_exact": False,
        "item_churn_computed": False,
    }
    compatibility = build_comparison_compatibility(current, baseline)
    comparison = RunComparison(
        id=uuid.uuid4(),
        project_id=project_id,
        source_id=current.source_id if current.source_id == baseline.source_id else None,
        baseline_run_id=baseline.id,
        current_run_id=current.id,
        algorithm_version=ALGORITHM_VERSION,
        options_hash=DEFAULT_OPTIONS_HASH,
        trigger="manual",
        state="queued",
        compatibility=compatibility,
        progress={"phase": "queued", "processed": 0},
        summary=initial_summary,
        created_by_user_id=auth.user_id,
        created_by_token_id=auth.token_id,
    )
    db.add(comparison)
    audit_action = "COMPARISON_CREATED"
    write_audit_event(
        db,
        action=audit_action,
        object_type="run_comparison",
        object_id=str(comparison.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "baseline_run_id": str(baseline.id),
            "current_run_id": str(current.id),
            "algorithm_version": ALGORITHM_VERSION,
        },
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.execute(select(RunComparison).where(*identity_filter)).scalar_one_or_none()
        if existing is None:
            raise
        return _comparison_out(existing, baseline, current)
    db.refresh(comparison)

    try:
        enqueue_worker_job(
            {
                "job_type": "comparison",
                "comparison_id": comparison.id,
                "project_id": project_id,
            }
        )
    except Exception:  # Database recovery scanning is the durable fallback.
        logger.warning("comparison enqueue failed; worker recovery will claim it comparison_id=%s", comparison.id)
    return _comparison_out(comparison, baseline, current)


@router.get("", response_model=dict)
def list_comparisons(
    project_id: uuid.UUID,
    comparison_state: str | None = Query(default=None, alias="state", max_length=24),
    source_id: uuid.UUID | None = Query(default=None),
    current_run_id: uuid.UUID | None = Query(default=None),
    baseline_run_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS, SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    normalized_state = comparison_state.strip().lower() if comparison_state else None
    if normalized_state and normalized_state not in COMPARISON_STATES:
        raise HTTPException(status_code=400, detail="unsupported comparison state")
    stmt = select(RunComparison).where(RunComparison.project_id == project_id)
    if normalized_state:
        stmt = stmt.where(RunComparison.state == normalized_state)
    if source_id:
        stmt = stmt.where(RunComparison.source_id == source_id)
    if current_run_id:
        stmt = stmt.where(RunComparison.current_run_id == current_run_id)
    if baseline_run_id:
        stmt = stmt.where(RunComparison.baseline_run_id == baseline_run_id)
    stmt = apply_keyset_pagination(stmt, COMPARISON_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), COMPARISON_CURSOR, limit)
    run_ids = {row.baseline_run_id for row in rows} | {row.current_run_id for row in rows}
    runs = {
        run.id: run
        for run in db.execute(select(ScanRun).where(ScanRun.id.in_(run_ids or {uuid.UUID(int=0)}))).scalars()
    }
    return {
        "items": [
            _comparison_out(row, runs.get(row.baseline_run_id), runs.get(row.current_run_id)).model_dump(mode="json")
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{comparison_id}", response_model=ComparisonOut)
def get_comparison(
    project_id: uuid.UUID,
    comparison_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS, SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    comparison = _get_comparison(db, project_id, comparison_id)
    baseline = db.get(ScanRun, comparison.baseline_run_id)
    current = db.get(ScanRun, comparison.current_run_id)
    return _comparison_out(comparison, baseline, current)


@router.post("/{comparison_id}/retry", response_model=ComparisonOut, status_code=status.HTTP_202_ACCEPTED)
def retry_comparison(
    project_id: uuid.UUID,
    comparison_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_RUNS, SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    # Keep the idempotent and terminal-state reads cheap. A failed comparison
    # is re-read under the project admission lock below before any state is
    # reset, so this optimistic read never authorizes a mutation by itself.
    comparison = db.execute(
        select(RunComparison)
        .where(RunComparison.id == comparison_id, RunComparison.project_id == project_id)
    ).scalar_one_or_none()
    if comparison is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    _require_current_comparison_algorithm(comparison)
    baseline = _get_complete_run(db, project_id, comparison.baseline_run_id, "baseline")
    current = _get_complete_run(db, project_id, comparison.current_run_id, "current")
    if comparison.state in {"queued", "running"}:
        return _comparison_out(comparison, baseline, current)
    if comparison.state == "complete":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "COMPARISON_ALREADY_COMPLETE",
                "message": "A completed comparison cannot be retried; create a comparison with different runs instead.",
            },
        )
    if comparison.state != "failed":
        raise HTTPException(status_code=409, detail="comparison is not in a retryable state")

    settings = get_settings()
    # Creation and retry consume the same admission budget so alternating the
    # two endpoints cannot bypass the project comparison rate limit.
    rate_limiter.check(
        request,
        "comparison_create",
        limit=settings.api_comparison_rate_limit,
        window_seconds=settings.api_comparison_rate_window_seconds,
        actor_key=f"{auth.user_id or auth.token_id}:{project_id}",
    )
    # Match worker lock order: job -> source -> project admission. Ingest owns
    # source -> project, so taking project first here would invert that order.
    lock_worker_job(db, comparison_id)
    comparison = db.execute(
        select(RunComparison)
        .where(RunComparison.id == comparison_id, RunComparison.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if comparison is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    _require_current_comparison_algorithm(comparison)
    baseline = _get_complete_run(db, project_id, comparison.baseline_run_id, "baseline")
    current = _get_complete_run(db, project_id, comparison.current_run_id, "current")
    if comparison.state in {"queued", "running"}:
        return _comparison_out(comparison, baseline, current)
    if comparison.state == "complete":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "COMPARISON_ALREADY_COMPLETE",
                "message": "A completed comparison cannot be retried; create a comparison with different runs instead.",
            },
        )
    if comparison.state != "failed":
        raise HTTPException(status_code=409, detail="comparison is not in a retryable state")

    if comparison.trigger == "automatic" and comparison.source_id is not None:
        source_candidate = db.get(CollectionSource, comparison.source_id)
        if source_candidate is not None:
            lock_monitoring_source(db, source_candidate.source_key)
    comparison_lock_key = project_id.int % (2**63 - 1)
    db.execute(select(func.pg_advisory_xact_lock(comparison_lock_key)))

    active_count = int(
        db.execute(
            select(func.count(RunComparison.id)).where(
                RunComparison.project_id == project_id,
                RunComparison.state.in_(("queued", "running")),
            )
        ).scalar()
        or 0
    )
    if active_count >= settings.api_comparison_max_active_per_project:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "COMPARISON_CAPACITY_REACHED",
                "message": "This project already has the maximum number of active comparisons. Retry shortly.",
            },
            headers={"Retry-After": "5"},
        )

    try:
        source_coverage_updated = publish_automatic_baseline_recovery(
            db,
            comparison,
            findings_only=False,
            require_enabled=True,
        )
    except AutomaticSourceDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "COMPARISON_SOURCE_DISABLED",
                "message": "Enable this comparison's collection source before retrying automatic recovery.",
            },
        ) from exc
    previous_attempt_count = comparison.attempt_count
    previous_error_code = comparison.error_code
    comparison.state = "queued"
    comparison.progress = {"phase": "queued", "processed": 0, "operator_retry": True}
    comparison.summary = {
        "appeared": 0,
        "disappeared": 0,
        "changed": 0,
        "indeterminate": 0,
        "total": 0,
        "exact": False,
        "resource_summary_exact": False,
        "item_churn_computed": False,
    }
    comparison.error_code = None
    comparison.error_message = None
    comparison.attempt_count = 0
    comparison.started_at = None
    comparison.completed_at = None
    comparison.heartbeat_at = None
    comparison.next_retry_at = None
    write_audit_event(
        db,
        action="COMPARISON_RETRY_REQUESTED",
        object_type="run_comparison",
        object_id=str(comparison.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "previous_attempt_count": previous_attempt_count,
            "previous_error_code": previous_error_code,
            "baseline_run_id": str(comparison.baseline_run_id),
            "current_run_id": str(comparison.current_run_id),
            "source_coverage_updated": source_coverage_updated,
        },
    )
    db.commit()
    db.refresh(comparison)
    try:
        enqueue_worker_job(
            {"job_type": "comparison", "comparison_id": comparison.id, "project_id": project_id}
        )
    except Exception:
        logger.warning("comparison retry enqueue failed; worker recovery will claim it comparison_id=%s", comparison.id)
    return _comparison_out(comparison, baseline, current)


@router.get("/{comparison_id}/resource-changes", response_model=dict)
def list_resource_changes(
    project_id: uuid.UUID,
    comparison_id: uuid.UUID,
    request: Request,
    change_type: str | None = Query(default=None, max_length=24),
    provider: str | None = Query(default=None, max_length=32),
    category: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=512),
    search: str | None = Query(default=None, max_length=512, deprecated=True),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS, SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    comparison = _get_comparison(db, project_id, comparison_id)
    if comparison.state != "complete":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "COMPARISON_NOT_COMPLETE",
                "message": f"Comparison results are not available while state is {comparison.state}.",
            },
        )

    stmt = select(ComparisonResourceChange).where(ComparisonResourceChange.comparison_id == comparison_id)
    if change_type:
        normalized_type = change_type.strip().lower()
        if normalized_type not in CHANGE_TYPES:
            raise HTTPException(status_code=400, detail="unsupported change_type")
        stmt = stmt.where(ComparisonResourceChange.change_type == normalized_type)
    if provider:
        stmt = stmt.where(ComparisonResourceChange.provider == provider.strip().lower())
    if category:
        normalized_category = category.strip().lower()
        stmt = stmt.where(ComparisonResourceChange.change_categories.contains([normalized_category]))
    query_text = q if q is not None else search
    normalized_query = query_text.strip() if query_text else ""
    if normalized_query:
        pattern = f"%{escape_like(normalized_query)}%"
        stmt = stmt.where(ComparisonResourceChange.search_text.ilike(pattern, escape="\\"))
    stmt = apply_keyset_pagination(stmt, CHANGE_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), CHANGE_CURSOR, limit)
    item_counts_by_resource: dict[int, dict[str, int]] = {}
    if rows:
        for resource_change_id, item_change_type, count in db.execute(
            select(
                ComparisonItemChange.resource_change_id,
                ComparisonItemChange.change_type,
                func.count(ComparisonItemChange.id),
            )
            .where(
                ComparisonItemChange.comparison_id == comparison_id,
                ComparisonItemChange.resource_change_id.in_([row.id for row in rows]),
            )
            .group_by(ComparisonItemChange.resource_change_id, ComparisonItemChange.change_type)
        ).all():
            item_counts_by_resource.setdefault(int(resource_change_id), {})[str(item_change_type)] = int(count)
    item_history_computed = bool((comparison.summary or {}).get("item_churn_computed"))
    item_history_exact = bool((comparison.summary or {}).get("item_summary_exact"))

    items: list[ComparisonResourceChangeOut] = []
    for row in rows:
        before = dict(row.before_snapshot or {}) if row.before_resource_id is not None else None
        after = dict(row.after_snapshot or {}) if row.after_resource_id is not None else None
        if before is not None:
            before.setdefault("resource_id", row.before_resource_id)
            before.setdefault("endpoint_key", row.endpoint_key_before)
            before.setdefault("name", row.resource_name_before)
        if after is not None:
            after.setdefault("resource_id", row.after_resource_id)
            after.setdefault("endpoint_key", row.endpoint_key_after)
            after.setdefault("name", row.resource_name_after)
        items.append(
            ComparisonResourceChangeOut(
                id=row.id,
                identity_key=row.identity_key,
                change_type=row.change_type,
                provider=row.provider,
                resource_type=row.resource_type,
                provider_resource_id=row.provider_resource_id,
                before=before,
                after=after,
                change_categories=list(row.change_categories or []),
                structural_state=row.structural_state,
                access_state=row.access_state,
                content_state=row.content_state,
                access_interpretation=row.access_interpretation,
                match={"basis": row.match_basis, "quality": row.match_quality},
                item_changes=(
                    {
                        "state": "computed",
                        "exact": item_history_exact,
                        "counts": {
                            item_type: item_counts_by_resource.get(row.id, {}).get(item_type, 0)
                            for item_type in sorted(ITEM_CHANGE_TYPES)
                        },
                        "total": sum(item_counts_by_resource.get(row.id, {}).values()),
                        "before_count": row.item_count_before,
                        "after_count": row.item_count_after,
                    }
                    if item_history_computed
                    else {
                        "state": "not_computed",
                        "exact": False,
                        "counts": None,
                        "total": None,
                        "before_count": row.item_count_before,
                        "after_count": row.item_count_after,
                    }
                ),
                impact_rank=row.impact_rank,
            )
        )
    write_audit_event(
        db,
        action="COMPARISON_RESOURCE_CHANGES_LISTED",
        object_type="run_comparison",
        object_id=str(comparison_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "change_type": change_type.strip().lower() if change_type else None,
            "provider": provider.strip().lower() if provider else None,
            "category": category.strip().lower() if category else None,
            "query_applied": bool(normalized_query),
            "cursor_applied": cursor is not None,
            "limit": limit,
            "result_count": len(items),
            "has_next_page": next_cursor is not None,
        },
    )
    db.commit()
    algorithm_current = comparison_algorithm_is_current(comparison.algorithm_version)
    return {
        "items": items,
        "next_cursor": next_cursor,
        "comparison_state": comparison.state,
        "algorithm_version": comparison.algorithm_version,
        "algorithm_current": algorithm_current,
        "algorithm_warning": None if algorithm_current else LEGACY_ALGORITHM_WARNING,
    }


@router.get("/{comparison_id}/item-changes", response_model=dict)
def list_item_changes(
    project_id: uuid.UUID,
    comparison_id: uuid.UUID,
    request: Request,
    change_type: str | None = Query(default=None, max_length=32),
    resource_change_id: int | None = Query(default=None, ge=1),
    q: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS, SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    comparison = _get_comparison(db, project_id, comparison_id)
    if comparison.state != "complete":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "COMPARISON_NOT_COMPLETE",
                "message": f"Comparison results are not available while state is {comparison.state}.",
            },
        )
    stmt = select(ComparisonItemChange).where(ComparisonItemChange.comparison_id == comparison_id)
    normalized_type = change_type.strip().lower() if change_type else None
    if normalized_type:
        if normalized_type not in ITEM_CHANGE_TYPES:
            raise HTTPException(status_code=400, detail="unsupported item change_type")
        stmt = stmt.where(ComparisonItemChange.change_type == normalized_type)
    if resource_change_id:
        stmt = stmt.where(ComparisonItemChange.resource_change_id == resource_change_id)
    if q and q.strip():
        stmt = stmt.where(
            ComparisonItemChange.search_text.ilike(f"%{escape_like(q.strip())}%", escape="\\")
        )
    stmt = apply_keyset_pagination(stmt, ITEM_CHANGE_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), ITEM_CHANGE_CURSOR, limit)
    items = [
        ComparisonItemChangeOut(
            id=row.id,
            resource_change_id=row.resource_change_id,
            identity_key=row.identity_key,
            change_type=row.change_type,
            provider=row.provider,
            before=dict(row.before_snapshot or {}) if row.before_item_id is not None else None,
            after=dict(row.after_snapshot or {}) if row.after_item_id is not None else None,
            change_categories=list(row.change_categories or []),
            evidence_state=row.evidence_state,
            limitations=[str(item) for item in (row.limitations or [])],
            match={"basis": row.match_basis, "quality": row.match_quality},
            impact_rank=row.impact_rank,
        ).model_dump(mode="json")
        for row in rows
    ]
    write_audit_event(
        db,
        action="COMPARISON_ITEM_CHANGES_LISTED",
        object_type="run_comparison",
        object_id=str(comparison_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={
            **request_meta(request),
            "result_count": len(rows),
            "change_type": normalized_type,
            "resource_change_id": resource_change_id,
            "query_applied": bool(q and q.strip()),
            "cursor_applied": cursor is not None,
            "limit": limit,
            "has_next_page": next_cursor is not None,
        },
    )
    db.commit()
    algorithm_current = comparison_algorithm_is_current(comparison.algorithm_version)
    item_limitations = list((comparison.summary or {}).get("item_limitations") or [])
    if not algorithm_current:
        item_limitations.append(LEGACY_ALGORITHM_WARNING)
    return {
        "items": items,
        "next_cursor": next_cursor,
        "comparison_state": comparison.state,
        "algorithm_version": comparison.algorithm_version,
        "algorithm_current": algorithm_current,
        "algorithm_warning": None if algorithm_current else LEGACY_ALGORITHM_WARNING,
        "interpretation": {
            "state": "computed" if (comparison.summary or {}).get("item_churn_computed") else "not_computed",
            "exact": bool((comparison.summary or {}).get("item_summary_exact")),
            "limitations": item_limitations,
        },
    }
