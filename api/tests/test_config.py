import pytest

from app.config import Settings


def test_log_level_normalized() -> None:
    settings = Settings(log_level="debug")
    assert settings.log_level == "DEBUG"


def test_production_rejects_weak_jwt_secret() -> None:
    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            jwt_secret="too-short",
            token_pepper="x" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
        )


def test_production_rejects_default_seed_password() -> None:
    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="ChangeMe123456",
        )


def test_production_requires_secure_auth_cookie() -> None:
    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=False,
        )


def test_seed_admin_password_must_match_policy() -> None:
    with pytest.raises(ValueError, match="SEED_ADMIN_PASSWORD must satisfy the configured password policy"):
        Settings(
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            password_require_special=True,
        )


def test_seed_admin_env_requires_email_and_password_pair() -> None:
    with pytest.raises(ValueError, match="SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD must either both be set or both be unset"):
        Settings(seed_admin_email="admin@example.com")


def test_password_min_length_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="password_min_length must be at least 1"):
        Settings(password_min_length=0)


def test_password_min_length_allows_short_values_when_policy_is_explicit() -> None:
    settings = Settings(
        password_min_length=3,
        password_require_lowercase=False,
        password_require_uppercase=False,
        password_require_number=False,
        seed_admin_email="admin@example.com",
        seed_admin_password="abc",
    )

    assert settings.password_min_length == 3
