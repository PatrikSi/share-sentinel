import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import ProjectRole
from app.models import ApiToken, ProjectMember, User
from app.security import decode_access_token, hash_external_token

bearer_scheme = HTTPBearer(auto_error=False)


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



def _unauthorized(message: str = "unauthorized") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message)



def get_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("missing bearer token")

    token = credentials.credentials

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
            return AuthContext(user_id=user_id, token_id=None, token_project_id=None, token_role=None)
        except JWTError:
            # Fallback to API token lookup for token strings that resemble JWTs.
            pass

    token_hash = hash_external_token(token)
    stmt = select(ApiToken).where(ApiToken.token_hash == token_hash, ApiToken.revoked_at.is_(None))
    api_token = db.execute(stmt).scalar_one_or_none()
    if api_token is None:
        raise _unauthorized("invalid api token")

    api_token.last_used_at = datetime.now(tz=UTC)
    db.add(api_token)
    db.commit()

    return AuthContext(
        user_id=api_token.user_id,
        token_id=api_token.id,
        token_project_id=api_token.project_id,
        token_role=api_token.role,
    )



def get_current_user(auth: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> User:
    if auth.user_id is None:
        raise _unauthorized("invalid identity")
    user = db.get(User, auth.user_id)
    if not user or not user.is_active:
        raise _unauthorized("inactive user")
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



def request_meta(request: Request) -> dict[str, str | None]:
    return {
        "request_id": getattr(request.state, "request_id", None),
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
