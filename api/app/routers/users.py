import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_sysadmin, request_meta, require_token_scopes
from app.models import User
from app.schemas import UserApprovalIn, UserCreateIn, UserOut, UserUpdateIn
from app.security import hash_password, validate_password_strength
from app.services.audit import write_audit_event
from app.token_scopes import SCOPE_READ_USERS, SCOPE_WRITE_USERS

router = APIRouter(prefix="/users", tags=["users"])


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
    try:
        validate_password_strength(payload.password, get_settings().password_min_length)
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
        },
    )
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


@router.get("", response_model=dict)
def list_users(
    search: str | None = None,
    include_pending_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: AuthContext = Depends(require_token_scopes(SCOPE_READ_USERS)),
    __: User = Depends(require_sysadmin),
):
    _ = __
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except ValueError:
            offset = 0

    stmt = select(User)
    if search:
        stmt = stmt.where(User.email.ilike(f"%{search}%"))
    if include_pending_only:
        stmt = stmt.where(User.is_approved.is_(False))

    stmt = stmt.order_by(User.created_at.desc()).offset(offset).limit(limit)
    users = db.execute(stmt).scalars().all()
    return {
        "items": [
            {
                "id": str(u.id),
                "email": u.email,
                "is_active": u.is_active,
                "is_sysadmin": u.is_sysadmin,
                "is_approved": u.is_approved,
                "approved_at": u.approved_at.isoformat() if u.approved_at else None,
                "approved_by_user_id": str(u.approved_by_user_id) if u.approved_by_user_id else None,
                "ui_theme": u.ui_theme,
                "created_at": u.created_at.isoformat(),
            }
            for u in users
        ],
        "next_cursor": str(offset + limit) if len(users) == limit else None,
    }


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

    if payload.email is not None:
        user.email = payload.email.lower()

    if payload.password is not None:
        try:
            validate_password_strength(payload.password, get_settings().password_min_length)
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

    user.is_active = is_active
    db.add(user)
    write_audit_event(
        db,
        action="USER_STATUS_UPDATED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=auth.user_id,
        metadata={**request_meta(request), "is_active": is_active},
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

    user.is_approved = payload.is_approved
    if payload.is_approved:
        user.approved_at = datetime.now(tz=UTC)
        user.approved_by_user_id = auth.user_id
    else:
        user.approved_at = None
        user.approved_by_user_id = None

    db.add(user)
    write_audit_event(
        db,
        action="USER_APPROVAL_UPDATED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=auth.user_id,
        metadata={**request_meta(request), "is_approved": user.is_approved},
    )
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


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
