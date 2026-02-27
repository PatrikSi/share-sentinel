import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import and_, cast, delete, func, or_, select, String
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_project_role, request_meta
from app.enums import ProjectRole, RunStatus
from app.models import Endpoint, Item, Resource, ScanRun
from app.pagination import next_cursor, parse_cursor
from app.rate_limit import RateLimiter
from app.schemas import RunCreateIn, RunOut
from app.services.audit import write_audit_event
from app.services.queue import enqueue_ingest_job
from app.services.storage import upload_fileobj

router = APIRouter(prefix="/projects/{project_id}/runs", tags=["runs"])
rate_limiter = RateLimiter()


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
        summary=run.summary,
    )


@router.post("", response_model=RunOut)
def create_run(
    project_id: uuid.UUID,
    payload: RunCreateIn,
    request: Request,
    db: Session = Depends(get_db),
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
    db.commit()
    db.refresh(run)
    return _to_run_out(run)


@router.get("", response_model=dict)
def list_runs(
    project_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    offset = parse_cursor(cursor)

    stmt = (
        select(ScanRun)
        .where(ScanRun.project_id == project_id)
        .order_by(ScanRun.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    runs = db.execute(stmt).scalars().all()
    return {
        "items": [_to_run_out(r).model_dump(mode="json") for r in runs],
        "next_cursor": next_cursor(offset, limit, len(runs)),
    }


@router.get("/{run_id}", response_model=RunOut)
def get_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run = _get_run(db, project_id, run_id)
    return _to_run_out(run)


@router.delete("/{run_id}")
def delete_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)
    run = _get_run(db, project_id, run_id)
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
    return {"ok": True}


@router.post("/{run_id}/artifact")
async def upload_artifact(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.OPERATOR, auth, db)
    rate_limiter.check(request, "artifact_upload", limit=30, window_seconds=60)

    settings = get_settings()
    run = _get_run(db, project_id, run_id)
    if run.status not in {RunStatus.PENDING_UPLOAD, RunStatus.UPLOADED, RunStatus.FAILED}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run state does not accept upload")

    content_type = file.content_type if file else request.headers.get("content-type", "application/octet-stream")
    suffix = ".ndjson.gz" if "gzip" in (content_type or "") else ".ndjson"
    key = f"projects/{project_id}/runs/{run_id}/artifact{suffix}"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        temp_path = tmp.name

    sha256 = hashlib.sha256()
    size = 0

    try:
        with open(temp_path, "wb") as out:
            if file:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > settings.upload_max_bytes:
                        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="upload too large")
                    sha256.update(chunk)
                    out.write(chunk)
            else:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > settings.upload_max_bytes:
                        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="upload too large")
                    sha256.update(chunk)
                    out.write(chunk)

        if size == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty upload")

        with open(temp_path, "rb") as up:
            upload_fileobj(up, key=key, content_type=content_type)

    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    run.artifact_key = key
    run.artifact_size = size
    run.artifact_sha256 = sha256.hexdigest()
    run.artifact_content_type = content_type
    run.status = RunStatus.UPLOADED
    run.ingest_progress = {"line_offset": 0}

    enqueue_ingest_job(
        {
            "run_id": str(run.id),
            "project_id": str(project_id),
            "artifact_key": key,
            "schema_version": 1,
            "uploaded_at": datetime.now(tz=UTC).isoformat(),
        }
    )

    write_audit_event(
        db,
        action="ARTIFACT_UPLOADED",
        object_type="scan_run",
        object_id=str(run.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "size": size, "content_type": content_type},
    )

    db.add(run)
    db.commit()
    return {"ok": True, "run_id": str(run.id), "artifact_key": key}


@router.get("/{run_id}/endpoints")
def list_endpoints(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    offset = parse_cursor(cursor)

    stmt = select(Endpoint).where(Endpoint.run_id == run_id)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Endpoint.endpoint_key.ilike(pattern),
                Endpoint.ip.ilike(pattern),
                Endpoint.hostname.ilike(pattern),
                Endpoint.domain.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Endpoint.id.asc()).offset(offset).limit(limit)
    rows = db.execute(stmt).scalars().all()
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
        "next_cursor": next_cursor(offset, limit, len(rows)),
    }


@router.get("/{run_id}/endpoints/{endpoint_id}/resources")
def endpoint_resources(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    endpoint_id: int,
    db: Session = Depends(get_db),
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
    return {
        "items": [
            {
                "id": r.id,
                "resource_type": r.resource_type,
                "name": r.name,
                "remark": r.remark,
                "access_level": r.access_level,
            }
            for r in resources
        ]
    }


@router.get("/{run_id}/resources/{resource_id}/items")
def resource_items(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    resource_id: int,
    search: str | None = None,
    path_prefix: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    offset = parse_cursor(cursor)

    stmt = select(Item).where(Item.run_id == run_id, Item.resource_id == resource_id)
    if search:
        stmt = stmt.where(Item.name.ilike(f"%{search}%"))
    if path_prefix:
        stmt = stmt.where(Item.path.ilike(f"{path_prefix}%"))

    stmt = stmt.order_by(Item.id.asc()).offset(offset).limit(limit)
    items = db.execute(stmt).scalars().all()
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
        "next_cursor": next_cursor(offset, limit, len(items)),
    }


@router.get("/{run_id}/search/items")
def search_items(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    q: str | None = None,
    ext: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    _get_run(db, project_id, run_id)

    offset = parse_cursor(cursor)

    stmt = select(Item).where(Item.run_id == run_id)
    if q:
        stmt = stmt.where(or_(Item.name.ilike(f"%{q}%"), cast(Item.path, String).ilike(f"%{q}%")))
    if ext:
        ext = ext if ext.startswith(".") else f".{ext}"
        stmt = stmt.where(func.lower(Item.name).like(f"%{ext.lower()}"))

    stmt = stmt.order_by(Item.id.asc()).offset(offset).limit(limit)
    items = db.execute(stmt).scalars().all()
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
        "next_cursor": next_cursor(offset, limit, len(items)),
    }
