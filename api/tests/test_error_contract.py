from app.main import app
from fastapi import Query
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError


def _ensure_error_contract_routes() -> None:
    known_paths = {getattr(route, "path", None) for route in app.router.routes}
    if "/_test/error-contract-validation" not in known_paths:

        @app.get("/_test/error-contract-validation", include_in_schema=False)
        def _validation_route(value: int = Query(ge=1)) -> dict:
            return {"value": value}

    if "/_test/error-contract-db-timeout" not in known_paths:

        @app.get("/_test/error-contract-db-timeout", include_in_schema=False)
        def _database_timeout_route() -> None:
            raise SQLAlchemyTimeoutError("pool is exhausted")

    if "/_test/error-contract-unhandled" not in known_paths:

        @app.get("/_test/error-contract-unhandled", include_in_schema=False)
        def _unhandled_route() -> None:
            raise RuntimeError("sensitive internal context")


def test_http_errors_keep_detail_and_add_traceable_error_contract() -> None:
    response = TestClient(app).get("/does-not-exist", headers={"x-request-id": "contract-test"})

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Not Found",
        "error": {
            "code": "not_found",
            "message": "Not Found",
            "request_id": "contract-test",
        },
    }
    assert response.headers["x-request-id"] == "contract-test"


def test_validation_errors_are_structured_without_removing_fastapi_detail() -> None:
    _ensure_error_contract_routes()

    response = TestClient(app).get("/_test/error-contract-validation?value=0")

    assert response.status_code == 422
    payload = response.json()
    assert isinstance(payload["detail"], list)
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["message"] == "Request validation failed"
    assert payload["error"]["request_id"] == response.headers["x-request-id"]


def test_database_pool_saturation_is_retriable_and_does_not_leak_exception_text() -> None:
    _ensure_error_contract_routes()

    response = TestClient(app, raise_server_exceptions=False).get("/_test/error-contract-db-timeout")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    payload = response.json()
    assert payload["error"]["code"] == "database_capacity_exceeded"
    assert payload["error"]["request_id"] == response.headers["x-request-id"]
    assert "pool is exhausted" not in response.text


def test_unhandled_errors_are_traceable_without_leaking_internal_context() -> None:
    _ensure_error_contract_routes()

    response = TestClient(app, raise_server_exceptions=False).get(
        "/_test/error-contract-unhandled",
        headers={"x-request-id": "internal-error-test"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "An unexpected server error occurred",
        "error": {
            "code": "internal_error",
            "message": "An unexpected server error occurred",
            "request_id": "internal-error-test",
        },
    }
    assert response.headers["x-request-id"] == "internal-error-test"
    assert "sensitive internal context" not in response.text
