import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from app.enums import UITheme
from app.models import RefreshToken
from app.routers import auth as auth_router
from app.schemas import LoginIn


class _ExecuteResult:
    def __init__(self, row: Any | None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


@dataclass
class _FakeDb:
    execute_row: Any | None = None
    added: list[Any] = field(default_factory=list)
    commit_count: int = 0

    def execute(self, _statement):
        return _ExecuteResult(self.execute_row)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def commit(self):
        self.commit_count += 1


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_login_sets_session_and_refresh_cookies(monkeypatch) -> None:
    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash="stored-hash",
        session_version=4,
        is_active=True,
        is_sysadmin=False,
        is_approved=True,
        approved_at=None,
        approved_by_user_id=None,
        ui_theme=UITheme.SYSTEM,
    )
    fake_db = _FakeDb(execute_row=user)
    auth_cookie_calls: list[tuple[str, str]] = []
    refresh_cookie_calls: list[str] = []
    access_token_calls: list[tuple[str, int | None, str | None]] = []

    monkeypatch.setattr(auth_router, "check_login_throttle", lambda *_args, **_kwargs: SimpleNamespace(blocked=False, retry_after_seconds=None))
    monkeypatch.setattr(auth_router.rate_limiter, "check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "verify_password", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(auth_router, "clear_login_failures", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        auth_router,
        "make_access_token",
        lambda subject, session_version=None, session_id=None: access_token_calls.append((subject, session_version, session_id))
        or "access-token",
    )
    monkeypatch.setattr(auth_router, "generate_csrf_token", lambda: "csrf-token")
    monkeypatch.setattr(auth_router, "random_token", lambda *_args, **_kwargs: "refresh-token")
    monkeypatch.setattr(auth_router, "hash_external_token", lambda value: f"hash:{value}")
    monkeypatch.setattr(auth_router, "get_settings", lambda: SimpleNamespace(refresh_token_days=14))
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "set_auth_cookies", lambda _response, access, csrf: auth_cookie_calls.append((access, csrf)))
    monkeypatch.setattr(auth_router, "set_refresh_cookie", lambda _response, refresh: refresh_cookie_calls.append(refresh))

    result = auth_router.login(LoginIn(email="User@Example.com", password="secret"), _request(), Response(), fake_db)

    assert result.user.email == "user@example.com"
    assert "access_token" not in result.model_dump()
    assert "refresh_token" not in result.model_dump()
    assert "csrf_token" not in result.model_dump()
    assert fake_db.commit_count == 1
    assert len(fake_db.added) == 1
    assert isinstance(fake_db.added[0], RefreshToken)
    assert auth_cookie_calls == [("access-token", "csrf-token")]
    assert refresh_cookie_calls == ["refresh-token"]
    assert access_token_calls == [(str(user.id), 4, str(fake_db.added[0].id))]
