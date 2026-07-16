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


def test_non_testing_env_rejects_placeholder_secrets() -> None:
    with pytest.raises(ValueError, match="jwt_secret must be replaced before startup"):
        Settings(jwt_secret="replace-before-running-secret-value-0123456789", token_pepper="y" * 64)

    with pytest.raises(ValueError, match="token_pepper must be replaced before startup"):
        Settings(jwt_secret="x" * 64, token_pepper="change-me-token-pepper-value-0123456789")

    with pytest.raises(ValueError, match="SEED_ADMIN_PASSWORD must be replaced before startup"):
        Settings(
            seed_admin_email="admin@example.com",
            seed_admin_password="change-me-password",
            password_min_length=3,
            password_require_lowercase=False,
            password_require_uppercase=False,
            password_require_number=False,
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


def test_production_requires_csrf_and_disallows_legacy_unscoped_tokens() -> None:
    with pytest.raises(ValueError, match="auth_require_csrf must be true in production"):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=True,
            auth_require_csrf=False,
        )

    with pytest.raises(ValueError, match="allow_legacy_unscoped_tokens must be false in production"):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=True,
            allow_legacy_unscoped_tokens=True,
        )

    with pytest.raises(ValueError, match="allow_never_expiring_api_tokens must be false in production"):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=True,
            allow_never_expiring_api_tokens=True,
        )


def test_staging_requires_production_security_posture() -> None:
    with pytest.raises(ValueError, match="auth_cookie_secure must be true in production"):
        Settings(
            app_env="staging",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=False,
        )


def test_production_requires_trusted_proxy_cidrs() -> None:
    with pytest.raises(ValueError, match="trusted_proxy_cidrs must be set in production"):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=True,
            trusted_proxy_cidrs="",
        )


def test_production_requires_explicit_trusted_hosts() -> None:
    with pytest.raises(ValueError, match="trusted_hosts must name the deployed hostnames in production"):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=True,
            trusted_proxy_cidrs="10.0.0.0/8",
            trusted_hosts="*",
        )


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValueError, match="cors_origins must not contain a wildcard in production"):
        Settings(
            app_env="production",
            jwt_secret="x" * 64,
            token_pepper="y" * 64,
            seed_admin_email="admin@example.com",
            seed_admin_password="StrongPassword123",
            auth_cookie_secure=True,
            trusted_proxy_cidrs="10.0.0.0/8",
            trusted_hosts="sentinel.example.com",
            cors_origins="*",
        )


def test_trusted_proxy_cidrs_reject_invalid_networks() -> None:
    with pytest.raises(ValueError, match="invalid trusted proxy CIDR"):
        Settings(trusted_proxy_cidrs="not-a-network")


def test_app_env_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="app_env must be one of"):
        Settings(app_env="prodution")


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


def test_default_api_token_expiry_requires_positive_days_when_never_expiring_tokens_are_disabled() -> None:
    with pytest.raises(
        ValueError,
        match="default_api_token_expiry_days must be at least 1 when allow_never_expiring_api_tokens is false",
    ):
        Settings(default_api_token_expiry_days=0, allow_never_expiring_api_tokens=False)


def test_default_api_token_expiry_can_be_zero_when_never_expiring_tokens_are_explicitly_enabled() -> None:
    settings = Settings(default_api_token_expiry_days=0, allow_never_expiring_api_tokens=True)

    assert settings.default_api_token_expiry_days == 0
    assert settings.allow_never_expiring_api_tokens is True
