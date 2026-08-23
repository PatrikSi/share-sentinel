import pytest
from app.config import get_settings
from app.deps import _enforce_csrf_if_needed
from fastapi import HTTPException
from starlette.requests import Request


def _make_request(method: str, cookie_header: str | None = None, csrf_header: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode("utf-8")))
    if csrf_header is not None:
        header_name = get_settings().auth_csrf_header_name.encode("utf-8")
        headers.append((header_name, csrf_header.encode("utf-8")))

    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
        "query_string": b"",
    }
    return Request(scope)


def test_csrf_not_required_for_safe_methods(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_require_csrf", True)
    request = _make_request("GET")
    _enforce_csrf_if_needed(request)


def test_csrf_required_for_unsafe_methods(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_require_csrf", True)
    cookie_name = settings.auth_csrf_cookie_name
    request = _make_request("POST", cookie_header=f"{cookie_name}=token123", csrf_header="token123")
    _enforce_csrf_if_needed(request)


def test_csrf_rejects_missing_or_mismatched_values(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_require_csrf", True)
    cookie_name = settings.auth_csrf_cookie_name
    request = _make_request("POST", cookie_header=f"{cookie_name}=token123", csrf_header="different")
    with pytest.raises(HTTPException) as exc:
        _enforce_csrf_if_needed(request)
    assert exc.value.status_code == 403
