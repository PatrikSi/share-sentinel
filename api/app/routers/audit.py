import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth_context, request_meta, require_project_role, require_token_scopes
from app.enums import ProjectRole
from app.models import AuditEvent
from app.pagination import KeysetColumn, apply_keyset_pagination, paginate_rows, parse_datetime_cursor_value
from app.services.audit import write_audit_event
from app.token_scopes import SCOPE_READ_AUDIT

router = APIRouter(prefix="/projects/{project_id}/audit", tags=["audit"])
PROJECT_AUDIT_CURSOR = (
    KeysetColumn("ts", AuditEvent.ts, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", AuditEvent.id, direction="desc"),
)


@router.get("")
def list_audit_events(
    project_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_AUDIT)),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)

    stmt = select(AuditEvent).where(AuditEvent.project_id == project_id)
    stmt = apply_keyset_pagination(stmt, PROJECT_AUDIT_CURSOR, cursor, limit)
    events, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), PROJECT_AUDIT_CURSOR, limit)
    write_audit_event(
        db,
        action="AUDIT_VIEWED",
        object_type="project",
        object_id=str(project_id),
        actor_user_id=auth.user_id,
        actor_token_id=auth.token_id,
        project_id=project_id,
        metadata={**request_meta(request), "limit": limit, "cursor": cursor, "result_count": len(events)},
    )
    db.commit()
    return {
        "items": [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "actor_user_id": str(e.actor_user_id) if e.actor_user_id else None,
                "actor_token_id": str(e.actor_token_id) if e.actor_token_id else None,
                "action": e.action,
                "object_type": e.object_type,
                "object_id": e.object_id,
                "metadata": e.metadata_json,
            }
            for e in events
        ],
        "next_cursor": next_cursor,
    }
