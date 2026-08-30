import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.deps import AuthContext
from app.routers import audit as audit_router


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.commits = 0

    def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)

    def commit(self):
        self.commits += 1


def test_project_audit_uses_retained_actor_and_project_attribution(monkeypatch) -> None:
    project_id = uuid.uuid4()
    user_ref = uuid.uuid4()
    token_ref = uuid.uuid4()
    event = SimpleNamespace(
        id=42,
        ts=datetime.now(tz=UTC),
        project_id=None,
        project_ref=project_id,
        actor_user_id=None,
        actor_user_ref=user_ref,
        actor_email_snapshot="retired@example.com",
        actor_token_id=None,
        actor_token_ref=token_ref,
        actor_token_name_snapshot="retired collector",
        action="RUN_CREATED",
        object_type="scan_run",
        object_id="run-1",
        metadata_json={"result": "ok"},
    )
    db = _Db([event])
    auth = AuthContext(
        user_id=uuid.uuid4(),
        token_id=None,
        token_project_id=None,
        token_role=None,
        token_scopes=None,
    )
    audits = []
    monkeypatch.setattr(audit_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        audit_router,
        "write_audit_event",
        lambda *_args, **kwargs: audits.append(kwargs),
    )

    result = audit_router.list_audit_events(
        project_id=project_id,
        request=SimpleNamespace(state=SimpleNamespace(request_id=None), headers={}, client=None),
        limit=100,
        cursor=None,
        db=db,
        _=auth,
        auth=auth,
    )

    item = result["items"][0]
    assert item["actor_user_id"] == str(user_ref)
    assert item["actor_email"] == "retired@example.com"
    assert item["actor_token_id"] == str(token_ref)
    assert item["actor_token_name"] == "retired collector"
    statement = str(db.statements[0])
    assert "audit_events.project_ref" in statement
    assert "audit_events.project_id" in statement
    assert db.commits == 1
    assert audits[0]["project_id"] == project_id
