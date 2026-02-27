import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_project_role, request_meta
from app.enums import ProjectRole, RunStatus
from app.models import Endpoint, Item, Resource, ScanRun
from app.pagination import next_cursor, parse_cursor
from app.services.audit import write_audit_event

router = APIRouter(prefix="/projects/{project_id}/inventory", tags=["inventory"])


def _parse_run_ids(raw: str | None) -> list[uuid.UUID]:
    if not raw:
        return []

    values: list[uuid.UUID] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            values.append(uuid.UUID(token))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid run_id: {token}") from exc
    return values


def _base_project_filters(project_id: uuid.UUID):
    return (
        ScanRun.project_id == project_id,
        ScanRun.status.in_([RunStatus.COMPLETE, RunStatus.INGESTING]),
    )


def _audit_read(
    db: Session,
    request: Request,
    auth: AuthContext,
    project_id: uuid.UUID,
    action: str,
    metadata: dict,
) -> None:
    write_audit_event(
        db,
        action=action,
        object_type="project_inventory",
        object_id=str(project_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), **metadata},
    )


@router.get("/extensions")
def inventory_extensions(
    project_id: uuid.UUID,
    request: Request,
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)

    ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
    stmt = (
        select(ext_expr.label("ext"), func.count(Item.id).label("count"))
        .select_from(Item)
        .join(ScanRun, ScanRun.id == Item.run_id)
        .where(*_base_project_filters(project_id), Item.is_dir.is_(False), ext_expr.is_not(None))
        .group_by(ext_expr)
        .order_by(func.count(Item.id).desc(), ext_expr.asc())
        .limit(limit)
    )
    if run_id_list:
        stmt = stmt.where(Item.run_id.in_(run_id_list))

    rows = db.execute(stmt).all()
    items = [{"ext": row.ext, "count": int(row.count)} for row in rows if row.ext]

    _audit_read(
        db,
        request,
        auth,
        project_id,
        action="PROJECT_INVENTORY_EXTENSIONS_LISTED",
        metadata={"run_ids": run_ids, "limit": limit, "result_count": len(items)},
    )
    db.commit()
    return {"items": items}


@router.get("/items")
def inventory_items(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None),
    ext: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    share: str | None = Query(default=None),
    path_prefix: str | None = Query(default=None),
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    is_dir: bool | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    offset = parse_cursor(cursor)

    stmt = (
        select(
            Item.id,
            Item.run_id,
            ScanRun.name.label("run_name"),
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Endpoint.ip,
            Resource.name.label("resource_name"),
            Resource.access_level,
            Item.path,
            Item.name,
            Item.is_dir,
        )
        .select_from(Item)
        .join(Resource, (Resource.id == Item.resource_id) & (Resource.run_id == Item.run_id))
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .join(ScanRun, ScanRun.id == Item.run_id)
        .where(*_base_project_filters(project_id))
    )

    if run_id_list:
        stmt = stmt.where(Item.run_id.in_(run_id_list))
    if is_dir is not None:
        stmt = stmt.where(Item.is_dir.is_(is_dir))
    if path_prefix:
        stmt = stmt.where(Item.path.ilike(f"{path_prefix.strip()}%"))
    if share:
        stmt = stmt.where(Resource.name.ilike(f"%{share.strip()}%"))
    if endpoint:
        pattern = f"%{endpoint.strip()}%"
        stmt = stmt.where(or_(Endpoint.endpoint_key.ilike(pattern), Endpoint.hostname.ilike(pattern), Endpoint.ip.ilike(pattern)))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Item.name.ilike(pattern),
                Item.path.ilike(pattern),
                Resource.name.ilike(pattern),
                Endpoint.endpoint_key.ilike(pattern),
                Endpoint.hostname.ilike(pattern),
                Endpoint.ip.ilike(pattern),
            )
        )
    if ext:
        normalized = ext.strip().lower()
        if normalized and not normalized.startswith("."):
            normalized = f".{normalized}"
        ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
        stmt = stmt.where(ext_expr == normalized)

    rows = db.execute(stmt.order_by(Item.id.desc()).offset(offset).limit(limit)).all()
    items = [
        {
            "id": row.id,
            "run_id": str(row.run_id),
            "run_name": row.run_name,
            "endpoint_key": row.endpoint_key,
            "hostname": row.hostname,
            "ip": row.ip,
            "resource_name": row.resource_name,
            "access_level": row.access_level.value if hasattr(row.access_level, "value") else row.access_level,
            "path": row.path,
            "name": row.name,
            "is_dir": bool(row.is_dir),
        }
        for row in rows
    ]

    _audit_read(
        db,
        request,
        auth,
        project_id,
        action="PROJECT_INVENTORY_ITEMS_LISTED",
        metadata={"q": q, "ext": ext, "endpoint": endpoint, "share": share, "path_prefix": path_prefix, "run_ids": run_ids, "limit": limit},
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}


@router.get("/resources")
def inventory_resources(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    access_level: str | None = Query(default=None),
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    offset = parse_cursor(cursor)

    stmt = (
        select(
            Resource.id,
            Resource.run_id,
            ScanRun.name.label("run_name"),
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Resource.name,
            Resource.remark,
            Resource.access_level,
            func.count(Item.id).label("item_count"),
        )
        .select_from(Resource)
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .join(ScanRun, ScanRun.id == Resource.run_id)
        .outerjoin(Item, (Item.resource_id == Resource.id) & (Item.run_id == Resource.run_id))
        .where(*_base_project_filters(project_id))
        .group_by(
            Resource.id,
            Resource.run_id,
            ScanRun.name,
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Resource.name,
            Resource.remark,
            Resource.access_level,
        )
    )

    if run_id_list:
        stmt = stmt.where(Resource.run_id.in_(run_id_list))
    if access_level:
        normalized_access_level = access_level.strip().lower()
        if normalized_access_level not in {"no_access", "list_only", "readable"}:
            raise HTTPException(status_code=400, detail="invalid access_level")
        stmt = stmt.where(Resource.access_level == normalized_access_level)
    if endpoint:
        pattern = f"%{endpoint.strip()}%"
        stmt = stmt.where(or_(Endpoint.endpoint_key.ilike(pattern), Endpoint.hostname.ilike(pattern)))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(Resource.name.ilike(pattern), Resource.remark.ilike(pattern), Endpoint.endpoint_key.ilike(pattern)))

    rows = db.execute(stmt.order_by(Resource.id.desc()).offset(offset).limit(limit)).all()
    items = [
        {
            "id": row.id,
            "run_id": str(row.run_id),
            "run_name": row.run_name,
            "endpoint_key": row.endpoint_key,
            "hostname": row.hostname,
            "name": row.name,
            "remark": row.remark,
            "access_level": row.access_level.value if hasattr(row.access_level, "value") else row.access_level,
            "item_count": int(row.item_count or 0),
        }
        for row in rows
    ]

    _audit_read(
        db,
        request,
        auth,
        project_id,
        action="PROJECT_INVENTORY_RESOURCES_LISTED",
        metadata={"q": q, "endpoint": endpoint, "access_level": access_level, "run_ids": run_ids, "limit": limit},
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}


@router.get("/endpoints")
def inventory_endpoints(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None),
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    offset = parse_cursor(cursor)

    stmt = (
        select(
            Endpoint.id,
            Endpoint.run_id,
            ScanRun.name.label("run_name"),
            Endpoint.endpoint_key,
            Endpoint.ip,
            Endpoint.hostname,
            Endpoint.domain,
            Endpoint.smb_signing,
            func.count(func.distinct(Resource.id)).label("resource_count"),
            func.count(Item.id).label("item_count"),
        )
        .select_from(Endpoint)
        .join(ScanRun, ScanRun.id == Endpoint.run_id)
        .outerjoin(Resource, (Resource.endpoint_id == Endpoint.id) & (Resource.run_id == Endpoint.run_id))
        .outerjoin(Item, (Item.resource_id == Resource.id) & (Item.run_id == Resource.run_id))
        .where(*_base_project_filters(project_id))
        .group_by(
            Endpoint.id,
            Endpoint.run_id,
            ScanRun.name,
            Endpoint.endpoint_key,
            Endpoint.ip,
            Endpoint.hostname,
            Endpoint.domain,
            Endpoint.smb_signing,
        )
    )

    if run_id_list:
        stmt = stmt.where(Endpoint.run_id.in_(run_id_list))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Endpoint.endpoint_key.ilike(pattern),
                Endpoint.ip.ilike(pattern),
                Endpoint.hostname.ilike(pattern),
                Endpoint.domain.ilike(pattern),
            )
        )

    rows = db.execute(stmt.order_by(Endpoint.id.desc()).offset(offset).limit(limit)).all()
    items = [
        {
            "id": row.id,
            "run_id": str(row.run_id),
            "run_name": row.run_name,
            "endpoint_key": row.endpoint_key,
            "ip": row.ip,
            "hostname": row.hostname,
            "domain": row.domain,
            "smb_signing": row.smb_signing,
            "resource_count": int(row.resource_count or 0),
            "item_count": int(row.item_count or 0),
        }
        for row in rows
    ]

    _audit_read(
        db,
        request,
        auth,
        project_id,
        action="PROJECT_INVENTORY_ENDPOINTS_LISTED",
        metadata={"q": q, "run_ids": run_ids, "limit": limit},
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}
