import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.enums import UITheme
from app.routers import auth as auth_router
from app.schemas import RegisterIn


class _ExecuteResult:
    def __init__(self, row: Any | None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


@dataclass
class _FakeDb:
    execute_row: Any | None = None
    flush_error: Exception | None = None
    added: list[Any] = field(default_factory=list)
    commit_count: int = 0
    rollback_count: int = 0

    def execute(self, _statement):
        return _ExecuteResult(self.execute_row)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        if self.flush_error is not None:
            raise self.flush_error
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(tz=UTC)
            if getattr(obj, "ui_theme", None) is None:
                obj.ui_theme = UITheme.SYSTEM

    def commit(self):
        self.commit_count += 1

    def refresh(self, _obj):
        return None

    def rollback(self):
        self.rollback_count += 1


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/register",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_register_applies_rate_limit_and_persists_user(monkeypatch) -> None:
    fake_db = _FakeDb(execute_row=None)
    rate_limit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        auth_router,
        "get_settings",
        lambda: SimpleNamespace(allow_self_registration=True, password_min_length=12),
    )
    monkeypatch.setattr(auth_router, "validate_password_strength", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "hash_password", lambda *_args, **_kwargs: "hashed")
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        auth_router.rate_limiter,
        "check",
        lambda request, bucket, limit, window_seconds, actor_key, fail_open: rate_limit_calls.append(
            {
                "bucket": bucket,
                "limit": limit,
                "window_seconds": window_seconds,
                "actor_key": actor_key,
                "fail_open": fail_open,
            }
        ),
    )

    payload = RegisterIn(email="User@Example.com", password="StrongPassword12345")
    result = auth_router.register(payload, _request(), fake_db)

    assert result.email == "user@example.com"
    assert result.is_active is True
    assert result.is_approved is False
    assert fake_db.commit_count == 1
    assert rate_limit_calls == [
        {
            "bucket": "auth_register",
            "limit": 10,
            "window_seconds": 300,
            "actor_key": "register:user@example.com:127.0.0.1",
            "fail_open": False,
        }
    ]


def test_register_returns_throttle_error_when_rate_limited(monkeypatch) -> None:
    fake_db = _FakeDb(execute_row=None)

    monkeypatch.setattr(
        auth_router,
        "get_settings",
        lambda: SimpleNamespace(allow_self_registration=True, password_min_length=12),
    )
    monkeypatch.setattr(
        auth_router.rate_limiter,
        "check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPException(status_code=429, detail="rate limit exceeded")
        ),
    )

    payload = RegisterIn(email="user@example.com", password="StrongPassword12345")
    with pytest.raises(HTTPException) as exc:
        auth_router.register(payload, _request(), fake_db)

    assert exc.value.status_code == 429
    assert fake_db.commit_count == 0
    assert fake_db.added == []


def test_register_handles_integrity_error_as_conflict(monkeypatch) -> None:
    fake_db = _FakeDb(execute_row=None, flush_error=IntegrityError("insert", {}, Exception("duplicate")))

    monkeypatch.setattr(
        auth_router,
        "get_settings",
        lambda: SimpleNamespace(allow_self_registration=True, password_min_length=12),
    )
    monkeypatch.setattr(auth_router, "validate_password_strength", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "hash_password", lambda *_args, **_kwargs: "hashed")
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router.rate_limiter, "check", lambda *_args, **_kwargs: None)

    payload = RegisterIn(email="user@example.com", password="StrongPassword12345")
    with pytest.raises(HTTPException) as exc:
        auth_router.register(payload, _request(), fake_db)

    assert exc.value.status_code == 409
    assert "email already exists" in exc.value.detail
    assert fake_db.rollback_count == 1
    assert fake_db.commit_count == 0
