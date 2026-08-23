from app.main import app
from fastapi.testclient import TestClient


def test_cors_preflight_allows_raw_artifact_filename_header() -> None:
    response = TestClient(app).options(
        "/projects/00000000-0000-0000-0000-000000000000/runs/00000000-0000-0000-0000-000000000000/artifact",
        headers={
            "Origin": "http://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-artifact-filename,x-csrf-token",
        },
    )

    assert response.status_code == 200
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "x-artifact-filename" in allowed_headers
