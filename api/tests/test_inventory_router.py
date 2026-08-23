import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_session_user
from app.main import app
from app.models import SavedInvestigation
from app.routers import inventory as inventory_router
from fastapi.testclient import TestClient


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        return self._rows[0]


@dataclass
class _FakeDb:
    execute_queue: list[_ExecuteResult] = field(default_factory=list)
    get_map: dict[tuple[object, object], object] = field(default_factory=dict)
    added: list[object] = field(default_factory=list)
    deleted: list[object] = field(default_factory=list)
    commit_count: int = 0

    def execute(self, _statement):
        if not self.execute_queue:
            raise AssertionError("unexpected execute() call")
        return self.execute_queue.pop(0)

    def get(self, model, key):
        return self.get_map.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commit_count += 1

    def flush(self):
        now = datetime.now(tz=UTC)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if getattr(obj, "created_at", None) is None:
                obj.created_at = now
            if getattr(obj, "updated_at", None) is None:
                obj.updated_at = now

    def refresh(self, _obj):
        return None


def _client_for_db(fake_db: _FakeDb) -> TestClient:
    user_id = uuid.uuid4()

    def _override_auth():
        return AuthContext(user_id=user_id, token_id=None, token_project_id=None, token_role=None, token_scopes=None)

    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_auth_context] = _override_auth
    app.dependency_overrides[require_session_user] = lambda: object()
    return TestClient(app)


def _clear_overrides():
    app.dependency_overrides.clear()


def test_inventory_saved_investigations_create_list_and_delete(monkeypatch) -> None:
    project_id = uuid.uuid4()
    fake_db = _FakeDb()
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)

    existing = SavedInvestigation(
        project_id=project_id,
        created_by_user_id=uuid.uuid4(),
        name="Existing search",
        description="Old investigation",
        target_tab="items",
        query_text="ext:.xlsx endpoint:fs-01",
        definition_json={"active_tab": "items", "ext": ".xlsx"},
    )
    existing.id = uuid.uuid4()
    existing.created_at = datetime.now(tz=UTC)
    existing.updated_at = existing.created_at
    fake_db.execute_queue.append(_ExecuteResult([existing]))

    client = _client_for_db(fake_db)
    try:
        create_response = client.post(
            f"/projects/{project_id}/inventory/investigations",
            json={
                "name": "Quarterly review",
                "description": "Track finance workbooks",
                "target_tab": "items",
                "query_text": 'endpoint:fs-01 ext:.xlsx "quarterly review"',
                "definition": {"active_tab": "items", "endpoint": "fs-01", "ext": ".xlsx"},
            },
        )
        created_id = uuid.UUID(create_response.json()["id"])
        created = next(obj for obj in fake_db.added if isinstance(obj, SavedInvestigation))
        fake_db.get_map[(SavedInvestigation, created_id)] = created

        list_response = client.get(f"/projects/{project_id}/inventory/investigations")
        delete_response = client.delete(f"/projects/{project_id}/inventory/investigations/{created_id}")
    finally:
        _clear_overrides()

    assert create_response.status_code == 200
    assert create_response.json()["name"] == "Quarterly review"
    assert create_response.json()["definition"]["endpoint"] == "fs-01"

    assert list_response.status_code == 200
    payload = list_response.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["name"] == "Existing search"

    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True}
    assert fake_db.deleted == [created]


def test_inventory_saved_investigations_update(monkeypatch) -> None:
    project_id = uuid.uuid4()
    investigation_id = uuid.uuid4()
    fake_db = _FakeDb()
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)

    existing = SavedInvestigation(
        project_id=project_id,
        created_by_user_id=uuid.uuid4(),
        name="Existing search",
        description="Old investigation",
        target_tab="items",
        query_text="ext:.xlsx endpoint:fs-01",
        definition_json={"active_tab": "items", "ext": ".xlsx"},
    )
    existing.id = investigation_id
    existing.created_at = datetime.now(tz=UTC)
    existing.updated_at = existing.created_at
    fake_db.get_map[(SavedInvestigation, investigation_id)] = existing

    client = _client_for_db(fake_db)
    try:
        update_response = client.patch(
            f"/projects/{project_id}/inventory/investigations/{investigation_id}",
            json={
                "name": "Finance view",
                "description": "Updated investigation",
                "target_tab": "resources",
                "query_text": "access = readable",
                "definition": {"active_tab": "resources", "resource_access": "readable"},
            },
        )
    finally:
        _clear_overrides()

    assert update_response.status_code == 200
    payload = update_response.json()
    assert payload["name"] == "Finance view"
    assert payload["target_tab"] == "resources"
    assert payload["definition"]["resource_access"] == "readable"
    assert existing.name == "Finance view"
    assert existing.query_text == "access = readable"
