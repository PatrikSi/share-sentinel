from app.middleware import _normalize_request_id


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
