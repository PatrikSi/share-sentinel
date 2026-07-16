import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from fastapi import Response
from jwt import InvalidTokenError
from passlib.exc import UnknownHashError
from passlib.context import CryptContext

from app.config import get_settings
from app.password_policy import validate_password_strength

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(plain_password, password_hash)
    except (UnknownHashError, ValueError):
        return False


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def session_version_value(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def next_session_version(value: Any) -> int:
    return session_version_value(value) + 1


def make_access_token(subject: str, session_version: int | None = None, session_id: str | None = None) -> str:
    settings = get_settings()
    now = datetime.now(tz=UTC)
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "sub": subject,
        "type": "access",
        "sv": session_version_value(session_version),
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
    }
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "iss", "sub"]},
    )
    if payload.get("type") != "access":
        raise InvalidTokenError("invalid token type")
    return payload


def random_token(bytes_len: int = 32) -> str:
    return secrets.token_urlsafe(bytes_len)


def hash_external_token(raw_token: str) -> str:
    settings = get_settings()
    digest = hashlib.sha256()
    digest.update(settings.token_pepper.encode("utf-8"))
    digest.update(raw_token.encode("utf-8"))
    return digest.hexdigest()


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_auth_cookies(response: Response, access_token: str, csrf_token: str) -> None:
    settings = get_settings()
    cookie_common = {
        "domain": settings.auth_cookie_domain,
        "path": settings.auth_cookie_path,
        "max_age": settings.access_token_minutes * 60,
        "secure": settings.auth_cookie_secure,
        "samesite": settings.auth_cookie_samesite,
    }
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=access_token,
        httponly=True,
        **cookie_common,
    )
    response.set_cookie(
        key=settings.auth_csrf_cookie_name,
        value=csrf_token,
        httponly=False,
        **cookie_common,
    )


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.auth_refresh_cookie_name,
        value=refresh_token,
        domain=settings.auth_cookie_domain,
        path=settings.auth_cookie_path,
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        httponly=True,
    )


def clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    cookie_common = {
        "domain": settings.auth_cookie_domain,
        "path": settings.auth_cookie_path,
    }
    response.delete_cookie(settings.auth_cookie_name, **cookie_common)
    response.delete_cookie(settings.auth_csrf_cookie_name, **cookie_common)
    response.delete_cookie(settings.auth_refresh_cookie_name, **cookie_common)
