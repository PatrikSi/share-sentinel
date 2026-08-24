import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

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
    statements: list[object] = field(default_factory=list)

    def execute(self, _statement):
        self.statements.append(_statement)
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


def test_parse_run_ids_deduplicates_and_bounds_query_width() -> None:
    run_id = uuid.uuid4()

    assert inventory_router._parse_run_ids(f"{run_id},{run_id}") == [run_id]

    oversized = ",".join(str(uuid.uuid4()) for _ in range(inventory_router.MAX_INVENTORY_RUN_IDS + 1))
    try:
        inventory_router._parse_run_ids(oversized)
    except Exception as exc:  # HTTPException is intentionally part of the router contract.
        assert getattr(exc, "status_code", None) == 400
        assert "too many run_ids" in str(getattr(exc, "detail", ""))
    else:
        raise AssertionError("oversized run_ids should be rejected")


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


def test_inventory_resources_accepts_unknown_and_exposes_access_capabilities(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    capabilities = {
        "list": {"status": "allowed", "attempted": 1, "allowed": 1, "denied": 0, "inconclusive": 0},
        "read_file": {"status": "inconclusive", "attempted": 1, "allowed": 0, "denied": 0, "inconclusive": 1},
    }
    fake_db = _FakeDb(
        execute_queue=[
            _ExecuteResult(
                [
                    SimpleNamespace(
                        id=7,
                        run_id=run_id,
                        run_name="Access probe",
                        endpoint_key="host:445",
                        hostname="host",
                        endpoint_metadata={"display_name": "File server"},
                        name="Finance",
                        remark=None,
                        access_level="unknown",
                        access_capabilities=capabilities,
                        resource_type="smb_share",
                        item_count=3,
                    )
                ]
            )
        ]
    )
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/projects/{project_id}/inventory/resources?access_level=unknown")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["items"][0]["access_level"] == "unknown"
    assert response.json()["items"][0]["access_capabilities"] == capabilities
    assert response.json()["items"][0]["provider"] == "smb"
    assert response.json()["items"][0]["endpoint_metadata"]["display_name"] == "File server"


def test_inventory_items_exposes_sharepoint_provider_identity_and_exposure(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_db = _FakeDb(
        execute_queue=[
            _ExecuteResult(
                [
                    SimpleNamespace(
                        id=11,
                        run_id=run_id,
                        run_name="Delegated view",
                        endpoint_key="sharepoint:site-1",
                        hostname="contoso.sharepoint.com",
                        ip=None,
                        endpoint_metadata={"display_name": "Finance site"},
                        resource_name="Documents",
                        access_level="list_only",
                        access_capabilities={},
                        resource_type="sharepoint_library",
                        resource_provider="sharepoint",
                        path="/Budgets/FY26.xlsx",
                        name="FY26.xlsx",
                        is_dir=False,
                        size_bytes=184933,
                        allocation_size_bytes=None,
                        mtime=None,
                        created_at=None,
                        accessed_at=None,
                        changed_at=None,
                        file_attributes=[],
                        provider="sharepoint",
                        provider_item_id="item-1",
                        provider_parent_id="parent-1",
                        web_url="https://contoso.sharepoint.com/sites/Finance/FY26.xlsx",
                        mime_type="application/vnd.test",
                        deleted=False,
                        provider_metadata={"site_id": "site-1", "drive_id": "drive-1"},
                        exposure="USER_VISIBLE",
                        exposure_evidence={"basis": "delegated_visibility"},
                    )
                ]
            )
        ]
    )
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(
            f"/projects/{project_id}/inventory/items",
            params={
                "provider": "sharepoint",
                "resource_type": "sharepoint_library",
                "exposure": "USER_VISIBLE",
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["share_type"] == "sharepoint"
    assert item["resource_type"] == "sharepoint_library"
    assert item["provider_item_id"] == "item-1"
    assert item["provider_parent_id"] == "parent-1"
    assert item["metadata"]["drive_id"] == "drive-1"
    assert item["exposure"] == "USER_VISIBLE"
    assert item["deleted"] is False
    assert item["endpoint_metadata"]["display_name"] == "Finance site"
    assert "items.deleted IS false" in str(fake_db.statements[0])


def test_inventory_endpoints_provider_filter_includes_legacy_endpoint_via_resources(
    monkeypatch,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_db = _FakeDb(
        execute_queue=[
            _ExecuteResult(
                [
                    SimpleNamespace(
                        id=17,
                        run_id=run_id,
                        run_name="Legacy SMB scan",
                        endpoint_key="fileserver:445",
                        ip="10.0.0.17",
                        hostname="fileserver",
                        domain="CONTOSO",
                        smb_signing="required",
                        provider=None,
                        provider_metadata={},
                        resource_count=2,
                        item_count=12,
                    )
                ]
            )
        ]
    )
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(
            f"/projects/{project_id}/inventory/endpoints",
            params={"provider": "smb", "q": "Finance Site"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["items"][0]["endpoint_key"] == "fileserver:445"
    sql = str(
        fake_db.statements[0].compile(compile_kwargs={"literal_binds": True})
    ).lower()
    assert "endpoints.provider" in sql
    assert "resources.provider" in sql
    assert "resources.resource_type" in sql
    assert "exists" in sql
    assert "count(distinct" in sql
    assert "display_name" in sql
    assert "site_name" in sql


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
