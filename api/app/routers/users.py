import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_sysadmin, request_meta
from app.models import User
from app.schemas import UserCreateIn, UserOut
from app.security import hash_password
from app.services.audit import write_audit_event

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserOut)
def create_user(
    payload: UserCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: User = Depends(require_sysadmin),
):
    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        is_active=payload.is_active,
        is_sysadmin=payload.is_sysadmin,
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
        metadata={**request_meta(request), "email": user.email},
    )
    db.commit()
    db.refresh(user)
    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        is_sysadmin=user.is_sysadmin,
        ui_theme=user.ui_theme,
    )


@router.get("", response_model=dict)
def list_users(
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_sysadmin),
):
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except ValueError:
            offset = 0

    stmt = select(User)
    if search:
        stmt = stmt.where(User.email.ilike(f"%{search}%"))

    stmt = stmt.order_by(User.created_at.desc()).offset(offset).limit(limit)
    users = db.execute(stmt).scalars().all()
    return {
        "items": [
            {
                "id": str(u.id),
                "email": u.email,
                "is_active": u.is_active,
                "is_sysadmin": u.is_sysadmin,
                "ui_theme": u.ui_theme,
                "created_at": u.created_at.isoformat(),
            }
            for u in users
        ],
        "next_cursor": str(offset + limit) if len(users) == limit else None,
    }


@router.patch("/{user_id}/status", response_model=UserOut)
def update_user_status(
    user_id: uuid.UUID,
    is_active: bool,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _: User = Depends(require_sysadmin),
):
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
    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        is_sysadmin=user.is_sysadmin,
        ui_theme=user.ui_theme,
    )
