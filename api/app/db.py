from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings


def escape_like(value: str) -> str:
    """Escape special SQL LIKE/ILIKE characters (%, _, \\) in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


settings = get_settings()
DATABASE_CONNECT_TIMEOUT_SECONDS = settings.api_database_connect_timeout_seconds
DATABASE_STATEMENT_TIMEOUT_MS = settings.api_database_statement_timeout_ms
DATABASE_LOCK_TIMEOUT_MS = settings.api_database_lock_timeout_ms
ENGINE_CONNECT_ARGS = {
    "connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS,
    "options": (
        f"-c statement_timeout={DATABASE_STATEMENT_TIMEOUT_MS} "
        f"-c lock_timeout={DATABASE_LOCK_TIMEOUT_MS}"
    ),
}

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.api_database_pool_size,
    max_overflow=settings.api_database_max_overflow,
    pool_timeout=settings.api_database_pool_timeout_seconds,
    pool_recycle=settings.api_database_pool_recycle_seconds,
    pool_use_lifo=True,
    connect_args=ENGINE_CONNECT_ARGS,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
