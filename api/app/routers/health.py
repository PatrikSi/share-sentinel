from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app import metrics as metrics_module

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
    except Exception:  # noqa: BLE001
        checks["database"] = "error"

    try:
        redis_client.ping()
        checks["redis"] = "ok"
    except redis.RedisError:
        checks["redis"] = "error"

    ok = all(value == "ok" for value in checks.values())
    status_code = 200 if ok else 503
    return JSONResponse(status_code=status_code, content={"ok": ok, "checks": checks})


@router.get("/metrics", include_in_schema=False)
def metrics():
    return PlainTextResponse(
        metrics_module.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
