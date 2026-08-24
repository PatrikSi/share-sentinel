import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import String, and_, case, cast, func, not_, or_, select
from sqlalchemy.orm import Session

from app.db import escape_like, get_db
from app.deps import (
    AuthContext,
    get_auth_context,
    request_meta,
    require_project_role,
    require_session_user,
    require_token_scopes,
)
from app.enums import ProjectRole, RunStatus
from app.models import Endpoint, Item, Resource, SavedInvestigation, ScanRun, User
from app.pagination import KeysetColumn, apply_keyset_pagination, paginate_rows, parse_int_cursor_value
from app.schemas import SavedInvestigationIn, SavedInvestigationOut, SavedInvestigationUpdateIn
from app.services.audit import write_audit_event
from app.services.inventory_query import InventoryQueryClause, parse_inventory_query
from app.share_types import share_type_from_resource_type
from app.token_scopes import SCOPE_READ_INVENTORY

router = APIRouter(prefix="/projects/{project_id}/inventory", tags=["inventory"])
INVENTORY_ITEM_CURSOR = (KeysetColumn("id", Item.id, direction="desc", parser=parse_int_cursor_value),)
INVENTORY_RESOURCE_CURSOR = (KeysetColumn("id", Resource.id, direction="desc", parser=parse_int_cursor_value),)
INVENTORY_ENDPOINT_CURSOR = (KeysetColumn("id", Endpoint.id, direction="desc", parser=parse_int_cursor_value),)
MAX_INVENTORY_RUN_IDS = 100
MAX_FILTER_CHARS = 512
MAX_PATH_FILTER_CHARS = 4096
MAX_QUERY_DSL_CHARS = 4096
MAX_RUN_IDS_FILTER_CHARS = 4096


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

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) > MAX_INVENTORY_RUN_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"too many run_ids; maximum is {MAX_INVENTORY_RUN_IDS}",
        )

    values: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for token in parts:
        try:
            parsed = uuid.UUID(token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid run_id: {token}") from exc
        if parsed not in seen:
            seen.add(parsed)
            values.append(parsed)
    return values


def _base_project_filters(project_id: uuid.UUID):
    return (
        ScanRun.project_id == project_id,
        ScanRun.status.in_([RunStatus.COMPLETE, RunStatus.INGESTING]),
    )


def _provider_from_resource_type_expression():
    resource_type = func.lower(cast(Resource.resource_type, String))
    return case(
        (resource_type == "smb_share", "smb"),
        (resource_type == "nfs_share", "nfs"),
        (resource_type == "sharepoint_library", "sharepoint"),
        else_=None,
    )


def _resource_provider_expression(*, include_endpoint: bool = True):
    candidates = [Resource.provider]
    if include_endpoint:
        candidates.append(Endpoint.provider)
    candidates.append(_provider_from_resource_type_expression())
    return func.coalesce(*candidates)


def _item_provider_expression():
    return func.coalesce(
        Item.provider,
        Resource.provider,
        Endpoint.provider,
        _provider_from_resource_type_expression(),
    )


def _resource_provider_equals_expression(value: str, *, include_endpoint: bool = True):
    inferred = _provider_from_resource_type_expression()
    branches = [Resource.provider == value]
    if include_endpoint:
        branches.extend(
            (
                and_(Resource.provider.is_(None), Endpoint.provider == value),
                and_(
                    Resource.provider.is_(None),
                    Endpoint.provider.is_(None),
                    inferred == value,
                ),
            )
        )
    else:
        branches.append(and_(Resource.provider.is_(None), inferred == value))
    return or_(*branches)


def _item_provider_equals_expression(value: str):
    inferred = _provider_from_resource_type_expression()
    return or_(
        Item.provider == value,
        and_(Item.provider.is_(None), Resource.provider == value),
        and_(
            Item.provider.is_(None),
            Resource.provider.is_(None),
            Endpoint.provider == value,
        ),
        and_(
            Item.provider.is_(None),
            Resource.provider.is_(None),
            Endpoint.provider.is_(None),
            inferred == value,
        ),
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
    "unknown": "unknown",
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
}


def _escape_like(value: str) -> str:
    return escape_like(value)


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
            (
                Item.name,
                Item.path,
                Item.provider_item_id,
                Item.provider_parent_id,
                Item.web_url,
                Resource.name,
                Resource.provider_resource_id,
                Resource.web_url,
                Endpoint.endpoint_key,
                Endpoint.hostname,
                Endpoint.ip,
                Endpoint.provider_metadata["display_name"].astext,
                Endpoint.provider_metadata["site_name"].astext,
            ),
            clause.operator,
            clause.value,
        )
    elif clause.field == "endpoint":
        expression = _multi_column_string_match(
            (
                Endpoint.endpoint_key,
                Endpoint.hostname,
                Endpoint.ip,
                Endpoint.provider_metadata["display_name"].astext,
                Endpoint.provider_metadata["site_name"].astext,
                Endpoint.provider_metadata["web_url"].astext,
            ),
            clause.operator,
            clause.value,
        )
    elif clause.field == "share":
        expression = _string_match_expression(Resource.name, clause.operator, clause.value)
    elif clause.field == "path":
        expression = _string_match_expression(Item.path, clause.operator, clause.value)
    elif clause.field == "ext":
        expression = _ext_match_expression(ext_expr, clause.operator, clause.value)
    elif clause.field == "access":
        expression = _access_match_expression(Resource.access_level, clause.operator, clause.value)
    elif clause.field == "provider":
        expression = (
            _item_provider_equals_expression(clause.value.strip().lower())
            if clause.operator == "equals"
            else _string_match_expression(
                _item_provider_expression(),
                clause.operator,
                clause.value,
            )
        )
    elif clause.field == "resource_type":
        expression = _string_match_expression(Resource.resource_type, clause.operator, clause.value)
    elif clause.field == "exposure":
        expression = _string_match_expression(Item.exposure, clause.operator, clause.value)
    else:
        expression = _string_match_expression(
            ScanRun.collection_context["source"].astext,
            clause.operator,
            clause.value,
        )
    return not_(expression) if clause.negated else expression


def _resource_inventory_clause_expression(clause: InventoryQueryClause):
    ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
    if clause.field == "search":
        expression = _multi_column_string_match(
            (
                Resource.name,
                Resource.remark,
                Resource.provider_resource_id,
                Resource.web_url,
                Endpoint.endpoint_key,
                Endpoint.hostname,
                Endpoint.provider_metadata["display_name"].astext,
                Endpoint.provider_metadata["site_name"].astext,
            ),
            clause.operator,
            clause.value,
        )
        return not_(expression) if clause.negated else expression

    if clause.field == "endpoint":
        expression = _multi_column_string_match(
            (
                Endpoint.endpoint_key,
                Endpoint.hostname,
                Endpoint.provider_metadata["display_name"].astext,
                Endpoint.provider_metadata["site_name"].astext,
                Endpoint.provider_metadata["web_url"].astext,
            ),
            clause.operator,
            clause.value,
        )
        return not_(expression) if clause.negated else expression

    if clause.field == "share":
        expression = _string_match_expression(Resource.name, clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    if clause.field == "access":
        expression = _access_match_expression(Resource.access_level, clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    if clause.field == "provider":
        expression = (
            _resource_provider_equals_expression(clause.value.strip().lower())
            if clause.operator == "equals"
            else _string_match_expression(
                _resource_provider_expression(),
                clause.operator,
                clause.value,
            )
        )
        return not_(expression) if clause.negated else expression

    if clause.field == "resource_type":
        expression = _string_match_expression(Resource.resource_type, clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    if clause.field == "exposure":
        expression = _string_match_expression(Resource.exposure, clause.operator, clause.value)
        return not_(expression) if clause.negated else expression

    if clause.field == "source":
        expression = _string_match_expression(
            ScanRun.collection_context["source"].astext,
            clause.operator,
            clause.value,
        )
        return not_(expression) if clause.negated else expression

    item_subquery = (
        select(1)
        .select_from(Item)
        .where(
            Item.run_id == Resource.run_id,
            Item.resource_id == Resource.id,
            Item.deleted.is_(False),
        )
        .correlate(Resource)
    )
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
            (
                Endpoint.endpoint_key,
                Endpoint.ip,
                Endpoint.hostname,
                Endpoint.domain,
                Endpoint.provider_metadata["display_name"].astext,
                Endpoint.provider_metadata["site_name"].astext,
                Endpoint.provider_metadata["web_url"].astext,
            ),
            clause.operator,
            clause.value,
        )
        return not_(expression) if clause.negated else expression

    if clause.field == "endpoint":
        expression = _multi_column_string_match(
            (
                Endpoint.endpoint_key,
                Endpoint.ip,
                Endpoint.hostname,
                Endpoint.provider_metadata["display_name"].astext,
                Endpoint.provider_metadata["site_name"].astext,
                Endpoint.provider_metadata["web_url"].astext,
            ),
            clause.operator,
            clause.value,
        )
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

    if clause.field == "provider":
        child_match = (
            select(1)
            .select_from(Resource)
            .where(
                Resource.run_id == Endpoint.run_id,
                Resource.endpoint_id == Endpoint.id,
                (
                    _resource_provider_equals_expression(
                        clause.value.strip().lower(),
                        include_endpoint=False,
                    )
                    if clause.operator == "equals"
                    else _string_match_expression(
                        _resource_provider_expression(include_endpoint=False),
                        clause.operator,
                        clause.value,
                    )
                ),
            )
            .correlate(Endpoint)
            .exists()
        )
        direct_match = (
            Endpoint.provider == clause.value.strip().lower()
            if clause.operator == "equals"
            else _string_match_expression(Endpoint.provider, clause.operator, clause.value)
        )
        expression = or_(direct_match, child_match)
        return not_(expression) if clause.negated else expression

    if clause.field == "source":
        expression = _string_match_expression(
            ScanRun.collection_context["source"].astext,
            clause.operator,
            clause.value,
        )
        return not_(expression) if clause.negated else expression

    if clause.field in {"resource_type", "exposure"}:
        column = Resource.resource_type if clause.field == "resource_type" else Resource.exposure
        expression = (
            select(1)
            .select_from(Resource)
            .where(
                Resource.run_id == Endpoint.run_id,
                Resource.endpoint_id == Endpoint.id,
                _string_match_expression(column, clause.operator, clause.value),
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
        .where(Item.deleted.is_(False))
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
    _user: User = Depends(require_session_user),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    _ = _user
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

@router.patch("/investigations/{investigation_id}", response_model=SavedInvestigationOut)
def update_saved_investigation(
    project_id: uuid.UUID,
    investigation_id: uuid.UUID,
    payload: SavedInvestigationUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(require_session_user),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    _ = _user
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    investigation = db.get(SavedInvestigation, investigation_id)
    if investigation is None or investigation.project_id != project_id:
        raise HTTPException(status_code=404, detail="investigation not found")

    if payload.name is not None:
        investigation.name = payload.name
    if payload.description is not None:
        investigation.description = payload.description
    if payload.target_tab is not None:
        investigation.target_tab = payload.target_tab
    if payload.query_text is not None:
        investigation.query_text = payload.query_text
    if payload.definition is not None:
        investigation.definition_json = payload.definition

    db.add(investigation)
    write_audit_event(
        db,
        action="PROJECT_INVESTIGATION_UPDATED",
        object_type="project_inventory",
        object_id=str(investigation_id),
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
    _user: User = Depends(require_session_user),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    _ = _user
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
    run_ids: str | None = Query(default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)

    scope_runs_stmt = select(ScanRun.id).where(*_base_project_filters(project_id))
    if run_id_list:
        scope_runs_stmt = scope_runs_stmt.where(ScanRun.id.in_(run_id_list))

    scope_runs = scope_runs_stmt.cte("scope_runs")
    scope_run_ids = select(scope_runs.c.id)

    run_totals = db.execute(
        select(
            func.count(ScanRun.id).label("runs_total"),
            func.count(ScanRun.id).filter(ScanRun.status == RunStatus.COMPLETE).label("runs_complete"),
            func.count(ScanRun.id).filter(ScanRun.status == RunStatus.INGESTING).label("runs_ingesting"),
            func.max(ScanRun.created_at).label("latest_run_at"),
        ).where(ScanRun.project_id == project_id)
    ).one()
    runs_total = int(run_totals.runs_total or 0)
    runs_complete = int(run_totals.runs_complete or 0)
    runs_ingesting = int(run_totals.runs_ingesting or 0)
    latest_run_at = run_totals.latest_run_at

    entity_totals = db.execute(
        select(
            select(func.count()).select_from(scope_runs).scalar_subquery().label("scope_runs"),
            select(func.count(Endpoint.id))
            .where(Endpoint.run_id.in_(scope_run_ids))
            .scalar_subquery()
            .label("endpoints"),
            select(func.count(Resource.id))
            .where(Resource.run_id.in_(scope_run_ids))
            .scalar_subquery()
            .label("resources"),
            select(func.count(func.distinct(func.coalesce(Endpoint.hostname, Endpoint.ip, Endpoint.endpoint_key))))
            .where(Endpoint.run_id.in_(scope_run_ids))
            .scalar_subquery()
            .label("unique_hosts"),
        )
    ).one()
    project_run_count_in_scope = int(entity_totals.scope_runs or 0)
    endpoint_count = int(entity_totals.endpoints or 0)
    resource_count = int(entity_totals.resources or 0)
    unique_hosts = int(entity_totals.unique_hosts or 0)

    item_totals = db.execute(
        select(
            func.count(Item.id),
            func.count(Item.id).filter(Item.is_dir.is_(False)),
            func.count(Item.id).filter(Item.is_dir.is_(True)),
            func.count(func.distinct(func.lower(func.substring(Item.name, r"\.[^.]+$")))).filter(
                Item.is_dir.is_(False),
            ),
        ).where(Item.run_id.in_(scope_run_ids), Item.deleted.is_(False))
    ).one()
    items_total = int(item_totals[0] or 0)
    files_total = int(item_totals[1] or 0)
    directories_total = int(item_totals[2] or 0)
    file_types_count = int(item_totals[3] or 0)
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
    run_ids: str | None = Query(default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"),
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
        .where(
            *_base_project_filters(project_id),
            Item.is_dir.is_(False),
            Item.deleted.is_(False),
            ext_expr.is_not(None),
        )
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
    q: str | None = Query(default=None, max_length=MAX_FILTER_CHARS),
    query_dsl: str | None = Query(default=None, max_length=MAX_QUERY_DSL_CHARS),
    ext: str | None = Query(default=None, max_length=MAX_FILTER_CHARS),
    endpoint: str | None = Query(default=None, max_length=MAX_FILTER_CHARS),
    share: str | None = Query(default=None, max_length=MAX_FILTER_CHARS),
    path_prefix: str | None = Query(default=None, max_length=MAX_PATH_FILTER_CHARS),
    provider: str | None = Query(default=None, max_length=32),
    resource_type: str | None = Query(default=None, max_length=64),
    exposure: str | None = Query(default=None, max_length=32),
    source: str | None = Query(default=None, max_length=64),
    run_ids: str | None = Query(default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"),
    is_dir: bool | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    query_groups = parse_inventory_query(query_dsl)

    stmt = (
        select(
            Item.id,
            Item.run_id,
            ScanRun.name.label("run_name"),
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Endpoint.ip,
            Endpoint.provider_metadata.label("endpoint_metadata"),
            Resource.name.label("resource_name"),
            Resource.access_level,
            Resource.access_capabilities,
            Resource.resource_type,
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
            _item_provider_expression().label("provider"),
            Item.provider_item_id,
            Item.provider_parent_id,
            Item.web_url,
            Item.mime_type,
            Item.deleted,
            Item.provider_metadata,
            Item.exposure,
            Item.exposure_evidence,
        )
        .select_from(Item)
        .join(Resource, (Resource.id == Item.resource_id) & (Resource.run_id == Item.run_id))
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .join(ScanRun, ScanRun.id == Item.run_id)
        .where(*_base_project_filters(project_id))
    )

    if run_id_list:
        stmt = stmt.where(Item.run_id.in_(run_id_list))
    if not include_deleted:
        stmt = stmt.where(Item.deleted.is_(False))
    if is_dir is not None:
        stmt = stmt.where(Item.is_dir.is_(is_dir))
    if path_prefix:
        escaped = _escape_like(path_prefix.strip())
        stmt = stmt.where(Item.path.ilike(f"{escaped}%", escape="\\"))
    if share:
        escaped = _escape_like(share.strip())
        stmt = stmt.where(Resource.name.ilike(f"%{escaped}%", escape="\\"))
    if endpoint:
        escaped = _escape_like(endpoint.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(or_(Endpoint.endpoint_key.ilike(pattern, escape="\\"), Endpoint.hostname.ilike(pattern, escape="\\"), Endpoint.ip.ilike(pattern, escape="\\")))
    if provider:
        stmt = stmt.where(_item_provider_equals_expression(provider.strip().lower()))
    if resource_type:
        stmt = stmt.where(func.lower(cast(Resource.resource_type, String)) == resource_type.strip().lower())
    if exposure:
        stmt = stmt.where(Item.exposure == exposure.strip().upper())
    if source:
        stmt = stmt.where(func.lower(ScanRun.collection_context["source"].astext) == source.strip().lower())
    if q:
        escaped = _escape_like(q.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Item.name.ilike(pattern, escape="\\"),
                Item.path.ilike(pattern, escape="\\"),
                Item.provider_item_id.ilike(pattern, escape="\\"),
                Item.provider_parent_id.ilike(pattern, escape="\\"),
                Item.web_url.ilike(pattern, escape="\\"),
                Resource.name.ilike(pattern, escape="\\"),
                Resource.provider_resource_id.ilike(pattern, escape="\\"),
                Resource.web_url.ilike(pattern, escape="\\"),
                Endpoint.endpoint_key.ilike(pattern, escape="\\"),
                Endpoint.hostname.ilike(pattern, escape="\\"),
                Endpoint.ip.ilike(pattern, escape="\\"),
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
        normalized = ext.strip().lower()
        if normalized and not normalized.startswith("."):
            normalized = f".{normalized}"
        ext_expr = func.lower(func.substring(Item.name, r"\.[^.]+$"))
        stmt = stmt.where(ext_expr == normalized)
    if query_groups:
        stmt = _apply_inventory_query_groups(stmt, query_groups, _item_inventory_clause_expression)

    stmt = apply_keyset_pagination(stmt, INVENTORY_ITEM_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).all(), INVENTORY_ITEM_CURSOR, limit)
    items = [
        {
            "id": row.id,
            "run_id": str(row.run_id),
            "run_name": row.run_name,
            "endpoint_key": row.endpoint_key,
            "hostname": row.hostname,
            "ip": row.ip,
            "endpoint_metadata": getattr(row, "endpoint_metadata", None) or {},
            "resource_name": row.resource_name,
            "access_level": row.access_level.value if hasattr(row.access_level, "value") else row.access_level,
            "access_capabilities": row.access_capabilities or {},
            "share_type": share_type_from_resource_type(row.resource_type),
            "resource_type": row.resource_type.value if hasattr(row.resource_type, "value") else row.resource_type,
            "path": row.path,
            "name": row.name,
            "is_dir": bool(row.is_dir),
            "size_bytes": row.size_bytes,
            "allocation_size_bytes": row.allocation_size_bytes,
            "mtime": row.mtime.isoformat() if row.mtime else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "accessed_at": row.accessed_at.isoformat() if row.accessed_at else None,
            "changed_at": row.changed_at.isoformat() if row.changed_at else None,
            "file_attributes": row.file_attributes or [],
            "provider": getattr(row, "provider", None)
            or share_type_from_resource_type(row.resource_type),
            "provider_item_id": getattr(row, "provider_item_id", None),
            "provider_parent_id": getattr(row, "provider_parent_id", None),
            "web_url": getattr(row, "web_url", None),
            "mime_type": getattr(row, "mime_type", None),
            "deleted": bool(getattr(row, "deleted", False)),
            "metadata": getattr(row, "provider_metadata", None) or {},
            "exposure": getattr(row, "exposure", None),
            "exposure_evidence": getattr(row, "exposure_evidence", None) or {},
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
            "provider": provider,
            "resource_type": resource_type,
            "exposure": exposure,
            "source": source,
            "include_deleted": include_deleted,
            "run_ids": run_ids,
            "limit": limit,
        },
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor}


@router.get("/resources")
def inventory_resources(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None, max_length=MAX_FILTER_CHARS),
    query_dsl: str | None = Query(default=None, max_length=MAX_QUERY_DSL_CHARS),
    endpoint: str | None = Query(default=None, max_length=MAX_FILTER_CHARS),
    access_level: str | None = Query(default=None),
    provider: str | None = Query(default=None, max_length=32),
    resource_type: str | None = Query(default=None, max_length=64),
    exposure: str | None = Query(default=None, max_length=32),
    source: str | None = Query(default=None, max_length=64),
    run_ids: str | None = Query(default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    query_groups = parse_inventory_query(query_dsl)

    item_count_subquery = (
        select(func.count(Item.id))
        .where(
            Item.run_id == Resource.run_id,
            Item.resource_id == Resource.id,
            Item.deleted.is_(False),
        )
        .correlate(Resource)
        .scalar_subquery()
    )
    stmt = (
        select(
            Resource.id,
            Resource.run_id,
            ScanRun.name.label("run_name"),
            Endpoint.endpoint_key,
            Endpoint.hostname,
            Endpoint.provider_metadata.label("endpoint_metadata"),
            Resource.name,
            Resource.remark,
            Resource.access_level,
            Resource.access_capabilities,
            Resource.resource_type,
            _resource_provider_expression().label("provider"),
            Resource.provider_resource_id,
            Resource.web_url,
            Resource.provider_metadata,
            Resource.exposure,
            Resource.exposure_evidence,
            item_count_subquery.label("item_count"),
        )
        .select_from(Resource)
        .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
        .join(ScanRun, ScanRun.id == Resource.run_id)
        .where(*_base_project_filters(project_id))
    )

    if run_id_list:
        stmt = stmt.where(Resource.run_id.in_(run_id_list))
    if access_level:
        normalized_access_level = access_level.strip().lower()
        if normalized_access_level not in {"unknown", "no_access", "list_only", "readable"}:
            raise HTTPException(status_code=400, detail="invalid access_level")
        stmt = stmt.where(Resource.access_level == normalized_access_level)
    if endpoint:
        escaped = _escape_like(endpoint.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(or_(Endpoint.endpoint_key.ilike(pattern, escape="\\"), Endpoint.hostname.ilike(pattern, escape="\\")))
    if provider:
        stmt = stmt.where(_resource_provider_equals_expression(provider.strip().lower()))
    if resource_type:
        stmt = stmt.where(func.lower(cast(Resource.resource_type, String)) == resource_type.strip().lower())
    if exposure:
        stmt = stmt.where(Resource.exposure == exposure.strip().upper())
    if source:
        stmt = stmt.where(func.lower(ScanRun.collection_context["source"].astext) == source.strip().lower())
    if q:
        escaped = _escape_like(q.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Resource.name.ilike(pattern, escape="\\"),
                Resource.remark.ilike(pattern, escape="\\"),
                Resource.provider_resource_id.ilike(pattern, escape="\\"),
                Resource.web_url.ilike(pattern, escape="\\"),
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
    if query_groups:
        stmt = _apply_inventory_query_groups(stmt, query_groups, _resource_inventory_clause_expression)

    stmt = apply_keyset_pagination(stmt, INVENTORY_RESOURCE_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).all(), INVENTORY_RESOURCE_CURSOR, limit)
    items = [
        {
            "id": row.id,
            "run_id": str(row.run_id),
            "run_name": row.run_name,
            "endpoint_key": row.endpoint_key,
            "hostname": row.hostname,
            "endpoint_metadata": getattr(row, "endpoint_metadata", None) or {},
            "name": row.name,
            "remark": row.remark,
            "access_level": row.access_level.value if hasattr(row.access_level, "value") else row.access_level,
            "access_capabilities": row.access_capabilities or {},
            "share_type": share_type_from_resource_type(row.resource_type),
            "resource_type": row.resource_type.value if hasattr(row.resource_type, "value") else row.resource_type,
            "provider": getattr(row, "provider", None)
            or share_type_from_resource_type(row.resource_type),
            "provider_resource_id": getattr(row, "provider_resource_id", None),
            "web_url": getattr(row, "web_url", None),
            "metadata": getattr(row, "provider_metadata", None) or {},
            "exposure": getattr(row, "exposure", None),
            "exposure_evidence": getattr(row, "exposure_evidence", None) or {},
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
            "provider": provider,
            "resource_type": resource_type,
            "exposure": exposure,
            "source": source,
            "run_ids": run_ids,
            "limit": limit,
        },
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor}


@router.get("/endpoints")
def inventory_endpoints(
    project_id: uuid.UUID,
    request: Request,
    q: str | None = Query(default=None, max_length=MAX_FILTER_CHARS),
    query_dsl: str | None = Query(default=None, max_length=MAX_QUERY_DSL_CHARS),
    provider: str | None = Query(default=None, max_length=32),
    source: str | None = Query(default=None, max_length=64),
    run_ids: str | None = Query(default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    query_groups = parse_inventory_query(query_dsl)

    resource_count_subquery = (
        select(func.count(Resource.id))
        .where(Resource.run_id == Endpoint.run_id, Resource.endpoint_id == Endpoint.id)
        .correlate(Endpoint)
        .scalar_subquery()
    )
    item_count_subquery = (
        select(func.count(Item.id))
        .select_from(Resource)
        .join(Item, (Item.resource_id == Resource.id) & (Item.run_id == Resource.run_id))
        .where(
            Resource.run_id == Endpoint.run_id,
            Resource.endpoint_id == Endpoint.id,
            Item.deleted.is_(False),
        )
        .correlate(Endpoint)
        .scalar_subquery()
    )
    child_provider = _resource_provider_expression(include_endpoint=False)
    inferred_provider_subquery = (
        select(
            case(
                (func.count(func.distinct(child_provider)) == 1, func.min(child_provider)),
                else_=None,
            )
        )
        .where(
            Resource.run_id == Endpoint.run_id,
            Resource.endpoint_id == Endpoint.id,
        )
        .correlate(Endpoint)
        .scalar_subquery()
    )
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
            func.coalesce(Endpoint.provider, inferred_provider_subquery).label("provider"),
            Endpoint.provider_metadata,
            resource_count_subquery.label("resource_count"),
            item_count_subquery.label("item_count"),
        )
        .select_from(Endpoint)
        .join(ScanRun, ScanRun.id == Endpoint.run_id)
        .where(*_base_project_filters(project_id))
    )

    if run_id_list:
        stmt = stmt.where(Endpoint.run_id.in_(run_id_list))
    if provider:
        normalized_provider = provider.strip().lower()
        child_provider_match = (
            select(1)
            .select_from(Resource)
            .where(
                Resource.run_id == Endpoint.run_id,
                Resource.endpoint_id == Endpoint.id,
                _resource_provider_equals_expression(
                    normalized_provider,
                    include_endpoint=False,
                ),
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
    if source:
        stmt = stmt.where(func.lower(ScanRun.collection_context["source"].astext) == source.strip().lower())
    if q:
        escaped = _escape_like(q.strip())
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
    if query_groups:
        stmt = _apply_inventory_query_groups(stmt, query_groups, _endpoint_inventory_clause_expression)

    stmt = apply_keyset_pagination(stmt, INVENTORY_ENDPOINT_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).all(), INVENTORY_ENDPOINT_CURSOR, limit)
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
            "provider": getattr(row, "provider", None),
            "metadata": getattr(row, "provider_metadata", None) or {},
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
        metadata={
            "q": q,
            "query_dsl": query_dsl,
            "provider": provider,
            "source": source,
            "run_ids": run_ids,
            "limit": limit,
        },
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor}
