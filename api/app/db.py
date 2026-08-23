from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings


def escape_like(value: str) -> str:
    """Escape special SQL LIKE/ILIKE characters (%, _, \\) in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


settings = get_settings()
DATABASE_CONNECT_TIMEOUT_SECONDS = 5
ENGINE_CONNECT_ARGS = {"connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS}

engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=ENGINE_CONNECT_ARGS)
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
