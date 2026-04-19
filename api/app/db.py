from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings


def escape_like(value: str) -> str:
    """Escape special SQL LIKE/ILIKE characters (%, _, \\) in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
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
