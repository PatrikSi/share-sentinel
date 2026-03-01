import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.models import RefreshToken, User
from app.routers import auth as auth_router
from app.schemas import RefreshIn


class _ExecuteResult:
    def __init__(self, row: Any | None):
        self._row = row

    def first(self):
        return self._row


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

    def commit(self):
        self.commit_count += 1


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/refresh",
        "headers": [],
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
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, is_active=True, is_approved=True)

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
    monkeypatch.setattr(auth_router, "make_access_token", lambda *_args, **_kwargs: "new-access")
    monkeypatch.setattr(auth_router, "generate_csrf_token", lambda: "csrf-token")
    monkeypatch.setattr(auth_router, "set_auth_cookies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "get_settings", lambda: SimpleNamespace(refresh_token_days=14))

    payload = RefreshIn(refresh_token="old-refresh")
    first = auth_router.refresh(payload, _request(), Response(), fake_db)
    assert first["access_token"] == "new-access"
    assert first["refresh_token"] == "new-refresh"
    assert first["csrf_token"] == "csrf-token"
    assert fake_db.commit_count == 1
    assert len(fake_db.added) == 1
    assert isinstance(fake_db.added[0], RefreshToken)

    with pytest.raises(HTTPException) as exc:
        auth_router.refresh(payload, _request(), Response(), fake_db)
    assert exc.value.status_code == 401
    assert fake_db.commit_count == 1
    assert any(call["actor_key"] == "refresh" for call in rate_limit_calls)
    assert any(call["actor_key"] == "refresh:hash:old-refresh" for call in rate_limit_calls)
