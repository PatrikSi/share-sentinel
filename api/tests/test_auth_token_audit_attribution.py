import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.deps import AuthContext
from app.enums import ProjectRole
from app.models import ApiToken, User
from app.routers import auth as auth_router
from app.schemas import ApiTokenCreateIn
from starlette.requests import Request


class _FakeDb:
    def __init__(self, owner: Any | None = None, token: Any | None = None):
        self.owner = owner
        self.token = token
        self.added: list[Any] = []
        self.commit_count = 0

    def get(self, model, _key):
        if model is User:
            return self.owner
        if model is ApiToken:
            return self.token
        raise AssertionError(f"unexpected model lookup: {model}")

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if isinstance(obj, ApiToken):
                obj.id = obj.id or uuid.uuid4()
                obj.created_at = obj.created_at or datetime.now(tz=UTC)
                obj.last_used_at = obj.last_used_at or None
                obj.revoked_at = obj.revoked_at or None

    def commit(self):
        self.commit_count += 1

    def refresh(self, _obj):
        return None


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 5000),
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def _token_auth(user_id: uuid.UUID, token_id: uuid.UUID, project_id: uuid.UUID) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        token_id=token_id,
        token_project_id=project_id,
        token_role=ProjectRole.ADMIN,
        token_scopes=["write:tokens"],
    )


def test_create_api_token_attributes_api_token_actor(monkeypatch) -> None:
    user_id = uuid.uuid4()
    actor_token_id = uuid.uuid4()
    project_id = uuid.uuid4()
    owner = SimpleNamespace(id=user_id, is_sysadmin=True)
    fake_db = _FakeDb(owner=owner)
    audit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        auth_router,
        "get_settings",
        lambda: SimpleNamespace(
            require_user_for_api_token_create=False,
            default_api_token_expiry_days=30,
            allow_never_expiring_api_tokens=False,
        ),
    )
    monkeypatch.setattr(auth_router, "get_project_role", lambda *_args: ProjectRole.ADMIN)
    monkeypatch.setattr(auth_router.rate_limiter, "check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_router, "random_token", lambda _length: "new-token")
    monkeypatch.setattr(auth_router, "hash_external_token", lambda _token: "new-token-hash")
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **kwargs: audit_calls.append(kwargs))

    result = auth_router.create_api_token(
        ApiTokenCreateIn(
            project_id=project_id,
            name="rotation token",
            role=ProjectRole.ADMIN,
            scopes=["write:tokens"],
        ),
        _request("POST", "/auth/api-tokens"),
        fake_db,
        _token_auth(user_id, actor_token_id, project_id),
    )

    assert result.token == "new-token"
    assert fake_db.commit_count == 1
    assert audit_calls == [
        {
            "action": "API_TOKEN_CREATED",
            "object_type": "api_token",
            "object_id": str(result.token_meta.id),
            "actor_user_id": user_id,
            "actor_token_id": actor_token_id,
            "project_id": project_id,
            "metadata": {
                "request_id": None,
                "ip": "127.0.0.1",
                "user_agent": None,
                "scopes": ["write:tokens"],
                "expires_at": result.token_meta.expires_at.isoformat(),
            },
        }
    ]


def test_revoke_api_token_attributes_api_token_actor(monkeypatch) -> None:
    user_id = uuid.uuid4()
    actor_token_id = uuid.uuid4()
    project_id = uuid.uuid4()
    revoked_token_id = uuid.uuid4()
    token = SimpleNamespace(
        id=revoked_token_id,
        user_id=user_id,
        project_id=project_id,
        revoked_at=None,
    )
    fake_db = _FakeDb(token=token)
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(auth_router, "write_audit_event", lambda *_args, **kwargs: audit_calls.append(kwargs))

    result = auth_router.revoke_api_token(
        revoked_token_id,
        _request("DELETE", f"/auth/api-tokens/{revoked_token_id}"),
        fake_db,
        _token_auth(user_id, actor_token_id, project_id),
    )

    assert result == {"ok": True}
    assert token.revoked_at is not None
    assert fake_db.commit_count == 1
    assert audit_calls == [
        {
            "action": "API_TOKEN_REVOKED",
            "object_type": "api_token",
            "object_id": str(revoked_token_id),
            "actor_user_id": user_id,
            "actor_token_id": actor_token_id,
            "project_id": project_id,
            "metadata": {
                "request_id": None,
                "ip": "127.0.0.1",
                "user_agent": None,
            },
        }
    ]
