import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.models import RefreshToken, User
from app.routers import auth as auth_router
from app.schemas import RefreshIn
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response


class _ExecuteResult:
    def __init__(self, row: Any | None):
        self._row = row

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        if self._row is None:
            return []
        if isinstance(self._row, list):
            return self._row
        return [self._row]


@dataclass
class _FakeDb:
    execute_row: Any | None = None
    get_map: dict[tuple[Any, Any], Any] = field(default_factory=dict)
    added: list[Any] = field(default_factory=list)
    commit_count: int = 0

    def execute(self, _statement):
        return _ExecuteResult(self.execute_row)

    def get(self, model, key):
        return self.get_map.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def commit(self):
        self.commit_count += 1


def _settings(**overrides):
    values = {
        "auth_cookie_name": "share_sentinel_session",
        "auth_csrf_cookie_name": "share_sentinel_csrf",
        "auth_csrf_header_name": "x-csrf-token",
        "auth_refresh_cookie_name": "share_sentinel_refresh",
        "refresh_token_days": 14,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(cookie_header: str | None = None, csrf_header: str | None = None) -> Request:
    settings = auth_router.get_settings()
    headers: list[tuple[bytes, bytes]] = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode("utf-8")))
    if csrf_header is not None:
        headers.append((settings.auth_csrf_header_name.encode("utf-8"), csrf_header.encode("utf-8")))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/refresh",
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_consume_refresh_token_returns_none_when_no_matching_row() -> None:
    fake_db = _FakeDb(execute_row=None)
    consumed = auth_router._consume_refresh_token(fake_db, "missing-token-hash", datetime.now(tz=UTC))
    assert consumed is None


def test_refresh_allows_single_use_and_rejects_replay(monkeypatch) -> None:
    user_id = uuid.uuid4()
    used = False
    consumed = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)
    fake_db = _FakeDb()
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, is_active=True, is_approved=True, session_version=7)
    access_token_calls: list[tuple[str, int | None, str | None]] = []

    def _consume_once(_db, _token_hash, _now):
        nonlocal used
        if used:
            return None
        used = True
        return consumed

    rate_limit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        auth_router.rate_limiter,
        "check",
        lambda _request, scope, limit, window_seconds, actor_key, fail_open: rate_limit_calls.append(
            {
                "scope": scope,
                "limit": limit,
                "window_seconds": window_seconds,
                "actor_key": actor_key,
                "fail_open": fail_open,
            }
        ),
    )
    monkeypatch.setattr(auth_router, "_consume_refresh_token", _consume_once)
    monkeypatch.setattr(auth_router, "hash_external_token", lambda value: f"hash:{value}")
    monkeypatch.setattr(auth_router, "random_token", lambda *_args, **_kwargs: "new-refresh")
    monkeypatch.setattr(
        auth_router,
        "make_access_token",
        lambda subject, session_version=None, session_id=None: access_token_calls.append((subject, session_version, session_id))
        or "new-access",
    )
    monkeypatch.setattr(auth_router, "generate_csrf_token", lambda: "csrf-token")
    monkeypatch.setattr(auth_router, "set_auth_cookies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "set_refresh_cookie", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "get_settings", lambda: _settings())

    payload = RefreshIn(refresh_token="old-refresh")
    first = auth_router.refresh(_request(), Response(), payload, fake_db)
    assert first.ok is True
    assert fake_db.commit_count == 1
    assert len(fake_db.added) == 1
    assert isinstance(fake_db.added[0], RefreshToken)

    with pytest.raises(HTTPException) as exc:
        auth_router.refresh(_request(), Response(), payload, fake_db)
    assert exc.value.status_code == 401
    assert fake_db.commit_count == 1
    assert any(call["actor_key"] == "refresh" for call in rate_limit_calls)
    assert any(call["actor_key"] == "refresh:hash:old-refresh" for call in rate_limit_calls)
    assert access_token_calls == [(str(user_id), 7, str(fake_db.added[0].id))]


def test_refresh_accepts_refresh_cookie_when_csrf_matches(monkeypatch) -> None:
    user_id = uuid.uuid4()
    consumed = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)
    fake_db = _FakeDb()
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, is_active=True, is_approved=True)
    monkeypatch.setattr(auth_router, "get_settings", lambda: _settings())
    settings = auth_router.get_settings()
    cookie_header = (
        f"{settings.auth_refresh_cookie_name}=cookie-refresh; "
        f"{settings.auth_csrf_cookie_name}=csrf-token"
    )
    auth_cookie_calls: list[tuple[str, str]] = []
    refresh_cookie_calls: list[str] = []

    monkeypatch.setattr(auth_router, "_consume_refresh_token", lambda *_args, **_kwargs: consumed)
    monkeypatch.setattr(auth_router, "hash_external_token", lambda value: f"hash:{value}")
    monkeypatch.setattr(auth_router, "random_token", lambda *_args, **_kwargs: "next-refresh")
    monkeypatch.setattr(auth_router, "make_access_token", lambda *_args, **_kwargs: "next-access")
    monkeypatch.setattr(auth_router, "generate_csrf_token", lambda: "next-csrf")
    monkeypatch.setattr(
        auth_router,
        "set_auth_cookies",
        lambda _response, access, csrf: auth_cookie_calls.append((access, csrf)),
    )
    monkeypatch.setattr(
        auth_router,
        "set_refresh_cookie",
        lambda _response, refresh: refresh_cookie_calls.append(refresh),
    )
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router.rate_limiter, "check", lambda *_args, **_kwargs: None)

    result = auth_router.refresh(
        _request(cookie_header=cookie_header, csrf_header="csrf-token"),
        Response(),
        None,
        fake_db,
    )

    assert result.ok is True
    assert auth_cookie_calls == [("next-access", "next-csrf")]
    assert refresh_cookie_calls == ["next-refresh"]


def test_refresh_requires_csrf_when_using_refresh_cookie() -> None:
    settings = auth_router.get_settings()
    cookie_header = (
        f"{settings.auth_refresh_cookie_name}=cookie-refresh; "
        f"{settings.auth_csrf_cookie_name}=csrf-token"
    )

    with pytest.raises(HTTPException) as exc:
        auth_router.refresh(_request(cookie_header=cookie_header), Response(), None, _FakeDb())

    assert exc.value.status_code == 403


def test_logout_requires_csrf_when_auth_cookie_present() -> None:
    auth_router.get_settings.cache_clear()
    settings = auth_router.get_settings()
    cookie_header = f"{settings.auth_cookie_name}=session-token; {settings.auth_csrf_cookie_name}=csrf-token"

    with pytest.raises(HTTPException) as exc:
        auth_router.logout(_request(cookie_header=cookie_header), Response(), None, _FakeDb())

    assert exc.value.status_code == 403


def test_logout_allows_cookie_session_when_csrf_matches() -> None:
    auth_router.get_settings.cache_clear()
    settings = auth_router.get_settings()
    cookie_header = f"{settings.auth_cookie_name}=session-token; {settings.auth_csrf_cookie_name}=csrf-token"

    result = auth_router.logout(
        _request(cookie_header=cookie_header, csrf_header="csrf-token"),
        Response(),
        None,
        _FakeDb(),
    )

    assert result == {"ok": True}


def test_refresh_requires_csrf_when_refresh_cookie_present(monkeypatch) -> None:
    monkeypatch.setattr(auth_router, "get_settings", lambda: _settings())
    settings = auth_router.get_settings()
    cookie_header = (
        f"{settings.auth_refresh_cookie_name}=cookie-refresh; "
        f"{settings.auth_csrf_cookie_name}=csrf-token"
    )

    with pytest.raises(HTTPException) as exc:
        auth_router.refresh(_request(cookie_header=cookie_header), Response(), None, _FakeDb())

    assert exc.value.status_code == 403


def test_refresh_prefers_cookie_token_over_body_token(monkeypatch) -> None:
    captured: list[str] = []
    user_id = uuid.uuid4()
    consumed = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)
    fake_db = _FakeDb()
    monkeypatch.setattr(auth_router, "get_settings", lambda: _settings())
    settings = auth_router.get_settings()
    cookie_header = (
        f"{settings.auth_refresh_cookie_name}=cookie-refresh; "
        f"{settings.auth_csrf_cookie_name}=csrf-token"
    )
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, is_active=True, is_approved=True)

    monkeypatch.setattr(auth_router, "_consume_refresh_token", lambda *_args, **_kwargs: consumed)
    monkeypatch.setattr(auth_router, "hash_external_token", lambda value: captured.append(value) or f"hash:{value}")
    monkeypatch.setattr(auth_router, "random_token", lambda *_args, **_kwargs: "next-refresh")
    monkeypatch.setattr(auth_router, "make_access_token", lambda *_args, **_kwargs: "next-access")
    monkeypatch.setattr(auth_router, "generate_csrf_token", lambda: "next-csrf")
    monkeypatch.setattr(auth_router, "set_auth_cookies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "set_refresh_cookie", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router.rate_limiter, "check", lambda *_args, **_kwargs: None)

    auth_router.refresh(
        _request(cookie_header=cookie_header, csrf_header="csrf-token"),
        Response(),
        RefreshIn(refresh_token="body-refresh"),
        fake_db,
    )

    assert captured[0] == "cookie-refresh"


def test_logout_requires_csrf_when_refresh_cookie_present(monkeypatch) -> None:
    monkeypatch.setattr(auth_router, "get_settings", lambda: _settings())
    settings = auth_router.get_settings()
    cookie_header = (
        f"{settings.auth_refresh_cookie_name}=cookie-refresh; "
        f"{settings.auth_csrf_cookie_name}=csrf-token"
    )

    with pytest.raises(HTTPException) as exc:
        auth_router.logout(_request(cookie_header=cookie_header), Response(), None, _FakeDb())

    assert exc.value.status_code == 403


def test_logout_revokes_refresh_cookie_when_present(monkeypatch) -> None:
    monkeypatch.setattr(auth_router, "get_settings", lambda: _settings())
    settings = auth_router.get_settings()
    refresh_token = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), revoked_at=None)
    fake_db = _FakeDb(execute_row=refresh_token)
    cookie_header = (
        f"{settings.auth_refresh_cookie_name}=refresh-cookie; "
        f"{settings.auth_csrf_cookie_name}=csrf-token"
    )

    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "hash_external_token", lambda value: f"hash:{value}")

    result = auth_router.logout(
        _request(cookie_header=cookie_header, csrf_header="csrf-token"),
        Response(),
        None,
        fake_db,
    )

    assert result == {"ok": True}
    assert refresh_token.revoked_at is not None
    assert fake_db.commit_count == 1


def test_logout_all_revokes_refresh_sessions_and_bumps_session_version(monkeypatch) -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, session_version=2)
    refresh_tokens = [
        SimpleNamespace(id=uuid.uuid4(), user_id=user_id, revoked_at=None),
        SimpleNamespace(id=uuid.uuid4(), user_id=user_id, revoked_at=None),
    ]
    fake_db = _FakeDb(execute_row=refresh_tokens)
    audit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **kwargs: audit_calls.append(kwargs))

    result = auth_router.logout_all(_request(), Response(), fake_db, user)

    assert result == {"ok": True, "revoked_sessions": 2}
    assert user.session_version == 3
    assert all(token.revoked_at is not None for token in refresh_tokens)
    assert fake_db.commit_count == 1
    assert audit_calls[-1]["metadata"]["session_version"] == 3
