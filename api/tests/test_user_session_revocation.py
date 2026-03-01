import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from starlette.requests import Request

from app.deps import AuthContext
from app.enums import UITheme
from app.models import User
from app.routers import users as users_router
from app.schemas import UserApprovalIn


@dataclass
class _FakeDb:
    get_map: dict[tuple[Any, Any], Any] = field(default_factory=dict)
    execute_result: Any | None = None
    added: list[Any] = field(default_factory=list)
    commit_count: int = 0
    refresh_count: int = 0

    def get(self, model, key):
        return self.get_map.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    def execute(self, _statement):
        if self.execute_result is None:
            return SimpleNamespace(rowcount=0)
        return self.execute_result

    def commit(self):
        self.commit_count += 1

    def refresh(self, _obj):
        self.refresh_count += 1

    def flush(self):
        return None

    def rollback(self):
        return None


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "PATCH",
        "path": "/users/test",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def _user(user_id: uuid.UUID, is_active: bool = True, is_approved: bool = True):
    return SimpleNamespace(
        id=user_id,
        email="user@example.com",
        password_hash="hash",
        is_active=is_active,
        is_sysadmin=False,
        is_approved=is_approved,
        approved_at=None,
        approved_by_user_id=None,
        ui_theme=UITheme.SYSTEM,
    )


def test_update_user_status_revokes_sessions_when_disabling(monkeypatch) -> None:
    user_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    fake_db = _FakeDb(get_map={(User, user_id): _user(user_id, is_active=True, is_approved=True)})
    auth = AuthContext(user_id=actor_id, token_id=None, token_project_id=None, token_role=None, token_scopes=None)
    audit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(users_router, "_revoke_active_refresh_tokens", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(users_router, "write_audit_event", lambda *_args, **kwargs: audit_calls.append(kwargs))

    result = users_router.update_user_status(user_id, False, _request(), fake_db, auth, auth, SimpleNamespace(id=actor_id))

    assert result.is_active is False
    assert fake_db.commit_count == 1
    assert fake_db.refresh_count == 1
    assert audit_calls[-1]["metadata"]["revoked_sessions"] == 2


def test_update_user_status_does_not_revoke_when_enabling(monkeypatch) -> None:
    user_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    fake_db = _FakeDb(get_map={(User, user_id): _user(user_id, is_active=False, is_approved=True)})
    auth = AuthContext(user_id=actor_id, token_id=None, token_project_id=None, token_role=None, token_scopes=None)
    audit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        users_router,
        "_revoke_active_refresh_tokens",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not revoke")),
    )
    monkeypatch.setattr(users_router, "write_audit_event", lambda *_args, **kwargs: audit_calls.append(kwargs))

    result = users_router.update_user_status(user_id, True, _request(), fake_db, auth, auth, SimpleNamespace(id=actor_id))

    assert result.is_active is True
    assert fake_db.commit_count == 1
    assert audit_calls[-1]["metadata"]["revoked_sessions"] == 0


def test_update_user_approval_revokes_sessions_when_unapproving(monkeypatch) -> None:
    user_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    fake_db = _FakeDb(get_map={(User, user_id): _user(user_id, is_active=True, is_approved=True)})
    auth = AuthContext(user_id=actor_id, token_id=None, token_project_id=None, token_role=None, token_scopes=None)
    audit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(users_router, "_revoke_active_refresh_tokens", lambda *_args, **_kwargs: 5)
    monkeypatch.setattr(users_router, "write_audit_event", lambda *_args, **kwargs: audit_calls.append(kwargs))

    result = users_router.update_user_approval(
        user_id,
        UserApprovalIn(is_approved=False),
        _request(),
        fake_db,
        auth,
        auth,
        SimpleNamespace(id=actor_id),
    )

    assert result.is_approved is False
    assert fake_db.commit_count == 1
    assert fake_db.refresh_count == 1
    assert audit_calls[-1]["metadata"]["revoked_sessions"] == 5


def test_revoke_active_refresh_tokens_returns_rowcount() -> None:
    user_id = uuid.uuid4()
    fake_db = _FakeDb(execute_result=SimpleNamespace(rowcount=3))
    assert users_router._revoke_active_refresh_tokens(fake_db, user_id) == 3
