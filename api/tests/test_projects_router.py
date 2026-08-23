import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_sysadmin
from app.enums import ProjectRole
from app.main import app
from app.models import Project, ProjectMember, User
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError


class _ExecuteResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar(self):
        if not self._rows:
            return None
        return self._rows[0]

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        return self._rows[0]


@dataclass
class _FakeDb:
    execute_queue: list[_ExecuteResult] = field(default_factory=list)
    get_map: dict[tuple[Any, Any], Any] = field(default_factory=dict)
    added: list[Any] = field(default_factory=list)
    deleted: list[Any] = field(default_factory=list)
    commit_count: int = 0
    flush_error: Exception | None = None

    def execute(self, _statement, params=None):
        if params is not None and "key" in params:
            return _ExecuteResult([])
        if not self.execute_queue:
            raise AssertionError("unexpected execute() call with empty queue")
        return self.execute_queue.pop(0)

    def get(self, model, key):
        return self.get_map.get((model, _normalize_key(key)))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commit_count += 1

    def flush(self):
        if self.flush_error is not None:
            raise self.flush_error
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                try:
                    obj.id = uuid.uuid4()
                except Exception:  # noqa: BLE001
                    pass
            if getattr(obj, "created_at", None) is None:
                try:
                    obj.created_at = datetime.now(tz=UTC)
                except Exception:  # noqa: BLE001
                    pass

    def rollback(self):
        return None

    def refresh(self, _obj):
        return None


def _normalize_key(key):
    if isinstance(key, dict):
        return tuple(sorted(key.items()))
    return key


def _client_for_db(fake_db: _FakeDb, actor_user_id: uuid.UUID | None = None) -> TestClient:
    user_id = actor_user_id or uuid.uuid4()

    def _override_auth():
        return AuthContext(user_id=user_id, token_id=None, token_project_id=None, token_role=None, token_scopes=None)

    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_auth_context] = _override_auth
    app.dependency_overrides[require_sysadmin] = lambda: SimpleNamespace(id=user_id, is_sysadmin=True)
    return TestClient(app)


def _clear_overrides():
    app.dependency_overrides.clear()


def test_projects_members_upsert_rejects_demoting_last_project_admin() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    target_user_id = uuid.uuid4()
    fake_db.execute_queue.append(_ExecuteResult([ProjectRole.ADMIN]))
    fake_db.execute_queue.append(_ExecuteResult([0]))
    fake_db.get_map[(User, target_user_id)] = SimpleNamespace(id=target_user_id, email="admin@example.com")
    membership = SimpleNamespace(project_id=project_id, user_id=target_user_id, role=ProjectRole.ADMIN)
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": target_user_id}))] = membership

    client = _client_for_db(fake_db)
    try:
        response = client.post(
            f"/projects/{project_id}/members",
            json={"user_id": str(target_user_id), "role": "viewer"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert "at least one project admin must remain" in response.json()["detail"]
    assert membership.role == ProjectRole.ADMIN


def test_projects_members_remove_rejects_removing_last_project_admin() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    target_user_id = uuid.uuid4()
    fake_db.execute_queue.append(_ExecuteResult([ProjectRole.ADMIN]))
    fake_db.execute_queue.append(_ExecuteResult([0]))
    membership = SimpleNamespace(project_id=project_id, user_id=target_user_id, role=ProjectRole.ADMIN)
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": target_user_id}))] = membership

    client = _client_for_db(fake_db)
    try:
        response = client.delete(f"/projects/{project_id}/members/{target_user_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert "at least one project admin must remain" in response.json()["detail"]
    assert fake_db.deleted == []


def test_projects_members_upsert_allows_demotion_when_other_admin_exists() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    target_user_id = uuid.uuid4()
    fake_db.execute_queue.append(_ExecuteResult([ProjectRole.ADMIN]))
    fake_db.execute_queue.append(_ExecuteResult([1]))
    fake_db.get_map[(User, target_user_id)] = SimpleNamespace(id=target_user_id, email="admin@example.com")
    membership = SimpleNamespace(project_id=project_id, user_id=target_user_id, role=ProjectRole.ADMIN)
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": target_user_id}))] = membership

    client = _client_for_db(fake_db)
    try:
        response = client.post(
            f"/projects/{project_id}/members",
            json={"user_id": str(target_user_id), "role": "viewer"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert membership.role == ProjectRole.VIEWER


def test_projects_create_rejects_duplicate_name_conflict() -> None:
    fake_db = _FakeDb(flush_error=IntegrityError("insert", {}, Exception("duplicate project name")))

    client = _client_for_db(fake_db)
    try:
        response = client.post("/projects", json={"name": "Core"})
    finally:
        _clear_overrides()

    assert response.status_code == 409
    assert response.json()["detail"] == "project name already exists"
    assert len([obj for obj in fake_db.added if isinstance(obj, Project)]) == 1
