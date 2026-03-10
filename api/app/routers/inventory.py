import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import String, and_, cast, func, not_, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_project_role, request_meta, require_token_scopes
from app.enums import ProjectRole, RunStatus
from app.models import Endpoint, Item, Resource, SavedInvestigation, ScanRun
from app.pagination import next_cursor, parse_cursor
from app.schemas import SavedInvestigationIn, SavedInvestigationOut
from app.share_types import share_type_from_resource_type
from app.services.inventory_query import InventoryQueryClause, parse_inventory_query
from app.services.audit import write_audit_event
from app.token_scopes import SCOPE_READ_INVENTORY

router = APIRouter(prefix="/projects/{project_id}/inventory", tags=["inventory"])


def _saved_investigation_out(model: SavedInvestigation) -> SavedInvestigationOut:
    return SavedInvestigationOut(
        id=model.id,
        project_id=model.project_id,
        created_by_user_id=model.created_by_user_id,
        name=model.name,
        description=model.description,
        target_tab=model.target_tab,
        query_text=model.query_text,
        definition=model.definition_json or {},
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


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


ACCESS_LEVEL_ALIASES = {
    "no_access": "no_access",
    "none": "no_access",
    "denied": "no_access",
    "list_only": "list_only",
    "list": "list_only",
    "browse": "list_only",
    "readable": "readable",
    "read": "readable",
    "read_only": "readable",
    "read_write": "readable",
    "read-write": "readable",
    "write": "readable",
    "writable": "readable",
}


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_access_query_value(value: str) -> str:
    token = value.strip().lower().replace(" ", "_")
    return ACCESS_LEVEL_ALIASES.get(token, token)


def _normalize_ext_query_value(value: str) -> str:
    token = value.strip().lower()
    if token and not token.startswith("."):
        token = f".{token}"
    return token


def _string_match_expression(column, operator: str, value: str):
    normalized_column = func.coalesce(cast(column, String), "")
    if operator == "equals":
        return func.lower(normalized_column) == value.lower()

    escaped = _escape_like(value)
    if operator == "startswith":
        return normalized_column.ilike(f"{escaped}%", escape="\\")
    return normalized_column.ilike(f"%{escaped}%", escape="\\")


def _multi_column_string_match(columns: tuple, operator: str, value: str):
    expressions = [_string_match_expression(column, operator, value) for column in columns]
    return or_(*expressions)


def _ext_match_expression(column, operator: str, value: str):
    normalized = _normalize_ext_query_value(value)
    return _string_match_expression(column, operator, normalized)


def _access_match_expression(column, operator: str, value: str):
    normalized = _normalize_access_query_value(value)
    return _string_match_expression(column, operator, normalized)


def _apply_inventory_query_groups(stmt, groups: list[list[InventoryQueryClause]], clause_builder):
    if not groups:
        return stmt

    group_expressions = []
    for group in groups:
        clause_expressions = [clause_builder(clause) for clause in group]
        if not clause_expressions:
            continue
        group_expressions.append(and_(*clause_expressions) if len(clause_expressions) > 1 else clause_expressions[0])

    if not group_expressions:
        return stmt
    if len(group_expressions) == 1:
        return stmt.where(group_expressions[0])
    return stmt.where(or_(*group_expressions))


def _item_inventory_clause_expression(clause: InventoryQueryClause):
    ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
    if clause.field == "search":
        expression = _multi_column_string_match(
            (Item.name, Item.path, Resource.name, Endpoint.endpoint_key, Endpoint.hostname, Endpoint.ip),
            clause.operator,
            clause.value,
        )
    elif clause.field == "endpoint":
        expression = _multi_column_string_match((Endpoint.endpoint_key, Endpoint.hostname, Endpoint.ip), clause.operator, clause.value)
    elif clause.field == "share":
        expression = _string_match_expression(Resource.name, clause.operator, clause.value)
    elif clause.field == "path":
        expression = _string_match_expression(Item.path, clause.operator, clause.value)
    elif clause.field == "ext":
        expression = _ext_match_expression(ext_expr, clause.operator, clause.value)
    else:
        expression = _access_match_expression(Resource.access_level, clause.operator, clause.value)
    return not_(expression) if clause.negated else expression


def _resource_inventory_clause_expression(clause: InventoryQueryClause):
    ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
    if clause.field == "search":
        expression = _multi_column_string_match(
            (Resource.name, Resource.remark, Endpoint.endpoint_key, Endpoint.hostname),
            clause.operator,
            clause.value,
        )
        return not_(expression) if clause.negated else expression

    if clause.field == "endpoint":
        expression = _multi_column_string_match((Endpoint.endpoint_key, Endpoint.hostname), clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    if clause.field == "share":
        expression = _string_match_expression(Resource.name, clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    if clause.field == "access":
        expression = _access_match_expression(Resource.access_level, clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    item_subquery = select(1).select_from(Item).where(Item.run_id == Resource.run_id, Item.resource_id == Resource.id).correlate(Resource)
    if clause.field == "path":
        item_subquery = item_subquery.where(_string_match_expression(Item.path, clause.operator, clause.value))
    else:
        item_subquery = item_subquery.where(_ext_match_expression(ext_expr, clause.operator, clause.value))

    expression = item_subquery.exists()
    return not_(expression) if clause.negated else expression


def _endpoint_inventory_clause_expression(clause: InventoryQueryClause):
    ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
    if clause.field == "search":
        expression = _multi_column_string_match(
            (Endpoint.endpoint_key, Endpoint.ip, Endpoint.hostname, Endpoint.domain),
            clause.operator,
            clause.value,
        )
        return not_(expression) if clause.negated else expression

    if clause.field == "endpoint":
        expression = _multi_column_string_match((Endpoint.endpoint_key, Endpoint.ip, Endpoint.hostname), clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    if clause.field == "share":
        expression = (
            select(1)
            .select_from(Resource)
            .where(
                Resource.run_id == Endpoint.run_id,
                Resource.endpoint_id == Endpoint.id,
                _string_match_expression(Resource.name, clause.operator, clause.value),
            )
            .correlate(Endpoint)
            .exists()
        )
        return not_(expression) if clause.negated else expression

    if clause.field == "access":
        expression = (
            select(1)
            .select_from(Resource)
            .where(
                Resource.run_id == Endpoint.run_id,
                Resource.endpoint_id == Endpoint.id,
                _access_match_expression(Resource.access_level, clause.operator, clause.value),
            )
            .correlate(Endpoint)
            .exists()
        )
        return not_(expression) if clause.negated else expression

    item_subquery = (
        select(1)
        .select_from(Resource)
        .join(Item, (Item.resource_id == Resource.id) & (Item.run_id == Resource.run_id))
        .where(Resource.run_id == Endpoint.run_id, Resource.endpoint_id == Endpoint.id)
        .correlate(Endpoint)
    )
    if clause.field == "path":
        item_subquery = item_subquery.where(_string_match_expression(Item.path, clause.operator, clause.value))
    else:
        item_subquery = item_subquery.where(_ext_match_expression(ext_expr, clause.operator, clause.value))

    expression = item_subquery.exists()
    return not_(expression) if clause.negated else expression


@router.get("/investigations", response_model=dict)
def list_saved_investigations(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    investigations = (
        db.execute(
            select(SavedInvestigation)
            .where(SavedInvestigation.project_id == project_id)
            .order_by(SavedInvestigation.updated_at.desc(), SavedInvestigation.created_at.desc())
        )
        .scalars()
        .all()
    )

    _audit_read(
        db,
        request,
        auth,
        project_id,
        action="PROJECT_INVESTIGATIONS_LISTED",
        metadata={"result_count": len(investigations)},
    )
    db.commit()
    return {"items": [_saved_investigation_out(model).model_dump(mode="json") for model in investigations]}


@router.post("/investigations", response_model=SavedInvestigationOut)
def create_saved_investigation(
    project_id: uuid.UUID,
    payload: SavedInvestigationIn,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    investigation = SavedInvestigation(
        project_id=project_id,
        created_by_user_id=auth.user_id,
        name=payload.name,
        description=payload.description,
        target_tab=payload.target_tab,
        query_text=payload.query_text,
        definition_json=payload.definition,
    )
    db.add(investigation)
    db.flush()

    write_audit_event(
        db,
        action="PROJECT_INVESTIGATION_CREATED",
        object_type="project_inventory",
        object_id=str(investigation.id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "name": investigation.name, "target_tab": investigation.target_tab},
    )
    db.commit()
    db.refresh(investigation)
    return _saved_investigation_out(investigation)


@router.delete("/investigations/{investigation_id}")
def delete_saved_investigation(
    project_id: uuid.UUID,
    investigation_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    investigation = db.get(SavedInvestigation, investigation_id)
    if investigation is None or investigation.project_id != project_id:
        raise HTTPException(status_code=404, detail="investigation not found")

    db.delete(investigation)
    write_audit_event(
        db,
        action="PROJECT_INVESTIGATION_DELETED",
        object_type="project_inventory",
        object_id=str(investigation_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata=request_meta(request),
    )
    db.commit()
    return {"ok": True}


@router.get("/stats")
def inventory_stats(
    project_id: uuid.UUID,
    request: Request,
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)

    scope_runs_stmt = select(ScanRun.id).where(*_base_project_filters(project_id))
    if run_id_list:
        scope_runs_stmt = scope_runs_stmt.where(ScanRun.id.in_(run_id_list))

    scope_runs = scope_runs_stmt.subquery()
    scope_run_ids = select(scope_runs.c.id)

    runs_total = int(db.execute(select(func.count(ScanRun.id)).where(ScanRun.project_id == project_id)).scalar() or 0)
    runs_complete = int(
        db.execute(select(func.count(ScanRun.id)).where(ScanRun.project_id == project_id, ScanRun.status == RunStatus.COMPLETE)).scalar() or 0
    )
    runs_ingesting = int(
        db.execute(select(func.count(ScanRun.id)).where(ScanRun.project_id == project_id, ScanRun.status == RunStatus.INGESTING)).scalar() or 0
    )
    latest_run_at = db.execute(select(func.max(ScanRun.created_at)).where(ScanRun.project_id == project_id)).scalar()

    endpoint_count = int(db.execute(select(func.count(Endpoint.id)).where(Endpoint.run_id.in_(scope_run_ids))).scalar() or 0)
    resource_count = int(db.execute(select(func.count(Resource.id)).where(Resource.run_id.in_(scope_run_ids))).scalar() or 0)

    item_totals = db.execute(
        select(
            func.count(Item.id),
            func.count(Item.id).filter(Item.is_dir.is_(False)),
            func.count(Item.id).filter(Item.is_dir.is_(True)),
        ).where(Item.run_id.in_(scope_run_ids))
    ).one()
    items_total = int(item_totals[0] or 0)
    files_total = int(item_totals[1] or 0)
    directories_total = int(item_totals[2] or 0)

    ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
    file_types_count = int(
        db.execute(
            select(func.count(func.distinct(ext_expr))).where(
                Item.run_id.in_(scope_run_ids),
                Item.is_dir.is_(False),
                ext_expr.is_not(None),
            )
        ).scalar()
        or 0
    )

    unique_hosts = int(
        db.execute(
            select(func.count(func.distinct(func.coalesce(Endpoint.hostname, Endpoint.ip, Endpoint.endpoint_key)))).where(
                Endpoint.run_id.in_(scope_run_ids)
            )
        ).scalar()
        or 0
    )

    project_run_count_in_scope = int(db.execute(select(func.count()).select_from(scope_runs)).scalar() or 0)
    _audit_read(
        db,
        request,
        auth,
        project_id,
        action="PROJECT_INVENTORY_STATS_VIEWED",
        metadata={"run_ids": run_ids, "scope_runs": project_run_count_in_scope},
    )
    db.commit()

    return {
        "runs_total": runs_total,
        "runs_complete": runs_complete,
        "runs_ingesting": runs_ingesting,
        "scope_runs": project_run_count_in_scope,
        "endpoints": endpoint_count,
        "shares": resource_count,
        "items": items_total,
        "files": files_total,
        "directories": directories_total,
        "file_types": file_types_count,
        "unique_hosts": unique_hosts,
        "latest_run_at": latest_run_at.isoformat() if latest_run_at else None,
    }


@router.get("/extensions")
def inventory_extensions(
    project_id: uuid.UUID,
    request: Request,
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
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
    query_dsl: str | None = Query(default=None),
    ext: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    share: str | None = Query(default=None),
    path_prefix: str | None = Query(default=None),
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    is_dir: bool | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    query_groups = parse_inventory_query(query_dsl)
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
            Resource.resource_type,
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
    if query_groups:
        stmt = _apply_inventory_query_groups(stmt, query_groups, _item_inventory_clause_expression)

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
            "share_type": share_type_from_resource_type(row.resource_type),
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
        metadata={
            "q": q,
            "query_dsl": query_dsl,
            "ext": ext,
            "endpoint": endpoint,
            "share": share,
            "path_prefix": path_prefix,
            "run_ids": run_ids,
            "limit": limit,
        },
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}


@router.get("/resources")
def inventory_resources(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None),
    query_dsl: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    access_level: str | None = Query(default=None),
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    query_groups = parse_inventory_query(query_dsl)
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
            Resource.resource_type,
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
            Resource.resource_type,
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
    if query_groups:
        stmt = _apply_inventory_query_groups(stmt, query_groups, _resource_inventory_clause_expression)

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
            "share_type": share_type_from_resource_type(row.resource_type),
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
        metadata={
            "q": q,
            "query_dsl": query_dsl,
            "endpoint": endpoint,
            "access_level": access_level,
            "run_ids": run_ids,
            "limit": limit,
        },
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}


@router.get("/endpoints")
def inventory_endpoints(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None),
    query_dsl: str | None = Query(default=None),
    run_ids: str | None = Query(default=None, description="comma-separated run UUIDs"),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    query_groups = parse_inventory_query(query_dsl)
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
    if query_groups:
        stmt = _apply_inventory_query_groups(stmt, query_groups, _endpoint_inventory_clause_expression)

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
        metadata={"q": q, "query_dsl": query_dsl, "run_ids": run_ids, "limit": limit},
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}
