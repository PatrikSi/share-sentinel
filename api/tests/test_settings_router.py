import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app.db import get_db
from app.deps import AuthContext, get_auth_context, require_sysadmin
from app.enums import ProjectRole, RunStatus
from app.main import app
from app.models import ApiToken, AuditEvent, Project, ProjectMember, User


class _ExecuteResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        if not self._rows:
            return None
        return self._rows[0]

    def scalars(self):
        return self

    def scalar(self):
        if not self._rows:
            return None
        return self._rows[0]


@dataclass
class _FakeDb:
    execute_queue: list[_ExecuteResult] = field(default_factory=list)
    executed_statements: list[tuple[Any, Any | None]] = field(default_factory=list)
    get_map: dict[tuple[Any, Any], Any] = field(default_factory=dict)
    added: list[Any] = field(default_factory=list)
    deleted: list[Any] = field(default_factory=list)
    commit_count: int = 0

    def execute(self, _statement, params=None):
        self.executed_statements.append((_statement, params))
        statement_text = str(_statement)
        if "pg_try_advisory_xact_lock" in statement_text:
            return _ExecuteResult([True])
        if "pg_advisory_xact_lock" in statement_text:
            return _ExecuteResult([1])
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

    def rollback(self):
        return None

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


def _project_catalog_row(project_id: uuid.UUID, **overrides: Any) -> SimpleNamespace:
    values = {
        "project_id": project_id,
        "project_name": "Core",
        "created_at": datetime.now(tz=UTC),
        "member_count": 3,
        "admin_count": 1,
        "token_count": 4,
        "active_token_count": 3,
        "run_count": 2,
        "artifact_count": 2,
        "blocking_run_count": 0,
        "last_run_at": datetime.now(tz=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


def test_settings_project_catalog_and_detail() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    blocking_run = SimpleNamespace(
        id=uuid.uuid4(),
        name="Queued ingest",
        status=RunStatus.UPLOADED,
        created_at=datetime.now(tz=UTC),
    )
    fake_db.execute_queue.extend(
        [
            _ExecuteResult([_project_catalog_row(project_id, blocking_run_count=1)]),
            _ExecuteResult([_project_catalog_row(project_id, blocking_run_count=1)]),
            _ExecuteResult(
                [
                    SimpleNamespace(status=RunStatus.COMPLETE, count=5),
                    SimpleNamespace(status=RunStatus.UPLOADED, count=1),
                ]
            ),
            _ExecuteResult([blocking_run]),
        ]
    )

    client = _client_for_db(fake_db)
    try:
        catalog_response = client.get("/settings/projects/catalog")
        detail_response = client.get(f"/settings/projects/{project_id}")
    finally:
        _clear_overrides()

    assert catalog_response.status_code == 200
    catalog_payload = catalog_response.json()
    assert len(catalog_payload["items"]) == 1
    assert catalog_payload["items"][0]["id"] == str(project_id)
    assert catalog_payload["items"][0]["has_blocking_runs"] is True
    assert catalog_payload["items"][0]["blocking_run_count"] == 1

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["id"] == str(project_id)
    assert detail_payload["run_status_counts"]["COMPLETE"] == 5
    assert detail_payload["run_status_counts"]["UPLOADED"] == 1
    assert detail_payload["run_status_counts"]["INGESTING"] == 0
    assert detail_payload["blocking_runs"] == [
        {
            "id": str(blocking_run.id),
            "name": "Queued ingest",
            "status": "UPLOADED",
            "created_at": blocking_run.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]


def test_settings_project_rename() -> None:
    fake_db = _FakeDb()
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    project = SimpleNamespace(id=project_id, name="Core", created_at=datetime.now(tz=UTC))
    fake_db.get_map[(Project, project_id)] = project

    client = _client_for_db(fake_db, actor_user_id=actor_id)
    try:
        response = client.patch(f"/settings/projects/{project_id}", json={"name": "Renamed Core"})
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(project_id)
    assert payload["name"] == "Renamed Core"
    assert project.name == "Renamed Core"
    audit_event = next(obj for obj in fake_db.added if isinstance(obj, AuditEvent))
    assert audit_event.action == "SETTINGS_PROJECT_RENAMED"
    assert audit_event.metadata_json["previous_name"] == "Core"


def test_settings_project_delete_reports_artifact_failures(monkeypatch) -> None:
    fake_db = _FakeDb()
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    project = SimpleNamespace(id=project_id, name="Core")
    run_a = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        name="Baseline",
        status=RunStatus.COMPLETE,
        created_at=datetime.now(tz=UTC),
        artifact_key="projects/core/runs/a/artifact.ndjson",
    )
    run_b = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        name="Follow-up",
        status=RunStatus.FAILED,
        created_at=datetime.now(tz=UTC),
        artifact_key="projects/core/runs/b/artifact.ndjson",
    )
    fake_db.get_map[(Project, project_id)] = project
    fake_db.execute_queue.append(_ExecuteResult([run_a, run_b]))

    def _fake_delete_object(key: str) -> None:
        if key == run_b.artifact_key:
            raise OSError("disk busy")

    monkeypatch.setattr("app.routers.settings.delete_object", _fake_delete_object)

    client = _client_for_db(fake_db, actor_user_id=actor_id)
    try:
        response = client.delete(f"/settings/projects/{project_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == str(project_id)
    assert payload["project_name"] == "Core"
    assert payload["deleted_run_count"] == 2
    assert payload["deleted_artifact_count"] == 2
    assert payload["artifact_delete_failures"] == [
        {
            "artifact_key": run_b.artifact_key,
            "error": "disk busy",
        }
    ]
    assert fake_db.deleted == [project]
    assert fake_db.commit_count == 1
    audit_event = next(obj for obj in fake_db.added if isinstance(obj, AuditEvent))
    assert audit_event.action == "SETTINGS_PROJECT_DELETED"
    assert audit_event.metadata_json["deleted_run_count"] == 2


def test_settings_project_delete_blocks_uploaded_or_ingesting_runs() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    fake_db.get_map[(Project, project_id)] = SimpleNamespace(id=project_id, name="Core")
    fake_db.execute_queue.append(
        _ExecuteResult(
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    name="Queued ingest",
                    status=RunStatus.UPLOADED,
                    created_at=datetime.now(tz=UTC),
                    artifact_key="projects/core/runs/a/artifact.ndjson",
                )
            ]
        )
    )

    client = _client_for_db(fake_db)
    try:
        response = client.delete(f"/settings/projects/{project_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 409
    assert "UPLOADED or INGESTING" in response.json()["detail"]
    assert fake_db.deleted == []
    assert fake_db.commit_count == 0


def test_settings_project_delete_blocks_when_run_lock_cannot_be_acquired(monkeypatch) -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    fake_db.get_map[(Project, project_id)] = SimpleNamespace(id=project_id, name="Core")
    fake_db.execute_queue.append(
        _ExecuteResult(
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    name="Complete run",
                    status=RunStatus.COMPLETE,
                    created_at=datetime.now(tz=UTC),
                    artifact_key="projects/core/runs/a/artifact.ndjson",
                )
            ]
        )
    )
    monkeypatch.setattr("app.routers.settings._try_lock_run_for_mutation", lambda *_args: False)

    client = _client_for_db(fake_db)
    try:
        response = client.delete(f"/settings/projects/{project_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 409
    assert "cannot be locked" in response.json()["detail"]
    assert fake_db.deleted == []
    assert fake_db.commit_count == 0


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


def test_settings_api_tokens_support_exact_project_filter() -> None:
    fake_db = _FakeDb()
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        project_id=project_id,
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

    client = _client_for_db(fake_db, actor_user_id=actor_id)
    try:
        response = client.get(f"/settings/api-tokens?project_id={project_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert "api_tokens.project_id" in str(fake_db.executed_statements[0][0])
    audit_event = next(obj for obj in fake_db.added if isinstance(obj, AuditEvent))
    assert audit_event.metadata_json["project_id"] == str(project_id)


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


def test_settings_audit_supports_exact_project_filter() -> None:
    fake_db = _FakeDb()
    project_id = uuid.uuid4()
    event = SimpleNamespace(
        id=44,
        ts=datetime.now(tz=UTC),
        actor_user_id=uuid.uuid4(),
        actor_token_id=None,
        project_id=project_id,
        action="RUN_CREATED",
        object_type="scan_run",
        object_id="run-2",
        metadata_json={"ip": "127.0.0.1"},
    )
    row = SimpleNamespace(AuditEvent=event, actor_email="admin@example.com", project_name="Core")
    fake_db.execute_queue.append(_ExecuteResult([row]))

    client = _client_for_db(fake_db)
    try:
        response = client.get(f"/settings/audit?project_id={project_id}")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert "audit_events.project_id" in str(fake_db.executed_statements[0][0])
    audit_event = next(obj for obj in fake_db.added if isinstance(obj, AuditEvent))
    assert audit_event.metadata_json["project_id"] == str(project_id)


def test_settings_audit_export_returns_csv_attachment() -> None:
    fake_db = _FakeDb()
    event = SimpleNamespace(
        id=43,
        ts=datetime.now(tz=UTC),
        actor_user_id=uuid.uuid4(),
        actor_token_id=None,
        project_id=uuid.uuid4(),
        action="RUN_CREATED",
        object_type="scan_run",
        object_id="run-1",
        metadata_json={"ip": "127.0.0.1", "source": "ui"},
    )
    row = SimpleNamespace(AuditEvent=event, actor_email="auditor@example.com", project_name="Core")
    fake_db.execute_queue.append(_ExecuteResult([row]))

    client = _client_for_db(fake_db)
    try:
        response = client.get("/settings/audit/export?format=csv&q=run")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in response.headers["content-disposition"]
    assert "RUN_CREATED" in response.text
    assert "auditor@example.com" in response.text
    assert '"source"' in response.text
    assert '"ui"' in response.text


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


def test_settings_overview_returns_aggregate_counts() -> None:
    fake_db = _FakeDb()
    fake_db.execute_queue.extend(
        [
            _ExecuteResult([7]),
            _ExecuteResult([5]),
            _ExecuteResult([2]),
            _ExecuteResult([1]),
            _ExecuteResult([9]),
            _ExecuteResult([6]),
            _ExecuteResult([3]),
            _ExecuteResult([datetime(2026, 3, 12, tzinfo=UTC)]),
            _ExecuteResult([4]),
            _ExecuteResult(
                [
                    SimpleNamespace(
                        AuditEvent=SimpleNamespace(
                            id=11,
                            ts=datetime.now(tz=UTC),
                            actor_user_id=uuid.uuid4(),
                            actor_token_id=None,
                            project_id=uuid.uuid4(),
                            action="LOGIN_SUCCESS",
                            object_type="user",
                            object_id="user-1",
                            metadata_json={"ip": "127.0.0.1"},
                        ),
                        actor_email="admin@example.com",
                        project_name="Core",
                    )
                ]
            ),
        ]
    )

    client = _client_for_db(fake_db)
    try:
        response = client.get("/settings/overview")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["users"] == {"total": 7, "active": 5, "pending": 2, "sysadmins": 1}
    assert payload["tokens"]["total"] == 9
    assert payload["tokens"]["active"] == 6
    assert payload["tokens"]["revoked"] == 3
    assert payload["tokens"]["never_expires"] == 3
    assert payload["security"]["allow_never_expiring_api_tokens"] is False
    assert payload["projects"]["total"] == 4
    assert payload["recent_audit"][0]["project_name"] == "Core"


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


def test_settings_api_token_create_rejects_zero_day_default_when_never_expiring_tokens_are_disabled(monkeypatch) -> None:
    fake_db = _FakeDb()
    actor_id = uuid.uuid4()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    fake_db.get_map[(User, user_id)] = SimpleNamespace(
        id=user_id,
        email="owner@example.com",
        is_sysadmin=False,
        is_active=True,
        is_approved=True,
    )
    fake_db.get_map[(Project, project_id)] = SimpleNamespace(id=project_id, name="Core")
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": user_id}))] = SimpleNamespace(
        project_id=project_id,
        user_id=user_id,
        role=ProjectRole.ADMIN,
    )

    monkeypatch.setattr("app.routers.settings.get_settings", lambda: SimpleNamespace(default_api_token_expiry_days=0, allow_never_expiring_api_tokens=False))

    client = _client_for_db(fake_db, actor_user_id=actor_id)
    try:
        response = client.post(
            "/settings/api-tokens",
            json={
                "user_id": str(user_id),
                "project_id": str(project_id),
                "name": "global-agent",
                "role": "admin",
                "scopes": [],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert response.json()["detail"] == "never-expiring api tokens are disabled"


def test_settings_api_token_update_rejects_never_expiring_tokens_by_default(monkeypatch) -> None:
    fake_db = _FakeDb()
    actor_id = uuid.uuid4()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token_id = uuid.uuid4()
    fake_db.get_map[(ApiToken, token_id)] = SimpleNamespace(
        id=token_id,
        user_id=user_id,
        project_id=project_id,
        name="collector",
        role=ProjectRole.ADMIN,
        scopes=["read:projects"],
        expires_at=datetime.now(tz=UTC),
        revoked_at=None,
    )
    fake_db.get_map[(User, user_id)] = SimpleNamespace(
        id=user_id,
        email="owner@example.com",
        is_sysadmin=False,
        is_active=True,
        is_approved=True,
    )
    fake_db.get_map[(Project, project_id)] = SimpleNamespace(id=project_id, name="Core")
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": user_id}))] = SimpleNamespace(
        project_id=project_id,
        user_id=user_id,
        role=ProjectRole.ADMIN,
    )

    monkeypatch.setattr("app.routers.settings.get_settings", lambda: SimpleNamespace(default_api_token_expiry_days=90, allow_never_expiring_api_tokens=False))

    client = _client_for_db(fake_db, actor_user_id=actor_id)
    try:
        response = client.patch(
            f"/settings/api-tokens/{token_id}",
            json={"never_expires": True},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert response.json()["detail"] == "never-expiring api tokens are disabled"


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
    fake_db.execute_queue.append(
        _ExecuteResult(
            [
                SimpleNamespace(id=project_a, name="Core"),
                SimpleNamespace(id=project_b, name="Infra"),
            ]
        )
    )

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
    assert payload["partial"] is False


def test_users_assign_user_all_projects_preserves_last_project_admin() -> None:
    fake_db = _FakeDb()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    fake_db.get_map[(User, user_id)] = SimpleNamespace(id=user_id, email="admin@example.com")
    existing = SimpleNamespace(project_id=project_id, user_id=user_id, role=ProjectRole.ADMIN)
    fake_db.get_map[(ProjectMember, _normalize_key({"project_id": project_id, "user_id": user_id}))] = existing
    fake_db.execute_queue.append(_ExecuteResult([SimpleNamespace(id=project_id, name="Core")]))
    fake_db.execute_queue.append(_ExecuteResult([0]))

    client = _client_for_db(fake_db)
    try:
        response = client.post(
            f"/users/{user_id}/assign-all-projects",
            json={"role": "viewer", "overwrite_existing": True},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["assigned_projects"] == 0
    assert payload["partial"] is True
    assert payload["skipped_projects"] == [
        {
            "project_id": str(project_id),
            "project_name": "Core",
            "reason": "last project admin would be removed",
        }
    ]
    assert existing.role == ProjectRole.ADMIN


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
