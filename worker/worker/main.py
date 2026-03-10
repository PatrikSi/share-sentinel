import gzip
import json
import logging
import os
import socket
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import boto3
import psycopg
import redis
from botocore.exceptions import BotoCoreError, ClientError

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
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "share-sentinel-artifacts")

STREAM_NAME = "ingest_jobs"
GROUP_NAME = "ingest_workers"
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"

BATCH_SIZE = _read_int_env("INGEST_BATCH_SIZE", 5000, min_value=1)
PROGRESS_EVERY_LINES = _read_int_env("INGEST_PROGRESS_EVERY_LINES", 2000, min_value=1)
RECOVERY_SCAN_SECONDS = _read_int_env("INGEST_RECOVERY_SCAN_SECONDS", 8, min_value=1)
PENDING_IDLE_MS = _read_int_env("INGEST_PENDING_IDLE_MS", 60000, min_value=1)
JSON_COMPAT_MAX_BYTES = _read_int_env("INGEST_JSON_COMPAT_MAX_BYTES", 50 * 1024 * 1024, min_value=1024)

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
)

INGEST_OPERATION_EXCEPTIONS = (
    psycopg.Error,
    BotoCoreError,
    ClientError,
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


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def advisory_lock_key(run_id: str) -> int:
    return uuid.UUID(run_id).int % (2**63 - 1)


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
            rec.get("access_level", "no_access"),
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
            INSERT INTO ingest_errors (run_id, severity, code, message, endpoint_key, resource_name, path)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    rows.clear()


def update_run_status(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    line_offset: int,
    summary: dict[str, Any],
    last_error: str | None = None,
):
    ingest_progress = {"line_offset": line_offset}
    if last_error:
        ingest_progress["last_error"] = last_error
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


def _records_from_nested_json(doc: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    endpoints_raw = doc.get("endpoints")
    if not isinstance(endpoints_raw, list):
        return []

    records: list[dict[str, Any]] = []
    for raw_endpoint in endpoints_raw:
        if not isinstance(raw_endpoint, dict):
            continue

        endpoint_key = str(raw_endpoint.get("endpoint_key") or "").strip()
        if not endpoint_key:
            ip = str(raw_endpoint.get("ip") or "").strip()
            hostname = str(raw_endpoint.get("hostname") or "").strip()
            endpoint_key = f"{ip}:445" if ip else (f"{hostname}:445" if hostname else "unknown:445")

        records.append(
            {
                "type": "endpoint",
                "run_id": run_id,
                "endpoint_key": endpoint_key,
                "ip": raw_endpoint.get("ip"),
                "hostname": raw_endpoint.get("hostname"),
                "domain": raw_endpoint.get("domain"),
            }
        )

        raw_shares = raw_endpoint.get("shares")
        if not isinstance(raw_shares, list):
            continue

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


def discover_uploaded_runs(limit: int = 8) -> list[dict[str, str]]:
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            """
            SELECT id::text, project_id::text, artifact_key
            FROM scan_runs
            WHERE status = 'UPLOADED' AND artifact_key IS NOT NULL
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "run_id": row[0],
            "project_id": row[1],
            "artifact_key": row[2],
        }
        for row in rows
    ]


def process_job(fields: dict[str, str]) -> None:
    run_id = _normalize_uuid_str(fields.get("run_id"))
    project_id = _normalize_uuid_str(fields.get("project_id"))
    artifact_key = fields.get("artifact_key")
    last_line_offset = 0
    last_counts = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}

    if not run_id:
        logger.error("invalid job payload missing or invalid run_id: %s", fields)
        return

    with psycopg.connect(DATABASE_URL) as conn:
        lock_key = advisory_lock_key(run_id)
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,)).fetchone()[0]
        if not locked:
            logger.info("run is already being processed run_id=%s", run_id)
            return

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
                return

            db_project_id, db_artifact_key, status, summary_raw, progress_raw, artifact_content_type, artifact_size = row
            project_id = project_id or db_project_id
            artifact_key = artifact_key or db_artifact_key
            if not artifact_key:
                update_run_status(conn, run_id, "FAILED", parse_offset(progress_raw), parse_summary(summary_raw), "missing artifact key")
                conn.commit()
                return
            if status in {"COMPLETE", "FAILED"}:
                return

            counts = parse_summary(summary_raw)
            line_offset = parse_offset(progress_raw)
            last_line_offset = line_offset
            last_counts = counts.copy()

            update_run_status(conn, run_id, "INGESTING", line_offset, counts)
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

            obj = s3.get_object(Bucket=S3_BUCKET, Key=artifact_key)
            body = obj["Body"]
            def process_record(rec: dict[str, Any]) -> None:
                nonlocal counts
                rec = _bind_record_to_ingest_run(rec, run_id)

                valid, reason = validate_record(rec)
                if not valid:
                    error_batch.append(
                        (
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
                        (
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

            raw_bytes: bytes | None = None
            json_records: list[dict[str, Any]] | None = None
            content_type = str(artifact_content_type or "").lower()
            size_value = int(artifact_size or 0)

            if artifact_key.endswith(".gz"):
                reader = gzip.GzipFile(fileobj=body)
            elif "json" in content_type and "ndjson" not in content_type and size_value <= JSON_COMPAT_MAX_BYTES:
                raw_bytes = body.read()
                try:
                    json_doc = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                    json_records = records_from_json_document(json_doc, run_id)
                except (TypeError, ValueError):
                    json_records = None
                    reader = raw_bytes.splitlines()
            else:
                reader = body.iter_lines()

            current_line = 0
            if json_records is not None:
                for rec in json_records:
                    current_line += 1
                    if current_line <= line_offset:
                        continue
                    line_offset = current_line
                    process_record(rec)

                    if line_offset % PROGRESS_EVERY_LINES == 0:
                        flush_item_batch(conn, item_batch)
                        flush_error_batch(conn, error_batch)
                        update_run_status(conn, run_id, "INGESTING", line_offset, counts)
                        conn.commit()
                        last_line_offset = line_offset
                        last_counts = counts.copy()
            else:
                for raw_line in reader:
                    current_line += 1
                    if current_line <= line_offset:
                        continue

                    line_offset = current_line
                    if isinstance(raw_line, bytes):
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    else:
                        line = str(raw_line).strip()
                    if not line:
                        continue

                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as exc:
                        error_batch.append((run_id, "error", "JSON_DECODE_ERROR", str(exc), None, None, None))
                        counts["errors"] += 1
                        if len(error_batch) >= BATCH_SIZE:
                            flush_error_batch(conn, error_batch)
                        continue

                    process_record(rec)
                    if line_offset % PROGRESS_EVERY_LINES == 0:
                        flush_item_batch(conn, item_batch)
                        flush_error_batch(conn, error_batch)
                        update_run_status(conn, run_id, "INGESTING", line_offset, counts)
                        conn.commit()
                        last_line_offset = line_offset
                        last_counts = counts.copy()

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
        except INGEST_OPERATION_EXCEPTIONS as exc:
            logger.exception("job failed run_id=%s", run_id)
            try:
                update_run_status(conn, run_id, "FAILED", last_line_offset, last_counts, last_error=str(exc))
                if project_id:
                    write_audit(
                        conn,
                        project_id,
                        "INGEST_FAILED",
                        "scan_run",
                        run_id,
                        {"worker": CONSUMER_NAME, "error": str(exc)},
                    )
                conn.commit()
            except psycopg.Error:
                logger.exception("failed to persist ingest failure state run_id=%s", run_id)
            raise
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


def ensure_group() -> None:
    try:
        redis_client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def claim_stale_messages() -> list[tuple[str, dict[str, str]]]:
    try:
        result = redis_client.xautoclaim(
            STREAM_NAME,
            GROUP_NAME,
            CONSUMER_NAME,
            min_idle_time=PENDING_IDLE_MS,
            start_id="0-0",
            count=10,
        )
    except redis.RedisError:
        return []

    if not result:
        return []

    # redis-py returns (next_start_id, [(id, fields), ...], [deleted_ids])
    messages = result[1] if len(result) > 1 else []
    return messages or []


def main() -> int:
    ensure_group()
    logger.info("worker started consumer=%s", CONSUMER_NAME)

    last_recovery_scan = 0.0
    last_redis_error_log = 0.0

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
            time.sleep(1)

        if messages:
            for _, jobs in messages:
                for message_id, fields in jobs:
                    try:
                        process_job(fields)
                        redis_client.xack(STREAM_NAME, GROUP_NAME, message_id)
                    except STREAM_MESSAGE_RETRYABLE_EXCEPTIONS:
                        logger.exception(
                            "failed processing stream message message_id=%s run_id=%s",
                            message_id,
                            _safe_run_id(fields),
                        )
                        time.sleep(1)

        stale_jobs = claim_stale_messages()
        for message_id, fields in stale_jobs:
            try:
                process_job(fields)
                redis_client.xack(STREAM_NAME, GROUP_NAME, message_id)
            except STREAM_MESSAGE_RETRYABLE_EXCEPTIONS:
                logger.exception(
                    "failed processing claimed stream message message_id=%s run_id=%s",
                    message_id,
                    _safe_run_id(fields),
                )
                time.sleep(1)

        if time.time() - last_recovery_scan >= RECOVERY_SCAN_SECONDS:
            for recovered in discover_uploaded_runs(limit=5):
                try:
                    process_job(recovered)
                except STREAM_MESSAGE_RETRYABLE_EXCEPTIONS:
                    logger.exception("failed processing recovered uploaded run run_id=%s", _safe_run_id(recovered))
                    time.sleep(1)
            last_recovery_scan = time.time()


if __name__ == "__main__":
    sys.exit(main())
