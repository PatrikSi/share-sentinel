import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import (
    _enforce_csrf_if_needed,
    AuthContext,
    get_auth_context,
    get_current_user,
    get_project_role,
    require_session_user,
    require_sysadmin,
    request_meta,
    require_token_scopes,
    resolve_client_ip,
)
from app.enums import ProjectRole
from app.models import ApiToken, RefreshToken, User
from app.password_policy import password_policy_kwargs
from app.rate_limit import RateLimiter
from app.schemas import (
    ApiTokenCreateIn,
    ApiTokenCreateOut,
    ApiTokenOut,
    ChangePasswordIn,
    LoginIn,
    RefreshOut,
    RefreshIn,
    RegisterIn,
    RegistrationSettingsOut,
    SecuritySettingsOut,
    SessionOut,
    ThemeUpdateIn,
    UserOut,
)
from app.security import (
    clear_auth_cookies,
    generate_csrf_token,
    hash_external_token,
    hash_password,
    make_access_token,
    next_session_version,
    random_token,
    set_auth_cookies,
    set_refresh_cookie,
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
    return RegistrationSettingsOut(
        allow_self_registration=settings.allow_self_registration,
        password_min_length=settings.password_min_length,
        password_require_lowercase=settings.password_require_lowercase,
        password_require_uppercase=settings.password_require_uppercase,
        password_require_number=settings.password_require_number,
        password_require_special=settings.password_require_special,
    )


@router.get("/security-settings", response_model=SecuritySettingsOut)
def security_settings(_admin: User = Depends(require_sysadmin)):
    settings = get_settings()
    return SecuritySettingsOut(
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


@router.post("/register", response_model=UserOut)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.allow_self_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="self-registration is disabled")
    email = payload.email.lower()
    client_ip = resolve_client_ip(request)
    rate_limiter.check(
        request,
        "auth_register",
        limit=10,
        window_seconds=300,
        actor_key=f"register:{email}:{client_ip}",
        fail_open=False,
    )

    try:
        validate_password_strength(payload.password, **password_policy_kwargs(settings))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    stmt = select(User).where(func.lower(User.email) == email)
    existing = db.execute(stmt).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        is_active=True,
        is_sysadmin=False,
        is_approved=False,
        approved_at=None,
        approved_by_user_id=None,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists") from exc

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


@router.post("/login", response_model=SessionOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
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

    refresh_raw = random_token(48)
    refresh_hash = hash_external_token(refresh_raw)
    settings = get_settings()

    refresh = RefreshToken(
        user_id=user.id,
        token_hash=refresh_hash,
        expires_at=datetime.now(tz=UTC) + timedelta(days=settings.refresh_token_days),
    )
    db.add(refresh)
    db.flush()
    access_token = make_access_token(str(user.id), getattr(user, "session_version", 1), session_id=str(refresh.id))
    csrf_token = generate_csrf_token()
    set_auth_cookies(response, access_token, csrf_token)
    set_refresh_cookie(response, refresh_raw)

    write_audit_event(
        db,
        action="LOGIN_SUCCESS",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=user.id,
        metadata=request_meta(request),
    )
    db.commit()

    return SessionOut(user=_to_user_out(user))


@router.post("/refresh", response_model=RefreshOut)
def refresh(request: Request, response: Response, payload: RefreshIn | None = None, db: Session = Depends(get_db)):
    refresh_raw, refresh_source = _resolve_refresh_token_raw(request, payload)
    if refresh_source == "cookie":
        _enforce_csrf_if_needed(request)
    token_hash = hash_external_token(refresh_raw)
    # Apply both per-client and per-token throttles. Per-token alone is bypassable with random tokens.
    rate_limiter.check(
        request,
        "auth_refresh",
        limit=120,
        window_seconds=60,
        actor_key="refresh",
        fail_open=False,
    )
    rate_limiter.check(
        request,
        "auth_refresh",
        limit=60,
        window_seconds=60,
        actor_key=f"refresh:{token_hash}",
        fail_open=False,
    )

    now = datetime.now(tz=UTC)
    refresh_token = _consume_refresh_token(db, token_hash, now)
    if refresh_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")

    user = db.get(User, refresh_token.user_id)
    if user is None or not user.is_active or not user.is_approved:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")

    new_refresh_raw = random_token(48)
    new_refresh_hash = hash_external_token(new_refresh_raw)
    settings = get_settings()
    replacement = RefreshToken(
        user_id=refresh_token.user_id,
        token_hash=new_refresh_hash,
        expires_at=datetime.now(tz=UTC) + timedelta(days=settings.refresh_token_days),
    )
    db.add(replacement)
    db.flush()

    access_token = make_access_token(
        str(refresh_token.user_id),
        getattr(user, "session_version", 1),
        session_id=str(replacement.id),
    )
    csrf_token = generate_csrf_token()
    set_auth_cookies(response, access_token, csrf_token)
    set_refresh_cookie(response, new_refresh_raw)
    write_audit_event(
        db,
        action="TOKEN_REFRESHED",
        object_type="refresh_token",
        object_id=str(refresh_token.id),
        actor_user_id=refresh_token.user_id,
        metadata=request_meta(request),
    )
    db.commit()
    return RefreshOut()


@router.post("/logout")
def logout(request: Request, response: Response, payload: RefreshIn | None = None, db: Session = Depends(get_db)):
    settings = get_settings()
    if request.cookies.get(settings.auth_cookie_name) or request.cookies.get(settings.auth_refresh_cookie_name):
        _enforce_csrf_if_needed(request)
    refresh_raw, _ = _resolve_refresh_token_raw(request, payload, raise_when_missing=False)
    if refresh_raw:
        token_hash = hash_external_token(refresh_raw)
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
    clear_auth_cookies(response)
    return {"ok": True}


@router.post("/logout-all")
def logout_all(request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(require_session_user)):
    stmt = select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
    active_tokens = db.execute(stmt).scalars().all()
    revoked = 0
    now = datetime.now(tz=UTC)
    user.session_version = next_session_version(getattr(user, "session_version", 1))
    db.add(user)
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
        metadata={**request_meta(request), "revoked_sessions": revoked, "session_version": user.session_version},
    )
    db.commit()
    clear_auth_cookies(response)
    return {"ok": True, "revoked_sessions": revoked}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return _to_user_out(user)


@router.patch("/me/theme", response_model=UserOut)
def update_theme(payload: ThemeUpdateIn, db: Session = Depends(get_db), user: User = Depends(require_session_user)):
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
    user: User = Depends(require_session_user),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="current password is incorrect")

    settings = get_settings()
    try:
        validate_password_strength(payload.new_password, **password_policy_kwargs(settings))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    now = datetime.now(tz=UTC)
    user.password_hash = hash_password(payload.new_password)
    user.session_version = next_session_version(getattr(user, "session_version", 1))
    db.add(user)
    active_sessions = db.execute(
        select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
    ).scalars().all()
    for session in active_sessions:
        session.revoked_at = now
        db.add(session)
    write_audit_event(
        db,
        action="PASSWORD_CHANGED",
        object_type="user",
        object_id=str(user.id),
        actor_user_id=user.id,
        metadata={
            **request_meta(request),
            "revoked_sessions": len(active_sessions),
            "session_version": user.session_version,
        },
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

    owner = db.get(User, auth.user_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user login required")
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
    if expires_in_days == 0 and not settings.allow_never_expiring_api_tokens:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="never-expiring api tokens are disabled")
    expires_at = datetime.now(tz=UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    scopes = normalize_token_scopes(payload.scopes)
    if not scopes:
        scopes = default_scopes_for_project_role(payload.role)
    _enforce_scope_policy_for_owner(owner, payload.role, scopes)

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


def _enforce_scope_policy_for_owner(owner: User, role: ProjectRole, scopes: list[str]) -> None:
    if owner.is_sysadmin:
        return
    allowed_scopes = set(default_scopes_for_project_role(role))
    disallowed = sorted(scope for scope in scopes if scope not in allowed_scopes)
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"non-sysadmin token scopes must match role defaults: {', '.join(disallowed)}",
        )


def _resolve_refresh_token_raw(request: Request, payload: RefreshIn | None, *, raise_when_missing: bool = True) -> tuple[str | None, str | None]:
    settings = get_settings()
    refresh_cookie = request.cookies.get(settings.auth_refresh_cookie_name)
    if refresh_cookie:
        return refresh_cookie, "cookie"

    if payload is not None and payload.refresh_token:
        return payload.refresh_token, "body"

    if raise_when_missing:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
    return None, None


def _consume_refresh_token(db: Session, token_hash: str, now: datetime) -> RefreshToken | None:
    row = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at >= now,
        )
        .values(revoked_at=now)
        .returning(RefreshToken)
    ).first()
    if row is None:
        return None
    return row[0]
