import pytest

from app.security import hash_external_token, hash_password, validate_password_strength, verify_password


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
