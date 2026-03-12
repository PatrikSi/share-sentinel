import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from ipaddress import ip_address, ip_network

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.enums import ProjectRole
from app.models import ApiToken, ProjectMember, User
from app.security import decode_access_token, hash_external_token
from app.token_scopes import has_required_scope, normalize_token_scopes

bearer_scheme = HTTPBearer(auto_error=False)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


ROLE_ORDER = {
    ProjectRole.VIEWER: 1,
    ProjectRole.OPERATOR: 2,
    ProjectRole.ADMIN: 3,
}


@dataclass
class AuthContext:
    user_id: uuid.UUID | None
    token_id: uuid.UUID | None
    token_project_id: uuid.UUID | None
    token_role: ProjectRole | None
    token_scopes: list[str] | None


def _unauthorized(message: str = "unauthorized") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message)


def get_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    request.state.token_scopes = None
    token_source = "header"

    token: str | None = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    else:
        token = _resolve_cookie_token(request)
        token_source = "cookie"
    if not token:
        raise _unauthorized("missing bearer token")

    if token_source == "cookie":
        _enforce_csrf_if_needed(request)

    if token.count(".") == 2:
        try:
            payload = decode_access_token(token)
            try:
                user_id = uuid.UUID(payload["sub"])
            except Exception as exc:  # noqa: BLE001
                raise _unauthorized("invalid access token subject") from exc

            user = db.get(User, user_id)
            if not user or not user.is_active:
                raise _unauthorized("user not active")
            if not user.is_approved:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account pending approval")
            return AuthContext(user_id=user_id, token_id=None, token_project_id=None, token_role=None, token_scopes=None)
        except JWTError:
            # Fallback to API token lookup for token strings that resemble JWTs.
            pass
    elif token_source == "cookie":
        raise _unauthorized("invalid session token")

    token_hash = hash_external_token(token)
    stmt = select(ApiToken).where(ApiToken.token_hash == token_hash, ApiToken.revoked_at.is_(None))
    api_token = db.execute(stmt).scalar_one_or_none()
    if api_token is None:
        raise _unauthorized("invalid api token")

    now = datetime.now(tz=UTC)
    if api_token.expires_at and _coerce_utc(api_token.expires_at) < now:
        raise _unauthorized("api token expired")

    user = db.get(User, api_token.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("user not active")
    if not user.is_approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account pending approval")
    membership = db.get(ProjectMember, {"project_id": api_token.project_id, "user_id": api_token.user_id})
    if membership is None:
        raise _unauthorized("invalid api token")
    if ROLE_ORDER[membership.role] < ROLE_ORDER[api_token.role]:
        raise _unauthorized("invalid api token")

    if _should_update_last_used(api_token.last_used_at, now):
        _persist_api_token_last_used(api_token.id, now)
        api_token.last_used_at = now

    token_scopes = normalize_token_scopes(api_token.scopes)
    request.state.token_scopes = token_scopes

    return AuthContext(
        user_id=api_token.user_id,
        token_id=api_token.id,
        token_project_id=api_token.project_id,
        token_role=api_token.role,
        token_scopes=token_scopes,
    )


def get_current_user(auth: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> User:
    if auth.user_id is None:
        raise _unauthorized("invalid identity")
    user = db.get(User, auth.user_id)
    if not user or not user.is_active:
        raise _unauthorized("inactive user")
    if not user.is_approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account pending approval")
    return user


def require_sysadmin(user: User = Depends(get_current_user)) -> User:
    if not user.is_sysadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="sysadmin required")
    return user


def get_project_role(db: Session, auth: AuthContext, project_id: uuid.UUID) -> ProjectRole | None:
    if auth.token_id:
        if auth.token_project_id == project_id:
            return auth.token_role
        return None

    if auth.user_id is None:
        return None

    stmt = select(ProjectMember.role).where(ProjectMember.project_id == project_id, ProjectMember.user_id == auth.user_id)
    return db.execute(stmt).scalar_one_or_none()


def require_project_role(
    project_id: uuid.UUID,
    min_role: ProjectRole,
    auth: AuthContext,
    db: Session,
) -> ProjectRole:
    role = get_project_role(db, auth, project_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a project member")
    if ROLE_ORDER[role] < ROLE_ORDER[min_role]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    return role


def require_token_scopes(*required_scopes: str):
    def _checker(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if auth.token_id is None:
            return auth

        granted = set(auth.token_scopes or [])
        settings = get_settings()
        if not granted and settings.allow_legacy_unscoped_tokens:
            return auth

        for required_scope in required_scopes:
            if not has_required_scope(granted, required_scope):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient token scope")

        return auth

    return _checker


def request_meta(request: Request) -> dict[str, str | None]:
    return {
        "request_id": getattr(request.state, "request_id", None),
        "ip": resolve_client_ip(request),
        "user_agent": request.headers.get("user-agent"),
    }


def resolve_client_ip(request: Request) -> str:
    settings = get_settings()
    remote_ip = request.client.host if request.client and request.client.host else "unknown"
    if remote_ip == "unknown":
        return remote_ip

    trusted_proxy_cidrs = [cidr.strip() for cidr in settings.trusted_proxy_cidrs.split(",") if cidr.strip()]
    if not _is_trusted_proxy(remote_ip, trusted_proxy_cidrs):
        return remote_ip

    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return remote_ip

    first_hop = forwarded_for.split(",")[0].strip()
    if not first_hop:
        return remote_ip

    try:
        ip_address(first_hop)
    except ValueError:
        return remote_ip
    return first_hop


def _is_trusted_proxy(remote_ip: str, trusted_proxy_cidrs: list[str]) -> bool:
    if not trusted_proxy_cidrs:
        return False

    try:
        parsed_remote_ip = ip_address(remote_ip)
    except ValueError:
        return False

    for raw_cidr in trusted_proxy_cidrs:
        try:
            network = ip_network(raw_cidr, strict=False)
        except ValueError:
            continue
        if parsed_remote_ip in network:
            return True
    return False


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _should_update_last_used(last_used_at: datetime | None, now: datetime) -> bool:
    if last_used_at is None:
        return True
    elapsed = (now - _coerce_utc(last_used_at)).total_seconds()
    return elapsed >= get_settings().api_token_last_used_update_interval_seconds


def _persist_api_token_last_used(token_id: uuid.UUID, last_used_at: datetime) -> None:
    with SessionLocal.begin() as db:
        db.execute(update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=last_used_at))


def _resolve_cookie_token(request: Request) -> str | None:
    settings = get_settings()
    return request.cookies.get(settings.auth_cookie_name)


def _enforce_csrf_if_needed(request: Request) -> None:
    settings = get_settings()
    if not settings.auth_require_csrf:
        return
    if request.method.upper() not in UNSAFE_METHODS:
        return

    csrf_cookie = request.cookies.get(settings.auth_csrf_cookie_name)
    csrf_header = request.headers.get(settings.auth_csrf_header_name)
    if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing or invalid CSRF token")
