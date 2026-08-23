from app.config import get_settings
from app.deps import resolve_client_ip
from starlette.requests import Request


def _make_request(remote_ip: str, x_forwarded_for: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if x_forwarded_for is not None:
        headers.append((b"x-forwarded-for", x_forwarded_for.encode("utf-8")))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (remote_ip, 1234),
        "scheme": "http",
        "query_string": b"",
    }
    return Request(scope)


def test_resolve_client_ip_returns_remote_for_untrusted_proxy(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "192.168.0.0/16")

    request = _make_request("10.1.1.20", x_forwarded_for="203.0.113.11")
    assert resolve_client_ip(request) == "10.1.1.20"


def test_resolve_client_ip_uses_forwarded_for_when_proxy_is_trusted(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "10.0.0.0/8")

    request = _make_request("10.1.1.20", x_forwarded_for="203.0.113.11, 198.51.100.2")
    assert resolve_client_ip(request) == "198.51.100.2"


def test_resolve_client_ip_skips_trusted_proxy_chain_from_right(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "10.0.0.0/8,198.51.100.0/24")

    request = _make_request("10.1.1.20", x_forwarded_for="203.0.113.11, 198.51.100.2")
    assert resolve_client_ip(request) == "203.0.113.11"


def test_resolve_client_ip_rejects_invalid_forwarded_chain(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "10.0.0.0/8")

    request = _make_request("10.1.1.20", x_forwarded_for="not-an-ip, 198.51.100.2")
    assert resolve_client_ip(request) == "10.1.1.20"
