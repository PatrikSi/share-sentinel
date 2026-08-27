from functools import lru_cache
from ipaddress import ip_network
from math import isfinite

from pydantic import EmailStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.password_policy import password_policy_kwargs, validate_password_strength

PLACEHOLDER_PREFIXES = ("change-me", "changeme", "replace-", "replace_", "example-", "your-")
DEFAULT_JWT_SECRET = "dev-secret-not-for-production-0123456789"
DEFAULT_TOKEN_PEPPER = "dev-token-pepper-not-for-production-012345"
MAX_UPLOAD_CHUNK_BYTES = 128 * 1024 * 1024


def looks_like_placeholder(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = "development"
    app_name: str = "share-sentinel-api"
    log_level: str = "INFO"
    api_root_path: str = ""

    database_url: str = "postgresql+psycopg://share_sentinel:share_sentinel@db:5432/share_sentinel"
    api_database_pool_size: int = 10
    api_database_max_overflow: int = 20
    api_database_pool_timeout_seconds: int = 10
    api_database_pool_recycle_seconds: int = 1800
    api_database_connect_timeout_seconds: int = 5
    api_database_statement_timeout_ms: int = 30_000
    api_database_lock_timeout_ms: int = 5_000
    api_run_diff_max_items: int = 250_000
    api_comparison_max_active_per_project: int = 3
    api_comparison_rate_limit: int = 12
    api_comparison_rate_window_seconds: int = 60
    api_inventory_export_max_concurrent: int = 4
    api_inventory_export_rate_limit: int = 12
    api_inventory_export_rate_window_seconds: int = 60
    migration_database_connect_timeout_seconds: int = 10
    migration_database_lock_timeout_ms: int = 60_000
    redis_url: str = "redis://redis:6379/0"
    redis_connect_timeout_seconds: float = 3.0
    redis_socket_timeout_seconds: float = 5.0

    artifact_storage_path: str = "/artifacts"

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_issuer: str = "share-sentinel"
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    token_pepper: str = DEFAULT_TOKEN_PEPPER
    require_user_for_api_token_create: bool = True
    allow_legacy_unscoped_tokens: bool = False
    default_api_token_expiry_days: int = 90
    allow_never_expiring_api_tokens: bool = False
    api_token_last_used_update_interval_seconds: int = 300
    auth_cookie_name: str = "share_sentinel_session"
    auth_cookie_domain: str | None = None
    auth_cookie_path: str = "/"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"
    auth_csrf_cookie_name: str = "share_sentinel_csrf"
    auth_csrf_header_name: str = "x-csrf-token"
    auth_refresh_cookie_name: str = "share_sentinel_refresh"
    auth_require_csrf: bool = True

    cors_origins: str = "http://localhost"
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    trusted_proxy_cidrs: str = ""
    allow_self_registration: bool = False

    upload_max_bytes: int = 10 * 1024 * 1024 * 1024
    upload_chunk_bytes: int = 8 * 1024 * 1024
    rate_limit_fail_open: bool = False
    redis_stream_retries: int = 3
    redis_stream_maxlen: int = 250000
    auth_login_max_attempts: int = 8
    auth_login_window_seconds: int = 300
    auth_login_lockout_seconds: int = 900
    password_min_length: int = 12
    password_require_lowercase: bool = True
    password_require_uppercase: bool = True
    password_require_number: bool = True
    password_require_special: bool = False
    seed_admin_email: EmailStr | None = None
    seed_admin_password: str | None = None

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: str) -> str:
        normalized = str(value).strip().lower() or "development"
        allowed = {"development", "dev", "testing", "test", "staging", "stage", "production", "prod"}
        if normalized not in allowed:
            raise ValueError(
                "app_env must be one of: development, dev, testing, test, staging, stage, production, prod"
            )
        return normalized

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return str(value).strip().upper() or "INFO"

    @field_validator("auth_cookie_samesite", mode="before")
    @classmethod
    def _normalize_samesite(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError("auth_cookie_samesite must be one of: lax, strict, none")
        return normalized

    @field_validator("auth_cookie_domain", mode="before")
    @classmethod
    def _normalize_cookie_domain(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("auth_csrf_header_name", mode="before")
    @classmethod
    def _normalize_csrf_header_name(cls, value: str) -> str:
        return str(value).strip().lower()

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _validate_trusted_proxy_cidrs(cls, value: str) -> str:
        normalized = str(value or "").strip()
        for raw_cidr in (item.strip() for item in normalized.split(",")):
            if not raw_cidr:
                continue
            try:
                ip_network(raw_cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy CIDR: {raw_cidr}") from exc
        return normalized

    @field_validator("password_min_length")
    @classmethod
    def _validate_password_min_length(cls, value: int) -> int:
        if value < 1:
            raise ValueError("password_min_length must be at least 1")
        if value > 256:
            raise ValueError("password_min_length must be 256 or less")
        return value

    @field_validator("jwt_secret", "token_pepper")
    @classmethod
    def _validate_secret_length(cls, value: str, info) -> str:
        normalized = str(value).strip()
        if len(normalized) < 32:
            raise ValueError(f"{info.field_name} must be at least 32 characters")
        return normalized

    @field_validator("default_api_token_expiry_days")
    @classmethod
    def _validate_default_api_token_expiry_days(cls, value: int) -> int:
        if value < 0:
            raise ValueError("default_api_token_expiry_days must be 0 or greater")
        if value > 3650:
            raise ValueError("default_api_token_expiry_days must be 3650 or less")
        return value

    @field_validator(
        "access_token_minutes",
        "refresh_token_days",
        "upload_max_bytes",
        "upload_chunk_bytes",
        "redis_stream_retries",
        "redis_stream_maxlen",
        "auth_login_max_attempts",
        "auth_login_window_seconds",
        "auth_login_lockout_seconds",
    )
    @classmethod
    def _validate_positive_runtime_setting(cls, value: int, info) -> int:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        return value

    @field_validator(
        "api_database_pool_size",
        "api_database_pool_timeout_seconds",
        "api_database_pool_recycle_seconds",
        "api_database_connect_timeout_seconds",
        "api_database_statement_timeout_ms",
        "api_database_lock_timeout_ms",
        "api_run_diff_max_items",
        "api_comparison_max_active_per_project",
        "api_comparison_rate_limit",
        "api_comparison_rate_window_seconds",
        "api_inventory_export_max_concurrent",
        "api_inventory_export_rate_limit",
        "api_inventory_export_rate_window_seconds",
        "migration_database_connect_timeout_seconds",
        "migration_database_lock_timeout_ms",
    )
    @classmethod
    def _validate_positive_database_setting(cls, value: int, info) -> int:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        return value

    @field_validator("api_database_pool_size")
    @classmethod
    def _validate_database_pool_size(cls, value: int) -> int:
        if value > 200:
            raise ValueError("api_database_pool_size must be 200 or less")
        return value

    @field_validator("api_database_max_overflow")
    @classmethod
    def _validate_database_max_overflow(cls, value: int) -> int:
        if value < 0:
            raise ValueError("api_database_max_overflow must be zero or greater")
        if value > 500:
            raise ValueError("api_database_max_overflow must be 500 or less")
        return value

    @field_validator(
        "api_database_pool_timeout_seconds",
        "api_database_connect_timeout_seconds",
        "migration_database_connect_timeout_seconds",
    )
    @classmethod
    def _validate_database_wait_timeout(cls, value: int, info) -> int:
        if value > 300:
            raise ValueError(f"{info.field_name} must be 300 seconds or less")
        return value

    @field_validator("api_database_pool_recycle_seconds")
    @classmethod
    def _validate_database_pool_recycle(cls, value: int) -> int:
        if value > 86_400:
            raise ValueError("api_database_pool_recycle_seconds must be 86400 seconds or less")
        return value

    @field_validator(
        "api_database_statement_timeout_ms",
        "api_database_lock_timeout_ms",
        "migration_database_lock_timeout_ms",
    )
    @classmethod
    def _validate_database_query_timeout(cls, value: int, info) -> int:
        if value > 3_600_000:
            raise ValueError(f"{info.field_name} must be 3600000 milliseconds or less")
        return value

    @field_validator("api_run_diff_max_items")
    @classmethod
    def _validate_api_run_diff_max_items(cls, value: int) -> int:
        if value > 5_000_000:
            raise ValueError("api_run_diff_max_items must be 5000000 or less")
        return value

    @field_validator("api_comparison_max_active_per_project")
    @classmethod
    def _validate_comparison_concurrency(cls, value: int) -> int:
        if value > 100:
            raise ValueError("api_comparison_max_active_per_project must be 100 or less")
        return value

    @field_validator("api_comparison_rate_limit", "api_comparison_rate_window_seconds")
    @classmethod
    def _validate_comparison_rate_settings(cls, value: int, info) -> int:
        if value > 86_400:
            raise ValueError(f"{info.field_name} must be 86400 or less")
        return value

    @field_validator("api_inventory_export_max_concurrent")
    @classmethod
    def _validate_inventory_export_concurrency(cls, value: int) -> int:
        if value > 100:
            raise ValueError("api_inventory_export_max_concurrent must be 100 or less")
        return value

    @field_validator("api_inventory_export_rate_limit", "api_inventory_export_rate_window_seconds")
    @classmethod
    def _validate_inventory_export_rate_settings(cls, value: int, info) -> int:
        if value > 86_400:
            raise ValueError(f"{info.field_name} must be 86400 or less")
        return value

    @field_validator("api_token_last_used_update_interval_seconds")
    @classmethod
    def _validate_non_negative_runtime_setting(cls, value: int, info) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be zero or greater")
        return value

    @field_validator("upload_chunk_bytes")
    @classmethod
    def _validate_upload_chunk_upper_bound(cls, value: int) -> int:
        if value > MAX_UPLOAD_CHUNK_BYTES:
            raise ValueError(f"upload_chunk_bytes must be {MAX_UPLOAD_CHUNK_BYTES} bytes or less")
        return value

    @field_validator("redis_connect_timeout_seconds", "redis_socket_timeout_seconds")
    @classmethod
    def _validate_redis_timeout(cls, value: float, info) -> float:
        if not isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        if value > 60:
            raise ValueError(f"{info.field_name} must be 60 seconds or less")
        return value

    @model_validator(mode="after")
    def _validate_seed_admin_settings(self):
        has_seed_email = bool(self.seed_admin_email)
        has_seed_password = bool(self.seed_admin_password)
        if has_seed_email != has_seed_password:
            raise ValueError("SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD must either both be set or both be unset")
        if self.seed_admin_password and looks_like_placeholder(self.seed_admin_password):
            raise ValueError("SEED_ADMIN_PASSWORD must be replaced before startup")
        if self.seed_admin_password:
            try:
                validate_password_strength(self.seed_admin_password, **password_policy_kwargs(self))
            except ValueError as exc:
                raise ValueError(f"SEED_ADMIN_PASSWORD must satisfy the configured password policy: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _validate_upload_settings(self):
        if self.upload_chunk_bytes > self.upload_max_bytes:
            raise ValueError("upload_chunk_bytes must be less than or equal to upload_max_bytes")
        return self

    @model_validator(mode="after")
    def _validate_non_testing_secret_placeholders(self):
        if self.app_env.lower() in {"testing", "test"}:
            return self
        if looks_like_placeholder(self.jwt_secret):
            raise ValueError("jwt_secret must be replaced before startup")
        if looks_like_placeholder(self.token_pepper):
            raise ValueError("token_pepper must be replaced before startup")
        if self.app_env.lower() in {"production", "prod", "staging", "stage"}:
            if self.jwt_secret == DEFAULT_JWT_SECRET:
                raise ValueError("jwt_secret must be replaced in production")
            if self.token_pepper == DEFAULT_TOKEN_PEPPER:
                raise ValueError("token_pepper must be replaced in production")
        return self

    @model_validator(mode="after")
    def _validate_production_settings(self):
        if self.default_api_token_expiry_days == 0 and not self.allow_never_expiring_api_tokens:
            raise ValueError(
                "default_api_token_expiry_days must be at least 1 when allow_never_expiring_api_tokens is false"
            )
        if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
            raise ValueError("auth_cookie_secure must be true when auth_cookie_samesite is none")
        if self.app_env.lower() in {"production", "prod", "staging", "stage"}:
            if not self.auth_require_csrf:
                raise ValueError("auth_require_csrf must be true in production")
            if self.allow_legacy_unscoped_tokens:
                raise ValueError("allow_legacy_unscoped_tokens must be false in production")
            if not self.auth_cookie_secure:
                raise ValueError("auth_cookie_secure must be true in production")
            if self.allow_never_expiring_api_tokens:
                raise ValueError("allow_never_expiring_api_tokens must be false in production")
            if not str(self.trusted_proxy_cidrs or "").strip():
                raise ValueError("trusted_proxy_cidrs must be set in production")
            trusted_hosts = [host.strip().lower() for host in self.trusted_hosts.split(",") if host.strip()]
            if not trusted_hosts or "*" in trusted_hosts or "testserver" in trusted_hosts:
                raise ValueError("trusted_hosts must name the deployed hostnames in production")
            cors_origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
            if "*" in cors_origins:
                raise ValueError("cors_origins must not contain a wildcard in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=".env")
