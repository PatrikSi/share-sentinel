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
            seed_admin_password="StrongPassword123",
        )


def test_production_rejects_default_seed_password() -> None:
    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_password="change-me-please-12-plus",
        )


def test_production_requires_secure_auth_cookie() -> None:
    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=False,
        )
