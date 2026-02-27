from app.security import hash_external_token, hash_password, verify_password


def test_password_hash_roundtrip() -> None:
    password = "this-is-a-long-test-password"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed)


def test_external_token_hash_is_deterministic() -> None:
    token = "sample-token"
    assert hash_external_token(token) == hash_external_token(token)
