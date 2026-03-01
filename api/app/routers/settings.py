import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_sysadmin, request_meta, require_token_scopes
from app.models import ApiToken, AuditEvent, Project, ProjectMember, User
from app.pagination import next_cursor, parse_cursor
from app.schemas import ApiTokenAdminOut, AuditEventOut, ProjectMembershipOut, ProjectMembershipUpsertIn, ProjectOut
from app.services.audit import write_audit_event
from app.token_scopes import SCOPE_READ_AUDIT, SCOPE_READ_MEMBERS, SCOPE_READ_TOKENS, SCOPE_WRITE_MEMBERS, SCOPE_WRITE_TOKENS, normalize_token_scopes

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/projects", response_model=list[ProjectOut])
def list_all_projects(
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    projects = db.execute(select(Project).order_by(Project.created_at.desc())).scalars().all()
    return [ProjectOut(id=project.id, name=project.name, created_at=project.created_at) for project in projects]


@router.get("/api-tokens", response_model=dict)
def list_all_api_tokens(
    request: Request,
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_TOKENS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    offset = parse_cursor(cursor)

    stmt = (
        select(ApiToken, User.email.label("user_email"), Project.name.label("project_name"))
        .join(User, User.id == ApiToken.user_id)
        .join(Project, Project.id == ApiToken.project_id)
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                ApiToken.name.ilike(pattern),
                User.email.ilike(pattern),
                Project.name.ilike(pattern),
                cast(ApiToken.id, String).ilike(pattern),
            )
        )

    rows = db.execute(stmt.order_by(ApiToken.created_at.desc()).offset(offset).limit(limit)).all()
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
        metadata={**request_meta(request), "q": q, "limit": limit, "cursor": cursor, "result_count": len(items)},
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}


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


@router.get("/audit", response_model=dict)
def list_global_audit(
    request: Request,
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_AUDIT)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    offset = parse_cursor(cursor)

    stmt = (
        select(
            AuditEvent,
            User.email.label("actor_email"),
            Project.name.label("project_name"),
        )
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .outerjoin(Project, Project.id == AuditEvent.project_id)
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                AuditEvent.action.ilike(pattern),
                AuditEvent.object_type.ilike(pattern),
                AuditEvent.object_id.ilike(pattern),
                User.email.ilike(pattern),
                Project.name.ilike(pattern),
            )
        )

    rows = db.execute(stmt.order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).offset(offset).limit(limit)).all()
    items = [
        AuditEventOut(
            id=row.AuditEvent.id,
            ts=row.AuditEvent.ts,
            actor_user_id=row.AuditEvent.actor_user_id,
            actor_email=row.actor_email,
            actor_token_id=row.AuditEvent.actor_token_id,
            project_id=row.AuditEvent.project_id,
            project_name=row.project_name,
            action=row.AuditEvent.action,
            object_type=row.AuditEvent.object_type,
            object_id=row.AuditEvent.object_id,
            metadata=row.AuditEvent.metadata_json,
        ).model_dump(mode="json")
        for row in rows
    ]

    write_audit_event(
        db,
        action="SETTINGS_AUDIT_LISTED",
        object_type="system",
        object_id="audit",
        actor_user_id=auth.user_id,
        metadata={**request_meta(request), "q": q, "limit": limit, "cursor": cursor, "result_count": len(items)},
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}


@router.get("/rbac/project-memberships", response_model=dict)
def list_project_memberships(
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_MEMBERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    offset = parse_cursor(cursor)

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
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Project.name.ilike(pattern),
                User.email.ilike(pattern),
                cast(ProjectMember.project_id, String).ilike(pattern),
                cast(ProjectMember.user_id, String).ilike(pattern),
            )
        )

    rows = db.execute(stmt.order_by(Project.name.asc(), User.email.asc()).offset(offset).limit(limit)).all()
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
    return {"items": items, "next_cursor": next_cursor(offset, limit, len(items))}


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
