import pytest
from app.config import MAX_UPLOAD_CHUNK_BYTES, Settings


def test_log_level_normalized() -> None:
    settings = Settings(log_level="debug")
    assert settings.log_level == "DEBUG"


def test_api_database_capacity_defaults_are_bounded() -> None:
    settings = Settings()

    assert settings.api_database_pool_size == 10
    assert settings.api_database_max_overflow == 20
    assert settings.api_database_pool_timeout_seconds == 10
    assert settings.api_database_connect_timeout_seconds == 5
    assert settings.api_database_statement_timeout_ms == 30_000
    assert settings.api_database_lock_timeout_ms == 5_000
    assert settings.api_run_diff_max_items == 250_000
    assert settings.migration_database_connect_timeout_seconds == 10
    assert settings.migration_database_lock_timeout_ms == 60_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_database_pool_size", 0),
        ("api_database_pool_size", 201),
        ("api_database_max_overflow", -1),
        ("api_database_max_overflow", 501),
        ("api_database_pool_timeout_seconds", 301),
        ("api_database_connect_timeout_seconds", 301),
        ("api_database_pool_recycle_seconds", 86_401),
        ("api_database_statement_timeout_ms", 3_600_001),
        ("api_database_lock_timeout_ms", 0),
        ("api_run_diff_max_items", 5_000_001),
        ("migration_database_connect_timeout_seconds", 301),
        ("migration_database_lock_timeout_ms", 3_600_001),
    ],
)
def test_api_database_capacity_settings_reject_unsafe_values(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        Settings(**{field: value})


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


def test_production_rejects_builtin_development_secrets() -> None:
    with pytest.raises(ValueError, match="jwt_secret must be replaced in production"):
        Settings(
            app_env="production",
            token_pepper="y" * 64,
            auth_cookie_secure=True,
            trusted_proxy_cidrs="10.0.0.0/8",
            trusted_hosts="sentinel.example.com",
        )


def test_production_runtime_does_not_require_seed_admin_credentials() -> None:
    settings = Settings(
        app_env="production",
        jwt_secret="x" * 64,
        token_pepper="y" * 64,
        auth_cookie_secure=True,
        trusted_proxy_cidrs="10.0.0.0/8",
        trusted_hosts="sentinel.example.com",
        cors_origins="https://sentinel.example.com",
    )

    assert settings.seed_admin_email is None
    assert settings.seed_admin_password is None


def test_blank_cookie_domain_is_normalized_and_samesite_none_requires_secure_cookie() -> None:
    assert Settings(auth_cookie_domain="").auth_cookie_domain is None

    with pytest.raises(ValueError, match="auth_cookie_secure must be true when auth_cookie_samesite is none"):
        Settings(auth_cookie_samesite="none", auth_cookie_secure=False)


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


def test_seed_admin_email_must_be_usable_by_login_schema() -> None:
    with pytest.raises(ValueError, match="valid email address"):
        Settings(seed_admin_email="admin@example.test", seed_admin_password="StrongPassword123")


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


@pytest.mark.parametrize(
    "field",
    [
        "access_token_minutes",
        "refresh_token_days",
        "upload_max_bytes",
        "upload_chunk_bytes",
        "redis_stream_retries",
        "redis_stream_maxlen",
        "auth_login_max_attempts",
        "auth_login_window_seconds",
        "auth_login_lockout_seconds",
    ],
)
@pytest.mark.parametrize("invalid", [0, -1])
def test_critical_runtime_counts_and_windows_must_be_positive(field: str, invalid: int) -> None:
    with pytest.raises(ValueError, match="must be greater than zero"):
        Settings(**{field: invalid})


def test_api_token_last_used_interval_may_be_zero_but_not_negative() -> None:
    assert Settings(api_token_last_used_update_interval_seconds=0).api_token_last_used_update_interval_seconds == 0

    with pytest.raises(ValueError, match="must be zero or greater"):
        Settings(api_token_last_used_update_interval_seconds=-1)


def test_upload_chunk_is_memory_bounded_and_cannot_exceed_upload_limit() -> None:
    with pytest.raises(ValueError, match="upload_chunk_bytes must be .* bytes or less"):
        Settings(upload_chunk_bytes=MAX_UPLOAD_CHUNK_BYTES + 1, upload_max_bytes=MAX_UPLOAD_CHUNK_BYTES + 1)

    with pytest.raises(ValueError, match="upload_chunk_bytes must be less than or equal to upload_max_bytes"):
        Settings(upload_chunk_bytes=1024, upload_max_bytes=512)


@pytest.mark.parametrize("field", ["redis_connect_timeout_seconds", "redis_socket_timeout_seconds"])
def test_redis_timeouts_must_be_positive_and_bounded(field: str) -> None:
    with pytest.raises(ValueError, match="must be greater than zero"):
        Settings(**{field: 0})

    with pytest.raises(ValueError, match="must be 60 seconds or less"):
        Settings(**{field: 61})

    for non_finite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="must be finite"):
            Settings(**{field: non_finite})
