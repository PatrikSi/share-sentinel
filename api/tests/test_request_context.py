from app.main import app
from app.middleware import _normalize_request_id
from fastapi.testclient import TestClient


def test_normalize_request_id_generates_value_when_missing() -> None:
    request_id = _normalize_request_id(None)
    assert request_id
    assert len(request_id) <= 128


def test_normalize_request_id_sanitizes_input() -> None:
    request_id = _normalize_request_id("abc123-._$%^")
    assert request_id == "abc123-._"


def test_normalize_request_id_truncates_long_values() -> None:
    request_id = _normalize_request_id("a" * 500)
    assert len(request_id) == 128


def test_api_responses_include_security_headers() -> None:
    response = TestClient(app).get("/healthz")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["permissions-policy"] == "camera=(), geolocation=(), microphone=()"
    assert response.headers["x-request-id"]


def test_untrusted_host_is_rejected() -> None:
    response = TestClient(app).get("/healthz", headers={"host": "attacker.example"})

    assert response.status_code == 400
