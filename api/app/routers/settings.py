import asyncio
import csv
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import String, and_, case, cast, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.config import get_settings
from app.csv_utils import spreadsheet_safe_csv_value
from app.db import SessionLocal, escape_like, get_db
from app.deps import AuthContext, get_auth_context, request_meta, require_sysadmin, require_token_scopes
from app.enums import ProjectRole, RunStatus
from app.locking import lock_project_admin_guard
from app.models import ApiToken, AuditEvent, Project, ProjectMember, ScanRun, User
from app.pagination import (
    KeysetColumn,
    apply_keyset_pagination,
    paginate_rows,
    parse_datetime_cursor_value,
    parse_int_cursor_value,
    parse_uuid_cursor_value,
)
from app.routers import users as users_router
from app.schemas import (
    ApiTokenAdminCreateIn,
    ApiTokenAdminCreateOut,
    ApiTokenAdminOut,
    ApiTokenAdminUpdateIn,
    AuditEventOut,
    ProjectDeleteIn,
    ProjectMembershipOut,
    ProjectMembershipUpsertIn,
    ProjectOut,
    ProjectUpdateIn,
    SecuritySettingsOut,
    SettingsProjectArtifactDeleteFailureOut,
    SettingsProjectBlockingRunOut,
    SettingsProjectCatalogItemOut,
    SettingsProjectDeleteOut,
    SettingsProjectDetailOut,
    UserAssignAllProjectsIn,
)
from app.security import hash_external_token, random_token
from app.services.audit import write_audit_event
from app.services.storage import delete_object
from app.token_scopes import (
    ALLOWED_API_TOKEN_SCOPES,
    SCOPE_READ_AUDIT,
    SCOPE_READ_MEMBERS,
    SCOPE_READ_TOKENS,
    SCOPE_WRITE_MEMBERS,
    SCOPE_WRITE_PROJECTS,
    SCOPE_WRITE_TOKENS,
    default_scopes_for_project_role,
    normalize_token_scopes,
)

router = APIRouter(prefix="/settings", tags=["settings"])
logger = logging.getLogger("share_sentinel.settings")
MAX_SETTINGS_SEARCH_CHARS = 512
AUDIT_EXPORT_BATCH_SIZE = 200
AUDIT_EXPORT_FIELDS = (
    "id",
    "ts",
    "action",
    "object_type",
    "object_id",
    "actor_email",
    "actor_user_id",
    "actor_token_name",
    "actor_token_id",
    "project_name",
    "project_id",
    "metadata",
)


async def _close_audit_export_stream(stream: Iterator[bytes]) -> None:
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
            logger.exception("audit_export_stream_close_failed")
            break
    if close_task.done() and not close_task.cancelled():
        try:
            close_task.result()
        except Exception:
            logger.exception("audit_export_stream_close_failed")
    if pending_cancellation is not None:
        raise pending_cancellation


class _AuditStreamingResponse(StreamingResponse):
    """Close the sync generator on disconnect so its terminal audit is durable."""

    def __init__(self, stream: Iterator[bytes], **kwargs) -> None:
        self._sync_stream = stream
        super().__init__(stream, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _close_audit_export_stream(self._sync_stream)
SETTINGS_API_TOKEN_CURSOR = (
    KeysetColumn(
        "created_at",
        ApiToken.created_at,
        direction="desc",
        parser=parse_datetime_cursor_value,
        getter=lambda row: row.ApiToken.created_at,
    ),
    KeysetColumn(
        "id",
        ApiToken.id,
        direction="desc",
        parser=parse_uuid_cursor_value,
        getter=lambda row: row.ApiToken.id,
    ),
)
SETTINGS_PROJECT_CURSOR = (
    KeysetColumn("project_name", Project.name, getter=lambda row: row.project_name),
    KeysetColumn(
        "project_id",
        Project.id,
        parser=parse_uuid_cursor_value,
        getter=lambda row: row.project_id,
    ),
)
SETTINGS_AUDIT_CURSOR = (
    KeysetColumn(
        "ts",
        AuditEvent.ts,
        direction="desc",
        parser=parse_datetime_cursor_value,
        getter=lambda row: row.AuditEvent.ts,
    ),
    KeysetColumn(
        "id",
        AuditEvent.id,
        direction="desc",
        parser=parse_int_cursor_value,
        getter=lambda row: row.AuditEvent.id,
    ),
)
SETTINGS_PROJECT_MEMBERSHIP_CURSOR = (
    KeysetColumn("project_name", Project.name),
    KeysetColumn("user_email", User.email),
    KeysetColumn("project_id", ProjectMember.project_id, parser=parse_uuid_cursor_value),
    KeysetColumn("user_id", ProjectMember.user_id, parser=parse_uuid_cursor_value),
)

ROLE_ORDER = {
    ProjectRole.VIEWER: 1,
    ProjectRole.OPERATOR: 2,
    ProjectRole.ADMIN: 3,
}
PROJECT_DELETE_BLOCKING_STATUSES = (RunStatus.UPLOADED, RunStatus.INGESTING)


def _run_lock_key(run_id: uuid.UUID) -> int:
    return run_id.int % (2**63 - 1)


def _try_lock_run_for_mutation(db: Session, run_id: uuid.UUID) -> bool:
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _run_lock_key(run_id)}).scalar())


def _global_audit_stmt(q: str | None, project_id: uuid.UUID | None = None):
    stmt = (
        select(
            AuditEvent,
            func.coalesce(AuditEvent.actor_user_ref, AuditEvent.actor_user_id).label("actor_user_ref"),
            func.coalesce(AuditEvent.actor_email_snapshot, User.email).label("actor_email"),
            func.coalesce(AuditEvent.actor_token_ref, AuditEvent.actor_token_id).label("actor_token_ref"),
            func.coalesce(AuditEvent.actor_token_name_snapshot, ApiToken.name).label("actor_token_name"),
            func.coalesce(AuditEvent.project_ref, AuditEvent.project_id).label("project_ref"),
            func.coalesce(AuditEvent.project_name_snapshot, Project.name).label("project_name"),
        )
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .outerjoin(ApiToken, ApiToken.id == AuditEvent.actor_token_id)
        .outerjoin(Project, Project.id == AuditEvent.project_id)
    )
    if project_id is not None:
        stmt = stmt.where(
            or_(
                AuditEvent.project_ref == project_id,
                (AuditEvent.project_ref.is_(None) & (AuditEvent.project_id == project_id)),
            )
        )
    if q:
        escaped = escape_like(q.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                AuditEvent.action.ilike(pattern, escape="\\"),
                AuditEvent.object_type.ilike(pattern, escape="\\"),
                AuditEvent.object_id.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
                AuditEvent.actor_email_snapshot.ilike(pattern, escape="\\"),
                ApiToken.name.ilike(pattern, escape="\\"),
                AuditEvent.actor_token_name_snapshot.ilike(pattern, escape="\\"),
                Project.name.ilike(pattern, escape="\\"),
                AuditEvent.project_name_snapshot.ilike(pattern, escape="\\"),
                cast(AuditEvent.actor_user_ref, String).ilike(pattern, escape="\\"),
                cast(AuditEvent.actor_token_ref, String).ilike(pattern, escape="\\"),
                cast(AuditEvent.project_ref, String).ilike(pattern, escape="\\"),
            )
        )
    return stmt


def _serialize_audit_rows(rows) -> list[dict]:
    items: list[dict] = []
    for row in rows:
        event = row.AuditEvent
        items.append(
            AuditEventOut(
                id=event.id,
                ts=event.ts,
                actor_user_id=getattr(row, "actor_user_ref", None)
                or getattr(event, "actor_user_ref", None)
                or event.actor_user_id,
                actor_email=getattr(row, "actor_email", None)
                or getattr(event, "actor_email_snapshot", None),
                actor_token_id=getattr(row, "actor_token_ref", None)
                or getattr(event, "actor_token_ref", None)
                or event.actor_token_id,
                actor_token_name=getattr(row, "actor_token_name", None)
                or getattr(event, "actor_token_name_snapshot", None),
                project_id=getattr(row, "project_ref", None)
                or getattr(event, "project_ref", None)
                or event.project_id,
                project_name=getattr(row, "project_name", None)
                or getattr(event, "project_name_snapshot", None),
                action=event.action,
                object_type=event.object_type,
                object_id=event.object_id,
                metadata=event.metadata_json,
            ).model_dump(mode="json")
        )
    return items


def _audit_export_csv_row(item: dict) -> bytes:
    values = {
        "id": item["id"],
        "ts": item["ts"],
        "action": item["action"],
        "object_type": item["object_type"],
        "object_id": item["object_id"],
        "actor_email": item["actor_email"] or "",
        "actor_user_id": item["actor_user_id"] or "",
        "actor_token_name": item["actor_token_name"] or "",
        "actor_token_id": item["actor_token_id"] or "",
        "project_name": item["project_name"] or "",
        "project_id": item["project_id"] or "",
        "metadata": json.dumps(item["metadata"] or {}, ensure_ascii=True, sort_keys=True),
    }
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerow(
        [spreadsheet_safe_csv_value(values[field]) for field in AUDIT_EXPORT_FIELDS]
    )
    return output.getvalue().encode("utf-8")


def _record_audit_export_terminal(
    *,
    action: str,
    auth: AuthContext,
    actor_email_snapshot: str | None,
    actor_token_name_snapshot: str | None = None,
    project_id: uuid.UUID | None,
    project_name_snapshot: str | None = None,
    request_metadata: dict,
    metadata: dict,
) -> None:
    db = SessionLocal()
    try:
        write_audit_event(
            db,
            action=action,
            object_type="system",
            object_id="audit",
            actor_user_ref=auth.user_id,
            actor_email_snapshot=actor_email_snapshot,
            actor_token_ref=auth.token_id,
            actor_token_name_snapshot=actor_token_name_snapshot,
            project_ref=project_id,
            project_name_snapshot=project_name_snapshot,
            metadata={**request_metadata, **metadata},
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            logger.exception("audit_export_terminal_rollback_failed action=%s", action)
        logger.exception("audit_export_terminal_audit_failed action=%s", action)
    finally:
        try:
            db.close()
        except Exception:
            logger.exception("audit_export_terminal_session_close_failed action=%s", action)


def _load_audit_export_batch(
    *,
    q: str | None,
    project_id: uuid.UUID | None,
    snapshot_id: int,
    before_ts: datetime | None,
    before_id: int | None,
    limit: int,
) -> tuple[list[dict], datetime | None, int | None]:
    db = SessionLocal()
    try:
        stmt = _global_audit_stmt(q, project_id=project_id).where(AuditEvent.id <= snapshot_id)
        if before_ts is not None and before_id is not None:
            stmt = stmt.where(
                or_(
                    AuditEvent.ts < before_ts,
                    and_(AuditEvent.ts == before_ts, AuditEvent.id < before_id),
                )
            )
        rows = db.execute(
            stmt.order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(limit)
        ).all()
        items = _serialize_audit_rows(rows)
        if not rows:
            return items, None, None
        last_event = rows[-1].AuditEvent
        return items, last_event.ts, int(last_event.id)
    finally:
        try:
            db.rollback()
        except Exception:
            logger.exception("audit_export_batch_rollback_failed")
        try:
            db.close()
        except Exception:
            logger.exception("audit_export_batch_session_close_failed")


def _audit_export_chunks(
    *,
    q: str | None,
    project_id: uuid.UUID | None,
    export_format: str,
    max_rows: int,
    expected_rows: int,
    truncated: bool,
    snapshot_id: int,
    auth: AuthContext,
    actor_email_snapshot: str | None,
    actor_token_name_snapshot: str | None = None,
    project_name_snapshot: str | None = None,
    request_metadata: dict,
) -> Iterator[bytes]:
    exported_count = 0
    before_ts: datetime | None = None
    before_id: int | None = None
    completed = False
    terminal_recorded = False
    try:
        if export_format == "csv":
            output = StringIO(newline="")
            csv.writer(output, lineterminator="\r\n").writerow(AUDIT_EXPORT_FIELDS)
            yield output.getvalue().encode("utf-8")
        else:
            yield b"[\n"

        first_json_item = True
        while exported_count < expected_rows:
            batch_limit = min(AUDIT_EXPORT_BATCH_SIZE, expected_rows - exported_count)
            items, next_before_ts, next_before_id = _load_audit_export_batch(
                q=q,
                project_id=project_id,
                snapshot_id=snapshot_id,
                before_ts=before_ts,
                before_id=before_id,
                limit=batch_limit,
            )
            if not items:
                break

            for item in items:
                if export_format == "csv":
                    yield _audit_export_csv_row(item)
                else:
                    prefix = b"" if first_json_item else b",\n"
                    yield prefix + json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
                    first_json_item = False
                exported_count += 1

            before_ts = next_before_ts
            before_id = next_before_id
            if len(items) < batch_limit:
                break

        if export_format == "json":
            yield b"\n]\n"
        _record_audit_export_terminal(
            action="SETTINGS_AUDIT_EXPORTED",
            auth=auth,
            actor_email_snapshot=actor_email_snapshot,
            actor_token_name_snapshot=actor_token_name_snapshot,
            project_id=project_id,
            project_name_snapshot=project_name_snapshot,
            request_metadata=request_metadata,
            metadata={
                "format": export_format,
                "max_rows": max_rows,
                "snapshot_id": snapshot_id,
                "exported_count": exported_count,
                "truncated": truncated,
            },
        )
        terminal_recorded = True
        completed = True
    except GeneratorExit:
        raise
    except BaseException as exc:
        _record_audit_export_terminal(
            action="SETTINGS_AUDIT_EXPORT_FAILED",
            auth=auth,
            actor_email_snapshot=actor_email_snapshot,
            actor_token_name_snapshot=actor_token_name_snapshot,
            project_id=project_id,
            project_name_snapshot=project_name_snapshot,
            request_metadata=request_metadata,
            metadata={
                "format": export_format,
                "max_rows": max_rows,
                "snapshot_id": snapshot_id,
                "exported_count": exported_count,
                "truncated": truncated,
                "error_type": type(exc).__name__[:120],
            },
        )
        terminal_recorded = True
        raise
    finally:
        if not completed and not terminal_recorded:
            _record_audit_export_terminal(
                action="SETTINGS_AUDIT_EXPORT_INTERRUPTED",
                auth=auth,
                actor_email_snapshot=actor_email_snapshot,
                actor_token_name_snapshot=actor_token_name_snapshot,
                project_id=project_id,
                project_name_snapshot=project_name_snapshot,
                request_metadata=request_metadata,
                metadata={
                    "format": export_format,
                    "max_rows": max_rows,
                    "snapshot_id": snapshot_id,
                    "exported_count": exported_count,
                    "truncated": truncated,
                },
            )


def _project_catalog_stmt(q: str | None = None, project_id: uuid.UUID | None = None):
    member_counts = (
        select(
            ProjectMember.project_id.label("project_id"),
            func.count(ProjectMember.user_id).label("member_count"),
            func.sum(case((ProjectMember.role == ProjectRole.ADMIN, 1), else_=0)).label("admin_count"),
        )
        .group_by(ProjectMember.project_id)
        .subquery()
    )
    token_counts = (
        select(
            ApiToken.project_id.label("project_id"),
            func.count(ApiToken.id).label("token_count"),
            func.sum(case((ApiToken.revoked_at.is_(None), 1), else_=0)).label("active_token_count"),
        )
        .group_by(ApiToken.project_id)
        .subquery()
    )
    run_counts = (
        select(
            ScanRun.project_id.label("project_id"),
            func.count(ScanRun.id).label("run_count"),
            func.sum(case((ScanRun.artifact_key.is_not(None), 1), else_=0)).label("artifact_count"),
            func.sum(case((ScanRun.status.in_(PROJECT_DELETE_BLOCKING_STATUSES), 1), else_=0)).label("blocking_run_count"),
            func.max(ScanRun.created_at).label("last_run_at"),
        )
        .group_by(ScanRun.project_id)
        .subquery()
    )

    stmt = (
        select(
            Project.id.label("project_id"),
            Project.name.label("project_name"),
            Project.created_at.label("created_at"),
            func.coalesce(member_counts.c.member_count, 0).label("member_count"),
            func.coalesce(member_counts.c.admin_count, 0).label("admin_count"),
            func.coalesce(token_counts.c.token_count, 0).label("token_count"),
            func.coalesce(token_counts.c.active_token_count, 0).label("active_token_count"),
            func.coalesce(run_counts.c.run_count, 0).label("run_count"),
            func.coalesce(run_counts.c.artifact_count, 0).label("artifact_count"),
            func.coalesce(run_counts.c.blocking_run_count, 0).label("blocking_run_count"),
            run_counts.c.last_run_at.label("last_run_at"),
        )
        .outerjoin(member_counts, member_counts.c.project_id == Project.id)
        .outerjoin(token_counts, token_counts.c.project_id == Project.id)
        .outerjoin(run_counts, run_counts.c.project_id == Project.id)
    )
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    if q:
        escaped = escape_like(q.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Project.name.ilike(pattern, escape="\\"),
                cast(Project.id, String).ilike(pattern, escape="\\"),
            )
        )
    return stmt


def _project_catalog_item_out(row) -> SettingsProjectCatalogItemOut:
    blocking_run_count = int(row.blocking_run_count or 0)
    return SettingsProjectCatalogItemOut(
        id=row.project_id,
        name=row.project_name,
        created_at=row.created_at,
        member_count=int(row.member_count or 0),
        admin_count=int(row.admin_count or 0),
        token_count=int(row.token_count or 0),
        active_token_count=int(row.active_token_count or 0),
        run_count=int(row.run_count or 0),
        artifact_count=int(row.artifact_count or 0),
        blocking_run_count=blocking_run_count,
        has_blocking_runs=blocking_run_count > 0,
        last_run_at=row.last_run_at,
    )


def _project_detail_out(db: Session, project_id: uuid.UUID) -> SettingsProjectDetailOut:
    row = db.execute(_project_catalog_stmt(project_id=project_id)).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    status_counts = {run_status.value: 0 for run_status in RunStatus}
    status_rows = db.execute(
        select(ScanRun.status.label("status"), func.count(ScanRun.id).label("count"))
        .where(ScanRun.project_id == project_id)
        .group_by(ScanRun.status)
    ).all()
    for status_row in status_rows:
        status_counts[status_row.status.value] = int(status_row.count or 0)

    blocking_runs = db.execute(
        select(ScanRun)
        .where(ScanRun.project_id == project_id, ScanRun.status.in_(PROJECT_DELETE_BLOCKING_STATUSES))
        .order_by(ScanRun.created_at.desc(), ScanRun.id.desc())
    ).scalars().all()

    summary = _project_catalog_item_out(row)
    return SettingsProjectDetailOut(
        **summary.model_dump(),
        run_status_counts=status_counts,
        blocking_runs=[
            SettingsProjectBlockingRunOut(
                id=run.id,
                name=run.name,
                status=run.status,
                created_at=run.created_at,
            )
            for run in blocking_runs
        ],
    )


def _delete_artifacts_best_effort(keys: list[str]) -> list[SettingsProjectArtifactDeleteFailureOut]:
    failures: list[SettingsProjectArtifactDeleteFailureOut] = []
    for key in keys:
        try:
            delete_object(key)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            logger.warning("failed to delete project artifact key=%s", key, exc_info=True)
            failures.append(
                SettingsProjectArtifactDeleteFailureOut(
                    artifact_key=key,
                    error=str(exc) or exc.__class__.__name__,
                )
            )
    return failures


@router.get("/projects", response_model=list[ProjectOut])
def list_all_projects(
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    projects = db.execute(select(Project).order_by(Project.created_at.desc())).scalars().all()
    return [ProjectOut(id=project.id, name=project.name, created_at=project.created_at) for project in projects]


@router.get("/projects/catalog", response_model=dict)
def list_project_catalog(
    q: str | None = Query(default=None, max_length=MAX_SETTINGS_SEARCH_CHARS),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    stmt = apply_keyset_pagination(_project_catalog_stmt(q=q), SETTINGS_PROJECT_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).all(), SETTINGS_PROJECT_CURSOR, limit)
    items = [_project_catalog_item_out(row).model_dump(mode="json") for row in rows]
    return {"items": items, "next_cursor": next_cursor}


@router.get("/projects/{project_id}", response_model=SettingsProjectDetailOut)
def get_project_detail(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    return _project_detail_out(db, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def rename_project(
    project_id: uuid.UUID,
    payload: ProjectUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_PROJECTS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    previous_name = project.name
    project.name = payload.name
    db.add(project)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="project name already exists") from exc

    write_audit_event(
        db,
        action="SETTINGS_PROJECT_RENAMED",
        object_type="project",
        object_id=str(project_id),
        actor_user_id=auth.user_id,
        project_ref=project_id,
        metadata={**request_meta(request), "previous_name": previous_name, "name": project.name},
    )
    db.commit()
    db.refresh(project)
    return ProjectOut(id=project.id, name=project.name, created_at=project.created_at)


@router.delete("/projects/{project_id}", response_model=SettingsProjectDeleteOut)
def delete_project(
    project_id: uuid.UUID,
    payload: ProjectDeleteIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_PROJECTS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    lock_project_admin_guard(db, project_id)
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if payload.confirm_name != project.name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project name confirmation does not match")

    runs = db.execute(
        select(ScanRun).where(ScanRun.project_id == project_id).order_by(ScanRun.created_at.desc(), ScanRun.id.desc())
    ).scalars().all()

    if any(run.status in PROJECT_DELETE_BLOCKING_STATUSES for run in runs):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project has runs in UPLOADED or INGESTING state",
        )

    for run in runs:
        if not _try_lock_run_for_mutation(db, run.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="project has a run that cannot be locked for deletion",
            )

    artifact_keys = [run.artifact_key for run in runs if run.artifact_key]
    db.delete(project)
    write_audit_event(
        db,
        action="SETTINGS_PROJECT_DELETED",
        object_type="project",
        object_id=str(project_id),
        actor_user_id=auth.user_id,
        project_ref=project_id,
        project_name_snapshot=project.name,
        metadata={
            **request_meta(request),
            "project_id": str(project_id),
            "project_name": project.name,
            "deleted_run_count": len(runs),
            "deleted_artifact_count": len(artifact_keys),
        },
    )
    db.commit()

    artifact_delete_failures = _delete_artifacts_best_effort(artifact_keys)
    return SettingsProjectDeleteOut(
        project_id=project_id,
        project_name=project.name,
        deleted_run_count=len(runs),
        deleted_artifact_count=len(artifact_keys),
        artifact_delete_failures=artifact_delete_failures,
    )


@router.get("/overview", response_model=dict)
def settings_overview(
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS, SCOPE_READ_TOKENS, SCOPE_READ_AUDIT)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    settings = get_settings()

    users_total = int(db.execute(select(func.count(User.id))).scalar() or 0)
    users_active = int(db.execute(select(func.count(User.id)).where(User.is_active.is_(True))).scalar() or 0)
    users_pending = int(db.execute(select(func.count(User.id)).where(User.is_approved.is_(False))).scalar() or 0)
    users_sysadmins = int(db.execute(select(func.count(User.id)).where(User.is_sysadmin.is_(True))).scalar() or 0)

    tokens_total = int(db.execute(select(func.count(ApiToken.id))).scalar() or 0)
    tokens_active = int(db.execute(select(func.count(ApiToken.id)).where(ApiToken.revoked_at.is_(None))).scalar() or 0)
    tokens_never_expires = int(
        db.execute(
            select(func.count(ApiToken.id)).where(ApiToken.revoked_at.is_(None), ApiToken.expires_at.is_(None))
        ).scalar()
        or 0
    )
    tokens_last_active_at = db.execute(
        select(func.max(ApiToken.last_used_at)).where(ApiToken.revoked_at.is_(None))
    ).scalar()
    projects_total = int(db.execute(select(func.count(Project.id))).scalar() or 0)

    recent_audit_rows = db.execute(
        _global_audit_stmt(None).order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(5)
    ).all()

    security = SecuritySettingsOut(
        allow_self_registration=settings.allow_self_registration,
        auth_require_csrf=settings.auth_require_csrf,
        auth_cookie_secure=settings.auth_cookie_secure,
        allow_never_expiring_api_tokens=settings.allow_never_expiring_api_tokens,
        password_min_length=settings.password_min_length,
        password_require_lowercase=settings.password_require_lowercase,
        password_require_uppercase=settings.password_require_uppercase,
        password_require_number=settings.password_require_number,
        password_require_special=settings.password_require_special,
        auth_login_max_attempts=settings.auth_login_max_attempts,
        auth_login_window_seconds=settings.auth_login_window_seconds,
        auth_login_lockout_seconds=settings.auth_login_lockout_seconds,
        default_api_token_expiry_days=settings.default_api_token_expiry_days,
        rbac_enabled=True,
        mfa_enabled=False,
        sso_enabled=False,
        scim_enabled=False,
        password_history_enforced=False,
        session_idle_timeout_minutes=None,
    )

    return {
        "security": security.model_dump(mode="json"),
        "users": {
            "total": users_total,
            "active": users_active,
            "pending": users_pending,
            "sysadmins": users_sysadmins,
        },
        "tokens": {
            "total": tokens_total,
            "active": tokens_active,
            "revoked": max(0, tokens_total - tokens_active),
            "never_expires": tokens_never_expires,
            "last_active_at": tokens_last_active_at.isoformat() if tokens_last_active_at else None,
        },
        "projects": {"total": projects_total},
        "recent_audit": _serialize_audit_rows(recent_audit_rows),
    }


@router.get("/api-tokens", response_model=dict)
def list_all_api_tokens(
    request: Request,
    q: str | None = Query(default=None, max_length=MAX_SETTINGS_SEARCH_CHARS),
    project_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_TOKENS)),
    __: User = Depends(require_sysadmin),
):
    _ = __

    stmt = (
        select(ApiToken, User.email.label("user_email"), Project.name.label("project_name"))
        .join(User, User.id == ApiToken.user_id)
        .join(Project, Project.id == ApiToken.project_id)
    )
    if project_id is not None:
        stmt = stmt.where(ApiToken.project_id == project_id)
    if q:
        escaped = escape_like(q.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                ApiToken.name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
                Project.name.ilike(pattern, escape="\\"),
                cast(ApiToken.id, String).ilike(pattern, escape="\\"),
            )
        )

    stmt = apply_keyset_pagination(stmt, SETTINGS_API_TOKEN_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).all(), SETTINGS_API_TOKEN_CURSOR, limit)
    items = [
        ApiTokenAdminOut(
            id=row.ApiToken.id,
            user_id=row.ApiToken.user_id,
            user_email=row.user_email,
            project_id=row.ApiToken.project_id,
            project_name=row.project_name,
            name=row.ApiToken.name,
            role=row.ApiToken.role,
            scopes=normalize_token_scopes(row.ApiToken.scopes),
            last_used_at=row.ApiToken.last_used_at,
            expires_at=row.ApiToken.expires_at,
            created_at=row.ApiToken.created_at,
            revoked_at=row.ApiToken.revoked_at,
        ).model_dump(mode="json")
        for row in rows
    ]

    write_audit_event(
        db,
        action="SETTINGS_API_TOKENS_LISTED",
        object_type="system",
        object_id="api_tokens",
        actor_user_id=auth.user_id,
        metadata={
            **request_meta(request),
            "q": q,
            "project_id": str(project_id) if project_id else None,
            "limit": limit,
            "cursor": cursor,
            "result_count": len(items),
        },
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor}


@router.post("/api-tokens", response_model=ApiTokenAdminCreateOut)
def create_any_api_token(
    payload: ApiTokenAdminCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_TOKENS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    user = db.get(User, payload.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user is disabled")
    if not user.is_approved:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user is not approved")

    project = db.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    membership = db.get(ProjectMember, {"project_id": payload.project_id, "user_id": payload.user_id})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user must be a project member before creating a token")
    if ROLE_ORDER[payload.role] > ROLE_ORDER[membership.role]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="token role cannot exceed membership role")

    settings = get_settings()
    expires_in_days = payload.expires_in_days
    if expires_in_days is None:
        expires_in_days = settings.default_api_token_expiry_days
    if expires_in_days == 0 and not settings.allow_never_expiring_api_tokens:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="never-expiring api tokens are disabled")
    expires_at = datetime.now(tz=UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    scopes = normalize_token_scopes(payload.scopes)
    if not scopes:
        scopes = default_scopes_for_project_role(payload.role)
    _enforce_scope_policy(user, payload.role, scopes)

    token_raw = random_token(48)
    token_hash = hash_external_token(token_raw)
    token = ApiToken(
        user_id=payload.user_id,
        project_id=payload.project_id,
        token_hash=token_hash,
        name=payload.name,
        role=payload.role,
        scopes=scopes,
        expires_at=expires_at,
        revoked_at=None,
    )
    db.add(token)
    db.flush()

    write_audit_event(
        db,
        action="SETTINGS_API_TOKEN_CREATED",
        object_type="api_token",
        object_id=str(token.id),
        actor_user_id=auth.user_id,
        project_id=payload.project_id,
        metadata={
            **request_meta(request),
            "target_user_id": str(payload.user_id),
            "role": payload.role.value,
            "scopes": scopes,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    db.commit()
    db.refresh(token)
    return ApiTokenAdminCreateOut(
        token=token_raw,
        token_meta=_api_token_admin_out(token, user.email, project.name),
    )


@router.patch("/api-tokens/{token_id}", response_model=ApiTokenAdminOut)
def update_any_api_token(
    token_id: uuid.UUID,
    payload: ApiTokenAdminUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_TOKENS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    settings = get_settings()
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    if token.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot update a revoked token")
    if payload.never_expires and payload.expires_in_days is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot set both never_expires and expires_in_days")
    if payload.never_expires and not settings.allow_never_expiring_api_tokens:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="never-expiring api tokens are disabled")

    owner = db.get(User, token.user_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token owner not found")
    project = db.get(Project, token.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token project not found")

    membership = db.get(ProjectMember, {"project_id": token.project_id, "user_id": token.user_id})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="token owner is no longer a project member")

    next_role = payload.role if payload.role is not None else token.role
    if ROLE_ORDER[next_role] > ROLE_ORDER[membership.role]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="token role cannot exceed membership role")

    if payload.name is not None:
        token.name = payload.name
    token.role = next_role
    if payload.scopes is not None:
        next_scopes = normalize_token_scopes(payload.scopes)
        token.scopes = next_scopes or default_scopes_for_project_role(next_role)
    elif payload.role is not None:
        # Keep scopes aligned with an updated role when scopes are not explicitly set.
        token.scopes = default_scopes_for_project_role(next_role)
    if payload.never_expires:
        token.expires_at = None
    elif payload.expires_in_days is not None:
        token.expires_at = datetime.now(tz=UTC) + timedelta(days=payload.expires_in_days)

    normalized_scopes = normalize_token_scopes(token.scopes)
    _enforce_scope_policy(owner, token.role, normalized_scopes)

    db.add(token)
    write_audit_event(
        db,
        action="SETTINGS_API_TOKEN_UPDATED",
        object_type="api_token",
        object_id=str(token.id),
        actor_user_id=auth.user_id,
        project_id=token.project_id,
        metadata={
            **request_meta(request),
            "name": token.name,
            "role": token.role.value,
            "scopes": normalized_scopes,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        },
    )
    db.commit()
    db.refresh(token)
    return _api_token_admin_out(token, owner.email, project.name)


@router.post("/api-tokens/{token_id}/rotate", response_model=ApiTokenAdminCreateOut)
def rotate_any_api_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_TOKENS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    if token.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot rotate a revoked token")

    owner = db.get(User, token.user_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token owner not found")
    project = db.get(Project, token.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token project not found")

    next_raw = random_token(48)
    token.token_hash = hash_external_token(next_raw)
    token.last_used_at = None
    db.add(token)
    write_audit_event(
        db,
        action="SETTINGS_API_TOKEN_ROTATED",
        object_type="api_token",
        object_id=str(token.id),
        actor_user_id=auth.user_id,
        project_id=token.project_id,
        metadata=request_meta(request),
    )
    db.commit()
    db.refresh(token)
    return ApiTokenAdminCreateOut(
        token=next_raw,
        token_meta=_api_token_admin_out(token, owner.email, project.name),
    )


@router.delete("/api-tokens/{token_id}")
def revoke_any_api_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_TOKENS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")

    token.revoked_at = datetime.now(tz=UTC)
    db.add(token)
    write_audit_event(
        db,
        action="SETTINGS_API_TOKEN_REVOKED",
        object_type="api_token",
        object_id=str(token.id),
        actor_user_id=auth.user_id,
        project_id=token.project_id,
        metadata=request_meta(request),
    )
    db.commit()
    return {"ok": True}


@router.get("/api-token-scopes")
def list_api_token_scope_catalog(
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_TOKENS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    return {
        "allowed_scopes": sorted(ALLOWED_API_TOKEN_SCOPES),
        "defaults_by_role": {
            ProjectRole.VIEWER.value: default_scopes_for_project_role(ProjectRole.VIEWER),
            ProjectRole.OPERATOR.value: default_scopes_for_project_role(ProjectRole.OPERATOR),
            ProjectRole.ADMIN.value: default_scopes_for_project_role(ProjectRole.ADMIN),
        },
    }


@router.get("/audit", response_model=dict)
def list_global_audit(
    request: Request,
    q: str | None = Query(default=None, max_length=MAX_SETTINGS_SEARCH_CHARS),
    project_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_AUDIT)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    stmt = _global_audit_stmt(q, project_id=project_id)
    stmt = apply_keyset_pagination(stmt, SETTINGS_AUDIT_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).all(), SETTINGS_AUDIT_CURSOR, limit)
    items = _serialize_audit_rows(rows)

    write_audit_event(
        db,
        action="SETTINGS_AUDIT_LISTED",
        object_type="system",
        object_id="audit",
        actor_user_id=auth.user_id,
        metadata={
            **request_meta(request),
            "q": q,
            "project_id": str(project_id) if project_id else None,
            "limit": limit,
            "cursor": cursor,
            "result_count": len(items),
        },
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor}


@router.get("/audit/export")
def export_global_audit(
    request: Request,
    q: str | None = Query(default=None, max_length=MAX_SETTINGS_SEARCH_CHARS),
    project_id: uuid.UUID | None = Query(default=None),
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    max_rows: int = Query(default=5000, ge=1, le=20000),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_AUDIT)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    actor_email_snapshot = str(__.email) if getattr(__, "email", None) else None
    actor_token = db.get(ApiToken, auth.token_id) if auth.token_id is not None else None
    project = db.get(Project, project_id) if project_id is not None else None
    actor_token_name_snapshot = getattr(actor_token, "name", None)
    project_name_snapshot = getattr(project, "name", None)
    snapshot_id = int(db.execute(select(func.max(AuditEvent.id))).scalar() or 0)
    candidate_ids = (
        _global_audit_stmt(q, project_id=project_id)
        .where(AuditEvent.id <= snapshot_id)
        .with_only_columns(AuditEvent.id)
        .limit(max_rows + 1)
        .subquery()
    )
    available_count = int(
        db.execute(select(func.count()).select_from(candidate_ids)).scalar() or 0
    )
    exported_count = min(max_rows, available_count)
    truncated = available_count > max_rows
    request_metadata = request_meta(request)

    write_audit_event(
        db,
        action="SETTINGS_AUDIT_EXPORT_REQUESTED",
        object_type="system",
        object_id="audit",
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_ref=project_id,
        metadata={
            **request_metadata,
            "q": q,
            "project_id": str(project_id) if project_id else None,
            "format": format,
            "max_rows": max_rows,
            "snapshot_id": snapshot_id,
            "expected_exported_count": exported_count,
            "truncated": truncated,
        },
    )
    db.commit()

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    extension = "json" if format == "json" else "csv"
    filename = f"share-sentinel-audit-{timestamp}.{extension}"
    return _AuditStreamingResponse(
        _audit_export_chunks(
            q=q,
            project_id=project_id,
            export_format=format,
            max_rows=max_rows,
            expected_rows=exported_count,
            truncated=truncated,
            snapshot_id=snapshot_id,
            auth=auth,
            actor_email_snapshot=actor_email_snapshot,
            actor_token_name_snapshot=actor_token_name_snapshot,
            project_name_snapshot=project_name_snapshot,
            request_metadata=request_metadata,
        ),
        media_type="application/json" if format == "json" else "text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Row-Count": str(exported_count),
            "X-Export-Row-Limit": str(max_rows),
            "X-Export-Truncated": str(truncated).lower(),
            "X-Export-Snapshot-ID": str(snapshot_id),
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/rbac/project-memberships", response_model=dict)
def list_project_memberships(
    q: str | None = Query(default=None, max_length=MAX_SETTINGS_SEARCH_CHARS),
    user_ids: list[uuid.UUID] | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    stmt = (
        select(
            ProjectMember.project_id,
            Project.name.label("project_name"),
            ProjectMember.user_id,
            User.email.label("user_email"),
            ProjectMember.role,
        )
        .join(Project, Project.id == ProjectMember.project_id)
        .join(User, User.id == ProjectMember.user_id)
    )
    if q:
        escaped = escape_like(q.strip())
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Project.name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
                cast(ProjectMember.project_id, String).ilike(pattern, escape="\\"),
                cast(ProjectMember.user_id, String).ilike(pattern, escape="\\"),
            )
        )
    if user_ids:
        stmt = stmt.where(ProjectMember.user_id.in_(user_ids))
    if project_id is not None:
        stmt = stmt.where(ProjectMember.project_id == project_id)

    stmt = apply_keyset_pagination(stmt, SETTINGS_PROJECT_MEMBERSHIP_CURSOR, cursor, limit)
    rows, next_cursor = paginate_rows(db.execute(stmt).all(), SETTINGS_PROJECT_MEMBERSHIP_CURSOR, limit)
    items = [
        ProjectMembershipOut(
            project_id=row.project_id,
            project_name=row.project_name,
            user_id=row.user_id,
            user_email=row.user_email,
            role=row.role,
        ).model_dump(mode="json")
        for row in rows
    ]
    return {"items": items, "next_cursor": next_cursor}


@router.post("/rbac/project-memberships")
def upsert_project_membership(
    payload: ProjectMembershipUpsertIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if db.get(User, payload.user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    membership = db.get(ProjectMember, {"project_id": payload.project_id, "user_id": payload.user_id})
    if membership:
        if membership.role == ProjectRole.ADMIN and payload.role != ProjectRole.ADMIN:
            if _count_project_admins(db, payload.project_id, exclude_user_id=payload.user_id) < 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="at least one project admin must remain",
                )
        membership.role = payload.role
        db.add(membership)
    else:
        db.add(ProjectMember(project_id=payload.project_id, user_id=payload.user_id, role=payload.role))

    write_audit_event(
        db,
        action="SETTINGS_PROJECT_MEMBERSHIP_UPSERT",
        object_type="project_member",
        object_id=f"{payload.project_id}:{payload.user_id}",
        actor_user_id=auth.user_id,
        project_id=payload.project_id,
        metadata={**request_meta(request), "role": payload.role.value},
    )
    db.commit()
    return {"ok": True}


@router.delete("/rbac/project-memberships/{project_id}/{user_id}")
def remove_project_membership(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    membership = db.get(ProjectMember, {"project_id": project_id, "user_id": user_id})
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")
    if membership.role == ProjectRole.ADMIN:
        if _count_project_admins(db, project_id, exclude_user_id=user_id) < 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="at least one project admin must remain")

    db.delete(membership)
    write_audit_event(
        db,
        action="SETTINGS_PROJECT_MEMBERSHIP_REMOVED",
        object_type="project_member",
        object_id=f"{project_id}:{user_id}",
        actor_user_id=auth.user_id,
        project_id=project_id,
        metadata=request_meta(request),
    )
    db.commit()
    return {"ok": True}


@router.post("/rbac/users/{user_id}/assign-all-projects")
def assign_user_memberships_to_all_projects(
    user_id: uuid.UUID,
    payload: UserAssignAllProjectsIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    role = payload.role
    overwrite_existing = payload.overwrite_existing

    result = users_router._assign_user_to_all_projects(db, user_id, role, overwrite_existing=overwrite_existing)

    write_audit_event(
        db,
        action="SETTINGS_USER_ASSIGNED_ALL_PROJECTS",
        object_type="user",
        object_id=str(user_id),
        actor_user_id=auth.user_id,
        metadata={
            **request_meta(request),
            "role": role.value,
            "overwrite_existing": overwrite_existing,
            **result,
        },
    )
    db.commit()
    return {"ok": True, **result}


def _count_project_admins(db: Session, project_id: uuid.UUID, exclude_user_id: uuid.UUID | None = None) -> int:
    lock_project_admin_guard(db, project_id)
    stmt = select(func.count(ProjectMember.user_id)).where(
        ProjectMember.project_id == project_id,
        ProjectMember.role == ProjectRole.ADMIN,
    )
    if exclude_user_id is not None:
        stmt = stmt.where(ProjectMember.user_id != exclude_user_id)
    return int(db.execute(stmt).scalar() or 0)


def _enforce_scope_policy(user: User, role: ProjectRole, scopes: list[str]) -> None:
    if user.is_sysadmin:
        return
    allowed_scopes = set(default_scopes_for_project_role(role))
    disallowed = sorted(scope for scope in scopes if scope not in allowed_scopes)
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"non-sysadmin token scopes must match role defaults: {', '.join(disallowed)}",
        )


def _api_token_admin_out(token: ApiToken, user_email: str, project_name: str) -> ApiTokenAdminOut:
    return ApiTokenAdminOut(
        id=token.id,
        user_id=token.user_id,
        user_email=user_email,
        project_id=token.project_id,
        project_name=project_name,
        name=token.name,
        role=token.role,
        scopes=normalize_token_scopes(token.scopes),
        last_used_at=token.last_used_at,
        expires_at=token.expires_at,
        created_at=token.created_at,
        revoked_at=token.revoked_at,
    )
