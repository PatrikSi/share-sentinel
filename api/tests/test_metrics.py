import pytest
from app import metrics
from app.deps import require_sysadmin
from app.main import app
from fastapi.testclient import TestClient


def _ensure_error_route() -> None:
    if any(getattr(route, "path", None) == "/_test/metrics-error" for route in app.router.routes):
        return

    @app.get("/_test/metrics-error", include_in_schema=False)
    def _metrics_error() -> None:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_tests()
    yield
    metrics.reset_for_tests()


def test_metrics_endpoint_exposes_request_and_error_counters() -> None:
    app.dependency_overrides[require_sysadmin] = lambda: object()
    with TestClient(app) as client:
        ok_response = client.get("/healthz")
        missing_response = client.get("/does-not-exist")
        metrics_response = client.get("/metrics")
    app.dependency_overrides.clear()

    assert ok_response.status_code == 200
    assert missing_response.status_code == 404
    assert metrics_response.status_code == 200
    assert metrics_response.headers["content-type"].startswith("text/plain")

    payload = metrics_response.text
    assert 'share_sentinel_http_requests_total{method="GET",path="/healthz",status="200"} 1' in payload
    assert 'share_sentinel_http_requests_total{method="GET",path="__unmatched__",status="404"} 1' in payload
    assert 'share_sentinel_http_request_errors_total{method="GET",path="__unmatched__",error="http_4xx"} 1' in payload
    assert 'share_sentinel_http_request_duration_seconds_count{method="GET",path="/healthz"} 1' in payload


def test_metrics_capture_uncaught_exceptions() -> None:
    _ensure_error_route()
    app.dependency_overrides[require_sysadmin] = lambda: object()
    with TestClient(app, raise_server_exceptions=False) as client:
        error_response = client.get("/_test/metrics-error")
        metrics_response = client.get("/metrics")
    app.dependency_overrides.clear()

    assert error_response.status_code == 500
    assert metrics_response.status_code == 200

    payload = metrics_response.text
    assert 'share_sentinel_http_requests_total{method="GET",path="/_test/metrics-error",status="500"} 1' in payload
    assert 'share_sentinel_http_request_errors_total{method="GET",path="/_test/metrics-error",error="RuntimeError"} 1' in payload
    assert 'share_sentinel_http_request_duration_seconds_count{method="GET",path="/_test/metrics-error"} 1' in payload
