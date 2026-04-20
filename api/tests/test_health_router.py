import redis
from fastapi.testclient import TestClient

from app.db import get_db
from app.deps import require_sysadmin
from app.main import app
from app.routers import health as health_router


class _DbOK:
    def execute(self, _query):
        return 1


class _DbFail:
    def execute(self, _query):
        raise RuntimeError("db down")


class _RedisOK:
    def ping(self):
        return True


class _RedisFail:
    def ping(self):
        raise redis.RedisError("redis down")


def test_healthz_ready_ok(monkeypatch) -> None:
    monkeypatch.setattr(health_router, "redis_client", _RedisOK())
    monkeypatch.setattr(health_router.storage, "artifact_storage_ready", lambda: True)

    def _override_db():
        yield _DbOK()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as client:
        response = client.get("/healthz/ready")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["checks"]["database"] == "ok"
    assert payload["checks"]["redis"] == "ok"


def test_healthz_ready_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr(health_router, "redis_client", _RedisFail())
    monkeypatch.setattr(health_router.storage, "artifact_storage_ready", lambda: False)

    def _override_db():
        yield _DbFail()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as client:
        response = client.get("/healthz/ready")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["checks"]["database"] == "error"
    assert payload["checks"]["redis"] == "error"
    assert payload["checks"]["artifact_storage"] == "error"


def test_healthz_deep_ok(monkeypatch) -> None:
    monkeypatch.setattr(health_router, "redis_client", _RedisOK())
    monkeypatch.setattr(health_router.storage, "artifact_storage_ready", lambda: True)

    def _override_db():
        yield _DbOK()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_sysadmin] = lambda: object()
    with TestClient(app) as client:
        response = client.get("/healthz/deep")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["checks"]["database"] == "ok"
    assert payload["checks"]["redis"] == "ok"
    assert payload["checks"]["artifact_storage"] == "ok"


def test_healthz_deep_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr(health_router, "redis_client", _RedisFail())
    monkeypatch.setattr(health_router.storage, "artifact_storage_ready", lambda: False)

    def _override_db():
        yield _DbFail()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_sysadmin] = lambda: object()
    with TestClient(app) as client:
        response = client.get("/healthz/deep")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["checks"]["database"] == "error"
    assert payload["checks"]["redis"] == "error"
    assert payload["checks"]["artifact_storage"] == "error"
