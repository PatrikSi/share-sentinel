import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_sysadmin
from app.enums import ProjectRole
from app.main import app
from app.models import ApiToken, Project, ProjectMember, User


class _ExecuteResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar(self):
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

    def execute(self, _statement):
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


def test_settings_projects_lists_global_projects() -> None:
    fake_db = _FakeDb()
    project = SimpleNamespace(id=uuid.uuid4(), name="Core", created_at=datetime.now(tz=UTC))
    fake_db.execute_queue.append(_ExecuteResult([project]))

    client = _client_for_db(fake_db)
    try:
        response = client.get("/settings/projects")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == str(project.id)
    assert payload[0]["name"] == "Core"


def test_settings_api_tokens_list_and_revoke() -> None:
    fake_db = _FakeDb()
    actor_id = uuid.uuid4()
    token = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="collector",
        role=ProjectRole.ADMIN,
        scopes=["read:projects"],
        last_used_at=None,
        expires_at=None,
        created_at=datetime.now(tz=UTC),
        revoked_at=None,
    )
    row = SimpleNamespace(ApiToken=token, user_email="owner@example.com", project_name="Core")
    fake_db.execute_queue.append(_ExecuteResult([row]))
    fake_db.get_map[(ApiToken, token.id)] = token

    client = _client_for_db(fake_db, actor_user_id=actor_id)
    try:
        list_response = client.get("/settings/api-tokens")
        revoke_response = client.delete(f"/settings/api-tokens/{token.id}")
    finally:
        _clear_overrides()

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert len(list_payload["items"]) == 1
    assert list_payload["items"][0]["id"] == str(token.id)
    assert list_payload["items"][0]["user_email"] == "owner@example.com"
    assert list_payload["items"][0]["project_name"] == "Core"

    assert revoke_response.status_code == 200
    assert revoke_response.json() == {"ok": True}
    assert token.revoked_at is not None
    assert fake_db.commit_count == 2


def test_settings_audit_lists_global_events() -> None:
    fake_db = _FakeDb()
    event = SimpleNamespace(
        id=42,
        ts=datetime.now(tz=UTC),
        actor_user_id=uuid.uuid4(),
        actor_token_id=None,
        project_id=uuid.uuid4(),
        action="LOGIN_SUCCESS",
        object_type="user",
        object_id="test-user",
        metadata_json={"ip": "127.0.0.1"},
    )
    row = SimpleNamespace(AuditEvent=event, actor_email="admin@example.com", project_name="Core")
    fake_db.execute_queue.append(_ExecuteResult([row]))

    client = _client_for_db(fake_db)
    try:
        response = client.get("/settings/audit")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == 42
    assert payload["items"][0]["action"] == "LOGIN_SUCCESS"
    assert payload["items"][0]["actor_email"] == "admin@example.com"
    assert payload["items"][0]["project_name"] == "Core"
    assert payload["items"][0]["metadata"]["ip"] == "127.0.0.1"


def test_settings_rbac_upsert_and_remove_membership() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    existing_membership = SimpleNamespace(project_id=project_id, user_id=user_id, role=ProjectRole.VIEWER)
    fake_db.get_map[(Project, project_id)] = SimpleNamespace(id=project_id, name="Core")
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, email="analyst@example.com")
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": user_id}))] = existing_membership
    fake_db.execute_queue.append(_ExecuteResult([1]))

    client = _client_for_db(fake_db)
    try:
        upsert_response = client.post(
            "/settings/rbac/project-memberships",
            json={"project_id": str(project_id), "user_id": str(user_id), "role": "admin"},
        )
        remove_response = client.delete(f"/settings/rbac/project-memberships/{project_id}/{user_id}")
    finally:
        _clear_overrides()

    assert upsert_response.status_code == 200
    assert upsert_response.json() == {"ok": True}
    assert existing_membership.role == ProjectRole.ADMIN

    assert remove_response.status_code == 200
    assert remove_response.json() == {"ok": True}
    assert fake_db.deleted == [existing_membership]
    assert fake_db.commit_count == 2


def test_settings_rbac_lists_memberships() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    row = SimpleNamespace(
        project_id=project_id,
        project_name="Core",
        user_id=user_id,
        user_email="viewer@example.com",
        role=ProjectRole.VIEWER,
    )
    fake_db.execute_queue.append(_ExecuteResult([row]))

    client = _client_for_db(fake_db)
    try:
        response = client.get("/settings/rbac/project-memberships")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["project_id"] == str(project_id)
    assert payload["items"][0]["user_email"] == "viewer@example.com"
    assert payload["items"][0]["role"] == "viewer"


def test_settings_api_token_catalog_and_create_and_rotate() -> None:
    fake_db = _FakeDb()
    actor_id = uuid.uuid4()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    target_user = SimpleNamespace(id=user_id, email="owner@example.com", is_sysadmin=False, is_active=True, is_approved=True)
    target_project = SimpleNamespace(id=project_id, name="Core")
    target_membership = SimpleNamespace(project_id=project_id, user_id=user_id, role=ProjectRole.ADMIN)
    fake_db.get_map[(User, user_id)] = target_user
    fake_db.get_map[(Project, project_id)] = target_project
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": user_id}))] = target_membership

    client = _client_for_db(fake_db, actor_user_id=actor_id)
    try:
        catalog_response = client.get("/settings/api-token-scopes")
        create_response = client.post(
            "/settings/api-tokens",
            json={
                "user_id": str(user_id),
                "project_id": str(project_id),
                "name": "global-agent",
                "role": "admin",
                "scopes": [],
                "expires_in_days": 30,
            },
        )
        created_id = create_response.json()["token_meta"]["id"]
        created_token = next(obj for obj in fake_db.added if isinstance(obj, ApiToken))
        fake_db.get_map[(ApiToken, uuid.UUID(created_id))] = created_token
        rotate_response = client.post(f"/settings/api-tokens/{created_id}/rotate")
    finally:
        _clear_overrides()

    assert catalog_response.status_code == 200
    catalog_payload = catalog_response.json()
    assert "allowed_scopes" in catalog_payload
    assert "defaults_by_role" in catalog_payload

    assert create_response.status_code == 200
    create_payload = create_response.json()
    assert create_payload["token"]
    assert create_payload["token_meta"]["user_email"] == "owner@example.com"
    assert create_payload["token_meta"]["project_name"] == "Core"

    assert rotate_response.status_code == 200
    rotate_payload = rotate_response.json()
    assert rotate_payload["token"]
    assert rotate_payload["token_meta"]["id"] == created_id


def test_settings_rejects_removing_last_project_admin() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    membership = SimpleNamespace(project_id=project_id, user_id=user_id, role=ProjectRole.ADMIN)
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": user_id}))] = membership
    fake_db.execute_queue.append(_ExecuteResult([0]))

    client = _client_for_db(fake_db)
    try:
        response = client.delete(f"/settings/rbac/project-memberships/{project_id}/{user_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert "at least one project admin must remain" in response.json()["detail"]


def test_settings_rejects_demoting_last_project_admin() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    fake_db.get_map[(Project, project_id)] = SimpleNamespace(id=project_id, name="Core")
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, email="admin@example.com")
    membership = SimpleNamespace(project_id=project_id, user_id=user_id, role=ProjectRole.ADMIN)
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": user_id}))] = membership
    fake_db.execute_queue.append(_ExecuteResult([0]))

    client = _client_for_db(fake_db)
    try:
        response = client.post(
            "/settings/rbac/project-memberships",
            json={"project_id": str(project_id), "user_id": str(user_id), "role": "viewer"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert "at least one project admin must remain" in response.json()["detail"]


def test_settings_assign_user_all_projects_endpoint() -> None:
    fake_db = _FakeDb()
    user_id = uuid.uuid4()
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, email="user@example.com")
    fake_db.execute_queue.append(_ExecuteResult([SimpleNamespace(id=project_a), SimpleNamespace(id=project_b)]))

    client = _client_for_db(fake_db)
    try:
        response = client.post(
            f"/settings/rbac/users/{user_id}/assign-all-projects",
            json={"role": "operator", "overwrite_existing": False},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["assigned_projects"] == 2


def test_settings_token_create_rejects_non_member_target_user() -> None:
    fake_db = _FakeDb()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, email="owner@example.com", is_sysadmin=False, is_active=True, is_approved=True)
    fake_db.get_map[(Project, project_id)] = SimpleNamespace(id=project_id, name="Core")

    client = _client_for_db(fake_db)
    try:
        response = client.post(
            "/settings/api-tokens",
            json={
                "user_id": str(user_id),
                "project_id": str(project_id),
                "name": "collector",
                "role": "viewer",
                "scopes": [],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert "user must be a project member" in response.json()["detail"]
