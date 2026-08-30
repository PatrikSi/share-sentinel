import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.db import get_db
from app.deps import AuthContext, get_auth_context
from app.enums import ErrorSeverity, RunStatus
from app.main import app
from app.routers import runs as runs_router
from fastapi.testclient import TestClient


class _ExecuteResult:
    def __init__(self, rows: list[Any]):
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
    commit_count: int = 0
    statements: list[Any] = field(default_factory=list)

    def execute(self, _statement):
        self.statements.append(_statement)
        if not self.execute_queue:
            raise AssertionError("unexpected execute() call")
        return self.execute_queue.pop(0)

    def commit(self):
        self.commit_count += 1


def _client_for_db(fake_db: _FakeDb) -> TestClient:
    actor_id = uuid.uuid4()

    def _override_auth():
        return AuthContext(user_id=actor_id, token_id=None, token_project_id=None, token_role=None, token_scopes=None)

    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_auth_context] = _override_auth
    return TestClient(app)


def _clear_overrides():
    app.dependency_overrides.clear()


def test_access_evidence_pages_enforce_response_memory_ceiling() -> None:
    fake_db = _FakeDb()
    client = _client_for_db(fake_db)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    base = f"/projects/{project_id}/runs/{run_id}/resources/1/access-evidence"

    try:
        assessment_response = client.get(
            base,
            params={
                "assessment_limit": runs_router.ACCESS_EVIDENCE_ASSESSMENT_PAGE_MAX + 1,
            },
        )
        entry_response = client.get(
            base,
            params={
                "entry_limit": runs_router.ACCESS_EVIDENCE_ENTRY_PAGE_MAX + 1,
            },
        )
    finally:
        _clear_overrides()

    assert assessment_response.status_code == 422
    assert entry_response.status_code == 422
    assert fake_db.statements == []


def test_to_run_out_includes_ingest_progress() -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Nightly ingest",
        description="Ops validation",
        target_scope={"hosts": ["fs-01"]},
        created_at=datetime.now(tz=UTC),
        status=RunStatus.INGESTING,
        artifact_size=4096,
        artifact_sha256="a" * 64,
        artifact_content_type="application/x-ndjson",
        ingest_progress={"line_offset": 2048},
        summary={"endpoints": 2, "resources": 5, "items": 100, "errors": 1},
        collection_context={
            "source": "sharepoint",
            "collection_mode": "delegated_user_view",
            "assessed_identity": "alice@contoso.example",
        },
    )

    payload = runs_router._to_run_out(run)

    assert payload.ingest_progress == {"line_offset": 2048}
    assert payload.artifact_sha256 == "a" * 64
    assert payload.artifact_content_type == "application/x-ndjson"
    assert payload.collection_context["source"] == "sharepoint"
    assert payload.collection_context["assessed_identity"] == "alice@contoso.example"


def test_list_run_errors_returns_issue_rows(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_run = SimpleNamespace(id=run_id, project_id=project_id)
    fake_error = SimpleNamespace(
        id=7,
        severity=ErrorSeverity.WARN,
        code="LIST_FILES_PARTIAL",
        message="stopped after file limit",
        endpoint_key="10.0.0.15:445",
        resource_name="Engineering",
        path="\\Engineering",
        created_at=datetime.now(tz=UTC),
    )
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([fake_run]), _ExecuteResult([fake_error])])
    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "write_audit_event", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/projects/{project_id}/runs/{run_id}/errors?severity=warn&search=limit")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["next_cursor"] is None
    assert payload["items"] == [
        {
            "id": 7,
            "severity": "warn",
            "code": "LIST_FILES_PARTIAL",
            "message": "stopped after file limit",
            "endpoint_key": "10.0.0.15:445",
            "resource_name": "Engineering",
            "path": "\\Engineering",
            "created_at": fake_error.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert fake_db.commit_count == 1


def test_list_run_activity_returns_timeline_rows(monkeypatch) -> None:
    assert "AUTOMATIC_BASELINE_UNAVAILABLE" in runs_router.RUN_ACTIVITY_ACTIONS
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_run = SimpleNamespace(id=run_id, project_id=project_id)
    event_ts = datetime.now(tz=UTC)
    fake_event = SimpleNamespace(
        id=11,
        ts=event_ts,
        action="INGEST_COMPLETED",
        object_type="scan_run",
        object_id=str(run_id),
        metadata_json={
            "line_offset": 26,
            "counts": {"errors": 1},
            "ip": "192.0.2.10",
            "user_agent": "sensitive workstation fingerprint",
            "request_id": "internal-correlation-id",
            "future_internal_field": {"secret": "must not escape"},
        },
    )
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([fake_run]), _ExecuteResult([fake_event])])
    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "write_audit_event", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/projects/{project_id}/runs/{run_id}/activity?limit=25")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["next_cursor"] is None
    assert payload["items"] == [
        {
            "id": 11,
            "ts": event_ts.isoformat().replace("+00:00", "Z"),
            "action": "INGEST_COMPLETED",
            "object_type": "scan_run",
            "object_id": str(run_id),
            "metadata": {"line_offset": 26, "counts": {"errors": 1}},
        }
    ]
    assert fake_db.commit_count == 1


def test_endpoint_resources_uses_bounded_keyset_pagination(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_run = SimpleNamespace(id=run_id, project_id=project_id)
    resources = [
        SimpleNamespace(
            id=10,
            resource_type="smb_share",
            name="Finance",
            remark=None,
            access_level="readable",
            access_capabilities={},
        ),
        SimpleNamespace(
            id=11,
            resource_type="smb_share",
            name="Engineering",
            remark=None,
            access_level="list_only",
            access_capabilities={},
        ),
    ]
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([fake_run]), _ExecuteResult(resources)])
    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "write_audit_event", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/projects/{project_id}/runs/{run_id}/endpoints/7/resources?limit=1")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["items"]] == [10]
    assert payload["next_cursor"]
    assert fake_db.commit_count == 1


def test_run_endpoints_searches_sharepoint_site_metadata_and_legacy_provider(
    monkeypatch,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_run = SimpleNamespace(id=run_id, project_id=project_id)
    endpoint = SimpleNamespace(
        id=7,
        endpoint_key="sharepoint:site-1",
        ip=None,
        hostname="contoso.sharepoint.com",
        domain=None,
        smb_dialect=None,
        smb_signing=None,
        auth_method="app",
        provider="sharepoint",
        provider_metadata={"display_name": "Finance site"},
    )
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([fake_run]), _ExecuteResult([endpoint])])
    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "write_audit_event", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(
            f"/projects/{project_id}/runs/{run_id}/endpoints",
            params={"search": "Finance site", "provider": "sharepoint"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    sql = str(fake_db.statements[1].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "display_name" in sql
    assert "site_name" in sql
    assert "resources.resource_type" in sql
    assert "resources.provider =" in sql


def test_endpoint_resources_rejects_endpoint_outside_project_run(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([])])
    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/projects/{project_id}/runs/{run_id}/endpoints/999/resources")
    finally:
        _clear_overrides()

    assert response.status_code == 404
    assert response.json()["detail"] == "endpoint not found in run"


def test_run_item_search_returns_sharepoint_resource_and_site_context(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fake_run = SimpleNamespace(id=run_id, project_id=project_id)
    item = SimpleNamespace(
        id=22,
        resource_id=9,
        resource_name="Shared Documents",
        resource_type="sharepoint_library",
        provider_resource_id="drive-1",
        endpoint_key="sharepoint:site-1",
        hostname="contoso.sharepoint.com",
        endpoint_metadata={"display_name": "Finance site", "site_id": "site-1"},
        path="/Budgets/FY26.xlsx",
        name="FY26.xlsx",
        is_dir=False,
        size_bytes=1024,
        allocation_size_bytes=None,
        mtime=None,
        created_at=None,
        accessed_at=None,
        changed_at=None,
        file_attributes=[],
        provider="sharepoint",
        provider_item_id="item-1",
        provider_parent_id="folder-1",
        web_url="https://contoso.sharepoint.com/sites/finance/Budgets/FY26.xlsx",
        mime_type="application/vnd.test",
        deleted=False,
        provider_metadata={"site_id": "site-1", "drive_id": "drive-1"},
        exposure="USER_VISIBLE",
        exposure_evidence={"basis": "delegated_visibility"},
    )
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([fake_run]), _ExecuteResult([item])])
    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "write_audit_event", lambda *_args, **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        response = client.get(
            f"/projects/{project_id}/runs/{run_id}/search/items",
            params={"q": "Finance site", "provider": "sharepoint"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()["items"][0]
    assert payload["resource_name"] == "Shared Documents"
    assert payload["provider_resource_id"] == "drive-1"
    assert payload["endpoint_key"] == "sharepoint:site-1"
    assert payload["endpoint_metadata"]["display_name"] == "Finance site"
    sql = str(fake_db.statements[1].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "items.provider =" in sql
    assert "items.deleted is false" in sql
    assert "display_name" in sql
