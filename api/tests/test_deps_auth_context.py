import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.deps import get_auth_context, require_session_user, require_sysadmin
from app.enums import ProjectRole
from app.models import ProjectMember, User


class _ExecuteResult:
    def __init__(self, row: Any | None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


@dataclass
class _FakeDb:
    token_row: Any | None
    get_map: dict[tuple[Any, Any], Any] = field(default_factory=dict)
    added: list[Any] = field(default_factory=list)

    def execute(self, _stmt):
        return _ExecuteResult(self.token_row)

    def get(self, model, key):
        if isinstance(key, dict):
            key = tuple(sorted(key.items()))
        return self.get_map.get((model, key))

    def add(self, obj):
        self.added.append(obj)


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/auth/me",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_api_token_auth_rejects_missing_project_membership() -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        project_id=project_id,
        role=ProjectRole.ADMIN,
        scopes=["read:projects"],
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        last_used_at=None,
    )
    user = SimpleNamespace(id=user_id, is_active=True, is_approved=True)
    fake_db = _FakeDb(token_row=token)
    fake_db.get_map[(User, user_id)] = user

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-token")
    with pytest.raises(HTTPException) as exc:
        get_auth_context(_request(), credentials, fake_db)

    assert exc.value.status_code == 401


def test_api_token_auth_rejects_token_role_above_membership() -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        project_id=project_id,
        role=ProjectRole.ADMIN,
        scopes=["read:projects"],
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        last_used_at=None,
    )
    user = SimpleNamespace(id=user_id, is_active=True, is_approved=True)
    membership = SimpleNamespace(project_id=project_id, user_id=user_id, role=ProjectRole.VIEWER)

    fake_db = _FakeDb(token_row=token)
    fake_db.get_map[(User, user_id)] = user
    fake_db.get_map[(ProjectMember, tuple(sorted({"project_id": project_id, "user_id": user_id}.items())))] = membership

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-token")
    with pytest.raises(HTTPException) as exc:
        get_auth_context(_request(), credentials, fake_db)

    assert exc.value.status_code == 401


def test_api_token_auth_accepts_membership_and_updates_last_used(monkeypatch) -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        project_id=project_id,
        role=ProjectRole.OPERATOR,
        scopes=["read:runs"],
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        last_used_at=None,
    )
    user = SimpleNamespace(id=user_id, is_active=True, is_approved=True)
    membership = SimpleNamespace(project_id=project_id, user_id=user_id, role=ProjectRole.ADMIN)

    fake_db = _FakeDb(token_row=token)
    fake_db.get_map[(User, user_id)] = user
    fake_db.get_map[(ProjectMember, tuple(sorted({"project_id": project_id, "user_id": user_id}.items())))] = membership
    persisted: list[tuple[uuid.UUID, datetime]] = []
    monkeypatch.setattr("app.deps._persist_api_token_last_used", lambda token_id, last_used_at: persisted.append((token_id, last_used_at)))

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-token")
    auth = get_auth_context(_request(), credentials, fake_db)

    assert auth.user_id == user_id
    assert auth.token_id == token.id
    assert auth.token_project_id == project_id
    assert auth.token_role == ProjectRole.OPERATOR
    assert auth.token_scopes == ["read:runs"]
    assert token.last_used_at is not None
    assert persisted == [(token.id, token.last_used_at)]
    assert fake_db.added == []


def test_require_session_user_rejects_api_token_auth() -> None:
    auth = SimpleNamespace(token_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), is_sysadmin=False)

    with pytest.raises(HTTPException) as exc:
        require_session_user(auth, user)

    assert exc.value.status_code == 403
    assert exc.value.detail == "user login required"


def test_require_sysadmin_rejects_api_token_auth() -> None:
    auth = SimpleNamespace(token_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), is_sysadmin=True)

    with pytest.raises(HTTPException) as exc:
        require_sysadmin(auth, user)

    assert exc.value.status_code == 403
    assert exc.value.detail == "user login required"
