from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_sysadmin
from app import metrics as metrics_module

router = APIRouter(tags=["health"])
settings = get_settings()
redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


@router.get("/healthz")
def healthz():
    return {"ok": True}


def _dependency_checks(db: Session) -> tuple[bool, dict[str, str]]:
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
    return ok, checks


@router.get("/healthz/ready")
def healthz_ready(db: Session = Depends(get_db)):
    ok, checks = _dependency_checks(db)
    status_code = 200 if ok else 503
    return JSONResponse(status_code=status_code, content={"ok": ok, "checks": checks})


@router.get("/healthz/deep")
def healthz_deep(
    db: Session = Depends(get_db),
    _=Depends(require_sysadmin),
):
    ok, checks = _dependency_checks(db)
    status_code = 200 if ok else 503
    return JSONResponse(status_code=status_code, content={"ok": ok, "checks": checks})


@router.get("/metrics", include_in_schema=False)
def metrics(_=Depends(require_sysadmin)):
    return PlainTextResponse(
        metrics_module.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
