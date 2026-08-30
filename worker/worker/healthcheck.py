import json
import math
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import redis


def _read_timeout(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(0.1, value)


def _read_non_negative(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


REDIS_CONNECT_TIMEOUT_SECONDS = _read_timeout("REDIS_CONNECT_TIMEOUT_SECONDS", 3.0)
REDIS_SOCKET_TIMEOUT_SECONDS = _read_timeout("REDIS_SOCKET_TIMEOUT_SECONDS", 5.0)
WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS = max(
    1,
    int(_read_timeout("WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS", 5.0)),
)
WORKER_DATABASE_STATEMENT_TIMEOUT_MS = max(
    1000,
    int(_read_timeout("WORKER_DATABASE_STATEMENT_TIMEOUT_MS", 120000.0)),
)
ARTIFACT_STORAGE_MIN_FREE_BYTES = max(
    0,
    int(_read_non_negative("ARTIFACT_STORAGE_MIN_FREE_BYTES", 1024 * 1024 * 1024)),
)
ARTIFACT_STORAGE_MIN_FREE_PERCENT = min(
    100.0,
    _read_non_negative("ARTIFACT_STORAGE_MIN_FREE_PERCENT", 5.0),
)


def _parse_heartbeat(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_heartbeat(path: Path, timeout_seconds: int, now: datetime | None = None) -> None:
    payload = _parse_heartbeat(path)
    heartbeat_at = datetime.fromisoformat(str(payload["ts"]))
    current_time = now or datetime.now(tz=UTC)
    age_seconds = (current_time - heartbeat_at).total_seconds()
    if age_seconds > timeout_seconds:
        raise RuntimeError(f"worker heartbeat is stale age_seconds={age_seconds:.1f}")


def check_database(database_url: str) -> None:
    normalized_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(
        normalized_url,
        connect_timeout=WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={WORKER_DATABASE_STATEMENT_TIMEOUT_MS}",
    ) as conn:
        conn.execute("SELECT 1").fetchone()


def check_redis(redis_url: str) -> None:
    redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    ).ping()


def check_artifact_storage(artifact_storage_path: str) -> None:
    root = Path(artifact_storage_path)
    if not root.exists() or not root.is_dir():
        raise RuntimeError("artifact storage path is missing")
    next(root.iterdir(), None)
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError("artifact storage path is not accessible")
    usage = shutil.disk_usage(root)
    free_percent = (usage.free / usage.total * 100.0) if usage.total else 0.0
    if usage.free < ARTIFACT_STORAGE_MIN_FREE_BYTES or free_percent < ARTIFACT_STORAGE_MIN_FREE_PERCENT:
        raise RuntimeError(
            "artifact storage free space is below the configured threshold "
            f"free_bytes={usage.free} free_percent={free_percent:.3f}"
        )


def run_healthcheck(
    database_url: str,
    redis_url: str,
    artifact_storage_path: str,
    heartbeat_path: str,
    timeout_seconds: int,
) -> tuple[bool, dict[str, str]]:
    checks: dict[str, str] = {}

    try:
        check_heartbeat(Path(heartbeat_path), timeout_seconds)
        checks["heartbeat"] = "ok"
    except Exception:  # noqa: BLE001
        checks["heartbeat"] = "error"

    try:
        check_database(database_url)
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        checks["database"] = "error"

    try:
        check_redis(redis_url)
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "error"

    try:
        check_artifact_storage(artifact_storage_path)
        checks["artifact_storage"] = "ok"
    except Exception:  # noqa: BLE001
        checks["artifact_storage"] = "error"

    # Redis transports prompt delivery, but the worker deliberately recovers
    # durable database-backed ingest and comparison work while Redis is down.
    # Report that dependency as degraded without causing a restart loop.
    if checks["redis"] == "error":
        checks["redis"] = "degraded"
    ok = all(checks[name] == "ok" for name in ("heartbeat", "database", "artifact_storage"))
    return ok, checks


def main() -> int:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://share_sentinel:share_sentinel@db:5432/share_sentinel",
    )
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    artifact_storage_path = os.getenv("ARTIFACT_STORAGE_PATH", "/artifacts")
    heartbeat_path = os.getenv("WORKER_HEARTBEAT_PATH", "/tmp/share-sentinel-worker-heartbeat.json")
    timeout_seconds = _read_timeout("WORKER_HEALTH_TIMEOUT_SECONDS", 45.0)

    ok, checks = run_healthcheck(database_url, redis_url, artifact_storage_path, heartbeat_path, timeout_seconds)
    if ok:
        return 0

    print(json.dumps({"ok": ok, "checks": checks}, separators=(",", ":")), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
