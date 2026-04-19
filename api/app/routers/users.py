import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import escape_like, get_db
from app.deps import AuthContext, get_auth_context, require_sysadmin, request_meta, require_token_scopes
from app.enums import ProjectRole
from app.models import Project, ProjectMember, RefreshToken, User
from app.pagination import KeysetColumn, apply_keyset_pagination, paginate_rows, parse_datetime_cursor_value, parse_uuid_cursor_value
from app.password_policy import password_policy_kwargs
from app.schemas import UserAdminOut, UserApprovalIn, UserAssignAllProjectsIn, UserCreateIn, UserOut, UserUpdateIn
from app.security import hash_password, validate_password_strength
from app.services.audit import write_audit_event
from app.token_scopes import SCOPE_READ_USERS, SCOPE_WRITE_MEMBERS, SCOPE_WRITE_USERS, has_required_scope

router = APIRouter(prefix="/users", tags=["users"])
USER_LIST_CURSOR = (
    KeysetColumn("created_at", User.created_at, direction="desc", parser=parse_datetime_cursor_value),
    KeysetColumn("id", User.id, direction="desc", parser=parse_uuid_cursor_value),
)


@router.post("", response_model=UserOut)
def create_user(
    payload: UserCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_USERS)),
    admin: User = Depends(require_sysadmin),
):
    _ = admin
    settings = get_settings()
    try:
        validate_password_strength(payload.password, **password_policy_kwargs(settings))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    approved_at = datetime.now(tz=UTC) if payload.is_approved else None
    approved_by_user_id = auth.user_id if payload.is_approved else None

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        is_active=payload.is_active,
        is_sysadmin=payload.is_sysadmin,
        is_approved=payload.is_approved,
        approved_at=approved_at,
        approved_by_user_id=approved_by_user_id,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists") from exc

    assignment_result: dict[str, object] = {"assigned_projects": 0, "skipped_projects": [], "partial": False}
    if payload.add_to_all_projects:
        _require_member_write_scope_if_token(auth)
        assignment_result = _assign_user_to_all_projects(db, user.id, payload.all_projects_role, overwrite_existing=False)

    write_audit_event(
        db,
        action="USER_CREATED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=auth.user_id,
        metadata={
            **request_meta(request),
            "email": user.email,
            "is_active": user.is_active,
            "is_sysadmin": user.is_sysadmin,
            "is_approved": user.is_approved,
            "add_to_all_projects": payload.add_to_all_projects,
            "all_projects_role": payload.all_projects_role.value if payload.add_to_all_projects else None,
            **assignment_result,
        },
    )
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


@router.get("", response_model=dict)
def list_users(
    search: str | None = None,
    include_pending_only: bool = False,
    project_id: uuid.UUID | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    is_approved: bool | None = Query(default=None),
    is_sysadmin: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_USERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __

    stmt = select(User)
    if search:
        pattern = f"%{escape_like(search)}%"
        project_name_match = exists(
            select(ProjectMember.user_id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(ProjectMember.user_id == User.id, Project.name.ilike(pattern, escape="\\"))
        )
        stmt = stmt.where(or_(User.email.ilike(pattern, escape="\\"), project_name_match))
    if project_id is not None:
        stmt = stmt.where(
            exists(select(ProjectMember.user_id).where(ProjectMember.user_id == User.id, ProjectMember.project_id == project_id))
        )
    if include_pending_only:
        stmt = stmt.where(User.is_approved.is_(False))
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    if is_approved is not None:
        stmt = stmt.where(User.is_approved.is_(is_approved))
    if is_sysadmin is not None:
        stmt = stmt.where(User.is_sysadmin.is_(is_sysadmin))

    stmt = apply_keyset_pagination(stmt, USER_LIST_CURSOR, cursor, limit)
    users, next_cursor = paginate_rows(db.execute(stmt).scalars().all(), USER_LIST_CURSOR, limit)
    return {
        "items": [_to_user_admin_out(u).model_dump(mode="json") for u in users],
        "next_cursor": next_cursor,
    }


@router.get("/{user_id}", response_model=UserAdminOut)
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_USERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _to_user_admin_out(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_USERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    prev_is_active = user.is_active
    prev_is_approved = user.is_approved

    next_is_active = user.is_active if payload.is_active is None else payload.is_active
    next_is_approved = user.is_approved if payload.is_approved is None else payload.is_approved
    next_is_sysadmin = user.is_sysadmin if payload.is_sysadmin is None else payload.is_sysadmin
    _enforce_admin_safety(db, auth.user_id, user, next_is_active, next_is_approved, next_is_sysadmin)

    if payload.email is not None:
        user.email = payload.email.lower()

    if payload.password is not None:
        settings = get_settings()
        try:
            validate_password_strength(payload.password, **password_policy_kwargs(settings))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        user.password_hash = hash_password(payload.password)

    if payload.is_active is not None:
        user.is_active = payload.is_active

    if payload.is_sysadmin is not None:
        user.is_sysadmin = payload.is_sysadmin

    if payload.is_approved is not None:
        if payload.is_approved:
            user.is_approved = True
            user.approved_at = datetime.now(tz=UTC)
            user.approved_by_user_id = auth.user_id
        else:
            user.is_approved = False
            user.approved_at = None
            user.approved_by_user_id = None

    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists") from exc
    revoked_sessions = 0
    if (prev_is_active and not user.is_active) or (prev_is_approved and not user.is_approved):
        revoked_sessions = _revoke_active_refresh_tokens(db, user.id)

    write_audit_event(
        db,
        action="USER_UPDATED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=auth.user_id,
        metadata={
            **request_meta(request),
            "is_active": user.is_active,
            "is_sysadmin": user.is_sysadmin,
            "is_approved": user.is_approved,
            "revoked_sessions": revoked_sessions,
        },
    )
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


@router.patch("/{user_id}/status", response_model=UserOut)
def update_user_status(
    user_id: uuid.UUID,
    is_active: bool,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_USERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    was_active = user.is_active
    _enforce_admin_safety(db, auth.user_id, user, is_active, user.is_approved, user.is_sysadmin)

    user.is_active = is_active
    db.add(user)
    revoked_sessions = _revoke_active_refresh_tokens(db, user.id) if was_active and not is_active else 0
    write_audit_event(
        db,
        action="USER_STATUS_UPDATED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=auth.user_id,
        metadata={**request_meta(request), "is_active": is_active, "revoked_sessions": revoked_sessions},
    )
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


@router.patch("/{user_id}/approval", response_model=UserOut)
def update_user_approval(
    user_id: uuid.UUID,
    payload: UserApprovalIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_USERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    was_approved = user.is_approved
    _enforce_admin_safety(db, auth.user_id, user, user.is_active, payload.is_approved, user.is_sysadmin)

    user.is_approved = payload.is_approved
    if payload.is_approved:
        user.approved_at = datetime.now(tz=UTC)
        user.approved_by_user_id = auth.user_id
    else:
        user.approved_at = None
        user.approved_by_user_id = None

    db.add(user)
    revoked_sessions = _revoke_active_refresh_tokens(db, user.id) if was_approved and not user.is_approved else 0
    write_audit_event(
        db,
        action="USER_APPROVAL_UPDATED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=auth.user_id,
        metadata={**request_meta(request), "is_approved": user.is_approved, "revoked_sessions": revoked_sessions},
    )
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


@router.post("/{user_id}/assign-all-projects")
def assign_user_to_all_projects(
    user_id: uuid.UUID,
    payload: UserAssignAllProjectsIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_USERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    _require_member_write_scope_if_token(auth)
    result = _assign_user_to_all_projects(db, user_id, payload.role, overwrite_existing=payload.overwrite_existing)
    write_audit_event(
        db,
        action="USER_ASSIGNED_TO_ALL_PROJECTS",
        object_type="user",
        object_id=str(user_id),
        actor_user_id=auth.user_id,
        metadata={
            **request_meta(request),
            "role": payload.role.value,
            "overwrite_existing": payload.overwrite_existing,
            **result,
        },
    )
    db.commit()
    return {"ok": True, **result}


def _to_user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        is_sysadmin=user.is_sysadmin,
        is_approved=user.is_approved,
        approved_at=user.approved_at,
        approved_by_user_id=user.approved_by_user_id,
        ui_theme=user.ui_theme,
    )


def _to_user_admin_out(user: User) -> UserAdminOut:
    return UserAdminOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        is_sysadmin=user.is_sysadmin,
        is_approved=user.is_approved,
        approved_at=user.approved_at,
        approved_by_user_id=user.approved_by_user_id,
        ui_theme=user.ui_theme,
        created_at=user.created_at,
    )


def _enforce_admin_safety(
    db: Session,
    actor_user_id: uuid.UUID | None,
    target_user: User,
    next_is_active: bool,
    next_is_approved: bool,
    next_is_sysadmin: bool,
) -> None:
    is_self_update = actor_user_id is not None and actor_user_id == target_user.id
    if is_self_update:
        if not next_is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="you cannot disable your own account")
        if not next_is_approved:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="you cannot unapprove your own account")
        if not next_is_sysadmin:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="you cannot remove your own sysadmin access")

    target_is_active_admin = target_user.is_sysadmin and target_user.is_active and target_user.is_approved
    target_will_remain_active_admin = next_is_sysadmin and next_is_active and next_is_approved
    if target_is_active_admin and not target_will_remain_active_admin:
        remaining = _count_active_approved_sysadmins(db, exclude_user_id=target_user.id)
        if remaining < 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="at least one active approved sysadmin must remain",
            )


def _count_active_approved_sysadmins(db: Session, exclude_user_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count(User.id)).where(
        User.is_sysadmin.is_(True),
        User.is_active.is_(True),
        User.is_approved.is_(True),
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return int(db.execute(stmt).scalar() or 0)


def _require_member_write_scope_if_token(auth: AuthContext) -> None:
    if auth.token_id is None:
        return
    granted = set(auth.token_scopes or [])
    if not has_required_scope(granted, SCOPE_WRITE_MEMBERS):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient token scope")


def _assign_user_to_all_projects(db: Session, user_id: uuid.UUID, role: ProjectRole, overwrite_existing: bool) -> dict[str, object]:
    projects = db.execute(select(Project.id, Project.name)).all()
    assigned = 0
    skipped_projects: list[dict[str, str]] = []
    for row in projects:
        project_id = row.id
        project_name = getattr(row, "name", str(project_id))
        membership = db.get(ProjectMember, {"project_id": project_id, "user_id": user_id})
        if membership:
            if overwrite_existing and membership.role != role:
                if membership.role == ProjectRole.ADMIN and role != ProjectRole.ADMIN:
                    if _count_project_admins(db, project_id, exclude_user_id=user_id) < 1:
                        skipped_projects.append(
                            {
                                "project_id": str(project_id),
                                "project_name": str(project_name),
                                "reason": "last project admin would be removed",
                            }
                        )
                        continue
                membership.role = role
                db.add(membership)
                assigned += 1
            continue
        db.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))
        assigned += 1
    return {
        "assigned_projects": assigned,
        "skipped_projects": skipped_projects,
        "partial": bool(skipped_projects),
    }


def _count_project_admins(db: Session, project_id: uuid.UUID, exclude_user_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count(ProjectMember.user_id)).where(
        ProjectMember.project_id == project_id,
        ProjectMember.role == ProjectRole.ADMIN,
    )
    if exclude_user_id is not None:
        stmt = stmt.where(ProjectMember.user_id != exclude_user_id)
    return int(db.execute(stmt).scalar() or 0)


def _revoke_active_refresh_tokens(db: Session, user_id: uuid.UUID) -> int:
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(tz=UTC))
    )
    return int(getattr(result, "rowcount", 0) or 0)
