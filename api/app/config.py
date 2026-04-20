from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.password_policy import password_policy_kwargs, validate_password_strength


PLACEHOLDER_PREFIXES = ("change-me", "changeme", "replace-", "replace_", "example-", "your-")


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
    redis_url: str = "redis://redis:6379/0"

    artifact_storage_path: str = "/artifacts"

    jwt_secret: str = "dev-secret-not-for-production-0123456789"
    jwt_issuer: str = "share-sentinel"
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    token_pepper: str = "dev-token-pepper-not-for-production-012345"
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
    seed_admin_email: str | None = None
    seed_admin_password: str | None = None

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: str) -> str:
        normalized = str(value).strip().lower() or "development"
        allowed = {"development", "dev", "testing", "test", "staging", "stage", "production", "prod"}
        if normalized not in allowed:
            raise ValueError("app_env must be one of: development, dev, testing, test, staging, stage, production, prod")
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

    @field_validator("auth_csrf_header_name", mode="before")
    @classmethod
    def _normalize_csrf_header_name(cls, value: str) -> str:
        return str(value).strip().lower()

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
    def _validate_non_testing_secret_placeholders(self):
        if self.app_env.lower() in {"testing", "test"}:
            return self
        if looks_like_placeholder(self.jwt_secret):
            raise ValueError("jwt_secret must be replaced before startup")
        if looks_like_placeholder(self.token_pepper):
            raise ValueError("token_pepper must be replaced before startup")
        return self

    @model_validator(mode="after")
    def _validate_production_settings(self):
        if self.default_api_token_expiry_days == 0 and not self.allow_never_expiring_api_tokens:
            raise ValueError(
                "default_api_token_expiry_days must be at least 1 when allow_never_expiring_api_tokens is false"
            )
        if self.app_env.lower() in {"production", "prod", "staging", "stage"}:
            if not self.auth_require_csrf:
                raise ValueError("auth_require_csrf must be true in production")
            if self.allow_legacy_unscoped_tokens:
                raise ValueError("allow_legacy_unscoped_tokens must be false in production")
            if not self.seed_admin_email or not self.seed_admin_password:
                raise ValueError("SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD must both be set in production")
            if not self.auth_cookie_secure:
                raise ValueError("auth_cookie_secure must be true in production")
            if self.allow_never_expiring_api_tokens:
                raise ValueError("allow_never_expiring_api_tokens must be false in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=".env")
