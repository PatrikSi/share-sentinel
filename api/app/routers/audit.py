import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_project_role
from app.enums import ProjectRole
from app.models import AuditEvent
from app.pagination import next_cursor, parse_cursor

router = APIRouter(prefix="/projects/{project_id}/audit", tags=["audit"])


@router.get("")
def list_audit_events(
    project_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    require_project_role(project_id, ProjectRole.ADMIN, auth, db)
    offset = parse_cursor(cursor)

    stmt = (
        select(AuditEvent)
        .where(AuditEvent.project_id == project_id)
        .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    events = db.execute(stmt).scalars().all()
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
        "next_cursor": next_cursor(offset, limit, len(events)),
    }
