import asyncio
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import escape_like, get_db
from app.deps import AuthContext, get_auth_context, require_project_role, request_meta, require_token_scopes
from app.enums import ErrorSeverity, ProjectRole, RunStatus
from app.models import AuditEvent, Endpoint, IngestError, Item, Resource, ScanRun
from app.pagination import (
    KeysetColumn,
    apply_keyset_pagination,
    paginate_rows,
    parse_datetime_cursor_value,
    parse_int_cursor_value,
    parse_uuid_cursor_value,
)
from app.rate_limit import RateLimiter
from app.schemas import IngestErrorOut, RunActivityEventOut, RunCreateIn, RunOut
from app.share_types import share_type_from_resource_type
from app.services.audit import write_audit_event
from app.services.queue import enqueue_ingest_job
from app.services.storage import (
    abort_multipart_upload,
    complete_multipart_upload,
    create_multipart_upload,
    delete_object,
    upload_part,
)
from app.token_scopes import SCOPE_READ_RUNS, SCOPE_WRITE_RUNS

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
RUN_LIST_CURSOR = (
    KeysetColumn("created_at", ScanRun.created_at, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", ScanRun.id, direction="desc", parser=parse_uuid_cursor_value),
)
RUN_ENDPOINT_CURSOR = (KeysetColumn("id", Endpoint.id, parser=parse_int_cursor_value),)
RUN_ITEM_CURSOR = (KeysetColumn("id", Item.id, parser=parse_int_cursor_value),)
RUN_ERROR_CURSOR = (KeysetColumn("id", IngestError.id, parser=parse_int_cursor_value),)
RUN_ACTIVITY_CURSOR = (
    KeysetColumn("ts", AuditEvent.ts, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", AuditEvent.id, direction="desc"),
)
RUN_ACTIVITY_ACTIONS = {
    "RUN_CREATED",
    "ARTIFACT_UPLOADED",
    "INGEST_QUEUED",
    "INGEST_QUEUE_FALLBACK",
    "INGEST_STARTED",
    "INGEST_COMPLETED",
    "INGEST_FAILED",
}
EMPTY_RUN_SUMMARY = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}


def _get_run(db: Session, project_id: uuid.UUID, run_id: uuid.UUID) -> ScanRun:
    stmt = select(ScanRun).where(ScanRun.id == run_id, ScanRun.project_id == project_id)
    run = db.execute(stmt).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


def _to_run_out(run: ScanRun) -> RunOut:
    return RunOut(
        id=run.id,
        project_id=run.project_id,
        name=run.name,
        description=run.description,
        target_scope=run.target_scope,
        created_at=run.created_at,
        status=run.status,
        artifact_size=run.artifact_size,
        ingest_progress=run.ingest_progress,
        summary=run.summary,
    )


def _run_summary_payload(run: ScanRun) -> dict:
    return {
        "id": str(run.id),
        "name": run.name,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "status": run.status.value if hasattr(run.status, "value") else run.status,
    }


def _clear_run_ingest_data(db: Session, run: ScanRun) -> None:
    db.execute(delete(Item).where(Item.run_id == run.id))
    db.execute(delete(Resource).where(Resource.run_id == run.id))
    db.execute(delete(Endpoint).where(Endpoint.run_id == run.id))
    db.execute(delete(IngestError).where(IngestError.run_id == run.id))
    run.summary = dict(EMPTY_RUN_SUMMARY)
    run.ingest_progress = {"line_offset": 0}


def _delete_artifact_quietly(key: str | None) -> None:
    if not key:
        return
    try:
        delete_object(key)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning("failed to delete artifact key=%s", key, exc_info=True)


def _resource_identity(endpoint_key: str, resource_type: str, share_name: str) -> tuple[str, str, str]:
    return (endpoint_key or "", resource_type or "", share_name or "")


def _sorted_resource_keys(keys: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return sorted(keys, key=lambda value: (value[0].lower(), value[2].lower(), value[1].lower()))


def _normalized_resource_key(key: tuple[str, str, str]) -> tuple[str, str, str]:
    return (key[0].lower(), key[2].lower(), key[1].lower())


def _resource_diff_record(
    endpoint_key: str,
    hostname: str | None,
    ip: str | None,
    share_name: str,
    resource_type: str,
    access_level: str | None,
    item_paths: set[str] | None = None,
) -> dict:
    return {
        "endpoint_key": endpoint_key,
        "hostname": hostname,
        "ip": ip,
        "share_name": share_name,
        "resource_type": resource_type,
        "share_type": share_type_from_resource_type(resource_type),
        "access_level": access_level,
        "item_paths": item_paths or set(),
    }


def _load_run_diff_snapshot(db: Session, run_id: uuid.UUID) -> dict[tuple[str, str, str], dict]:
    resource_rows = db.execute(
        select(
            Resource.name,
            Resource.access_level,
            Resource.resource_type,
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Endpoint.ip,
        )
        .select_from(Resource)
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .where(Resource.run_id == run_id)
    ).all()

    snapshot: dict[tuple[str, str, str], dict] = {}
    for row in resource_rows:
        resource_type = row.resource_type.value if hasattr(row.resource_type, "value") else str(row.resource_type)
        access_level = row.access_level.value if hasattr(row.access_level, "value") else row.access_level
        identity = _resource_identity(row.endpoint_key, resource_type, row.name)
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

    item_rows = db.execute(
        select(
            Resource.name,
            Resource.resource_type,
            Endpoint.endpoint_key,
            Item.path,
        )
        .select_from(Item)
        .join(Resource, (Resource.id == Item.resource_id) & (Resource.run_id == Item.run_id))
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .where(Item.run_id == run_id)
    ).all()

    for row in item_rows:
        resource_type = row.resource_type.value if hasattr(row.resource_type, "value") else str(row.resource_type)
        identity = _resource_identity(row.endpoint_key, resource_type, row.name)
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
            snapshot[identity] = record
        record["item_paths"].add(row.path)

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
            Item.path,
        )
        .select_from(Resource)
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .outerjoin(Item, (Item.resource_id == Resource.id) & (Item.run_id == Resource.run_id))
        .where(Resource.run_id == run_id)
        .order_by(
            func.lower(Endpoint.endpoint_key),
            func.lower(cast(Resource.name, String)),
            func.lower(cast(Resource.resource_type, String)),
            Item.path.asc().nulls_last(),
        )
    )

    current_key: tuple[str, str, str] | None = None
    current_record: dict | None = None
    for row in db.execute(stmt):
        resource_type = row.resource_type.value if hasattr(row.resource_type, "value") else str(row.resource_type)
        access_level = row.access_level.value if hasattr(row.access_level, "value") else row.access_level
        key = _resource_identity(row.endpoint_key, resource_type, row.name)
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
            )
        if row.path is not None and current_record is not None:
            current_record["item_paths"].add(row.path)

    if current_key is not None and current_record is not None:
        yield current_key, current_record


def _next_resource_record(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def _serialize_diff_resource(resource: dict) -> dict:
    return {
        "endpoint_key": resource["endpoint_key"],
        "hostname": resource["hostname"],
        "ip": resource["ip"],
        "share_name": resource["share_name"],
        "share_type": resource["share_type"],
        "access_level": resource["access_level"],
        "item_count": len(resource["item_paths"]),
    }


def _build_run_diff_from_iters(current_iter, baseline_iter, example_limit: int = 5) -> dict:
    current_iter = iter(current_iter)
    baseline_iter = iter(baseline_iter)
    current_entry = _next_resource_record(current_iter)
    baseline_entry = _next_resource_record(baseline_iter)

    new_shares = []
    disappeared_shares = []
    item_churn = []

    while current_entry is not None or baseline_entry is not None:
        if baseline_entry is None:
            _current_key, current_resource = current_entry
            new_shares.append(_serialize_diff_resource(current_resource))
            current_entry = _next_resource_record(current_iter)
            continue

        if current_entry is None:
            _baseline_key, baseline_resource = baseline_entry
            disappeared_shares.append(_serialize_diff_resource(baseline_resource))
            baseline_entry = _next_resource_record(baseline_iter)
            continue

        current_key, current_resource = current_entry
        baseline_key, baseline_resource = baseline_entry
        if _normalized_resource_key(current_key) < _normalized_resource_key(baseline_key):
            new_shares.append(_serialize_diff_resource(current_resource))
            current_entry = _next_resource_record(current_iter)
            continue

        if _normalized_resource_key(current_key) > _normalized_resource_key(baseline_key):
            disappeared_shares.append(_serialize_diff_resource(baseline_resource))
            baseline_entry = _next_resource_record(baseline_iter)
            continue

        added_paths = sorted(current_resource["item_paths"] - baseline_resource["item_paths"])
        removed_paths = sorted(baseline_resource["item_paths"] - current_resource["item_paths"])
        if added_paths or removed_paths:
            item_churn.append(
                {
                    "endpoint_key": current_resource["endpoint_key"],
                    "hostname": current_resource["hostname"] or baseline_resource["hostname"],
                    "ip": current_resource["ip"] or baseline_resource["ip"],
                    "share_name": current_resource["share_name"],
                    "share_type": current_resource["share_type"],
                    "access_level": current_resource["access_level"] or baseline_resource["access_level"],
                    "added_items": len(added_paths),
                    "removed_items": len(removed_paths),
                    "added_examples": added_paths[:example_limit],
                    "removed_examples": removed_paths[:example_limit],
                }
            )

        current_entry = _next_resource_record(current_iter)
        baseline_entry = _next_resource_record(baseline_iter)

    item_churn.sort(
        key=lambda item: (
            -(item["added_items"] + item["removed_items"]),
            item["endpoint_key"].lower(),
            item["share_name"].lower(),
        )
    )

    return {
        "summary": {
            "new_shares": len(new_shares),
            "disappeared_shares": len(disappeared_shares),
            "changed_shares": len(item_churn),
            "added_items": sum(item["added_items"] for item in item_churn),
            "removed_items": sum(item["removed_items"] for item in item_churn),
        },
        "new_shares": new_shares,
        "disappeared_shares": disappeared_shares,
        "item_churn": item_churn,
    }


def _build_run_diff(current_snapshot: dict[tuple[str, str, str], dict], baseline_snapshot: dict[tuple[str, str, str], dict], example_limit: int = 5) -> dict:
    current_keys = set(current_snapshot)
    baseline_keys = set(baseline_snapshot)

    new_shares = []
    for key in _sorted_resource_keys(current_keys - baseline_keys):
        resource = current_snapshot[key]
        new_shares.append(
            {
                "endpoint_key": resource["endpoint_key"],
                "hostname": resource["hostname"],
                "ip": resource["ip"],
                "share_name": resource["share_name"],
                "share_type": resource["share_type"],
                "access_level": resource["access_level"],
                "item_count": len(resource["item_paths"]),
            }
        )

    disappeared_shares = []
    for key in _sorted_resource_keys(baseline_keys - current_keys):
        resource = baseline_snapshot[key]
        disappeared_shares.append(
            {
                "endpoint_key": resource["endpoint_key"],
                "hostname": resource["hostname"],
                "ip": resource["ip"],
                "share_name": resource["share_name"],
                "share_type": resource["share_type"],
                "access_level": resource["access_level"],
                "item_count": len(resource["item_paths"]),
            }
        )

    item_churn = []
    for key in _sorted_resource_keys(current_keys & baseline_keys):
        current_resource = current_snapshot[key]
        baseline_resource = baseline_snapshot[key]
        added_paths = sorted(current_resource["item_paths"] - baseline_resource["item_paths"])
        removed_paths = sorted(baseline_resource["item_paths"] - current_resource["item_paths"])
        if not added_paths and not removed_paths:
            continue
        item_churn.append(
            {
                "endpoint_key": current_resource["endpoint_key"],
                "hostname": current_resource["hostname"] or baseline_resource["hostname"],
                "ip": current_resource["ip"] or baseline_resource["ip"],
                "share_name": current_resource["share_name"],
                "share_type": current_resource["share_type"],
                "access_level": current_resource["access_level"] or baseline_resource["access_level"],
                "added_items": len(added_paths),
                "removed_items": len(removed_paths),
                "added_examples": added_paths[:example_limit],
                "removed_examples": removed_paths[:example_limit],
            }
        )

    item_churn.sort(
        key=lambda item: (
            -(item["added_items"] + item["removed_items"]),
            item["endpoint_key"].lower(),
            item["share_name"].lower(),
        )
    )

    return {
        "summary": {
            "new_shares": len(new_shares),
            "disappeared_shares": len(disappeared_shares),
            "changed_shares": len(item_churn),
            "added_items": sum(item["added_items"] for item in item_churn),
            "removed_items": sum(item["removed_items"] for item in item_churn),
        },
        "new_shares": new_shares,
        "disappeared_shares": disappeared_shares,
        "item_churn": item_churn,
    }


def _default_baseline_run(db: Session, project_id: uuid.UUID, run: ScanRun) -> ScanRun | None:
    stmt = (
        select(ScanRun)
        .where(
            ScanRun.project_id == project_id,
            ScanRun.id != run.id,
            ScanRun.status == RunStatus.COMPLETE,
            ScanRun.created_at <= run.created_at,
        )
        .order_by(ScanRun.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


async def _enqueue_with_retries(payload: dict, retries: int) -> bool:
    for attempt in range(retries):
        try:
            enqueue_ingest_job(payload)
            return True
        except Exception:  # noqa: BLE001
            if attempt + 1 >= retries:
                return False
            await asyncio.sleep(min(2**attempt, 4))
    return False


async def _upload_artifact_stream(
    request: Request,
    file: UploadFile | None,
    key: str,
    content_type: str | None,
) -> tuple[int, str]:
    settings = get_settings()
    chunk_bytes = max(5 * 1024 * 1024, settings.upload_chunk_bytes)
    artifact_kind = _artifact_kind(content_type, key)

    upload_id = await run_in_threadpool(create_multipart_upload, key, content_type)
    sha256 = hashlib.sha256()
    size = 0
    part_number = 1
    parts: list[dict] = []
    buffer = bytearray()
    signature_buffer = bytearray()
    signature_checked = False

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
                if not signature_checked and len(signature_buffer) < 64:
                    signature_buffer.extend(chunk[: 64 - len(signature_buffer)])
                    signature_checked = _validate_artifact_signature(artifact_kind, bytes(signature_buffer))
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
                if not signature_checked and len(signature_buffer) < 64:
                    signature_buffer.extend(chunk[: 64 - len(signature_buffer)])
                    signature_checked = _validate_artifact_signature(artifact_kind, bytes(signature_buffer))
                buffer.extend(chunk)
                await flush_parts()

        if size == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty upload")
        if not signature_checked:
            _validate_artifact_signature(artifact_kind, bytes(signature_buffer), final=True)

        await flush_parts(force=True)
        await run_in_threadpool(complete_multipart_upload, key, upload_id, parts)
        return size, sha256.hexdigest()
    except Exception:
        try:
            await run_in_threadpool(abort_multipart_upload, key, upload_id)
        except Exception:  # noqa: BLE001
            logger.exception("failed to abort multipart upload key=%s upload_id=%s", key, upload_id)
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


def _normalize_content_type(content_type: str | None) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


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
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="unsupported artifact content type")
    if lowered_filename and not any(lowered_filename.endswith(suffix) for suffix in ALLOWED_ARTIFACT_SUFFIXES):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="unsupported artifact filename")
    if not lowered_filename and normalized_content_type in {"", "application/octet-stream"}:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="content type required for raw artifact upload")


def _validate_artifact_signature(kind: str, sample: bytes, final: bool = False) -> bool:
    if kind == "gzip":
        if len(sample) < 2:
            if final:
                raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="artifact does not match gzip payload")
            return False
        if not sample.startswith(b"\x1f\x8b"):
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="artifact does not match gzip payload")
        return True

    stripped = sample.lstrip(b" \t\r\n")
    if not stripped:
        return final
    if stripped[:1] not in {b"{", b"["}:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="artifact does not look like JSON")
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
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)

    stmt = select(ScanRun).where(ScanRun.project_id == project_id)
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
        metadata={"limit": limit, "cursor": cursor, "result_count": len(runs)},
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
    search: str | None = Query(default=None),
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
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    current_run = _get_run(db, project_id, run_id)
    baseline_run = _get_run(db, project_id, baseline_run_id) if baseline_run_id else _default_baseline_run(db, project_id, current_run)

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
            "summary": {"new_shares": 0, "disappeared_shares": 0, "changed_shares": 0, "added_items": 0, "removed_items": 0},
            "new_shares": [],
            "disappeared_shares": [],
            "item_churn": [],
        }

    payload = _build_run_diff_from_iters(
        _iter_run_diff_resources(db, current_run.id),
        _iter_run_diff_resources(db, baseline_run.id),
    )
    payload["current_run"] = _run_summary_payload(current_run)
    payload["baseline_run"] = _run_summary_payload(baseline_run)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="RUN_DIFF_VIEWED",
        object_type="scan_run",
        object_id=str(run_id),
        metadata={**payload["summary"], "baseline_run_id": str(baseline_run.id)},
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
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    actor_key = str(auth.token_id or auth.user_id or "anon")
    rate_limiter.check(request, "artifact_upload", limit=30, window_seconds=60, actor_key=f"upload:{actor_key}", fail_open=True)

    settings = get_settings()
    run = _get_run(db, project_id, run_id)
    if run.status not in {RunStatus.PENDING_UPLOAD, RunStatus.UPLOADED, RunStatus.FAILED}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run state does not accept upload")
    previous_status = run.status
    previous_artifact_key = run.artifact_key

    content_type = _normalize_content_type(file.content_type if file else request.headers.get("content-type", "application/octet-stream"))
    _validate_artifact_upload_headers(content_type, file.filename if file else None)
    suffix = _artifact_suffix(content_type, file.filename if file else None)
    key = f"projects/{project_id}/runs/{run_id}/artifact{suffix}"

    started = time.perf_counter()
    size, digest = await _upload_artifact_stream(request, file, key, content_type)

    if previous_status == RunStatus.FAILED:
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
            **request_meta(request),
            "size": size,
            "content_type": content_type,
            "upload_ms": int((time.perf_counter() - started) * 1000),
        },
    )
    db.commit()
    if previous_status == RunStatus.FAILED and previous_artifact_key != key:
        _delete_artifact_quietly(previous_artifact_key)

    payload = {
        "run_id": str(run.id),
        "project_id": str(project_id),
        "artifact_key": key,
        "schema_version": 1,
        "uploaded_at": datetime.now(tz=UTC).isoformat(),
    }
    queued = await _enqueue_with_retries(payload, settings.redis_stream_retries)
    if queued:
        write_audit_event(
            db,
            action="INGEST_QUEUED",
            object_type="scan_run",
            object_id=str(run.id),
            actor_user_id=auth.user_id,
            actor_token_id=auth.token_id,
            project_id=project_id,
            metadata=request_meta(request),
        )
    else:
        write_audit_event(
            db,
            action="INGEST_QUEUE_FALLBACK",
            object_type="scan_run",
            object_id=str(run.id),
            actor_user_id=auth.user_id,
            actor_token_id=auth.token_id,
            project_id=project_id,
            metadata={**request_meta(request), "reason": "redis enqueue failed"},
        )
    db.commit()

    return {
        "ok": True,
        "run_id": str(run.id),
        "artifact_key": key,
        "queued": queued,
    }


@router.get("/{run_id}/endpoints")
def list_endpoints(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    stmt = select(Endpoint).where(Endpoint.run_id == run_id)
    if search:
        escaped = escape_like(search)
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Endpoint.endpoint_key.ilike(pattern, escape="\\"),
                Endpoint.ip.ilike(pattern, escape="\\"),
                Endpoint.hostname.ilike(pattern, escape="\\"),
                Endpoint.domain.ilike(pattern, escape="\\"),
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
        metadata={"search": search, "limit": limit, "cursor": cursor, "result_count": len(rows)},
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
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    stmt = (
        select(Resource)
        .where(Resource.run_id == run_id, Resource.endpoint_id == endpoint_id)
        .order_by(Resource.id.asc())
    )
    resources = db.execute(stmt).scalars().all()

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="RESOURCES_LISTED",
        object_type="endpoint",
        object_id=str(endpoint_id),
        metadata={"run_id": str(run_id), "result_count": len(resources)},
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
            }
            for r in resources
        ]
    }


@router.get("/{run_id}/resources/{resource_id}/items")
def resource_items(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    resource_id: int,
    request: Request,
    search: str | None = None,
    path_prefix: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    stmt = select(Item).where(Item.run_id == run_id, Item.resource_id == resource_id)
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
                "mtime": i.mtime.isoformat() if i.mtime else None,
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
    q: str | None = None,
    ext: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_RUNS)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    stmt = select(Item).where(Item.run_id == run_id)
    if q:
        escaped = escape_like(q)
        stmt = stmt.where(or_(Item.name.ilike(f"%{escaped}%", escape="\\"), cast(Item.path, String).ilike(f"%{escaped}%", escape="\\")))
    if ext:
        ext = ext if ext.startswith(".") else f".{ext}"
        stmt = stmt.where(func.lower(Item.name).like(f"%{escape_like(ext.lower())}", escape="\\"))

    stmt = apply_keyset_pagination(stmt, RUN_ITEM_CURSOR, cursor, limit)
    items, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), RUN_ITEM_CURSOR, limit)

    _write_read_audit(
        db,
        request,
        auth,
        project_id,
        action="ITEMS_SEARCHED",
        object_type="scan_run",
        object_id=str(run_id),
        metadata={"q": q, "ext": ext, "limit": limit, "cursor": cursor, "result_count": len(items)},
    )
    db.commit()

    return {
        "items": [
            {
                "id": i.id,
                "resource_id": i.resource_id,
                "path": i.path,
                "name": i.name,
                "is_dir": i.is_dir,
            }
            for i in items
        ],
        "next_cursor": next_cursor,
    }
