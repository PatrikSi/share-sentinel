from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db

router = APIRouter(tags=["health"])
settings = get_settings()
redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/healthz/deep")
def healthz_deep(db: Session = Depends(get_db)):
    checks: dict[str, str] = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error:{type(exc).__name__}"

    try:
        redis_client.ping()
        checks["redis"] = "ok"
    except redis.RedisError as exc:
        checks["redis"] = f"error:{type(exc).__name__}"

    ok = all(value == "ok" for value in checks.values())
    status_code = 200 if ok else 503
    return JSONResponse(status_code=status_code, content={"ok": ok, "checks": checks})
