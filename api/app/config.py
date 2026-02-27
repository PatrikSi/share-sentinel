from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "share-sentinel-api"
    log_level: str = "INFO"
    api_root_path: str = ""

    database_url: str = "postgresql+psycopg://smbguard:smbguard@db:5432/smbguard"
    redis_url: str = "redis://redis:6379/0"

    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "smbguard-artifacts"

    jwt_secret: str = "dev-secret"
    jwt_issuer: str = "smbguard"
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    token_pepper: str = "dev-pepper"
    require_user_for_api_token_create: bool = True

    cors_origins: str = "http://localhost"

    upload_max_bytes: int = 10 * 1024 * 1024 * 1024
    upload_chunk_bytes: int = 8 * 1024 * 1024
    rate_limit_fail_open: bool = False
    redis_stream_retries: int = 3
    seed_admin_email: str | None = None
    seed_admin_password: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
