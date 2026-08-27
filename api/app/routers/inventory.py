import asyncio
import csv
import io
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import date, datetime
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import String, and_, case, cast, func, not_, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.config import get_settings
from app.db import SessionLocal, escape_like, get_db
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
from app.rate_limit import RateLimiter
from app.schemas import SavedInvestigationIn, SavedInvestigationOut, SavedInvestigationUpdateIn
from app.services.access_evidence import build_access_evidence_summary
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
INVENTORY_EXPORT_BATCH_SIZE = 100
INVENTORY_EXPORT_CHUNK_BYTES = 256 * 1024
logger = logging.getLogger("share_sentinel.inventory")
rate_limiter = RateLimiter()

INVENTORY_EXPORT_COLUMNS: dict[str, tuple[str, ...]] = {
    "items": (
        "id",
        "run_id",
        "run_name",
        "endpoint_key",
        "hostname",
        "ip",
        "endpoint_metadata",
        "resource_name",
        "provider",
        "share_type",
        "resource_type",
        "access_level",
        "access_capabilities",
        "access_evidence_summary",
        "path",
        "name",
        "item_type",
        "is_dir",
        "size_bytes",
        "allocation_size_bytes",
        "mtime",
        "created_at",
        "accessed_at",
        "changed_at",
        "file_attributes",
        "web_url",
        "mime_type",
        "provider_item_id",
        "provider_parent_id",
        "deleted",
        "exposure",
        "metadata",
        "exposure_evidence",
    ),
    "resources": (
        "id",
        "run_id",
        "run_name",
        "endpoint_key",
        "hostname",
        "endpoint_metadata",
        "name",
        "remark",
        "provider",
        "share_type",
        "resource_type",
        "access_level",
        "access_capabilities",
        "access_evidence_summary",
        "provider_resource_id",
        "web_url",
        "exposure",
        "metadata",
        "exposure_evidence",
        "item_count",
    ),
    "endpoints": (
        "id",
        "run_id",
        "run_name",
        "endpoint_key",
        "ip",
        "hostname",
        "domain",
        "smb_signing",
        "provider",
        "metadata",
        "resource_count",
        "item_count",
    ),
}


class _InventoryExportAdmission:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                logger.error("inventory_export_admission_release_without_slot")
                return
            self._active -= 1

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


inventory_export_admission = _InventoryExportAdmission(get_settings().api_inventory_export_max_concurrent)


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


def _item_type_match_expression(operator: str, value: str):
    normalized_value = value.strip().lower().replace(" ", "_")
    normalized_value = {
        "dir": "directory",
        "folder": "directory",
        "directories": "directory",
        "folders": "directory",
        "files": "file",
    }.get(normalized_value, normalized_value)
    item_type = case((Item.is_dir.is_(True), "directory"), else_="file")
    return _string_match_expression(item_type, operator, normalized_value)


def _file_archive_status_match_expression(operator: str, value: str):
    normalized_value = value.strip().lower().replace("-", "_").replace(" ", "_")
    normalized_value = {
        "archived": "fully_archived",
        "active": "not_archived",
        "notarchived": "not_archived",
    }.get(normalized_value, normalized_value)
    return _string_match_expression(
        Item.provider_metadata["file_archive_status"].astext,
        operator,
        normalized_value,
    )


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
    elif clause.field == "item_type":
        expression = _item_type_match_expression(clause.operator, clause.value)
    elif clause.field == "file_archive_status":
        expression = _file_archive_status_match_expression(clause.operator, clause.value)
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
    elif clause.field == "item_type":
        item_subquery = item_subquery.where(_item_type_match_expression(clause.operator, clause.value))
    elif clause.field == "file_archive_status":
        item_subquery = item_subquery.where(_file_archive_status_match_expression(clause.operator, clause.value))
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
    elif clause.field == "item_type":
        item_subquery = item_subquery.where(_item_type_match_expression(clause.operator, clause.value))
    elif clause.field == "file_archive_status":
        item_subquery = item_subquery.where(_file_archive_status_match_expression(clause.operator, clause.value))
    else:
        item_subquery = item_subquery.where(_ext_match_expression(ext_expr, clause.operator, clause.value))

    expression = item_subquery.exists()
    return not_(expression) if clause.negated else expression


def _inventory_export_statement(
    project_id: uuid.UUID,
    tab: str,
    run_ids: list[uuid.UUID],
    query_groups: list[list[InventoryQueryClause]],
    *,
    include_deleted: bool,
):
    if tab == "items":
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
                Item.permission_summary.label("access_evidence_summary"),
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
        if run_ids:
            stmt = stmt.where(Item.run_id.in_(run_ids))
        if not include_deleted:
            stmt = stmt.where(Item.deleted.is_(False))
        stmt = _apply_inventory_query_groups(stmt, query_groups, _item_inventory_clause_expression)
        return stmt.order_by(Item.id.desc())

    if tab == "resources":
        item_count = (
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
                Resource.permission_summary.label("access_evidence_summary"),
                Resource.resource_type,
                _resource_provider_expression().label("provider"),
                Resource.provider_resource_id,
                Resource.web_url,
                Resource.provider_metadata,
                Resource.exposure,
                Resource.exposure_evidence,
                item_count.label("item_count"),
            )
            .select_from(Resource)
            .join(Endpoint, (Endpoint.id == Resource.endpoint_id) & (Endpoint.run_id == Resource.run_id))
            .join(ScanRun, ScanRun.id == Resource.run_id)
            .where(*_base_project_filters(project_id))
        )
        if run_ids:
            stmt = stmt.where(Resource.run_id.in_(run_ids))
        stmt = _apply_inventory_query_groups(stmt, query_groups, _resource_inventory_clause_expression)
        return stmt.order_by(Resource.id.desc())

    resource_count = (
        select(func.count(Resource.id))
        .where(Resource.run_id == Endpoint.run_id, Resource.endpoint_id == Endpoint.id)
        .correlate(Endpoint)
        .scalar_subquery()
    )
    item_count = (
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
    inferred_provider = (
        select(
            case(
                (func.count(func.distinct(child_provider)) == 1, func.min(child_provider)),
                else_=None,
            )
        )
        .where(Resource.run_id == Endpoint.run_id, Resource.endpoint_id == Endpoint.id)
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
            func.coalesce(Endpoint.provider, inferred_provider).label("provider"),
            Endpoint.provider_metadata,
            resource_count.label("resource_count"),
            item_count.label("item_count"),
        )
        .select_from(Endpoint)
        .join(ScanRun, ScanRun.id == Endpoint.run_id)
        .where(*_base_project_filters(project_id))
    )
    if run_ids:
        stmt = stmt.where(Endpoint.run_id.in_(run_ids))
    stmt = _apply_inventory_query_groups(stmt, query_groups, _endpoint_inventory_clause_expression)
    return stmt.order_by(Endpoint.id.desc())


def _enum_value(value):
    return value.value if isinstance(value, Enum) else value


def _inventory_export_record(tab: str, row) -> dict[str, object]:
    if tab == "items":
        resource_type = _enum_value(row.resource_type)
        return {
            "id": row.id,
            "run_id": row.run_id,
            "run_name": row.run_name,
            "endpoint_key": row.endpoint_key,
            "hostname": row.hostname,
            "ip": row.ip,
            "endpoint_metadata": getattr(row, "endpoint_metadata", None) or {},
            "resource_name": row.resource_name,
            "provider": getattr(row, "provider", None) or share_type_from_resource_type(resource_type),
            "share_type": share_type_from_resource_type(resource_type),
            "resource_type": resource_type,
            "access_level": _enum_value(row.access_level),
            "access_capabilities": row.access_capabilities or {},
            "access_evidence_summary": build_access_evidence_summary(
                getattr(row, "access_evidence_summary", None),
                row.access_level,
                row.access_capabilities,
                getattr(row, "exposure", None),
            ),
            "path": row.path,
            "name": row.name,
            "item_type": "directory" if row.is_dir else "file",
            "is_dir": bool(row.is_dir),
            "size_bytes": row.size_bytes,
            "allocation_size_bytes": row.allocation_size_bytes,
            "mtime": row.mtime,
            "created_at": row.created_at,
            "accessed_at": row.accessed_at,
            "changed_at": row.changed_at,
            "file_attributes": row.file_attributes or [],
            "web_url": getattr(row, "web_url", None),
            "mime_type": getattr(row, "mime_type", None),
            "provider_item_id": getattr(row, "provider_item_id", None),
            "provider_parent_id": getattr(row, "provider_parent_id", None),
            "deleted": bool(getattr(row, "deleted", False)),
            "exposure": getattr(row, "exposure", None),
            "metadata": getattr(row, "provider_metadata", None) or {},
            "exposure_evidence": getattr(row, "exposure_evidence", None) or {},
        }

    if tab == "resources":
        resource_type = _enum_value(row.resource_type)
        return {
            "id": row.id,
            "run_id": row.run_id,
            "run_name": row.run_name,
            "endpoint_key": row.endpoint_key,
            "hostname": row.hostname,
            "endpoint_metadata": getattr(row, "endpoint_metadata", None) or {},
            "name": row.name,
            "remark": row.remark,
            "provider": getattr(row, "provider", None) or share_type_from_resource_type(resource_type),
            "share_type": share_type_from_resource_type(resource_type),
            "resource_type": resource_type,
            "access_level": _enum_value(row.access_level),
            "access_capabilities": row.access_capabilities or {},
            "access_evidence_summary": build_access_evidence_summary(
                getattr(row, "access_evidence_summary", None),
                row.access_level,
                row.access_capabilities,
                getattr(row, "exposure", None),
            ),
            "provider_resource_id": getattr(row, "provider_resource_id", None),
            "web_url": getattr(row, "web_url", None),
            "exposure": getattr(row, "exposure", None),
            "metadata": getattr(row, "provider_metadata", None) or {},
            "exposure_evidence": getattr(row, "exposure_evidence", None) or {},
            "item_count": int(row.item_count or 0),
        }

    return {
        "id": row.id,
        "run_id": row.run_id,
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


def _spreadsheet_safe_csv_value(value: object) -> str:
    value = _enum_value(value)
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (datetime, date)):
        text = value.isoformat()
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    else:
        text = str(value)

    formula_candidate = text.lstrip(" \t\r\n")
    leading_characters = text[: len(text) - len(formula_candidate)]
    if any(character in "\t\r\n" for character in leading_characters) or formula_candidate.startswith(
        ("=", "+", "-", "@")
    ):
        return f"'{text}"
    return text


def _csv_record_bytes(values: tuple | list) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerow(values)
    return output.getvalue().encode("utf-8")


def _inventory_csv_chunks(
    tab: str,
    rows: list,
    *,
    include_header: bool = False,
    max_chunk_bytes: int = INVENTORY_EXPORT_CHUNK_BYTES,
) -> Iterator[bytes]:
    if max_chunk_bytes <= 0:
        raise ValueError("max_chunk_bytes must be greater than zero")

    columns = INVENTORY_EXPORT_COLUMNS[tab]
    records: Iterator[bytes]

    def encoded_records() -> Iterator[bytes]:
        if include_header:
            yield _csv_record_bytes(columns)
        for row in rows:
            record = _inventory_export_record(tab, row)
            yield _csv_record_bytes([_spreadsheet_safe_csv_value(record.get(column)) for column in columns])

    records = encoded_records()
    buffer = bytearray()
    for encoded_record in records:
        offset = 0
        while offset < len(encoded_record):
            capacity = max_chunk_bytes - len(buffer)
            take = min(capacity, len(encoded_record) - offset)
            buffer.extend(encoded_record[offset : offset + take])
            offset += take
            if len(buffer) == max_chunk_bytes:
                yield bytes(buffer)
                buffer.clear()
    if buffer:
        yield bytes(buffer)


def _inventory_export_id_column(tab: str):
    if tab == "items":
        return Item.id
    if tab == "resources":
        return Resource.id
    return Endpoint.id


def _inventory_export_batch(
    db: Session,
    project_id: uuid.UUID,
    tab: str,
    run_ids: list[uuid.UUID],
    query_groups: list[list[InventoryQueryClause]],
    *,
    include_deleted: bool,
    high_watermark: int | None,
    after_id: int | None,
    batch_size: int | None = None,
) -> list:
    effective_batch_size = batch_size or INVENTORY_EXPORT_BATCH_SIZE
    stmt = _inventory_export_statement(
        project_id,
        tab,
        run_ids,
        query_groups,
        include_deleted=include_deleted,
    )
    id_column = _inventory_export_id_column(tab)
    if high_watermark is not None:
        stmt = stmt.where(id_column <= high_watermark)
    if after_id is not None:
        stmt = stmt.where(id_column < after_id)
    return db.execute(stmt.limit(effective_batch_size)).all()


def _rollback_and_close_export_session(db: Session, *, context: str) -> None:
    try:
        db.rollback()
    except Exception:
        logger.exception("inventory_export_session_rollback_failed context=%s", context)
    finally:
        try:
            db.close()
        except Exception:
            logger.exception("inventory_export_session_close_failed context=%s", context)


def _load_inventory_export_batch_in_new_session(
    project_id: uuid.UUID,
    tab: str,
    run_ids: list[uuid.UUID],
    query_groups: list[list[InventoryQueryClause]],
    *,
    include_deleted: bool,
    high_watermark: int,
    after_id: int,
) -> list:
    batch_db = SessionLocal()
    try:
        return _inventory_export_batch(
            batch_db,
            project_id,
            tab,
            run_ids,
            query_groups,
            include_deleted=include_deleted,
            high_watermark=high_watermark,
            after_id=after_id,
        )
    finally:
        _rollback_and_close_export_session(batch_db, context="batch")


def _record_inventory_export_terminal_audit(
    *,
    action: str,
    project_id: uuid.UUID,
    auth: AuthContext,
    request_metadata: dict,
    metadata: dict,
) -> None:
    audit_db = SessionLocal()
    try:
        write_audit_event(
            audit_db,
            action=action,
            object_type="project_inventory",
            object_id=str(project_id),
            actor_user_id=auth.user_id,
            actor_token_id=auth.token_id,
            project_id=project_id,
            metadata={**request_metadata, **metadata},
        )
        audit_db.commit()
    except Exception:
        try:
            audit_db.rollback()
        except Exception:
            logger.exception("inventory_export_audit_rollback_failed action=%s", action)
        logger.exception("inventory_export_terminal_audit_failed action=%s", action)
    finally:
        try:
            audit_db.close()
        except Exception:
            logger.exception("inventory_export_audit_close_failed action=%s", action)


def _check_inventory_export_rate_limit(request: Request, auth: AuthContext) -> None:
    settings = get_settings()
    actor_key = str(auth.token_id or auth.user_id or "anonymous")
    rate_limiter.check(
        request,
        "inventory_export",
        limit=settings.api_inventory_export_rate_limit,
        window_seconds=settings.api_inventory_export_rate_window_seconds,
        actor_key=f"inventory-export:{actor_key}",
    )


def _record_first_batch_failure_audit_best_effort(
    db: Session,
    request: Request,
    auth: AuthContext,
    project_id: uuid.UUID,
    *,
    metadata: dict,
) -> None:
    try:
        db.rollback()
    except Exception:
        logger.exception("inventory_export_first_batch_rollback_failed")
    try:
        _audit_read(
            db,
            request,
            auth,
            project_id,
            action="PROJECT_INVENTORY_CSV_EXPORT_FAILED",
            metadata=metadata,
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            logger.exception("inventory_export_first_batch_audit_rollback_failed")
        logger.exception("inventory_export_first_batch_audit_failed")


def _inventory_csv_stream(
    *,
    project_id: uuid.UUID,
    tab: str,
    run_ids: list[uuid.UUID],
    query_groups: list[list[InventoryQueryClause]],
    include_deleted: bool,
    high_watermark: int,
    first_batch: list,
    export_id: str,
    auth: AuthContext,
    request_metadata: dict,
    scope_metadata: dict,
) -> Iterator[bytes]:
    started = time.monotonic()
    row_count = 0
    batch_count = 0
    outcome = "cancelled"
    failure_type: str | None = None
    batch = first_batch

    try:
        if batch:
            row_count += len(batch)
            batch_count += 1
        yield from _inventory_csv_chunks(tab, batch, include_header=True)

        while len(batch) == INVENTORY_EXPORT_BATCH_SIZE:
            last_id = int(batch[-1].id)
            batch = _load_inventory_export_batch_in_new_session(
                project_id,
                tab,
                run_ids,
                query_groups,
                include_deleted=include_deleted,
                high_watermark=high_watermark,
                after_id=last_id,
            )
            if not batch:
                break
            row_count += len(batch)
            batch_count += 1
            yield from _inventory_csv_chunks(tab, batch)
        outcome = "completed"
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "cancelled"
        raise
    except BaseException as exc:
        outcome = "failed"
        failure_type = type(exc).__name__
        raise
    finally:
        terminal_metadata = {
            **scope_metadata,
            "export_id": export_id,
            "result_count": row_count,
            "batch_count": batch_count,
            "duration_ms": max(0, int((time.monotonic() - started) * 1_000)),
        }
        if failure_type:
            terminal_metadata["failure_type"] = failure_type
        _record_inventory_export_terminal_audit(
            action=f"PROJECT_INVENTORY_CSV_EXPORT_{outcome.upper()}",
            project_id=project_id,
            auth=auth,
            request_metadata=request_metadata,
            metadata=terminal_metadata,
        )


def _next_inventory_csv_chunk(stream: Iterator[bytes]) -> tuple[bool, bytes | None]:
    try:
        return True, next(stream)
    except StopIteration:
        return False, None


async def _close_inventory_csv_stream(stream: Iterator[bytes]) -> None:
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    close_task = asyncio.create_task(run_in_threadpool(close))
    pending_cancellation: asyncio.CancelledError | None = None
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as exc:
            pending_cancellation = pending_cancellation or exc
            continue
        except Exception:
            logger.exception("inventory_export_stream_close_failed")
            break
    if close_task.done() and not close_task.cancelled():
        try:
            close_task.result()
        except Exception:
            logger.exception("inventory_export_stream_close_failed")
    if pending_cancellation is not None:
        raise pending_cancellation


class _InventoryCSVStreamingResponse(StreamingResponse):
    def __init__(
        self,
        stream: Iterator[bytes],
        *,
        initial_chunk: bytes,
        release_slot,
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        self._sync_stream = stream
        self._initial_chunk = initial_chunk
        self._release_slot = release_slot
        self._cleanup_task: asyncio.Task | None = None
        super().__init__(self._stream_chunks(), media_type=media_type, headers=headers)

    async def _cleanup_once(self) -> None:
        try:
            await _close_inventory_csv_stream(self._sync_stream)
        finally:
            try:
                self._release_slot()
            except Exception:
                logger.exception("inventory_export_admission_release_failed")

    async def _finish(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_once())

        pending_cancellation: asyncio.CancelledError | None = None
        while not self._cleanup_task.done():
            try:
                await asyncio.shield(self._cleanup_task)
            except asyncio.CancelledError as exc:
                pending_cancellation = pending_cancellation or exc
                continue
            except Exception:
                logger.exception("inventory_export_cleanup_failed")
                break
        if self._cleanup_task.done() and not self._cleanup_task.cancelled():
            try:
                self._cleanup_task.result()
            except Exception:
                logger.exception("inventory_export_cleanup_failed")
        if pending_cancellation is not None:
            raise pending_cancellation

    async def _stream_chunks(self):
        try:
            yield self._initial_chunk
            while True:
                has_chunk, chunk = await run_in_threadpool(
                    _next_inventory_csv_chunk,
                    self._sync_stream,
                )
                if not has_chunk:
                    break
                if chunk:
                    yield chunk
        finally:
            await self._finish()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette can observe a disconnect before it ever starts iterating
            # the body (or while sending response.start). The response owns the
            # cleanup as well as the iterator so that case cannot leak a slot.
            await self._finish()


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
    run_ids: str | None = Query(
        default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"
    ),
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
    run_ids: str | None = Query(
        default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"
    ),
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


@router.get("/export.csv")
def export_inventory_csv(
    project_id: uuid.UUID,
    request: Request,
    tab: str = Query(default="items", pattern="^(items|resources|endpoints)$"),
    query_dsl: str | None = Query(default=None, max_length=MAX_QUERY_DSL_CHARS),
    run_ids: str | None = Query(
        default=None,
        max_length=MAX_RUN_IDS_FILTER_CHARS,
        description="comma-separated run UUIDs",
    ),
    include_deleted: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_INVENTORY)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.VIEWER, auth, db)
    run_id_list = _parse_run_ids(run_ids)
    query_groups = parse_inventory_query(query_dsl)
    effective_include_deleted = include_deleted if tab == "items" else False
    _check_inventory_export_rate_limit(request, auth)
    if not inventory_export_admission.try_acquire():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inventory export capacity is temporarily exhausted; retry shortly",
            headers={"Retry-After": "5"},
        )

    export_id = str(uuid.uuid4())
    scope_metadata = {
        "tab": tab,
        "query_dsl": query_dsl,
        "run_ids": [str(run_id) for run_id in run_id_list],
        "all_eligible_runs": not run_id_list,
        "include_deleted": effective_include_deleted,
        "batch_size": INVENTORY_EXPORT_BATCH_SIZE,
        "max_chunk_bytes": INVENTORY_EXPORT_CHUNK_BYTES,
        "consistency": "high_watermark_bounded_live_non_snapshot",
    }
    slot_handed_to_stream = False
    sync_stream: Iterator[bytes] | None = None
    try:
        try:
            # The first descending page is also a bounded preflight and captures the
            # highest matching id. Later inserts receive higher ids and are excluded;
            # updates/deletes to existing ids remain intentionally live/non-snapshot.
            first_batch = _inventory_export_batch(
                db,
                project_id,
                tab,
                run_id_list,
                query_groups,
                include_deleted=effective_include_deleted,
                high_watermark=None,
                after_id=None,
            )
            db.rollback()
            high_watermark = int(first_batch[0].id) if first_batch else 0
            scope_metadata["high_watermark"] = high_watermark
            _audit_read(
                db,
                request,
                auth,
                project_id,
                action="PROJECT_INVENTORY_CSV_EXPORT_STARTED",
                metadata={**scope_metadata, "export_id": export_id, "result_count": 0},
            )
            db.commit()
        except Exception as exc:
            _record_first_batch_failure_audit_best_effort(
                db,
                request,
                auth,
                project_id,
                metadata={
                    **scope_metadata,
                    "export_id": export_id,
                    "result_count": 0,
                    "batch_count": 0,
                    "failure_type": type(exc).__name__,
                },
            )
            raise

        sync_stream = _inventory_csv_stream(
            project_id=project_id,
            tab=tab,
            run_ids=run_id_list,
            query_groups=query_groups,
            include_deleted=effective_include_deleted,
            high_watermark=high_watermark,
            first_batch=first_batch,
            export_id=export_id,
            auth=auth,
            request_metadata=request_meta(request),
            scope_metadata=scope_metadata,
        )
        has_initial_chunk, initial_chunk = _next_inventory_csv_chunk(sync_stream)
        if not has_initial_chunk or initial_chunk is None:
            raise RuntimeError("inventory CSV stream did not produce a header")
        filename = f"share-sentinel-inventory-{tab}-{project_id}.csv"
        response = _InventoryCSVStreamingResponse(
            sync_stream,
            initial_chunk=initial_chunk,
            release_slot=inventory_export_admission.release,
            media_type="text/csv",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
                "X-Share-Sentinel-Export-Consistency": "high-watermark-bounded-live-non-snapshot",
                "X-Share-Sentinel-Export-High-Watermark": str(high_watermark),
            },
        )
        slot_handed_to_stream = True
        return response
    finally:
        if not slot_handed_to_stream:
            try:
                if sync_stream is not None:
                    sync_stream.close()
            except Exception:
                logger.exception("inventory_export_pre_stream_close_failed")
            finally:
                inventory_export_admission.release()


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
    run_ids: str | None = Query(
        default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"
    ),
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
            Resource.permission_summary.label("access_evidence_summary"),
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
        stmt = stmt.where(
            or_(
                Endpoint.endpoint_key.ilike(pattern, escape="\\"),
                Endpoint.hostname.ilike(pattern, escape="\\"),
                Endpoint.ip.ilike(pattern, escape="\\"),
            )
        )
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
            "access_evidence_summary": build_access_evidence_summary(
                getattr(row, "access_evidence_summary", None),
                row.access_level,
                row.access_capabilities,
                getattr(row, "exposure", None),
            ),
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
            "provider": getattr(row, "provider", None) or share_type_from_resource_type(row.resource_type),
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
    run_ids: str | None = Query(
        default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"
    ),
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
            Resource.permission_summary.label("access_evidence_summary"),
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
        stmt = stmt.where(
            or_(Endpoint.endpoint_key.ilike(pattern, escape="\\"), Endpoint.hostname.ilike(pattern, escape="\\"))
        )
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
            "access_evidence_summary": build_access_evidence_summary(
                getattr(row, "access_evidence_summary", None),
                row.access_level,
                row.access_capabilities,
                getattr(row, "exposure", None),
            ),
            "share_type": share_type_from_resource_type(row.resource_type),
            "resource_type": row.resource_type.value if hasattr(row.resource_type, "value") else row.resource_type,
            "provider": getattr(row, "provider", None) or share_type_from_resource_type(row.resource_type),
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
    run_ids: str | None = Query(
        default=None, max_length=MAX_RUN_IDS_FILTER_CHARS, description="comma-separated run UUIDs"
    ),
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
