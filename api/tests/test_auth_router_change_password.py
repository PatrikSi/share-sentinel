import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers import auth as auth_router
from app.schemas import ChangePasswordIn


class _ExecuteResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


@dataclass
class _FakeDb:
    execute_queue: list[_ExecuteResult] = field(default_factory=list)
    added: list[Any] = field(default_factory=list)
    commit_count: int = 0

    def execute(self, _statement):
        if not self.execute_queue:
            raise AssertionError("unexpected execute() call with empty queue")
        return self.execute_queue.pop(0)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commit_count += 1


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/change-password",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_change_password_rejects_invalid_current_password(monkeypatch) -> None:
    fake_db = _FakeDb()
    user = SimpleNamespace(id=uuid.uuid4(), password_hash="old-hash")
    payload = ChangePasswordIn(current_password="bad", new_password="NewPassword123456")

    monkeypatch.setattr(auth_router, "verify_password", lambda *_args, **_kwargs: False)

    with pytest.raises(HTTPException) as exc:
        auth_router.change_password(payload, _request(), fake_db, user)

    assert exc.value.status_code == 400
    assert fake_db.commit_count == 0


def test_change_password_revokes_active_refresh_sessions(monkeypatch) -> None:
    fake_db = _FakeDb()
    user = SimpleNamespace(id=uuid.uuid4(), password_hash="old-hash")
    active_session = SimpleNamespace(id=uuid.uuid4(), user_id=user.id, revoked_at=None)
    fake_db.execute_queue.append(_ExecuteResult([active_session]))
    payload = ChangePasswordIn(current_password="old-password", new_password="NewPassword123456")

    monkeypatch.setattr(auth_router, "verify_password", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(auth_router, "validate_password_strength", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "hash_password", lambda *_args, **_kwargs: "new-hash")
    monkeypatch.setattr(auth_router, "get_settings", lambda: SimpleNamespace(password_min_length=12))
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)

    response = auth_router.change_password(payload, _request(), fake_db, user)

    assert response == {"ok": True}
    assert user.password_hash == "new-hash"
    assert active_session.revoked_at is not None
    assert active_session.revoked_at > datetime.now(tz=UTC) - timedelta(minutes=1)
    assert fake_db.commit_count == 1
