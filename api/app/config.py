from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.password_policy import password_policy_kwargs, validate_password_strength


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_name: str = "share-sentinel-api"
    log_level: str = "INFO"
    api_root_path: str = ""

    database_url: str = "postgresql+psycopg://share_sentinel:share_sentinel@db:5432/share_sentinel"
    redis_url: str = "redis://redis:6379/0"

    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "share-sentinel-artifacts"

    jwt_secret: str = "dev-secret"
    jwt_issuer: str = "share-sentinel"
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    token_pepper: str = "dev-pepper"
    require_user_for_api_token_create: bool = True
    allow_legacy_unscoped_tokens: bool = False
    default_api_token_expiry_days: int = 90
    api_token_last_used_update_interval_seconds: int = 300
    auth_cookie_name: str = "share_sentinel_session"
    auth_cookie_domain: str | None = None
    auth_cookie_path: str = "/"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"
    auth_csrf_cookie_name: str = "share_sentinel_csrf"
    auth_csrf_header_name: str = "x-csrf-token"
    auth_require_csrf: bool = True

    cors_origins: str = "http://localhost"
    trusted_proxy_cidrs: str = ""
    allow_self_registration: bool = False

    upload_max_bytes: int = 10 * 1024 * 1024 * 1024
    upload_chunk_bytes: int = 8 * 1024 * 1024
    rate_limit_fail_open: bool = False
    redis_stream_retries: int = 3
    redis_stream_maxlen: int = 0
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
        if value < 8:
            raise ValueError("password_min_length must be at least 8")
        if value > 256:
            raise ValueError("password_min_length must be 256 or less")
        return value

    @model_validator(mode="after")
    def _validate_seed_admin_settings(self):
        has_seed_email = bool(self.seed_admin_email)
        has_seed_password = bool(self.seed_admin_password)
        if has_seed_email != has_seed_password:
            raise ValueError("SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD must either both be set or both be unset")
        if self.seed_admin_password:
            try:
                validate_password_strength(self.seed_admin_password, **password_policy_kwargs(self))
            except ValueError as exc:
                raise ValueError(f"SEED_ADMIN_PASSWORD must satisfy the configured password policy: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _validate_production_settings(self):
        if self.app_env.lower() in {"production", "prod"}:
            if self.jwt_secret == "dev-secret" or len(self.jwt_secret) < 32:
                raise ValueError("jwt_secret must be set and at least 32 characters in production")
            if self.token_pepper == "dev-pepper" or len(self.token_pepper) < 32:
                raise ValueError("token_pepper must be set and at least 32 characters in production")
            if not self.seed_admin_email or not self.seed_admin_password:
                raise ValueError("SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD must both be set in production")
            if self.seed_admin_password in {"ChangeMe123456", "change-me-please-12-plus"}:
                raise ValueError("SEED_ADMIN_PASSWORD must not use the default value in production")
            if not self.auth_cookie_secure:
                raise ValueError("auth_cookie_secure must be true in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
