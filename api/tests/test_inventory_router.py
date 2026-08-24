import asyncio
import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_session_user
from app.main import app
from app.models import SavedInvestigation
from app.routers import inventory as inventory_router
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

@dataclass
class _FakeDb:
    execute_queue: list[_ExecuteResult | BaseException] = field(default_factory=list)
    get_map: dict[tuple[object, object], object] = field(default_factory=dict)
    added: list[object] = field(default_factory=list)
    deleted: list[object] = field(default_factory=list)
    commit_count: int = 0
    rollback_count: int = 0
    statements: list[object] = field(default_factory=list)

    def execute(self, _statement):
        self.statements.append(_statement)
        if not self.execute_queue:
            raise AssertionError("unexpected execute() call")
        result = self.execute_queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def get(self, model, key):
        return self.get_map.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

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


def test_inventory_csv_export_applies_item_type_scope_and_defends_spreadsheets(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    modified_at = datetime(2026, 8, 24, 12, 30, tzinfo=UTC)
    dangerous_path = '  +SUM(1,1), "quoted"\nnext'
    fake_db = _FakeDb(
        execute_queue=[
            _ExecuteResult(
                [
                    SimpleNamespace(
                        id=41,
                        run_id=run_id,
                        run_name="Quarterly export",
                        endpoint_key="fileserver:445",
                        hostname="fileserver",
                        ip="192.0.2.41",
                        endpoint_metadata={"display_name": 'Finance, "HQ"'},
                        resource_name="Finance",
                        access_level="readable",
                        access_capabilities={"read_file": {"status": "allowed"}},
                        resource_type="smb_share",
                        path=dangerous_path,
                        name='=HYPERLINK("https://example.test")',
                        is_dir=False,
                        size_bytes=2048,
                        allocation_size_bytes=4096,
                        mtime=modified_at,
                        created_at=None,
                        accessed_at=None,
                        changed_at=None,
                        file_attributes=["archive"],
                        provider="smb",
                        provider_item_id=None,
                        provider_parent_id=None,
                        web_url=None,
                        mime_type=None,
                        deleted=False,
                        provider_metadata={"note": "line one\nline two"},
                        exposure=None,
                        exposure_evidence={},
                    )
                ]
            )
        ]
    )
    audits: list[dict] = []
    terminal_audits: list[dict] = []
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_check_inventory_export_rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_audit_read", lambda *_args, **kwargs: audits.append(kwargs))
    monkeypatch.setattr(
        inventory_router,
        "_record_inventory_export_terminal_audit",
        lambda **kwargs: terminal_audits.append(kwargs),
    )

    client = _client_for_db(fake_db)
    try:
        response = client.get(
            f"/projects/{project_id}/inventory/export.csv",
            params={
                "tab": "items",
                "query_dsl": "item_type=file",
                "run_ids": str(run_id),
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-share-sentinel-export-consistency"] == (
        "high-watermark-bounded-live-non-snapshot"
    )
    assert response.headers["x-share-sentinel-export-high-watermark"] == "41"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="share-sentinel-inventory-items-{project_id}.csv"'
    )

    csv_rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(csv_rows) == 1
    exported = csv_rows[0]
    assert tuple(exported) == inventory_router.INVENTORY_EXPORT_COLUMNS["items"]
    assert exported["name"] == '\'=HYPERLINK("https://example.test")'
    assert exported["path"] == f"'{dangerous_path}"
    assert exported["item_type"] == "file"
    assert exported["is_dir"] == "false"
    assert exported["mtime"] == modified_at.isoformat()
    assert exported["endpoint_metadata"] == '{"display_name":"Finance, \\"HQ\\""}'
    assert exported["metadata"] == '{"note":"line one\\nline two"}'

    sql = str(fake_db.statements[0].compile(compile_kwargs={"literal_binds": True})).lower()
    normalized_sql = sql.replace("-", "")
    assert str(project_id).replace("-", "") in normalized_sql
    assert str(run_id).replace("-", "") in normalized_sql
    assert "items.is_dir is true" in sql
    assert "= 'file'" in sql
    assert "items.deleted is false" in sql
    assert "items.id <=" not in sql
    assert audits[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_STARTED"
    assert audits[0]["metadata"]["tab"] == "items"
    assert audits[0]["metadata"]["result_count"] == 0
    assert audits[0]["metadata"]["high_watermark"] == 41
    assert audits[0]["metadata"]["consistency"] == "high_watermark_bounded_live_non_snapshot"
    assert terminal_audits[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_COMPLETED"
    assert terminal_audits[0]["metadata"]["result_count"] == 1
    assert terminal_audits[0]["metadata"]["batch_count"] == 1
    assert fake_db.commit_count == 1
    assert fake_db.rollback_count == 1
    assert inventory_router.inventory_export_admission.active == 0


def test_inventory_csv_export_streams_more_than_twenty_thousand_rows_in_keyset_batches(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    total_rows = 20_005

    def endpoint_row(row_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=row_id,
            run_id=run_id,
            run_name="Scale export",
            endpoint_key=f"host-{row_id}:445",
            ip=f"192.0.2.{row_id % 255}",
            hostname=f"host-{row_id}",
            domain="EXAMPLE",
            smb_signing="required",
            provider="smb",
            provider_metadata={},
            resource_count=1,
            item_count=2,
        )

    first_batch = [
        endpoint_row(row_id)
        for row_id in range(total_rows, total_rows - inventory_router.INVENTORY_EXPORT_BATCH_SIZE, -1)
    ]
    fake_db = _FakeDb(execute_queue=[_ExecuteResult(first_batch)])
    audits: list[dict] = []
    terminal_audits: list[dict] = []
    after_ids: list[int] = []

    def load_batch(*_args, after_id: int, high_watermark: int, **_kwargs):
        assert high_watermark == total_rows
        after_ids.append(after_id)
        stop = max(0, after_id - inventory_router.INVENTORY_EXPORT_BATCH_SIZE - 1)
        return [endpoint_row(row_id) for row_id in range(after_id - 1, stop, -1)]

    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_check_inventory_export_rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_audit_read", lambda *_args, **kwargs: audits.append(kwargs))
    monkeypatch.setattr(inventory_router, "_load_inventory_export_batch_in_new_session", load_batch)
    monkeypatch.setattr(
        inventory_router,
        "_record_inventory_export_terminal_audit",
        lambda **kwargs: terminal_audits.append(kwargs),
    )

    client = _client_for_db(fake_db)
    try:
        response = client.get(
            f"/projects/{project_id}/inventory/export.csv",
            params={"tab": "endpoints", "run_ids": str(run_id)},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    reader = csv.DictReader(io.StringIO(response.text))
    row_count = 0
    first_id = None
    last_id = None
    for exported in reader:
        row_count += 1
        first_id = first_id or exported["id"]
        last_id = exported["id"]

    assert row_count == total_rows
    assert first_id == str(total_rows)
    assert last_id == "1"
    assert after_ids == list(
        range(
            total_rows - inventory_router.INVENTORY_EXPORT_BATCH_SIZE + 1,
            5,
            -inventory_router.INVENTORY_EXPORT_BATCH_SIZE,
        )
    )
    assert terminal_audits[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_COMPLETED"
    assert terminal_audits[0]["metadata"]["result_count"] == total_rows
    assert terminal_audits[0]["metadata"]["batch_count"] == 201
    sql = str(fake_db.statements[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "order by endpoints.id desc" in sql
    assert "endpoints.id <=" not in sql
    assert "limit 100" in sql
    assert fake_db.commit_count == 1
    assert inventory_router.inventory_export_admission.active == 0


def test_inventory_export_batch_uses_strict_descending_id_keyset() -> None:
    project_id = uuid.uuid4()
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([])])

    rows = inventory_router._inventory_export_batch(
        fake_db,
        project_id,
        "resources",
        [],
        [],
        include_deleted=False,
        high_watermark=900,
        after_id=501,
        batch_size=73,
    )

    assert rows == []
    sql = str(fake_db.statements[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "resources.id < 501" in sql
    assert "resources.id <= 900" in sql
    assert "order by resources.id desc" in sql
    assert "limit 73" in sql


def test_inventory_csv_stream_audits_generator_cancellation(monkeypatch) -> None:
    project_id = uuid.uuid4()
    terminal_audits: list[dict] = []
    monkeypatch.setattr(
        inventory_router,
        "_record_inventory_export_terminal_audit",
        lambda **kwargs: terminal_audits.append(kwargs),
    )
    auth = AuthContext(
        user_id=uuid.uuid4(),
        token_id=None,
        token_project_id=None,
        token_role=None,
        token_scopes=None,
    )
    stream = inventory_router._inventory_csv_stream(
        project_id=project_id,
        tab="items",
        run_ids=[],
        query_groups=[],
        include_deleted=False,
        high_watermark=0,
        first_batch=[],
        export_id="export-test",
        auth=auth,
        request_metadata={},
        scope_metadata={"tab": "items"},
    )

    assert next(stream).startswith(b"id,run_id,run_name")
    stream.close()

    assert terminal_audits[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_CANCELLED"
    assert terminal_audits[0]["metadata"]["result_count"] == 0
    assert terminal_audits[0]["metadata"]["batch_count"] == 0


def test_inventory_csv_export_has_stable_headers_for_every_tab(monkeypatch) -> None:
    project_id = uuid.uuid4()
    fake_db = _FakeDb(
        execute_queue=[
            _ExecuteResult([]),
            _ExecuteResult([]),
            _ExecuteResult([]),
        ]
    )
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_check_inventory_export_rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_audit_read", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_record_inventory_export_terminal_audit", lambda **_kwargs: None)

    client = _client_for_db(fake_db)
    try:
        responses = {
            tab: client.get(f"/projects/{project_id}/inventory/export.csv", params={"tab": tab})
            for tab in ("items", "resources", "endpoints")
        }
    finally:
        _clear_overrides()

    for tab, response in responses.items():
        assert response.status_code == 200
        header = next(csv.reader(io.StringIO(response.text)))
        assert tuple(header) == inventory_router.INVENTORY_EXPORT_COLUMNS[tab]
        assert f"inventory-{tab}-{project_id}.csv" in response.headers["content-disposition"]


def test_inventory_csv_chunks_bound_large_metadata_and_preserve_valid_csv() -> None:
    payload = "x" * 750_000
    row = SimpleNamespace(
        id=17,
        run_id=uuid.uuid4(),
        run_name="Large metadata",
        endpoint_key="server:445",
        ip="192.0.2.17",
        hostname="server",
        domain="EXAMPLE",
        smb_signing="required",
        provider="smb",
        provider_metadata={"payload": payload},
        resource_count=1,
        item_count=2,
    )

    chunks = list(
        inventory_router._inventory_csv_chunks(
            "endpoints",
            [row],
            include_header=True,
            max_chunk_bytes=4096,
        )
    )

    assert len(chunks) > 100
    assert all(0 < len(chunk) <= 4096 for chunk in chunks)
    previous_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(len(payload) * 2)
        exported = next(csv.DictReader(io.StringIO(b"".join(chunks).decode("utf-8"))))
    finally:
        csv.field_size_limit(previous_limit)
    assert exported["id"] == "17"
    assert exported["metadata"] == '{"payload":"' + payload + '"}'


def test_inventory_csv_chunks_reject_non_positive_chunk_budget() -> None:
    with pytest.raises(ValueError, match="max_chunk_bytes must be greater than zero"):
        list(inventory_router._inventory_csv_chunks("items", [], max_chunk_bytes=0))


def test_inventory_csv_stream_midstream_failure_audits_and_closes_batch_session(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    batch_session = SimpleNamespace(rollback_count=0, close_count=0)

    def execute(_statement):
        raise RuntimeError("database page failed")

    def rollback():
        batch_session.rollback_count += 1

    def close():
        batch_session.close_count += 1

    batch_session.execute = execute
    batch_session.rollback = rollback
    batch_session.close = close
    terminal_audits: list[dict[str, Any]] = []
    monkeypatch.setattr(inventory_router, "INVENTORY_EXPORT_BATCH_SIZE", 1)
    monkeypatch.setattr(inventory_router, "SessionLocal", lambda: batch_session)
    monkeypatch.setattr(
        inventory_router,
        "_record_inventory_export_terminal_audit",
        lambda **kwargs: terminal_audits.append(kwargs),
    )
    first_row = SimpleNamespace(
        id=9,
        run_id=run_id,
        run_name="Failure test",
        endpoint_key="server:445",
        ip="192.0.2.9",
        hostname="server",
        domain="EXAMPLE",
        smb_signing="required",
        provider="smb",
        provider_metadata={},
        resource_count=1,
        item_count=1,
    )
    auth = AuthContext(
        user_id=uuid.uuid4(),
        token_id=None,
        token_project_id=None,
        token_role=None,
        token_scopes=None,
    )
    stream = inventory_router._inventory_csv_stream(
        project_id=project_id,
        tab="endpoints",
        run_ids=[],
        query_groups=[],
        include_deleted=False,
        high_watermark=9,
        first_batch=[first_row],
        export_id="export-failure",
        auth=auth,
        request_metadata={},
        scope_metadata={"tab": "endpoints"},
    )

    with pytest.raises(RuntimeError, match="database page failed"):
        list(stream)

    assert batch_session.rollback_count == 1
    assert batch_session.close_count == 1
    assert terminal_audits[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_FAILED"
    assert terminal_audits[0]["metadata"]["result_count"] == 1
    assert terminal_audits[0]["metadata"]["batch_count"] == 1
    assert terminal_audits[0]["metadata"]["failure_type"] == "RuntimeError"


def test_inventory_csv_terminal_audit_failure_is_best_effort_and_closes_session(monkeypatch) -> None:
    audit_session = SimpleNamespace(commit_count=0, rollback_count=0, close_count=0)

    def commit():
        audit_session.commit_count += 1
        raise RuntimeError("audit database unavailable")

    def rollback():
        audit_session.rollback_count += 1

    def close():
        audit_session.close_count += 1

    audit_session.commit = commit
    audit_session.rollback = rollback
    audit_session.close = close
    monkeypatch.setattr(inventory_router, "SessionLocal", lambda: audit_session)
    monkeypatch.setattr(inventory_router, "write_audit_event", lambda *_args, **_kwargs: None)
    auth = AuthContext(
        user_id=uuid.uuid4(),
        token_id=None,
        token_project_id=None,
        token_role=None,
        token_scopes=None,
    )
    stream = inventory_router._inventory_csv_stream(
        project_id=uuid.uuid4(),
        tab="items",
        run_ids=[],
        query_groups=[],
        include_deleted=False,
        high_watermark=0,
        first_batch=[],
        export_id="export-audit-failure",
        auth=auth,
        request_metadata={},
        scope_metadata={"tab": "items"},
    )

    output = b"".join(stream)

    assert output.startswith(b"id,run_id,run_name")
    assert audit_session.commit_count == 1
    assert audit_session.rollback_count == 1
    assert audit_session.close_count == 1


def test_inventory_csv_asgi_disconnect_closes_generator_audits_and_releases_slot(monkeypatch) -> None:
    terminal_audits: list[dict[str, Any]] = []
    releases: list[bool] = []
    monkeypatch.setattr(
        inventory_router,
        "_record_inventory_export_terminal_audit",
        lambda **kwargs: terminal_audits.append(kwargs),
    )
    auth = AuthContext(
        user_id=uuid.uuid4(),
        token_id=None,
        token_project_id=None,
        token_role=None,
        token_scopes=None,
    )
    row = SimpleNamespace(
        id=1,
        run_id=uuid.uuid4(),
        run_name="Disconnect",
        endpoint_key="server:445",
        ip="192.0.2.1",
        hostname="server",
        domain="EXAMPLE",
        smb_signing="required",
        provider="smb",
        provider_metadata={"payload": "x" * (inventory_router.INVENTORY_EXPORT_CHUNK_BYTES * 2)},
        resource_count=1,
        item_count=1,
    )
    sync_stream = inventory_router._inventory_csv_stream(
        project_id=uuid.uuid4(),
        tab="endpoints",
        run_ids=[],
        query_groups=[],
        include_deleted=False,
        high_watermark=1,
        first_batch=[row],
        export_id="export-disconnect",
        auth=auth,
        request_metadata={},
        scope_metadata={"tab": "endpoints"},
    )
    has_initial_chunk, initial_chunk = inventory_router._next_inventory_csv_chunk(sync_stream)
    assert has_initial_chunk is True
    assert initial_chunk is not None
    response = inventory_router._InventoryCSVStreamingResponse(
        sync_stream,
        initial_chunk=initial_chunk,
        release_slot=lambda: releases.append(True),
        media_type="text/csv",
        headers={},
    )

    async def disconnect_after_first_body() -> list[dict[str, Any]]:
        first_body = asyncio.Event()
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()
                await asyncio.Event().wait()

        async def receive() -> dict[str, str]:
            await first_body.wait()
            return {"type": "http.disconnect"}

        await response(
            {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}},
            receive,
            send,
        )
        return sent

    sent_messages = asyncio.run(disconnect_after_first_body())

    assert any(message["type"] == "http.response.body" for message in sent_messages)
    assert releases == [True]
    assert terminal_audits[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_CANCELLED"
    assert terminal_audits[0]["metadata"]["result_count"] == 1


def test_inventory_csv_disconnect_before_body_still_audits_and_releases_slot(monkeypatch) -> None:
    terminal_audits: list[dict[str, Any]] = []
    releases: list[bool] = []
    monkeypatch.setattr(
        inventory_router,
        "_record_inventory_export_terminal_audit",
        lambda **kwargs: terminal_audits.append(kwargs),
    )
    auth = AuthContext(
        user_id=uuid.uuid4(),
        token_id=None,
        token_project_id=None,
        token_role=None,
        token_scopes=None,
    )
    sync_stream = inventory_router._inventory_csv_stream(
        project_id=uuid.uuid4(),
        tab="items",
        run_ids=[],
        query_groups=[],
        include_deleted=False,
        high_watermark=0,
        first_batch=[],
        export_id="export-early-disconnect",
        auth=auth,
        request_metadata={},
        scope_metadata={"tab": "items"},
    )
    has_initial_chunk, initial_chunk = inventory_router._next_inventory_csv_chunk(sync_stream)
    assert has_initial_chunk is True
    assert initial_chunk is not None
    response = inventory_router._InventoryCSVStreamingResponse(
        sync_stream,
        initial_chunk=initial_chunk,
        release_slot=lambda: releases.append(True),
        media_type="text/csv",
        headers={},
    )

    async def fail_response_start() -> None:
        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        async def send(_message: dict[str, Any]) -> None:
            raise OSError("client disconnected before response start")

        with pytest.raises(ClientDisconnect):
            await response(
                {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}},
                receive,
                send,
            )

    asyncio.run(fail_response_start())

    assert releases == [True]
    assert terminal_audits[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_CANCELLED"
    assert terminal_audits[0]["metadata"]["result_count"] == 0


def test_inventory_csv_preflight_failure_preserves_original_when_failure_audit_fails(monkeypatch) -> None:
    project_id = uuid.uuid4()
    fake_db = _FakeDb(execute_queue=[RuntimeError("original preflight failure")])
    failed_audit_attempts: list[dict[str, Any]] = []
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_check_inventory_export_rate_limit", lambda *_args, **_kwargs: None)

    def failed_audit(*_args, **kwargs):
        failed_audit_attempts.append(kwargs)
        raise RuntimeError("secondary audit failure")

    monkeypatch.setattr(inventory_router, "_audit_read", failed_audit)
    client = _client_for_db(fake_db)
    try:
        with pytest.raises(RuntimeError, match="original preflight failure"):
            client.get(f"/projects/{project_id}/inventory/export.csv")
    finally:
        _clear_overrides()

    assert failed_audit_attempts[0]["action"] == "PROJECT_INVENTORY_CSV_EXPORT_FAILED"
    assert fake_db.rollback_count == 2
    assert inventory_router.inventory_export_admission.active == 0


def test_inventory_csv_export_rejects_when_process_capacity_is_exhausted(monkeypatch) -> None:
    project_id = uuid.uuid4()
    fake_db = _FakeDb()
    admission = inventory_router._InventoryExportAdmission(1)
    assert admission.try_acquire() is True
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_check_inventory_export_rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "inventory_export_admission", admission)
    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/projects/{project_id}/inventory/export.csv")
    finally:
        _clear_overrides()
        admission.release()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "capacity" in response.json()["detail"]
    assert fake_db.statements == []


def test_inventory_csv_rate_limit_uses_configured_actor_scope(monkeypatch) -> None:
    token_id = uuid.uuid4()
    captured: dict[str, Any] = {}
    request = inventory_router.Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/inventory/export.csv",
            "headers": [],
            "client": ("192.0.2.44", 4444),
        }
    )
    auth = AuthContext(
        user_id=None,
        token_id=token_id,
        token_project_id=uuid.uuid4(),
        token_role=None,
        token_scopes=None,
    )
    monkeypatch.setattr(
        inventory_router,
        "get_settings",
        lambda: SimpleNamespace(
            api_inventory_export_rate_limit=7,
            api_inventory_export_rate_window_seconds=90,
        ),
    )
    monkeypatch.setattr(
        inventory_router.rate_limiter,
        "check",
        lambda *args, **kwargs: captured.update({"args": args, "kwargs": kwargs}),
    )

    inventory_router._check_inventory_export_rate_limit(request, auth)

    assert captured["args"] == (request, "inventory_export")
    assert captured["kwargs"] == {
        "limit": 7,
        "window_seconds": 90,
        "actor_key": f"inventory-export:{token_id}",
    }


def test_inventory_csv_export_rate_limit_runs_before_process_admission(monkeypatch) -> None:
    project_id = uuid.uuid4()
    fake_db = _FakeDb()
    admission = inventory_router._InventoryExportAdmission(1)
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "inventory_export_admission", admission)

    def reject_rate_limit(*_args, **_kwargs):
        raise inventory_router.HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": "60"},
        )

    monkeypatch.setattr(inventory_router, "_check_inventory_export_rate_limit", reject_rate_limit)
    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/projects/{project_id}/inventory/export.csv")
    finally:
        _clear_overrides()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert admission.active == 0
    assert fake_db.statements == []


def test_inventory_csv_export_releases_admission_if_response_construction_fails(monkeypatch) -> None:
    project_id = uuid.uuid4()
    fake_db = _FakeDb(execute_queue=[_ExecuteResult([])])
    admission = inventory_router._InventoryExportAdmission(1)
    monkeypatch.setattr(inventory_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_check_inventory_export_rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_audit_read", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory_router, "_record_inventory_export_terminal_audit", lambda **_kwargs: None)
    monkeypatch.setattr(inventory_router, "inventory_export_admission", admission)

    def fail_response(*_args, **_kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(inventory_router, "_InventoryCSVStreamingResponse", fail_response)
    client = _client_for_db(fake_db)
    try:
        with pytest.raises(RuntimeError, match="response construction failed"):
            client.get(f"/projects/{project_id}/inventory/export.csv")
    finally:
        _clear_overrides()

    assert admission.active == 0


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
