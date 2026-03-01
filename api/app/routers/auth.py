import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import AuthContext, get_auth_context, get_current_user, get_project_role, request_meta, require_token_scopes, resolve_client_ip
from app.enums import ProjectRole
from app.models import ApiToken, RefreshToken, User
from app.rate_limit import RateLimiter
from app.schemas import (
    ApiTokenCreateIn,
    ApiTokenCreateOut,
    ApiTokenOut,
    ChangePasswordIn,
    LoginIn,
    RefreshIn,
    RegisterIn,
    RegistrationSettingsOut,
    ThemeUpdateIn,
    TokenPairOut,
    UserOut,
)
from app.security import (
    hash_external_token,
    hash_password,
    make_access_token,
    random_token,
    validate_password_strength,
    verify_password,
)
from app.services.audit import write_audit_event
from app.services.auth_rate_limit import check_login_throttle, clear_login_failures, record_login_failure
from app.token_scopes import SCOPE_READ_TOKENS, SCOPE_WRITE_TOKENS, default_scopes_for_project_role, normalize_token_scopes

router = APIRouter(prefix="/auth", tags=["auth"])
rate_limiter = RateLimiter()


@router.get("/registration-settings", response_model=RegistrationSettingsOut)
def registration_settings():
    settings = get_settings()
    return RegistrationSettingsOut(allow_self_registration=settings.allow_self_registration)


@router.post("/register", response_model=UserOut)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.allow_self_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="self-registration is disabled")

    try:
        validate_password_strength(payload.password, settings.password_min_length)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    stmt = select(User).where(func.lower(User.email) == payload.email.lower())
    existing = db.execute(stmt).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists")

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        is_active=True,
        is_sysadmin=False,
        is_approved=False,
        approved_at=None,
        approved_by_user_id=None,
    )
    db.add(user)
    db.flush()

    write_audit_event(
        db,
        action="USER_SELF_REGISTERED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=user.id,
        metadata={**request_meta(request), "email": user.email, "is_approved": user.is_approved},
    )
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


@router.post("/login", response_model=TokenPairOut)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = payload.email.lower()
    client_ip = resolve_client_ip(request)

    throttle = check_login_throttle(email, client_ip)
    if throttle.blocked:
        detail = "Too many failed login attempts. Try again later."
        headers = {"Retry-After": str(throttle.retry_after_seconds)} if throttle.retry_after_seconds else None
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail, headers=headers)

    rate_limiter.check(
        request,
        "auth_login",
        limit=20,
        window_seconds=60,
        actor_key=f"login:{email}",
        fail_open=False,
    )

    stmt = select(User).where(func.lower(User.email) == email)
    user = db.execute(stmt).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        record_login_failure(email, client_ip)
        write_audit_event(
            db,
            action="LOGIN_FAILED",
            object_type="user",
            object_id=email,
            metadata=request_meta(request),
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    if not user.is_approved:
        write_audit_event(
            db,
            action="LOGIN_BLOCKED_UNAPPROVED",
            object_type="user",
            object_id=str(user.id),
            actor_user_id=user.id,
            metadata=request_meta(request),
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account pending admin approval")

    if not user.is_active:
        write_audit_event(
            db,
            action="LOGIN_BLOCKED_DISABLED",
            object_type="user",
            object_id=str(user.id),
            actor_user_id=user.id,
            metadata=request_meta(request),
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user disabled")

    clear_login_failures(email, client_ip)

    access_token = make_access_token(str(user.id))
    refresh_raw = random_token(48)
    refresh_hash = hash_external_token(refresh_raw)
    settings = get_settings()

    refresh = RefreshToken(
        user_id=user.id,
        token_hash=refresh_hash,
        expires_at=datetime.now(tz=UTC) + timedelta(days=settings.refresh_token_days),
    )
    db.add(refresh)

    write_audit_event(
        db,
        action="LOGIN_SUCCESS",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=user.id,
        metadata=request_meta(request),
    )
    db.commit()

    return TokenPairOut(
        access_token=access_token,
        refresh_token=refresh_raw,
        user=_to_user_out(user),
    )


@router.post("/refresh")
def refresh(payload: RefreshIn, request: Request, db: Session = Depends(get_db)):
    token_hash = hash_external_token(payload.refresh_token)
    rate_limiter.check(
        request,
        "auth_refresh",
        limit=60,
        window_seconds=60,
        actor_key=f"refresh:{token_hash}",
        fail_open=False,
    )

    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
    refresh_token = db.execute(stmt).scalar_one_or_none()
    if refresh_token is None or refresh_token.expires_at < datetime.now(tz=UTC):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")

    user = db.get(User, refresh_token.user_id)
    if user is None or not user.is_active or not user.is_approved:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")

    # Rotate refresh tokens to reduce replay window.
    refresh_token.revoked_at = datetime.now(tz=UTC)
    new_refresh_raw = random_token(48)
    new_refresh_hash = hash_external_token(new_refresh_raw)
    settings = get_settings()
    replacement = RefreshToken(
        user_id=refresh_token.user_id,
        token_hash=new_refresh_hash,
        expires_at=datetime.now(tz=UTC) + timedelta(days=settings.refresh_token_days),
    )
    db.add(replacement)

    access_token = make_access_token(str(refresh_token.user_id))
    write_audit_event(
        db,
        action="TOKEN_REFRESHED",
        object_type="refresh_token",
        object_id=str(refresh_token.id),
        actor_user_id=refresh_token.user_id,
        metadata=request_meta(request),
    )
    db.add(refresh_token)
    db.commit()
    return {"access_token": access_token, "refresh_token": new_refresh_raw}


@router.post("/logout")
def logout(payload: RefreshIn, request: Request, db: Session = Depends(get_db)):
    token_hash = hash_external_token(payload.refresh_token)
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
    refresh_token = db.execute(stmt).scalar_one_or_none()
    if refresh_token is not None:
        refresh_token.revoked_at = datetime.now(tz=UTC)
        write_audit_event(
            db,
            action="LOGOUT",
            object_type="refresh_token",
            object_id=str(refresh_token.id),
            actor_user_id=refresh_token.user_id,
            metadata=request_meta(request),
        )
        db.add(refresh_token)
        db.commit()
    return {"ok": True}


@router.post("/logout-all")
def logout_all(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
    active_tokens = db.execute(stmt).scalars().all()
    revoked = 0
    now = datetime.now(tz=UTC)
    for token in active_tokens:
        token.revoked_at = now
        db.add(token)
        revoked += 1

    write_audit_event(
        db,
        action="LOGOUT_ALL",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=user.id,
        metadata={**request_meta(request), "revoked_sessions": revoked},
    )
    db.commit()
    return {"ok": True, "revoked_sessions": revoked}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return _to_user_out(user)


@router.patch("/me/theme", response_model=UserOut)
def update_theme(payload: ThemeUpdateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    user.ui_theme = payload.ui_theme
    db.add(user)
    db.commit()
    db.refresh(user)
    return _to_user_out(user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="current password is incorrect")

    settings = get_settings()
    try:
        validate_password_strength(payload.new_password, settings.password_min_length)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    write_audit_event(
        db,
        action="PASSWORD_CHANGED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=user.id,
        metadata=request_meta(request),
    )
    db.commit()
    return {"ok": True}


@router.post("/api-tokens", response_model=ApiTokenCreateOut)
def create_api_token(
    payload: ApiTokenCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_TOKENS)),
):
    settings = get_settings()
    if auth.user_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user login required")
    if settings.require_user_for_api_token_create and auth.token_id is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user login required")

    role = get_project_role(db, auth, payload.project_id)
    if role != ProjectRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="project admin required")
    rate_limiter.check(
        request,
        "api_token_create",
        limit=30,
        window_seconds=300,
        actor_key=f"user:{auth.user_id}",
        fail_open=False,
    )

    token_raw = random_token(48)
    token_hash = hash_external_token(token_raw)

    expires_in_days = payload.expires_in_days
    if expires_in_days is None:
        expires_in_days = settings.default_api_token_expiry_days
    expires_at = datetime.now(tz=UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    scopes = normalize_token_scopes(payload.scopes)
    if not scopes:
        scopes = default_scopes_for_project_role(payload.role)

    token = ApiToken(
        user_id=auth.user_id,
        project_id=payload.project_id,
        token_hash=token_hash,
        name=payload.name,
        role=payload.role,
        scopes=scopes,
        expires_at=expires_at,
    )
    db.add(token)
    db.flush()
    write_audit_event(
        db,
        action="API_TOKEN_CREATED",
        object_type="api_token",
        object_id=str(token.id),
        actor_user_id=auth.user_id,
        project_id=payload.project_id,
        metadata={**request_meta(request), "scopes": scopes, "expires_at": expires_at.isoformat() if expires_at else None},
    )
    db.commit()
    db.refresh(token)

    return ApiTokenCreateOut(
        token=token_raw,
        token_meta=ApiTokenOut(
            id=token.id,
            project_id=token.project_id,
            name=token.name,
            role=token.role,
            scopes=normalize_token_scopes(token.scopes),
            last_used_at=token.last_used_at,
            expires_at=token.expires_at,
            created_at=token.created_at,
            revoked_at=token.revoked_at,
        ),
    )


@router.get("/api-tokens", response_model=list[ApiTokenOut])
def list_api_tokens(
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_token_scopes(SCOPE_READ_TOKENS)),
):
    if auth.user_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user login required")
    stmt = select(ApiToken).where(ApiToken.user_id == auth.user_id).order_by(ApiToken.created_at.desc())
    tokens = db.execute(stmt).scalars().all()
    return [
        ApiTokenOut(
            id=t.id,
            project_id=t.project_id,
            name=t.name,
            role=t.role,
            scopes=normalize_token_scopes(t.scopes),
            last_used_at=t.last_used_at,
            expires_at=t.expires_at,
            created_at=t.created_at,
            revoked_at=t.revoked_at,
        )
        for t in tokens
    ]


@router.delete("/api-tokens/{token_id}")
def revoke_api_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_token_scopes(SCOPE_WRITE_TOKENS)),
):
    if auth.user_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user login required")

    token = db.get(ApiToken, token_id)
    if token is None or token.user_id != auth.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")

    token.revoked_at = datetime.now(tz=UTC)
    db.add(token)
    write_audit_event(
        db,
        action="API_TOKEN_REVOKED",
        object_type="api_token",
        object_id=str(token.id),
        actor_user_id=auth.user_id,
        project_id=token.project_id,
        metadata=request_meta(request),
    )
    db.commit()
    return {"ok": True}


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
