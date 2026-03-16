import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app.db import get_db
from app.deps import AuthContext, get_auth_context
from app.enums import ErrorSeverity, RunStatus
from app.main import app
from app.routers import runs as runs_router


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

    def execute(self, _statement):
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
        ingest_progress={"line_offset": 2048},
        summary={"endpoints": 2, "resources": 5, "items": 100, "errors": 1},
    )

    payload = runs_router._to_run_out(run)

    assert payload.ingest_progress == {"line_offset": 2048}


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
        metadata_json={"line_offset": 26, "counts": {"errors": 1}},
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
