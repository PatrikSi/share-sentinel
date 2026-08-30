import redis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import metrics as metrics_module
from app.db import engine, get_db
from app.deps import require_sysadmin
from app.redis_client import create_redis_client
from app.services import storage

router = APIRouter(tags=["health"])
redis_client = create_redis_client()


@router.get("/healthz")
def healthz():
    return {"ok": True}


def _dependency_checks(db: Session, *, deep: bool = False) -> tuple[bool, dict[str, str], dict[str, object]]:
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

    storage_status = storage.artifact_storage_status(verify_write=deep)
    checks["artifact_storage"] = storage_status["state"]

    ok = all(value == "ok" for value in checks.values())
    return ok, checks, {"artifact_storage": storage_status}


@router.get("/healthz/ready")
def healthz_ready(db: Session = Depends(get_db)):
    ok, checks, details = _dependency_checks(db)
    status_code = 200 if ok else 503
    return JSONResponse(status_code=status_code, content={"ok": ok, "checks": checks, "details": details})


@router.get("/healthz/deep")
def healthz_deep(
    db: Session = Depends(get_db),
    _=Depends(require_sysadmin),
):
    ok, checks, details = _dependency_checks(db, deep=True)
    status_code = 200 if ok else 503
    return JSONResponse(status_code=status_code, content={"ok": ok, "checks": checks, "details": details})


@router.get("/metrics", include_in_schema=False)
def metrics(_=Depends(require_sysadmin)):
    return PlainTextResponse(
        metrics_module.render_prometheus(engine.pool),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
