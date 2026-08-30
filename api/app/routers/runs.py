import asyncio
import hashlib
import heapq
import logging
import time
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import String, and_, case, cast, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import SessionLocal, escape_like, get_db
from app.deps import AuthContext, get_auth_context, request_meta, require_project_role, require_token_scopes
from app.enums import ErrorSeverity, ProjectRole, RunStatus
from app.locking import lock_project_admin_guard
from app.models import (
    AuditEvent,
    Endpoint,
    IngestError,
    Item,
    PermissionAssessment,
    PermissionEntry,
    PermissionPrincipal,
    Resource,
    RunComparison,
    ScanRun,
)
from app.pagination import (
    KeysetColumn,
    apply_keyset_pagination,
    paginate_rows,
    parse_datetime_cursor_value,
    parse_int_cursor_value,
    parse_uuid_cursor_value,
)
from app.rate_limit import RateLimiter
from app.schemas import AccessEvidenceOut, IngestErrorOut, RunActivityEventOut, RunCreateIn, RunOut
from app.services.access_evidence import build_access_evidence_summary
from app.services.audit import write_audit_event
from app.services.queue import enqueue_ingest_job
from app.services.storage import (
    abort_multipart_upload,
    complete_multipart_upload,
    create_multipart_upload,
    delete_object,
    upload_part,
)
from app.share_types import share_type_from_resource_type
from app.token_scopes import SCOPE_READ_INVENTORY, SCOPE_READ_RUNS, SCOPE_WRITE_RUNS

router = APIRouter(prefix="/projects/{project_id}/runs", tags=["runs"])
rate_limiter = RateLimiter()
logger = logging.getLogger("share_sentinel.runs")
ALLOWED_ARTIFACT_SUFFIXES = (".json", ".json.gz", ".ndjson", ".ndjson.gz", ".jsonl", ".jsonl.gz")
ALLOWED_ARTIFACT_CONTENT_TYPES = {
    "application/json",
    "application/x-ndjson",
    "application/gzip",
    "application/x-gzip",
    "application/octet-stream",
}
RAW_ARTIFACT_FILENAME_HEADER = "x-artifact-filename"
ARTIFACT_SIGNATURE_SNIFF_BYTES = 4096
RUN_LIST_CURSOR = (
    KeysetColumn("created_at", ScanRun.created_at, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", ScanRun.id, direction="desc", parser=parse_uuid_cursor_value),
)
RUN_ENDPOINT_CURSOR = (KeysetColumn("id", Endpoint.id, parser=parse_int_cursor_value),)
RUN_RESOURCE_CURSOR = (KeysetColumn("id", Resource.id, parser=parse_int_cursor_value),)
RUN_ITEM_CURSOR = (KeysetColumn("id", Item.id, parser=parse_int_cursor_value),)
RUN_ERROR_CURSOR = (KeysetColumn("id", IngestError.id, parser=parse_int_cursor_value),)
RUN_ACTIVITY_CURSOR = (
    KeysetColumn("ts", AuditEvent.ts, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", AuditEvent.id, direction="desc", parser=parse_int_cursor_value),
)
EFFECTIVE_ACCESS_PRINCIPAL_CURSOR = (
    KeysetColumn("id", PermissionPrincipal.id, parser=parse_int_cursor_value),
)
RUN_ACTIVITY_ACTIONS = {
    "RUN_CREATED",
    "ARTIFACT_UPLOADED",
    "INGEST_QUEUED",
    "INGEST_QUEUE_FALLBACK",
    "INGEST_STARTED",
    "INGEST_PAUSED",
    "INGEST_RETRY_SCHEDULED",
    "INGEST_COMPLETED",
    "INGEST_FAILED",
}
EMPTY_RUN_SUMMARY = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}
UPLOADABLE_RUN_STATUSES = frozenset({RunStatus.PENDING_UPLOAD, RunStatus.UPLOADED, RunStatus.FAILED})
MAX_SEARCH_CHARS = 512
MAX_PATH_PREFIX_CHARS = 4096
MAX_RUN_DIFF_DETAIL_RECORDS = 2000
DEFAULT_RUN_DIFF_DETAIL_RECORDS = 500
ACCESS_EVIDENCE_ASSESSMENT_PAGE_MAX = 25
ACCESS_EVIDENCE_ENTRY_PAGE_MAX = 100
EFFECTIVE_ACCESS_ENTRY_RESPONSE_MAX = 1000
EFFECTIVE_ACCESS_ENTRY_PRINCIPAL_MAX = 100
EFFECTIVE_ACCESS_ASSESSMENT_MAX = 100
RECOGNIZED_PROVIDER_EFFECTIVE_DECISIONS = {
    "allow": "allow",
    "allowed": "allow",
    "effective_allow": "allow",
    "deny": "deny",
    "denied": "deny",
    "no_access": "deny",
    "effective_deny": "deny",
    "mixed": "mixed",
}


def _provider_computed_effective_decision(
    assessments: list[PermissionAssessment],
    *,
    assessments_truncated: bool,
    principal_filtered: bool,
) -> tuple[str, PermissionAssessment | None]:
    """Trust only one complete, explicit provider-computed resource assessment."""

    if principal_filtered or assessments_truncated or len(assessments) != 1:
        return "unknown", None
    assessment = assessments[0]
    decision = RECOGNIZED_PROVIDER_EFFECTIVE_DECISIONS.get(
        str(assessment.effective_access_status or "").strip().lower()
    )
    complete_resolution_values = {"complete", "resolved", "fully_resolved"}
    complete_provider_computation = (
        assessment.assessment_state == "complete"
        and assessment.retrieval_coverage in {"complete", "full", "all_returned"}
        and assessment.semantic_coverage in {"effective_access", "effective_permissions"}
        and assessment.principal_resolution in complete_resolution_values
        and assessment.entries_omitted == 0
        and assessment.unknown_entries == 0
        and decision is not None
        and (decision != "deny" or assessment.negative_conclusion_supported)
    )
    return (decision, assessment) if complete_provider_computation and decision is not None else ("unknown", None)


def _provider_from_resource_type_expression():
    resource_type = func.lower(cast(Resource.resource_type, String))
    return case(
        (resource_type == "smb_share", "smb"),
        (resource_type == "nfs_share", "nfs"),
        (resource_type == "sharepoint_library", "sharepoint"),
        else_=None,
    )


def _resource_provider_expression():
    return func.coalesce(Resource.provider, _provider_from_resource_type_expression())


def _item_provider_expression():
    resource_provider = (
        select(_resource_provider_expression())
        .where(
            Resource.run_id == Item.run_id,
            Resource.id == Item.resource_id,
        )
        .correlate(Item)
        .scalar_subquery()
    )
    return func.coalesce(Item.provider, resource_provider)


def _resource_provider_equals_expression(value: str):
    return or_(
        Resource.provider == value,
        and_(
            Resource.provider.is_(None),
            _provider_from_resource_type_expression() == value,
        ),
    )


def _item_provider_equals_expression(value: str):
    inferred_provider = (
        select(_resource_provider_expression())
        .where(
            Resource.run_id == Item.run_id,
            Resource.id == Item.resource_id,
        )
        .correlate(Item)
        .scalar_subquery()
    )
    return or_(
        Item.provider == value,
        and_(Item.provider.is_(None), inferred_provider == value),
    )


def _run_lock_key(run_id: uuid.UUID) -> int:
    return run_id.int % (2**63 - 1)


def _try_lock_run_for_mutation(db: Session, run_id: uuid.UUID) -> bool:
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _run_lock_key(run_id)}).scalar())


def _get_run(db: Session, project_id: uuid.UUID, run_id: uuid.UUID) -> ScanRun:
    stmt = select(ScanRun).where(ScanRun.id == run_id, ScanRun.project_id == project_id)
    run = db.execute(stmt).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


def _require_endpoint(
    db: Session,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    endpoint_id: int,
) -> None:
    endpoint = db.execute(
        select(Endpoint.id)
        .join(ScanRun, ScanRun.id == Endpoint.run_id)
        .where(
            Endpoint.id == endpoint_id,
            Endpoint.run_id == run_id,
            ScanRun.project_id == project_id,
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint not found in run")


def _require_resource(
    db: Session,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    resource_id: int,
) -> Resource:
    resource = db.execute(
        select(Resource)
        .join(ScanRun, ScanRun.id == Resource.run_id)
        .where(
            Resource.id == resource_id,
            Resource.run_id == run_id,
            ScanRun.project_id == project_id,
        )
    ).scalar_one_or_none()
    if resource is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="resource not found in run")
    return resource


def _to_run_out(run: ScanRun) -> RunOut:
    return RunOut(
        id=run.id,
        project_id=run.project_id,
        source_id=getattr(run, "source_id", None),
        name=run.name,
        description=run.description,
        target_scope=run.target_scope,
        created_at=run.created_at,
        status=run.status,
        artifact_size=run.artifact_size,
        artifact_sha256=run.artifact_sha256,
        artifact_content_type=run.artifact_content_type,
        ingest_progress=run.ingest_progress,
        summary=run.summary,
        collection_context=getattr(run, "collection_context", None) or {},
    )


def _run_summary_payload(run: ScanRun) -> dict:
    return {
        "id": str(run.id),
        "name": run.name,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "status": run.status.value if hasattr(run.status, "value") else run.status,
        "collection_context": getattr(run, "collection_context", None) or {},
    }


def _run_diff_compatibility(current_run: ScanRun, baseline_run: ScanRun | None) -> dict:
    if baseline_run is None:
        return {
            "compatible": False,
            "warning": "No baseline run is available for comparison.",
            "mismatched_fields": [],
        }

    current = getattr(current_run, "collection_context", None) or {}
    baseline = getattr(baseline_run, "collection_context", None) or {}
    if not current or not baseline:
        return {
            "compatible": False,
            "warning": "Collection context is missing for at least one run; comparison semantics are unknown.",
            "mismatched_fields": [],
        }

    fields = (
        "source",
        "provider",
        "collection_mode",
        "auth_mode",
        "auth_type",
        "tenant_id",
        "client_id",
        "assessed_identity",
        "scopes",
        "roles",
        "discovery_completeness",
        "materialized_snapshot",
    )
    if not (current.get("materialized_snapshot") is True and baseline.get("materialized_snapshot") is True):
        fields += ("sync_mode",)

    def comparison_value(context: dict, field: str):
        value = context.get(field)
        if field in {"scopes", "roles"} and isinstance(value, list):
            return sorted(str(item) for item in value)
        return value

    mismatched = [field for field in fields if comparison_value(current, field) != comparison_value(baseline, field)]
    current_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
    baseline_metadata = baseline.get("metadata") if isinstance(baseline.get("metadata"), dict) else {}
    for field in (
        "discovery_strategy",
        "discovery_authoritative",
        "files_included",
        "permissions_assessed",
    ):
        if current_metadata.get(field) != baseline_metadata.get(field):
            mismatched.append(f"metadata.{field}")
    current_collection = (
        current_metadata.get("collection") if isinstance(current_metadata.get("collection"), dict) else {}
    )
    baseline_collection = (
        baseline_metadata.get("collection") if isinstance(baseline_metadata.get("collection"), dict) else {}
    )

    def effective_targeted_sites(collection: dict) -> tuple[str, ...]:
        target_scope = collection.get("target_scope") if isinstance(collection.get("target_scope"), dict) else {}
        raw_sites = target_scope.get("targeted_sites")
        if raw_sites is None:
            return ()
        if not isinstance(raw_sites, list):
            return ("<invalid-targeted-sites>",)
        normalized: set[str] = set()
        for site in raw_sites:
            if not isinstance(site, str) or not site.strip():
                continue
            value = unicodedata.normalize("NFC", site).strip()
            if value != "/":
                value = value.rstrip("/")
            normalized.add(value.casefold())
        if any(not isinstance(site, str) for site in raw_sites):
            normalized.add("<invalid-targeted-site>")
        return tuple(sorted(normalized))

    if effective_targeted_sites(current_collection) != effective_targeted_sites(baseline_collection):
        mismatched.append("metadata.collection.target_scope")

    warnings: list[str] = []
    is_sharepoint_comparison = any(
        str(context.get(field) or "").strip().lower() == "sharepoint"
        for context in (current, baseline)
        for field in ("source", "provider")
    )
    unknown_fields: list[str] = []
    if is_sharepoint_comparison:
        common_required = (
            "source",
            "provider",
            "collection_mode",
            "auth_mode",
            "auth_type",
            "tenant_id",
            "client_id",
            "discovery_completeness",
            "materialized_snapshot",
            "sync_mode",
        )

        def value_is_known(context: dict, field: str) -> bool:
            value = context.get(field)
            if field == "materialized_snapshot":
                return isinstance(value, bool)
            return isinstance(value, str) and bool(value.strip())

        for label, context, metadata in (
            ("current", current, current_metadata),
            ("baseline", baseline, baseline_metadata),
        ):
            for field in common_required:
                if not value_is_known(context, field):
                    unknown_fields.append(f"{label}.{field}")
            auth_type = str(context.get("auth_type") or "").strip().lower()
            if auth_type == "delegated":
                if not value_is_known(context, "assessed_identity"):
                    unknown_fields.append(f"{label}.assessed_identity")
                if not isinstance(context.get("scopes"), list):
                    unknown_fields.append(f"{label}.scopes")
            elif auth_type == "application" and not isinstance(context.get("roles"), list):
                unknown_fields.append(f"{label}.roles")
            if not isinstance(metadata.get("files_included"), bool):
                unknown_fields.append(f"{label}.metadata.files_included")
            if (
                not isinstance(metadata.get("discovery_strategy"), str)
                or not str(metadata.get("discovery_strategy")).strip()
            ):
                unknown_fields.append(f"{label}.metadata.discovery_strategy")
            if not isinstance(metadata.get("discovery_authoritative"), bool):
                unknown_fields.append(f"{label}.metadata.discovery_authoritative")
    if unknown_fields:
        mismatched.extend(f"unknown:{field}" for field in unknown_fields)
        warnings.append(
            "Required SharePoint comparison context is unknown or missing: " + ", ".join(unknown_fields) + "."
        )
    if is_sharepoint_comparison:
        opaque_labels = [
            label
            for label, context in (("current", current), ("baseline", baseline))
            if str(context.get("jwt_inspection") or "").strip().casefold()
            == "opaque_token_context_supplied_by_operator"
        ]
        if opaque_labels:
            mismatched.extend(f"unknown:{label}.permissions" for label in opaque_labels)
            warnings.append("Microsoft Graph permissions cannot be verified for opaque imported-token SharePoint runs.")
    if mismatched:
        differing = [field for field in mismatched if not field.startswith("unknown:")]
        if differing:
            warnings.append("Collection perspectives differ: " + ", ".join(differing) + ".")
    if current.get("partial") is True or baseline.get("partial") is True:
        warnings.append("At least one run reports partial collection coverage.")
    if current.get("materialized_snapshot") is False or baseline.get("materialized_snapshot") is False:
        warnings.append("At least one run is not a materialized point-in-time snapshot.")
    return {
        "compatible": not warnings,
        "warning": " ".join(warnings) if warnings else None,
        "mismatched_fields": mismatched,
    }


def _strict_run_diff_compatibility(current_run: ScanRun, baseline_run: ScanRun | None) -> dict:
    """Gate legacy item previews with the current provider scope contracts.

    The bounded diff still computes snapshot set differences for convenience,
    but its single historical ``compatible`` flag may authorize New/Disappeared
    and Added/Removed wording only when both structural and content dimensions
    pass the provider-specific comparison contract. SMB also needs the worker's
    endpoint identity preflight, which this synchronous endpoint cannot perform.
    """

    legacy = _run_diff_compatibility(current_run, baseline_run)
    if baseline_run is None:
        return legacy

    # Local import avoids a module-import cycle: the comparison router reuses
    # the generic helper above while this request-time adapter consumes its
    # stricter dimension-specific contract.
    from app.routers.comparisons import build_comparison_compatibility

    strict = build_comparison_compatibility(current_run, baseline_run)
    structural = strict.get("structural_interpretable") is True
    content = strict.get("content_interpretable") is True
    identity = strict.get("identity_applicable") is False or strict.get("identity_scope_exact") is True
    compatible = bool(legacy.get("compatible") is True and structural and content and identity)

    warnings = [str(legacy.get("warning") or "").strip()]
    warnings.extend(str(reason).strip() for reason in strict.get("reasons", []) if str(reason).strip())
    if not identity and strict.get("identity_applicable") is True:
        warnings.append(
            "Definitive SMB absence claims require the materialized comparison's endpoint identity preflight."
        )
    warnings = list(dict.fromkeys(warning for warning in warnings if warning))

    mismatched = [str(field) for field in legacy.get("mismatched_fields", []) if str(field)]
    if not structural:
        mismatched.append("comparison.structural_scope")
    if not content:
        mismatched.append("comparison.content_scope")
    if not identity:
        mismatched.append("comparison.identity_scope")
    return {
        "compatible": compatible,
        "warning": " ".join(warnings) if warnings else None,
        "mismatched_fields": list(dict.fromkeys(mismatched)),
        "dimensions": {
            "structural_interpretable": structural,
            "content_interpretable": content,
            "identity_scope_exact": identity,
        },
    }


def _clear_run_ingest_data(db: Session, run: ScanRun) -> None:
    # A replacement artifact invalidates every materialized result involving
    # this run. Delete comparison headers first so no stale partial result can
    # survive while resource foreign keys are cleared.
    db.execute(
        delete(RunComparison).where(
            or_(RunComparison.baseline_run_id == run.id, RunComparison.current_run_id == run.id)
        )
    )
    db.execute(delete(PermissionPrincipal).where(PermissionPrincipal.run_id == run.id))
    db.execute(delete(Item).where(Item.run_id == run.id))
    db.execute(delete(Resource).where(Resource.run_id == run.id))
    db.execute(delete(Endpoint).where(Endpoint.run_id == run.id))
    db.execute(delete(IngestError).where(IngestError.run_id == run.id))
    run.summary = dict(EMPTY_RUN_SUMMARY)
    run.ingest_progress = {"line_offset": 0}
    run.collection_context = {}


def _require_uploadable_run(run: ScanRun) -> None:
    if run.status not in UPLOADABLE_RUN_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run state does not accept upload")


def _rollback_upload_session_quietly(db: Session, run_id: uuid.UUID, operation: str) -> None:
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("failed to roll back %s transaction run_id=%s", operation, run_id)


def _close_upload_session_quietly(db: Session, run_id: uuid.UUID, operation: str) -> None:
    try:
        db.close()
    except Exception:  # noqa: BLE001
        logger.exception("failed to close %s session run_id=%s", operation, run_id)


def _delete_artifact_quietly(key: str | None) -> None:
    if not key:
        return
    try:
        delete_object(key)
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001 - cleanup must not mask the primary operation.
        logger.warning("failed to delete artifact key=%s", key, exc_info=True)


def _delete_superseded_artifact(previous_key: str | None, current_key: str) -> None:
    if previous_key and previous_key != current_key:
        _delete_artifact_quietly(previous_key)


def _resource_identity(
    endpoint_key: str,
    resource_type: str,
    share_name: str,
    provider_resource_id: str | None = None,
) -> tuple[str, str, str]:
    if provider_resource_id:
        # Provider-native IDs survive DNS/IP/site aliases and endpoint
        # renames. Retaining endpoint_key here would turn a move into a false
        # disappear+appear pair.
        return ("", resource_type or "", f"provider:{provider_resource_id}")
    return (endpoint_key or "", resource_type or "", share_name or "")


def _sorted_resource_keys(keys: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return sorted(keys, key=_normalized_resource_key)


def _normalized_resource_key(key: tuple[str, str, str]) -> tuple[str, str, str]:
    identity = key[2]
    normalized_identity = identity if identity.startswith("provider:") else identity.lower()
    return (key[0].lower(), normalized_identity, key[1].lower())


def _resource_diff_record(
    endpoint_key: str,
    hostname: str | None,
    ip: str | None,
    share_name: str,
    resource_type: str,
    access_level: str | None,
    item_paths: set[str] | None = None,
    provider_resource_id: str | None = None,
) -> dict:
    record = {
        "endpoint_key": endpoint_key,
        "hostname": hostname,
        "ip": ip,
        "share_name": share_name,
        "resource_type": resource_type,
        "share_type": share_type_from_resource_type(resource_type),
        "access_level": access_level,
        "item_paths": item_paths or set(),
    }
    if provider_resource_id:
        record["provider_resource_id"] = provider_resource_id
        record["item_identities"] = {}
    return record


def _load_run_diff_snapshot(db: Session, run_id: uuid.UUID) -> dict[tuple[str, str, str], dict]:
    resource_rows = db.execute(
        select(
            Resource.name,
            Resource.access_level,
            Resource.resource_type,
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Endpoint.ip,
            Resource.provider_resource_id,
        )
        .select_from(Resource)
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .where(Resource.run_id == run_id)
    ).all()

    snapshot: dict[tuple[str, str, str], dict] = {}
    for row in resource_rows:
        resource_type = row.resource_type.value if hasattr(row.resource_type, "value") else str(row.resource_type)
        access_level = row.access_level.value if hasattr(row.access_level, "value") else row.access_level
        provider_resource_id = getattr(row, "provider_resource_id", None)
        identity = _resource_identity(
            row.endpoint_key,
            resource_type,
            row.name,
            provider_resource_id,
        )
        snapshot[identity] = {
            "endpoint_key": row.endpoint_key,
            "hostname": row.hostname,
            "ip": row.ip,
            "share_name": row.name,
            "resource_type": resource_type,
            "share_type": share_type_from_resource_type(row.resource_type),
            "access_level": access_level,
            "item_paths": set(),
        }
        if provider_resource_id:
            snapshot[identity]["provider_resource_id"] = provider_resource_id
            snapshot[identity]["item_identities"] = {}

    item_rows = db.execute(
        select(
            Resource.name,
            Resource.resource_type,
            Endpoint.endpoint_key,
            Item.path,
            Resource.provider_resource_id,
            Item.provider_item_id,
            Item.deleted,
        )
        .select_from(Item)
        .join(Resource, (Resource.id == Item.resource_id) & (Resource.run_id == Item.run_id))
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .where(Item.run_id == run_id, Item.deleted.is_(False))
    ).all()

    for row in item_rows:
        resource_type = row.resource_type.value if hasattr(row.resource_type, "value") else str(row.resource_type)
        provider_resource_id = getattr(row, "provider_resource_id", None)
        identity = _resource_identity(
            row.endpoint_key,
            resource_type,
            row.name,
            provider_resource_id,
        )
        record = snapshot.get(identity)
        if record is None:
            record = {
                "endpoint_key": row.endpoint_key,
                "hostname": None,
                "ip": None,
                "share_name": row.name,
                "resource_type": resource_type,
                "share_type": share_type_from_resource_type(row.resource_type),
                "access_level": None,
                "item_paths": set(),
            }
            if provider_resource_id:
                record["provider_resource_id"] = provider_resource_id
                record["item_identities"] = {}
            snapshot[identity] = record
        record["item_paths"].add(row.path)
        provider_item_id = getattr(row, "provider_item_id", None)
        if provider_item_id:
            record.setdefault("item_identities", {})[f"provider:{provider_item_id}"] = row.path

    return snapshot


def _iter_run_diff_resources(db: Session, run_id: uuid.UUID):
    stmt = (
        select(
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Endpoint.ip,
            Resource.name,
            Resource.resource_type,
            Resource.access_level,
            Resource.provider_resource_id,
            Item.path,
            Item.provider_item_id,
            Item.deleted,
        )
        .select_from(Resource)
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .outerjoin(
            Item,
            (Item.resource_id == Resource.id) & (Item.run_id == Resource.run_id) & (Item.deleted.is_(False)),
        )
        .where(Resource.run_id == run_id)
        .order_by(
            case(
                (Resource.provider_resource_id.is_not(None), ""),
                else_=func.lower(Endpoint.endpoint_key),
            ),
            case(
                (
                    Resource.provider_resource_id.is_not(None),
                    "provider:" + Resource.provider_resource_id,
                ),
                else_=func.lower(Resource.name),
            ),
            func.lower(cast(Resource.resource_type, String)),
            Item.path.asc().nulls_last(),
        )
    )

    current_key: tuple[str, str, str] | None = None
    current_record: dict | None = None
    for row in db.execute(stmt.execution_options(yield_per=1000)):
        resource_type = row.resource_type.value if hasattr(row.resource_type, "value") else str(row.resource_type)
        access_level = row.access_level.value if hasattr(row.access_level, "value") else row.access_level
        provider_resource_id = getattr(row, "provider_resource_id", None)
        key = _resource_identity(
            row.endpoint_key,
            resource_type,
            row.name,
            provider_resource_id,
        )
        if key != current_key:
            if current_key is not None and current_record is not None:
                yield current_key, current_record
            current_key = key
            current_record = _resource_diff_record(
                endpoint_key=row.endpoint_key,
                hostname=row.hostname,
                ip=row.ip,
                share_name=row.name,
                resource_type=resource_type,
                access_level=access_level,
                provider_resource_id=provider_resource_id,
            )
        if row.path is not None and current_record is not None:
            current_record["item_paths"].add(row.path)
            provider_item_id = getattr(row, "provider_item_id", None)
            if provider_item_id:
                current_record.setdefault("item_identities", {})[f"provider:{provider_item_id}"] = row.path

    if current_key is not None and current_record is not None:
        yield current_key, current_record


def _next_resource_record(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def _serialize_diff_resource(resource: dict) -> dict:
    serialized = {
        "endpoint_key": resource["endpoint_key"],
        "hostname": resource["hostname"],
        "ip": resource["ip"],
        "share_name": resource["share_name"],
        "share_type": resource["share_type"],
        "access_level": resource["access_level"],
        "item_count": len(_item_identity_map(resource)),
    }
    if resource.get("provider_resource_id"):
        serialized["provider_resource_id"] = resource["provider_resource_id"]
    return serialized


def _item_identity_map(resource: dict) -> dict[str, str]:
    identities = resource.get("item_identities")
    if isinstance(identities, dict):
        normalized = {str(identity): str(path) for identity, path in identities.items()}
        identified_paths = set(normalized.values())
        for path in resource.get("item_paths", set()):
            if str(path) not in identified_paths:
                normalized[f"path:{path}"] = str(path)
        return normalized
    return {f"path:{path}": str(path) for path in resource.get("item_paths", set())}


def _set_difference_summary(left: set[str], right: set[str], example_limit: int) -> tuple[int, list[str]]:
    count = 0

    def iter_difference():
        nonlocal count
        for path in left:
            if path not in right:
                count += 1
                yield path

    examples = heapq.nsmallest(example_limit, iter_difference())
    return count, examples


def _identity_difference_summary(
    left: dict[str, str],
    right: dict[str, str],
    example_limit: int,
) -> tuple[int, list[str]]:
    identities = left.keys() - right.keys()
    return len(identities), heapq.nsmallest(example_limit, (left[identity] for identity in identities))


def _moved_item_summary(
    current: dict[str, str],
    baseline: dict[str, str],
    example_limit: int,
) -> tuple[int, list[dict[str, str]]]:
    moved = [
        {
            "provider_item_id": identity.removeprefix("provider:"),
            "from_path": baseline[identity],
            "to_path": current[identity],
        }
        for identity in current.keys() & baseline.keys()
        if identity.startswith("provider:") and current[identity] != baseline[identity]
    ]
    moved.sort(key=lambda item: (item["to_path"].lower(), item["from_path"].lower(), item["provider_item_id"]))
    return len(moved), moved[:example_limit]


def _build_run_diff_from_iters(
    current_iter,
    baseline_iter,
    example_limit: int = 5,
    detail_limit: int | None = None,
) -> dict:
    current_iter = iter(current_iter)
    baseline_iter = iter(baseline_iter)
    current_entry = _next_resource_record(current_iter)
    baseline_entry = _next_resource_record(baseline_iter)

    new_shares: list[dict] = []
    disappeared_shares: list[dict] = []
    new_share_count = 0
    disappeared_share_count = 0
    changed_share_count = 0
    added_item_count = 0
    removed_item_count = 0
    moved_item_count = 0

    def append_bounded(items: list[dict], record: dict) -> None:
        if detail_limit is None or len(items) < detail_limit:
            items.append(record)

    def iter_item_churn():
        nonlocal current_entry
        nonlocal baseline_entry
        nonlocal new_share_count
        nonlocal disappeared_share_count
        nonlocal changed_share_count
        nonlocal added_item_count
        nonlocal removed_item_count
        nonlocal moved_item_count

        while current_entry is not None or baseline_entry is not None:
            if baseline_entry is None:
                _current_key, current_resource = current_entry
                new_share_count += 1
                append_bounded(new_shares, _serialize_diff_resource(current_resource))
                current_entry = _next_resource_record(current_iter)
                continue

            if current_entry is None:
                _baseline_key, baseline_resource = baseline_entry
                disappeared_share_count += 1
                append_bounded(disappeared_shares, _serialize_diff_resource(baseline_resource))
                baseline_entry = _next_resource_record(baseline_iter)
                continue

            current_key, current_resource = current_entry
            baseline_key, baseline_resource = baseline_entry
            if _normalized_resource_key(current_key) < _normalized_resource_key(baseline_key):
                new_share_count += 1
                append_bounded(new_shares, _serialize_diff_resource(current_resource))
                current_entry = _next_resource_record(current_iter)
                continue

            if _normalized_resource_key(current_key) > _normalized_resource_key(baseline_key):
                disappeared_share_count += 1
                append_bounded(disappeared_shares, _serialize_diff_resource(baseline_resource))
                baseline_entry = _next_resource_record(baseline_iter)
                continue

            current_items = _item_identity_map(current_resource)
            baseline_items = _item_identity_map(baseline_resource)
            added_items, added_examples = _identity_difference_summary(
                current_items,
                baseline_items,
                example_limit,
            )
            removed_items, removed_examples = _identity_difference_summary(
                baseline_items,
                current_items,
                example_limit,
            )
            moved_items, moved_examples = _moved_item_summary(
                current_items,
                baseline_items,
                example_limit,
            )
            current_access_level = current_resource.get("access_level")
            baseline_access_level = baseline_resource.get("access_level")
            access_level_changed = current_access_level != baseline_access_level
            if added_items or removed_items or moved_items or access_level_changed:
                changed_share_count += 1
                added_item_count += added_items
                removed_item_count += removed_items
                moved_item_count += moved_items
                churn_record = {
                    "endpoint_key": current_resource["endpoint_key"],
                    "hostname": current_resource["hostname"] or baseline_resource["hostname"],
                    "ip": current_resource["ip"] or baseline_resource["ip"],
                    "share_name": current_resource["share_name"],
                    "share_type": current_resource["share_type"],
                    "access_level": current_access_level or baseline_access_level,
                    "access_level_changed": access_level_changed,
                    "added_items": added_items,
                    "removed_items": removed_items,
                    "added_examples": added_examples,
                    "removed_examples": removed_examples,
                }
                if access_level_changed:
                    churn_record["previous_access_level"] = baseline_access_level
                provider_resource_id = current_resource.get("provider_resource_id") or baseline_resource.get(
                    "provider_resource_id"
                )
                if provider_resource_id:
                    churn_record["provider_resource_id"] = provider_resource_id
                if moved_items:
                    churn_record["moved_items"] = moved_items
                    churn_record["moved_examples"] = moved_examples
                yield churn_record

            current_entry = _next_resource_record(current_iter)
            baseline_entry = _next_resource_record(baseline_iter)

    churn_sort_key = lambda item: (  # noqa: E731
        -(item["added_items"] + item["removed_items"] + item.get("moved_items", 0)),
        item["endpoint_key"].lower(),
        item["share_name"].lower(),
    )
    churn_iter = iter_item_churn()
    if detail_limit is None:
        item_churn = sorted(churn_iter, key=churn_sort_key)
    else:
        item_churn = heapq.nsmallest(detail_limit, churn_iter, key=churn_sort_key)

    payload = {
        "summary": {
            "new_shares": new_share_count,
            "disappeared_shares": disappeared_share_count,
            "changed_shares": changed_share_count,
            "added_items": added_item_count,
            "removed_items": removed_item_count,
        },
        "new_shares": new_shares,
        "disappeared_shares": disappeared_shares,
        "item_churn": item_churn,
    }
    if moved_item_count:
        payload["summary"]["moved_items"] = moved_item_count
    if detail_limit is not None:
        sections = {
            "new_shares": new_share_count > len(new_shares),
            "disappeared_shares": disappeared_share_count > len(disappeared_shares),
            "item_churn": changed_share_count > len(item_churn),
        }
        payload["truncation"] = {
            "detail_limit": detail_limit,
            "truncated": any(sections.values()),
            "sections": sections,
        }
    return payload


def _build_run_diff(
    current_snapshot: dict[tuple[str, str, str], dict],
    baseline_snapshot: dict[tuple[str, str, str], dict],
    example_limit: int = 5,
) -> dict:
    current_iter = ((key, current_snapshot[key]) for key in _sorted_resource_keys(current_snapshot))
    baseline_iter = ((key, baseline_snapshot[key]) for key in _sorted_resource_keys(baseline_snapshot))
    return _build_run_diff_from_iters(current_iter, baseline_iter, example_limit=example_limit)


def _default_baseline_run(db: Session, project_id: uuid.UUID, run: ScanRun) -> ScanRun | None:
    stmt = (
        select(ScanRun)
        .where(
            ScanRun.project_id == project_id,
            ScanRun.id != run.id,
            ScanRun.status == RunStatus.COMPLETE,
            ScanRun.created_at <= run.created_at,
        )
        .order_by(ScanRun.created_at.desc(), ScanRun.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _run_diff_item_count(db: Session, run: ScanRun) -> int:
    # The summary is an ingest-time convenience value and can be stale on
    # legacy, repaired, or partially replayed runs. This count is an OOM guard,
    # so only authoritative persisted rows are safe to trust.
    return int(
        db.execute(select(func.count(Item.id)).where(Item.run_id == run.id, Item.deleted.is_(False))).scalar() or 0
    )


def _enforce_run_diff_item_limit(db: Session, current_run: ScanRun, baseline_run: ScanRun) -> int:
    settings = get_settings()
    total_items = _run_diff_item_count(db, current_run) + _run_diff_item_count(db, baseline_run)
    if total_items > settings.api_run_diff_max_items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"run diff covers {total_items} items, exceeding the synchronous limit of "
                f"{settings.api_run_diff_max_items}; reduce scan scope or provision a larger "
                "API_RUN_DIFF_MAX_ITEMS limit with sufficient API memory"
            ),
        )
    return total_items


def _require_complete_run_for_diff(run: ScanRun, label: str) -> None:
    status_value = run.status.value if hasattr(run.status, "value") else str(run.status)
    if status_value != RunStatus.COMPLETE.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"run diff requires a COMPLETE {label} run; {label} run status is {status_value}"),
        )


def _get_upload_auth_context(
    auth: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_RUNS)),
    db: Session = Depends(get_db),
) -> AuthContext:
    """Release the authentication transaction before streaming request bytes."""
    db.rollback()
    return auth


def _preflight_artifact_upload(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    auth: AuthContext,
) -> None:
    """Validate current authorization and run state in a short-lived session."""
    db = SessionLocal()
    try:
        require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
        run = _get_run(db, project_id, run_id)
        _require_uploadable_run(run)
    finally:
        _close_upload_session_quietly(db, run_id, "artifact upload preflight")


def _commit_uploaded_artifact(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    auth: AuthContext,
    key: str,
    size: int,
    digest: str,
    content_type: str,
    upload_ms: int,
    request_metadata: dict,
) -> uuid.UUID:
    """Select an uploaded object under the run lock and clean up safely.

    Before COMMIT starts, failures delete the unreferenced immutable object.
    Once COMMIT is attempted its outcome is ambiguous, so the object is kept
    for the database pointer or orphan reconciliation.
    """
    db = SessionLocal()
    commit_attempted = False
    previous_artifact_key: str | None = None
    authoritative_run_id = run_id
    try:
        # Serialize artifact-pointer changes with project deletion. Keep the
        # project-before-run ordering used by delete_project so concurrent
        # upload, deletion, and worker activity cannot form a lock cycle.
        lock_project_admin_guard(db, project_id)
        require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
        run = _get_run(db, project_id, run_id)
        if not _try_lock_run_for_mutation(db, run.id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run is currently ingesting")
        db.refresh(run)
        _require_uploadable_run(run)

        previous_status = run.status
        previous_artifact_key = run.artifact_key
        authoritative_run_id = run.id
        if previous_status == RunStatus.FAILED or previous_artifact_key is not None:
            _clear_run_ingest_data(db, run)

        run.artifact_key = key
        run.artifact_size = size
        run.artifact_sha256 = digest
        run.artifact_content_type = content_type
        run.status = RunStatus.UPLOADED
        run.ingest_progress = {"line_offset": 0}
        db.add(run)

        write_audit_event(
            db,
            action="ARTIFACT_UPLOADED",
            object_type="scan_run",
            object_id=str(run.id),
            actor_user_id=auth.user_id,
            actor_token_id=auth.token_id,
            project_id=project_id,
            metadata={
                **request_metadata,
                "size": size,
                "content_type": content_type,
                "upload_ms": upload_ms,
            },
        )
        commit_attempted = True
        db.commit()
    except BaseException:
        _rollback_upload_session_quietly(db, run_id, "artifact pointer")
        if not commit_attempted:
            _delete_artifact_quietly(key)
        raise
    finally:
        _close_upload_session_quietly(db, run_id, "artifact pointer")

    _delete_superseded_artifact(previous_artifact_key, key)
    return authoritative_run_id


def _write_enqueue_audit(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    auth: AuthContext,
    queued: bool,
    request_metadata: dict,
) -> None:
    db = SessionLocal()
    try:
        write_audit_event(
            db,
            action="INGEST_QUEUED" if queued else "INGEST_QUEUE_FALLBACK",
            object_type="scan_run",
            object_id=str(run_id),
            actor_user_id=auth.user_id,
            actor_token_id=auth.token_id,
            project_id=project_id,
            metadata=request_metadata if queued else {**request_metadata, "reason": "redis enqueue failed"},
        )
        db.commit()
    except BaseException:
        _rollback_upload_session_quietly(db, run_id, "ingest enqueue audit")
        raise
    finally:
        _close_upload_session_quietly(db, run_id, "ingest enqueue audit")


async def _run_critical_upload_step(func, *args):
    """Let a bounded durable step finish before propagating cancellation."""
    task = asyncio.create_task(run_in_threadpool(func, *args))
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
            continue

    # A durability error takes precedence over cancellation so the API and
    # logs retain the actual database failure. Cleanup is owned by the helper.
    result = task.result()
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


async def _enqueue_with_retries(payload: dict, retries: int) -> bool:
    for attempt in range(retries):
        try:
            await run_in_threadpool(enqueue_ingest_job, payload)
            return True
        except Exception:  # noqa: BLE001
            if attempt + 1 >= retries:
                return False
            await asyncio.sleep(min(2**attempt, 4))
    return False


async def _check_upload_rate_limit(request: Request, actor_key: str) -> None:
    await run_in_threadpool(
        rate_limiter.check,
        request,
        "artifact_upload",
        limit=30,
        window_seconds=60,
        actor_key=f"upload:{actor_key}",
    )


async def _run_cleanup_in_threadpool(func, *args) -> None:
    """Finish bounded storage cleanup even when the request task is cancelled."""
    cleanup_task = asyncio.create_task(run_in_threadpool(func, *args))
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
        except Exception:  # noqa: BLE001
            break
    try:
        cleanup_task.result()
    except BaseException:  # Cleanup must never mask the original failure/cancellation.
        logger.exception("artifact cleanup failed")


async def _upload_artifact_stream(
    request: Request,
    file: UploadFile | None,
    key: str,
    content_type: str | None,
) -> tuple[int, str]:
    settings = get_settings()
    chunk_bytes = settings.upload_chunk_bytes
    artifact_kind = _artifact_kind(content_type, key)

    upload_id = await run_in_threadpool(create_multipart_upload, key, content_type)
    sha256 = hashlib.sha256()
    size = 0
    part_number = 1
    parts: list[dict] = []
    buffer = bytearray()
    signature_buffer = bytearray()
    signature_checked = False

    def inspect_signature(chunk: bytes) -> None:
        nonlocal signature_checked
        if signature_checked:
            return
        remaining = ARTIFACT_SIGNATURE_SNIFF_BYTES - len(signature_buffer)
        if remaining > 0:
            signature_buffer.extend(chunk[:remaining])
        signature_checked = _validate_artifact_signature(artifact_kind, bytes(signature_buffer))
        if not signature_checked and len(signature_buffer) >= ARTIFACT_SIGNATURE_SNIFF_BYTES:
            _validate_artifact_signature(artifact_kind, bytes(signature_buffer), final=True)

    async def flush_parts(force: bool = False) -> None:
        nonlocal part_number
        while len(buffer) >= chunk_bytes or (force and buffer):
            if len(buffer) >= chunk_bytes:
                payload = bytes(buffer[:chunk_bytes])
                del buffer[:chunk_bytes]
            else:
                payload = bytes(buffer)
                buffer.clear()

            etag = await run_in_threadpool(upload_part, key, upload_id, part_number, payload)
            parts.append({"ETag": etag, "PartNumber": part_number})
            part_number += 1

    try:
        if file:
            while True:
                chunk = await run_in_threadpool(file.file.read, chunk_bytes)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.upload_max_bytes:
                    raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="upload too large")
                sha256.update(chunk)
                inspect_signature(chunk)
                buffer.extend(chunk)
                await flush_parts()
        else:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > settings.upload_max_bytes:
                    raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="upload too large")
                sha256.update(chunk)
                inspect_signature(chunk)
                buffer.extend(chunk)
                await flush_parts()

        if size == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty upload")
        if not signature_checked:
            _validate_artifact_signature(artifact_kind, bytes(signature_buffer), final=True)

        await flush_parts(force=True)
        await run_in_threadpool(complete_multipart_upload, key, upload_id, parts)
        return size, sha256.hexdigest()
    except BaseException:
        await _run_cleanup_in_threadpool(abort_multipart_upload, key, upload_id)
        raise


def _artifact_suffix(content_type: str | None, filename: str | None) -> str:
    lowered_content_type = _normalize_content_type(content_type)
    lowered_filename = (filename or "").lower()
    if lowered_filename.endswith(".json.gz"):
        return ".json.gz"
    if lowered_filename.endswith(".ndjson.gz") or lowered_filename.endswith(".jsonl.gz"):
        return ".ndjson.gz"
    if lowered_filename.endswith(".json"):
        return ".json"
    if lowered_filename.endswith(".ndjson") or lowered_filename.endswith(".jsonl"):
        return ".ndjson"

    is_gzip = "gzip" in lowered_content_type or lowered_filename.endswith(".gz")
    if "application/json" in lowered_content_type:
        return ".json.gz" if is_gzip else ".json"
    return ".ndjson.gz" if is_gzip else ".ndjson"


def _new_artifact_key(project_id: uuid.UUID, run_id: uuid.UUID, suffix: str) -> str:
    return f"projects/{project_id}/runs/{run_id}/artifact-{uuid.uuid4().hex}{suffix}"


def _normalize_content_type(content_type: str | None) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


def _raw_artifact_filename(request: Request) -> str | None:
    raw = request.headers.get(RAW_ARTIFACT_FILENAME_HEADER)
    if raw is None:
        return None
    filename = raw
    if (
        not filename
        or filename != filename.strip()
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid {RAW_ARTIFACT_FILENAME_HEADER} header",
        )
    return filename


def _artifact_kind(content_type: str | None, filename_or_key: str | None) -> str:
    lowered_content_type = _normalize_content_type(content_type)
    lowered_name = (filename_or_key or "").lower()
    if lowered_name.endswith(".json.gz") or lowered_name.endswith(".ndjson.gz") or lowered_name.endswith(".jsonl.gz"):
        return "gzip"
    if lowered_name.endswith(".ndjson") or lowered_name.endswith(".jsonl"):
        return "ndjson"
    if lowered_name.endswith(".json"):
        return "json"
    if lowered_content_type in {"application/gzip", "application/x-gzip"}:
        return "gzip"
    if lowered_content_type == "application/x-ndjson":
        return "ndjson"
    return "json"


def _validate_artifact_upload_headers(content_type: str | None, filename: str | None) -> None:
    normalized_content_type = _normalize_content_type(content_type)
    lowered_filename = (filename or "").lower()

    if normalized_content_type not in ALLOWED_ARTIFACT_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="unsupported artifact content type"
        )
    if lowered_filename and not any(lowered_filename.endswith(suffix) for suffix in ALLOWED_ARTIFACT_SUFFIXES):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="unsupported artifact filename")
    if not lowered_filename and normalized_content_type in {"", "application/octet-stream"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="content type required for raw artifact upload"
        )


def _validate_artifact_signature(kind: str, sample: bytes, final: bool = False) -> bool:
    if kind == "gzip":
        if len(sample) < 2:
            if final:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="artifact does not match gzip payload"
                )
            return False
        if not sample.startswith(b"\x1f\x8b"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="artifact does not match gzip payload"
            )
        return True

    stripped = sample.lstrip(b" \t\r\n")
    if not stripped:
        if final:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="artifact does not look like JSON",
            )
        return False
    if stripped[:1] not in {b"{", b"["}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="artifact does not look like JSON"
        )
    return True


def _write_read_audit(
    db: Session,
    request: Request,
    auth: AuthContext,
    project_id: uuid.UUID,
    action: str,
    object_type: str,
    object_id: str,
    metadata: dict | None = None,
) -> None:
    write_audit_event(
        db,
        action=action,
        object_type=object_type,
        object_id=object_id,
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), **(metadata or {})},
    )


@router.post("", response_model=RunOut)
def create_run(
    project_id: uuid.UUID,
    payload: RunCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    # Project deletion snapshots and locks the project's runs while holding
    # this guard. Taking it before authorization ensures the role/project
    # lookup is re-evaluated after any concurrent deletion commits.
    lock_project_admin_guard(db, project_id)
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)

    run = ScanRun(
        id=payload.run_id or uuid.uuid4(),
        project_id=project_id,
        name=payload.name,
        description=payload.description,
        target_scope=payload.target_scope,
        created_by_user_id=auth.user_id,
        created_by_token_id=auth.token_id,
        status=RunStatus.PENDING_UPLOAD,
    )
    db.add(run)

    write_audit_event(
        db,
        action="RUN_CREATED",
        object_type="scan_run",
        object_id=str(run.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata=request_meta(request),
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run already exists") from exc

    db.refresh(run)
    return _to_run_out(run)


@router.get("", response_model=dict)
def list_runs(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None, max_length=MAX_SEARCH_CHARS),
    run_status: RunStatus | None = Query(default=None, alias="status"),
    source_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)

    stmt = select(ScanRun).where(ScanRun.project_id == project_id)
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(
                ScanRun.name.ilike(pattern, escape="\\"),
                ScanRun.description.ilike(pattern, escape="\\"),
            )
        )
    if run_status is not None:
        stmt = stmt.where(ScanRun.status == run_status)
    if source_id is not None:
        stmt = stmt.where(ScanRun.source_id == source_id)
    stmt = apply_keyset_pagination(stmt, RUN_LIST_CURSOR, cursor, limit)
    runs, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), RUN_LIST_CURSOR, limit)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="RUNS_LISTED",
        object_type="project",
        object_id=str(project_id),
        metadata={
            "limit": limit,
            "cursor": cursor,
            "result_count": len(runs),
            "q": q,
            "status": run_status.value if run_status else None,
            "source_id": str(source_id) if source_id else None,
        },
    )
    db.commit()

    return {
        "items": [_to_run_out(r).model_dump(mode="json") for r in runs],
        "next_cursor": next_cursor,
    }


@router.get("/{run_id}", response_model=RunOut)
def get_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run = _get_run(db, project_id, run_id)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="RUN_VIEWED",
        object_type="scan_run",
        object_id=str(run_id),
    )
    db.commit()
    return _to_run_out(run)


@router.get("/{run_id}/errors", response_model=dict)
def list_run_errors(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    severity: ErrorSeverity | None = Query(default=None),
    search: str | None = Query(default=None, max_length=MAX_SEARCH_CHARS),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    stmt = select(IngestError).where(IngestError.run_id == run_id).order_by(IngestError.id.asc())
    if severity is not None:
        stmt = stmt.where(IngestError.severity == severity)
    if search:
        escaped = escape_like(search)
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                IngestError.code.ilike(pattern, escape="\\"),
                IngestError.message.ilike(pattern, escape="\\"),
                IngestError.endpoint_key.ilike(pattern, escape="\\"),
                IngestError.resource_name.ilike(pattern, escape="\\"),
                cast(IngestError.path, String).ilike(pattern, escape="\\"),
            )
        )

    stmt = apply_keyset_pagination(stmt, RUN_ERROR_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), RUN_ERROR_CURSOR, limit)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="RUN_ERRORS_LISTED",
        object_type="scan_run",
        object_id=str(run_id),
        metadata={
            "severity": severity.value if severity else None,
            "search": search,
            "limit": limit,
            "cursor": cursor,
            "result_count": len(rows),
        },
    )
    db.commit()

    return {
        "items": [IngestErrorOut.model_validate(row, from_attributes=True).model_dump(mode="json") for row in rows],
        "next_cursor": next_cursor,
    }


@router.get("/{run_id}/activity", response_model=dict)
def list_run_activity(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    stmt = (
        select(AuditEvent)
        .where(
            AuditEvent.project_id == project_id,
            AuditEvent.object_type == "scan_run",
            AuditEvent.object_id == str(run_id),
            AuditEvent.action.in_(RUN_ACTIVITY_ACTIONS),
        )
        .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
    )
    stmt = apply_keyset_pagination(stmt, RUN_ACTIVITY_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), RUN_ACTIVITY_CURSOR, limit)

    write_audit_event(
        db,
        action="RUN_ACTIVITY_LISTED",
        object_type="scan_run",
        object_id=str(run_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "limit": limit, "cursor": cursor, "result_count": len(rows)},
    )
    db.commit()

    return {
        "items": [
            RunActivityEventOut(
                id=row.id,
                ts=row.ts,
                action=row.action,
                object_type=row.object_type,
                object_id=row.object_id,
                metadata=row.metadata_json,
            ).model_dump(mode="json")
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{run_id}/diff", response_model=dict)
def diff_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    baseline_run_id: uuid.UUID | None = Query(default=None),
    detail_limit: int = Query(
        default=DEFAULT_RUN_DIFF_DETAIL_RECORDS,
        ge=1,
        le=MAX_RUN_DIFF_DETAIL_RECORDS,
    ),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS, SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    current_run = _get_run(db, project_id, run_id)
    _require_complete_run_for_diff(current_run, "current")
    baseline_run = (
        _get_run(db, project_id, baseline_run_id)
        if baseline_run_id
        else _default_baseline_run(db, project_id, current_run)
    )
    if baseline_run is not None:
        _require_complete_run_for_diff(baseline_run, "baseline")

    if baseline_run is None:
        _write_read_audit(
            db,
            request,
            auth,
            project_id,
            action="RUN_DIFF_VIEWED",
            object_type="scan_run",
            object_id=str(run_id),
            metadata={"baseline_run_id": None, "has_baseline": False},
        )
        db.commit()
        return {
            "current_run": _run_summary_payload(current_run),
            "baseline_run": None,
            "comparison_compatibility": _run_diff_compatibility(current_run, None),
            "summary": {
                "new_shares": 0,
                "disappeared_shares": 0,
                "changed_shares": 0,
                "added_items": 0,
                "removed_items": 0,
            },
            "new_shares": [],
            "disappeared_shares": [],
            "item_churn": [],
            "truncation": {
                "detail_limit": detail_limit,
                "truncated": False,
                "sections": {
                    "new_shares": False,
                    "disappeared_shares": False,
                    "item_churn": False,
                },
            },
        }

    compared_items = _enforce_run_diff_item_limit(db, current_run, baseline_run)
    payload = _build_run_diff_from_iters(
        _iter_run_diff_resources(db, current_run.id),
        _iter_run_diff_resources(db, baseline_run.id),
        detail_limit=detail_limit,
    )
    payload["current_run"] = _run_summary_payload(current_run)
    payload["baseline_run"] = _run_summary_payload(baseline_run)
    payload["comparison_compatibility"] = _strict_run_diff_compatibility(current_run, baseline_run)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="RUN_DIFF_VIEWED",
        object_type="scan_run",
        object_id=str(run_id),
        metadata={
            **payload["summary"],
            "baseline_run_id": str(baseline_run.id),
            "compared_items": compared_items,
            "detail_limit": detail_limit,
            "truncated": payload["truncation"]["truncated"],
        },
    )
    db.commit()
    return payload


@router.delete("/{run_id}")
def delete_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)
    run = _get_run(db, project_id, run_id)
    if not _try_lock_run_for_mutation(db, run.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run is currently ingesting")
    artifact_key = run.artifact_key
    db.execute(delete(ScanRun).where(ScanRun.id == run.id))
    write_audit_event(
        db,
        action="RUN_DELETED",
        object_type="scan_run",
        object_id=str(run.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata=request_meta(request),
    )
    db.commit()
    _delete_artifact_quietly(artifact_key)
    return {"ok": True}


@router.post("/{run_id}/artifact")
async def upload_artifact(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    file: UploadFile | None = File(default=None),
    auth: AuthContext = Depends(_get_upload_auth_context),
):
    await run_in_threadpool(_preflight_artifact_upload, project_id, run_id, auth)
    actor_key = str(auth.token_id or auth.user_id or "anon")
    await _check_upload_rate_limit(request, actor_key)

    settings = get_settings()
    content_type = _normalize_content_type(
        file.content_type if file else request.headers.get("content-type", "application/octet-stream")
    )
    filename = file.filename if file else _raw_artifact_filename(request)
    _validate_artifact_upload_headers(content_type, filename)
    suffix = _artifact_suffix(content_type, filename)
    key = _new_artifact_key(project_id, run_id, suffix)
    request_metadata = request_meta(request)
    started = time.perf_counter()
    size, digest = await _upload_artifact_stream(request, file, key, content_type)
    authoritative_run_id = await _run_critical_upload_step(
        _commit_uploaded_artifact,
        project_id,
        run_id,
        auth,
        key,
        size,
        digest,
        content_type,
        int((time.perf_counter() - started) * 1000),
        request_metadata,
    )

    payload = {
        "run_id": str(authoritative_run_id),
        "project_id": str(project_id),
        "artifact_key": key,
        "schema_version": 1,
        "uploaded_at": datetime.now(tz=UTC).isoformat(),
    }
    queued = await _enqueue_with_retries(payload, settings.redis_stream_retries)
    await _run_critical_upload_step(
        _write_enqueue_audit,
        project_id,
        authoritative_run_id,
        auth,
        queued,
        request_metadata,
    )

    return {
        "ok": True,
        "run_id": str(authoritative_run_id),
        "artifact_key": key,
        "artifact_sha256": digest,
        "queued": queued,
    }


@router.get("/{run_id}/endpoints")
def list_endpoints(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    search: str | None = Query(default=None, max_length=MAX_SEARCH_CHARS),
    provider: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    stmt = select(Endpoint).where(Endpoint.run_id == run_id)
    if provider:
        normalized_provider = provider.strip().lower()
        child_provider_match = (
            select(1)
            .select_from(Resource)
            .where(
                Resource.run_id == Endpoint.run_id,
                Resource.endpoint_id == Endpoint.id,
                _resource_provider_equals_expression(normalized_provider),
            )
            .correlate(Endpoint)
            .exists()
        )
        stmt = stmt.where(
            or_(
                Endpoint.provider == normalized_provider,
                child_provider_match,
            )
        )
    if search:
        escaped = escape_like(search)
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Endpoint.endpoint_key.ilike(pattern, escape="\\"),
                Endpoint.ip.ilike(pattern, escape="\\"),
                Endpoint.hostname.ilike(pattern, escape="\\"),
                Endpoint.domain.ilike(pattern, escape="\\"),
                Endpoint.provider_metadata["display_name"].astext.ilike(
                    pattern,
                    escape="\\",
                ),
                Endpoint.provider_metadata["site_name"].astext.ilike(
                    pattern,
                    escape="\\",
                ),
                Endpoint.provider_metadata["web_url"].astext.ilike(
                    pattern,
                    escape="\\",
                ),
            )
        )

    stmt = apply_keyset_pagination(stmt, RUN_ENDPOINT_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), RUN_ENDPOINT_CURSOR, limit)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="ENDPOINTS_LISTED",
        object_type="scan_run",
        object_id=str(run_id),
        metadata={
            "search": search,
            "provider": provider,
            "limit": limit,
            "cursor": cursor,
            "result_count": len(rows),
        },
    )
    db.commit()

    return {
        "items": [
            {
                "id": r.id,
                "endpoint_key": r.endpoint_key,
                "ip": r.ip,
                "hostname": r.hostname,
                "domain": r.domain,
                "smb_dialect": r.smb_dialect,
                "smb_signing": r.smb_signing,
                "auth_method": r.auth_method,
                "provider": getattr(r, "provider", None),
                "metadata": getattr(r, "provider_metadata", None) or {},
            }
            for r in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{run_id}/endpoints/{endpoint_id}/resources")
def endpoint_resources(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    endpoint_id: int,
    request: Request,
    provider: str | None = Query(default=None, max_length=32),
    resource_type: str | None = Query(default=None, max_length=64),
    exposure: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=200, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _require_endpoint(db, project_id, run_id, endpoint_id)

    stmt = select(Resource).where(Resource.run_id == run_id, Resource.endpoint_id == endpoint_id)
    if provider:
        stmt = stmt.where(_resource_provider_equals_expression(provider.strip().lower()))
    if resource_type:
        stmt = stmt.where(func.lower(cast(Resource.resource_type, String)) == resource_type.strip().lower())
    if exposure:
        stmt = stmt.where(Resource.exposure == exposure.strip().upper())
    stmt = apply_keyset_pagination(stmt, RUN_RESOURCE_CURSOR, cursor, limit)
    resources, next_cursor = paginate_rows(
        db.execute(stmt).scalars().all(),
        RUN_RESOURCE_CURSOR,
        limit,
    )

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="RESOURCES_LISTED",
        object_type="endpoint",
        object_id=str(endpoint_id),
        metadata={
            "run_id": str(run_id),
            "provider": provider,
            "resource_type": resource_type,
            "exposure": exposure,
            "limit": limit,
            "cursor": cursor,
            "result_count": len(resources),
        },
    )
    db.commit()

    return {
        "items": [
            {
                "id": r.id,
                "resource_type": r.resource_type.value if hasattr(r.resource_type, "value") else r.resource_type,
                "share_type": share_type_from_resource_type(r.resource_type),
                "name": r.name,
                "remark": r.remark,
                "access_level": r.access_level.value if hasattr(r.access_level, "value") else r.access_level,
                "access_capabilities": r.access_capabilities or {},
                "provider": getattr(r, "provider", None) or share_type_from_resource_type(r.resource_type),
                "provider_resource_id": getattr(r, "provider_resource_id", None),
                "web_url": getattr(r, "web_url", None),
                "metadata": getattr(r, "provider_metadata", None) or {},
                "exposure": getattr(r, "exposure", None),
                "exposure_evidence": getattr(r, "exposure_evidence", None) or {},
                "access_evidence_summary": build_access_evidence_summary(
                    getattr(r, "permission_summary", None),
                    r.access_level,
                    r.access_capabilities,
                    getattr(r, "exposure", None),
                ),
            }
            for r in resources
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{run_id}/resources/{resource_id}/access-evidence", response_model=AccessEvidenceOut)
def resource_access_evidence(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    resource_id: int,
    request: Request,
    assessment_limit: int = Query(
        default=ACCESS_EVIDENCE_ASSESSMENT_PAGE_MAX,
        ge=1,
        le=ACCESS_EVIDENCE_ASSESSMENT_PAGE_MAX,
    ),
    entry_limit: int = Query(
        default=ACCESS_EVIDENCE_ENTRY_PAGE_MAX,
        ge=1,
        le=ACCESS_EVIDENCE_ENTRY_PAGE_MAX,
    ),
    after_assessment_id: int | None = Query(default=None, ge=0),
    after_entry_id: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    _inventory_scope: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    resource = _require_resource(db, project_id, run_id, resource_id)
    run = _get_run(db, project_id, run_id)
    endpoint = db.execute(
        select(Endpoint).where(Endpoint.id == resource.endpoint_id, Endpoint.run_id == run_id)
    ).scalar_one_or_none()

    assessment_stmt = select(PermissionAssessment).where(
        PermissionAssessment.run_id == run_id,
        PermissionAssessment.resource_id == resource_id,
    )
    if after_assessment_id is not None:
        assessment_stmt = assessment_stmt.where(PermissionAssessment.id > after_assessment_id)
    assessments = (
        db.execute(assessment_stmt.order_by(PermissionAssessment.id).limit(assessment_limit + 1)).scalars().all()
    )
    assessments_truncated = len(assessments) > assessment_limit
    assessments = assessments[:assessment_limit]
    assessment_ids = [assessment.id for assessment in assessments]

    entry_rows: list[tuple[PermissionEntry, PermissionPrincipal | None]] = []
    if assessment_ids:
        entry_stmt = (
            select(PermissionEntry, PermissionPrincipal)
            .outerjoin(
                PermissionPrincipal,
                and_(
                    PermissionPrincipal.id == PermissionEntry.principal_id,
                    PermissionPrincipal.run_id == PermissionEntry.run_id,
                ),
            )
            .where(
                PermissionEntry.run_id == run_id,
                PermissionEntry.assessment_id.in_(assessment_ids),
            )
            .order_by(PermissionEntry.id)
            .limit(entry_limit + 1)
        )
        if after_entry_id is not None:
            entry_stmt = entry_stmt.where(PermissionEntry.id > after_entry_id)
        entry_rows = list(db.execute(entry_stmt).all())
    entries_truncated = len(entry_rows) > entry_limit
    visible_entry_rows = entry_rows[:entry_limit]
    entries_by_assessment: dict[int, list[dict]] = {assessment_id: [] for assessment_id in assessment_ids}
    for entry, principal in visible_entry_rows:
        principal_payload = None
        if principal is not None:
            principal_payload = {
                "principal_key": principal.principal_key,
                "provider": principal.provider,
                "identifier_namespace": principal.identifier_namespace,
                "authority": principal.authority,
                "native_id": principal.native_id,
                "kind": principal.kind,
                "display_name": principal.display_name,
                "login_name": principal.login_name,
                "email": principal.email,
                "resolution": principal.resolution_state,
                "resolution_source": principal.resolution_source,
                "aliases": principal.aliases or [],
            }
        entries_by_assessment.setdefault(entry.assessment_id, []).append(
            {
                "id": entry.id,
                "entry_key": entry.entry_key,
                "provider_entry_id": entry.provider_entry_id,
                "ordinal": entry.ordinal,
                "entry_kind": entry.entry_kind,
                "effect": entry.entry_effect,
                "normalized_rights": entry.normalized_rights or [],
                "inherited_state": entry.inherited_state,
                "expiration_at": entry.expiration_at.isoformat() if entry.expiration_at else None,
                "evidence_hash": entry.evidence_hash,
                "principal": principal_payload,
                "provider_details": entry.provider_details or {},
            }
        )

    assessment_payloads = [
        {
            "id": assessment.id,
            "assessment_key": assessment.assessment_key,
            "provider": assessment.provider,
            "semantics": assessment.semantics,
            "permission_surface": assessment.permission_surface,
            "method": assessment.method,
            "subject": {
                "kind": assessment.subject_kind,
                "key": assessment.subject_key,
                "provider_id": assessment.subject_provider_id,
                "path": assessment.subject_path,
                "item_id": assessment.item_id,
            },
            "assessment_state": assessment.assessment_state,
            "state": assessment.assessment_state,
            "coverage": {
                "selection_scope": assessment.selection_scope,
                "selection": assessment.selection_coverage,
                "retrieval": assessment.retrieval_coverage,
                "provider_visibility": assessment.provider_visibility,
                "semantic": assessment.semantic_coverage,
                "principal_resolution": assessment.principal_resolution,
                "effective_access": assessment.effective_access_status,
                "negative_conclusion_supported": assessment.negative_conclusion_supported,
            },
            "counts": {
                "observed": assessment.entries_observed,
                "emitted": assessment.entries_emitted,
                "omitted": assessment.entries_omitted,
                "unknown": assessment.unknown_entries,
            },
            "evidence_hash": assessment.evidence_hash,
            "entry_set_hash": assessment.entry_set_hash,
            "observed_at": assessment.observed_at.isoformat() if assessment.observed_at else None,
            "limitations": assessment.limitations or [],
            "error_code": assessment.error_code,
            "errors": assessment.errors or [],
            "summary": assessment.summary or {},
            "provider_details": assessment.provider_details or {},
            "entries": entries_by_assessment.get(assessment.id, []),
        }
        for assessment in assessments
    ]
    summary = build_access_evidence_summary(
        resource.permission_summary,
        resource.access_level,
        resource.access_capabilities,
        resource.exposure,
    )
    total_assessments = int(
        db.execute(
            select(func.count(PermissionAssessment.id)).where(
                PermissionAssessment.run_id == run_id,
                PermissionAssessment.resource_id == resource_id,
            )
        ).scalar()
        or 0
    )
    total_entries = int(
        db.execute(
            select(func.count(PermissionEntry.id))
            .join(
                PermissionAssessment,
                and_(
                    PermissionAssessment.id == PermissionEntry.assessment_id,
                    PermissionAssessment.run_id == PermissionEntry.run_id,
                ),
            )
            .where(
                PermissionEntry.run_id == run_id,
                PermissionAssessment.run_id == run_id,
                PermissionAssessment.resource_id == resource_id,
            )
        ).scalar()
        or 0
    )

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="ACCESS_EVIDENCE_VIEWED",
        object_type="resource",
        object_id=str(resource_id),
        metadata={
            "run_id": str(run_id),
            "assessment_count": total_assessments,
            "entry_count": total_entries,
        },
    )
    db.commit()
    return AccessEvidenceOut(
        resource={
            "id": resource.id,
            "name": resource.name,
            "resource_type": resource.resource_type.value
            if hasattr(resource.resource_type, "value")
            else str(resource.resource_type),
            "provider": resource.provider or share_type_from_resource_type(resource.resource_type),
            "provider_resource_id": resource.provider_resource_id,
            "endpoint_key": endpoint.endpoint_key if endpoint else None,
            "web_url": resource.web_url,
        },
        overall={
            **summary,
            "direct_assessment_available": total_assessments > 0,
            "assessment_count": total_assessments,
            "entry_count": total_entries,
            "access_level": resource.access_level.value
            if hasattr(resource.access_level, "value")
            else str(resource.access_level),
            "access_capabilities": resource.access_capabilities or {},
            "exposure": resource.exposure,
            "exposure_evidence": resource.exposure_evidence or {},
        },
        assessments=assessment_payloads,
        provenance={
            "run_id": str(run.id),
            "run_name": run.name,
            "run_created_at": run.created_at.isoformat() if run.created_at else None,
            "collection_context": run.collection_context or {},
            "pagination": {
                "assessment_limit": assessment_limit,
                "after_assessment_id": after_assessment_id,
                "assessments_truncated": assessments_truncated,
                "next_assessment_id": assessments[-1].id if assessments_truncated and assessments else None,
                "entry_limit": entry_limit,
                "after_entry_id": after_entry_id,
                "entries_truncated": entries_truncated,
                "next_entry_id": visible_entry_rows[-1][0].id if entries_truncated and visible_entry_rows else None,
            },
        },
    )


@router.get("/{run_id}/resources/{resource_id}/effective-access", response_model=dict)
def resource_effective_access(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    resource_id: int,
    request: Request,
    principal_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    _inventory_scope: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    """Explain current evidence without overstating effective authorization.

    Direct provider entries and observed capability probes are intentionally
    separate planes. Current collectors do not emit group membership graphs
    or a provider-computed effective decision, so ACL entries remain bounded
    evidence unless an assessment explicitly declares effective computation.
    """

    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    resource = _require_resource(db, project_id, run_id, resource_id)
    run = _get_run(db, project_id, run_id)
    endpoint = db.execute(
        select(Endpoint).where(Endpoint.id == resource.endpoint_id, Endpoint.run_id == run_id)
    ).scalar_one_or_none()

    assessment_rows = list(
        db.execute(
            select(PermissionAssessment)
            .where(
                PermissionAssessment.run_id == run_id,
                PermissionAssessment.resource_id == resource_id,
                PermissionAssessment.item_id.is_(None),
            )
            .order_by(PermissionAssessment.id)
            .limit(EFFECTIVE_ACCESS_ASSESSMENT_MAX + 1)
        ).scalars()
    )
    assessments_truncated = len(assessment_rows) > EFFECTIVE_ACCESS_ASSESSMENT_MAX
    assessments = assessment_rows[:EFFECTIVE_ACCESS_ASSESSMENT_MAX]
    assessment_ids = [assessment.id for assessment in assessments]
    principal_stmt = select(PermissionPrincipal).where(
        PermissionPrincipal.run_id == run_id,
        PermissionPrincipal.id.in_(
            select(PermissionEntry.principal_id).where(
                PermissionEntry.run_id == run_id,
                PermissionEntry.assessment_id.in_(assessment_ids or [-1]),
                PermissionEntry.principal_id.is_not(None),
            )
        ),
    )
    if principal_id is not None:
        principal_stmt = principal_stmt.where(PermissionPrincipal.id == principal_id)
    principal_stmt = apply_keyset_pagination(principal_stmt, EFFECTIVE_ACCESS_PRINCIPAL_CURSOR, cursor, limit)
    principals, next_cursor = paginate_rows(
        db.execute(principal_stmt).scalars().all(), EFFECTIVE_ACCESS_PRINCIPAL_CURSOR, limit
    )
    if principal_id is not None and not principals:
        raise HTTPException(status_code=404, detail="principal was not observed for this resource")

    principal_ids = [principal.id for principal in principals]
    entry_rows: list[PermissionEntry] = []
    entry_counts: dict[int, int] = {}
    active_entry_counts: dict[int, int] = {}
    expired_entry_counts: dict[int, int] = {}
    expiration_indeterminate_counts: dict[int, int] = {}
    entry_effects: dict[int, tuple[bool, bool]] = {}
    if principal_ids and assessment_ids:
        known_active_at_observation = or_(
            PermissionEntry.expiration_at.is_(None),
            and_(
                PermissionAssessment.observed_at.is_not(None),
                PermissionEntry.expiration_at > PermissionAssessment.observed_at,
            ),
        )
        known_expired_at_observation = and_(
            PermissionEntry.expiration_at.is_not(None),
            PermissionAssessment.observed_at.is_not(None),
            PermissionEntry.expiration_at <= PermissionAssessment.observed_at,
        )
        expiration_indeterminate = and_(
            PermissionEntry.expiration_at.is_not(None),
            PermissionAssessment.observed_at.is_(None),
        )
        entry_summary_rows = db.execute(
            select(
                PermissionEntry.principal_id,
                func.count(PermissionEntry.id),
                func.count(PermissionEntry.id).filter(known_active_at_observation),
                func.count(PermissionEntry.id).filter(known_expired_at_observation),
                func.count(PermissionEntry.id).filter(expiration_indeterminate),
                func.bool_or(PermissionEntry.entry_effect == "allow").filter(
                    known_active_at_observation
                ),
                func.bool_or(PermissionEntry.entry_effect == "deny").filter(
                    known_active_at_observation
                ),
            )
            .join(PermissionAssessment, PermissionAssessment.id == PermissionEntry.assessment_id)
            .where(
                PermissionEntry.run_id == run_id,
                PermissionEntry.assessment_id.in_(assessment_ids),
                PermissionEntry.principal_id.in_(principal_ids),
            )
            .group_by(PermissionEntry.principal_id)
        ).all()
        entry_counts = {int(row[0]): int(row[1]) for row in entry_summary_rows if row[0] is not None}
        active_entry_counts = {
            int(row[0]): int(row[2]) for row in entry_summary_rows if row[0] is not None
        }
        expired_entry_counts = {
            int(row[0]): int(row[3]) for row in entry_summary_rows if row[0] is not None
        }
        expiration_indeterminate_counts = {
            int(row[0]): int(row[4]) for row in entry_summary_rows if row[0] is not None
        }
        entry_effects = {
            int(row[0]): (bool(row[5]), bool(row[6])) for row in entry_summary_rows if row[0] is not None
        }
        ranked_entries = (
            select(
                PermissionEntry.id.label("entry_id"),
                func.row_number()
                .over(partition_by=PermissionEntry.principal_id, order_by=PermissionEntry.id)
                .label("principal_rank"),
            )
            .join(PermissionAssessment, PermissionAssessment.id == PermissionEntry.assessment_id)
            .where(
                PermissionEntry.run_id == run_id,
                PermissionEntry.assessment_id.in_(assessment_ids),
                PermissionEntry.principal_id.in_(principal_ids),
                known_active_at_observation,
            )
            .subquery()
        )
        entry_rows = list(
            db.execute(
                select(PermissionEntry)
                .join(ranked_entries, ranked_entries.c.entry_id == PermissionEntry.id)
                .where(ranked_entries.c.principal_rank <= EFFECTIVE_ACCESS_ENTRY_PRINCIPAL_MAX)
                .order_by(PermissionEntry.principal_id, PermissionEntry.id)
                .limit(EFFECTIVE_ACCESS_ENTRY_RESPONSE_MAX + 1)
            ).scalars()
        )
    response_entries_truncated = len(entry_rows) > EFFECTIVE_ACCESS_ENTRY_RESPONSE_MAX
    entry_rows = entry_rows[:EFFECTIVE_ACCESS_ENTRY_RESPONSE_MAX]
    entries_by_principal: dict[int, list[PermissionEntry]] = {principal.id: [] for principal in principals}
    for entry in entry_rows:
        if entry.principal_id is not None:
            entries = entries_by_principal.setdefault(entry.principal_id, [])
            if len(entries) < EFFECTIVE_ACCESS_ENTRY_PRINCIPAL_MAX:
                entries.append(entry)

    global_limitations: list[str] = []
    if not assessments:
        global_limitations.append("No normalized direct-permission assessment was collected for this resource.")
    if assessments_truncated:
        global_limitations.append(
            "Resource-level assessments exceeded the response analysis limit; effective conclusions are unknown."
        )
    if any(assessment.assessment_state != "complete" for assessment in assessments):
        global_limitations.append("At least one direct-permission assessment is incomplete.")
    if any(assessment.retrieval_coverage != "complete" for assessment in assessments):
        global_limitations.append("Provider permission retrieval was partial or indeterminate.")
    if any(assessment.entries_omitted > 0 or assessment.unknown_entries > 0 for assessment in assessments):
        global_limitations.append("Some provider permission entries were omitted or could not be normalized.")
    complete_resolution_values = {"complete", "resolved", "fully_resolved"}
    if any(assessment.principal_resolution not in complete_resolution_values for assessment in assessments):
        global_limitations.append("Group membership and principal resolution are not complete for this evidence.")
    if any(assessment.semantic_coverage not in {"effective_access", "effective_permissions"} for assessment in assessments):
        global_limitations.append("The provider evidence describes grants or ACL structure, not effective access.")
    if sum(expired_entry_counts.values()):
        global_limitations.append(
            "Known-expired permission entries were excluded using each assessment's observation timestamp."
        )
    if sum(expiration_indeterminate_counts.values()):
        global_limitations.append(
            "Some expiring entries lacked an assessment observation timestamp; their effect is indeterminate and excluded."
        )
    for assessment in assessments:
        global_limitations.extend(str(item) for item in (assessment.limitations or []) if str(item).strip())
    global_limitations = list(dict.fromkeys(global_limitations))

    provider_computed_decision, provider_assessment = _provider_computed_effective_decision(
        assessments,
        assessments_truncated=assessments_truncated,
        principal_filtered=principal_id is not None,
    )
    provider_computed_subject: dict[str, Any] | None = None
    if provider_assessment is not None:
        provider_computed_subject = {
            "assessment_id": provider_assessment.id,
            "subject_kind": provider_assessment.subject_kind,
            "subject_key": provider_assessment.subject_key,
            "subject_provider_id": provider_assessment.subject_provider_id,
            "subject_path": provider_assessment.subject_path,
            "provider": provider_assessment.provider,
            "method": provider_assessment.method,
            "observed_at": (
                provider_assessment.observed_at.isoformat() if provider_assessment.observed_at else None
            ),
        }

    principal_payloads: list[dict] = []
    any_direct = False
    for principal in principals:
        entries = entries_by_principal.get(principal.id, [])
        allow_rights = sorted(
            {str(right) for entry in entries if entry.entry_effect == "allow" for right in (entry.normalized_rights or [])}
        )
        deny_rights = sorted(
            {str(right) for entry in entries if entry.entry_effect == "deny" for right in (entry.normalized_rights or [])}
        )
        allow_observed, deny_observed = entry_effects.get(principal.id, (False, False))
        any_direct = any_direct or active_entry_counts.get(principal.id, 0) > 0
        if allow_observed and deny_observed:
            direct_decision = "mixed"
        elif allow_observed:
            direct_decision = "allow_evidence"
        elif deny_observed:
            direct_decision = "deny_evidence"
        else:
            direct_decision = "unknown"
        principal_limitations = list(global_limitations)
        if principal.kind in {"group", "sharepoint_group", "security_group", "distribution_group"}:
            principal_limitations.append("Group membership was not expanded; member effective access is unknown.")
        if principal.resolution_state not in complete_resolution_values:
            principal_limitations.append("The principal was not fully resolved to a current identity object.")
        rights_truncated = active_entry_counts.get(principal.id, len(entries)) > len(entries)
        if rights_truncated:
            principal_limitations.append("Displayed rights are truncated; effect summary uses all matching entries.")
        effective_decision = "unknown"
        principal_payloads.append(
            {
                "id": principal.id,
                "principal_key": principal.principal_key,
                "provider": principal.provider,
                "identifier_namespace": principal.identifier_namespace,
                "authority": principal.authority,
                "native_id": principal.native_id,
                "kind": principal.kind,
                "display_name": principal.display_name,
                "login_name": principal.login_name,
                "email": principal.email,
                "resolution": principal.resolution_state,
                "resolution_source": principal.resolution_source,
                "direct_decision": direct_decision,
                "effective_decision": effective_decision,
                "allow_rights": allow_rights,
                "deny_rights": deny_rights,
                "rights_truncated": rights_truncated,
                "entries": [
                    {
                        "id": entry.id,
                        "assessment_id": entry.assessment_id,
                        "effect": entry.entry_effect,
                        "rights": entry.normalized_rights or [],
                        "inherited_state": entry.inherited_state,
                        "expiration_at": entry.expiration_at.isoformat() if entry.expiration_at else None,
                        "evidence_hash": entry.evidence_hash,
                    }
                    for entry in entries
                ],
                "entry_count": entry_counts.get(principal.id, len(entries)),
                "active_entry_count": active_entry_counts.get(principal.id, len(entries)),
                "expired_entry_count": expired_entry_counts.get(principal.id, 0),
                "expiration_indeterminate_entry_count": expiration_indeterminate_counts.get(
                    principal.id, 0
                ),
                "entries_returned": len(entries),
                "entries_truncated": active_entry_counts.get(principal.id, len(entries)) > len(entries),
                "limitations": list(dict.fromkeys(principal_limitations)),
            }
        )

    observed = build_access_evidence_summary(
        resource.permission_summary,
        resource.access_level,
        resource.access_capabilities,
        resource.exposure,
    )["capability_observations"]
    observed_decision = "unknown"
    if observed.get("allowed") and observed.get("denied"):
        observed_decision = "mixed_observations"
    elif observed.get("allowed"):
        observed_decision = "allow_observed"
    elif observed.get("denied"):
        observed_decision = "deny_observed"
    # A keyset page of principals cannot establish a resource-wide effective
    # decision. Preserve unknown until complete, fresh identity-membership and
    # inheritance evidence can be proven.
    decision = provider_computed_decision
    analysis_state = (
        "computed"
        if provider_computed_decision != "unknown"
        else ("bounded" if any_direct or observed.get("evidence_available") else "indeterminate")
    )
    page_decision = "unknown"

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="EFFECTIVE_ACCESS_ANALYZED",
        object_type="resource",
        object_id=str(resource_id),
        metadata={
            "run_id": str(run_id),
            "principal_id": principal_id,
            "principal_count": len(principals),
            "analysis_state": analysis_state,
            "assessments_truncated": assessments_truncated,
            "entries_returned": sum(len(entries) for entries in entries_by_principal.values()),
            "entries_truncated": response_entries_truncated,
        },
    )
    db.commit()
    return {
        "resource": {
            "id": resource.id,
            "name": resource.name,
            "resource_type": resource.resource_type.value
            if hasattr(resource.resource_type, "value")
            else str(resource.resource_type),
            "provider": resource.provider or share_type_from_resource_type(resource.resource_type),
            "provider_resource_id": resource.provider_resource_id,
            "endpoint_key": endpoint.endpoint_key if endpoint else None,
            "web_url": resource.web_url,
        },
        "analysis_state": analysis_state,
        "decision": decision,
        "capabilities": resource.access_capabilities or {},
        "principals": {
            "items": principal_payloads,
            "next_cursor": next_cursor,
            "page_decision": page_decision,
            "page_decision_scope": "returned_principals_only",
        },
        "evidence_planes": {
            "direct_provider": {
                "state": "available" if assessments else "not_assessed",
                "assessment_count": len(assessments),
                "assessments_truncated": assessments_truncated,
                "assessment_limit": EFFECTIVE_ACCESS_ASSESSMENT_MAX,
                "page_entry_count": sum(entry_counts.values()),
                "page_active_entry_count": sum(active_entry_counts.values()),
                "page_expired_entry_count": sum(expired_entry_counts.values()),
                "page_expiration_indeterminate_entry_count": sum(
                    expiration_indeterminate_counts.values()
                ),
                "entries_returned": sum(len(entries) for entries in entries_by_principal.values()),
                "entries_truncated": response_entries_truncated
                or sum(active_entry_counts.values())
                > sum(len(entries) for entries in entries_by_principal.values()),
                "entry_response_limit": EFFECTIVE_ACCESS_ENTRY_RESPONSE_MAX,
                "entry_per_principal_limit": EFFECTIVE_ACCESS_ENTRY_PRINCIPAL_MAX,
                "interpretation": "provider grants or ACL structure; not automatically effective access",
            },
            "observed_capability": {
                **observed,
                "decision": observed_decision,
                "subject_scope": "assessed_identity_only",
            },
            "computed_effective": {
                "state": "provider_computed" if provider_computed_decision != "unknown" else "not_computed",
                "decision": decision,
                "decision_scope": "single_declared_assessment_subject"
                if provider_computed_decision != "unknown"
                else "unknown",
                "subject": provider_computed_subject,
                "qualifying_assessment_count": int(provider_assessment is not None),
                "reason": None
                if provider_computed_decision != "unknown"
                else (
                    "no single complete assessment supplied a recognized provider-computed effective decision; "
                    "complete group membership and inheritance evidence is unavailable"
                ),
            },
        },
        "limitations": global_limitations,
        "provenance": {
            "run_id": str(run.id),
            "run_created_at": run.created_at.isoformat() if run.created_at else None,
            "collection_context": run.collection_context or {},
        },
    }


@router.get("/{run_id}/resources/{resource_id}/items")
def resource_items(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    resource_id: int,
    request: Request,
    search: str | None = Query(default=None, max_length=MAX_SEARCH_CHARS),
    path_prefix: str | None = Query(default=None, max_length=MAX_PATH_PREFIX_CHARS),
    provider: str | None = Query(default=None, max_length=32),
    exposure: str | None = Query(default=None, max_length=32),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    resource = _require_resource(db, project_id, run_id, resource_id)
    resource_provider = getattr(resource, "provider", None) or share_type_from_resource_type(resource.resource_type)

    stmt = select(Item).where(Item.run_id == run_id, Item.resource_id == resource_id)
    if not include_deleted:
        stmt = stmt.where(Item.deleted.is_(False))
    if provider:
        stmt = stmt.where(_item_provider_equals_expression(provider.strip().lower()))
    if exposure:
        stmt = stmt.where(Item.exposure == exposure.strip().upper())
    if search:
        escaped = escape_like(search)
        stmt = stmt.where(Item.name.ilike(f"%{escaped}%", escape="\\"))
    if path_prefix:
        escaped = escape_like(path_prefix)
        stmt = stmt.where(Item.path.ilike(f"{escaped}%", escape="\\"))

    stmt = apply_keyset_pagination(stmt, RUN_ITEM_CURSOR, cursor, limit)
    items, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), RUN_ITEM_CURSOR, limit)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="ITEMS_LISTED",
        object_type="resource",
        object_id=str(resource_id),
        metadata={
            "run_id": str(run_id),
            "search": search,
            "path_prefix": path_prefix,
            "provider": provider,
            "exposure": exposure,
            "include_deleted": include_deleted,
            "limit": limit,
            "cursor": cursor,
            "result_count": len(items),
        },
    )
    db.commit()

    return {
        "items": [
            {
                "id": i.id,
                "path": i.path,
                "name": i.name,
                "is_dir": i.is_dir,
                "size_bytes": i.size_bytes,
                "allocation_size_bytes": i.allocation_size_bytes,
                "mtime": i.mtime.isoformat() if i.mtime else None,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "accessed_at": i.accessed_at.isoformat() if i.accessed_at else None,
                "changed_at": i.changed_at.isoformat() if i.changed_at else None,
                "file_attributes": i.file_attributes or [],
                "provider": getattr(i, "provider", None) or resource_provider,
                "provider_item_id": getattr(i, "provider_item_id", None),
                "provider_parent_id": getattr(i, "provider_parent_id", None),
                "web_url": getattr(i, "web_url", None),
                "mime_type": getattr(i, "mime_type", None),
                "deleted": bool(getattr(i, "deleted", False)),
                "metadata": getattr(i, "provider_metadata", None) or {},
                "exposure": getattr(i, "exposure", None),
                "exposure_evidence": getattr(i, "exposure_evidence", None) or {},
            }
            for i in items
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{run_id}/search/items")
def search_items(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None, max_length=MAX_SEARCH_CHARS),
    ext: str | None = Query(default=None, max_length=MAX_SEARCH_CHARS),
    provider: str | None = Query(default=None, max_length=32),
    exposure: str | None = Query(default=None, max_length=32),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    effective_provider = func.coalesce(
        Item.provider,
        Resource.provider,
        _provider_from_resource_type_expression(),
    )
    stmt = (
        select(
            Item.id,
            Item.resource_id,
            Item.path,
            Item.name,
            Item.is_dir,
            Item.size_bytes,
            Item.allocation_size_bytes,
            Item.mtime,
            Item.created_at,
            Item.accessed_at,
            Item.changed_at,
            Item.file_attributes,
            effective_provider.label("provider"),
            Item.provider_item_id,
            Item.provider_parent_id,
            Item.web_url,
            Item.mime_type,
            Item.deleted,
            Item.provider_metadata,
            Item.exposure,
            Item.exposure_evidence,
            Resource.name.label("resource_name"),
            Resource.resource_type,
            Resource.provider_resource_id,
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Endpoint.provider_metadata.label("endpoint_metadata"),
        )
        .select_from(Item)
        .join(
            Resource,
            (Resource.id == Item.resource_id) & (Resource.run_id == Item.run_id),
        )
        .join(
            Endpoint,
            (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id),
        )
        .where(Item.run_id == run_id)
    )
    if not include_deleted:
        stmt = stmt.where(Item.deleted.is_(False))
    if provider:
        normalized_provider = provider.strip().lower()
        stmt = stmt.where(
            or_(
                Item.provider == normalized_provider,
                and_(
                    Item.provider.is_(None),
                    _resource_provider_equals_expression(normalized_provider),
                ),
            )
        )
    if exposure:
        stmt = stmt.where(Item.exposure == exposure.strip().upper())
    if q:
        escaped = escape_like(q)
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Item.name.ilike(pattern, escape="\\"),
                cast(Item.path, String).ilike(pattern, escape="\\"),
                Item.provider_item_id.ilike(pattern, escape="\\"),
                Item.web_url.ilike(pattern, escape="\\"),
                Resource.name.ilike(pattern, escape="\\"),
                Resource.provider_resource_id.ilike(pattern, escape="\\"),
                Endpoint.endpoint_key.ilike(pattern, escape="\\"),
                Endpoint.provider_metadata["display_name"].astext.ilike(
                    pattern,
                    escape="\\",
                ),
                Endpoint.provider_metadata["site_name"].astext.ilike(
                    pattern,
                    escape="\\",
                ),
            )
        )
    if ext:
        ext = ext if ext.startswith(".") else f".{ext}"
        stmt = stmt.where(func.lower(Item.name).like(f"%{escape_like(ext.lower())}", escape="\\"))

    stmt = apply_keyset_pagination(stmt, RUN_ITEM_CURSOR, cursor, limit)
    items, next_cursor = paginate_rows(db.execute(stmt).all(), RUN_ITEM_CURSOR, limit)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="ITEMS_SEARCHED",
        object_type="scan_run",
        object_id=str(run_id),
        metadata={
            "q": q,
            "ext": ext,
            "provider": provider,
            "exposure": exposure,
            "include_deleted": include_deleted,
            "limit": limit,
            "cursor": cursor,
            "result_count": len(items),
        },
    )
    db.commit()

    return {
        "items": [
            {
                "id": i.id,
                "resource_id": i.resource_id,
                "resource_name": i.resource_name,
                "resource_type": i.resource_type.value if hasattr(i.resource_type, "value") else i.resource_type,
                "share_type": share_type_from_resource_type(i.resource_type),
                "provider_resource_id": getattr(i, "provider_resource_id", None),
                "endpoint_key": i.endpoint_key,
                "hostname": i.hostname,
                "endpoint_metadata": getattr(i, "endpoint_metadata", None) or {},
                "path": i.path,
                "name": i.name,
                "is_dir": i.is_dir,
                "size_bytes": i.size_bytes,
                "allocation_size_bytes": i.allocation_size_bytes,
                "mtime": i.mtime.isoformat() if i.mtime else None,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "accessed_at": i.accessed_at.isoformat() if i.accessed_at else None,
                "changed_at": i.changed_at.isoformat() if i.changed_at else None,
                "file_attributes": i.file_attributes or [],
                "provider": getattr(i, "provider", None),
                "provider_item_id": getattr(i, "provider_item_id", None),
                "provider_parent_id": getattr(i, "provider_parent_id", None),
                "web_url": getattr(i, "web_url", None),
                "mime_type": getattr(i, "mime_type", None),
                "deleted": bool(getattr(i, "deleted", False)),
                "metadata": getattr(i, "provider_metadata", None) or {},
                "exposure": getattr(i, "exposure", None),
                "exposure_evidence": getattr(i, "exposure_evidence", None) or {},
            }
            for i in items
        ],
        "next_cursor": next_cursor,
    }
