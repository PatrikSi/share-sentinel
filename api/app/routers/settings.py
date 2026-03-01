import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import ProjectRole
from app.deps import AuthContext, get_auth_context, require_sysadmin, request_meta, require_token_scopes
from app.models import ApiToken, AuditEvent, Project, ProjectMember, User
from app.pagination import next_cursor, parse_cursor
from app.schemas import (
    ApiTokenAdminCreateIn,
    ApiTokenAdminCreateOut,
    ApiTokenAdminOut,
    ApiTokenAdminUpdateIn,
    AuditEventOut,
    ProjectMembershipOut,
    ProjectMembershipUpsertIn,
    ProjectOut,
    UserAssignAllProjectsIn,
)
from app.security import hash_external_token, random_token
from app.services.audit import write_audit_event
from app.token_scopes import (
    ALLOWED_API_TOKEN_SCOPES,
    SCOPE_READ_AUDIT,
    SCOPE_READ_MEMBERS,
    SCOPE_READ_TOKENS,
    SCOPE_WRITE_MEMBERS,
    SCOPE_WRITE_TOKENS,
    default_scopes_for_project_role,
    normalize_token_scopes,
)

router = APIRouter(prefix="/settings", tags=["settings"])

ROLE_ORDER = {
    ProjectRole.VIEWER: 1,
    ProjectRole.OPERATOR: 2,
    ProjectRole.ADMIN: 3,
}


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
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    if token.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot update a revoked token")
    if payload.never_expires and payload.expires_in_days is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot set both never_expires and expires_in_days")

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

    projects = db.execute(select(Project.id)).all()
    assigned = 0
    for row in projects:
        project_id = row.id
        membership = db.get(ProjectMember, {"project_id": project_id, "user_id": user_id})
        if membership:
            if overwrite_existing and membership.role != role:
                if membership.role == ProjectRole.ADMIN and role != ProjectRole.ADMIN:
                    if _count_project_admins(db, project_id, exclude_user_id=user_id) < 1:
                        continue
                membership.role = role
                db.add(membership)
                assigned += 1
            continue
        db.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))
        assigned += 1

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
            "assigned_projects": assigned,
        },
    )
    db.commit()
    return {"ok": True, "assigned_projects": assigned}


def _count_project_admins(db: Session, project_id: uuid.UUID, exclude_user_id: uuid.UUID | None = None) -> int:
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
