import pytest
from app.security import (
    clear_auth_cookies,
    generate_csrf_token,
    hash_external_token,
    hash_password,
    set_auth_cookies,
    set_refresh_cookie,
    validate_password_strength,
    verify_password,
)
from fastapi import Response


def test_password_hash_roundtrip() -> None:
    password = "this-is-a-long-test-password"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed)


def test_external_token_hash_is_deterministic() -> None:
    token = "sample-token"
    assert hash_external_token(token) == hash_external_token(token)


def test_verify_password_handles_invalid_hash() -> None:
    assert verify_password("pass", "not-a-passlib-hash") is False


def test_password_strength_policy() -> None:
    validate_password_strength("VeryStrongPassword123", 12)
    with pytest.raises(ValueError):
        validate_password_strength("short1A", 12)


def test_password_strength_policy_supports_special_character_requirement() -> None:
    with pytest.raises(ValueError, match="special character"):
        validate_password_strength("VeryStrongPassword123", 12, require_special=True)

    validate_password_strength("VeryStrongPassword123!", 12, require_special=True)


def test_password_strength_policy_can_disable_character_classes() -> None:
    validate_password_strength("alllowercasepassword", 12, require_uppercase=False, require_number=False)


def test_auth_cookie_helpers() -> None:
    response = Response()
    csrf_token = generate_csrf_token()
    assert csrf_token

    set_auth_cookies(response, "access-token", csrf_token)
    set_refresh_cookie(response, "refresh-token")
    clear_auth_cookies(response)
    raw_headers = [value.decode("latin-1") for key, value in response.raw_headers if key.lower() == b"set-cookie"]
    assert any("share_sentinel_session=" in header for header in raw_headers)
    assert any("share_sentinel_csrf=" in header for header in raw_headers)
    assert any("share_sentinel_refresh=" in header for header in raw_headers)
    assert any("Max-Age=0" in header for header in raw_headers)
