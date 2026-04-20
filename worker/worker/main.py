import gzip
import hashlib
import json
import logging
import os
import socket
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import ijson
import psycopg
import redis

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("share_sentinel.worker")


def _read_int_env(name: str, default: int, min_value: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("invalid integer value for %s=%r; using default=%s", name, raw, default)
        return default

    if value < min_value:
        logger.warning("value for %s=%s is below min=%s; using min", name, value, min_value)
        return min_value
    return value


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://share_sentinel:share_sentinel@db:5432/share_sentinel").replace(
    "postgresql+psycopg://", "postgresql://"
)
ARTIFACT_STORAGE_PATH = os.getenv("ARTIFACT_STORAGE_PATH", "/artifacts")

STREAM_NAME = "ingest_jobs"
GROUP_NAME = "ingest_workers"
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"

BATCH_SIZE = _read_int_env("INGEST_BATCH_SIZE", 5000, min_value=1)
PROGRESS_EVERY_LINES = _read_int_env("INGEST_PROGRESS_EVERY_LINES", 2000, min_value=1)
RECOVERY_SCAN_SECONDS = _read_int_env("INGEST_RECOVERY_SCAN_SECONDS", 8, min_value=1)
RECOVERY_SCAN_LIMIT = _read_int_env("INGEST_RECOVERY_SCAN_LIMIT", 8, min_value=1)
PENDING_IDLE_MS = _read_int_env("INGEST_PENDING_IDLE_MS", 60000, min_value=1)
JSON_COMPAT_MAX_BYTES = _read_int_env("INGEST_JSON_COMPAT_MAX_BYTES", 50 * 1024 * 1024, min_value=1024)
GZIP_DECOMPRESSED_MAX_BYTES = _read_int_env("INGEST_GZIP_MAX_BYTES", 4 * 1024 * 1024 * 1024, min_value=1024)
GZIP_DECOMPRESSED_MAX_RATIO = _read_int_env("INGEST_GZIP_MAX_EXPANSION_RATIO", 200, min_value=1)
STALE_INGESTING_SECONDS = _read_int_env("INGEST_STALE_RUN_SECONDS", 300, min_value=30)
INGEST_MAX_RETRIES = _read_int_env("INGEST_MAX_RETRIES", 4, min_value=0)
INGEST_RETRY_BASE_SECONDS = _read_int_env("INGEST_RETRY_BASE_SECONDS", 30, min_value=1)
INGEST_RETRY_MAX_SECONDS = _read_int_env("INGEST_RETRY_MAX_SECONDS", 900, min_value=1)
WORKER_HEARTBEAT_PATH = os.getenv("WORKER_HEARTBEAT_PATH", "/tmp/share-sentinel-worker-heartbeat.json")
WORKER_HEARTBEAT_INTERVAL_SECONDS = _read_int_env("WORKER_HEARTBEAT_INTERVAL_SECONDS", 15, min_value=1)
WORKER_HEALTH_TIMEOUT_SECONDS = _read_int_env("WORKER_HEALTH_TIMEOUT_SECONDS", 45, min_value=5)

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

INGEST_OPERATION_EXCEPTIONS = (
    psycopg.Error,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)
STREAM_MESSAGE_RETRYABLE_EXCEPTIONS = INGEST_OPERATION_EXCEPTIONS + (redis.RedisError,)
SHARE_TYPE_TO_RESOURCE_TYPE = {
    "smb": "smb_share",
    "nfs": "nfs_share",
}
RESOURCE_TYPE_TO_SHARE_TYPE = {value: key for key, value in SHARE_TYPE_TO_RESOURCE_TYPE.items()}
ACCESS_LEVEL_ALIASES = {
    "no_access": "no_access",
    "none": "no_access",
    "denied": "no_access",
    "list_only": "list_only",
    "list": "list_only",
    "browse": "list_only",
    "enumerate": "list_only",
    "readable": "readable",
    "read": "readable",
    "read_only": "readable",
    "read-write": "readable",
    "read_write": "readable",
    "write": "readable",
    "writable": "readable",
    "modify": "readable",
    "full": "readable",
    "full_control": "readable",
    "rw": "readable",
}
GZIP_DECOMPRESSED_LIMIT_ERROR = "gzip artifact exceeds decompressed size limit"


def _safe_run_id(fields: dict[str, str] | None) -> str | None:
    if not isinstance(fields, dict):
        return None
    return _normalize_uuid_str(fields.get("run_id"))


def _normalize_uuid_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _should_log_redis_error(last_logged_at: float, now: float, interval_seconds: float = 30.0) -> bool:
    return now - last_logged_at >= interval_seconds


def should_ack_stream_result(result: str) -> bool:
    return result != "busy"


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def advisory_lock_key(run_id: str) -> int:
    return uuid.UUID(run_id).int % (2**63 - 1)


def _artifact_key_to_path(key: str) -> Path:
    pure_path = PurePosixPath(str(key or ""))
    if pure_path.is_absolute():
        raise ValueError("artifact key must be relative")
    parts = tuple(part for part in pure_path.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise ValueError("artifact key must remain inside artifact storage")
    return Path(ARTIFACT_STORAGE_PATH).joinpath(*parts)


def open_artifact_stream(key: str):
    return open(_artifact_key_to_path(key), "rb")


def write_audit(conn: psycopg.Connection, project_id: str, action: str, object_type: str, object_id: str, metadata: dict[str, Any]):
    conn.execute(
        """
        INSERT INTO audit_events (project_id, action, object_type, object_id, metadata)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (project_id, action, object_type, object_id, json.dumps(metadata)),
    )


def upsert_endpoint(conn: psycopg.Connection, run_id: str, rec: dict[str, Any]) -> int:
    row = conn.execute(
        """
        INSERT INTO endpoints (run_id, endpoint_key, ip, hostname, domain, smb_dialect, smb_signing, auth_method)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, endpoint_key)
        DO UPDATE SET
            ip = COALESCE(EXCLUDED.ip, endpoints.ip),
            hostname = COALESCE(EXCLUDED.hostname, endpoints.hostname),
            domain = COALESCE(EXCLUDED.domain, endpoints.domain),
            smb_dialect = COALESCE(EXCLUDED.smb_dialect, endpoints.smb_dialect),
            smb_signing = COALESCE(EXCLUDED.smb_signing, endpoints.smb_signing),
            auth_method = COALESCE(EXCLUDED.auth_method, endpoints.auth_method)
        RETURNING id
        """,
        (
            run_id,
            rec.get("endpoint_key"),
            rec.get("ip"),
            rec.get("hostname"),
            rec.get("domain"),
            (rec.get("smb") or {}).get("dialect"),
            (rec.get("smb") or {}).get("signing"),
            (rec.get("auth") or {}).get("method"),
        ),
    ).fetchone()
    return int(row[0])


def upsert_resource(conn: psycopg.Connection, run_id: str, endpoint_id: int, rec: dict[str, Any]) -> int:
    row = conn.execute(
        """
        INSERT INTO resources (run_id, endpoint_id, resource_type, name, remark, access_level)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, endpoint_id, resource_type, name)
        DO UPDATE SET
            remark = COALESCE(EXCLUDED.remark, resources.remark),
            access_level = EXCLUDED.access_level
        RETURNING id
        """,
        (
            run_id,
            endpoint_id,
            rec.get("resource_type", "smb_share"),
            rec.get("name"),
            rec.get("remark"),
            _normalize_access_level(rec.get("access_level")),
        ),
    ).fetchone()
    return int(row[0])


def flush_item_batch(conn: psycopg.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO items (run_id, resource_id, path, name, is_dir)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id, resource_id, path) DO NOTHING
            """,
            rows,
        )
    rows.clear()


def flush_error_batch(conn: psycopg.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ingest_errors (run_id, severity, code, message, endpoint_key, resource_name, path, fingerprint)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, fingerprint) DO NOTHING
            """,
            rows,
        )
    rows.clear()


def _ingest_error_fingerprint(
    severity: str,
    code: str,
    message: str,
    endpoint_key: str | None,
    resource_name: str | None,
    path: str | None,
) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    digest.update(
        json.dumps(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "endpoint_key": endpoint_key,
                "resource_name": resource_name,
                "path": path,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )
    return digest.hexdigest()


def build_ingest_error_row(
    run_id: str,
    severity: str,
    code: str,
    message: str,
    endpoint_key: str | None,
    resource_name: str | None,
    path: str | None,
) -> tuple[str, str, str, str, str | None, str | None, str | None, str]:
    return (
        run_id,
        severity,
        code,
        message,
        endpoint_key,
        resource_name,
        path,
        _ingest_error_fingerprint(severity, code, message, endpoint_key, resource_name, path),
    )


def update_run_status(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    line_offset: int,
    summary: dict[str, Any],
    last_error: str | None = None,
    extra_progress: dict[str, Any] | None = None,
):
    ingest_progress = {"line_offset": line_offset, "heartbeat_at": now_iso()}
    if last_error:
        ingest_progress["last_error"] = last_error
    if extra_progress:
        ingest_progress.update(extra_progress)
    conn.execute(
        """
        UPDATE scan_runs
        SET status = %s,
            ingest_progress = %s::jsonb,
            summary = %s::jsonb
        WHERE id = %s
        """,
        (status, json.dumps(ingest_progress), json.dumps(summary), run_id),
    )


def parse_summary(raw: Any) -> dict[str, int]:
    base = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}
    if not isinstance(raw, dict):
        return base
    for key in base:
        value = raw.get(key)
        if isinstance(value, int):
            base[key] = value
    return base


def parse_offset(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 0
    value = raw.get("line_offset", 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_attempt_count(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 0
    value = raw.get("attempt_count", 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _normalize_share_type(raw_share_type: Any, raw_resource_type: Any = None) -> str:
    if isinstance(raw_share_type, str):
        normalized = raw_share_type.strip().lower()
        if normalized in SHARE_TYPE_TO_RESOURCE_TYPE:
            return normalized

    if isinstance(raw_resource_type, str):
        normalized_resource_type = raw_resource_type.strip().lower()
        share_type = RESOURCE_TYPE_TO_SHARE_TYPE.get(normalized_resource_type)
        if share_type:
            return share_type

    return "smb"


def _resource_type_from_share_type(share_type: str) -> str:
    return SHARE_TYPE_TO_RESOURCE_TYPE.get(share_type, "smb_share")


def _normalize_access_level(raw_access_level: Any) -> str:
    if isinstance(raw_access_level, str):
        normalized = raw_access_level.strip().lower().replace(" ", "_")
        if normalized in ACCESS_LEVEL_ALIASES:
            return ACCESS_LEVEL_ALIASES[normalized]
    return "no_access"


def _bind_record_to_ingest_run(rec: dict[str, Any], run_id: str) -> dict[str, Any]:
    record_run_id = rec.get("run_id")
    if record_run_id is None or str(record_run_id) != run_id:
        normalized = dict(rec)
        normalized["run_id"] = run_id
        return normalized
    return rec


def validate_record(rec: dict[str, Any]) -> tuple[bool, str | None]:
    rec_type = rec.get("type")
    if rec_type not in {"run_meta", "endpoint", "resource", "item", "error", "run_end"}:
        return False, "unknown record type"

    required: dict[str, tuple[str, ...]] = {
        "run_meta": ("schema_version", "tool", "tool_version", "run_id", "started_at"),
        "endpoint": ("run_id", "endpoint_key"),
        "resource": ("run_id", "endpoint_key", "name"),
        "item": ("run_id", "endpoint_key", "resource_name", "path"),
        "error": ("run_id", "severity", "code", "message"),
        "run_end": ("run_id", "finished_at"),
    }
    for field in required[rec_type]:
        if rec.get(field) in (None, ""):
            return False, f"missing field: {field}"

    if rec_type == "run_meta":
        try:
            if int(rec.get("schema_version")) != 1:
                return False, "unsupported schema_version"
        except (TypeError, ValueError):
            return False, "invalid schema_version"

    if rec_type == "item" and not rec.get("name"):
        rec["name"] = PurePosixPath(str(rec.get("path", ""))).name or ""

    if rec_type in {"resource", "item"}:
        share_type = _normalize_share_type(rec.get("share_type"), rec.get("resource_type"))
        rec["share_type"] = share_type
        rec["resource_type"] = _resource_type_from_share_type(share_type)
    if rec_type == "resource":
        rec["access_level"] = _normalize_access_level(rec.get("access_level"))
    return True, None


def _normalize_windows_path(value: Any) -> str:
    path = str(value or "\\").replace("/", "\\").strip()
    if not path:
        return "\\"
    if not path.startswith("\\"):
        path = f"\\{path}"
    return path


def _join_windows_path(parent: Any, name: Any) -> str:
    base = _normalize_windows_path(parent)
    leaf = str(name or "").strip().strip("\\/")
    if not leaf:
        return base
    if base == "\\":
        return f"\\{leaf}"
    return base.rstrip("\\") + "\\" + leaf


def _iter_items_from_entries(
    run_id: str,
    endpoint_key: str,
    resource_name: str,
    entries: list[Any],
    share_type: str = "smb",
    parent_path: str = "\\",
):
    resource_type = _resource_type_from_share_type(share_type)
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue

        name = str(raw_entry.get("name") or "").strip()
        is_dir = bool(raw_entry.get("is_dir", False))
        full_path = _join_windows_path(raw_entry.get("path") or parent_path, name)
        if not name:
            name = PurePosixPath(full_path.replace("\\", "/")).name or full_path

        yield {
            "type": "item",
            "run_id": run_id,
            "endpoint_key": endpoint_key,
            "resource_type": resource_type,
            "share_type": share_type,
            "resource_name": resource_name,
            "path": full_path,
            "name": name,
            "is_dir": is_dir,
        }

        children = raw_entry.get("children")
        if is_dir and isinstance(children, list):
            yield from _iter_items_from_entries(run_id, endpoint_key, resource_name, children, share_type, full_path)


def _records_from_endpoint_payload(raw_endpoint: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    endpoint_key = str(raw_endpoint.get("endpoint_key") or "").strip()
    if not endpoint_key:
        ip = str(raw_endpoint.get("ip") or "").strip()
        hostname = str(raw_endpoint.get("hostname") or "").strip()
        endpoint_key = f"{ip}:445" if ip else (f"{hostname}:445" if hostname else "unknown:445")

    records = [
        {
            "type": "endpoint",
            "run_id": run_id,
            "endpoint_key": endpoint_key,
            "ip": raw_endpoint.get("ip"),
            "hostname": raw_endpoint.get("hostname"),
            "domain": raw_endpoint.get("domain"),
            "auth": raw_endpoint.get("auth") if isinstance(raw_endpoint.get("auth"), dict) else None,
            "smb": raw_endpoint.get("smb") if isinstance(raw_endpoint.get("smb"), dict) else None,
            "nfs": raw_endpoint.get("nfs") if isinstance(raw_endpoint.get("nfs"), dict) else None,
        }
    ]

    raw_shares = raw_endpoint.get("shares")
    if not isinstance(raw_shares, list):
        return records

    for raw_share in raw_shares:
        if not isinstance(raw_share, dict):
            continue

        share_name = str(raw_share.get("name") or "").strip()
        if not share_name:
            continue
        share_type = _normalize_share_type(raw_share.get("share_type"), raw_share.get("resource_type"))

        records.append(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": endpoint_key,
                "resource_type": _resource_type_from_share_type(share_type),
                "share_type": share_type,
                "name": share_name,
                "remark": raw_share.get("remark"),
                "access_level": raw_share.get("access_level", "no_access"),
            }
        )

        raw_entries = raw_share.get("entries")
        if isinstance(raw_entries, list):
            records.extend(_iter_items_from_entries(run_id, endpoint_key, share_name, raw_entries, share_type=share_type))

    return records


def _records_from_nested_json(doc: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    endpoints_raw = doc.get("endpoints")
    if not isinstance(endpoints_raw, list):
        return []

    records: list[dict[str, Any]] = []
    meta_raw = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    run_raw = doc.get("run") if isinstance(doc.get("run"), dict) else {}
    collection_raw = doc.get("collection") if isinstance(doc.get("collection"), dict) else {}
    schema_version = doc.get("schema_version", 1)
    started_at = meta_raw.get("started_at") or run_raw.get("created_at") or now_iso()
    finished_at = meta_raw.get("finished_at") or now_iso()

    records.append(
        {
            "type": "run_meta",
            "schema_version": int(schema_version) if isinstance(schema_version, int) else 1,
            "tool": meta_raw.get("tool") or "share-sentinel-import",
            "tool_version": meta_raw.get("tool_version") or "unknown",
            "run_id": meta_raw.get("run_id") or run_raw.get("run_id") or run_id,
            "started_at": started_at,
            "operator_label": meta_raw.get("operator_label"),
            "collection": collection_raw or None,
            "auth": meta_raw.get("auth") if isinstance(meta_raw.get("auth"), dict) else None,
        }
    )

    issue_summary = doc.get("issue_summary")
    if isinstance(issue_summary, list):
        for issue in issue_summary:
            if not isinstance(issue, dict):
                continue
            records.append(
                {
                    "type": "error",
                    "run_id": run_id,
                    "severity": issue.get("severity", "error"),
                    "code": issue.get("code", "UNKNOWN"),
                    "message": issue.get("sample_message") or issue.get("message") or "issue summary entry",
                    "hint": issue.get("sample_hint") or issue.get("hint"),
                }
            )

    for raw_endpoint in endpoints_raw:
        if not isinstance(raw_endpoint, dict):
            continue
        records.extend(_records_from_endpoint_payload(raw_endpoint, run_id))

    summary_raw = doc.get("summary")
    if isinstance(summary_raw, dict):
        records.append(
            {
                "type": "run_end",
                "run_id": run_id,
                "finished_at": finished_at,
                "stats": summary_raw,
            }
        )

    return records


def records_from_json_document(doc: Any, run_id: str) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        records = []
        for row in doc:
            if not isinstance(row, dict):
                continue
            rec = dict(row)
            rec.setdefault("run_id", run_id)
            records.append(rec)
        if records:
            return records
        raise ValueError("json array contains no object records")

    if isinstance(doc, dict) and isinstance(doc.get("type"), str):
        rec = dict(doc)
        rec.setdefault("run_id", run_id)
        return [rec]

    if isinstance(doc, dict):
        nested = _records_from_nested_json(doc, run_id)
        if nested:
            return nested

    raise ValueError("unsupported JSON artifact format")


def _is_json_artifact(artifact_key: str, content_type: str) -> bool:
    normalized_key = artifact_key.lower()
    if normalized_key.endswith(".json") or normalized_key.endswith(".json.gz"):
        return True
    return "json" in content_type and "ndjson" not in content_type


def _load_json_records_from_bytes(raw_bytes: bytes, run_id: str) -> list[dict[str, Any]] | None:
    try:
        json_doc = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return None
    return records_from_json_document(json_doc, run_id)


def _load_first_json_item(fp, prefix: str) -> Any:
    fp.seek(0)
    iterator = ijson.items(fp, prefix)
    try:
        return next(iterator)
    except StopIteration:
        return None


def _iter_records_from_streamable_json_file(fp, run_id: str):
    meta_raw = _load_first_json_item(fp, "meta")
    run_raw = _load_first_json_item(fp, "run")
    collection_raw = _load_first_json_item(fp, "collection")
    summary_raw = _load_first_json_item(fp, "summary")
    schema_version = _load_first_json_item(fp, "schema_version")
    started_at = (meta_raw or {}).get("started_at") if isinstance(meta_raw, dict) else None
    started_at = started_at or ((run_raw or {}).get("created_at") if isinstance(run_raw, dict) else None) or now_iso()
    finished_at = (meta_raw or {}).get("finished_at") if isinstance(meta_raw, dict) else None
    finished_at = finished_at or now_iso()

    if isinstance(meta_raw, dict) or isinstance(run_raw, dict) or isinstance(collection_raw, dict) or isinstance(summary_raw, dict):
        yield {
            "type": "run_meta",
            "schema_version": int(schema_version) if isinstance(schema_version, int) else 1,
            "tool": (meta_raw or {}).get("tool") if isinstance(meta_raw, dict) else "share-sentinel-import",
            "tool_version": (meta_raw or {}).get("tool_version") if isinstance(meta_raw, dict) else "unknown",
            "run_id": (meta_raw or {}).get("run_id") if isinstance(meta_raw, dict) else None,
            "started_at": started_at,
            "operator_label": (meta_raw or {}).get("operator_label") if isinstance(meta_raw, dict) else None,
            "collection": collection_raw if isinstance(collection_raw, dict) else None,
            "auth": (meta_raw or {}).get("auth") if isinstance(meta_raw, dict) and isinstance((meta_raw or {}).get("auth"), dict) else None,
        }

    issue_seen = False
    fp.seek(0)
    for issue in ijson.items(fp, "issue_summary.item"):
        if not isinstance(issue, dict):
            continue
        issue_seen = True
        yield {
            "type": "error",
            "run_id": run_id,
            "severity": issue.get("severity", "error"),
            "code": issue.get("code", "UNKNOWN"),
            "message": issue.get("sample_message") or issue.get("message") or "issue summary entry",
            "hint": issue.get("sample_hint") or issue.get("hint"),
        }

    endpoint_seen = False
    fp.seek(0)
    for raw_endpoint in ijson.items(fp, "endpoints.item"):
        if not isinstance(raw_endpoint, dict):
            continue
        endpoint_seen = True
        for record in _records_from_endpoint_payload(raw_endpoint, run_id):
            yield record

    if isinstance(summary_raw, dict):
        yield {
            "type": "run_end",
            "run_id": run_id,
            "finished_at": finished_at,
            "stats": summary_raw,
        }

    if not endpoint_seen and not issue_seen and not isinstance(meta_raw, dict) and not isinstance(run_raw, dict) and not isinstance(summary_raw, dict):
        raise ValueError("unsupported JSON artifact format")


def _read_json_compat_bytes(body, gzip_input: bool, max_bytes: int) -> bytes:
    reader = gzip.GzipFile(fileobj=body) if gzip_input else body
    total = 0
    buffer = bytearray()
    try:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("JSON artifact exceeds non-streamable compatibility limit")
            buffer.extend(chunk)
    finally:
        if gzip_input:
            reader.close()

    return bytes(buffer)


def _gzip_decompressed_limit(artifact_size: int | None) -> int:
    if isinstance(artifact_size, int) and artifact_size > 0:
        return min(GZIP_DECOMPRESSED_MAX_BYTES, max(JSON_COMPAT_MAX_BYTES, artifact_size * GZIP_DECOMPRESSED_MAX_RATIO))
    return GZIP_DECOMPRESSED_MAX_BYTES


class _LimitedReader:
    def __init__(self, reader, max_bytes: int, error_message: str):
        self._reader = reader
        self._max_bytes = max_bytes
        self._error_message = error_message
        self._total = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _track(self, size: int) -> None:
        self._total += max(0, size)
        if self._total > self._max_bytes:
            raise ValueError(self._error_message)

    def read(self, size: int = -1):
        chunk = self._reader.read(size)
        self._track(len(chunk or b""))
        return chunk

    def read1(self, size: int = -1):
        if hasattr(self._reader, "read1"):
            chunk = self._reader.read1(size)
        else:
            chunk = self._reader.read(size)
        self._track(len(chunk or b""))
        return chunk

    def readline(self, size: int = -1):
        line = self._reader.readline(size)
        self._track(len(line or b""))
        return line

    def readinto(self, b):
        count = self._reader.readinto(b)
        if count is not None:
            self._track(count)
        return count

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if line in {b"", ""}:
            raise StopIteration
        return line

    def close(self):
        return self._reader.close()

    def __getattr__(self, name: str):
        return getattr(self._reader, name)


def _public_ingest_error(exc: BaseException) -> str:
    if isinstance(exc, psycopg.Error):
        return "database operation failed during ingest"
    if isinstance(exc, OSError):
        return "artifact storage read failed during ingest"
    if isinstance(exc, TypeError):
        return "artifact contained an unexpected record shape"
    if isinstance(exc, ValueError):
        detail = str(exc).strip()
        if detail in {
            "missing artifact key",
            "unsupported JSON artifact format",
            "JSON artifact exceeds non-streamable compatibility limit",
            GZIP_DECOMPRESSED_LIMIT_ERROR,
        }:
            return detail
        return "artifact validation failed during ingest"
    return "unexpected ingest failure"


def _is_retryable_ingest_error(exc: BaseException) -> bool:
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError, OSError, RuntimeError))


def _retry_backoff_seconds(attempt_count: int) -> int:
    bounded_attempt = max(1, attempt_count)
    delay = INGEST_RETRY_BASE_SECONDS * (2 ** (bounded_attempt - 1))
    return min(INGEST_RETRY_MAX_SECONDS, delay)


def _write_worker_heartbeat(status: str, run_id: str | None = None, line_offset: int | None = None) -> None:
    heartbeat_path = Path(WORKER_HEARTBEAT_PATH)
    payload = {
        "ts": now_iso(),
        "consumer": CONSUMER_NAME,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "status": status,
    }
    if run_id:
        payload["run_id"] = run_id
    if line_offset is not None:
        payload["line_offset"] = line_offset

    try:
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = heartbeat_path.with_suffix(heartbeat_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(heartbeat_path)
    except OSError:
        logger.exception("failed writing worker heartbeat path=%s", heartbeat_path)


def discover_recoverable_runs(limit: int = 8) -> list[dict[str, str]]:
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            """
            SELECT id::text, project_id::text, artifact_key
            FROM scan_runs
            WHERE artifact_key IS NOT NULL
              AND (
                  (
                      status = 'UPLOADED'
                      AND COALESCE(
                          NULLIF(ingest_progress->>'next_retry_at', '')::timestamptz,
                          TO_TIMESTAMP(0)
                      ) <= NOW()
                  )
                  OR (
                      status = 'INGESTING'
                      AND COALESCE(
                          NULLIF(ingest_progress->>'heartbeat_at', '')::timestamptz,
                          created_at
                      ) <= NOW() - (%s * INTERVAL '1 second')
                  )
              )
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (STALE_INGESTING_SECONDS, limit),
        ).fetchall()
    return [
        {
            "run_id": row[0],
            "project_id": row[1],
            "artifact_key": row[2],
        }
        for row in rows
    ]


def discover_uploaded_runs(limit: int = 8) -> list[dict[str, str]]:
    return discover_recoverable_runs(limit=limit)


def process_job(fields: dict[str, str]) -> str:
    run_id = _normalize_uuid_str(fields.get("run_id"))
    project_id = _normalize_uuid_str(fields.get("project_id"))
    artifact_key = fields.get("artifact_key")
    last_line_offset = 0
    last_counts = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}

    if not run_id:
        logger.error("invalid job payload missing or invalid run_id: %s", fields)
        return "ignored"

    with psycopg.connect(DATABASE_URL) as conn:
        lock_key = advisory_lock_key(run_id)
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,)).fetchone()[0]
        if not locked:
            logger.info("run is already being processed run_id=%s", run_id)
            return "busy"

        try:
            row = conn.execute(
                """
                SELECT project_id::text, artifact_key, status::text, summary, ingest_progress, artifact_content_type, artifact_size
                FROM scan_runs
                WHERE id = %s
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                logger.warning("run not found run_id=%s", run_id)
                return "ignored"

            db_project_id, db_artifact_key, status, summary_raw, progress_raw, artifact_content_type, artifact_size = row
            project_id = project_id or db_project_id
            artifact_key = artifact_key or db_artifact_key
            if not artifact_key:
                update_run_status(conn, run_id, "FAILED", parse_offset(progress_raw), parse_summary(summary_raw), "missing artifact key")
                conn.commit()
                return "failed"
            if status in {"COMPLETE", "FAILED"}:
                return "ignored"

            counts = parse_summary(summary_raw)
            line_offset = parse_offset(progress_raw)
            attempt_count = parse_attempt_count(progress_raw)
            last_line_offset = line_offset
            last_counts = counts.copy()
            last_worker_heartbeat = 0.0

            def emit_processing_heartbeat(force: bool = False) -> None:
                nonlocal last_worker_heartbeat
                now = time.time()
                if force or now - last_worker_heartbeat >= WORKER_HEARTBEAT_INTERVAL_SECONDS:
                    _write_worker_heartbeat("processing", run_id=run_id, line_offset=line_offset)
                    last_worker_heartbeat = now

            emit_processing_heartbeat(force=True)
            update_run_status(conn, run_id, "INGESTING", line_offset, counts, extra_progress={"attempt_count": attempt_count})
            write_audit(
                conn,
                project_id,
                "INGEST_STARTED",
                "scan_run",
                run_id,
                {"worker": CONSUMER_NAME, "resume_from_line": line_offset, "ts": now_iso()},
            )
            conn.commit()

            endpoint_cache: dict[str, int] = {}
            resource_cache: dict[tuple[str, str, str], int] = {}
            item_batch: list[tuple] = []
            error_batch: list[tuple] = []

            def process_record(rec: dict[str, Any]) -> None:
                nonlocal counts
                rec = _bind_record_to_ingest_run(rec, run_id)

                valid, reason = validate_record(rec)
                if not valid:
                    error_batch.append(
                        build_ingest_error_row(
                            run_id,
                            "error",
                            "SCHEMA_INVALID",
                            reason or "invalid record",
                            rec.get("endpoint_key"),
                            rec.get("resource_name"),
                            rec.get("path"),
                        )
                    )
                    counts["errors"] += 1
                    if len(error_batch) >= BATCH_SIZE:
                        flush_error_batch(conn, error_batch)
                    return

                rec_type = rec.get("type")

                if rec_type == "endpoint":
                    endpoint_id = upsert_endpoint(conn, run_id, rec)
                    endpoint_cache[rec.get("endpoint_key", "")] = endpoint_id
                    counts["endpoints"] += 1
                elif rec_type == "resource":
                    endpoint_key = rec.get("endpoint_key", "")
                    resource_type = str(rec.get("resource_type") or "smb_share")
                    resource_name = rec.get("name", "")
                    endpoint_id = endpoint_cache.get(endpoint_key)
                    if endpoint_id is None:
                        endpoint_id = upsert_endpoint(conn, run_id, {"endpoint_key": endpoint_key})
                        endpoint_cache[endpoint_key] = endpoint_id
                    resource_id = upsert_resource(conn, run_id, endpoint_id, rec)
                    resource_cache[(endpoint_key, resource_name, resource_type)] = resource_id
                    counts["resources"] += 1
                elif rec_type == "item":
                    endpoint_key = rec.get("endpoint_key", "")
                    resource_name = rec.get("resource_name", "")
                    resource_type = str(rec.get("resource_type") or "smb_share")
                    key = (endpoint_key, resource_name, resource_type)
                    resource_id = resource_cache.get(key)
                    if resource_id is None:
                        endpoint_id = endpoint_cache.get(endpoint_key)
                        if endpoint_id is None:
                            endpoint_id = upsert_endpoint(conn, run_id, {"endpoint_key": endpoint_key})
                            endpoint_cache[endpoint_key] = endpoint_id
                        resource_id = upsert_resource(
                            conn,
                            run_id,
                            endpoint_id,
                            {
                                "resource_type": resource_type,
                                "name": resource_name,
                                "remark": None,
                                "access_level": "no_access",
                            },
                        )
                        resource_cache[key] = resource_id
                    item_batch.append(
                        (
                            run_id,
                            resource_id,
                            rec.get("path", ""),
                            rec.get("name", ""),
                            bool(rec.get("is_dir", False)),
                        )
                    )
                    counts["items"] += 1
                    if len(item_batch) >= BATCH_SIZE:
                        flush_item_batch(conn, item_batch)
                elif rec_type == "error":
                    error_batch.append(
                        build_ingest_error_row(
                            run_id,
                            rec.get("severity", "error"),
                            rec.get("code", "UNKNOWN"),
                            rec.get("message", ""),
                            rec.get("endpoint_key"),
                            rec.get("resource_name"),
                            rec.get("path"),
                        )
                    )
                    counts["errors"] += 1
                    if len(error_batch) >= BATCH_SIZE:
                        flush_error_batch(conn, error_batch)
                elif rec_type == "run_end":
                    incoming = rec.get("stats") or {}
                    counts = {
                        "endpoints": int(incoming.get("endpoints", counts["endpoints"])),
                        "resources": int(incoming.get("resources", counts["resources"])),
                        "items": int(incoming.get("items", counts["items"])),
                        "errors": int(incoming.get("errors", counts["errors"])),
                    }

            def process_record_iter(records_iter) -> int:
                nonlocal line_offset, last_line_offset, last_counts
                current_line = 0
                for rec in records_iter:
                    current_line += 1
                    if current_line <= line_offset:
                        continue
                    line_offset = current_line
                    emit_processing_heartbeat()
                    process_record(rec)

                    if line_offset % PROGRESS_EVERY_LINES == 0:
                        flush_item_batch(conn, item_batch)
                        flush_error_batch(conn, error_batch)
                        update_run_status(
                            conn,
                            run_id,
                            "INGESTING",
                            line_offset,
                            counts,
                            extra_progress={"attempt_count": attempt_count},
                        )
                        conn.commit()
                        last_line_offset = line_offset
                        last_counts = counts.copy()
                return current_line

            def process_ndjson_lines(reader) -> None:
                nonlocal line_offset, last_line_offset, last_counts
                current_line = 0
                for raw_line in reader:
                    current_line += 1
                    if current_line <= line_offset:
                        continue

                    line_offset = current_line
                    if isinstance(raw_line, bytes):
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    else:
                        line = str(raw_line).strip()
                    emit_processing_heartbeat()
                    if not line:
                        continue

                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as exc:
                        error_batch.append(
                            build_ingest_error_row(
                                run_id,
                                "error",
                                "JSON_DECODE_ERROR",
                                str(exc),
                                None,
                                None,
                                None,
                            )
                        )
                        counts["errors"] += 1
                        if len(error_batch) >= BATCH_SIZE:
                            flush_error_batch(conn, error_batch)
                        continue

                    process_record(rec)
                    if line_offset % PROGRESS_EVERY_LINES == 0:
                        flush_item_batch(conn, item_batch)
                        flush_error_batch(conn, error_batch)
                        update_run_status(
                            conn,
                            run_id,
                            "INGESTING",
                            line_offset,
                            counts,
                            extra_progress={"attempt_count": attempt_count},
                        )
                        conn.commit()
                        last_line_offset = line_offset
                        last_counts = counts.copy()

            json_records: list[dict[str, Any]] | None = None
            content_type = str(artifact_content_type or "").lower()
            json_candidate = _is_json_artifact(artifact_key, content_type)
            gzip_input = artifact_key.endswith(".gz")

            if json_candidate:
                try:
                    with open_artifact_stream(artifact_key) as body:
                        if gzip_input:
                            with gzip.GzipFile(fileobj=body) as gzip_reader, _LimitedReader(
                                gzip_reader,
                                _gzip_decompressed_limit(artifact_size),
                                GZIP_DECOMPRESSED_LIMIT_ERROR,
                            ) as json_reader:
                                process_record_iter(_iter_records_from_streamable_json_file(json_reader, run_id))
                        else:
                            process_record_iter(_iter_records_from_streamable_json_file(body, run_id))
                except ValueError:
                    with open_artifact_stream(artifact_key) as compat_body:
                        raw_json = _read_json_compat_bytes(
                            compat_body,
                            gzip_input=gzip_input,
                            max_bytes=JSON_COMPAT_MAX_BYTES,
                        )
                    json_records = _load_json_records_from_bytes(raw_json, run_id)
                    if json_records is None:
                        raise ValueError("unsupported JSON artifact format")
            else:
                with open_artifact_stream(artifact_key) as body:
                    if gzip_input:
                        with gzip.GzipFile(fileobj=body) as gzip_reader, _LimitedReader(
                            gzip_reader,
                            _gzip_decompressed_limit(artifact_size),
                            GZIP_DECOMPRESSED_LIMIT_ERROR,
                        ) as reader:
                            process_ndjson_lines(reader)
                    else:
                        process_ndjson_lines(body)

            if json_records is not None:
                process_record_iter(json_records)

            flush_item_batch(conn, item_batch)
            flush_error_batch(conn, error_batch)

            update_run_status(conn, run_id, "COMPLETE", line_offset, counts)
            write_audit(
                conn,
                project_id,
                "INGEST_COMPLETED",
                "scan_run",
                run_id,
                {"worker": CONSUMER_NAME, "line_offset": line_offset, "counts": counts},
            )
            conn.commit()
            last_line_offset = line_offset
            last_counts = counts.copy()
            _write_worker_heartbeat("idle")
            return "complete"
        except INGEST_OPERATION_EXCEPTIONS as exc:
            logger.exception("job failed run_id=%s", run_id)
            public_error = _public_ingest_error(exc)
            retryable = _is_retryable_ingest_error(exc)
            attempt_count = parse_attempt_count(progress_raw) + 1
            try:
                conn.rollback()
            except psycopg.Error:
                logger.exception("failed to rollback aborted ingest transaction run_id=%s", run_id)
            try:
                if retryable and attempt_count <= INGEST_MAX_RETRIES:
                    next_retry_at = datetime.now(tz=UTC) + timedelta(seconds=_retry_backoff_seconds(attempt_count))
                    update_run_status(
                        conn,
                        run_id,
                        "UPLOADED",
                        last_line_offset,
                        last_counts,
                        last_error=public_error,
                        extra_progress={
                            "attempt_count": attempt_count,
                            "last_attempt_at": now_iso(),
                            "next_retry_at": next_retry_at.isoformat(),
                        },
                    )
                    if project_id:
                        write_audit(
                            conn,
                            project_id,
                            "INGEST_RETRY_SCHEDULED",
                            "scan_run",
                            run_id,
                            {
                                "worker": CONSUMER_NAME,
                                "error": public_error,
                                "attempt_count": attempt_count,
                                "next_retry_at": next_retry_at.isoformat(),
                            },
                        )
                    conn.commit()
                    _write_worker_heartbeat("idle")
                    return "retry_scheduled"

                update_run_status(
                    conn,
                    run_id,
                    "FAILED",
                    last_line_offset,
                    last_counts,
                    last_error=public_error,
                    extra_progress={"attempt_count": attempt_count, "last_attempt_at": now_iso()},
                )
                if project_id:
                    write_audit(
                        conn,
                        project_id,
                        "INGEST_FAILED",
                        "scan_run",
                        run_id,
                        {"worker": CONSUMER_NAME, "error": public_error, "attempt_count": attempt_count},
                    )
                conn.commit()
                _write_worker_heartbeat("idle")
                return "failed"
            except psycopg.Error:
                logger.exception("failed to persist ingest failure state run_id=%s", run_id)
                try:
                    conn.rollback()
                except psycopg.Error:
                    logger.exception("failed to rollback after ingest failure state persistence run_id=%s", run_id)
            raise
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            except psycopg.Error:
                logger.exception("failed to release ingest advisory lock run_id=%s", run_id)


def ensure_group() -> None:
    try:
        redis_client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def ensure_group_with_retry() -> None:
    last_logged_at = 0.0
    while True:
        try:
            ensure_group()
            return
        except redis.RedisError as exc:
            now = time.time()
            if _should_log_redis_error(last_logged_at, now, interval_seconds=10.0):
                logger.warning("redis stream group setup failed, retrying: %s", exc)
                last_logged_at = now
            _write_worker_heartbeat("waiting_for_redis")
            time.sleep(1)


def claim_stale_messages(start_id: str = "0-0") -> tuple[str, list[tuple[str, dict[str, str]]]]:
    try:
        result = redis_client.xautoclaim(
            STREAM_NAME,
            GROUP_NAME,
            CONSUMER_NAME,
            min_idle_time=PENDING_IDLE_MS,
            start_id=start_id,
            count=10,
        )
    except redis.RedisError:
        return start_id, []

    if not result:
        return start_id, []

    # redis-py returns (next_start_id, [(id, fields), ...], [deleted_ids])
    next_start_id = result[0] if len(result) > 0 else start_id
    messages = result[1] if len(result) > 1 else []
    return next_start_id, messages or []


def main() -> int:
    ensure_group_with_retry()
    logger.info("worker started consumer=%s", CONSUMER_NAME)
    _write_worker_heartbeat("idle")

    last_recovery_scan = 0.0
    last_redis_error_log = 0.0
    last_idle_heartbeat = time.time()
    next_claim_start_id = "0-0"

    while True:
        messages = []
        try:
            messages = redis_client.xreadgroup(
                GROUP_NAME,
                CONSUMER_NAME,
                {STREAM_NAME: ">"},
                count=5,
                block=3000,
            )
        except redis.RedisError as exc:
            now = time.time()
            if _should_log_redis_error(last_redis_error_log, now):
                logger.warning("redis stream read failed, retrying: %s", exc)
                last_redis_error_log = now
            _write_worker_heartbeat("redis_retry")
            time.sleep(1)

        if messages:
            for _, jobs in messages:
                for message_id, fields in jobs:
                    try:
                        result = process_job(fields)
                        if should_ack_stream_result(result):
                            redis_client.xack(STREAM_NAME, GROUP_NAME, message_id)
                    except Exception:
                        logger.exception(
                            "failed processing stream message message_id=%s run_id=%s",
                            message_id,
                            _safe_run_id(fields),
                        )
                        time.sleep(1)

        next_claim_start_id, stale_jobs = claim_stale_messages(next_claim_start_id)
        for message_id, fields in stale_jobs:
            try:
                result = process_job(fields)
                if should_ack_stream_result(result):
                    redis_client.xack(STREAM_NAME, GROUP_NAME, message_id)
            except Exception:
                logger.exception(
                    "failed processing claimed stream message message_id=%s run_id=%s",
                    message_id,
                    _safe_run_id(fields),
                )
                time.sleep(1)

        if time.time() - last_recovery_scan >= RECOVERY_SCAN_SECONDS:
            for recovered in discover_recoverable_runs(limit=RECOVERY_SCAN_LIMIT):
                try:
                    process_job(recovered)
                except Exception:
                    logger.exception("failed processing recovered uploaded run run_id=%s", _safe_run_id(recovered))
                    time.sleep(1)
            last_recovery_scan = time.time()

        now = time.time()
        if now - last_idle_heartbeat >= WORKER_HEARTBEAT_INTERVAL_SECONDS:
            _write_worker_heartbeat("idle")
            last_idle_heartbeat = now


if __name__ == "__main__":
    sys.exit(main())
