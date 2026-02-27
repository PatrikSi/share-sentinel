import gzip
import json
import logging
import os
import socket
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
import psycopg
import redis

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("share_sentinel.worker")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://smbguard:smbguard@db:5432/smbguard").replace(
    "postgresql+psycopg://", "postgresql://"
)
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "smbguard-artifacts")

STREAM_NAME = "ingest_jobs"
GROUP_NAME = "ingest_workers"
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"


redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
)


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


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
            ip = EXCLUDED.ip,
            hostname = EXCLUDED.hostname,
            domain = EXCLUDED.domain,
            smb_dialect = EXCLUDED.smb_dialect,
            smb_signing = EXCLUDED.smb_signing,
            auth_method = EXCLUDED.auth_method
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
            remark = EXCLUDED.remark,
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


def insert_item(conn: psycopg.Connection, run_id: str, resource_id: int, rec: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO items (run_id, resource_id, path, name, is_dir)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (run_id, resource_id, path) DO NOTHING
        """,
        (
            run_id,
            resource_id,
            rec.get("path", ""),
            rec.get("name", ""),
            bool(rec.get("is_dir", False)),
        ),
    )


def insert_error(conn: psycopg.Connection, run_id: str, rec: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO ingest_errors (run_id, severity, code, message, endpoint_key, resource_name, path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            rec.get("severity", "error"),
            rec.get("code", "UNKNOWN"),
            rec.get("message", ""),
            rec.get("endpoint_key"),
            rec.get("resource_name"),
            rec.get("path"),
        ),
    )


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


def process_job(fields: dict[str, str]) -> None:
    run_id = fields.get("run_id")
    project_id = fields.get("project_id")
    artifact_key = fields.get("artifact_key")

    if not run_id or not project_id or not artifact_key:
        logger.error("invalid job payload: %s", fields)
        return

    counts = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}
    line_offset = 0
    endpoint_cache: dict[str, int] = {}
    resource_cache: dict[tuple[str, str], int] = {}

    with psycopg.connect(DATABASE_URL) as conn:
        try:
            update_run_status(conn, run_id, "INGESTING", line_offset, counts)
            write_audit(
                conn,
                project_id,
                "INGEST_STARTED",
                "scan_run",
                run_id,
                {"worker": CONSUMER_NAME, "ts": now_iso()},
            )
            conn.commit()

            obj = s3.get_object(Bucket=S3_BUCKET, Key=artifact_key)
            body = obj["Body"]
            reader = gzip.GzipFile(fileobj=body) if artifact_key.endswith(".gz") else body.iter_lines()

            for raw_line in reader:
                line_offset += 1
                if isinstance(raw_line, bytes):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                else:
                    line = str(raw_line).strip()
                if not line:
                    continue

                rec = json.loads(line)
                rec_type = rec.get("type")
                rec_run_id = rec.get("run_id")
                if rec_run_id and rec_run_id != run_id:
                    continue

                if rec_type == "endpoint":
                    endpoint_id = upsert_endpoint(conn, run_id, rec)
                    endpoint_cache[rec.get("endpoint_key", "")] = endpoint_id
                    counts["endpoints"] += 1
                elif rec_type == "resource":
                    endpoint_key = rec.get("endpoint_key", "")
                    endpoint_id = endpoint_cache.get(endpoint_key)
                    if endpoint_id is None:
                        endpoint_id = upsert_endpoint(conn, run_id, {"endpoint_key": endpoint_key})
                        endpoint_cache[endpoint_key] = endpoint_id
                    resource_id = upsert_resource(conn, run_id, endpoint_id, rec)
                    resource_cache[(endpoint_key, rec.get("name", ""))] = resource_id
                    counts["resources"] += 1
                elif rec_type == "item":
                    endpoint_key = rec.get("endpoint_key", "")
                    resource_name = rec.get("resource_name", "")
                    key = (endpoint_key, resource_name)
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
                                "resource_type": rec.get("resource_type", "smb_share"),
                                "name": resource_name,
                                "remark": None,
                                "access_level": "no_access",
                            },
                        )
                        resource_cache[key] = resource_id
                    insert_item(conn, run_id, resource_id, rec)
                    counts["items"] += 1
                elif rec_type == "error":
                    insert_error(conn, run_id, rec)
                    counts["errors"] += 1
                elif rec_type == "run_end":
                    incoming = rec.get("stats") or {}
                    counts = {
                        "endpoints": int(incoming.get("endpoints", counts["endpoints"])),
                        "resources": int(incoming.get("resources", counts["resources"])),
                        "items": int(incoming.get("items", counts["items"])),
                        "errors": int(incoming.get("errors", counts["errors"])),
                    }

                if line_offset % 2000 == 0:
                    update_run_status(conn, run_id, "INGESTING", line_offset, counts)
                    conn.commit()

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
        except Exception as exc:  # noqa: BLE001
            logger.exception("job failed run_id=%s", run_id)
            update_run_status(conn, run_id, "FAILED", line_offset, counts, last_error=str(exc))
            write_audit(
                conn,
                project_id,
                "INGEST_FAILED",
                "scan_run",
                run_id,
                {"worker": CONSUMER_NAME, "error": str(exc), "line_offset": line_offset},
            )
            conn.commit()
            raise


def ensure_group() -> None:
    try:
        redis_client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def main() -> int:
    ensure_group()
    logger.info("worker started consumer=%s", CONSUMER_NAME)

    while True:
        messages = redis_client.xreadgroup(
            GROUP_NAME,
            CONSUMER_NAME,
            {STREAM_NAME: ">"},
            count=1,
            block=5000,
        )
        if not messages:
            continue

        for _, jobs in messages:
            for message_id, fields in jobs:
                try:
                    process_job(fields)
                    redis_client.xack(STREAM_NAME, GROUP_NAME, message_id)
                except Exception:
                    # Leave in pending entries for retry / manual recovery.
                    time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
