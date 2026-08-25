import gzip
import hashlib
import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from urllib.parse import urlsplit

import ijson
import psycopg
import redis

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("share_sentinel.worker")


def _read_int_env(
    name: str,
    default: int,
    min_value: int = 1,
    max_value: int | None = None,
    *,
    strict: bool = False,
) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"{name} must be an integer; got {raw!r}") from None
        logger.warning("invalid integer value for %s=%r; using default=%s", name, raw, default)
        return default

    if value < min_value:
        if strict:
            raise ValueError(f"{name} must be at least {min_value}; got {value}")
        logger.warning("value for %s=%s is below min=%s; using min", name, value, min_value)
        return min_value
    if max_value is not None and value > max_value:
        if strict:
            raise ValueError(f"{name} must be at most {max_value}; got {value}")
        logger.warning("value for %s=%s exceeds max=%s; using max", name, value, max_value)
        return max_value
    return value


def _read_float_env(
    name: str,
    default: float,
    min_value: float = 0.1,
    max_value: float | None = None,
    *,
    strict: bool = False,
) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"{name} must be numeric; got {raw!r}") from None
        logger.warning("invalid numeric value for %s=%r; using default=%s", name, raw, default)
        return default

    if not math.isfinite(value):
        if strict:
            raise ValueError(f"{name} must be finite; got {raw!r}")
        logger.warning("non-finite numeric value for %s=%r; using default=%s", name, raw, default)
        return default
    if value < min_value:
        if strict:
            raise ValueError(f"{name} must be at least {min_value}; got {value}")
        logger.warning("value for %s=%s is below min=%s; using min", name, value, min_value)
        return min_value
    if max_value is not None and value > max_value:
        if strict:
            raise ValueError(f"{name} must be at most {max_value}; got {value}")
        logger.warning("value for %s=%s exceeds max=%s; using max", name, value, max_value)
        return max_value
    return value


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://share_sentinel:share_sentinel@db:5432/share_sentinel"
).replace("postgresql+psycopg://", "postgresql://")
ARTIFACT_STORAGE_PATH = os.getenv("ARTIFACT_STORAGE_PATH", "/artifacts")

STREAM_NAME = "ingest_jobs"
GROUP_NAME = "ingest_workers"
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"

MAX_INGEST_BATCH_SIZE = 10_000
MAX_INGEST_RECORD_BYTES = 16 * 1024 * 1024
MAX_INGEST_JSON_COMPAT_BYTES = 128 * 1024 * 1024
MAX_INGEST_GZIP_DECOMPRESSED_BYTES = 100 * 1024 * 1024 * 1024
MAX_INGEST_GZIP_EXPANSION_RATIO = 1000
MAX_INGEST_IDENTITY_CACHE_SIZE = 100_000
MAX_INGEST_RETRIES = 100

BATCH_SIZE = _read_int_env(
    "INGEST_BATCH_SIZE",
    5000,
    min_value=1,
    max_value=MAX_INGEST_BATCH_SIZE,
    strict=True,
)
PROGRESS_EVERY_LINES = _read_int_env("INGEST_PROGRESS_EVERY_LINES", 2000, min_value=1)
RECOVERY_SCAN_SECONDS = _read_int_env("INGEST_RECOVERY_SCAN_SECONDS", 8, min_value=1)
RECOVERY_SCAN_LIMIT = _read_int_env("INGEST_RECOVERY_SCAN_LIMIT", 8, min_value=1)
PENDING_IDLE_MS = _read_int_env("INGEST_PENDING_IDLE_MS", 60000, min_value=1)
JSON_COMPAT_MAX_BYTES = _read_int_env(
    "INGEST_JSON_COMPAT_MAX_BYTES",
    50 * 1024 * 1024,
    min_value=1024,
    max_value=MAX_INGEST_JSON_COMPAT_BYTES,
    strict=True,
)
INGEST_MAX_RECORD_BYTES = _read_int_env(
    "INGEST_MAX_RECORD_BYTES",
    8 * 1024 * 1024,
    min_value=1024,
    max_value=MAX_INGEST_RECORD_BYTES,
    strict=True,
)
GZIP_DECOMPRESSED_MAX_BYTES = _read_int_env(
    "INGEST_GZIP_MAX_BYTES",
    10 * 1024 * 1024 * 1024,
    min_value=1024,
    max_value=MAX_INGEST_GZIP_DECOMPRESSED_BYTES,
    strict=True,
)
GZIP_DECOMPRESSED_MAX_RATIO = _read_int_env(
    "INGEST_GZIP_MAX_EXPANSION_RATIO",
    200,
    min_value=1,
    max_value=MAX_INGEST_GZIP_EXPANSION_RATIO,
    strict=True,
)
STALE_INGESTING_SECONDS = _read_int_env("INGEST_STALE_RUN_SECONDS", 300, min_value=30)
INGEST_MAX_RETRIES = _read_int_env(
    "INGEST_MAX_RETRIES",
    4,
    min_value=0,
    max_value=MAX_INGEST_RETRIES,
    strict=True,
)
INGEST_RETRY_BASE_SECONDS = _read_int_env("INGEST_RETRY_BASE_SECONDS", 30, min_value=1)
INGEST_RETRY_MAX_SECONDS = _read_int_env("INGEST_RETRY_MAX_SECONDS", 900, min_value=1)
INGEST_RETRY_JITTER_RATIO = _read_float_env(
    "INGEST_RETRY_JITTER_RATIO",
    0.2,
    min_value=0.0,
    max_value=1.0,
    strict=True,
)
INGEST_IDENTITY_CACHE_SIZE = _read_int_env(
    "INGEST_IDENTITY_CACHE_SIZE",
    10000,
    min_value=1,
    max_value=MAX_INGEST_IDENTITY_CACHE_SIZE,
    strict=True,
)
WORKER_HEARTBEAT_PATH = os.getenv("WORKER_HEARTBEAT_PATH", "/tmp/share-sentinel-worker-heartbeat.json")
WORKER_HEARTBEAT_INTERVAL_SECONDS = _read_int_env("WORKER_HEARTBEAT_INTERVAL_SECONDS", 15, min_value=1)
WORKER_HEALTH_TIMEOUT_SECONDS = _read_int_env("WORKER_HEALTH_TIMEOUT_SECONDS", 45, min_value=5)
REDIS_CONNECT_TIMEOUT_SECONDS = _read_float_env("REDIS_CONNECT_TIMEOUT_SECONDS", 3.0)
REDIS_SOCKET_TIMEOUT_SECONDS = _read_float_env("REDIS_SOCKET_TIMEOUT_SECONDS", 5.0)
WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS = _read_int_env(
    "WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS",
    5,
    min_value=1,
)
WORKER_DATABASE_STATEMENT_TIMEOUT_MS = _read_int_env(
    "WORKER_DATABASE_STATEMENT_TIMEOUT_MS",
    120000,
    min_value=1000,
)
WORKER_DATABASE_LOCK_TIMEOUT_MS = _read_int_env(
    "WORKER_DATABASE_LOCK_TIMEOUT_MS",
    15000,
    min_value=100,
)

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
)

SHARE_TYPE_TO_RESOURCE_TYPE = {
    "smb": "smb_share",
    "nfs": "nfs_share",
    "sharepoint": "sharepoint_library",
}
RESOURCE_TYPE_TO_SHARE_TYPE = {value: key for key, value in SHARE_TYPE_TO_RESOURCE_TYPE.items()}
ACCESS_LEVEL_ALIASES = {
    "unknown": "unknown",
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
    "write": "unknown",
    "writable": "unknown",
    "modify": "unknown",
    "full": "readable",
    "full_control": "readable",
    "rw": "readable",
}
ACCESS_LEVEL_RANK = {
    "unknown": 0,
    "no_access": 1,
    "list_only": 2,
    "readable": 3,
}
ACCESS_CAPABILITY_STATUSES = {"allowed", "denied", "mixed", "not_tested", "inconclusive"}
ACCESS_CAPABILITY_NAMES = (
    "tree_connect",
    "list",
    "read_file",
    "create_file",
    "create_directory",
    "modify_file",
    "delete",
    "write_acl",
    "write_owner",
)
ACCESS_CAPABILITY_MAX_KEYS = 32
ACCESS_CAPABILITY_MAX_KEY_LENGTH = 64
ACCESS_CAPABILITY_MAX_COUNT = 2**31 - 1
_ACCESS_CAPABILITY_OUTCOME_BASE_LIMIT = ACCESS_CAPABILITY_MAX_COUNT // 3
ACCESS_CAPABILITY_OUTCOME_LIMITS = (
    _ACCESS_CAPABILITY_OUTCOME_BASE_LIMIT + (ACCESS_CAPABILITY_MAX_COUNT % 3),
    _ACCESS_CAPABILITY_OUTCOME_BASE_LIMIT,
    _ACCESS_CAPABILITY_OUTCOME_BASE_LIMIT,
)
ACCESS_CAPABILITY_EVIDENCE_FIELDS = {
    "method",
    "scope",
    "coverage",
    "reason_code",
    "protocol_status",
    "not_tested_reason",
}
ACCESS_CAPABILITY_MAX_EVIDENCE_LENGTH = 256
ACCESS_CAPABILITY_METADATA_TEXT_FIELDS = {
    "probe_method",
    "coverage",
    "assessment_summary",
    "assessment_reason",
    "share_presence",
}
ACCESS_CAPABILITY_METADATA_COUNT_FIELDS = {
    "probe_limit",
    "directory_samples",
    "file_samples",
    "directory_candidates_seen",
    "file_candidates_seen",
}
ACCESS_CAPABILITY_METADATA_BOOLEAN_FIELDS = {
    "partial",
    "complete",
    "listing_truncated",
    "finalized",
    "degraded",
    "transport_failed",
}
FILE_ATTRIBUTE_MAX_VALUES = 32
FILE_ATTRIBUTE_MAX_LENGTH = 64
GZIP_DECOMPRESSED_LIMIT_ERROR = "gzip artifact exceeds decompressed size limit"
NDJSON_RECORD_TOO_LARGE_ERROR = "NDJSON record exceeds configured size limit"
INVALID_GZIP_ARTIFACT_ERROR = "invalid gzip artifact"
JSON_COMPAT_LIMIT_ERROR = "JSON artifact exceeds non-streamable compatibility limit"
INVALID_UTF8_ARTIFACT_ERROR = "artifact contains invalid UTF-8"
ARTIFACT_FRAMING_ERROR = (
    "artifact must contain exactly one run_meta as its first record and exactly one run_end as its last record"
)
ENDPOINT_KEY_MAX_LENGTH = 255
RESOURCE_NAME_MAX_LENGTH = 255
ITEM_NAME_MAX_LENGTH = 255
# items.path participates in a PostgreSQL btree uniqueness constraint. Keep
# enough headroom for the other indexed columns and multibyte text overhead.
ITEM_PATH_MAX_BYTES = 2000
PROVIDER_ITEM_PATH_MAX_BYTES = 2000
SHAREPOINT_ITEM_PATH_MAX_CHARACTERS = 400
INGEST_ERROR_CODE_MAX_LENGTH = 128
INGEST_ERROR_MESSAGE_MAX_LENGTH = 4096
INGEST_ERROR_PATH_MAX_LENGTH = 4096
ERROR_SEVERITIES = {"warn", "error"}
PROVIDER_MAX_LENGTH = 32
PROVIDER_ID_MAX_LENGTH = 512
PROVIDER_URL_MAX_BYTES = 8192
PROVIDER_METADATA_MAX_BYTES = 64 * 1024
PROVIDER_METADATA_MAX_DEPTH = 6
PROVIDER_METADATA_MAX_ENTRIES = 512
PROVIDER_METADATA_MAX_LIST_ITEMS = 128
PROVIDER_METADATA_MAX_KEY_LENGTH = 128
PROVIDER_METADATA_MAX_TEXT_LENGTH = 4096
MIME_TYPE_MAX_LENGTH = 255
EXPOSURE_CLASSIFICATIONS = {
    "USER_VISIBLE",
    "BROAD_INTERNAL",
    "EXTERNAL",
    "ANONYMOUS",
    "RESTRICTED",
    "UNKNOWN",
}
FORBIDDEN_PROVIDER_METADATA_KEYS = {
    "access_token",
    "authorization",
    "bearer_token",
    "client_secret",
    "delta_link",
    "password",
    "private_key",
    "refresh_token",
    "token",
    "token_value",
}
FORBIDDEN_PROVIDER_METADATA_KEY_FINGERPRINTS = {
    "accesstoken",
    "authorization",
    "authorizationheader",
    "bearertoken",
    "clientsecret",
    "clientsecretvalue",
    "deltalink",
    "password",
    "privatekey",
    "refreshtoken",
    "token",
    "tokenvalue",
}
AUTH_CONTEXT_TEXT_FIELDS = {
    "auth_mode",
    "auth_type",
    "tenant_id",
    "tenant_name",
    "user_id",
    "user_principal_name",
    "client_id",
    "token_expiration",
    "jwt_inspection",
}
COLLECTION_CONTEXT_TEXT_FIELDS = {
    "source",
    "provider",
    "collection_mode",
    "assessed_identity",
    "status",
    "discovery_completeness",
    "sync_mode",
}

_CacheKey = TypeVar("_CacheKey")


class _BoundedLRUCache(OrderedDict[_CacheKey, int]):
    """Small identity map that cannot grow with the full artifact cardinality."""

    def __init__(self, max_size: int):
        super().__init__()
        self.max_size = max(1, int(max_size))

    def __setitem__(self, key: _CacheKey, value: int) -> None:
        if key in self:
            super().move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.max_size:
            self.popitem(last=False)

    def get(self, key: _CacheKey, default=None):
        value = super().get(key, default)
        if key in self:
            super().move_to_end(key)
        return value


class _GracefulWorkerShutdown(Exception):
    """Internal control flow used after a durable shutdown checkpoint."""


_shutdown_event = threading.Event()


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


def _handle_shutdown_signal(signum: int, _frame: Any) -> None:
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    if not _shutdown_event.is_set():
        logger.info("shutdown requested signal=%s; checkpointing active work", signal_name)
    _shutdown_event.set()


def _install_shutdown_signal_handlers() -> dict[int, Any]:
    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.signal(signum, _handle_shutdown_signal)
        except ValueError:
            logger.warning("unable to install shutdown signal handler signal=%s", signum)
    return previous_handlers


def _restore_shutdown_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for signum, previous_handler in previous_handlers.items():
        try:
            signal.signal(signum, previous_handler)
        except ValueError:
            logger.warning("unable to restore shutdown signal handler signal=%s", signum)


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


def write_audit(
    conn: psycopg.Connection, project_id: str, action: str, object_type: str, object_id: str, metadata: dict[str, Any]
):
    conn.execute(
        """
        INSERT INTO audit_events (project_id, action, object_type, object_id, metadata)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (project_id, action, object_type, object_id, json.dumps(metadata)),
    )


def update_run_collection_context(conn: psycopg.Connection, run_id: str, context: dict[str, Any]) -> None:
    if not context:
        return
    conn.execute(
        """
        UPDATE scan_runs
        SET collection_context = COALESCE(collection_context, '{}'::jsonb) || %s::jsonb
        WHERE id = %s
        """,
        (json.dumps(context), run_id),
    )


def upsert_endpoint(conn: psycopg.Connection, run_id: str, rec: dict[str, Any]) -> int:
    smb = rec.get("smb") if isinstance(rec.get("smb"), dict) else {}
    auth = rec.get("auth") if isinstance(rec.get("auth"), dict) else {}
    row = conn.execute(
        """
        INSERT INTO endpoints (
            run_id, endpoint_key, ip, hostname, domain, smb_dialect, smb_signing,
            auth_method, provider, provider_metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, endpoint_key)
        DO UPDATE SET
            ip = COALESCE(EXCLUDED.ip, endpoints.ip),
            hostname = COALESCE(EXCLUDED.hostname, endpoints.hostname),
            domain = COALESCE(EXCLUDED.domain, endpoints.domain),
            smb_dialect = COALESCE(EXCLUDED.smb_dialect, endpoints.smb_dialect),
            smb_signing = COALESCE(EXCLUDED.smb_signing, endpoints.smb_signing),
            auth_method = COALESCE(EXCLUDED.auth_method, endpoints.auth_method),
            provider = COALESCE(EXCLUDED.provider, endpoints.provider),
            provider_metadata = endpoints.provider_metadata || EXCLUDED.provider_metadata
        RETURNING id
        """,
        (
            run_id,
            rec.get("endpoint_key"),
            rec.get("ip"),
            rec.get("hostname"),
            rec.get("domain"),
            smb.get("dialect"),
            _normalize_smb_signing(smb),
            auth.get("method"),
            rec.get("provider"),
            json.dumps(rec.get("provider_metadata") or {}),
        ),
    ).fetchone()
    return int(row[0])


def upsert_resource(conn: psycopg.Connection, run_id: str, endpoint_id: int, rec: dict[str, Any]) -> int:
    resource_type = rec.get("resource_type", "smb_share")
    resource_name = rec.get("name")
    provider_resource_id = rec.get("provider_resource_id")
    incoming_capabilities = _normalize_access_capabilities(rec.get("access_capabilities"))
    incoming_access = _reconcile_access_level_with_capabilities(
        _normalize_access_level(rec.get("access_level")),
        incoming_capabilities,
    )

    if provider_resource_id:
        existing = conn.execute(
            """
            SELECT id, access_level::text, access_capabilities, provider_metadata,
                   exposure, exposure_evidence
            FROM resources
            WHERE run_id = %s
              AND endpoint_id = %s
              AND resource_type = %s
              AND provider_resource_id = %s
            FOR UPDATE
            """,
            (run_id, endpoint_id, resource_type, provider_resource_id),
        ).fetchone()
        if existing is None:
            # Upgrade an earlier legacy/out-of-order placeholder instead of
            # creating a duplicate for the same named resource.
            existing = conn.execute(
                """
                SELECT id, access_level::text, access_capabilities, provider_metadata,
                       exposure, exposure_evidence
                FROM resources
                WHERE run_id = %s
                  AND endpoint_id = %s
                  AND resource_type = %s
                  AND provider_resource_id IS NULL
                  AND name = %s
                FOR UPDATE
                """,
                (run_id, endpoint_id, resource_type, resource_name),
            ).fetchone()
    else:
        existing = conn.execute(
            """
            SELECT id, access_level::text, access_capabilities, provider_metadata,
                   exposure, exposure_evidence
            FROM resources
            WHERE run_id = %s
              AND endpoint_id = %s
              AND resource_type = %s
              AND provider_resource_id IS NULL
              AND name = %s
            FOR UPDATE
            """,
            (run_id, endpoint_id, resource_type, resource_name),
        ).fetchone()

    if existing is None:
        row = conn.execute(
            """
            INSERT INTO resources (
                run_id, endpoint_id, resource_type, name, remark, access_level,
                access_capabilities, provider, provider_resource_id, web_url,
                provider_metadata, exposure, exposure_evidence
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                run_id,
                endpoint_id,
                resource_type,
                resource_name,
                rec.get("remark"),
                incoming_access,
                json.dumps(incoming_capabilities),
                rec.get("provider"),
                provider_resource_id,
                rec.get("web_url"),
                json.dumps(rec.get("provider_metadata") or {}),
                rec.get("exposure"),
                json.dumps(rec.get("exposure_evidence") or {}),
            ),
        ).fetchone()
    else:
        existing_id, existing_access, existing_capabilities = existing[:3]
        existing_metadata = existing[3] if len(existing) > 3 and isinstance(existing[3], dict) else {}
        existing_exposure = existing[4] if len(existing) > 4 else None
        existing_exposure_evidence = existing[5] if len(existing) > 5 and isinstance(existing[5], dict) else {}
        merged_capabilities = _merge_access_capabilities(existing_capabilities, incoming_capabilities)
        merged_access = _reconcile_access_level_with_capabilities(
            _stronger_access_level(existing_access, incoming_access),
            merged_capabilities,
        )
        incoming_exposure = rec.get("exposure")
        merged_exposure = (
            existing_exposure
            if incoming_exposure in {None, "UNKNOWN"} and existing_exposure not in {None, "UNKNOWN"}
            else incoming_exposure or existing_exposure
        )
        merged_metadata = {**existing_metadata, **(rec.get("provider_metadata") or {})}
        merged_exposure_evidence = {
            **existing_exposure_evidence,
            **(rec.get("exposure_evidence") or {}),
        }
        row = conn.execute(
            """
            UPDATE resources
            SET name = %s,
                remark = COALESCE(%s, remark),
                access_level = %s,
                access_capabilities = %s,
                provider = COALESCE(%s, provider),
                provider_resource_id = COALESCE(%s, provider_resource_id),
                web_url = COALESCE(%s, web_url),
                provider_metadata = %s,
                exposure = %s,
                exposure_evidence = %s
            WHERE id = %s
            RETURNING id
            """,
            (
                resource_name,
                rec.get("remark"),
                merged_access,
                json.dumps(merged_capabilities),
                rec.get("provider"),
                provider_resource_id,
                rec.get("web_url"),
                json.dumps(merged_metadata),
                merged_exposure,
                json.dumps(merged_exposure_evidence),
                existing_id,
            ),
        ).fetchone()
    return int(row[0])


def flush_item_batch(conn: psycopg.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    padded_rows = [
        row + (None, None, None, None, None, False, "{}", None, "{}") if len(row) == 12 else row for row in rows
    ]
    provider_rows = [row for row in padded_rows if row[13] is not None]
    legacy_rows = [row for row in padded_rows if row[13] is None]

    insert_sql = """
        INSERT INTO items (
            run_id, resource_id, path, name, is_dir, size_bytes, allocation_size_bytes,
            mtime, created_at, accessed_at, changed_at, file_attributes, provider,
            provider_item_id, provider_parent_id, web_url, mime_type, deleted,
            provider_metadata, exposure, exposure_evidence
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """
    common_updates = """
            is_dir = CASE WHEN EXCLUDED.deleted THEN items.is_dir ELSE EXCLUDED.is_dir END,
            size_bytes = COALESCE(EXCLUDED.size_bytes, items.size_bytes),
            allocation_size_bytes = COALESCE(EXCLUDED.allocation_size_bytes, items.allocation_size_bytes),
            mtime = COALESCE(EXCLUDED.mtime, items.mtime),
            created_at = COALESCE(EXCLUDED.created_at, items.created_at),
            accessed_at = COALESCE(EXCLUDED.accessed_at, items.accessed_at),
            changed_at = COALESCE(EXCLUDED.changed_at, items.changed_at),
            file_attributes = CASE
                WHEN EXCLUDED.file_attributes = '[]'::jsonb THEN items.file_attributes
                ELSE EXCLUDED.file_attributes
            END,
            provider = COALESCE(EXCLUDED.provider, items.provider),
            provider_parent_id = COALESCE(EXCLUDED.provider_parent_id, items.provider_parent_id),
            web_url = COALESCE(EXCLUDED.web_url, items.web_url),
            mime_type = COALESCE(EXCLUDED.mime_type, items.mime_type),
            deleted = EXCLUDED.deleted,
            provider_metadata = items.provider_metadata || EXCLUDED.provider_metadata,
            exposure = CASE
                WHEN EXCLUDED.exposure IS NULL OR EXCLUDED.exposure = 'UNKNOWN'
                THEN items.exposure
                ELSE EXCLUDED.exposure
            END,
            exposure_evidence = items.exposure_evidence || EXCLUDED.exposure_evidence
    """
    with conn.cursor() as cur:
        if legacy_rows:
            cur.executemany(
                insert_sql
                + """
                ON CONFLICT (run_id, resource_id, path) WHERE provider_item_id IS NULL
                DO UPDATE SET name = EXCLUDED.name,
                """
                + common_updates,
                legacy_rows,
            )
        if provider_rows:
            cur.executemany(
                insert_sql
                + """
                ON CONFLICT (run_id, resource_id, provider_item_id) WHERE provider_item_id IS NOT NULL
                DO UPDATE SET
                    path = CASE WHEN EXCLUDED.deleted THEN items.path ELSE EXCLUDED.path END,
                    name = CASE WHEN EXCLUDED.deleted THEN items.name ELSE EXCLUDED.name END,
                """
                + common_updates,
                provider_rows,
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


def _resource_cache_key(
    endpoint_key: Any,
    resource_name: Any,
    resource_type: Any,
    provider_resource_id: Any = None,
) -> tuple[str, str, str]:
    identity = (
        f"provider:{provider_resource_id}"
        if isinstance(provider_resource_id, str) and provider_resource_id
        else str(resource_name or "")
    )
    return (
        str(endpoint_key or ""),
        identity,
        str(resource_type or "smb_share"),
    )


def load_resume_caches(
    conn: psycopg.Connection,
    run_id: str,
) -> tuple[_BoundedLRUCache[str], _BoundedLRUCache[tuple[str, str, str]]]:
    endpoint_cache = _BoundedLRUCache[str](INGEST_IDENTITY_CACHE_SIZE)
    resource_cache = _BoundedLRUCache[tuple[str, str, str]](INGEST_IDENTITY_CACHE_SIZE)
    rows = conn.execute(
        """
        SELECT e.id, e.endpoint_key, r.id, r.resource_type, r.name, r.provider_resource_id
        FROM endpoints AS e
        LEFT JOIN resources AS r
          ON r.run_id = e.run_id
         AND r.endpoint_id = e.id
        WHERE e.run_id = %s
        ORDER BY COALESCE(r.id, 0) DESC, e.id DESC
        LIMIT %s
        """,
        (run_id, INGEST_IDENTITY_CACHE_SIZE),
    ).fetchall()
    for row in rows:
        endpoint_id, endpoint_key, resource_id, resource_type, resource_name = row[:5]
        provider_resource_id = row[5] if len(row) > 5 else None
        normalized_endpoint_key = str(endpoint_key or "")
        endpoint_cache[normalized_endpoint_key] = int(endpoint_id)
        if resource_id is not None:
            resource_cache[
                _resource_cache_key(
                    normalized_endpoint_key,
                    resource_name,
                    resource_type,
                    provider_resource_id,
                )
            ] = int(resource_id)
    return endpoint_cache, resource_cache


def load_persisted_summary(conn: psycopg.Connection, run_id: str) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM endpoints WHERE run_id = %s),
            (SELECT COUNT(*) FROM resources WHERE run_id = %s),
            (SELECT COUNT(*) FROM items WHERE run_id = %s),
            (SELECT COUNT(*) FROM ingest_errors WHERE run_id = %s)
        """,
        (run_id, run_id, run_id, run_id),
    ).fetchone()
    if row is None:
        return {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}
    return {
        "endpoints": int(row[0]),
        "resources": int(row[1]),
        "items": int(row[2]),
        "errors": int(row[3]),
    }


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


def _truncate_text(value: Any, max_length: int) -> str:
    text = str(value or "").replace("\x00", "�")
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 1)] + "…"


def _truncate_optional_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    text = _truncate_text(value, max_length).strip()
    return text or None


def build_ingest_error_row(
    run_id: str,
    severity: str,
    code: str,
    message: str,
    endpoint_key: str | None,
    resource_name: str | None,
    path: str | None,
) -> tuple[str, str, str, str, str | None, str | None, str | None, str]:
    normalized_severity = str(severity or "error").strip().lower()
    if normalized_severity not in ERROR_SEVERITIES:
        normalized_severity = "error"
    normalized_code = _truncate_text(code or "UNKNOWN", INGEST_ERROR_CODE_MAX_LENGTH).strip() or "UNKNOWN"
    normalized_message = _truncate_text(message, INGEST_ERROR_MESSAGE_MAX_LENGTH)
    normalized_endpoint_key = _truncate_optional_text(endpoint_key, ENDPOINT_KEY_MAX_LENGTH)
    normalized_resource_name = _truncate_optional_text(resource_name, RESOURCE_NAME_MAX_LENGTH)
    normalized_path = _truncate_optional_text(path, INGEST_ERROR_PATH_MAX_LENGTH)
    return (
        run_id,
        normalized_severity,
        normalized_code,
        normalized_message,
        normalized_endpoint_key,
        normalized_resource_name,
        normalized_path,
        _ingest_error_fingerprint(
            normalized_severity,
            normalized_code,
            normalized_message,
            normalized_endpoint_key,
            normalized_resource_name,
            normalized_path,
        ),
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


def clear_persisted_ingest_inventory(conn: psycopg.Connection, run_id: str) -> None:
    """Remove any resumable partial inventory after terminal framing rejection."""

    conn.execute("DELETE FROM items WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM resources WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM endpoints WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM ingest_errors WHERE run_id = %s", (run_id,))
    conn.execute(
        "UPDATE scan_runs SET collection_context = '{}'::jsonb WHERE id = %s",
        (run_id,),
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


def parse_next_retry_at(raw: Any) -> datetime | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("next_retry_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_smb_signing(raw_smb: Any) -> str | None:
    if not isinstance(raw_smb, dict):
        return None
    signing = raw_smb.get("signing")
    if signing is not None:
        normalized = str(signing).strip()
        return normalized or None
    signing_required = raw_smb.get("signing_required")
    if isinstance(signing_required, bool):
        return "required" if signing_required else "not_required"
    return None


def _normalize_share_type(raw_share_type: Any, raw_resource_type: Any = None) -> str:
    if isinstance(raw_share_type, str):
        normalized = raw_share_type.strip().lower()
        if normalized in SHARE_TYPE_TO_RESOURCE_TYPE:
            return normalized
        if normalized:
            raise ValueError(f"unsupported share_type: {raw_share_type}")

    if isinstance(raw_resource_type, str):
        normalized_resource_type = raw_resource_type.strip().lower()
        share_type = RESOURCE_TYPE_TO_SHARE_TYPE.get(normalized_resource_type)
        if share_type:
            return share_type
        if normalized_resource_type:
            raise ValueError(f"unsupported resource_type: {raw_resource_type}")

    # Artifacts produced before share_type existed represented SMB only.
    return "smb"


def _resource_type_from_share_type(share_type: str) -> str:
    try:
        return SHARE_TYPE_TO_RESOURCE_TYPE[share_type]
    except KeyError as exc:
        raise ValueError(f"unsupported share_type: {share_type}") from exc


def _normalize_access_level(raw_access_level: Any) -> str:
    if isinstance(raw_access_level, str):
        normalized = raw_access_level.strip().lower().replace(" ", "_")
        if normalized in ACCESS_LEVEL_ALIASES:
            return ACCESS_LEVEL_ALIASES[normalized]
    return "unknown"


def _stronger_access_level(current: Any, incoming: Any) -> str:
    current_level = _normalize_access_level(current)
    incoming_level = _normalize_access_level(incoming)
    if ACCESS_LEVEL_RANK[incoming_level] > ACCESS_LEVEL_RANK[current_level]:
        return incoming_level
    return current_level


def _normalize_capability_count(raw_count: Any) -> int:
    if raw_count is None or isinstance(raw_count, bool):
        return 0
    try:
        count = int(raw_count)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(ACCESS_CAPABILITY_MAX_COUNT, max(0, count))


def _normalize_capability_status(raw_status: Any) -> str | None:
    if not isinstance(raw_status, str):
        return None
    status = raw_status.strip().lower().replace("-", "_").replace(" ", "_")
    return status if status in ACCESS_CAPABILITY_STATUSES else None


def _normalize_capability_outcome_counts(
    raw_allowed: Any,
    raw_denied: Any,
    raw_inconclusive: Any,
) -> tuple[int, int, int]:
    """Use fixed class budgets so max-based replay merges remain an idempotent join."""

    raw_counts = (raw_allowed, raw_denied, raw_inconclusive)
    return tuple(
        min(_normalize_capability_count(raw_count), limit)
        for raw_count, limit in zip(raw_counts, ACCESS_CAPABILITY_OUTCOME_LIMITS, strict=True)
    )


def _normalize_capability_key(raw_key: Any) -> str:
    return str(raw_key).strip().lower().replace("-", "_").replace(" ", "_")


def _capability_key_priority(key: str) -> tuple[int, int, str]:
    if key == "_metadata":
        return (0, 0, key)
    try:
        return (1, ACCESS_CAPABILITY_NAMES.index(key), key)
    except ValueError:
        return (2, 0, key)


def _limit_access_capability_keys(capabilities: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    keys = sorted(capabilities, key=_capability_key_priority)
    return {key: capabilities[key] for key in keys[:ACCESS_CAPABILITY_MAX_KEYS]}


def _status_from_capability_evidence(
    status: str | None,
    *,
    allowed: int,
    denied: int,
    inconclusive: int,
) -> str:
    if allowed > 0 and denied > 0:
        return "mixed"
    if allowed > 0:
        return "allowed"
    if denied > 0:
        return "denied"
    if inconclusive > 0:
        return "inconclusive"
    return status or "not_tested"


def _normalize_capability_metadata(raw_metadata: Any) -> dict[str, bool | int | str]:
    if not isinstance(raw_metadata, dict):
        return {}
    metadata: dict[str, bool | int | str] = {}
    for field in ACCESS_CAPABILITY_METADATA_TEXT_FIELDS:
        raw_value = raw_metadata.get(field)
        if isinstance(raw_value, str) and raw_value.strip():
            metadata[field] = raw_value.strip()[:ACCESS_CAPABILITY_MAX_EVIDENCE_LENGTH]
    for field in ACCESS_CAPABILITY_METADATA_COUNT_FIELDS:
        if field in raw_metadata:
            metadata[field] = _normalize_capability_count(raw_metadata.get(field))
    for field in ACCESS_CAPABILITY_METADATA_BOOLEAN_FIELDS:
        raw_value = raw_metadata.get(field)
        if isinstance(raw_value, bool):
            metadata[field] = raw_value
    return metadata


def _normalize_access_capabilities(raw_capabilities: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw_capabilities, str):
        try:
            raw_capabilities = json.loads(raw_capabilities)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw_capabilities, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    raw_entries = sorted(
        raw_capabilities.items(),
        key=lambda entry: _capability_key_priority(_normalize_capability_key(entry[0])),
    )
    for raw_key, raw_value in raw_entries:
        if len(normalized) >= ACCESS_CAPABILITY_MAX_KEYS:
            break
        key = _normalize_capability_key(raw_key)
        if not key or len(key) > ACCESS_CAPABILITY_MAX_KEY_LENGTH or key in normalized:
            continue

        if key == "_metadata":
            metadata = _normalize_capability_metadata(raw_value)
            if metadata:
                normalized[key] = metadata
            continue

        if isinstance(raw_value, str):
            status = _normalize_capability_status(raw_value)
            if status is None:
                continue
            raw_value = {"status": status}
        if not isinstance(raw_value, dict):
            continue

        allowed, denied, inconclusive = _normalize_capability_outcome_counts(
            raw_value.get("allowed"),
            raw_value.get("denied"),
            raw_value.get("inconclusive"),
        )
        attempted = max(
            _normalize_capability_count(raw_value.get("attempted")),
            allowed + denied + inconclusive,
        )
        attempted = min(ACCESS_CAPABILITY_MAX_COUNT, attempted)
        status = _status_from_capability_evidence(
            _normalize_capability_status(raw_value.get("status")),
            allowed=allowed,
            denied=denied,
            inconclusive=inconclusive,
        )
        capability: dict[str, int | str] = {
            "status": status,
            "attempted": attempted,
            "allowed": allowed,
            "denied": denied,
            "inconclusive": inconclusive,
        }
        for evidence_field in ACCESS_CAPABILITY_EVIDENCE_FIELDS:
            raw_evidence = raw_value.get(evidence_field)
            if not isinstance(raw_evidence, str):
                continue
            evidence = raw_evidence.strip()
            if evidence:
                capability[evidence_field] = evidence[:ACCESS_CAPABILITY_MAX_EVIDENCE_LENGTH]
        if "sample_limit" in raw_value:
            capability["sample_limit"] = _normalize_capability_count(raw_value.get("sample_limit"))
        normalized[key] = capability
    return normalized


def _merge_capability_status(current: str, incoming: str) -> str:
    statuses = {current, incoming}
    if "mixed" in statuses or statuses == {"allowed", "denied"}:
        return "mixed"
    if "allowed" in statuses:
        return "allowed"
    if "denied" in statuses:
        return "denied"
    if "inconclusive" in statuses:
        return "inconclusive"
    return "not_tested"


def _capability_status_rank(status: str) -> int:
    if status == "not_tested":
        return 0
    if status == "inconclusive":
        return 1
    if status in {"allowed", "denied"}:
        return 2
    return 3


def _merge_capability_metadata(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    current_complete = current.get("complete") is True
    incoming_complete = incoming.get("complete") is True
    current_finalized = current.get("finalized") is True
    incoming_finalized = incoming.get("finalized") is True
    authoritative_snapshot = None
    if current_finalized != incoming_finalized:
        authoritative_snapshot = current if current_finalized else incoming
    elif current_complete != incoming_complete:
        authoritative_snapshot = current if current_complete else incoming
    metadata: dict[str, Any] = {}
    for field in ACCESS_CAPABILITY_METADATA_TEXT_FIELDS:
        current_value = current.get(field)
        incoming_value = incoming.get(field)
        if authoritative_snapshot is not None and authoritative_snapshot.get(field):
            selected = authoritative_snapshot[field]
        else:
            values = sorted(value for value in (current_value, incoming_value) if value)
            selected = values[0] if values else None
        if selected:
            metadata[field] = selected
    for field in ACCESS_CAPABILITY_METADATA_COUNT_FIELDS:
        if field in current or field in incoming:
            metadata[field] = max(int(current.get(field, 0)), int(incoming.get(field, 0)))
    for field in ACCESS_CAPABILITY_METADATA_BOOLEAN_FIELDS:
        if field not in current and field not in incoming:
            continue
        if field == "complete":
            metadata[field] = current_complete or incoming_complete
        elif field == "finalized":
            metadata[field] = current_finalized or incoming_finalized
        elif authoritative_snapshot is not None and field in authoritative_snapshot:
            metadata[field] = bool(authoritative_snapshot[field])
        else:
            metadata[field] = bool(current.get(field, False) or incoming.get(field, False))
    return metadata


def _merge_access_capabilities(current: Any, incoming: Any) -> dict[str, dict[str, Any]]:
    current_normalized = _normalize_access_capabilities(current)
    incoming_normalized = _normalize_access_capabilities(incoming)
    merged = dict(current_normalized)
    for key, incoming_value in incoming_normalized.items():
        current_value = merged.get(key)
        if current_value is None:
            merged[key] = incoming_value
            continue
        if key == "_metadata":
            merged[key] = _merge_capability_metadata(current_value, incoming_value)
            continue

        allowed, denied, inconclusive = _normalize_capability_outcome_counts(
            max(int(current_value["allowed"]), int(incoming_value["allowed"])),
            max(int(current_value["denied"]), int(incoming_value["denied"])),
            max(int(current_value["inconclusive"]), int(incoming_value["inconclusive"])),
        )
        attempted = min(
            ACCESS_CAPABILITY_MAX_COUNT,
            max(
                int(current_value["attempted"]),
                int(incoming_value["attempted"]),
                allowed + denied + inconclusive,
            ),
        )
        status = _status_from_capability_evidence(
            _merge_capability_status(str(current_value["status"]), str(incoming_value["status"])),
            allowed=allowed,
            denied=denied,
            inconclusive=inconclusive,
        )
        merged[key] = {
            "status": status,
            "attempted": attempted,
            "allowed": allowed,
            "denied": denied,
            "inconclusive": inconclusive,
        }
        current_rank = _capability_status_rank(str(current_value["status"]))
        incoming_rank = _capability_status_rank(str(incoming_value["status"]))
        for evidence_field in ACCESS_CAPABILITY_EVIDENCE_FIELDS:
            current_evidence = current_value.get(evidence_field)
            incoming_evidence = incoming_value.get(evidence_field)
            if evidence_field == "not_tested_reason" and status != "not_tested":
                continue
            if incoming_rank > current_rank:
                chosen_evidence = incoming_evidence
            elif current_rank > incoming_rank:
                chosen_evidence = current_evidence
            else:
                values = sorted(value for value in (current_evidence, incoming_evidence) if value)
                if len(set(values)) <= 1:
                    chosen_evidence = values[0] if values else None
                elif evidence_field == "reason_code":
                    chosen_evidence = "multiple_outcomes"
                elif evidence_field in {"protocol_status", "method"}:
                    chosen_evidence = "multiple"
                elif evidence_field == "scope":
                    chosen_evidence = "mixed_sample"
                else:
                    chosen_evidence = values[0]
            if chosen_evidence:
                merged[key][evidence_field] = chosen_evidence
        if "sample_limit" in current_value or "sample_limit" in incoming_value:
            merged[key]["sample_limit"] = max(
                int(current_value.get("sample_limit", 0)),
                int(incoming_value.get("sample_limit", 0)),
            )
    return _limit_access_capability_keys(merged)


def _reconcile_access_level_with_capabilities(access_level: str, capabilities: Any) -> str:
    """Keep the compatibility summary consistent with stronger observed evidence."""

    normalized = _normalize_access_capabilities(capabilities)

    def _is_observed(capability: str) -> bool:
        evidence = normalized.get(capability)
        if not isinstance(evidence, dict):
            return False
        return evidence.get("status") in {"allowed", "mixed"} or int(evidence.get("allowed", 0)) > 0

    if _is_observed("read_file"):
        return _stronger_access_level(access_level, "readable")
    if _is_observed("list"):
        return _stronger_access_level(access_level, "list_only")

    non_listing_access_observed = any(
        _is_observed(capability)
        for capability in (
            "create_file",
            "create_directory",
            "modify_file",
            "delete",
            "write_acl",
            "write_owner",
        )
    )
    tree_connection_observed = _is_observed("tree_connect")
    if (tree_connection_observed or non_listing_access_observed) and _normalize_access_level(
        access_level
    ) == "no_access":
        # The legacy enum has no connected-but-not-listable or write-only state.
        # Either observation disproves the old no-access label.
        return "unknown"
    return _normalize_access_level(access_level)


def _normalize_item_size(raw_size: Any) -> int | None:
    if raw_size is None or isinstance(raw_size, bool):
        return None
    try:
        size = int(raw_size)
    except (TypeError, ValueError, OverflowError):
        return None
    if size < 0 or size > 2**63 - 1:
        return None
    return size


def _normalize_item_mtime(raw_mtime: Any) -> datetime | None:
    if raw_mtime is None or isinstance(raw_mtime, bool):
        return None
    try:
        if isinstance(raw_mtime, (int, float)):
            return datetime.fromtimestamp(raw_mtime, tz=UTC)
        normalized = str(raw_mtime).strip()
        if not normalized:
            return None
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _normalize_file_attributes(raw_attributes: Any) -> list[str]:
    if not isinstance(raw_attributes, list):
        return []
    normalized: list[str] = []
    for raw_attribute in raw_attributes:
        if len(normalized) >= FILE_ATTRIBUTE_MAX_VALUES:
            break
        if not isinstance(raw_attribute, str):
            continue
        attribute = raw_attribute.strip().lower().replace("-", "_").replace(" ", "_")
        if not attribute or len(attribute) > FILE_ATTRIBUTE_MAX_LENGTH or attribute in normalized:
            continue
        normalized.append(attribute)
    return normalized


def _normalize_provider(raw_provider: Any, share_type: str | None = None) -> str | None:
    if isinstance(raw_provider, str):
        provider = raw_provider.strip().lower().replace("-", "_").replace(" ", "_")
        if provider:
            if len(provider) > PROVIDER_MAX_LENGTH:
                raise ValueError(f"field provider exceeds {PROVIDER_MAX_LENGTH} characters")
            return provider
    if share_type in SHARE_TYPE_TO_RESOURCE_TYPE:
        return share_type
    return None


def _normalize_boolean(raw_value: Any, *, field: str, default: bool = False) -> bool:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, int) and raw_value in {0, 1}:
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    raise ValueError(f"field {field} must be a boolean")


def _normalize_optional_provider_text(raw_value: Any, max_length: int) -> str | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError("must be a string")
    value = raw_value.strip()
    if not value:
        return None
    if "\x00" in value:
        raise ValueError("contains a null character")
    if len(value) > max_length:
        raise ValueError(f"exceeds {max_length} characters")
    return value


def _normalize_web_url(raw_value: Any, *, provider: str | None) -> str | None:
    value = _normalize_optional_provider_text(raw_value, PROVIDER_URL_MAX_BYTES)
    if value is None:
        return None
    if len(value.encode("utf-8")) > PROVIDER_URL_MAX_BYTES:
        raise ValueError(f"field web_url exceeds {PROVIDER_URL_MAX_BYTES} UTF-8 bytes")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("field web_url is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("field web_url must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("field web_url must not contain credentials or a fragment")
    if provider == "sharepoint" and parsed.scheme != "https":
        raise ValueError("field web_url must use https for SharePoint")
    return value


def _provider_metadata_key_fingerprint(raw_key: str) -> str:
    return "".join(character for character in raw_key.lower() if character.isalnum())


def _is_forbidden_provider_metadata_key(raw_key: str) -> bool:
    normalized = raw_key.strip().lower().replace("-", "_").replace(" ", "_")
    fingerprint = _provider_metadata_key_fingerprint(raw_key)
    if normalized in FORBIDDEN_PROVIDER_METADATA_KEYS:
        return True
    if fingerprint in FORBIDDEN_PROVIDER_METADATA_KEY_FINGERPRINTS:
        return True
    return fingerprint.startswith(
        (
            "accesstoken",
            "authorizationheader",
            "bearertoken",
            "clientsecret",
            "privatekey",
            "refreshtoken",
        )
    )


def _reject_secret_keys(raw_value: Any, *, field: str, depth: int = 0) -> None:
    if depth > PROVIDER_METADATA_MAX_DEPTH:
        return
    if isinstance(raw_value, dict):
        for raw_key, value in raw_value.items():
            if isinstance(raw_key, str) and _is_forbidden_provider_metadata_key(raw_key):
                raise ValueError(f"field {field}.{raw_key} is secret or sensitive operational state")
            _reject_secret_keys(value, field=field, depth=depth + 1)
    elif isinstance(raw_value, list):
        for value in raw_value[: PROVIDER_METADATA_MAX_LIST_ITEMS + 1]:
            _reject_secret_keys(value, field=field, depth=depth + 1)


def _normalize_provider_metadata(raw_metadata: Any, *, field: str = "metadata") -> dict[str, Any]:
    if raw_metadata is None:
        return {}
    if not isinstance(raw_metadata, dict):
        raise ValueError(f"field {field} must be an object")

    entry_count = 0

    def normalize(value: Any, depth: int, path: str) -> Any:
        nonlocal entry_count
        if depth > PROVIDER_METADATA_MAX_DEPTH:
            raise ValueError(f"field {field} exceeds maximum nesting depth")
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"field {path} must be finite")
            return value
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError(f"field {path} contains a null character")
            if len(value) > PROVIDER_METADATA_MAX_TEXT_LENGTH:
                raise ValueError(f"field {path} exceeds {PROVIDER_METADATA_MAX_TEXT_LENGTH} characters")
            return value
        if isinstance(value, list):
            if len(value) > PROVIDER_METADATA_MAX_LIST_ITEMS:
                raise ValueError(f"field {path} exceeds {PROVIDER_METADATA_MAX_LIST_ITEMS} list items")
            return [normalize(item, depth + 1, f"{path}[]") for item in value]
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for raw_key, nested_value in value.items():
                if not isinstance(raw_key, str) or not raw_key.strip():
                    raise ValueError(f"field {path} contains an invalid key")
                key = raw_key.strip()
                if _is_forbidden_provider_metadata_key(key):
                    raise ValueError(f"field {path}.{key} is secret or sensitive operational state")
                if len(key) > PROVIDER_METADATA_MAX_KEY_LENGTH:
                    raise ValueError(f"field {path} key exceeds {PROVIDER_METADATA_MAX_KEY_LENGTH} characters")
                entry_count += 1
                if entry_count > PROVIDER_METADATA_MAX_ENTRIES:
                    raise ValueError(f"field {field} exceeds {PROVIDER_METADATA_MAX_ENTRIES} entries")
                normalized[key] = normalize(nested_value, depth + 1, f"{path}.{key}")
            return normalized
        raise ValueError(f"field {path} contains unsupported JSON value")

    normalized = normalize(raw_metadata, 0, field)
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"field {field} is not valid bounded JSON") from exc
    if len(encoded) > PROVIDER_METADATA_MAX_BYTES:
        raise ValueError(f"field {field} exceeds {PROVIDER_METADATA_MAX_BYTES} UTF-8 bytes")
    return normalized


def _normalize_exposure(raw_exposure: Any, *, provider: str | None) -> str | None:
    if raw_exposure is None:
        return "UNKNOWN" if provider == "sharepoint" else None
    if not isinstance(raw_exposure, str):
        raise ValueError("field exposure must be a string")
    exposure = raw_exposure.strip().upper().replace("-", "_").replace(" ", "_")
    if exposure not in EXPOSURE_CLASSIFICATIONS:
        allowed = ", ".join(sorted(EXPOSURE_CLASSIFICATIONS))
        raise ValueError(f"field exposure must be one of: {allowed}")
    return exposure


def _normalize_auth_context(raw_auth: Any) -> dict[str, Any]:
    if raw_auth is None:
        return {}
    if not isinstance(raw_auth, dict):
        raise ValueError("field auth_context must be an object")

    normalized: dict[str, Any] = {}
    for field in AUTH_CONTEXT_TEXT_FIELDS:
        raw_value = raw_auth.get(field)
        if raw_value is None:
            continue
        value = _normalize_optional_provider_text(raw_value, PROVIDER_METADATA_MAX_TEXT_LENGTH)
        if value is not None:
            normalized[field] = value
    for field in ("scopes", "roles"):
        raw_values = raw_auth.get(field)
        if raw_values is None:
            continue
        if not isinstance(raw_values, list) or len(raw_values) > PROVIDER_METADATA_MAX_LIST_ITEMS:
            raise ValueError(
                f"field auth_context.{field} must be a list with at most {PROVIDER_METADATA_MAX_LIST_ITEMS} values"
            )
        values: list[str] = []
        for raw_value in raw_values:
            value = _normalize_optional_provider_text(raw_value, 512)
            if value and value not in values:
                values.append(value)
        normalized[field] = values
    return normalized


def _normalize_collection_context(rec: dict[str, Any]) -> dict[str, Any]:
    raw_context = rec.get("collection_context") if isinstance(rec.get("collection_context"), dict) else {}
    raw_collection = rec.get("collection") if isinstance(rec.get("collection"), dict) else {}
    raw_auth = rec.get("auth_context")
    if raw_auth is None:
        raw_auth = raw_context.get("auth_context")
    if raw_auth is None and any(field in raw_context for field in AUTH_CONTEXT_TEXT_FIELDS | {"scopes", "roles"}):
        raw_auth = raw_context
    if raw_auth is None:
        raw_auth = rec.get("auth")
    auth = _normalize_auth_context(raw_auth)

    context: dict[str, Any] = {}
    for field in COLLECTION_CONTEXT_TEXT_FIELDS:
        raw_value = rec.get(field)
        if raw_value is None:
            raw_value = raw_context.get(field)
        if raw_value is None:
            raw_value = raw_collection.get(field)
        value = _normalize_optional_provider_text(raw_value, PROVIDER_METADATA_MAX_TEXT_LENGTH)
        if value is not None:
            context[field] = value

    for canonical_field, aliases in {
        "status": ("collection_status",),
        "discovery_completeness": ("completeness",),
        "sync_mode": ("snapshot_type",),
    }.items():
        if canonical_field in context:
            continue
        raw_value = None
        for container in (rec, raw_context, raw_collection):
            for alias in aliases:
                if container.get(alias) is not None:
                    raw_value = container.get(alias)
                    break
            if raw_value is not None:
                break
        value = _normalize_optional_provider_text(raw_value, PROVIDER_METADATA_MAX_TEXT_LENGTH)
        if value is not None:
            context[canonical_field] = value

    if "provider" not in context and context.get("source"):
        context["provider"] = str(context["source"])
    if "source" not in context and context.get("provider"):
        context["source"] = str(context["provider"])

    for field in ("partial", "materialized_snapshot"):
        raw_value = rec.get(field)
        if raw_value is None:
            raw_value = raw_context.get(field)
        if raw_value is None:
            raw_value = raw_collection.get(field)
        if raw_value is not None:
            if not isinstance(raw_value, bool):
                raise ValueError(f"field {field} must be a boolean")
            context[field] = raw_value

    context.update(auth)
    if "assessed_identity" not in context and auth.get("user_principal_name"):
        context["assessed_identity"] = auth["user_principal_name"]

    metadata_input = rec.get("metadata")
    if metadata_input is None:
        metadata_input = raw_context.get("metadata")
    if metadata_input is None:
        metadata_input = {}
    elif isinstance(metadata_input, dict):
        metadata_input = dict(metadata_input)
    if raw_collection and isinstance(metadata_input, dict):
        legacy_collection_metadata = {
            key: value
            for key, value in raw_collection.items()
            if key not in COLLECTION_CONTEXT_TEXT_FIELDS and key not in {"partial", "materialized_snapshot"}
        }
        if legacy_collection_metadata:
            metadata_input.setdefault("collection", legacy_collection_metadata)
    metadata = _normalize_provider_metadata(metadata_input, field="collection_context.metadata")
    if metadata:
        context["metadata"] = metadata
    if "materialized_snapshot" not in context and isinstance(metadata.get("snapshot_materialized"), bool):
        context["materialized_snapshot"] = metadata["snapshot_materialized"]
    if "discovery_completeness" not in context and isinstance(metadata.get("discovery_authoritative"), bool):
        context["discovery_completeness"] = (
            "authoritative" if metadata["discovery_authoritative"] else "non_authoritative"
        )
    if "sync_mode" not in context and isinstance(metadata.get("sync_mode"), str):
        context["sync_mode"] = metadata["sync_mode"]
    return context


def _bind_record_to_ingest_run(rec: dict[str, Any], run_id: str) -> dict[str, Any]:
    record_run_id = rec.get("run_id")
    if record_run_id is None or str(record_run_id) != run_id:
        normalized = dict(rec)
        normalized["run_id"] = run_id
        return normalized
    return rec


def _validate_text_value(
    value: Any,
    field: str,
    *,
    max_characters: int | None = None,
    max_bytes: int | None = None,
    allow_empty: bool = False,
) -> str | None:
    if not isinstance(value, str):
        return f"field {field} must be a string"
    if not allow_empty and not value.strip():
        return f"field {field} must not be empty"
    if "\x00" in value:
        return f"field {field} contains a null character"
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return f"field {field} contains invalid Unicode"
    if max_characters is not None and len(value) > max_characters:
        return f"field {field} exceeds {max_characters} characters"
    if max_bytes is not None and encoded_length > max_bytes:
        return f"field {field} exceeds {max_bytes} UTF-8 bytes"
    return None


def _validate_optional_text_value(
    value: Any,
    field: str,
    *,
    max_characters: int | None = None,
    max_bytes: int | None = None,
) -> str | None:
    if value is None:
        return None
    return _validate_text_value(
        value,
        field,
        max_characters=max_characters,
        max_bytes=max_bytes,
        allow_empty=True,
    )


def validate_record(rec: dict[str, Any]) -> tuple[bool, str | None]:
    rec_type = rec.get("type")
    if rec_type not in {"run_meta", "endpoint", "resource", "item", "error", "run_end"}:
        return False, "unknown record type"
    try:
        _reject_secret_keys(rec, field="record")
    except ValueError as exc:
        return False, str(exc)

    if rec_type == "item":
        try:
            rec["deleted"] = _normalize_boolean(rec.get("deleted"), field="deleted")
        except ValueError as exc:
            return False, str(exc)
        provider_item_id = rec.get("provider_item_id")
        if rec["deleted"] and not rec.get("path") and isinstance(provider_item_id, str) and provider_item_id.strip():
            # Graph tombstones can contain only the stable driveItem ID. Keep a
            # deterministic non-live row for history without pretending this is
            # its former pathname.
            digest = hashlib.sha256(provider_item_id.strip().encode("utf-8", errors="replace")).hexdigest()[:24]
            rec["path"] = f"/__deleted__/{digest}"
            rec.setdefault("name", "[deleted item]")

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

    for field in ("run_id",):
        reason = _validate_text_value(rec.get(field), field, max_characters=36)
        if reason:
            return False, reason

    if rec_type == "run_meta":
        try:
            if int(rec.get("schema_version")) != 1:
                return False, "unsupported schema_version"
        except (TypeError, ValueError):
            return False, "invalid schema_version"
        try:
            rec["collection_context"] = _normalize_collection_context(rec)
        except ValueError as exc:
            return False, str(exc)

    if rec_type == "endpoint":
        reason = _validate_text_value(
            rec.get("endpoint_key"),
            "endpoint_key",
            max_characters=ENDPOINT_KEY_MAX_LENGTH,
        )
        if reason:
            return False, reason
        for field, max_length in (("ip", 64), ("hostname", 255), ("domain", 255)):
            reason = _validate_optional_text_value(rec.get(field), field, max_characters=max_length)
            if reason:
                return False, reason
        for container_name, field_name in (("smb", "dialect"), ("smb", "signing"), ("auth", "method")):
            container = rec.get(container_name)
            if container is not None and not isinstance(container, dict):
                return False, f"field {container_name} must be an object"
            if isinstance(container, dict):
                reason = _validate_optional_text_value(
                    container.get(field_name),
                    f"{container_name}.{field_name}",
                    max_characters=64,
                )
                if reason:
                    return False, reason
        try:
            rec["provider"] = _normalize_provider(rec.get("provider"))
            rec["provider_metadata"] = _normalize_provider_metadata(
                rec.get("provider_metadata", rec.get("metadata")),
                field="metadata",
            )
            if rec["provider_metadata"].get("web_url") is not None:
                rec["provider_metadata"]["web_url"] = _normalize_web_url(
                    rec["provider_metadata"]["web_url"],
                    provider=rec["provider"],
                )
        except ValueError as exc:
            return False, str(exc)

    if rec_type in {"resource", "item"}:
        reason = _validate_text_value(
            rec.get("endpoint_key"),
            "endpoint_key",
            max_characters=ENDPOINT_KEY_MAX_LENGTH,
        )
        if reason:
            return False, reason

    if rec_type == "resource":
        reason = _validate_text_value(
            rec.get("name"),
            "name",
            max_characters=RESOURCE_NAME_MAX_LENGTH,
        )
        if reason:
            return False, reason
        reason = _validate_optional_text_value(rec.get("remark"), "remark", max_bytes=INGEST_MAX_RECORD_BYTES)
        if reason:
            return False, reason

    if rec_type == "item" and not rec.get("name") and isinstance(rec.get("path"), str):
        normalized_path = rec["path"].replace("\\", "/")
        rec["name"] = PurePosixPath(normalized_path).name or ""
    if rec_type == "item":
        for field, max_length in (("resource_name", RESOURCE_NAME_MAX_LENGTH), ("name", ITEM_NAME_MAX_LENGTH)):
            reason = _validate_text_value(rec.get(field), field, max_characters=max_length)
            if reason:
                return False, reason
        raw_share_type = str(rec.get("share_type") or "").strip().lower()
        raw_resource_type = str(rec.get("resource_type") or "").strip().lower()
        uses_provider_identity = (
            bool(rec.get("provider_item_id"))
            or raw_share_type == "sharepoint"
            or raw_resource_type == "sharepoint_library"
        )
        path_max_bytes = PROVIDER_ITEM_PATH_MAX_BYTES if uses_provider_identity else ITEM_PATH_MAX_BYTES
        path_max_characters = (
            SHAREPOINT_ITEM_PATH_MAX_CHARACTERS
            if raw_share_type == "sharepoint" or raw_resource_type == "sharepoint_library"
            else None
        )
        reason = _validate_text_value(
            rec.get("path"),
            "path",
            max_characters=path_max_characters,
            max_bytes=path_max_bytes,
        )
        if reason:
            return False, reason
        raw_size = rec.get("size_bytes") if rec.get("size_bytes") is not None else rec.get("size")
        rec["size_bytes"] = _normalize_item_size(raw_size)
        rec["allocation_size_bytes"] = _normalize_item_size(rec.get("allocation_size_bytes"))
        raw_mtime = rec.get("mtime") if rec.get("mtime") is not None else rec.get("modified_at")
        rec["mtime"] = _normalize_item_mtime(raw_mtime)
        rec["created_at"] = _normalize_item_mtime(rec.get("created_at"))
        rec["accessed_at"] = _normalize_item_mtime(rec.get("accessed_at"))
        rec["changed_at"] = _normalize_item_mtime(rec.get("changed_at"))
        rec["file_attributes"] = _normalize_file_attributes(rec.get("file_attributes"))
        try:
            rec["is_dir"] = _normalize_boolean(rec.get("is_dir"), field="is_dir")
        except ValueError as exc:
            return False, str(exc)

    if rec_type == "error":
        severity = rec.get("severity")
        if isinstance(severity, str):
            severity = severity.strip().lower()
            rec["severity"] = severity
        if severity not in ERROR_SEVERITIES:
            return False, "field severity must be warn or error"
        reason = _validate_text_value(
            rec.get("code"),
            "code",
            max_characters=INGEST_ERROR_CODE_MAX_LENGTH,
        )
        if reason:
            return False, reason
        reason = _validate_text_value(rec.get("message"), "message", max_bytes=INGEST_MAX_RECORD_BYTES)
        if reason:
            return False, reason

    if rec_type in {"resource", "item"}:
        try:
            share_type = _normalize_share_type(rec.get("share_type"), rec.get("resource_type"))
        except ValueError as exc:
            return False, str(exc)
        rec["share_type"] = share_type
        rec["resource_type"] = _resource_type_from_share_type(share_type)
        try:
            provider = _normalize_provider(rec.get("provider"), share_type)
            rec["provider"] = provider
            if rec_type == "resource":
                rec["provider_resource_id"] = _normalize_optional_provider_text(
                    rec.get("provider_resource_id"),
                    PROVIDER_ID_MAX_LENGTH,
                )
                if share_type == "sharepoint" and not rec["provider_resource_id"]:
                    return False, "SharePoint resource requires provider_resource_id (Graph drive ID)"
            else:
                rec["provider_resource_id"] = _normalize_optional_provider_text(
                    rec.get("provider_resource_id"),
                    PROVIDER_ID_MAX_LENGTH,
                )
                rec["provider_item_id"] = _normalize_optional_provider_text(
                    rec.get("provider_item_id"),
                    PROVIDER_ID_MAX_LENGTH,
                )
                rec["provider_parent_id"] = _normalize_optional_provider_text(
                    rec.get("provider_parent_id"),
                    PROVIDER_ID_MAX_LENGTH,
                )
                rec["mime_type"] = _normalize_optional_provider_text(
                    rec.get("mime_type"),
                    MIME_TYPE_MAX_LENGTH,
                )
                if share_type == "sharepoint" and not rec["provider_resource_id"]:
                    return False, "SharePoint item requires provider_resource_id (Graph drive ID)"
                if share_type == "sharepoint" and not rec["provider_item_id"]:
                    return False, "SharePoint item requires provider_item_id (Graph driveItem ID)"
                if share_type == "sharepoint":
                    if not str(rec.get("path") or "").startswith("/"):
                        return False, "SharePoint item path must be an absolute provider-relative path"
                    if "\\" in str(rec.get("path") or ""):
                        return False, "SharePoint item path must use forward slashes"
            rec["web_url"] = _normalize_web_url(rec.get("web_url"), provider=provider)
            raw_metadata = rec.get("provider_metadata", rec.get("metadata"))
            if raw_metadata is None:
                raw_metadata = {}
            elif isinstance(raw_metadata, dict):
                raw_metadata = dict(raw_metadata)
            for metadata_field in ("etag", "ctag"):
                if rec.get(metadata_field) is not None and isinstance(raw_metadata, dict):
                    raw_metadata.setdefault(metadata_field, rec.get(metadata_field))
            rec["provider_metadata"] = _normalize_provider_metadata(raw_metadata, field="metadata")
            rec["exposure"] = _normalize_exposure(rec.get("exposure"), provider=provider)
            rec["exposure_evidence"] = _normalize_provider_metadata(
                rec.get("exposure_evidence"),
                field="exposure_evidence",
            )
        except ValueError as exc:
            return False, str(exc)
    if rec_type == "resource":
        rec["access_level"] = _normalize_access_level(rec.get("access_level"))
        rec["access_capabilities"] = _normalize_access_capabilities(rec.get("access_capabilities"))
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


def _join_sharepoint_path(parent: Any, name: Any) -> str:
    raw_parent = str(parent or "/").replace("\\", "/").strip()
    base = "/" + raw_parent.strip("/") if raw_parent.strip("/") else "/"
    leaf = str(name or "").strip().strip("/")
    if not leaf:
        return base
    if PurePosixPath(base).name == leaf:
        return base
    if base == "/":
        return f"/{leaf}"
    return f"{base.rstrip('/')}/{leaf}"


def _iter_items_from_entries(
    run_id: str,
    endpoint_key: str,
    resource_name: str,
    entries: list[Any],
    share_type: str = "smb",
    parent_path: str = "\\",
    provider_resource_id: str | None = None,
):
    resource_type = _resource_type_from_share_type(share_type)
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue

        name = str(raw_entry.get("name") or "").strip()
        is_dir = bool(raw_entry.get("is_dir", False))
        if share_type == "sharepoint":
            full_path = _join_sharepoint_path(raw_entry.get("path") or parent_path, name)
        else:
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
            "size_bytes": raw_entry.get("size_bytes"),
            "allocation_size_bytes": raw_entry.get("allocation_size_bytes"),
            "mtime": raw_entry.get("mtime"),
            "created_at": raw_entry.get("created_at"),
            "accessed_at": raw_entry.get("accessed_at"),
            "changed_at": raw_entry.get("changed_at"),
            "file_attributes": raw_entry.get("file_attributes"),
            "provider": raw_entry.get("provider") or ("sharepoint" if share_type == "sharepoint" else None),
            "provider_resource_id": raw_entry.get("provider_resource_id")
            or raw_entry.get("drive_id")
            or provider_resource_id,
            "provider_item_id": raw_entry.get("provider_item_id"),
            "provider_parent_id": raw_entry.get("provider_parent_id"),
            "web_url": raw_entry.get("web_url"),
            "mime_type": raw_entry.get("mime_type"),
            "deleted": raw_entry.get("deleted", False),
            "exposure": raw_entry.get("exposure"),
            "exposure_evidence": raw_entry.get("exposure_evidence"),
            "metadata": raw_entry.get("metadata"),
            "size": raw_entry.get("size"),
            "modified_at": raw_entry.get("modified_at"),
            "etag": raw_entry.get("etag"),
            "ctag": raw_entry.get("ctag"),
        }

        children = raw_entry.get("children")
        if is_dir and isinstance(children, list):
            yield from _iter_items_from_entries(
                run_id,
                endpoint_key,
                resource_name,
                children,
                share_type,
                full_path,
                provider_resource_id,
            )


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
            "provider": raw_endpoint.get("provider"),
            "metadata": raw_endpoint.get("metadata"),
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
                "access_level": raw_share.get("access_level", "unknown"),
                "access_capabilities": raw_share.get("access_capabilities"),
                "provider": raw_share.get("provider"),
                "provider_resource_id": raw_share.get("provider_resource_id") or raw_share.get("drive_id"),
                "web_url": raw_share.get("web_url"),
                "metadata": raw_share.get("metadata"),
                "exposure": raw_share.get("exposure"),
                "exposure_evidence": raw_share.get("exposure_evidence"),
            }
        )

        raw_entries = raw_share.get("entries")
        if isinstance(raw_entries, list):
            records.extend(
                _iter_items_from_entries(
                    run_id,
                    endpoint_key,
                    share_name,
                    raw_entries,
                    share_type=share_type,
                    parent_path="/" if share_type == "sharepoint" else "\\",
                    provider_resource_id=raw_share.get("provider_resource_id") or raw_share.get("drive_id"),
                )
            )

    return records


def _records_from_nested_json(doc: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    endpoints_raw = doc.get("endpoints")
    if not isinstance(endpoints_raw, list):
        return []

    records: list[dict[str, Any]] = []
    meta_raw = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    run_raw = doc.get("run") if isinstance(doc.get("run"), dict) else {}
    collection_raw = doc.get("collection") if isinstance(doc.get("collection"), dict) else {}
    collection_context_raw = doc.get("collection_context") if isinstance(doc.get("collection_context"), dict) else {}
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
            "auth_context": meta_raw.get("auth_context") if isinstance(meta_raw.get("auth_context"), dict) else None,
            "collection_context": collection_context_raw or None,
            "source": meta_raw.get("source") or collection_raw.get("source"),
            "provider": meta_raw.get("provider") or collection_raw.get("provider"),
            "collection_mode": meta_raw.get("collection_mode") or collection_raw.get("collection_mode"),
            "assessed_identity": meta_raw.get("assessed_identity") or collection_raw.get("assessed_identity"),
            "collection_status": meta_raw.get("collection_status") or collection_raw.get("collection_status"),
            "partial": meta_raw.get("partial") if "partial" in meta_raw else collection_raw.get("partial"),
            "metadata": meta_raw.get("metadata") if isinstance(meta_raw.get("metadata"), dict) else None,
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
    records.append(
        {
            "type": "run_end",
            "run_id": run_id,
            "finished_at": finished_at,
            "stats": summary_raw if isinstance(summary_raw, dict) else {},
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
    if normalized_key.endswith((".ndjson", ".ndjson.gz", ".jsonl", ".jsonl.gz")):
        return False
    return "json" in content_type and "ndjson" not in content_type


def _load_json_records_from_bytes(raw_bytes: bytes, run_id: str) -> list[dict[str, Any]] | None:
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(INVALID_UTF8_ARTIFACT_ERROR) from exc
    try:
        json_doc = json.loads(decoded)
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
    collection_context_raw = _load_first_json_item(fp, "collection_context")
    summary_raw = _load_first_json_item(fp, "summary")
    schema_version = _load_first_json_item(fp, "schema_version")
    first_issue = _load_first_json_item(fp, "issue_summary.item")
    first_endpoint = _load_first_json_item(fp, "endpoints.item")
    started_at = (meta_raw or {}).get("started_at") if isinstance(meta_raw, dict) else None
    started_at = started_at or ((run_raw or {}).get("created_at") if isinstance(run_raw, dict) else None) or now_iso()
    finished_at = (meta_raw or {}).get("finished_at") if isinstance(meta_raw, dict) else None
    finished_at = finished_at or now_iso()

    recognized = (
        isinstance(meta_raw, dict)
        or isinstance(run_raw, dict)
        or isinstance(collection_raw, dict)
        or isinstance(collection_context_raw, dict)
        or isinstance(summary_raw, dict)
        or isinstance(first_issue, dict)
        or isinstance(first_endpoint, dict)
    )
    if recognized:
        yield {
            "type": "run_meta",
            "schema_version": int(schema_version) if isinstance(schema_version, int) else 1,
            "tool": (meta_raw or {}).get("tool") if isinstance(meta_raw, dict) else "share-sentinel-import",
            "tool_version": (meta_raw or {}).get("tool_version") if isinstance(meta_raw, dict) else "unknown",
            "run_id": (meta_raw or {}).get("run_id") if isinstance(meta_raw, dict) else None,
            "started_at": started_at,
            "operator_label": (meta_raw or {}).get("operator_label") if isinstance(meta_raw, dict) else None,
            "collection": collection_raw if isinstance(collection_raw, dict) else None,
            "auth": (meta_raw or {}).get("auth")
            if isinstance(meta_raw, dict) and isinstance((meta_raw or {}).get("auth"), dict)
            else None,
            "auth_context": (meta_raw or {}).get("auth_context")
            if isinstance(meta_raw, dict) and isinstance((meta_raw or {}).get("auth_context"), dict)
            else None,
            "collection_context": collection_context_raw if isinstance(collection_context_raw, dict) else None,
            "source": (meta_raw or {}).get("source") if isinstance(meta_raw, dict) else None,
            "provider": (meta_raw or {}).get("provider") if isinstance(meta_raw, dict) else None,
            "collection_mode": (meta_raw or {}).get("collection_mode") if isinstance(meta_raw, dict) else None,
            "assessed_identity": (meta_raw or {}).get("assessed_identity") if isinstance(meta_raw, dict) else None,
            "collection_status": (meta_raw or {}).get("collection_status") if isinstance(meta_raw, dict) else None,
            "partial": (meta_raw or {}).get("partial") if isinstance(meta_raw, dict) else None,
            "metadata": (meta_raw or {}).get("metadata") if isinstance(meta_raw, dict) else None,
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

    if recognized:
        yield {
            "type": "run_end",
            "run_id": run_id,
            "finished_at": finished_at,
            "stats": summary_raw if isinstance(summary_raw, dict) else {},
        }

    if not recognized and not endpoint_seen and not issue_seen:
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
                raise ValueError(JSON_COMPAT_LIMIT_ERROR)
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
        self._max_position = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _track(self, size: int) -> None:
        try:
            position = int(self._reader.tell())
        except (AttributeError, OSError, TypeError, ValueError):
            position = self._max_position + max(0, size)
        self._max_position = max(self._max_position, position)
        if self._max_position > self._max_bytes:
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

    def seek(self, offset: int, whence: int = 0):
        position = self._reader.seek(offset, whence)
        self._track(0)
        return position

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


def _iter_bounded_ndjson_lines(reader, max_record_bytes: int | None = None):
    record_limit = INGEST_MAX_RECORD_BYTES if max_record_bytes is None else max_record_bytes
    while True:
        raw_line = reader.readline(record_limit + 1)
        if raw_line in {b"", ""}:
            return
        if len(raw_line) > record_limit:
            raise ValueError(NDJSON_RECORD_TOO_LARGE_ERROR)
        yield raw_line


class ArtifactFramingError(ValueError):
    """The artifact does not have the required schema-v1 outer framing."""


class _ArtifactFramingState:
    def __init__(self) -> None:
        self.record_count = 0
        self.run_meta_count = 0
        self.run_end_count = 0
        self.run_end_seen = False

    def observe(self, record_type: object) -> None:
        if self.run_end_seen:
            raise ArtifactFramingError(f"{ARTIFACT_FRAMING_ERROR}; records follow run_end")
        if self.record_count == 0 and record_type != "run_meta":
            raise ArtifactFramingError(f"{ARTIFACT_FRAMING_ERROR}; first record is not run_meta")
        if record_type == "run_meta":
            self.run_meta_count += 1
            if self.run_meta_count > 1:
                raise ArtifactFramingError(f"{ARTIFACT_FRAMING_ERROR}; duplicate run_meta")
        elif record_type == "run_end":
            self.run_end_count += 1
            if self.run_end_count > 1:
                raise ArtifactFramingError(f"{ARTIFACT_FRAMING_ERROR}; duplicate run_end")
            self.run_end_seen = True
        self.record_count += 1

    def finish(self) -> None:
        if self.run_meta_count != 1 or self.run_end_count != 1:
            raise ArtifactFramingError(ARTIFACT_FRAMING_ERROR)


def _validate_record_iter_framing(
    records,
    *,
    progress_callback: Callable[[], None] | None = None,
) -> None:
    framing = _ArtifactFramingState()
    if progress_callback is not None:
        progress_callback()
    for record in records:
        if progress_callback is not None:
            progress_callback()
        record_type = record.get("type") if isinstance(record, dict) else None
        framing.observe(record_type)
    framing.finish()


def _validate_ndjson_framing(
    reader,
    *,
    progress_callback: Callable[[], None] | None = None,
) -> None:
    framing = _ArtifactFramingState()
    if progress_callback is not None:
        progress_callback()
    for raw_line in _iter_bounded_ndjson_lines(reader):
        if progress_callback is not None:
            progress_callback()
        try:
            line = raw_line.decode("utf-8").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
        except UnicodeDecodeError:
            line = "<invalid-utf8>"
            record_type = None
        else:
            if not line:
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                record_type = None
            else:
                record_type = record.get("type") if isinstance(record, dict) else None
        framing.observe(record_type)
    framing.finish()


def _validate_artifact_framing(
    artifact_key: str,
    content_type: str,
    artifact_size: int | None,
    run_id: str,
    *,
    progress_callback: Callable[[], None] | None = None,
) -> None:
    """Validate framing in a read-only pass before any inventory is persisted."""

    json_candidate = _is_json_artifact(artifact_key, content_type)
    gzip_input = artifact_key.endswith(".gz")
    if not json_candidate:
        with open_artifact_stream(artifact_key) as body:
            if gzip_input:
                with (
                    gzip.GzipFile(fileobj=body) as gzip_reader,
                    _LimitedReader(
                        gzip_reader,
                        _gzip_decompressed_limit(artifact_size),
                        GZIP_DECOMPRESSED_LIMIT_ERROR,
                    ) as reader,
                ):
                    _validate_ndjson_framing(reader, progress_callback=progress_callback)
            else:
                _validate_ndjson_framing(body, progress_callback=progress_callback)
        return

    try:
        with open_artifact_stream(artifact_key) as body:
            if gzip_input:
                with (
                    gzip.GzipFile(fileobj=body) as gzip_reader,
                    _LimitedReader(gzip_reader, JSON_COMPAT_MAX_BYTES, JSON_COMPAT_LIMIT_ERROR) as json_reader,
                ):
                    _validate_record_iter_framing(
                        _iter_records_from_streamable_json_file(json_reader, run_id),
                        progress_callback=progress_callback,
                    )
            else:
                with _LimitedReader(body, JSON_COMPAT_MAX_BYTES, JSON_COMPAT_LIMIT_ERROR) as json_reader:
                    _validate_record_iter_framing(
                        _iter_records_from_streamable_json_file(json_reader, run_id),
                        progress_callback=progress_callback,
                    )
        return
    except ArtifactFramingError:
        raise
    except ValueError as exc:
        if str(exc) == JSON_COMPAT_LIMIT_ERROR:
            raise

    with open_artifact_stream(artifact_key) as body:
        raw_json = _read_json_compat_bytes(
            body,
            gzip_input=gzip_input,
            max_bytes=JSON_COMPAT_MAX_BYTES,
        )
    json_records = _load_json_records_from_bytes(raw_json, run_id)
    if json_records is None:
        raise ValueError("unsupported JSON artifact format")
    _validate_record_iter_framing(json_records, progress_callback=progress_callback)


def _public_ingest_error(exc: BaseException) -> str:
    if isinstance(exc, ArtifactFramingError):
        return str(exc)
    if isinstance(exc, (gzip.BadGzipFile, EOFError, zlib.error)):
        return INVALID_GZIP_ARTIFACT_ERROR
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
            JSON_COMPAT_LIMIT_ERROR,
            GZIP_DECOMPRESSED_LIMIT_ERROR,
            NDJSON_RECORD_TOO_LARGE_ERROR,
            INVALID_UTF8_ARTIFACT_ERROR,
        }:
            return detail
        return "artifact validation failed during ingest"
    return "unexpected ingest failure"


def _is_retryable_ingest_error(exc: BaseException) -> bool:
    if isinstance(exc, gzip.BadGzipFile):
        return False
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return True
    if isinstance(exc, psycopg.Error):
        # Transaction serialization, deadlock, lock timeout, and statement
        # timeout are safe to replay because checkpoints are committed before
        # the offset advances durably.
        return getattr(exc, "sqlstate", None) in {"40001", "40P01", "55P03", "57014"}
    return isinstance(exc, OSError)


def _retry_backoff_seconds(attempt_count: int, *, jitter_key: str | None = None) -> int:
    bounded_attempt = max(1, attempt_count)
    base_delay = max(1, INGEST_RETRY_BASE_SECONDS)
    max_delay = max(1, INGEST_RETRY_MAX_SECONDS)
    # Cap the exponent before exponentiation. Normal attempt counts are small,
    # but corrupted persisted progress plus an unsafe retry configuration must
    # not trigger pathological big-integer work merely to reach the max delay.
    exponent_cap = (max_delay // base_delay).bit_length() if base_delay < max_delay else 0
    exponent = min(max(0, bounded_attempt - 1), exponent_cap)
    capped_delay = min(max_delay, base_delay * (2**exponent))
    jitter_ratio = min(1.0, max(0.0, INGEST_RETRY_JITTER_RATIO))
    if not jitter_key or jitter_ratio == 0:
        return capped_delay
    digest = hashlib.sha256(f"{jitter_key}:{bounded_attempt}".encode("utf-8")).digest()
    unit_interval = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    # Downward jitter retains the configured maximum retry budget while
    # spreading runs that failed during the same dependency outage.
    jittered = capped_delay * (1.0 - jitter_ratio * unit_interval)
    return max(1, int(round(jittered)))


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


def connect_database():
    options = (
        f"-c statement_timeout={WORKER_DATABASE_STATEMENT_TIMEOUT_MS} -c lock_timeout={WORKER_DATABASE_LOCK_TIMEOUT_MS}"
    )
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS,
        options=options,
    )


def discover_recoverable_runs(limit: int = 8) -> list[dict[str, str]]:
    with connect_database() as conn:
        rows = conn.execute(
            """
            WITH candidates AS (
                SELECT id
                FROM scan_runs
                WHERE artifact_key IS NOT NULL
                  AND (
                      (
                          status = 'UPLOADED'
                          AND COALESCE(
                              CASE
                                  WHEN pg_input_is_valid(
                                      NULLIF(ingest_progress->>'next_retry_at', ''),
                                      'timestamp with time zone'
                                  )
                                  THEN NULLIF(ingest_progress->>'next_retry_at', '')::timestamptz
                              END,
                              TO_TIMESTAMP(0)
                          ) <= NOW()
                      )
                      OR (
                          status = 'INGESTING'
                          AND COALESCE(
                              CASE
                                  WHEN pg_input_is_valid(
                                      NULLIF(ingest_progress->>'heartbeat_at', ''),
                                      'timestamp with time zone'
                                  )
                                  THEN NULLIF(ingest_progress->>'heartbeat_at', '')::timestamptz
                              END,
                              created_at
                          ) <= NOW() - (%s * INTERVAL '1 second')
                      )
                  )
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE scan_runs AS run
            SET status = 'INGESTING',
                ingest_progress = COALESCE(run.ingest_progress, '{}'::jsonb) || jsonb_build_object(
                    'heartbeat_at', NOW(),
                    'recovery_claimed_at', NOW(),
                    'recovery_claimed_by', %s::text
                )
            FROM candidates
            WHERE run.id = candidates.id
            RETURNING run.id::text, run.project_id::text, run.artifact_key
            """,
            (STALE_INGESTING_SECONDS, max(1, limit), CONSUMER_NAME),
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
    queued_project_id = _normalize_uuid_str(fields.get("project_id"))
    queued_artifact_key = fields.get("artifact_key")
    project_id = queued_project_id
    artifact_key: str | None = None
    progress_raw: Any = {}
    authoritative_row_loaded = False
    last_line_offset = 0
    last_counts = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}

    if not run_id:
        logger.error("invalid job payload missing or invalid run_id: %s", fields)
        return "ignored"

    with connect_database() as conn:
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

            db_project_id, db_artifact_key, status, summary_raw, progress_raw, artifact_content_type, artifact_size = (
                row
            )
            authoritative_row_loaded = True
            project_id = db_project_id
            artifact_key = db_artifact_key
            if queued_project_id and queued_project_id != db_project_id:
                logger.warning(
                    "ignoring stale queue project_id run_id=%s queued_project_id=%s database_project_id=%s",
                    run_id,
                    queued_project_id,
                    db_project_id,
                )
            if queued_artifact_key and queued_artifact_key != db_artifact_key:
                logger.info(
                    "ignoring superseded queue artifact run_id=%s queued_artifact_key=%s database_artifact_key=%s",
                    run_id,
                    queued_artifact_key,
                    db_artifact_key,
                )
            if not artifact_key:
                update_run_status(
                    conn,
                    run_id,
                    "FAILED",
                    parse_offset(progress_raw),
                    parse_summary(summary_raw),
                    "missing artifact key",
                )
                conn.commit()
                return "failed"
            if status in {"COMPLETE", "FAILED"}:
                return "ignored"
            next_retry_at = parse_next_retry_at(progress_raw)
            if status == "UPLOADED" and next_retry_at and next_retry_at > datetime.now(tz=UTC):
                logger.info("run retry is not due yet run_id=%s next_retry_at=%s", run_id, next_retry_at.isoformat())
                return "deferred"

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
            update_run_status(
                conn, run_id, "INGESTING", line_offset, counts, extra_progress={"attempt_count": attempt_count}
            )
            write_audit(
                conn,
                project_id,
                "INGEST_STARTED",
                "scan_run",
                run_id,
                {"worker": CONSUMER_NAME, "resume_from_line": line_offset, "ts": now_iso()},
            )
            conn.commit()

            item_batch: list[tuple] = []
            error_batch: list[tuple] = []

            def checkpoint_shutdown_if_requested() -> None:
                nonlocal last_line_offset, last_counts
                if not _shutdown_event.is_set():
                    return
                flush_item_batch(conn, item_batch)
                flush_error_batch(conn, error_batch)
                update_run_status(
                    conn,
                    run_id,
                    "UPLOADED",
                    line_offset,
                    counts,
                    extra_progress={
                        "attempt_count": attempt_count,
                        "paused_at": now_iso(),
                        "paused_by": CONSUMER_NAME,
                        "pause_reason": "worker_shutdown",
                    },
                )
                write_audit(
                    conn,
                    project_id,
                    "INGEST_PAUSED",
                    "scan_run",
                    run_id,
                    {
                        "worker": CONSUMER_NAME,
                        "line_offset": line_offset,
                        "reason": "worker_shutdown",
                    },
                )
                conn.commit()
                last_line_offset = line_offset
                last_counts = counts.copy()
                _write_worker_heartbeat("shutting_down", run_id=run_id, line_offset=line_offset)
                raise _GracefulWorkerShutdown

            def report_preflight_progress() -> None:
                checkpoint_shutdown_if_requested()
                emit_processing_heartbeat()

            _validate_artifact_framing(
                artifact_key,
                str(artifact_content_type or "").lower(),
                artifact_size,
                run_id,
                progress_callback=report_preflight_progress,
            )

            endpoint_cache: dict[str, int] = _BoundedLRUCache[str](INGEST_IDENTITY_CACHE_SIZE)
            resource_cache: dict[tuple[str, str, str], int] = _BoundedLRUCache[tuple[str, str, str]](
                INGEST_IDENTITY_CACHE_SIZE
            )
            if line_offset > 0:
                endpoint_cache, resource_cache = load_resume_caches(conn, run_id)
            producer_run_end_counts: dict[str, int] | None = None

            def process_record(rec: dict[str, Any]) -> None:
                nonlocal counts, producer_run_end_counts
                if not isinstance(rec, dict):
                    error_batch.append(
                        build_ingest_error_row(
                            run_id,
                            "error",
                            "SCHEMA_INVALID",
                            "record must be a JSON object",
                            None,
                            None,
                            None,
                        )
                    )
                    counts["errors"] += 1
                    if len(error_batch) >= BATCH_SIZE:
                        flush_error_batch(conn, error_batch)
                    return
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

                if rec_type == "run_meta":
                    update_run_collection_context(conn, run_id, rec.get("collection_context") or {})
                elif rec_type == "endpoint":
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
                    resource_cache[
                        _resource_cache_key(
                            endpoint_key,
                            resource_name,
                            resource_type,
                            rec.get("provider_resource_id"),
                        )
                    ] = resource_id
                    counts["resources"] += 1
                elif rec_type == "item":
                    endpoint_key = rec.get("endpoint_key", "")
                    resource_name = rec.get("resource_name", "")
                    resource_type = str(rec.get("resource_type") or "smb_share")
                    key = _resource_cache_key(
                        endpoint_key,
                        resource_name,
                        resource_type,
                        rec.get("provider_resource_id"),
                    )
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
                                "access_level": "unknown",
                                "access_capabilities": {},
                                "provider": rec.get("provider"),
                                "provider_resource_id": rec.get("provider_resource_id"),
                                "provider_metadata": {},
                                "exposure": None,
                                "exposure_evidence": {},
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
                            rec.get("size_bytes"),
                            rec.get("allocation_size_bytes"),
                            rec.get("mtime"),
                            rec.get("created_at"),
                            rec.get("accessed_at"),
                            rec.get("changed_at"),
                            json.dumps(rec.get("file_attributes") or []),
                            rec.get("provider"),
                            rec.get("provider_item_id"),
                            rec.get("provider_parent_id"),
                            rec.get("web_url"),
                            rec.get("mime_type"),
                            bool(rec.get("deleted", False)),
                            json.dumps(rec.get("provider_metadata") or {}),
                            rec.get("exposure"),
                            json.dumps(rec.get("exposure_evidence") or {}),
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
                    incoming = rec.get("stats")
                    if isinstance(incoming, dict) and incoming:
                        producer_run_end_counts = parse_summary(incoming)

            def persist_periodic_progress() -> None:
                nonlocal last_line_offset, last_counts
                if line_offset % PROGRESS_EVERY_LINES != 0:
                    return
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

            def process_record_iter(records_iter) -> int:
                nonlocal line_offset, last_line_offset, last_counts
                current_line = 0
                for rec in records_iter:
                    current_line += 1
                    if current_line <= line_offset:
                        continue
                    checkpoint_shutdown_if_requested()
                    line_offset = current_line
                    emit_processing_heartbeat()
                    process_record(rec)
                    persist_periodic_progress()
                return current_line

            def process_ndjson_lines(reader) -> None:
                nonlocal line_offset, last_line_offset, last_counts
                current_line = 0
                for raw_line in _iter_bounded_ndjson_lines(reader):
                    current_line += 1
                    if current_line <= line_offset:
                        continue

                    checkpoint_shutdown_if_requested()
                    line_offset = current_line
                    emit_processing_heartbeat()
                    line: str | None = None
                    if isinstance(raw_line, bytes):
                        try:
                            line = raw_line.decode("utf-8").strip()
                        except UnicodeDecodeError as exc:
                            error_batch.append(
                                build_ingest_error_row(
                                    run_id,
                                    "error",
                                    "UTF8_DECODE_ERROR",
                                    f"invalid UTF-8 at byte offset {exc.start}",
                                    None,
                                    None,
                                    None,
                                )
                            )
                            counts["errors"] += 1
                            if len(error_batch) >= BATCH_SIZE:
                                flush_error_batch(conn, error_batch)
                    else:
                        line = str(raw_line).strip()

                    if line:
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
                        else:
                            process_record(rec)

                    # A physical line is the resume unit. Blank and malformed
                    # lines must advance durable progress just like valid rows,
                    # otherwise a bad-data-only artifact can look stuck and
                    # repeatedly replay an unbounded transaction after failure.
                    persist_periodic_progress()

            json_records: list[dict[str, Any]] | None = None
            content_type = str(artifact_content_type or "").lower()
            json_candidate = _is_json_artifact(artifact_key, content_type)
            gzip_input = artifact_key.endswith(".gz")

            if json_candidate:
                try:
                    with open_artifact_stream(artifact_key) as body:
                        if gzip_input:
                            with (
                                gzip.GzipFile(fileobj=body) as gzip_reader,
                                _LimitedReader(
                                    gzip_reader,
                                    JSON_COMPAT_MAX_BYTES,
                                    JSON_COMPAT_LIMIT_ERROR,
                                ) as json_reader,
                            ):
                                process_record_iter(_iter_records_from_streamable_json_file(json_reader, run_id))
                        else:
                            with _LimitedReader(body, JSON_COMPAT_MAX_BYTES, JSON_COMPAT_LIMIT_ERROR) as json_reader:
                                process_record_iter(_iter_records_from_streamable_json_file(json_reader, run_id))
                except ValueError as exc:
                    if str(exc) == JSON_COMPAT_LIMIT_ERROR:
                        raise
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
                        with (
                            gzip.GzipFile(fileobj=body) as gzip_reader,
                            _LimitedReader(
                                gzip_reader,
                                _gzip_decompressed_limit(artifact_size),
                                GZIP_DECOMPRESSED_LIMIT_ERROR,
                            ) as reader,
                        ):
                            process_ndjson_lines(reader)
                    else:
                        process_ndjson_lines(body)

            if json_records is not None:
                process_record_iter(json_records)

            checkpoint_shutdown_if_requested()
            flush_item_batch(conn, item_batch)
            flush_error_batch(conn, error_batch)
            persisted_counts = load_persisted_summary(conn, run_id)
            if producer_run_end_counts is not None and producer_run_end_counts != persisted_counts:
                logger.warning(
                    "producer summary differs from persisted inventory run_id=%s producer=%s persisted=%s",
                    run_id,
                    producer_run_end_counts,
                    persisted_counts,
                )
            counts = persisted_counts

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
        except _GracefulWorkerShutdown:
            logger.info("ingest paused for worker shutdown run_id=%s line_offset=%s", run_id, last_line_offset)
            return "shutdown"
        except Exception as exc:
            logger.exception("job failed run_id=%s", run_id)
            if not authoritative_row_loaded:
                try:
                    conn.rollback()
                except psycopg.Error:
                    logger.exception("failed to rollback before authoritative run state was loaded run_id=%s", run_id)
                raise
            public_error = _public_ingest_error(exc)
            retryable = _is_retryable_ingest_error(exc)
            attempt_count = parse_attempt_count(progress_raw) + 1
            try:
                conn.rollback()
            except psycopg.Error:
                logger.exception("failed to rollback aborted ingest transaction run_id=%s", run_id)
            try:
                if isinstance(exc, ArtifactFramingError):
                    clear_persisted_ingest_inventory(conn, run_id)
                    last_line_offset = 0
                    last_counts = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}
                if retryable and attempt_count <= INGEST_MAX_RETRIES:
                    retry_delay_seconds = _retry_backoff_seconds(attempt_count, jitter_key=run_id)
                    next_retry_at = datetime.now(tz=UTC) + timedelta(seconds=retry_delay_seconds)
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
                            "retry_delay_seconds": retry_delay_seconds,
                            "retryable": True,
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
                                "retry_delay_seconds": retry_delay_seconds,
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
                    extra_progress={
                        "attempt_count": attempt_count,
                        "last_attempt_at": now_iso(),
                        "retryable": retryable,
                        "retry_exhausted": retryable and attempt_count > INGEST_MAX_RETRIES,
                    },
                )
                if project_id:
                    write_audit(
                        conn,
                        project_id,
                        "INGEST_FAILED",
                        "scan_run",
                        run_id,
                        {
                            "worker": CONSUMER_NAME,
                            "error": public_error,
                            "attempt_count": attempt_count,
                            "retryable": retryable,
                            "retry_exhausted": retryable and attempt_count > INGEST_MAX_RETRIES,
                        },
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


def try_ensure_group(last_logged_at: float) -> tuple[bool, float]:
    try:
        ensure_group()
        return True, last_logged_at
    except redis.RedisError as exc:
        now = time.time()
        if _should_log_redis_error(last_logged_at, now, interval_seconds=10.0):
            logger.warning("redis stream group setup failed; continuing database recovery: %s", exc)
            last_logged_at = now
        _write_worker_heartbeat("waiting_for_redis")
        return False, last_logged_at


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


def _run_worker_loop() -> None:
    last_recovery_scan = 0.0
    last_redis_error_log = 0.0
    last_group_error_log = 0.0
    last_database_recovery_error_log = 0.0
    last_idle_heartbeat = time.time()
    next_claim_start_id = "0-0"
    group_ready = False

    while not _shutdown_event.is_set():
        messages = []
        if not group_ready:
            group_ready, last_group_error_log = try_ensure_group(last_group_error_log)

        if group_ready:
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
                group_ready = False
                _write_worker_heartbeat("redis_retry")

        if messages:
            for _, jobs in messages:
                for message_id, fields in jobs:
                    if _shutdown_event.is_set():
                        break
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
                        _shutdown_event.wait(1)
                if _shutdown_event.is_set():
                    break

        if group_ready and not _shutdown_event.is_set():
            next_claim_start_id, stale_jobs = claim_stale_messages(next_claim_start_id)
            for message_id, fields in stale_jobs:
                if _shutdown_event.is_set():
                    break
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
                    _shutdown_event.wait(1)

        if not _shutdown_event.is_set() and time.time() - last_recovery_scan >= RECOVERY_SCAN_SECONDS:
            recovered_count = 0
            for _ in range(RECOVERY_SCAN_LIMIT):
                recovered_runs: list[dict[str, str]] = []
                try:
                    # Claim one run immediately before processing it. Claiming
                    # an entire serial batch would make queued work appear
                    # INGESTING while it was only waiting behind an older run.
                    recovered_runs = discover_recoverable_runs(limit=1)
                except psycopg.Error as exc:
                    now = time.time()
                    if _should_log_redis_error(last_database_recovery_error_log, now):
                        logger.warning("database recovery scan failed; retrying: %s", exc)
                        last_database_recovery_error_log = now
                    _write_worker_heartbeat("database_recovery_retry")
                    break
                if not recovered_runs:
                    break
                recovered = recovered_runs[0]
                recovered_count += 1
                try:
                    process_job(recovered)
                except Exception:
                    logger.exception("failed processing recovered uploaded run run_id=%s", _safe_run_id(recovered))
                    _shutdown_event.wait(1)
                if _shutdown_event.is_set():
                    break
            if recovered_count:
                logger.info("processed recoverable ingest runs count=%s", recovered_count)
            last_recovery_scan = time.time()

        now = time.time()
        if group_ready and now - last_idle_heartbeat >= WORKER_HEARTBEAT_INTERVAL_SECONDS:
            _write_worker_heartbeat("idle")
            last_idle_heartbeat = now
        if not group_ready:
            _shutdown_event.wait(1)


def main() -> int:
    _shutdown_event.clear()
    previous_handlers = _install_shutdown_signal_handlers()
    logger.info("worker started consumer=%s", CONSUMER_NAME)
    _write_worker_heartbeat("idle")
    try:
        _run_worker_loop()
    finally:
        _write_worker_heartbeat("stopped")
        _restore_shutdown_signal_handlers(previous_handlers)
    logger.info("worker stopped consumer=%s", CONSUMER_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
