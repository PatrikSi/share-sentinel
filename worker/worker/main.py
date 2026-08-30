import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import re
import signal
import socket
import stat
import sys
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
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
PERMISSION_ENTRY_BATCH_SIZE = _read_int_env(
    "INGEST_PERMISSION_ENTRY_BATCH_SIZE",
    min(BATCH_SIZE, 500),
    min_value=1,
    max_value=5000,
    strict=True,
)
PERMISSION_ENTRY_BATCH_MAX_BYTES = _read_int_env(
    "INGEST_PERMISSION_ENTRY_BATCH_MAX_BYTES",
    8 * 1024 * 1024,
    min_value=64 * 1024,
    max_value=64 * 1024 * 1024,
    strict=True,
)
PERMISSION_ASSESSMENT_PIPELINE_SIZE = min(PERMISSION_ENTRY_BATCH_SIZE, 250)
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
    "probe_abort_reason",
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
    "probes_aborted",
}
FILE_ATTRIBUTE_MAX_VALUES = 32
FILE_ATTRIBUTE_MAX_LENGTH = 64
GZIP_DECOMPRESSED_LIMIT_ERROR = "gzip artifact exceeds decompressed size limit"
NDJSON_RECORD_TOO_LARGE_ERROR = "NDJSON record exceeds configured size limit"
INVALID_GZIP_ARTIFACT_ERROR = "invalid gzip artifact"
JSON_COMPAT_LIMIT_ERROR = "JSON artifact exceeds non-streamable compatibility limit"
INVALID_UTF8_ARTIFACT_ERROR = "artifact contains invalid UTF-8"
ARTIFACT_INTEGRITY_METADATA_ERROR = "artifact integrity metadata is missing or invalid; upload the artifact again"
ARTIFACT_INTEGRITY_MISMATCH_ERROR = (
    "artifact integrity check failed; stored bytes no longer match the uploaded size and SHA-256; "
    "upload the artifact again"
)
ARTIFACT_INTEGRITY_READ_CHUNK_BYTES = 1024 * 1024
ARTIFACT_INTEGRITY_PROGRESS_BYTES = 8 * 1024 * 1024
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
TOKEN_EXPIRATION_MAX_LENGTH = 64
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
# A very small set of non-secret observations uses otherwise sensitive words.
# Keep this allowlist exact: aliases and versioned variants must be reviewed
# rather than silently inheriting the exception.
SAFE_PROVIDER_METADATA_KEY_FINGERPRINTS = {
    "haspassword",
    "tokenexpiration",
}
SENSITIVE_PROVIDER_METADATA_KEY_STEMS = {
    "apikey",
    "authorization",
    "credential",
    "deltalink",
    "password",
    "privatekey",
    "secret",
    "token",
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
    "tool_version",
}
PERMISSION_RECORD_TYPES = {"permission_assessment", "permission_entry"}
CONSUMER_STRUCTURAL_RECORD_ERROR = "CONSUMER_STRUCTURAL_RECORD_INVALID"
CONSUMER_CONTENT_RECORD_ERROR = "CONSUMER_CONTENT_RECORD_INVALID"
CONSUMER_UNCLASSIFIED_RECORD_ERROR = "CONSUMER_ARTIFACT_RECORD_INVALID"
CONSUMER_UNCLASSIFIED_INVENTORY_ERROR_CODES = {
    CONSUMER_UNCLASSIFIED_RECORD_ERROR,
    # Keep resumptions started by an older worker fail closed. Before the
    # dimension-specific codes existed, every rejected non-permission record
    # used SCHEMA_INVALID.
    "SCHEMA_INVALID",
    "UTF8_DECODE_ERROR",
    "JSON_DECODE_ERROR",
}

COMPARISON_ALGORITHM_VERSION = "resource-evidence-v2"
COMPARISON_DEFAULT_OPTIONS_HASH = hashlib.sha256(b"{}").hexdigest()
COMPARISON_ITEM_BATCH_SIZE = 5000
COMPARISON_WORK_QUANTUM_SECONDS = _read_int_env(
    "COMPARISON_WORK_QUANTUM_SECONDS", 30, min_value=5, max_value=300
)
COMPARISON_WORK_QUANTUM_BATCHES = _read_int_env(
    "COMPARISON_WORK_QUANTUM_BATCHES", 20, min_value=1, max_value=1000
)
FINDING_EVALUATION_BATCH_SIZE = _read_int_env(
    "FINDING_EVALUATION_BATCH_SIZE", 500, min_value=50, max_value=5000
)
FINDING_RESOLUTION_BATCH_SIZE = _read_int_env(
    "FINDING_RESOLUTION_BATCH_SIZE", 250, min_value=25, max_value=1000
)
FINDING_POLICIES = {
    "sharepoint.anonymous_access": {
        "version": 1,
        "title": "Anonymous SharePoint access",
        "description": "A SharePoint resource has explicit evidence of an anonymous sharing link.",
        "severity": "critical",
    },
    "sharepoint.broad_internal_access": {
        "version": 1,
        "title": "Organization-wide SharePoint access",
        "description": "A SharePoint resource has explicit organization-wide sharing evidence.",
        "severity": "medium",
    },
    "smb.write_observed": {
        "version": 1,
        "title": "SMB write capability observed",
        "description": "One or more bounded, non-mutating SMB capability probes indicate write access.",
        "severity": "high",
    },
    "resource.appeared": {
        "version": 1,
        "title": "Resource appeared",
        "description": "A resource appeared between structurally comparable collection runs.",
        "severity": "info",
    },
    "resource.disappeared": {
        "version": 1,
        "title": "Resource disappeared",
        "description": "A resource disappeared between structurally comparable collection runs.",
        "severity": "low",
    },
    "permission.evidence_changed": {
        "version": 1,
        "title": "Permission evidence changed",
        "description": "Comparable direct-permission or bounded capability evidence changed.",
        "severity": "high",
    },
    "comparison.indeterminate": {
        "version": 1,
        "title": "Change is indeterminate",
        "description": "Collection coverage or identity continuity does not support a definitive change conclusion.",
        "severity": "low",
    },
}


def _record_validation_error_code(record: dict[str, Any]) -> str:
    record_type = record.get("type")
    if record_type in PERMISSION_RECORD_TYPES:
        return "PERMISSION_EVIDENCE_INVALID"
    if record_type in {"endpoint", "resource"}:
        return CONSUMER_STRUCTURAL_RECORD_ERROR
    if record_type == "item":
        return CONSUMER_CONTENT_RECORD_ERROR
    return CONSUMER_UNCLASSIFIED_RECORD_ERROR


PERMISSION_PROVIDERS = {"smb", "sharepoint"}
PERMISSION_SEMANTICS = {
    "smb": "smb_windows_acl_v1",
    "sharepoint": "sharepoint_graph_permission_v1",
}
PERMISSION_SURFACES = {
    "smb": {"smb_filesystem_dacl", "smb_share_acl"},
    "sharepoint": {"sharepoint_graph_permissions"},
}
PERMISSION_MAX_LIST_VALUES = 128
PERMISSION_MAX_TEXT_LENGTH = 1024
PERMISSION_MAX_PATH_BYTES = 8192
PERMISSION_MAX_COUNT = 2**31 - 1
PERMISSION_SENSITIVE_KEY_FINGERPRINTS = {
    "shareid",
    "webhtml",
    "invitationurl",
    "linkurl",
    "rawsecuritydescriptor",
    "securitydescriptorbytes",
    "rawdescriptor",
    "rawsacl",
    "sddl",
}
SAFE_PERMISSION_DETAIL_KEY_FINGERPRINTS = {
    "daclacecount",
    "daclreservedbyte",
    "daclreservedword",
    "daclrevision",
    "daclsize",
    "daclstate",
    "descriptorcontrolflags",
    "descriptorcontrolretained",
    "descriptorrevision",
    "descriptorsize",
    "haspassword",
    "invitationsigninrequired",
    "linkscope",
    "linktype",
    "saclrequested",
    "saclretained",
}
SAFE_PROVIDER_BYTE_COUNT_KEY_FINGERPRINTS = {
    "allocationsizebytes",
    "maxartifactbytes",
    "sizebytes",
    "totalsizebytes",
}
SAFE_PROVIDER_BYTE_COUNT_MAX = 2**63 - 1
SAFE_PERMISSION_INTEGER_RANGES = {
    "daclacecount": (0, 65_535),
    "daclreservedbyte": (0, 255),
    "daclreservedword": (0, 65_535),
    "daclrevision": (0, 255),
    "daclsize": (0, 65_535),
    "descriptorcontrolretained": (0, 65_535),
    "descriptorrevision": (0, 255),
    "descriptorsize": (0, 65_535),
}
SAFE_PERMISSION_BOOLEAN_KEYS = {
    "haspassword",
    "invitationsigninrequired",
    "saclrequested",
    "saclretained",
}
SAFE_PERMISSION_ENUM_VALUES = {
    "daclstate": frozenset({"absent", "null", "malformed", "empty", "present", "unknown"}),
    "linkscope": frozenset({"anonymous", "organization", "users", "existing_access", "unknown"}),
    "linktype": frozenset({"view", "edit", "embed", "unknown"}),
}
SAFE_DESCRIPTOR_CONTROL_FLAGS = frozenset(
    {
        "owner_defaulted",
        "group_defaulted",
        "dacl_present",
        "dacl_defaulted",
        "dacl_auto_inherit_required",
        "dacl_auto_inherited",
        "dacl_protected",
        "self_relative",
    }
)

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
    # Resolve each path component relative to an already-open directory and
    # refuse symlinks at every level. This closes the shared-volume TOCTOU
    # window between validating a path and opening the artifact itself.
    path = _artifact_key_to_path(key)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("artifact storage requires O_NOFOLLOW support")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | nofollow
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | nofollow
    directory_fd = os.open(Path(ARTIFACT_STORAGE_PATH), directory_flags)
    try:
        relative_parts = path.relative_to(Path(ARTIFACT_STORAGE_PATH)).parts
        for part in relative_parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative_parts[-1], file_flags, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OSError("artifact path is not a regular file")
            return os.fdopen(file_fd, "rb")
        except Exception:
            os.close(file_fd)
            raise
    finally:
        os.close(directory_fd)


class ArtifactIntegrityError(RuntimeError):
    """The persisted upload provenance cannot authenticate the stored bytes."""


def _require_artifact_integrity(artifact_size: Any, artifact_sha256: Any) -> tuple[int, str]:
    if isinstance(artifact_size, bool) or not isinstance(artifact_size, int) or artifact_size < 0:
        raise ArtifactIntegrityError(ARTIFACT_INTEGRITY_METADATA_ERROR)
    if not isinstance(artifact_sha256, str):
        raise ArtifactIntegrityError(ARTIFACT_INTEGRITY_METADATA_ERROR)
    digest = artifact_sha256.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ArtifactIntegrityError(ARTIFACT_INTEGRITY_METADATA_ERROR)
    return artifact_size, digest


class _ArtifactIntegrityReader:
    """Hash a raw artifact as callers stream it, then authenticate at EOF."""

    def __init__(
        self,
        reader,
        expected: tuple[int, str],
        *,
        progress_callback: Callable[[], None] | None = None,
    ) -> None:
        self._reader = reader
        self._expected_size, self._expected_sha256 = expected
        self._progress_callback = progress_callback
        self._digest = hashlib.sha256()
        self._size = 0
        self._next_progress_size = ARTIFACT_INTEGRITY_PROGRESS_BYTES
        self._verified = False
        self._requires_full_rehash = False

    def _reset_hash(self) -> None:
        self._digest = hashlib.sha256()
        self._size = 0
        self._next_progress_size = ARTIFACT_INTEGRITY_PROGRESS_BYTES

    def _track(self, chunk):
        if chunk in {b"", ""}:
            return chunk
        if not isinstance(chunk, bytes):
            raise ArtifactIntegrityError(ARTIFACT_INTEGRITY_MISMATCH_ERROR)
        self._size += len(chunk)
        if self._size > self._expected_size:
            raise ArtifactIntegrityError(ARTIFACT_INTEGRITY_MISMATCH_ERROR)
        self._digest.update(chunk)
        if self._progress_callback is not None and self._size >= self._next_progress_size:
            self._progress_callback()
            self._next_progress_size = self._size + ARTIFACT_INTEGRITY_PROGRESS_BYTES
        return chunk

    def read(self, size: int = -1):
        return self._track(self._reader.read(size))

    def read1(self, size: int = -1):
        read = getattr(self._reader, "read1", self._reader.read)
        return self._track(read(size))

    def readline(self, size: int = -1):
        return self._track(self._reader.readline(size))

    def readinto(self, buffer):
        count = self._reader.readinto(buffer)
        if count is not None and count > 0:
            self._track(bytes(memoryview(buffer)[:count]))
        return count

    def readinto1(self, buffer):
        readinto = getattr(self._reader, "readinto1", self._reader.readinto)
        count = readinto(buffer)
        if count is not None and count > 0:
            self._track(bytes(memoryview(buffer)[:count]))
        return count

    def seek(self, offset: int, whence: int = 0):
        position = self._reader.seek(offset, whence)
        # Compact JSON parsing intentionally rewinds the stream for multiple
        # bounded ijson projections. Do not count those passes cumulatively;
        # verify() will perform one fresh bounded hash of the raw descriptor.
        self._requires_full_rehash = True
        self._reset_hash()
        return position

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if line == b"":
            raise StopIteration
        return line

    def close(self) -> None:
        # The outer open_verified_artifact_stream context owns the raw stream.
        # Some parser adapters close their input before this reader performs
        # its final bounded drain, so an inner close must not close it early.
        return None

    def verify(self) -> None:
        if self._verified:
            return
        if self._requires_full_rehash:
            self._reader.seek(0)
            self._reset_hash()
        while True:
            chunk = self.read(ARTIFACT_INTEGRITY_READ_CHUNK_BYTES)
            if not chunk:
                break
        if self._size != self._expected_size or not hmac.compare_digest(
            self._digest.hexdigest(), self._expected_sha256
        ):
            raise ArtifactIntegrityError(ARTIFACT_INTEGRITY_MISMATCH_ERROR)
        self._verified = True

    def __getattr__(self, name: str):
        return getattr(self._reader, name)


@contextmanager
def open_verified_artifact_stream(
    key: str,
    expected: tuple[int, str],
    *,
    progress_callback: Callable[[], None] | None = None,
):
    """Yield a raw stream and authenticate bytes before returning or surfacing parse failure."""

    with open_artifact_stream(key) as body:
        reader = _ArtifactIntegrityReader(body, expected, progress_callback=progress_callback)
        try:
            yield reader
        except _GracefulWorkerShutdown:
            # Shutdown checkpoints keep the run nonterminal. A resumed worker
            # authenticates the artifact before consuming another record.
            raise
        except Exception as original_error:
            try:
                reader.verify()
            except ArtifactIntegrityError as integrity_error:
                # Stored-byte mutation is the authoritative failure: it also
                # requires deleting rows committed by an earlier checkpoint.
                raise integrity_error from original_error
            except Exception as verification_error:
                # Authentication itself failed (for example, shared storage
                # disappeared). Surface that dependency error so normal retry
                # policy rechecks provenance before any further processing.
                raise verification_error from original_error
            raise
        else:
            reader.verify()


def verify_artifact_integrity(
    key: str,
    expected: tuple[int, str],
    *,
    progress_callback: Callable[[], None] | None = None,
) -> None:
    """Re-open and authenticate the current artifact object without materializing it."""

    with open_verified_artifact_stream(key, expected, progress_callback=progress_callback):
        pass


MAX_AUDIT_METADATA_BYTES = 64 * 1024
MAX_AUDIT_STRING_CHARS = 4096
MAX_AUDIT_COLLECTION_ITEMS = 100
MAX_AUDIT_DEPTH = 6
AUDIT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
SENSITIVE_AUDIT_KEY_FINGERPRINT_SUFFIXES = (
    "accesskey",
    "accesstoken",
    "apikey",
    "assertion",
    "authorization",
    "bearertoken",
    "clientsecret",
    "connectionstring",
    "cookie",
    "credential",
    "databaseurl",
    "hashes",
    "jwt",
    "lmhash",
    "nthash",
    "passphrase",
    "password",
    "pem",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
    "tokenhash",
)


def _audit_key_is_sensitive(value: object) -> bool:
    fingerprint = "".join(character for character in str(value).strip().casefold() if character.isalnum())
    return fingerprint.endswith(SENSITIVE_AUDIT_KEY_FINGERPRINT_SUFFIXES)


def _bounded_audit_string(value: object) -> str:
    normalized = str(value)
    if len(normalized) <= MAX_AUDIT_STRING_CHARS:
        return normalized
    omitted = len(normalized) - MAX_AUDIT_STRING_CHARS
    return f"{normalized[:MAX_AUDIT_STRING_CHARS]}…[truncated {omitted} chars]"


def _sanitize_audit_value(value: object, *, depth: int = 0) -> Any:
    if depth >= MAX_AUDIT_DEPTH:
        return "[truncated: maximum audit metadata depth exceeded]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_audit_string(value)
    if isinstance(value, (uuid.UUID, date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        pairs = list(value.items())
        for raw_key, item in pairs[:MAX_AUDIT_COLLECTION_ITEMS]:
            key = _bounded_audit_string(raw_key)
            sanitized[key] = (
                "[redacted]" if _audit_key_is_sensitive(key) else _sanitize_audit_value(item, depth=depth + 1)
            )
        if len(pairs) > MAX_AUDIT_COLLECTION_ITEMS:
            sanitized["_truncated_field_count"] = len(pairs) - MAX_AUDIT_COLLECTION_ITEMS
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        sanitized_items = [
            _sanitize_audit_value(item, depth=depth + 1) for item in items[:MAX_AUDIT_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_AUDIT_COLLECTION_ITEMS:
            sanitized_items.append({"_truncated_item_count": len(items) - MAX_AUDIT_COLLECTION_ITEMS})
        return sanitized_items
    return _bounded_audit_string(value)


def sanitize_audit_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    sanitized = _sanitize_audit_value(metadata or {})
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_AUDIT_METADATA_BYTES:
        return sanitized
    preserved = {
        key: value
        for key, value in sanitized.items()
        if key in {"request_id", "ip", "user_agent", "reason", "status"}
    }
    return {
        **preserved,
        "_metadata_truncated": True,
        "_encoded_bytes_before_truncation": len(encoded),
        "_top_level_keys": list(sanitized)[:MAX_AUDIT_COLLECTION_ITEMS],
    }


def _validate_audit_label(value: str, *, name: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length or not AUDIT_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(f"{name} must be a bounded audit identifier")
    return normalized


def _validate_audit_object_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 255:
        raise ValueError("object_id must be between 1 and 255 characters")
    return normalized


def write_audit(
    conn: psycopg.Connection, project_id: str, action: str, object_type: str, object_id: str, metadata: dict[str, Any]
):
    action = _validate_audit_label(action, name="action", max_length=120)
    object_type = _validate_audit_label(object_type, name="object_type", max_length=80)
    object_id = _validate_audit_object_id(object_id)
    sanitized_metadata = sanitize_audit_metadata(metadata)
    conn.execute(
        """
        INSERT INTO audit_events (project_id, action, object_type, object_id, metadata)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            project_id,
            action,
            object_type,
            object_id,
            json.dumps(sanitized_metadata, ensure_ascii=False, separators=(",", ":")),
        ),
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
    raw_provider_metadata = rec.get("provider_metadata")
    provider_metadata = dict(raw_provider_metadata) if isinstance(raw_provider_metadata, dict) else {}
    top_level_provider_id = rec.get("provider_endpoint_id")
    if top_level_provider_id and not provider_metadata.get("provider_endpoint_id"):
        provider_metadata["provider_endpoint_id"] = top_level_provider_id
    if not str(provider_metadata.get("provider_endpoint_id") or "").strip():
        # Out-of-order resources create an endpoint placeholder with no
        # provider identity.  A later identified endpoint may enrich it, but a
        # subsequent ID-less duplicate must never rewrite immutable identity
        # provenance on an already identified row.
        for field in (
            "provider_endpoint_id",
            "identity_source",
            "identity_strength",
            "server_guid",
            "advertised_names",
        ):
            provider_metadata.pop(field, None)
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
        WHERE
            NULLIF(endpoints.provider_metadata->>'provider_endpoint_id', '') IS NULL
            OR (
                (
                    NULLIF(EXCLUDED.provider_metadata->>'provider_endpoint_id', '') IS NULL
                    OR endpoints.provider_metadata->>'provider_endpoint_id'
                        = EXCLUDED.provider_metadata->>'provider_endpoint_id'
                )
                AND (
                    NULLIF(endpoints.provider_metadata->>'identity_source', '') IS NULL
                    OR NULLIF(EXCLUDED.provider_metadata->>'identity_source', '') IS NULL
                    OR endpoints.provider_metadata->>'identity_source'
                        = EXCLUDED.provider_metadata->>'identity_source'
                )
                AND (
                    NULLIF(endpoints.provider_metadata->>'identity_strength', '') IS NULL
                    OR NULLIF(EXCLUDED.provider_metadata->>'identity_strength', '') IS NULL
                    OR endpoints.provider_metadata->>'identity_strength'
                        = EXCLUDED.provider_metadata->>'identity_strength'
                )
                AND (
                    NULLIF(endpoints.provider_metadata->>'server_guid', '') IS NULL
                    OR NULLIF(EXCLUDED.provider_metadata->>'server_guid', '') IS NULL
                    OR endpoints.provider_metadata->>'server_guid'
                        = EXCLUDED.provider_metadata->>'server_guid'
                )
            )
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
            json.dumps(provider_metadata),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("duplicate endpoint records contain conflicting immutable provider identity")
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
                   exposure, exposure_evidence, permission_summary
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
                       exposure, exposure_evidence, permission_summary
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
                   exposure, exposure_evidence, permission_summary
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
                provider_metadata, exposure, exposure_evidence, permission_summary
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                json.dumps(rec.get("permission_summary") or {}),
            ),
        ).fetchone()
    else:
        existing_id, existing_access, existing_capabilities = existing[:3]
        existing_metadata = existing[3] if len(existing) > 3 and isinstance(existing[3], dict) else {}
        existing_exposure = existing[4] if len(existing) > 4 else None
        existing_exposure_evidence = existing[5] if len(existing) > 5 and isinstance(existing[5], dict) else {}
        existing_permission_summary = existing[6] if len(existing) > 6 and isinstance(existing[6], dict) else {}
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
        merged_permission_summary = {
            **existing_permission_summary,
            **(rec.get("permission_summary") or {}),
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
                exposure_evidence = %s,
                permission_summary = %s
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
                json.dumps(merged_permission_summary),
                existing_id,
            ),
        ).fetchone()
    return int(row[0])


def resolve_permission_subject(
    conn: psycopg.Connection,
    run_id: str,
    rec: dict[str, Any],
    endpoint_cache: dict[str, int],
    resource_cache: dict[tuple[str, str, str], int],
) -> tuple[int, int | None]:
    endpoint_key = str(rec.get("endpoint_key") or "")
    resource_name = str(rec.get("resource_name") or "")
    resource_type = "sharepoint_library" if rec.get("provider") == "sharepoint" else "smb_share"
    key = _resource_cache_key(
        endpoint_key,
        resource_name,
        resource_type,
        rec.get("provider_resource_id"),
    )
    resource_id = resource_cache.get(key)
    if resource_id is None:
        row = conn.execute(
            """
            SELECT resource.id
            FROM resources AS resource
            JOIN endpoints AS endpoint
              ON endpoint.id = resource.endpoint_id
             AND endpoint.run_id = resource.run_id
            WHERE resource.run_id = %s
              AND endpoint.endpoint_key = %s
              AND resource.resource_type = %s
              AND (
                    (%s IS NOT NULL AND resource.provider_resource_id = %s)
                 OR (%s IS NULL AND resource.provider_resource_id IS NULL AND resource.name = %s)
              )
            ORDER BY resource.id
            LIMIT 1
            """,
            (
                run_id,
                endpoint_key,
                resource_type,
                rec.get("provider_resource_id"),
                rec.get("provider_resource_id"),
                rec.get("provider_resource_id"),
                resource_name,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("permission evidence references a resource that has not been emitted")
        resource_id = int(row[0])
        resource_cache[key] = resource_id

    subject_kind = str(rec.get("subject_kind") or "").strip().lower()
    requires_item = subject_kind in {"item", "file", "directory", "folder", "drive_item"}
    if not requires_item:
        return resource_id, None

    provider_item_id = rec.get("provider_item_id")
    subject_path = rec.get("subject_path")
    if provider_item_id:
        row = conn.execute(
            """
            SELECT id
            FROM items
            WHERE run_id = %s AND resource_id = %s AND provider_item_id = %s
            LIMIT 1
            """,
            (run_id, resource_id, provider_item_id),
        ).fetchone()
    elif subject_path is not None:
        row = conn.execute(
            """
            SELECT id
            FROM items
            WHERE run_id = %s AND resource_id = %s AND path = %s
            LIMIT 1
            """,
            (run_id, resource_id, subject_path),
        ).fetchone()
    else:
        raise ValueError("item permission evidence requires provider_item_id or subject_path")
    if row is None:
        raise ValueError("permission evidence references an item that has not been emitted")
    return resource_id, int(row[0])


def upsert_permission_assessment(
    conn: psycopg.Connection,
    run_id: str,
    resource_id: int,
    item_id: int | None,
    rec: dict[str, Any],
) -> int:
    subject_provider_id = rec.get("provider_item_id") or rec.get("provider_resource_id")
    collision = conn.execute(
        """
        SELECT assessment_key, resource_id, item_id, provider, semantics,
               permission_surface, subject_key, subject_kind,
               subject_provider_id, subject_path, method
        FROM permission_assessments
        WHERE run_id = %s
          AND (
                assessment_key = %s
             OR (
                    resource_id = %s
                AND subject_key = %s
                AND semantics = %s
                AND permission_surface = %s
             )
          )
        FOR UPDATE
        """,
        (
            run_id,
            rec["assessment_key"],
            resource_id,
            rec["subject_key"],
            rec["semantics"],
            rec["permission_surface"],
        ),
    ).fetchone()
    if collision is not None and (
        collision[0] != rec["assessment_key"]
        or int(collision[1]) != resource_id
        or (int(collision[2]) if collision[2] is not None else None) != item_id
        or collision[3] != rec["provider"]
        or collision[4] != rec["semantics"]
        or collision[5] != rec["permission_surface"]
        or collision[6] != rec["subject_key"]
        or collision[7] != rec["subject_kind"]
        or collision[8] != subject_provider_id
        or collision[9] != rec.get("subject_path")
        or collision[10] != rec["method"]
    ):
        raise ValueError("permission assessment identity collides with a different subject or surface")
    row = conn.execute(
        """
        INSERT INTO permission_assessments (
            run_id, resource_id, item_id, assessment_key, subject_kind, subject_key,
            subject_provider_id, subject_path, provider, semantics, permission_surface,
            method, assessment_state, selection_scope, selection_coverage,
            retrieval_coverage, provider_visibility, semantic_coverage,
            principal_resolution, effective_access_status, negative_conclusion_supported,
            entries_observed, entries_emitted, entries_omitted, unknown_entries,
            evidence_hash, entry_set_hash, observed_at, limitations, error_code, errors,
            provider_details, summary
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s::jsonb
        )
        ON CONFLICT (run_id, assessment_key)
        DO UPDATE SET
            assessment_state = EXCLUDED.assessment_state,
            selection_scope = EXCLUDED.selection_scope,
            selection_coverage = EXCLUDED.selection_coverage,
            retrieval_coverage = EXCLUDED.retrieval_coverage,
            provider_visibility = EXCLUDED.provider_visibility,
            semantic_coverage = EXCLUDED.semantic_coverage,
            principal_resolution = EXCLUDED.principal_resolution,
            effective_access_status = EXCLUDED.effective_access_status,
            negative_conclusion_supported = EXCLUDED.negative_conclusion_supported,
            entries_observed = EXCLUDED.entries_observed,
            entries_emitted = EXCLUDED.entries_emitted,
            entries_omitted = EXCLUDED.entries_omitted,
            unknown_entries = EXCLUDED.unknown_entries,
            evidence_hash = EXCLUDED.evidence_hash,
            entry_set_hash = EXCLUDED.entry_set_hash,
            observed_at = EXCLUDED.observed_at,
            limitations = EXCLUDED.limitations,
            error_code = EXCLUDED.error_code,
            errors = EXCLUDED.errors,
            provider_details = EXCLUDED.provider_details,
            summary = EXCLUDED.summary
        WHERE permission_assessments.resource_id = EXCLUDED.resource_id
          AND permission_assessments.item_id IS NOT DISTINCT FROM EXCLUDED.item_id
          AND permission_assessments.provider = EXCLUDED.provider
          AND permission_assessments.semantics = EXCLUDED.semantics
          AND permission_assessments.permission_surface = EXCLUDED.permission_surface
          AND permission_assessments.subject_key = EXCLUDED.subject_key
          AND permission_assessments.subject_kind = EXCLUDED.subject_kind
          AND permission_assessments.subject_provider_id IS NOT DISTINCT FROM EXCLUDED.subject_provider_id
          AND permission_assessments.subject_path IS NOT DISTINCT FROM EXCLUDED.subject_path
          AND permission_assessments.method = EXCLUDED.method
        RETURNING id
        """,
        (
            run_id,
            resource_id,
            item_id,
            rec["assessment_key"],
            rec["subject_kind"],
            rec["subject_key"],
            subject_provider_id,
            rec.get("subject_path"),
            rec["provider"],
            rec["semantics"],
            rec["permission_surface"],
            rec["method"],
            rec["assessment_state"],
            rec["selection_scope"],
            rec["selection_coverage"],
            rec["retrieval_coverage"],
            rec["provider_visibility"],
            rec["semantic_coverage"],
            rec["principal_resolution"],
            rec["effective_access_status"],
            rec["negative_conclusion_supported"],
            rec["entries_observed"],
            rec["entries_emitted"],
            rec["entries_omitted"],
            rec["unknown_entries"],
            rec.get("evidence_hash"),
            rec.get("entry_set_hash"),
            rec.get("observed_at"),
            json.dumps(rec.get("limitations") or []),
            rec.get("error_code"),
            json.dumps(rec.get("errors") or []),
            json.dumps(rec.get("provider_details") or {}),
            json.dumps(rec.get("permission_summary") or {}),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("assessment_key was reused for a different permission subject")
    return int(row[0])


def upsert_permission_principal(
    conn: psycopg.Connection,
    run_id: str,
    principal: dict[str, Any] | None,
) -> int | None:
    if principal is None:
        return None
    row = conn.execute(
        """
        INSERT INTO permission_principals (
            run_id, provider, principal_key, identifier_namespace, authority,
            native_id, kind, display_name, login_name, email, resolution_state,
            resolution_source, aliases
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (run_id, provider, principal_key)
        DO UPDATE SET
            authority = COALESCE(EXCLUDED.authority, permission_principals.authority),
            native_id = COALESCE(EXCLUDED.native_id, permission_principals.native_id),
            kind = CASE
                WHEN permission_principals.kind = 'unknown' THEN EXCLUDED.kind
                ELSE permission_principals.kind
            END,
            display_name = COALESCE(EXCLUDED.display_name, permission_principals.display_name),
            login_name = COALESCE(EXCLUDED.login_name, permission_principals.login_name),
            email = COALESCE(EXCLUDED.email, permission_principals.email),
            resolution_state = CASE
                WHEN permission_principals.resolution_state = 'unresolved' THEN EXCLUDED.resolution_state
                ELSE permission_principals.resolution_state
            END,
            resolution_source = COALESCE(EXCLUDED.resolution_source, permission_principals.resolution_source),
            aliases = CASE
                WHEN jsonb_array_length(EXCLUDED.aliases) > jsonb_array_length(permission_principals.aliases)
                THEN EXCLUDED.aliases
                ELSE permission_principals.aliases
            END
        WHERE permission_principals.identifier_namespace = EXCLUDED.identifier_namespace
          AND (
              permission_principals.authority IS NULL
              OR EXCLUDED.authority IS NULL
              OR permission_principals.authority = EXCLUDED.authority
          )
          AND (
              permission_principals.native_id IS NULL
              OR EXCLUDED.native_id IS NULL
              OR permission_principals.native_id = EXCLUDED.native_id
          )
          AND (
              permission_principals.kind IS NULL
              OR permission_principals.kind = 'unknown'
              OR EXCLUDED.kind IS NULL
              OR EXCLUDED.kind = 'unknown'
              OR permission_principals.kind = EXCLUDED.kind
          )
        RETURNING id
        """,
        (
            run_id,
            principal["provider"],
            principal["principal_key"],
            principal["identifier_namespace"],
            principal.get("authority"),
            principal.get("native_id"),
            principal["kind"],
            principal.get("display_name"),
            principal.get("login_name"),
            principal.get("email"),
            principal["resolution"],
            principal.get("resolution_source"),
            json.dumps(principal.get("aliases") or []),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("principal_key was reused for a different permission principal")
    return int(row[0])


def upsert_permission_entry(
    conn: psycopg.Connection,
    run_id: str,
    assessment_id: int,
    principal_id: int | None,
    rec: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO permission_entries (
            run_id, assessment_id, principal_id, entry_key, provider_entry_id,
            ordinal, entry_kind, entry_effect, normalized_rights, inherited_state,
            expiration_at, evidence_hash, provider_details
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
        ON CONFLICT (assessment_id, entry_key)
        DO UPDATE SET
            principal_id = COALESCE(EXCLUDED.principal_id, permission_entries.principal_id),
            provider_entry_id = COALESCE(EXCLUDED.provider_entry_id, permission_entries.provider_entry_id),
            ordinal = COALESCE(EXCLUDED.ordinal, permission_entries.ordinal),
            expiration_at = COALESCE(EXCLUDED.expiration_at, permission_entries.expiration_at)
        WHERE permission_entries.run_id = EXCLUDED.run_id
          AND permission_entries.evidence_hash = EXCLUDED.evidence_hash
        RETURNING id
        """,
        (
            run_id,
            assessment_id,
            principal_id,
            rec["entry_key"],
            rec.get("provider_entry_id"),
            rec.get("ordinal"),
            rec["entry_kind"],
            rec["effect"],
            json.dumps(rec.get("normalized_rights") or []),
            rec["inherited_state"],
            rec.get("expiration_at"),
            rec["evidence_hash"],
            json.dumps(rec.get("provider_details") or {}),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("entry_key was reused with different permission evidence")
    return int(row[0])


def flush_permission_entry_batch(
    conn: psycopg.Connection,
    run_id: str,
    rows: list[tuple[int, int | None, dict[str, Any]]],
) -> None:
    """Persist normalized entries set-wise while retaining collision checks."""

    if not rows:
        return
    payload = [
        {
            "assessment_id": assessment_id,
            "principal_id": principal_id,
            "entry_key": rec["entry_key"],
            "provider_entry_id": rec.get("provider_entry_id"),
            "ordinal": rec.get("ordinal"),
            "entry_kind": rec["entry_kind"],
            "entry_effect": rec["effect"],
            "normalized_rights": rec.get("normalized_rights") or [],
            "inherited_state": rec["inherited_state"],
            "expiration_at": rec["expiration_at"].isoformat() if rec.get("expiration_at") else None,
            "evidence_hash": rec["evidence_hash"],
            "provider_details": rec.get("provider_details") or {},
        }
        for assessment_id, principal_id, rec in rows
    ]
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    collision = conn.execute(
        """
        WITH incoming AS (
            SELECT *
            FROM jsonb_to_recordset(%s::jsonb) AS entry(
                assessment_id bigint,
                entry_key text,
                evidence_hash text
            )
        ),
        collisions AS (
            SELECT assessment_id, entry_key
            FROM incoming
            GROUP BY assessment_id, entry_key
            HAVING COUNT(DISTINCT evidence_hash) > 1
            UNION ALL
            SELECT incoming.assessment_id, incoming.entry_key
            FROM incoming
            JOIN permission_entries AS existing
              ON existing.assessment_id = incoming.assessment_id
             AND existing.entry_key = incoming.entry_key
            WHERE existing.run_id <> %s
               OR existing.evidence_hash <> incoming.evidence_hash
        )
        SELECT 1 FROM collisions LIMIT 1
        """,
        (serialized, run_id),
    ).fetchone()
    if collision is not None:
        raise ValueError("entry_key was reused with different permission evidence")

    conn.execute(
        """
        WITH incoming AS (
            SELECT *
            FROM jsonb_to_recordset(%s::jsonb) AS entry(
                assessment_id bigint,
                principal_id bigint,
                entry_key text,
                provider_entry_id text,
                ordinal integer,
                entry_kind text,
                entry_effect text,
                normalized_rights jsonb,
                inherited_state text,
                expiration_at text,
                evidence_hash text,
                provider_details jsonb
            )
        ),
        deduplicated AS (
            SELECT DISTINCT ON (assessment_id, entry_key) *
            FROM incoming
            ORDER BY assessment_id, entry_key
        )
        INSERT INTO permission_entries (
            run_id, assessment_id, principal_id, entry_key, provider_entry_id,
            ordinal, entry_kind, entry_effect, normalized_rights, inherited_state,
            expiration_at, evidence_hash, provider_details
        )
        SELECT
            %s, assessment_id, principal_id, entry_key, provider_entry_id,
            ordinal, entry_kind, entry_effect, normalized_rights, inherited_state,
            NULLIF(expiration_at, '')::timestamptz, evidence_hash, provider_details
        FROM deduplicated
        ON CONFLICT (assessment_id, entry_key)
        DO UPDATE SET
            principal_id = COALESCE(EXCLUDED.principal_id, permission_entries.principal_id),
            provider_entry_id = COALESCE(EXCLUDED.provider_entry_id, permission_entries.provider_entry_id),
            ordinal = COALESCE(EXCLUDED.ordinal, permission_entries.ordinal),
            expiration_at = COALESCE(EXCLUDED.expiration_at, permission_entries.expiration_at)
        WHERE permission_entries.run_id = EXCLUDED.run_id
          AND permission_entries.evidence_hash = EXCLUDED.evidence_hash
        """,
        (serialized, run_id),
    )
    rows.clear()


def reconcile_permission_evidence_integrity(conn: psycopg.Connection, run_id: str) -> None:
    """Verify persisted entry cardinality and derive a consumer-owned set hash.

    Artifacts are input, not authority. A malformed or truncated entry record
    must not leave its parent assessment looking complete or suitable for a
    negative conclusion. The normalized hash intentionally uses only the
    persisted evidence hashes and preserves duplicates.
    """

    conn.execute(
        """
        WITH classified_entries AS (
            SELECT
                entry.assessment_id,
                entry.id AS entry_id,
                entry.evidence_hash,
                (
                    entry.principal_id IS NULL
                    AND NOT (
                        assessment.provider = 'sharepoint'
                        AND assessment.semantics = 'sharepoint_graph_permission_v1'
                        AND assessment.permission_surface = 'sharepoint_graph_permissions'
                        AND entry.entry_kind IN ('link', 'invitation')
                    )
                ) AS unresolved_principal,
                NOT (
                    (
                        assessment.provider = 'smb'
                        AND assessment.semantics = 'smb_windows_acl_v1'
                        AND assessment.permission_surface IN ('smb_filesystem_dacl', 'smb_share_acl')
                        AND entry.entry_kind = 'ace'
                    )
                    OR (
                        assessment.provider = 'sharepoint'
                        AND assessment.semantics = 'sharepoint_graph_permission_v1'
                        AND assessment.permission_surface = 'sharepoint_graph_permissions'
                        AND entry.entry_kind IN ('identity_grant', 'link', 'invitation')
                    )
                ) AS invalid_entry_kind
            FROM permission_entries AS entry
            JOIN permission_assessments AS assessment
              ON assessment.id = entry.assessment_id
             AND assessment.run_id = entry.run_id
            WHERE entry.run_id = %s
        ),
        persisted AS (
            SELECT
                assessment.id AS assessment_id,
                COUNT(entry.entry_id)::integer AS actual_entries,
                COUNT(entry.entry_id) FILTER (WHERE entry.unresolved_principal)::integer
                    AS unresolved_principal_entries,
                COUNT(entry.entry_id) FILTER (WHERE entry.invalid_entry_kind)::integer
                    AS invalid_entry_kind_entries,
                COUNT(entry.entry_id) FILTER (
                    WHERE entry.unresolved_principal OR entry.invalid_entry_kind
                )::integer AS noncomparable_entries,
                encode(
                    sha256(
                        convert_to(
                            COALESCE(
                                string_agg(
                                    entry.evidence_hash,
                                    E'\\n' ORDER BY entry.evidence_hash, entry.entry_id
                                ),
                                ''
                            ),
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS normalized_entries_hash
            FROM permission_assessments AS assessment
            LEFT JOIN classified_entries AS entry
              ON entry.assessment_id = assessment.id
            WHERE assessment.run_id = %s
            GROUP BY assessment.id
        ),
        integrity AS (
            SELECT
                assessment.id AS assessment_id,
                persisted.actual_entries,
                persisted.unresolved_principal_entries,
                persisted.invalid_entry_kind_entries,
                persisted.noncomparable_entries,
                encode(
                    sha256(
                        convert_to(
                            COALESCE(assessment.evidence_hash, '-') || E'\\n' || persisted.normalized_entries_hash,
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS normalized_entry_set_hash,
                persisted.actual_entries = assessment.entries_emitted AS counts_match,
                assessment.entry_set_hash IS NOT NULL
                    AND assessment.evidence_hash IS NOT NULL
                    AND assessment.assessment_state = 'complete'
                    AND assessment.retrieval_coverage IN ('complete', 'full', 'all_returned')
                    AND persisted.actual_entries = assessment.entries_emitted
                    AND assessment.entries_observed = assessment.entries_emitted
                    AND assessment.entries_omitted = 0
                    AND assessment.unknown_entries = 0
                    AND persisted.noncomparable_entries = 0
                    AS hash_eligible,
                assessment.assessment_state = 'complete'
                    AND assessment.entry_set_hash IS NOT NULL
                    AND assessment.evidence_hash IS NOT NULL
                    AND assessment.retrieval_coverage IN ('complete', 'full', 'all_returned')
                    AND persisted.actual_entries = assessment.entries_emitted
                    AND assessment.entries_observed = assessment.entries_emitted
                    AND assessment.entries_omitted = 0
                    AND assessment.unknown_entries = 0
                    AND persisted.noncomparable_entries = 0
                    AND assessment.provider = 'smb'
                    AND assessment.semantics = 'smb_windows_acl_v1'
                    AND assessment.permission_surface = 'smb_filesystem_dacl'
                    AND assessment.method = 'smb_query_security_info_read_control'
                    AND assessment.effective_access_status = 'not_computed'
                    AS negative_invariant
            FROM permission_assessments AS assessment
            JOIN persisted ON persisted.assessment_id = assessment.id
            WHERE assessment.run_id = %s
        )
        UPDATE permission_assessments AS assessment
        SET entry_set_hash = CASE
                WHEN integrity.hash_eligible THEN integrity.normalized_entry_set_hash
                ELSE NULL
            END,
            assessment_state = CASE
                WHEN (NOT integrity.counts_match OR integrity.noncomparable_entries > 0)
                     AND assessment.assessment_state = 'complete'
                THEN 'partial'
                ELSE assessment.assessment_state
            END,
            retrieval_coverage = CASE
                WHEN (NOT integrity.counts_match OR integrity.noncomparable_entries > 0)
                     AND assessment.retrieval_coverage IN ('complete', 'full', 'all_returned')
                THEN 'partial'
                ELSE assessment.retrieval_coverage
            END,
            unknown_entries = GREATEST(
                assessment.unknown_entries,
                integrity.noncomparable_entries
            ),
            negative_conclusion_supported = assessment.negative_conclusion_supported
                AND integrity.negative_invariant,
            error_code = CASE
                WHEN NOT integrity.counts_match
                THEN COALESCE(assessment.error_code, 'INGESTED_ENTRY_COUNT_MISMATCH')
                WHEN integrity.invalid_entry_kind_entries > 0
                THEN COALESCE(assessment.error_code, 'INGESTED_ENTRY_KIND_INVALID')
                WHEN integrity.unresolved_principal_entries > 0
                THEN COALESCE(assessment.error_code, 'INGESTED_ENTRY_PRINCIPAL_UNRESOLVED')
                ELSE assessment.error_code
            END,
            errors = assessment.errors
                || CASE
                    WHEN NOT integrity.counts_match
                     AND NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(assessment.errors) AS existing_error(value)
                        WHERE existing_error.value->>'code' = 'INGESTED_ENTRY_COUNT_MISMATCH'
                     )
                    THEN jsonb_build_array(
                    jsonb_build_object(
                        'code', 'INGESTED_ENTRY_COUNT_MISMATCH',
                        'message', 'Assessment declared '
                            || assessment.entries_emitted::text
                            || ' emitted entries but '
                            || integrity.actual_entries::text
                            || ' valid entries were persisted.'
                    )
                    )
                    ELSE '[]'::jsonb
                END
                || CASE
                    WHEN integrity.invalid_entry_kind_entries > 0
                     AND NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(assessment.errors) AS existing_error(value)
                        WHERE existing_error.value->>'code' = 'INGESTED_ENTRY_KIND_INVALID'
                     )
                    THEN jsonb_build_array(
                        jsonb_build_object(
                            'code', 'INGESTED_ENTRY_KIND_INVALID',
                            'message', integrity.invalid_entry_kind_entries::text
                                || ' persisted permission entries used an entry kind outside the reviewed provider contract.'
                        )
                    )
                    ELSE '[]'::jsonb
                END
                || CASE
                    WHEN integrity.unresolved_principal_entries > 0
                     AND NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(assessment.errors) AS existing_error(value)
                        WHERE existing_error.value->>'code' = 'INGESTED_ENTRY_PRINCIPAL_UNRESOLVED'
                     )
                    THEN jsonb_build_array(
                        jsonb_build_object(
                            'code', 'INGESTED_ENTRY_PRINCIPAL_UNRESOLVED',
                            'message', integrity.unresolved_principal_entries::text
                                || ' persisted permission entries did not have a stable provider principal identity.'
                        )
                    )
                    ELSE '[]'::jsonb
                END,
            summary = assessment.summary || jsonb_build_object(
                'entry_integrity', CASE
                    WHEN NOT integrity.counts_match THEN 'mismatch'
                    WHEN integrity.invalid_entry_kind_entries > 0 THEN 'entry_kind_invalid'
                    WHEN integrity.unresolved_principal_entries > 0 THEN 'principal_unresolved'
                    ELSE 'verified'
                END,
                'persisted_entries', integrity.actual_entries,
                'unresolved_principal_entries', integrity.unresolved_principal_entries,
                'invalid_entry_kind_entries', integrity.invalid_entry_kind_entries,
                'noncomparable_entries', integrity.noncomparable_entries,
                'entry_set_hash', CASE
                    WHEN integrity.hash_eligible THEN integrity.normalized_entry_set_hash
                    ELSE NULL
                END
            )
        FROM integrity
        WHERE assessment.id = integrity.assessment_id
        """,
        (run_id, run_id, run_id),
    )


def reconcile_permission_summaries(conn: psycopg.Connection, run_id: str) -> None:
    """Derive bounded summaries from normalized evidence in set-based queries."""

    conn.execute(
        """
        WITH classified_entries AS (
            SELECT
                entry.assessment_id,
                (
                    entry.principal_id IS NULL
                    AND NOT (
                        assessment.provider = 'sharepoint'
                        AND assessment.semantics = 'sharepoint_graph_permission_v1'
                        AND assessment.permission_surface = 'sharepoint_graph_permissions'
                        AND entry.entry_kind IN ('link', 'invitation')
                    )
                ) AS unresolved_principal,
                NOT (
                    (
                        assessment.provider = 'smb'
                        AND assessment.semantics = 'smb_windows_acl_v1'
                        AND assessment.permission_surface IN ('smb_filesystem_dacl', 'smb_share_acl')
                        AND entry.entry_kind = 'ace'
                    )
                    OR (
                        assessment.provider = 'sharepoint'
                        AND assessment.semantics = 'sharepoint_graph_permission_v1'
                        AND assessment.permission_surface = 'sharepoint_graph_permissions'
                        AND entry.entry_kind IN ('identity_grant', 'link', 'invitation')
                    )
                ) AS invalid_entry_kind
            FROM permission_entries AS entry
            JOIN permission_assessments AS assessment
              ON assessment.id = entry.assessment_id
             AND assessment.run_id = entry.run_id
            WHERE entry.run_id = %s
        ),
        persisted AS (
            SELECT
                assessment_id,
                COUNT(*)::bigint AS entry_count,
                COUNT(*) FILTER (WHERE unresolved_principal)::bigint
                    AS unresolved_principal_entry_count,
                COUNT(*) FILTER (WHERE invalid_entry_kind)::bigint
                    AS invalid_entry_kind_count,
                COUNT(*) FILTER (WHERE unresolved_principal OR invalid_entry_kind)::bigint
                    AS noncomparable_entry_count
            FROM classified_entries
            GROUP BY assessment_id
        ),
        evidence AS (
            SELECT
                resource_id,
                COUNT(*)::integer AS assessment_count,
                COALESCE(SUM(persisted.entry_count), 0)::bigint AS entry_count,
                COALESCE(SUM(entries_emitted), 0)::bigint AS declared_entry_count,
                COALESCE(SUM(persisted.unresolved_principal_entry_count), 0)::bigint
                    AS unresolved_principal_entry_count,
                COALESCE(SUM(persisted.invalid_entry_kind_count), 0)::bigint
                    AS invalid_entry_kind_count,
                COALESCE(SUM(persisted.noncomparable_entry_count), 0)::bigint
                    AS noncomparable_entry_count,
                BOOL_AND(
                    assessment_state = 'complete'
                    AND retrieval_coverage IN ('complete', 'full', 'all_returned')
                    AND entry_set_hash IS NOT NULL
                    AND entries_observed = entries_emitted
                    AND COALESCE(persisted.entry_count, 0) = entries_emitted
                    AND COALESCE(persisted.noncomparable_entry_count, 0) = 0
                    AND entries_omitted = 0
                    AND unknown_entries = 0
                ) AS comparable,
                BOOL_AND(negative_conclusion_supported)
                    AND BOOL_AND(
                        selection_scope IN ('root', 'share_root', 'resource_root', 'library_root', 'complete')
                    ) AS negative_supported,
                BOOL_AND(
                    negative_conclusion_supported
                    AND selection_coverage IN (
                        'exhaustive_for_scope', 'exhaustive_for_declared_scope',
                        'complete', 'full', 'all_returned'
                    )
                ) AS scope_exact,
                encode(
                    sha256(
                        convert_to(
                            string_agg(
                                encode(
                                    sha256(
                                        convert_to(
                                            jsonb_build_array(
                                                subject_key,
                                                semantics,
                                                permission_surface,
                                                entry_set_hash,
                                                entries_emitted
                                            )::text,
                                            'UTF8'
                                        )
                                    ),
                                    'hex'
                                ),
                                E'\n' ORDER BY subject_key, semantics, permission_surface
                            ),
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS comparison_evidence_hash,
                encode(
                    sha256(
                        convert_to(
                            string_agg(
                                encode(
                                    sha256(
                                        convert_to(
                                            jsonb_build_array(
                                                subject_key,
                                                semantics,
                                                permission_surface,
                                                method,
                                                assessment_state,
                                                selection_scope,
                                                selection_coverage,
                                                retrieval_coverage,
                                                provider_visibility,
                                                semantic_coverage,
                                                principal_resolution,
                                                entries_omitted,
                                                unknown_entries,
                                                provider_details->>'assessed_identity_fingerprint'
                                            )::text,
                                            'UTF8'
                                        )
                                    ),
                                    'hex'
                                ),
                                E'\n' ORDER BY subject_key, semantics, permission_surface
                            ),
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS comparison_quality_hash,
                BOOL_AND(assessment_state = 'complete') AS all_complete,
                BOOL_OR(assessment_state IN ('complete', 'partial')) AS any_observed,
                ARRAY_AGG(DISTINCT semantics ORDER BY semantics) AS semantics,
                ARRAY_AGG(DISTINCT permission_surface ORDER BY permission_surface) AS surfaces,
                MAX(observed_at) AS observed_at
            FROM permission_assessments AS assessment
            LEFT JOIN persisted ON persisted.assessment_id = assessment.id
            WHERE assessment.run_id = %s
            GROUP BY resource_id
        )
        UPDATE resources AS resource
        SET permission_summary = jsonb_build_object(
            'evidence_available', TRUE,
            'status', CASE
                WHEN evidence.all_complete THEN 'complete'
                WHEN evidence.any_observed THEN 'partial'
                ELSE 'failed'
            END,
            'assessment_count', evidence.assessment_count,
            'entry_count', evidence.entry_count,
            'declared_entry_count', evidence.declared_entry_count,
            'unresolved_principal_entry_count', evidence.unresolved_principal_entry_count,
            'invalid_entry_kind_count', evidence.invalid_entry_kind_count,
            'noncomparable_entry_count', evidence.noncomparable_entry_count,
            'comparable', evidence.comparable,
            'negative_conclusion_supported', evidence.negative_supported,
            'scope_exact', evidence.scope_exact,
            'comparison_evidence_hash', evidence.comparison_evidence_hash,
            'comparison_quality_hash', evidence.comparison_quality_hash,
            'semantics', to_jsonb(evidence.semantics),
            'permission_surfaces', to_jsonb(evidence.surfaces),
            'observed_at', evidence.observed_at
        )
        FROM evidence
        WHERE resource.id = evidence.resource_id AND resource.run_id = %s
        """,
        (run_id, run_id, run_id),
    )
    conn.execute(
        """
        WITH classified_entries AS (
            SELECT
                entry.assessment_id,
                (
                    entry.principal_id IS NULL
                    AND NOT (
                        assessment.provider = 'sharepoint'
                        AND assessment.semantics = 'sharepoint_graph_permission_v1'
                        AND assessment.permission_surface = 'sharepoint_graph_permissions'
                        AND entry.entry_kind IN ('link', 'invitation')
                    )
                ) AS unresolved_principal,
                NOT (
                    (
                        assessment.provider = 'smb'
                        AND assessment.semantics = 'smb_windows_acl_v1'
                        AND assessment.permission_surface IN ('smb_filesystem_dacl', 'smb_share_acl')
                        AND entry.entry_kind = 'ace'
                    )
                    OR (
                        assessment.provider = 'sharepoint'
                        AND assessment.semantics = 'sharepoint_graph_permission_v1'
                        AND assessment.permission_surface = 'sharepoint_graph_permissions'
                        AND entry.entry_kind IN ('identity_grant', 'link', 'invitation')
                    )
                ) AS invalid_entry_kind
            FROM permission_entries AS entry
            JOIN permission_assessments AS assessment
              ON assessment.id = entry.assessment_id
             AND assessment.run_id = entry.run_id
            WHERE entry.run_id = %s
        ),
        persisted AS (
            SELECT
                assessment_id,
                COUNT(*)::bigint AS entry_count,
                COUNT(*) FILTER (WHERE unresolved_principal)::bigint
                    AS unresolved_principal_entry_count,
                COUNT(*) FILTER (WHERE invalid_entry_kind)::bigint
                    AS invalid_entry_kind_count,
                COUNT(*) FILTER (WHERE unresolved_principal OR invalid_entry_kind)::bigint
                    AS noncomparable_entry_count
            FROM classified_entries
            GROUP BY assessment_id
        ),
        evidence AS (
            SELECT
                item_id,
                COUNT(*)::integer AS assessment_count,
                COALESCE(SUM(persisted.entry_count), 0)::bigint AS entry_count,
                COALESCE(SUM(entries_emitted), 0)::bigint AS declared_entry_count,
                COALESCE(SUM(persisted.unresolved_principal_entry_count), 0)::bigint
                    AS unresolved_principal_entry_count,
                COALESCE(SUM(persisted.invalid_entry_kind_count), 0)::bigint
                    AS invalid_entry_kind_count,
                COALESCE(SUM(persisted.noncomparable_entry_count), 0)::bigint
                    AS noncomparable_entry_count,
                BOOL_AND(
                    assessment_state = 'complete'
                    AND retrieval_coverage IN ('complete', 'full', 'all_returned')
                    AND entry_set_hash IS NOT NULL
                    AND entries_observed = entries_emitted
                    AND COALESCE(persisted.entry_count, 0) = entries_emitted
                    AND COALESCE(persisted.noncomparable_entry_count, 0) = 0
                    AND entries_omitted = 0
                    AND unknown_entries = 0
                ) AS comparable,
                encode(
                    sha256(
                        convert_to(
                            string_agg(
                                encode(
                                    sha256(
                                        convert_to(
                                            jsonb_build_array(
                                                subject_key,
                                                semantics,
                                                permission_surface,
                                                entry_set_hash,
                                                entries_emitted
                                            )::text,
                                            'UTF8'
                                        )
                                    ),
                                    'hex'
                                ),
                                E'\n' ORDER BY subject_key, semantics, permission_surface
                            ),
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS comparison_evidence_hash,
                encode(
                    sha256(
                        convert_to(
                            string_agg(
                                encode(
                                    sha256(
                                        convert_to(
                                            jsonb_build_array(
                                                subject_key,
                                                semantics,
                                                permission_surface,
                                                method,
                                                assessment_state,
                                                selection_scope,
                                                selection_coverage,
                                                retrieval_coverage,
                                                provider_visibility,
                                                semantic_coverage,
                                                principal_resolution,
                                                entries_omitted,
                                                unknown_entries,
                                                provider_details->>'assessed_identity_fingerprint'
                                            )::text,
                                            'UTF8'
                                        )
                                    ),
                                    'hex'
                                ),
                                E'\n' ORDER BY subject_key, semantics, permission_surface
                            ),
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS comparison_quality_hash,
                BOOL_AND(negative_conclusion_supported) AS negative_supported,
                BOOL_AND(assessment_state = 'complete') AS all_complete,
                BOOL_OR(assessment_state IN ('complete', 'partial')) AS any_observed,
                ARRAY_AGG(DISTINCT semantics ORDER BY semantics) AS semantics,
                ARRAY_AGG(DISTINCT permission_surface ORDER BY permission_surface) AS surfaces,
                MAX(observed_at) AS observed_at
            FROM permission_assessments AS assessment
            LEFT JOIN persisted ON persisted.assessment_id = assessment.id
            WHERE assessment.run_id = %s AND item_id IS NOT NULL
            GROUP BY item_id
        )
        UPDATE items AS item
        SET permission_summary = jsonb_build_object(
            'evidence_available', TRUE,
            'status', CASE
                WHEN evidence.all_complete THEN 'complete'
                WHEN evidence.any_observed THEN 'partial'
                ELSE 'failed'
            END,
            'assessment_count', evidence.assessment_count,
            'entry_count', evidence.entry_count,
            'declared_entry_count', evidence.declared_entry_count,
            'unresolved_principal_entry_count', evidence.unresolved_principal_entry_count,
            'invalid_entry_kind_count', evidence.invalid_entry_kind_count,
            'noncomparable_entry_count', evidence.noncomparable_entry_count,
            'comparable', evidence.comparable,
            'comparison_evidence_hash', evidence.comparison_evidence_hash,
            'comparison_quality_hash', evidence.comparison_quality_hash,
            'negative_conclusion_supported', evidence.negative_supported,
            'semantics', to_jsonb(evidence.semantics),
            'permission_surfaces', to_jsonb(evidence.surfaces),
            'observed_at', evidence.observed_at
        )
        FROM evidence
        WHERE item.id = evidence.item_id AND item.run_id = %s
        """,
        (run_id, run_id, run_id),
    )


def _permission_collection_integrity(
    collection_context: dict[str, Any],
    *,
    assessment_count: int,
    assessed_resource_count: int,
    incomplete_assessment_count: int,
    relevant_resource_count: int,
    rejected_record_count: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive permission coverage from evidence the consumer actually persisted.

    Producer coverage is a necessary input, never the authority. This keeps an
    omitted, malformed, or truncated permission stream from turning into an
    exact negative conclusion merely because its final run metadata claimed
    completion.
    """

    context = dict(collection_context) if isinstance(collection_context, dict) else {}
    metadata = dict(context.get("metadata")) if isinstance(context.get("metadata"), dict) else {}
    producer_summary = (
        metadata.get("permission_assessment") if isinstance(metadata.get("permission_assessment"), dict) else {}
    )
    raw_expected_objects = producer_summary.get("candidate_objects")
    expected_objects = (
        int(raw_expected_objects)
        if isinstance(raw_expected_objects, int)
        and not isinstance(raw_expected_objects, bool)
        and raw_expected_objects >= 0
        else None
    )

    producer_complete = metadata.get("permissions_complete") is True
    resource_coverage_complete = relevant_resource_count == 0 or assessed_resource_count >= relevant_resource_count
    # A producer-declared candidate count is a framing invariant, not a lower
    # bound. Extra persisted subjects can indicate duplicated or scope-drifted
    # records just as missing subjects indicate truncation.
    object_coverage_complete = expected_objects is None or assessment_count == expected_objects
    permission_assessed = assessment_count > 0
    permission_complete = bool(
        producer_complete
        and rejected_record_count == 0
        and incomplete_assessment_count == 0
        and resource_coverage_complete
        and object_coverage_complete
        and (relevant_resource_count == 0 or permission_assessed)
    )

    reasons: list[str] = []
    if not producer_complete:
        reasons.append("producer_did_not_declare_complete")
    if rejected_record_count:
        reasons.append("permission_records_rejected")
    if incomplete_assessment_count:
        reasons.append("assessment_integrity_incomplete")
    if not resource_coverage_complete:
        reasons.append("resource_coverage_incomplete")
    if not object_coverage_complete:
        reasons.append("declared_object_coverage_incomplete")

    integrity = {
        "contract_version": 1,
        "status": "verified_complete" if permission_complete else "incomplete",
        "producer_declared_complete": producer_complete,
        "assessments_persisted": assessment_count,
        "resources_assessed": assessed_resource_count,
        "resources_expected": relevant_resource_count,
        "incomplete_assessments": incomplete_assessment_count,
        "rejected_records": rejected_record_count,
        "expected_objects": expected_objects,
        "reasons": reasons,
    }
    metadata["permissions_assessed"] = permission_assessed
    metadata["permissions_complete"] = permission_complete
    metadata["permission_ingest"] = integrity
    context["metadata"] = metadata
    return context, integrity


def reconcile_permission_collection_context(conn: psycopg.Connection, run_id: str) -> dict[str, Any]:
    """Persist consumer-owned permission coverage and return its diagnostics."""

    row = conn.execute(
        """
        WITH assessment_metrics AS (
            SELECT
                COUNT(*)::integer AS assessment_count,
                COUNT(DISTINCT resource_id)::integer AS assessed_resource_count,
                COUNT(*) FILTER (
                    WHERE assessment_state <> 'complete'
                       OR retrieval_coverage NOT IN ('complete', 'full', 'all_returned')
                       OR entry_set_hash IS NULL
                       OR entries_observed <> entries_emitted
                       OR entries_omitted <> 0
                       OR unknown_entries <> 0
                )::integer AS incomplete_assessment_count
            FROM permission_assessments
            WHERE run_id = %s
        )
        SELECT
            run.collection_context,
            assessment_metrics.assessment_count,
            assessment_metrics.assessed_resource_count,
            assessment_metrics.incomplete_assessment_count,
            (
                SELECT COUNT(*)::integer
                FROM resources AS resource
                WHERE resource.run_id = %s
                  AND COALESCE(
                      resource.provider,
                      split_part(resource.resource_type::text, '_', 1)
                  ) IN ('smb', 'sharepoint')
            ) AS relevant_resource_count,
            (
                SELECT COUNT(*)::integer
                FROM ingest_errors AS ingest_error
                WHERE ingest_error.run_id = %s
                  AND ingest_error.code = 'PERMISSION_EVIDENCE_INVALID'
            ) AS rejected_record_count
        FROM scan_runs AS run
        CROSS JOIN assessment_metrics
        WHERE run.id = %s
        FOR UPDATE OF run
        """,
        (run_id, run_id, run_id, run_id),
    ).fetchone()
    if row is None:
        raise ValueError("run disappeared while reconciling permission evidence")

    context, integrity = _permission_collection_integrity(
        row[0] if isinstance(row[0], dict) else {},
        assessment_count=int(row[1] or 0),
        assessed_resource_count=int(row[2] or 0),
        incomplete_assessment_count=int(row[3] or 0),
        relevant_resource_count=int(row[4] or 0),
        rejected_record_count=int(row[5] or 0),
    )
    conn.execute(
        "UPDATE scan_runs SET collection_context = %s::jsonb WHERE id = %s",
        (json.dumps(context, ensure_ascii=True, separators=(",", ":")), run_id),
    )
    return integrity


def _validated_producer_inventory_counts(raw_stats: Any) -> dict[str, int] | None:
    """Return the producer's exact inventory cardinalities when well formed."""

    if not isinstance(raw_stats, dict):
        return None
    counts: dict[str, int] = {}
    for field in ("endpoints", "resources", "items"):
        value = raw_stats.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 2**63 - 1:
            return None
        counts[field] = value
    return counts


def _inventory_collection_integrity(
    collection_context: dict[str, Any],
    *,
    producer_counts: dict[str, int] | None,
    persisted_counts: dict[str, int],
    structural_rejected_records: int,
    content_rejected_records: int,
    unclassified_rejected_records: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail closed when the consumer could not persist the producer's inventory.

    Producer completeness remains necessary, but exact negative inventory
    conclusions additionally require every inventory record to validate and
    the terminal producer cardinalities to match the normalized rows. Keep the
    structural and content dimensions separate so one malformed item does not
    erase otherwise valid share/library presence observations.
    """

    context = dict(collection_context) if isinstance(collection_context, dict) else {}
    metadata = dict(context.get("metadata")) if isinstance(context.get("metadata"), dict) else {}
    persisted_inventory_counts = {
        field: max(0, int(persisted_counts.get(field, 0))) for field in ("endpoints", "resources", "items")
    }
    count_mismatches: dict[str, dict[str, int]] = {}
    if producer_counts is not None:
        for field in ("endpoints", "resources", "items"):
            producer_value = producer_counts[field]
            persisted_value = persisted_inventory_counts[field]
            if producer_value != persisted_value:
                count_mismatches[field] = {
                    "producer": producer_value,
                    "persisted": persisted_value,
                }

    structural_rejected_records = max(0, int(structural_rejected_records))
    content_rejected_records = max(0, int(content_rejected_records))
    unclassified_rejected_records = max(0, int(unclassified_rejected_records))
    producer_counts_valid = producer_counts is not None
    structural_integrity_verified = bool(
        producer_counts_valid
        and structural_rejected_records == 0
        and unclassified_rejected_records == 0
        and not ({"endpoints", "resources"} & count_mismatches.keys())
    )
    content_integrity_verified = bool(
        structural_integrity_verified and content_rejected_records == 0 and "items" not in count_mismatches
    )

    reasons: list[str] = []
    if not producer_counts_valid:
        reasons.append("producer_inventory_counts_missing_or_invalid")
    if structural_rejected_records:
        reasons.append("structural_records_rejected")
    if content_rejected_records:
        reasons.append("content_records_rejected")
    if unclassified_rejected_records:
        reasons.append("unclassified_artifact_records_rejected")
    reasons.extend(f"{field}_count_mismatch" for field in count_mismatches)

    integrity = {
        "contract_version": 1,
        "status": ("verified" if structural_integrity_verified and content_integrity_verified else "incomplete"),
        "structural_integrity_verified": structural_integrity_verified,
        "content_integrity_verified": content_integrity_verified,
        "producer_counts": producer_counts,
        "persisted_counts": persisted_inventory_counts,
        "count_mismatches": count_mismatches,
        "rejected_records": {
            "structural": structural_rejected_records,
            "content": content_rejected_records,
            "unclassified": unclassified_rejected_records,
        },
        "reasons": reasons,
    }
    # A consumer can only narrow producer truth claims. It must never promote
    # a producer-declared partial collection merely because row counts match.
    metadata["structural_complete"] = bool(
        metadata.get("structural_complete") is True and structural_integrity_verified
    )
    metadata["content_complete"] = bool(metadata.get("content_complete") is True and content_integrity_verified)
    metadata["inventory_ingest"] = integrity
    context["metadata"] = metadata
    return context, integrity


def reconcile_inventory_collection_context(
    conn: psycopg.Connection,
    run_id: str,
    *,
    producer_counts: dict[str, int] | None,
    persisted_counts: dict[str, int],
) -> dict[str, Any]:
    """Persist consumer-owned inventory completeness and diagnostics."""

    row = conn.execute(
        """
        SELECT
            run.collection_context,
            rejected.structural_rejected_records,
            rejected.content_rejected_records,
            rejected.unclassified_rejected_records
        FROM scan_runs AS run
        CROSS JOIN LATERAL (
            SELECT
                COUNT(*) FILTER (
                    WHERE ingest_error.code = %s
                )::integer AS structural_rejected_records,
                COUNT(*) FILTER (
                    WHERE ingest_error.code = %s
                )::integer AS content_rejected_records,
                COUNT(*) FILTER (
                    WHERE ingest_error.code = ANY(%s)
                )::integer AS unclassified_rejected_records
            FROM ingest_errors AS ingest_error
            WHERE ingest_error.run_id = run.id
        ) AS rejected
        WHERE run.id = %s
        FOR UPDATE OF run
        """,
        (
            CONSUMER_STRUCTURAL_RECORD_ERROR,
            CONSUMER_CONTENT_RECORD_ERROR,
            sorted(CONSUMER_UNCLASSIFIED_INVENTORY_ERROR_CODES),
            run_id,
        ),
    ).fetchone()
    if row is None:
        raise ValueError("run disappeared while reconciling inventory evidence")

    context, integrity = _inventory_collection_integrity(
        row[0] if isinstance(row[0], dict) else {},
        producer_counts=producer_counts,
        persisted_counts=persisted_counts,
        structural_rejected_records=int(row[1] or 0),
        content_rejected_records=int(row[2] or 0),
        unclassified_rejected_records=int(row[3] or 0),
    )
    conn.execute(
        "UPDATE scan_runs SET collection_context = %s::jsonb WHERE id = %s",
        (json.dumps(context, ensure_ascii=True, separators=(",", ":")), run_id),
    )
    return integrity


def prepare_run_identity_keys_batch(
    conn: psycopg.Connection,
    run_id: str,
    *,
    resource_after_id: int = 0,
    item_after_id: int = 0,
    limit: int = COMPARISON_ITEM_BATCH_SIZE,
) -> dict[str, int | bool]:
    """Populate one durable keyset batch without rewriting completed rows."""

    bounded_limit = max(1, min(int(limit), 20_000))
    resource_ids = [
        int(row[0])
        for row in conn.execute(
            """
            SELECT id
            FROM resources
            WHERE run_id = %s AND identity_key IS NULL AND id > %s
            ORDER BY id
            LIMIT %s
            """,
            (run_id, resource_after_id, bounded_limit),
        ).fetchall()
    ]
    if resource_ids:
        conn.execute(
            """
            UPDATE resources AS resource
            SET identity_key = encode(
                sha256(
                    convert_to(
                        CASE
                            WHEN resource.provider_resource_id IS NOT NULL THEN
                                jsonb_build_array(
                                    COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)),
                                    resource.resource_type::text,
                                    COALESCE(run.collection_context->>'tenant_id', ''),
                                    'provider_id',
                                    resource.provider_resource_id
                                )::text
                            ELSE
                                jsonb_build_array(
                                    COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)),
                                    resource.resource_type::text,
                                    COALESCE(run.collection_context->>'tenant_id', ''),
                                    'location',
                                    lower(endpoint.endpoint_key),
                                    CASE
                                        WHEN COALESCE(
                                            resource.provider,
                                            split_part(resource.resource_type::text, '_', 1)
                                        ) IN ('smb', 'sharepoint')
                                        THEN lower(resource.name)
                                        ELSE resource.name
                                    END
                                )::text
                        END,
                        'UTF8'
                    )
                ),
                'hex'
            )
            FROM endpoints AS endpoint, scan_runs AS run
            WHERE resource.run_id = %s
              AND resource.id = ANY(%s)
              AND resource.identity_key IS NULL
              AND endpoint.run_id = resource.run_id
              AND endpoint.id = resource.endpoint_id
              AND run.id = resource.run_id
            """,
            (run_id, resource_ids),
        )
        return {
            "resource_after_id": resource_ids[-1],
            "item_after_id": item_after_id,
            "resource_complete": False,
            "item_complete": False,
            "processed": len(resource_ids),
        }

    item_ids = [
        int(row[0])
        for row in conn.execute(
            """
            SELECT id
            FROM items
            WHERE run_id = %s AND identity_key IS NULL AND id > %s
            ORDER BY id
            LIMIT %s
            """,
            (run_id, item_after_id, bounded_limit),
        ).fetchall()
    ]
    if item_ids:
        conn.execute(
            """
            UPDATE items AS item
            SET identity_key = encode(
                sha256(
                    convert_to(
                        jsonb_build_array(
                            COALESCE(
                                item.provider,
                                resource.provider,
                                split_part(resource.resource_type::text, '_', 1)
                            ),
                            CASE WHEN item.provider_item_id IS NOT NULL THEN 'provider_id' ELSE 'path' END,
                            CASE
                                WHEN item.provider_item_id IS NOT NULL THEN item.provider_item_id
                                WHEN COALESCE(
                                    item.provider,
                                    resource.provider,
                                    split_part(resource.resource_type::text, '_', 1)
                                ) = 'smb' THEN lower(item.path)
                                ELSE item.path
                            END
                        )::text,
                        'UTF8'
                    )
                ),
                'hex'
            )
            FROM resources AS resource
            WHERE item.run_id = %s
              AND item.id = ANY(%s)
              AND item.identity_key IS NULL
              AND resource.run_id = item.run_id
              AND resource.id = item.resource_id
            """,
            (run_id, item_ids),
        )
    return {
        "resource_after_id": resource_after_id,
        "item_after_id": item_ids[-1] if item_ids else item_after_id,
        "resource_complete": True,
        "item_complete": not item_ids,
        "processed": len(item_ids),
    }


def prepare_run_identity_keys(
    conn: psycopg.Connection,
    run_id: str,
    *,
    commit_batches: bool = False,
) -> None:
    """Populate stable keys in bounded idempotent batches."""

    resource_after_id = 0
    item_after_id = 0
    while True:
        result = prepare_run_identity_keys_batch(
            conn,
            run_id,
            resource_after_id=resource_after_id,
            item_after_id=item_after_id,
        )
        resource_after_id = int(result["resource_after_id"])
        item_after_id = int(result["item_after_id"])
        if commit_batches:
            conn.execute(
                """
                UPDATE scan_runs
                SET ingest_progress = COALESCE(ingest_progress, '{}'::jsonb)
                    || jsonb_build_object('heartbeat_at', NOW(), 'identity_preparation_worker', %s::text)
                WHERE id = %s
                """,
                (CONSUMER_NAME, run_id),
            )
            conn.commit()
        if result["item_complete"] is True:
            return


def _canonical_monitoring_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_monitoring_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [_canonical_monitoring_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, str):
        return value.strip()
    return value


def _monitoring_provider(context: dict[str, Any]) -> str:
    raw = str(context.get("provider") or context.get("source") or "unknown")
    normalized = "+".join(
        sorted({part.strip().casefold() for part in raw.replace(",", "+").split("+") if part.strip()})
    )
    return (normalized or "unknown")[:32]


def _monitoring_target_scope(context: dict[str, Any], fallback: Any) -> dict[str, Any]:
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    collection = metadata.get("collection") if isinstance(metadata.get("collection"), dict) else {}
    target = collection.get("target_scope")
    if not isinstance(target, dict):
        target = fallback if isinstance(fallback, dict) else {}

    def credential_free(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): credential_free(item)
                for key, item in value.items()
                if not _is_forbidden_provider_metadata_key(str(key))
            }
        if isinstance(value, list):
            return [credential_free(item) for item in value]
        return value

    return _canonical_monitoring_value(credential_free(target))


def _monitoring_identity(context: dict[str, Any]) -> str | None:
    assessed = str(context.get("assessed_identity") or "").strip()
    if assessed:
        return assessed[:512]
    tenant_id = str(context.get("tenant_id") or "").strip()
    client_id = str(context.get("client_id") or "").strip()
    if tenant_id or client_id:
        return f"tenant={tenant_id or 'unknown'};client={client_id or 'unknown'}"[:512]
    auth_mode = str(context.get("auth_mode") or "").strip()
    return auth_mode[:512] or None


def _monitoring_coverage(context: dict[str, Any]) -> dict[str, Any]:
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    reasons: list[str] = []
    structural_complete = metadata.get("structural_complete") is True
    content_expected = metadata.get("files_included") is True
    content_complete = metadata.get("content_complete") is True
    partial = context.get("partial") is True
    if not structural_complete:
        reasons.append("Structural discovery was not declared complete.")
    if content_expected and not content_complete:
        reasons.append("Content enumeration was not declared complete.")
    if partial:
        reasons.append("The collector marked the snapshot partial.")
    if not context:
        reasons.append("Collection context was unavailable.")
    if reasons:
        state = "unknown" if not context else "partial"
    else:
        state = "complete"
    return {"state": state, "reasons": reasons}


def _monitoring_source_key(context: dict[str, Any], fallback_target_scope: dict[str, Any]) -> str | None:
    provider = _monitoring_provider(context)
    if not context or provider == "unknown":
        return None
    identity_payload = {
        "provider": provider,
        "graph_cloud": str(context.get("graph_cloud") or context.get("cloud_environment") or "")
        .strip()
        .casefold(),
        "target_scope": _monitoring_target_scope(context, fallback_target_scope),
        "assessed_identity": _monitoring_identity(context),
        "auth_mode": str(context.get("auth_mode") or "").strip().casefold(),
        "collection_mode": str(context.get("collection_mode") or "").strip().casefold(),
    }
    return hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def monitoring_source_advisory_lock_key(
    conn: psycopg.Connection,
    run_id: str,
    project_id: str,
) -> int | None:
    row = conn.execute(
        "SELECT target_scope, collection_context FROM scan_runs WHERE id = %s AND project_id = %s",
        (run_id, project_id),
    ).fetchone()
    if row is None:
        return None
    fallback_scope = dict(row[0]) if isinstance(row[0], dict) else {}
    context = dict(row[1]) if isinstance(row[1], dict) else {}
    source_key = _monitoring_source_key(context, fallback_scope)
    if source_key is None:
        return None
    return _monitoring_source_advisory_lock_key(source_key)


def _monitoring_source_advisory_lock_key(source_key: str) -> int:
    digest = hashlib.sha256(f"monitoring-source:{source_key}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % (2**63 - 1)


def monitoring_source_advisory_lock_key_from_id(
    conn: psycopg.Connection,
    source_id: str,
) -> int | None:
    row = conn.execute("SELECT source_key FROM collection_sources WHERE id = %s", (source_id,)).fetchone()
    return _monitoring_source_advisory_lock_key(str(row[0])) if row and row[0] else None


def _monitoring_source_display_name(provider: str, target_scope: dict[str, Any], run_name: str) -> str:
    targets: list[str] = []
    for field in ("targeted_sites", "hosts", "cidrs"):
        values = target_scope.get(field)
        if isinstance(values, list):
            targets.extend(str(value).strip() for value in values if str(value).strip())
    suffix = targets[0] if len(targets) == 1 else f"{len(targets)} targets" if targets else run_name
    return f"{provider.upper()} · {suffix}"[:255]


def _bounded_source_target_scope(target_scope: dict[str, Any]) -> dict[str, Any]:
    """Persist a deterministic response-safe summary while identity uses full scope."""

    canonical = _canonical_monitoring_value(target_scope)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    summary: dict[str, Any] = {}
    counts: dict[str, int] = {}
    truncated = len(encoded) > 4_096 or len(canonical) > 16

    def within_example_budget(candidate: dict[str, Any]) -> bool:
        return len(json.dumps(candidate, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) <= 4_096

    for key in sorted(canonical)[:16]:
        value = canonical[key]
        bounded_key = str(key)[:64]
        if isinstance(value, list):
            counts[bounded_key] = len(value)
            bounded_values: list[Any] = []
            for item in value[:20]:
                if isinstance(item, (str, int, float, bool)) or item is None:
                    candidate_item = item[:256] if isinstance(item, str) else item
                else:
                    candidate_item = {
                        "value_hash": hashlib.sha256(
                            json.dumps(item, sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest()
                    }
                candidate_values = [*bounded_values, candidate_item]
                if not within_example_budget({**summary, bounded_key: candidate_values}):
                    truncated = True
                    break
                bounded_values = candidate_values
            summary[bounded_key] = bounded_values
            truncated = truncated or len(value) > len(bounded_values)
        elif isinstance(value, dict):
            counts[bounded_key] = len(value)
            summary[bounded_key] = {
                "field_count": len(value),
                "value_hash": hashlib.sha256(
                    json.dumps(value, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            }
            truncated = True
        elif isinstance(value, str):
            candidate_value = value[:256]
            if within_example_budget({**summary, bounded_key: candidate_value}):
                summary[bounded_key] = candidate_value
            else:
                truncated = True
            truncated = truncated or len(value) > 256
        elif isinstance(value, (int, float, bool)) or value is None:
            if within_example_budget({**summary, bounded_key: value}):
                summary[bounded_key] = value
            else:
                truncated = True
        else:
            summary[bounded_key] = str(value)[:512]
            truncated = True
    summary["_scope_summary"] = {
        "scope_hash": hashlib.sha256(encoded).hexdigest(),
        "original_bytes": len(encoded),
        "top_level_field_count": len(canonical),
        "list_counts": counts,
        "truncated": truncated,
        "limitations": ["Target scope values are bounded examples; scope_hash identifies the full scope."]
        if truncated
        else [],
    }
    return summary


def _recompute_source_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    """Keep inventory, policy evaluation, and baseline coverage explicit."""

    inventory = coverage.get("inventory") if isinstance(coverage.get("inventory"), dict) else {}
    inventory_state = str(inventory.get("state") or coverage.get("state") or "unknown").casefold()
    inventory_reasons = [str(reason) for reason in inventory.get("reasons", []) if str(reason).strip()]
    findings = (
        coverage.get("monitoring_findings")
        if isinstance(coverage.get("monitoring_findings"), dict)
        else {"state": "unknown"}
    )
    baseline = (
        coverage.get("automatic_baseline")
        if isinstance(coverage.get("automatic_baseline"), dict)
        else {"state": "unknown"}
    )
    findings_state = str(findings.get("state") or "unknown").casefold()
    baseline_state = str(baseline.get("state") or "unknown").casefold()
    baseline_findings_state = str(baseline.get("findings_evaluation_state") or "complete").casefold()
    reasons = list(inventory_reasons)
    if findings_state != "complete":
        reasons.append(
            "Security finding evaluation is incomplete."
            if findings_state not in {"queued", "retrying", "evaluating"}
            else "Security finding evaluation is pending or retrying."
        )
    if baseline_state != "established":
        reasons.append("Automatic comparison coverage has not established a usable baseline.")
    elif baseline_findings_state != "complete":
        reasons.append("Automatic comparison finding evaluation is incomplete.")
    if inventory_state not in {"complete", "partial", "unknown"}:
        inventory_state = "unknown"
    coverage["state"] = (
        inventory_state
        if findings_state == "complete"
        and baseline_state == "established"
        and baseline_findings_state == "complete"
        else ("unknown" if inventory_state == "unknown" else "partial")
    )
    coverage["reasons"] = list(dict.fromkeys(reasons))
    return coverage


def update_collection_source_monitoring_coverage(
    conn: psycopg.Connection,
    *,
    source_id: str,
    run_id: str,
    findings: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> None:
    """Publish current successful-snapshot coverage while preserving attempt health."""

    row = conn.execute(
        """
        SELECT source.coverage, run.collection_context
        FROM collection_sources AS source
        JOIN scan_runs AS run ON run.id = %s
        WHERE source.id = %s AND run.source_id = source.id
        FOR UPDATE OF source
        """,
        (run_id, source_id),
    ).fetchone()
    if row is None:
        return
    coverage = dict(row[0]) if isinstance(row[0], dict) else {}
    context = dict(row[1]) if isinstance(row[1], dict) else {}
    inventory = _monitoring_coverage(context)
    coverage["inventory"] = inventory
    if findings is not None:
        coverage["monitoring_findings"] = {
            key: findings.get(key)
            for key in (
                "state",
                "phase",
                "attempt_count",
                "next_retry_at",
                "error_code",
                "reason",
                "observed",
                "resolved",
            )
            if key in findings
        } | {
            "run_id": run_id,
            "retryable": str(findings.get("state") or "") == "degraded",
        }
    if baseline is not None:
        coverage["automatic_baseline"] = baseline
    coverage = _recompute_source_coverage(coverage)
    conn.execute(
        "UPDATE collection_sources SET coverage = %s::jsonb, updated_at = NOW() WHERE id = %s",
        (json.dumps(coverage, ensure_ascii=True, separators=(",", ":")), source_id),
    )


def register_collection_source(
    conn: psycopg.Connection,
    run_id: str,
    project_id: str,
    *,
    succeeded: bool = True,
) -> str | None:
    """Idempotently register a credential-free collection source for a run."""

    row = conn.execute(
        """
        SELECT name, target_scope, collection_context, created_at
        FROM scan_runs
        WHERE id = %s AND project_id = %s
        FOR UPDATE
        """,
        (run_id, project_id),
    ).fetchone()
    if row is None or len(row) != 4:
        return None
    run_name, fallback_target_scope, raw_context, run_created_at = row
    context = dict(raw_context) if isinstance(raw_context, dict) else {}
    provider = _monitoring_provider(context)
    # Never guess source identity from a run name or an empty artifact. Doing
    # so would merge unrelated failures into a misleading health record.
    if not context or provider == "unknown":
        return None
    target_scope = _monitoring_target_scope(context, fallback_target_scope)
    persisted_target_scope = _bounded_source_target_scope(target_scope)
    assessed_identity = _monitoring_identity(context)
    source_key = _monitoring_source_key(context, target_scope)
    if source_key is None:
        return None
    source_id = str(uuid.uuid4())
    inventory_coverage = _monitoring_coverage(context)
    coverage = _recompute_source_coverage(
        {
            **inventory_coverage,
            "inventory": inventory_coverage,
            "monitoring_findings": {"state": "pending" if succeeded else "not_evaluated"},
            "automatic_baseline": {"state": "pending" if succeeded else "not_evaluated"},
            "last_terminal_status": "success" if succeeded else "failed",
        }
    )
    collector_version = str(context.get("tool_version") or "").strip()[:64] or None
    display_name = _monitoring_source_display_name(provider, target_scope, str(run_name or "collection"))
    persisted = conn.execute(
        """
        INSERT INTO collection_sources (
            id, project_id, source_key, display_name, provider,
            assessed_identity, target_scope, last_run_id, last_success_at,
            last_failure_at, collector_version, coverage, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
            CASE WHEN %s THEN %s::timestamptz END,
            CASE WHEN %s THEN NULL ELSE %s::timestamptz END,
            %s, %s::jsonb, NOW(), NOW()
        )
        ON CONFLICT (project_id, source_key) DO UPDATE SET
            provider = EXCLUDED.provider,
            assessed_identity = EXCLUDED.assessed_identity,
            target_scope = EXCLUDED.target_scope,
            last_run_id = EXCLUDED.last_run_id,
            last_success_at = CASE
                WHEN %s THEN EXCLUDED.last_success_at ELSE collection_sources.last_success_at
            END,
            last_failure_at = CASE
                WHEN %s THEN collection_sources.last_failure_at ELSE EXCLUDED.last_failure_at
            END,
            collector_version = COALESCE(EXCLUDED.collector_version, collection_sources.collector_version),
            coverage = EXCLUDED.coverage,
            updated_at = NOW()
        WHERE collection_sources.last_run_id IS NULL
           OR EXISTS (
                SELECT 1
                FROM scan_runs AS incoming_run
                LEFT JOIN scan_runs AS previous_run ON previous_run.id = collection_sources.last_run_id
                WHERE incoming_run.id = EXCLUDED.last_run_id
                  AND (
                        previous_run.id IS NULL
                        OR (incoming_run.created_at, incoming_run.id)
                           >= (previous_run.created_at, previous_run.id)
                  )
           )
        RETURNING id::text
        """,
        (
            source_id,
            project_id,
            source_key,
            display_name,
            provider,
            assessed_identity,
            json.dumps(persisted_target_scope, ensure_ascii=True, separators=(",", ":")),
            run_id,
            succeeded,
            run_created_at,
            succeeded,
            run_created_at,
            collector_version,
            json.dumps(coverage, ensure_ascii=True, separators=(",", ":")),
            succeeded,
            succeeded,
        ),
    ).fetchone()
    latest_state_updated = persisted is not None
    if persisted is None:
        persisted = conn.execute(
            "SELECT id::text FROM collection_sources WHERE project_id = %s AND source_key = %s",
            (project_id, source_key),
        ).fetchone()
        if persisted is None:
            return None
    source_id = str(persisted[0])
    timestamp_column = "last_success_at" if succeeded else "last_failure_at"
    conn.execute(
        f"""
        UPDATE collection_sources
        SET {timestamp_column} = GREATEST(
                COALESCE({timestamp_column}, %s::timestamptz),
                %s::timestamptz
            ),
            updated_at = NOW()
        WHERE id = %s
        """,
        (run_created_at, run_created_at, source_id),
    )
    conn.execute("UPDATE scan_runs SET source_id = %s WHERE id = %s", (source_id, run_id))
    write_audit(
        conn,
        project_id,
        "COLLECTION_SOURCE_OBSERVED",
        "collection_source",
        source_id,
        {
            "worker": CONSUMER_NAME,
            "run_id": run_id,
            "provider": provider,
            "coverage": coverage,
            "terminal_status": "success" if succeeded else "failed",
            "latest_state_updated": latest_state_updated,
        },
    )
    return source_id


def collection_source_automation_enabled(conn: psycopg.Connection, source_id: str) -> bool:
    row = conn.execute("SELECT enabled FROM collection_sources WHERE id = %s", (source_id,)).fetchone()
    return bool(row and row[0] is True)


def collection_source_run_is_latest_complete_candidate(
    conn: psycopg.Connection,
    source_id: str,
    run_id: str,
) -> bool:
    row = conn.execute(
        """
        SELECT NOT EXISTS (
            SELECT 1
            FROM scan_runs AS newer_run
            JOIN scan_runs AS current_run ON current_run.id = %s
            WHERE newer_run.source_id = %s
              AND newer_run.status = 'COMPLETE'
              AND (newer_run.created_at, newer_run.id)
                  > (current_run.created_at, current_run.id)
        )
        """,
        (run_id, source_id),
    ).fetchone()
    return bool(row and row[0] is True)


def _automatic_comparison_signature(context: dict[str, Any]) -> dict[str, Any]:
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    collection = metadata.get("collection") if isinstance(metadata.get("collection"), dict) else {}
    return _canonical_monitoring_value(
        {
            "provider": _monitoring_provider(context),
            "graph_cloud": context.get("graph_cloud") or context.get("cloud_environment"),
            "collection_mode": context.get("collection_mode"),
            "auth_type": context.get("auth_type"),
            "auth_mode": context.get("auth_mode"),
            "tenant_id": context.get("tenant_id"),
            "client_id": context.get("client_id"),
            "assessed_identity": context.get("assessed_identity"),
            "scopes": context.get("scopes") or [],
            "roles": context.get("roles") or [],
            "target_scope": collection.get("target_scope"),
            "enumeration": collection.get("enumeration"),
            "comparison_contracts": metadata.get("comparison_contracts"),
        }
    )


def _automatic_comparison_compatibility(
    current_context: dict[str, Any],
    baseline_context: dict[str, Any],
) -> dict[str, Any] | None:
    if not current_context or not baseline_context:
        return None
    if _automatic_comparison_signature(current_context) != _automatic_comparison_signature(baseline_context):
        return None
    current_meta = current_context.get("metadata") if isinstance(current_context.get("metadata"), dict) else {}
    baseline_meta = baseline_context.get("metadata") if isinstance(baseline_context.get("metadata"), dict) else {}
    authoritative = {
        "authoritative",
        "complete",
        "complete_for_declared_scope",
        "complete_for_granted_scope",
        "full",
        "targeted_scope",
    }
    structural = all(
        context.get("materialized_snapshot") is True
        and str(context.get("discovery_completeness") or "").strip().casefold() in authoritative
        and metadata.get("structural_complete") is True
        for context, metadata in ((current_context, current_meta), (baseline_context, baseline_meta))
    )
    files_included = all(meta.get("files_included") is True for meta in (current_meta, baseline_meta))
    content = structural and files_included and all(
        meta.get("content_complete") is True for meta in (current_meta, baseline_meta)
    )
    permissions_assessed = all(meta.get("permissions_assessed") is True for meta in (current_meta, baseline_meta))
    permissions_complete = permissions_assessed and all(
        meta.get("permissions_complete") is True for meta in (current_meta, baseline_meta)
    )
    providers = set(_monitoring_provider(current_context).split("+"))
    supported_access = bool(providers) and providers.issubset({"smb", "sharepoint"})
    direct_permissions = permissions_complete and supported_access
    capability_applicable = providers == {"smb"}
    contracts = current_meta.get("comparison_contracts") if isinstance(current_meta.get("comparison_contracts"), dict) else {}
    capability = capability_applicable and bool(contracts.get("capability"))
    reasons: list[str] = []
    if not structural:
        reasons.append("Automatic baseline context does not support structural conclusions.")
    if not content:
        reasons.append("Automatic baseline context does not support item-level conclusions.")
    if not direct_permissions:
        reasons.append("Direct-permission evidence is incomplete or unavailable for the automatic baseline.")
    dimensions = (structural, content, direct_permissions, not capability_applicable or capability)
    return {
        "status": "compatible" if all(dimensions) else ("partial" if any(dimensions) else "incompatible"),
        "structural_interpretable": structural,
        "content_interpretable": content,
        "access_context_comparable": True,
        "access_provider_coverage_complete": supported_access,
        "capability_applicable": capability_applicable,
        "capability_provider_coverage_complete": capability_applicable,
        "smb_identity_required": "smb" in providers,
        "identity_applicable": "smb" in providers,
        "identity_scope_exact": "smb" not in providers,
        "unsupported_access_providers": sorted(providers - {"smb", "sharepoint"}),
        "access_interpretable": direct_permissions,
        "capability_interpretable": capability,
        "direct_permissions_assessed": permissions_assessed,
        "direct_permissions_complete": permissions_complete,
        "direct_permissions_interpretable": direct_permissions,
        "direct_permissions_scope_exact": False,
        "automatic_baseline": True,
        "reasons": reasons,
    }


def create_automatic_comparison(
    conn: psycopg.Connection,
    *,
    project_id: str,
    source_id: str,
    current_run_id: str,
) -> str | None:
    current_row = conn.execute(
        "SELECT collection_context, created_at FROM scan_runs "
        "WHERE id = %s AND project_id = %s AND status = 'COMPLETE'",
        (current_run_id, project_id),
    ).fetchone()
    if current_row is None:
        return None
    current_context = dict(current_row[0]) if isinstance(current_row[0], dict) else {}
    current_created_at = current_row[1]
    candidates = conn.execute(
        """
        SELECT id::text, collection_context
        FROM scan_runs
        WHERE project_id = %s
          AND source_id = %s
          AND id <> %s
          AND status = 'COMPLETE'
          AND (created_at, id) < (%s, %s::uuid)
        ORDER BY created_at DESC, id DESC
        LIMIT 20
        """,
        (project_id, source_id, current_run_id, current_created_at, current_run_id),
    ).fetchall()
    baseline_run_id: str | None = None
    compatibility: dict[str, Any] | None = None
    for candidate_id, raw_context in candidates:
        baseline_context = dict(raw_context) if isinstance(raw_context, dict) else {}
        candidate_compatibility = _automatic_comparison_compatibility(current_context, baseline_context)
        if candidate_compatibility is not None:
            baseline_run_id = str(candidate_id)
            compatibility = candidate_compatibility
            break
    if baseline_run_id is None or compatibility is None:
        update_collection_source_monitoring_coverage(
            conn,
            source_id=source_id,
            run_id=current_run_id,
            baseline={
                "state": "unavailable",
                "reason": "no_prior_complete_run" if not candidates else "no_compatible_prior_run",
                "candidates_considered": len(candidates),
                "candidate_window_limit": 20,
                "candidate_window_exhausted": len(candidates) >= 20,
            },
        )
        return None

    comparison_id = str(uuid.uuid4())
    inserted = conn.execute(
        """
        INSERT INTO run_comparisons (
            id, project_id, source_id, baseline_run_id, current_run_id,
            algorithm_version, options_hash, trigger, state, compatibility,
            progress, summary, attempt_count, created_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, 'automatic', 'queued', %s::jsonb,
            '{"phase":"queued","processed":0,"automatic":true}'::jsonb,
            '{"appeared":0,"disappeared":0,"changed":0,"indeterminate":0,"total":0,"exact":false,"resource_summary_exact":false,"item_churn_computed":false}'::jsonb,
            0, NOW()
        )
        ON CONFLICT (
            project_id, baseline_run_id, current_run_id, algorithm_version, options_hash
        ) DO NOTHING
        RETURNING id::text
        """,
        (
            comparison_id,
            project_id,
            source_id,
            baseline_run_id,
            current_run_id,
            COMPARISON_ALGORITHM_VERSION,
            COMPARISON_DEFAULT_OPTIONS_HASH,
            json.dumps(compatibility, ensure_ascii=True, separators=(",", ":")),
        ),
    ).fetchone()
    if inserted is None:
        existing = conn.execute(
            """
            SELECT id::text, state
            FROM run_comparisons
            WHERE project_id = %s AND baseline_run_id = %s AND current_run_id = %s
              AND algorithm_version = %s AND options_hash = %s
            """,
            (
                project_id,
                baseline_run_id,
                current_run_id,
                COMPARISON_ALGORITHM_VERSION,
                COMPARISON_DEFAULT_OPTIONS_HASH,
            ),
        ).fetchone()
        comparison_id = str(existing[0]) if existing else comparison_id
    baseline_state = "established" if inserted is None and existing and str(existing[1]) == "complete" else "queued"
    update_collection_source_monitoring_coverage(
        conn,
        source_id=source_id,
        run_id=current_run_id,
        baseline={
            "state": baseline_state,
            "comparison_id": comparison_id,
            "baseline_run_id": baseline_run_id,
        },
    )
    if inserted is None:
        return comparison_id if existing else None
    write_audit(
        conn,
        project_id,
        "AUTOMATIC_COMPARISON_CREATED",
        "run_comparison",
        comparison_id,
        {
            "worker": CONSUMER_NAME,
            "source_id": source_id,
            "baseline_run_id": baseline_run_id,
            "current_run_id": current_run_id,
            "algorithm_version": COMPARISON_ALGORITHM_VERSION,
        },
    )
    return comparison_id


def _smb_identity_rows_stable(rows: list[tuple[Any, ...]], baseline_run_id: str, current_run_id: str) -> bool:
    planes: dict[str, dict[str, tuple[str, str, str]]] = {
        baseline_run_id: {},
        current_run_id: {},
    }
    valid_sources = {
        ("server_guid", "strong"),
        ("advertised_name", "moderate"),
        ("scan_target", "weak"),
    }

    def provider_id_valid(provider_id: str, source: str) -> bool:
        namespace = "smb-server-guid:v1:" if source == "server_guid" else "smb-server-name:v1:"
        if not provider_id.startswith(namespace):
            return False
        digest = provider_id[len(namespace) :]
        return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)

    for raw_run_id, raw_endpoint_key, raw_provider_id, raw_source, raw_strength in rows:
        run_id = str(raw_run_id)
        if run_id not in planes:
            return False
        endpoint_key = str(raw_endpoint_key or "").strip().casefold()
        provider_id = str(raw_provider_id or "").strip()
        source = str(raw_source or "").strip().casefold()
        strength = str(raw_strength or "").strip().casefold()
        if (
            not endpoint_key
            or not provider_id
            or (source, strength) not in valid_sources
            or not provider_id_valid(provider_id, source)
            or endpoint_key in planes[run_id]
        ):
            return False
        planes[run_id][endpoint_key] = (provider_id, source, strength)
    baseline = planes[baseline_run_id]
    current = planes[current_run_id]
    return bool(baseline) and baseline.keys() == current.keys() and baseline == current


def _comparison_smb_identity_status(
    conn: psycopg.Connection, baseline_run_id: str, current_run_id: str
) -> tuple[bool, bool]:
    row = conn.execute(
        """
        WITH smb_endpoints AS (
            SELECT endpoint.run_id::text AS run_id,
                   lower(btrim(endpoint.endpoint_key)) AS endpoint_key,
                   btrim(endpoint.provider_metadata->>'provider_endpoint_id') AS provider_endpoint_id,
                   lower(btrim(endpoint.provider_metadata->>'identity_source')) AS identity_source,
                   lower(btrim(endpoint.provider_metadata->>'identity_strength')) AS identity_strength,
                   lower(btrim(endpoint.provider_metadata->>'server_guid')) AS server_guid
            FROM endpoints AS endpoint
            WHERE endpoint.run_id IN (%s, %s)
              AND (
                  endpoint.provider = 'smb'
                  OR endpoint.provider_metadata->>'identity_source'
                     IN ('server_guid', 'advertised_name', 'scan_target')
                  OR EXISTS (
                      SELECT 1
                      FROM resources AS resource
                      WHERE resource.run_id = endpoint.run_id
                        AND resource.endpoint_id = endpoint.id
                        AND COALESCE(
                            resource.provider,
                            split_part(resource.resource_type::text, '_', 1)
                        ) = 'smb'
                  )
              )
        ),
        plane_stats AS (
            SELECT
                COUNT(*) FILTER (WHERE run_id = %s) > 0 AS baseline_present,
                COUNT(*) FILTER (WHERE run_id = %s) > 0 AS current_present,
                COALESCE(BOOL_AND(COALESCE(
                    endpoint_key <> ''
                    AND provider_endpoint_id IS NOT NULL
                    AND provider_endpoint_id <> ''
                    AND (
                        (
                            identity_source = 'server_guid'
                            AND identity_strength = 'strong'
                            AND provider_endpoint_id ~ '^smb-server-guid:v1:[0-9a-f]{64}$'
                            AND server_guid ~ '^[0-9a-f]{32}$'
                        )
                        OR (
                            identity_source = 'advertised_name'
                            AND identity_strength = 'moderate'
                            AND provider_endpoint_id ~ '^smb-server-name:v1:[0-9a-f]{64}$'
                        )
                        OR (
                            identity_source = 'scan_target'
                            AND identity_strength = 'weak'
                            AND provider_endpoint_id ~ '^smb-server-name:v1:[0-9a-f]{64}$'
                        )
                    ),
                    FALSE
                )), FALSE) AS metadata_valid,
                COALESCE(BOOL_AND(
                    identity_source = 'server_guid'
                    AND identity_strength = 'strong'
                ), FALSE) AS strong_identity_complete,
                COUNT(*) = COUNT(DISTINCT (run_id, endpoint_key)) AS endpoint_keys_unique
            FROM smb_endpoints
        ),
        baseline_minus_current AS (
            SELECT endpoint_key, provider_endpoint_id, identity_source, identity_strength, server_guid
            FROM smb_endpoints WHERE run_id = %s
            EXCEPT
            SELECT endpoint_key, provider_endpoint_id, identity_source, identity_strength, server_guid
            FROM smb_endpoints WHERE run_id = %s
        ),
        current_minus_baseline AS (
            SELECT endpoint_key, provider_endpoint_id, identity_source, identity_strength, server_guid
            FROM smb_endpoints WHERE run_id = %s
            EXCEPT
            SELECT endpoint_key, provider_endpoint_id, identity_source, identity_strength, server_guid
            FROM smb_endpoints WHERE run_id = %s
        )
        SELECT (
                   plane_stats.baseline_present
                   AND plane_stats.current_present
                   AND plane_stats.metadata_valid
                   AND plane_stats.endpoint_keys_unique
                   AND NOT EXISTS (SELECT 1 FROM baseline_minus_current)
                   AND NOT EXISTS (SELECT 1 FROM current_minus_baseline)
               ) AS stable,
               plane_stats.strong_identity_complete
        FROM plane_stats
        """,
        (
            baseline_run_id,
            current_run_id,
            baseline_run_id,
            current_run_id,
            baseline_run_id,
            current_run_id,
            current_run_id,
            baseline_run_id,
        ),
    ).fetchone()
    stable = bool(row and row[0] is True)
    return stable, bool(stable and row and row[1] is True)


def _comparison_smb_identity_stable(conn: psycopg.Connection, baseline_run_id: str, current_run_id: str) -> bool:
    return _comparison_smb_identity_status(conn, baseline_run_id, current_run_id)[0]


def _comparison_observes_smb_resources(conn: psycopg.Connection, baseline_run_id: str, current_run_id: str) -> bool:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM resources AS resource
            WHERE resource.run_id IN (%s, %s)
              AND COALESCE(
                  resource.provider,
                  split_part(resource.resource_type::text, '_', 1)
              ) = 'smb'
        )
        """,
        (baseline_run_id, current_run_id),
    ).fetchone()
    return bool(row and row[0] is True)


def flush_item_batch(conn: psycopg.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    padded_rows = [
        row + (None, None, None, None, None, False, "{}", None, "{}", "{}") if len(row) == 12 else row for row in rows
    ]
    provider_rows = [row for row in padded_rows if row[13] is not None]
    legacy_rows = [row for row in padded_rows if row[13] is None]

    insert_sql = """
        INSERT INTO items (
            run_id, resource_id, path, name, is_dir, size_bytes, allocation_size_bytes,
            mtime, created_at, accessed_at, changed_at, file_attributes, provider,
            provider_item_id, provider_parent_id, web_url, mime_type, deleted,
            provider_metadata, exposure, exposure_evidence, permission_summary
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
            exposure_evidence = items.exposure_evidence || EXCLUDED.exposure_evidence,
            permission_summary = items.permission_summary || EXCLUDED.permission_summary
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
    """Remove resumable rows after terminal artifact provenance/framing rejection."""

    conn.execute("DELETE FROM permission_entries WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM permission_assessments WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM permission_principals WHERE run_id = %s", (run_id,))
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


def _normalize_token_expiration(raw_value: Any, *, field: str) -> str | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError(f"field {field} must be an RFC3339 timestamp or null")
    value = raw_value.strip()
    if len(value) > TOKEN_EXPIRATION_MAX_LENGTH or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ValueError(f"field {field} must be an RFC3339 timestamp or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"field {field} must be an RFC3339 timestamp or null") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"field {field} must include an RFC3339 timezone offset")
    return value


def _is_forbidden_provider_metadata_key(raw_key: str) -> bool:
    normalized = raw_key.strip().lower().replace("-", "_").replace(" ", "_")
    fingerprint = _provider_metadata_key_fingerprint(raw_key)
    if fingerprint in SAFE_PROVIDER_METADATA_KEY_FINGERPRINTS:
        return False
    if normalized in FORBIDDEN_PROVIDER_METADATA_KEYS:
        return True
    if fingerprint in FORBIDDEN_PROVIDER_METADATA_KEY_FINGERPRINTS:
        return True
    # Artifact producers are untrusted and key aliases are effectively
    # unbounded. After the exact telemetry exceptions above, fail closed on
    # sensitive stems regardless of prefixes, suffixes, separators, or version
    # tags (dbPasswordBackup, credentialsV2, authToken, and similar variants).
    return any(stem in fingerprint for stem in SENSITIVE_PROVIDER_METADATA_KEY_STEMS)


def _reject_secret_keys(raw_value: Any, *, field: str, depth: int = 0) -> None:
    if depth > PROVIDER_METADATA_MAX_DEPTH:
        return
    if isinstance(raw_value, dict):
        for raw_key, value in raw_value.items():
            child_field = f"{field}.{raw_key}" if isinstance(raw_key, str) else field
            if isinstance(raw_key, str) and _is_forbidden_provider_metadata_key(raw_key):
                raise ValueError(f"field {child_field} is secret or sensitive operational state")
            if isinstance(raw_key, str) and _provider_metadata_key_fingerprint(raw_key) == "tokenexpiration":
                _normalize_token_expiration(value, field=child_field)
            _reject_secret_keys(value, field=child_field, depth=depth + 1)
    elif isinstance(raw_value, list):
        for value in raw_value[: PROVIDER_METADATA_MAX_LIST_ITEMS + 1]:
            _reject_secret_keys(value, field=f"{field}[]", depth=depth + 1)


def _normalize_provider_metadata(
    raw_metadata: Any,
    *,
    field: str = "metadata",
    allow_root_navigation_web_url: bool = False,
) -> dict[str, Any]:
    if raw_metadata is None:
        return {}
    if not isinstance(raw_metadata, dict):
        raise ValueError(f"field {field} must be an object")

    # Every provider-metadata surface is attacker-controlled artifact input.
    # Apply the permission-material policy here, at the common persistence
    # boundary, so a producer cannot bypass the dedicated permission fields by
    # nesting raw ACL bytes, descriptors, or bearer-like Graph links beneath an
    # arbitrary metadata key. The predicate retains only the explicitly
    # reviewed semantic telemetry allowlist.
    _reject_sensitive_permission_payload(
        raw_metadata,
        field=field,
        allow_root_navigation_web_url=allow_root_navigation_web_url,
    )

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
                if _provider_metadata_key_fingerprint(key) == "tokenexpiration":
                    nested_value = _normalize_token_expiration(nested_value, field=f"{path}.{key}")
                if "permissionsummary" in _provider_metadata_key_fingerprint(key):
                    # Resource metadata intentionally carries a duplicate of
                    # the normalized permission summary for provider-specific
                    # presentation. Treat aliases and descendant summaries as
                    # permission material instead of letting them pass through
                    # the generic JSON normalizer alone.
                    _reject_sensitive_permission_payload(
                        nested_value,
                        field=f"{path}.{key}",
                    )
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
        if field == "token_expiration":
            value = _normalize_token_expiration(raw_value, field="auth_context.token_expiration")
        else:
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

    raw_features = rec.get("artifact_features")
    if raw_features is None:
        raw_features = raw_context.get("artifact_features")
    if raw_features is not None:
        if not isinstance(raw_features, list) or len(raw_features) > 64:
            raise ValueError("field artifact_features must be a list with at most 64 values")
        features: list[str] = []
        for raw_feature in raw_features:
            feature = _normalize_optional_provider_text(raw_feature, 128)
            if feature and feature not in features:
                features.append(feature)
        context["artifact_features"] = features

    raw_schema_version = rec.get("schema_version", raw_context.get("artifact_schema_version"))
    if raw_schema_version is not None:
        try:
            schema_version = int(raw_schema_version)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("field schema_version must be an integer") from exc
        if schema_version not in {1, 2}:
            raise ValueError("field schema_version is unsupported")
        context["artifact_schema_version"] = schema_version

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


def _permission_key(raw_value: Any, *, field: str, fallback: Any | None = None) -> str:
    value = raw_value if raw_value not in (None, "") else fallback
    if value in (None, ""):
        raise ValueError(f"missing field: {field}")
    normalized = str(value).strip().lower()
    if len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized):
        return normalized
    if len(normalized) > 4096 or "\x00" in normalized:
        raise ValueError(f"field {field} is not a valid stable identifier")
    return hashlib.sha256(normalized.encode("utf-8", errors="strict")).hexdigest()


def _permission_count(raw_value: Any, *, field: str, default: int = 0) -> int:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        raise ValueError(f"field {field} must be a non-negative integer")
    try:
        value = int(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"field {field} must be a non-negative integer") from exc
    if value < 0 or value > PERMISSION_MAX_COUNT:
        raise ValueError(f"field {field} must be between 0 and {PERMISSION_MAX_COUNT}")
    return value


def _permission_text(
    raw_value: Any,
    *,
    field: str,
    max_length: int = PERMISSION_MAX_TEXT_LENGTH,
    default: str | None = None,
) -> str:
    value = raw_value if raw_value not in (None, "") else default
    if value is None:
        raise ValueError(f"missing field: {field}")
    normalized = _normalize_optional_provider_text(value, max_length)
    if normalized is None:
        raise ValueError(f"field {field} must not be empty")
    return normalized


def _permission_text_list(
    raw_value: Any,
    *,
    field: str,
    max_values: int = PERMISSION_MAX_LIST_VALUES,
    max_length: int = PERMISSION_MAX_TEXT_LENGTH,
) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list) or len(raw_value) > max_values:
        raise ValueError(f"field {field} must be a list with at most {max_values} values")
    values: list[str] = []
    for raw_item in raw_value:
        item = _normalize_optional_provider_text(raw_item, max_length)
        if item and item not in values:
            values.append(item)
    return values


def _is_sensitive_permission_payload_key(raw_key: str) -> bool:
    fingerprint = _provider_metadata_key_fingerprint(raw_key)
    if fingerprint in SAFE_PERMISSION_DETAIL_KEY_FINGERPRINTS | SAFE_PROVIDER_BYTE_COUNT_KEY_FINGERPRINTS:
        return False
    if fingerprint in PERMISSION_SENSITIVE_KEY_FINGERPRINTS:
        return True

    # Raw security descriptors and ACL/SACL bytes must never be retained. The
    # exact allowlist above contains every reviewed scalar fact emitted by the
    # built-in collector; all other descriptor/ACL aliases fail closed.
    if any(marker in fingerprint for marker in ("descriptor", "acl", "sddl")):
        return True

    # Graph invitation/share links are bearer-like material. Preserve only the
    # reviewed semantic observations above (link scope/type and whether sign-in
    # or a password is required), never provider URLs, HTML, hrefs, or IDs.
    if "link" in fingerprint:
        return True
    if "invite" in fingerprint or "invitation" in fingerprint:
        return True
    if any(marker in fingerprint for marker in ("weburl", "weburi", "webhtml", "webhref")):
        return True
    # Permission-context locators and opaque/raw containers have no reviewed
    # persisted semantics. Treat aliases as sensitive instead of relying on a
    # producer to label a bearer URL or serialized descriptor honestly.
    if fingerprint.endswith(("url", "uri", "href", "html")):
        return True
    if (
        fingerprint == "raw"
        or fingerprint.startswith("raw")
        or fingerprint in {"bytes", "binary", "blob", "payload"}
        or "bytes" in fingerprint
        or fingerprint.endswith(("binary", "blob", "payload"))
    ):
        return True
    if any(marker in fingerprint for marker in ("shareid", "sharingid")):
        return True
    if ("share" in fingerprint or "sharing" in fingerprint) and any(
        locator in fingerprint for locator in ("url", "uri", "href", "html")
    ):
        return True
    return False


def _validate_safe_permission_telemetry(raw_key: str, raw_value: Any, *, field: str) -> None:
    """Validate every exception to the permission-material deny policy.

    These fields are retained because they are bounded semantic facts emitted
    by the built-in collectors. An exact key match must not become a container
    or free-form string escape hatch for raw descriptors or bearer URLs.
    """

    fingerprint = _provider_metadata_key_fingerprint(raw_key)
    reviewed_keys = SAFE_PERMISSION_DETAIL_KEY_FINGERPRINTS | SAFE_PROVIDER_BYTE_COUNT_KEY_FINGERPRINTS
    if fingerprint not in reviewed_keys or raw_value is None:
        return

    invalid = False
    if fingerprint in SAFE_PROVIDER_BYTE_COUNT_KEY_FINGERPRINTS:
        invalid = type(raw_value) is not int or raw_value < 0 or raw_value > SAFE_PROVIDER_BYTE_COUNT_MAX
    elif fingerprint in SAFE_PERMISSION_INTEGER_RANGES:
        minimum, maximum = SAFE_PERMISSION_INTEGER_RANGES[fingerprint]
        invalid = type(raw_value) is not int or not minimum <= raw_value <= maximum
    elif fingerprint in SAFE_PERMISSION_BOOLEAN_KEYS:
        invalid = not isinstance(raw_value, bool)
    elif fingerprint in SAFE_PERMISSION_ENUM_VALUES:
        invalid = type(raw_value) is not str or raw_value not in SAFE_PERMISSION_ENUM_VALUES[fingerprint]
    elif fingerprint == "descriptorcontrolflags":
        invalid = (
            not isinstance(raw_value, list)
            or len(raw_value) > len(SAFE_DESCRIPTOR_CONTROL_FLAGS)
            or any(type(value) is not str or value not in SAFE_DESCRIPTOR_CONTROL_FLAGS for value in raw_value)
            or len(set(raw_value)) != len(raw_value)
        )
    else:  # Defensive: adding a safe key requires adding its value schema.
        invalid = True

    if invalid:
        raise ValueError(f"field {field}.{raw_key} contains invalid permission telemetry")


def _reject_sensitive_permission_payload(
    raw_value: Any,
    *,
    field: str,
    depth: int = 0,
    allow_root_navigation_web_url: bool = False,
) -> None:
    if depth > PROVIDER_METADATA_MAX_DEPTH:
        return
    if isinstance(raw_value, dict):
        for raw_key, nested in raw_value.items():
            root_navigation_url = (
                allow_root_navigation_web_url
                and depth == 0
                and isinstance(raw_key, str)
                and raw_key.strip() == "web_url"
            )
            if isinstance(raw_key, str) and not root_navigation_url:
                _validate_safe_permission_telemetry(raw_key, nested, field=field)
                if _is_sensitive_permission_payload_key(raw_key):
                    raise ValueError(f"field {field}.{raw_key} contains sensitive permission material")
            _reject_sensitive_permission_payload(
                nested,
                field=field,
                depth=depth + 1,
                allow_root_navigation_web_url=allow_root_navigation_web_url,
            )
    elif isinstance(raw_value, list):
        for nested in raw_value[: PROVIDER_METADATA_MAX_LIST_ITEMS + 1]:
            _reject_sensitive_permission_payload(
                nested,
                field=field,
                depth=depth + 1,
                allow_root_navigation_web_url=allow_root_navigation_web_url,
            )


def _normalize_permission_details(raw_value: Any, *, field: str) -> dict[str, Any]:
    _reject_sensitive_permission_payload(raw_value, field=field)
    return _normalize_provider_metadata(raw_value, field=field)


def _normalize_permission_errors(raw_value: Any, *, field: str) -> list[Any]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list) or len(raw_value) > 64:
        raise ValueError(f"field {field} must be a list with at most 64 values")
    _reject_sensitive_permission_payload(raw_value, field=field)
    wrapper = _normalize_provider_metadata({"values": raw_value}, field=field)
    return list(wrapper.get("values") or [])


def _normalize_permission_principal(raw_value: Any, *, provider: str) -> dict[str, Any] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError("field principal must be an object")
    _reject_sensitive_permission_payload(raw_value, field="principal")
    principal_provider = _permission_text(
        raw_value.get("provider"), field="principal.provider", max_length=32, default=provider
    ).lower()
    if principal_provider != provider:
        raise ValueError("field principal.provider must match the permission entry provider")
    namespace_default = "windows_sid" if provider == "smb" else "microsoft_graph_identity"
    identifier_namespace = _permission_text(
        raw_value.get("identifier_namespace"),
        field="principal.identifier_namespace",
        max_length=80,
        default=namespace_default,
    )
    native_id = _normalize_optional_provider_text(raw_value.get("native_id"), PROVIDER_ID_MAX_LENGTH)
    authority = _normalize_optional_provider_text(raw_value.get("authority"), PROVIDER_ID_MAX_LENGTH)
    if native_id is None or authority is None:
        raise ValueError("field principal requires stable native_id and authority values")
    kind = _permission_text(raw_value.get("kind"), field="principal.kind", max_length=40, default="unknown")
    fallback = json.dumps(
        [provider, identifier_namespace, authority or "", native_id or "", kind],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    # Provider keys are useful transport identifiers but not a trust boundary.
    # Own the canonical key so a forged/reused producer key cannot make two
    # different principals hash as the same evidence.
    principal_key = _permission_key(None, field="principal.principal_key", fallback=fallback)
    aliases = _permission_text_list(raw_value.get("aliases"), field="principal.aliases", max_values=32, max_length=1024)
    resolution = _permission_text(
        raw_value.get("resolution", raw_value.get("resolution_state")),
        field="principal.resolution",
        max_length=40,
        default="unresolved",
    )
    return {
        "provider": provider,
        "identifier_namespace": identifier_namespace,
        "principal_key": principal_key,
        "kind": kind,
        "native_id": native_id,
        "authority": authority,
        "display_name": _normalize_optional_provider_text(raw_value.get("display_name"), 1024),
        "login_name": _normalize_optional_provider_text(raw_value.get("login_name"), 1024),
        "email": _normalize_optional_provider_text(raw_value.get("email"), 1024),
        "resolution": resolution,
        "resolution_source": _normalize_optional_provider_text(raw_value.get("resolution_source"), 80),
        "aliases": aliases,
    }


def _normalize_permission_common(rec: dict[str, Any]) -> tuple[str, str, str]:
    provider = _permission_text(rec.get("provider"), field="provider", max_length=32).lower()
    if provider not in PERMISSION_PROVIDERS:
        raise ValueError("field provider must be smb or sharepoint for permission evidence")
    semantics = _permission_text(rec.get("semantics", rec.get("provider_semantics")), field="semantics", max_length=80)
    if semantics != PERMISSION_SEMANTICS[provider]:
        raise ValueError(f"field semantics must be {PERMISSION_SEMANTICS[provider]} for provider {provider}")
    surface = _permission_text(rec.get("permission_surface"), field="permission_surface", max_length=80)
    if surface not in PERMISSION_SURFACES[provider]:
        allowed = ", ".join(sorted(PERMISSION_SURFACES[provider]))
        raise ValueError(f"field permission_surface must be one of: {allowed}")
    return provider, semantics, surface


def _permission_assessment_semantic_details(provider: str, details: dict[str, Any]) -> dict[str, Any]:
    """Project provider payloads onto versioned, comparison-stable facts.

    Transport provenance and presentation labels remain available to operators,
    but must not make otherwise identical permission evidence compare as changed.
    """

    if provider != "smb":
        return details

    owner = details.get("owner") if isinstance(details.get("owner"), dict) else {}
    group = details.get("group") if isinstance(details.get("group"), dict) else {}
    return {
        "contract": "smb_windows_acl_v1",
        "descriptor_revision": details.get("descriptor_revision"),
        "descriptor_control_retained": details.get("descriptor_control_retained"),
        "owner_state": details.get("owner_state"),
        "owner_sid": owner.get("native_id"),
        "group_state": details.get("group_state"),
        "group_sid": group.get("native_id"),
        "dacl_state": details.get("dacl_state"),
        "dacl_revision": details.get("dacl_revision"),
        "dacl_ace_count": details.get("dacl_ace_count"),
    }


def _permission_entry_semantic_details(provider: str, details: dict[str, Any]) -> dict[str, Any]:
    if provider != "smb":
        return details
    return {
        "ace_type_code": details.get("ace_type_code"),
        "ace_flags": details.get("ace_flags"),
        "access_mask": details.get("access_mask"),
        "compound_type": details.get("compound_type"),
        "object_flags": details.get("object_flags"),
        "object_type_guid": details.get("object_type_guid"),
        "inherited_object_type_guid": details.get("inherited_object_type_guid"),
        "application_data_present": details.get("application_data_present") is True,
        "parse_error_present": bool(details.get("parse_error")),
    }


def _normalize_permission_assessment(rec: dict[str, Any]) -> None:
    provider, semantics, surface = _normalize_permission_common(rec)
    endpoint_key = _permission_text(rec.get("endpoint_key"), field="endpoint_key", max_length=ENDPOINT_KEY_MAX_LENGTH)
    resource_name = _permission_text(
        rec.get("resource_name", rec.get("name")), field="resource_name", max_length=RESOURCE_NAME_MAX_LENGTH
    )
    provider_resource_id = _normalize_optional_provider_text(rec.get("provider_resource_id"), PROVIDER_ID_MAX_LENGTH)
    provider_item_id = _normalize_optional_provider_text(
        rec.get("provider_item_id", rec.get("subject_id") if rec.get("subject_type") == "item" else None),
        PROVIDER_ID_MAX_LENGTH,
    )
    raw_path = rec.get("subject_path")
    if raw_path is not None:
        if not isinstance(raw_path, str) or "\x00" in raw_path:
            raise ValueError("field subject_path must be a string without null characters")
        if len(raw_path.encode("utf-8")) > PERMISSION_MAX_PATH_BYTES:
            raise ValueError(f"field subject_path exceeds {PERMISSION_MAX_PATH_BYTES} UTF-8 bytes")
        subject_path = raw_path
    else:
        subject_path = None
    subject_kind = _permission_text(rec.get("subject_kind"), field="subject_kind", max_length=32, default="share_root")
    if provider_resource_id:
        if provider_item_id:
            provider_subject = ["provider_item", provider_item_id]
        elif subject_kind in {"item", "file", "directory", "folder", "drive_item"}:
            provider_subject = ["provider_path", subject_path or ""]
        else:
            provider_subject = ["resource_root"]
        subject_identity = [provider, provider_resource_id, subject_kind, provider_subject]
    else:
        subject_identity = [provider, endpoint_key, resource_name, subject_kind, subject_path or ""]
    subject_fallback = json.dumps(
        subject_identity,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    # Paths in Samba-backed namespaces can be case-sensitive, so subject
    # identity deliberately avoids _permission_key's case folding.
    subject_key = hashlib.sha256(subject_fallback.encode("utf-8", errors="strict")).hexdigest()
    assessment_fallback = f"{semantics}:{surface}:{subject_key}"
    assessment_key = _permission_key(
        rec.get("assessment_key", rec.get("assessment_id")),
        field="assessment_key",
        fallback=assessment_fallback,
    )
    coverage = rec.get("coverage") if isinstance(rec.get("coverage"), dict) else {}
    observed = _permission_count(rec.get("entries_observed", rec.get("entries_expected")), field="entries_observed")
    emitted = _permission_count(rec.get("entries_emitted"), field="entries_emitted")
    omitted = _permission_count(rec.get("entries_omitted"), field="entries_omitted")
    unknown = _permission_count(rec.get("unknown_entries"), field="unknown_entries")
    if emitted + omitted > observed and observed != 0:
        raise ValueError("entries_emitted plus entries_omitted cannot exceed entries_observed")
    entry_set_hash_raw = rec.get("entry_set_hash")
    entry_set_hash = None
    if entry_set_hash_raw not in (None, ""):
        entry_set_hash = _permission_key(entry_set_hash_raw, field="entry_set_hash")
    observed_at = _normalize_item_mtime(rec.get("observed_at", rec.get("finished_at")))
    if rec.get("observed_at", rec.get("finished_at")) is not None and observed_at is None:
        raise ValueError("field observed_at must be a valid timestamp")
    provider_details_raw = rec.get("provider_details", rec.get("provider_payload"))
    details = _normalize_permission_details(provider_details_raw, field="provider_details")
    summary = _normalize_permission_details(rec.get("permission_summary"), field="permission_summary")
    # Producer hashes are hints only. Derive the stored assessment hash from
    # the normalized, bounded semantic payload so a stale or forged digest
    # cannot hide a descriptor-level change.
    evidence_hash = _permission_key(
        None,
        field="evidence_hash",
        fallback=json.dumps(
            [provider, semantics, surface, _permission_assessment_semantic_details(provider, details)],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ),
    )
    negative = _normalize_boolean(
        rec.get("negative_conclusion_supported"), field="negative_conclusion_supported", default=False
    )
    rec.update(
        {
            "assessment_key": assessment_key,
            "provider": provider,
            "semantics": semantics,
            "permission_surface": surface,
            "endpoint_key": endpoint_key,
            "resource_name": resource_name,
            "provider_resource_id": provider_resource_id,
            "provider_item_id": provider_item_id,
            "subject_kind": subject_kind,
            "subject_key": subject_key,
            "subject_path": subject_path,
            "method": _permission_text(rec.get("method"), field="method", max_length=80),
            "assessment_state": _permission_text(
                rec.get("assessment_state"), field="assessment_state", max_length=40, default="unknown"
            ),
            "selection_scope": _permission_text(
                rec.get("selection_scope", coverage.get("selection_scope")),
                field="selection_scope",
                max_length=64,
                default="unknown",
            ),
            "selection_coverage": _permission_text(
                rec.get("selection_coverage", coverage.get("selection")),
                field="selection_coverage",
                max_length=64,
                default="unknown",
            ),
            "retrieval_coverage": _permission_text(
                rec.get("retrieval_coverage", coverage.get("retrieval")),
                field="retrieval_coverage",
                max_length=64,
                default="unknown",
            ),
            "provider_visibility": _permission_text(
                rec.get("provider_visibility", rec.get("visibility", coverage.get("provider_visibility"))),
                field="provider_visibility",
                max_length=64,
                default="unknown",
            ),
            "semantic_coverage": _permission_text(
                rec.get("semantic_coverage", coverage.get("semantic")),
                field="semantic_coverage",
                max_length=64,
                default="direct_entries_only",
            ),
            "principal_resolution": _permission_text(
                rec.get("principal_resolution", coverage.get("principal_resolution")),
                field="principal_resolution",
                max_length=64,
                default="unknown",
            ),
            "effective_access_status": _permission_text(
                rec.get("effective_access_status"),
                field="effective_access_status",
                max_length=40,
                default="not_computed",
            ),
            "negative_conclusion_supported": negative,
            "entries_observed": observed,
            "entries_emitted": emitted,
            "entries_omitted": omitted,
            "unknown_entries": unknown,
            "evidence_hash": evidence_hash,
            "entry_set_hash": entry_set_hash,
            "observed_at": observed_at,
            "limitations": _permission_text_list(
                rec.get("limitations"), field="limitations", max_values=64, max_length=1024
            ),
            "error_code": _normalize_optional_provider_text(rec.get("error_code"), 128),
            "errors": _normalize_permission_errors(rec.get("errors"), field="errors"),
            "provider_details": details,
            "permission_summary": summary,
        }
    )


def _normalize_permission_entry(rec: dict[str, Any]) -> None:
    provider, semantics, surface = _normalize_permission_common(rec)
    assessment_key = _permission_key(rec.get("assessment_key", rec.get("assessment_id")), field="assessment_key")
    principal = _normalize_permission_principal(rec.get("principal"), provider=provider)
    principal_key = principal["principal_key"] if principal else None
    provider_entry_id = _normalize_optional_provider_text(rec.get("provider_entry_id"), PROVIDER_ID_MAX_LENGTH)
    rights = _permission_text_list(
        rec.get("normalized_rights", rec.get("provider_rights")),
        field="normalized_rights",
        max_values=64,
        max_length=80,
    )
    details = _normalize_permission_details(
        rec.get("provider_details", rec.get("provider_payload")), field="provider_details"
    )
    entry_kind = _permission_text(rec.get("entry_kind"), field="entry_kind", max_length=64, default="permission_entry")
    entry_effect = _permission_text(
        rec.get("effect", rec.get("entry_effect")), field="effect", max_length=40, default="unknown"
    ).lower()
    inherited_state = _permission_text(
        rec.get("inherited_state"), field="inherited_state", max_length=40, default="unknown"
    )
    ordinal = None
    if rec.get("ordinal") is not None:
        ordinal = _permission_count(rec.get("ordinal"), field="ordinal")
    expiration_at = _normalize_item_mtime(rec.get("expiration_at"))
    if rec.get("expiration_at") is not None and expiration_at is None:
        raise ValueError("field expiration_at must be a valid timestamp")
    entry_fallback = json.dumps(
        [
            assessment_key,
            provider_entry_id or "",
            ordinal,
            entry_kind,
            entry_effect,
            principal_key or "",
            rights,
            details,
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    entry_key = _permission_key(rec.get("entry_key", rec.get("entry_id")), field="entry_key", fallback=entry_fallback)
    evidence_fallback = json.dumps(
        [
            semantics,
            surface,
            entry_kind,
            entry_effect,
            principal_key or "",
            rights,
            inherited_state,
            expiration_at.isoformat() if expiration_at else None,
            ordinal if provider == "smb" else None,
            _permission_entry_semantic_details(provider, details),
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    # Always own the canonical digest after normalization. SMB includes ACE
    # order because DACL ordering is semantically relevant; Graph permission
    # response ordering is intentionally excluded.
    evidence_hash = _permission_key(None, field="evidence_hash", fallback=evidence_fallback)
    rec.update(
        {
            "assessment_key": assessment_key,
            "entry_key": entry_key,
            "provider": provider,
            "semantics": semantics,
            "permission_surface": surface,
            "provider_entry_id": provider_entry_id,
            "ordinal": ordinal,
            "principal_key": principal_key,
            "principal": principal,
            "entry_kind": entry_kind,
            "effect": entry_effect,
            "normalized_rights": rights,
            "inherited_state": inherited_state,
            "expiration_at": expiration_at,
            "evidence_hash": evidence_hash,
            "provider_details": details,
        }
    )


def validate_record(rec: dict[str, Any]) -> tuple[bool, str | None]:
    rec_type = rec.get("type")
    if rec_type not in {"run_meta", "endpoint", "resource", "item", "error", "run_end"} | PERMISSION_RECORD_TYPES:
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
        "permission_assessment": ("run_id",),
        "permission_entry": ("run_id",),
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
            if int(rec.get("schema_version")) not in {1, 2}:
                return False, "unsupported schema_version"
        except (TypeError, ValueError):
            return False, "invalid schema_version"
        try:
            rec["collection_context"] = _normalize_collection_context(rec)
        except ValueError as exc:
            return False, str(exc)

    if rec_type in PERMISSION_RECORD_TYPES:
        try:
            if rec_type == "permission_assessment":
                _normalize_permission_assessment(rec)
            else:
                _normalize_permission_entry(rec)
        except ValueError as exc:
            return False, str(exc)
        return True, None

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
                allow_root_navigation_web_url=True,
            )
            provider_endpoint_id = _normalize_optional_provider_text(
                rec.get("provider_endpoint_id"),
                PROVIDER_ID_MAX_LENGTH,
            )
            metadata_endpoint_id = _normalize_optional_provider_text(
                rec["provider_metadata"].get("provider_endpoint_id"),
                PROVIDER_ID_MAX_LENGTH,
            )
            if (
                provider_endpoint_id is not None
                and metadata_endpoint_id is not None
                and provider_endpoint_id != metadata_endpoint_id
            ):
                raise ValueError("field provider_endpoint_id conflicts with metadata.provider_endpoint_id")
            effective_endpoint_id = provider_endpoint_id or metadata_endpoint_id
            if effective_endpoint_id is not None:
                rec["provider_metadata"]["provider_endpoint_id"] = effective_endpoint_id
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
            if provider != share_type:
                raise ValueError("field provider conflicts with the provider implied by share_type/resource_type")
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
            rec["exposure_evidence"] = _normalize_permission_details(
                rec.get("exposure_evidence"),
                field="exposure_evidence",
            )
            rec["permission_summary"] = _normalize_permission_details(
                rec.get("permission_summary"),
                field="permission_summary",
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
            "permission_summary": raw_entry.get("permission_summary"),
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

    endpoint_metadata = (
        dict(raw_endpoint.get("metadata") or {}) if isinstance(raw_endpoint.get("metadata"), dict) else {}
    )
    if raw_endpoint.get("provider_endpoint_id") is not None:
        endpoint_metadata.setdefault("provider_endpoint_id", raw_endpoint.get("provider_endpoint_id"))
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
            "metadata": endpoint_metadata,
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
                "permission_summary": raw_share.get("permission_summary"),
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

        raw_assessments = raw_share.get("permission_assessments")
        if isinstance(raw_assessments, list):
            for raw_assessment in raw_assessments:
                if not isinstance(raw_assessment, dict):
                    continue
                assessment = dict(raw_assessment)
                nested_permission_entries = assessment.pop("entries", None)
                assessment.update(
                    {
                        "type": "permission_assessment",
                        "run_id": run_id,
                        "endpoint_key": assessment.get("endpoint_key") or endpoint_key,
                        "resource_name": assessment.get("resource_name") or share_name,
                        "provider": assessment.get("provider") or share_type,
                        "provider_resource_id": assessment.get("provider_resource_id")
                        or raw_share.get("provider_resource_id")
                        or raw_share.get("drive_id"),
                    }
                )
                records.append(assessment)
                if isinstance(nested_permission_entries, list):
                    for raw_permission_entry in nested_permission_entries:
                        if not isinstance(raw_permission_entry, dict):
                            continue
                        permission_entry = dict(raw_permission_entry)
                        permission_entry.update(
                            {
                                "type": "permission_entry",
                                "run_id": run_id,
                                "assessment_key": permission_entry.get("assessment_key")
                                or assessment.get("assessment_key")
                                or assessment.get("assessment_id"),
                                "provider": permission_entry.get("provider") or assessment.get("provider"),
                                "semantics": permission_entry.get("semantics")
                                or assessment.get("semantics")
                                or assessment.get("provider_semantics"),
                                "permission_surface": permission_entry.get("permission_surface")
                                or assessment.get("permission_surface"),
                            }
                        )
                        records.append(permission_entry)

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
            "artifact_features": doc.get("artifact_features")
            if isinstance(doc.get("artifact_features"), list)
            else meta_raw.get("artifact_features"),
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
    artifact_features = _load_first_json_item(fp, "artifact_features")
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
            "artifact_features": artifact_features
            if isinstance(artifact_features, list)
            else ((meta_raw or {}).get("artifact_features") if isinstance(meta_raw, dict) else None),
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
    artifact_integrity: tuple[int, str],
    *,
    progress_callback: Callable[[], None] | None = None,
) -> None:
    """Validate framing and stored-byte provenance before inventory writes."""

    json_candidate = _is_json_artifact(artifact_key, content_type)
    gzip_input = artifact_key.endswith(".gz")
    if not json_candidate:
        with open_verified_artifact_stream(
            artifact_key,
            artifact_integrity,
            progress_callback=progress_callback,
        ) as body:
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
        with open_verified_artifact_stream(
            artifact_key,
            artifact_integrity,
            progress_callback=progress_callback,
        ) as body:
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

    with open_verified_artifact_stream(
        artifact_key,
        artifact_integrity,
        progress_callback=progress_callback,
    ) as body:
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
    if isinstance(exc, ArtifactIntegrityError):
        return str(exc)
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


def _public_comparison_error(exc: BaseException) -> str:
    if isinstance(exc, ValueError):
        detail = str(exc).strip()
        if detail in {
            "comparison runs must both exist, belong to the project, and be COMPLETE",
            "comparison identity is ambiguous; a run contains duplicate or unkeyed resources",
        }:
            return detail
        return "comparison input or identity validation failed"
    if isinstance(exc, psycopg.Error):
        return "database operation failed while comparing runs"
    if isinstance(exc, OSError):
        return "a comparison dependency was unavailable"
    return "unexpected comparison failure"


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


def discover_recoverable_monitoring_evaluations(limit: int = 8) -> list[dict[str, str]]:
    """Claim bounded monitoring-only retries without re-ingesting artifacts."""

    with connect_database() as conn:
        rows = conn.execute(
            """
            WITH candidates AS (
                SELECT id
                FROM scan_runs
                WHERE status = 'COMPLETE'
                  AND source_id IS NOT NULL
                  AND (
                      (
                          COALESCE(ingest_progress #>> '{monitoring_findings,state}', '')
                              IN ('queued', 'retrying')
                          AND COALESCE(
                              CASE
                                  WHEN pg_input_is_valid(
                                      NULLIF(ingest_progress #>> '{monitoring_findings,next_retry_at}', ''),
                                      'timestamp with time zone'
                                  )
                                  THEN NULLIF(
                                      ingest_progress #>> '{monitoring_findings,next_retry_at}', ''
                                  )::timestamptz
                              END,
                              TO_TIMESTAMP(0)
                          ) <= NOW()
                      )
                      OR (
                          COALESCE(ingest_progress #>> '{monitoring_findings,state}', '') = 'evaluating'
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
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE scan_runs AS run
            SET ingest_progress = COALESCE(run.ingest_progress, '{}'::jsonb)
                || jsonb_build_object(
                    'heartbeat_at', NOW(),
                    'monitoring_worker', %s::text,
                    'monitoring_findings',
                        COALESCE(run.ingest_progress->'monitoring_findings', '{}'::jsonb)
                        || jsonb_build_object(
                            'state', 'evaluating',
                            'phase', COALESCE(
                                run.ingest_progress #>> '{monitoring_findings,phase}',
                                'candidates'
                            ),
                            'recovery_claimed_at', NOW(),
                            'recovery_claimed_by', %s::text
                        )
                )
            FROM candidates
            WHERE run.id = candidates.id
            RETURNING run.id::text, run.project_id::text, run.source_id::text
            """,
            (STALE_INGESTING_SECONDS, max(1, min(int(limit), 100)), CONSUMER_NAME, CONSUMER_NAME),
        ).fetchall()
    return [
        {
            "job_type": "monitoring_evaluation",
            "run_id": str(row[0]),
            "project_id": str(row[1]),
            "source_id": str(row[2]),
        }
        for row in rows
    ]


def reopen_expired_accepted_risk_findings(limit: int = 100) -> int:
    """Boundedly reopen expired risk acceptances and audit in one transaction."""

    with connect_database() as conn:
        rows = conn.execute(
            """
            WITH candidates AS (
                SELECT id
                FROM findings
                WHERE status = 'accepted_risk'
                  AND accepted_risk_expires_at <= NOW()
                ORDER BY accepted_risk_expires_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE findings AS finding
            SET status = 'open', accepted_risk_expires_at = NULL,
                resolved_at = NULL, revision = finding.revision + 1,
                updated_at = NOW()
            FROM candidates
            WHERE finding.id = candidates.id
            RETURNING finding.id::text, finding.project_id::text
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        for finding_id, project_id in rows:
            write_audit(
                conn,
                str(project_id),
                "FINDING_ACCEPTED_RISK_EXPIRED",
                "finding",
                str(finding_id),
                {"worker": CONSUMER_NAME, "new_status": "open"},
            )
    return len(rows)


def discover_recoverable_comparisons(limit: int = 8) -> list[dict[str, str]]:
    with connect_database() as conn:
        rows = conn.execute(
            """
            WITH candidates AS (
                SELECT id
                FROM run_comparisons
                WHERE (
                        state = 'queued'
                        AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                      )
                   OR (
                        state = 'running'
                        AND COALESCE(heartbeat_at, started_at, created_at)
                            <= NOW() - (%s * INTERVAL '1 second')
                   )
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE run_comparisons AS comparison
            SET state = 'running',
                heartbeat_at = NOW(),
                progress = COALESCE(comparison.progress, '{}'::jsonb) || jsonb_build_object(
                    'recovery_claimed_at', NOW(),
                    'recovery_claimed_by', %s::text
                )
            FROM candidates
            WHERE comparison.id = candidates.id
            RETURNING comparison.id::text, comparison.project_id::text
            """,
            (STALE_INGESTING_SECONDS, max(1, limit), CONSUMER_NAME),
        ).fetchall()
    return [
        {
            "job_type": "comparison",
            "comparison_id": row[0],
            "project_id": row[1],
        }
        for row in rows
    ]


def discover_recoverable_comparison_finding_evaluations(limit: int = 8) -> list[dict[str, str]]:
    """Claim derived finding retries while keeping a valid comparison visible."""

    with connect_database() as conn:
        rows = conn.execute(
            """
            WITH candidates AS (
                SELECT id
                FROM run_comparisons
                WHERE state = 'complete'
                  AND COALESCE(summary #>> '{findings_evaluation,state}', '') IN ('queued', 'retrying')
                  AND COALESCE(
                      CASE
                          WHEN pg_input_is_valid(
                              NULLIF(summary #>> '{findings_evaluation,next_retry_at}', ''),
                              'timestamp with time zone'
                          )
                          THEN NULLIF(
                              summary #>> '{findings_evaluation,next_retry_at}', ''
                          )::timestamptz
                      END,
                      TO_TIMESTAMP(0)
                  ) <= NOW()
                ORDER BY completed_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE run_comparisons AS comparison
            SET heartbeat_at = NOW(),
                summary = jsonb_set(
                    COALESCE(comparison.summary, '{}'::jsonb),
                    '{findings_evaluation}',
                    COALESCE(comparison.summary->'findings_evaluation', '{}'::jsonb)
                        || jsonb_build_object(
                            'state', 'evaluating',
                            'recovery_claimed_at', NOW(),
                            'recovery_claimed_by', %s::text
                        ),
                    TRUE
                )
            FROM candidates
            WHERE comparison.id = candidates.id
            RETURNING comparison.id::text, comparison.project_id::text
            """,
            (max(1, min(int(limit), 100)), CONSUMER_NAME),
        ).fetchall()
    return [
        {
            "job_type": "comparison_findings_evaluation",
            "comparison_id": str(row[0]),
            "project_id": str(row[1]),
        }
        for row in rows
    ]


def _comparison_batch_keys(
    conn: psycopg.Connection,
    baseline_run_id: str,
    current_run_id: str,
    after_key: str | None,
    limit: int,
) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT identity_key
            FROM (
                SELECT identity_key FROM resources WHERE run_id = %s
                UNION
                SELECT identity_key FROM resources WHERE run_id = %s
            ) AS identities
            WHERE identity_key IS NOT NULL
              AND (%s::text IS NULL OR identity_key > %s::text)
            ORDER BY identity_key
            LIMIT %s
            """,
            (baseline_run_id, current_run_id, after_key, after_key, max(1, limit)),
        ).fetchall()
    ]


def _materialize_comparison_batch(
    conn: psycopg.Connection,
    comparison_id: str,
    baseline_run_id: str,
    current_run_id: str,
    identity_keys: list[str],
    compatibility: dict[str, Any],
) -> int:
    if not identity_keys:
        return 0
    structural = compatibility.get("structural_interpretable") is True
    content = compatibility.get("content_interpretable") is True
    capability_access = compatibility.get("capability_interpretable") is True
    direct_permissions_expected = compatibility.get("direct_permissions_interpretable") is True
    row = conn.execute(
        """
        WITH baseline_counts AS (
            SELECT item.resource_id, COUNT(*)::bigint AS item_count
            FROM items AS item
            JOIN resources AS resource
              ON resource.id = item.resource_id AND resource.run_id = item.run_id
            WHERE item.run_id = %(baseline_run_id)s
              AND item.deleted IS FALSE
              AND resource.identity_key = ANY(%(identity_keys)s)
            GROUP BY item.resource_id
        ),
        current_counts AS (
            SELECT item.resource_id, COUNT(*)::bigint AS item_count
            FROM items AS item
            JOIN resources AS resource
              ON resource.id = item.resource_id AND resource.run_id = item.run_id
            WHERE item.run_id = %(current_run_id)s
              AND item.deleted IS FALSE
              AND resource.identity_key = ANY(%(identity_keys)s)
            GROUP BY item.resource_id
        ),
        baseline_permissions AS (
            SELECT
                resource.identity_key,
                COALESCE(resource.permission_summary->>'evidence_available', 'false') = 'true'
                    AS evidence_present,
                COALESCE(resource.permission_summary->>'comparable', 'false') = 'true'
                    AS evidence_comparable,
                COALESCE(resource.permission_summary->>'scope_exact', 'false') = 'true'
                    AS scope_exact,
                resource.permission_summary->>'comparison_evidence_hash' AS evidence_hash,
                resource.permission_summary->>'comparison_quality_hash' AS quality_hash
            FROM resources AS resource
            WHERE resource.run_id = %(baseline_run_id)s
              AND resource.identity_key = ANY(%(identity_keys)s)
        ),
        current_permissions AS (
            SELECT
                resource.identity_key,
                COALESCE(resource.permission_summary->>'evidence_available', 'false') = 'true'
                    AS evidence_present,
                COALESCE(resource.permission_summary->>'comparable', 'false') = 'true'
                    AS evidence_comparable,
                COALESCE(resource.permission_summary->>'scope_exact', 'false') = 'true'
                    AS scope_exact,
                resource.permission_summary->>'comparison_evidence_hash' AS evidence_hash,
                resource.permission_summary->>'comparison_quality_hash' AS quality_hash
            FROM resources AS resource
            WHERE resource.run_id = %(current_run_id)s
              AND resource.identity_key = ANY(%(identity_keys)s)
        ),
        permission_rollup AS (
            SELECT
                COALESCE(current_permissions.identity_key, baseline_permissions.identity_key) AS identity_key,
                COALESCE(baseline_permissions.evidence_present, FALSE)
                    OR COALESCE(current_permissions.evidence_present, FALSE) AS evidence_present,
                baseline_permissions.evidence_present
                    AND current_permissions.evidence_present
                    AND baseline_permissions.evidence_comparable
                    AND current_permissions.evidence_comparable
                    AND baseline_permissions.quality_hash IS NOT NULL
                    AND baseline_permissions.quality_hash = current_permissions.quality_hash
                    AND baseline_permissions.evidence_hash IS NOT NULL
                    AND current_permissions.evidence_hash IS NOT NULL
                    AS directly_comparable,
                baseline_permissions.scope_exact
                    AND current_permissions.scope_exact AS scope_exact,
                baseline_permissions.evidence_present
                    AND current_permissions.evidence_present
                    AND baseline_permissions.evidence_hash IS DISTINCT FROM current_permissions.evidence_hash
                    AS evidence_difference,
                baseline_permissions.evidence_present IS DISTINCT FROM current_permissions.evidence_present
                    OR baseline_permissions.evidence_comparable IS DISTINCT FROM current_permissions.evidence_comparable
                    OR baseline_permissions.quality_hash IS DISTINCT FROM current_permissions.quality_hash
                    AS quality_difference
            FROM baseline_permissions
            FULL OUTER JOIN current_permissions USING (identity_key)
        ),
        baseline AS (
            SELECT
                resource.id,
                resource.identity_key,
                resource.resource_type::text AS resource_type,
                COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) AS provider,
                resource.provider_resource_id,
                endpoint.provider_metadata->>'identity_strength' AS identity_strength,
                resource.name,
                endpoint.endpoint_key,
                resource.access_level::text AS access_level,
                resource.access_capabilities,
                COALESCE((
                    SELECT jsonb_object_agg(
                        capability.name,
                        jsonb_build_object(
                            'status', capability.evidence->>'status',
                            'method', capability.evidence->>'method',
                            'scope', capability.evidence->>'scope'
                        )
                        ORDER BY capability.name
                    )
                    FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                    WHERE capability.name <> '_metadata'
                ), '{}'::jsonb) AS comparable_capabilities,
                EXISTS (
                    SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                    WHERE capability.name <> '_metadata'
                      AND capability.evidence->>'status' <> 'not_tested'
                ) AS capability_observation_present,
                resource.access_capabilities#>>'{_metadata,assessed_identity_fingerprint}'
                    AS capability_identity_fingerprint,
                CASE
                    WHEN COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) = 'smb'
                    THEN COALESCE(resource.access_capabilities#>>'{_metadata,finalized}', 'false') = 'true'
                         AND COALESCE(resource.access_capabilities#>>'{_metadata,degraded}', 'true') = 'false'
                         AND COALESCE(resource.access_capabilities#>>'{_metadata,transport_failed}', 'true') = 'false'
                         AND COALESCE(
                             resource.access_capabilities#>>'{_metadata,assessed_identity_fingerprint}', ''
                         ) <> ''
                    ELSE FALSE
                END AS capability_observation_valid,
                CASE
                    WHEN COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) = 'smb'
                    THEN COALESCE(resource.access_capabilities->'list'->>'status', '') = 'allowed'
                         AND COALESCE(
                             resource.access_capabilities#>>'{_metadata,listing_truncated}', 'true'
                         ) = 'false'
                    WHEN COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1))
                         = 'sharepoint'
                    THEN TRUE
                    ELSE FALSE
                END AS content_observation_complete,
                resource.exposure,
                resource.permission_summary,
                resource.permission_summary || jsonb_build_object(
                    'status', CASE
                        WHEN COALESCE(resource.permission_summary->>'evidence_available', 'false') = 'true'
                            THEN COALESCE(resource.permission_summary->>'status', 'available')
                        WHEN EXISTS (
                            SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' <> 'not_tested'
                        ) THEN 'observed_capabilities'
                        ELSE 'not_assessed'
                    END,
                    'evidence_available',
                        COALESCE(resource.permission_summary->>'evidence_available', 'false') = 'true'
                        OR EXISTS (
                            SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' <> 'not_tested'
                        ),
                    'direct_permissions', resource.permission_summary,
                    'capability_observations', jsonb_build_object(
                        'evidence_available', EXISTS (
                            SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' <> 'not_tested'
                        ),
                        'allowed', COALESCE((
                            SELECT jsonb_agg(capability.name ORDER BY capability.name)
                            FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' IN ('allowed', 'mixed')
                        ), '[]'::jsonb),
                        'denied', COALESCE((
                            SELECT jsonb_agg(capability.name ORDER BY capability.name)
                            FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' IN ('denied', 'mixed')
                        ), '[]'::jsonb),
                        'inconclusive', COALESCE((
                            SELECT jsonb_agg(capability.name ORDER BY capability.name)
                            FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' = 'inconclusive'
                        ), '[]'::jsonb),
                        'raw', resource.access_capabilities
                    ),
                    'compatibility_access_level', resource.access_level::text,
                    'exposure', resource.exposure
                ) AS access_evidence_summary,
                COALESCE(baseline_counts.item_count, 0) AS item_count
            FROM resources AS resource
            JOIN endpoints AS endpoint
              ON endpoint.id = resource.endpoint_id AND endpoint.run_id = resource.run_id
            LEFT JOIN baseline_counts ON baseline_counts.resource_id = resource.id
            WHERE resource.run_id = %(baseline_run_id)s
              AND resource.identity_key = ANY(%(identity_keys)s)
        ),
        current AS (
            SELECT
                resource.id,
                resource.identity_key,
                resource.resource_type::text AS resource_type,
                COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) AS provider,
                resource.provider_resource_id,
                endpoint.provider_metadata->>'identity_strength' AS identity_strength,
                resource.name,
                endpoint.endpoint_key,
                resource.access_level::text AS access_level,
                resource.access_capabilities,
                COALESCE((
                    SELECT jsonb_object_agg(
                        capability.name,
                        jsonb_build_object(
                            'status', capability.evidence->>'status',
                            'method', capability.evidence->>'method',
                            'scope', capability.evidence->>'scope'
                        )
                        ORDER BY capability.name
                    )
                    FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                    WHERE capability.name <> '_metadata'
                ), '{}'::jsonb) AS comparable_capabilities,
                EXISTS (
                    SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                    WHERE capability.name <> '_metadata'
                      AND capability.evidence->>'status' <> 'not_tested'
                ) AS capability_observation_present,
                resource.access_capabilities#>>'{_metadata,assessed_identity_fingerprint}'
                    AS capability_identity_fingerprint,
                CASE
                    WHEN COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) = 'smb'
                    THEN COALESCE(resource.access_capabilities#>>'{_metadata,finalized}', 'false') = 'true'
                         AND COALESCE(resource.access_capabilities#>>'{_metadata,degraded}', 'true') = 'false'
                         AND COALESCE(resource.access_capabilities#>>'{_metadata,transport_failed}', 'true') = 'false'
                         AND COALESCE(
                             resource.access_capabilities#>>'{_metadata,assessed_identity_fingerprint}', ''
                         ) <> ''
                    ELSE FALSE
                END AS capability_observation_valid,
                CASE
                    WHEN COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) = 'smb'
                    THEN COALESCE(resource.access_capabilities->'list'->>'status', '') = 'allowed'
                         AND COALESCE(
                             resource.access_capabilities#>>'{_metadata,listing_truncated}', 'true'
                         ) = 'false'
                    WHEN COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1))
                         = 'sharepoint'
                    THEN TRUE
                    ELSE FALSE
                END AS content_observation_complete,
                resource.exposure,
                resource.permission_summary,
                resource.permission_summary || jsonb_build_object(
                    'status', CASE
                        WHEN COALESCE(resource.permission_summary->>'evidence_available', 'false') = 'true'
                            THEN COALESCE(resource.permission_summary->>'status', 'available')
                        WHEN EXISTS (
                            SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' <> 'not_tested'
                        ) THEN 'observed_capabilities'
                        ELSE 'not_assessed'
                    END,
                    'evidence_available',
                        COALESCE(resource.permission_summary->>'evidence_available', 'false') = 'true'
                        OR EXISTS (
                            SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' <> 'not_tested'
                        ),
                    'direct_permissions', resource.permission_summary,
                    'capability_observations', jsonb_build_object(
                        'evidence_available', EXISTS (
                            SELECT 1 FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' <> 'not_tested'
                        ),
                        'allowed', COALESCE((
                            SELECT jsonb_agg(capability.name ORDER BY capability.name)
                            FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' IN ('allowed', 'mixed')
                        ), '[]'::jsonb),
                        'denied', COALESCE((
                            SELECT jsonb_agg(capability.name ORDER BY capability.name)
                            FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' IN ('denied', 'mixed')
                        ), '[]'::jsonb),
                        'inconclusive', COALESCE((
                            SELECT jsonb_agg(capability.name ORDER BY capability.name)
                            FROM jsonb_each(resource.access_capabilities) AS capability(name, evidence)
                            WHERE capability.name <> '_metadata'
                              AND capability.evidence->>'status' = 'inconclusive'
                        ), '[]'::jsonb),
                        'raw', resource.access_capabilities
                    ),
                    'compatibility_access_level', resource.access_level::text,
                    'exposure', resource.exposure
                ) AS access_evidence_summary,
                COALESCE(current_counts.item_count, 0) AS item_count
            FROM resources AS resource
            JOIN endpoints AS endpoint
              ON endpoint.id = resource.endpoint_id AND endpoint.run_id = resource.run_id
            LEFT JOIN current_counts ON current_counts.resource_id = resource.id
            WHERE resource.run_id = %(current_run_id)s
              AND resource.identity_key = ANY(%(identity_keys)s)
        ),
        paired AS (
            SELECT
                COALESCE(current.identity_key, baseline.identity_key) AS identity_key,
                baseline.id AS before_id,
                current.id AS after_id,
                COALESCE(current.provider, baseline.provider) AS provider,
                COALESCE(current.resource_type, baseline.resource_type) AS resource_type,
                COALESCE(current.provider_resource_id, baseline.provider_resource_id) AS provider_resource_id,
                COALESCE(current.identity_strength, baseline.identity_strength) AS identity_strength,
                baseline.name AS before_name,
                current.name AS after_name,
                baseline.endpoint_key AS before_endpoint,
                current.endpoint_key AS after_endpoint,
                baseline.access_level AS before_access,
                current.access_level AS after_access,
                baseline.access_capabilities AS before_capabilities,
                current.access_capabilities AS after_capabilities,
                baseline.comparable_capabilities AS before_comparable_capabilities,
                current.comparable_capabilities AS after_comparable_capabilities,
                baseline.capability_observation_present AS before_capability_present,
                current.capability_observation_present AS after_capability_present,
                baseline.capability_identity_fingerprint AS before_capability_identity,
                current.capability_identity_fingerprint AS after_capability_identity,
                baseline.capability_observation_valid AS before_capability_valid,
                current.capability_observation_valid AS after_capability_valid,
                (
                    %(capability_access)s
                    AND baseline.capability_observation_present
                    AND current.capability_observation_present
                    AND baseline.capability_observation_valid
                    AND current.capability_observation_valid
                    AND baseline.capability_identity_fingerprint = current.capability_identity_fingerprint
                ) AS capability_access_comparable,
                baseline.content_observation_complete AS before_content_complete,
                current.content_observation_complete AS after_content_complete,
                baseline.exposure AS before_exposure,
                current.exposure AS after_exposure,
                baseline.permission_summary AS before_permissions,
                current.permission_summary AS after_permissions,
                baseline.access_evidence_summary AS before_access_summary,
                current.access_evidence_summary AS after_access_summary,
                baseline.item_count AS before_items,
                current.item_count AS after_items,
                (%(direct_permissions)s AND COALESCE(permission_rollup.directly_comparable, FALSE))
                    AS direct_access_comparable,
                COALESCE(permission_rollup.evidence_present, FALSE) AS permission_evidence_present,
                (
                    COALESCE(permission_rollup.evidence_present, FALSE)
                    OR baseline.capability_observation_present
                    OR current.capability_observation_present
                ) AS access_evidence_present,
                COALESCE(permission_rollup.scope_exact, FALSE) AS direct_access_scope_exact,
                COALESCE(permission_rollup.evidence_difference, FALSE) AS raw_permission_difference,
                COALESCE(permission_rollup.quality_difference, FALSE) AS permission_quality_difference
            FROM baseline
            FULL OUTER JOIN current USING (identity_key)
            LEFT JOIN permission_rollup
              ON permission_rollup.identity_key = COALESCE(current.identity_key, baseline.identity_key)
        ),
        classified AS (
            SELECT
                paired.*,
                (before_id IS NOT NULL AND after_id IS NOT NULL AND %(structural)s AND (
                    CASE
                        WHEN provider = 'smb'
                        THEN lower(before_name) IS DISTINCT FROM lower(after_name)
                        ELSE before_name IS DISTINCT FROM after_name
                    END
                    OR CASE
                        WHEN provider IN ('smb', 'nfs')
                        THEN lower(before_endpoint) IS DISTINCT FROM lower(after_endpoint)
                        ELSE before_endpoint IS DISTINCT FROM after_endpoint
                    END
                )) AS location_changed,
                (before_id IS NOT NULL AND after_id IS NOT NULL AND NOT %(structural)s AND (
                    CASE
                        WHEN provider = 'smb'
                        THEN lower(before_name) IS DISTINCT FROM lower(after_name)
                        ELSE before_name IS DISTINCT FROM after_name
                    END
                    OR CASE
                        WHEN provider IN ('smb', 'nfs')
                        THEN lower(before_endpoint) IS DISTINCT FROM lower(after_endpoint)
                        ELSE before_endpoint IS DISTINCT FROM after_endpoint
                    END
                )) AS location_indeterminate,
                (before_id IS NOT NULL AND after_id IS NOT NULL
                    AND capability_access_comparable
                    AND (
                    before_access IS DISTINCT FROM after_access
                    OR before_comparable_capabilities IS DISTINCT FROM after_comparable_capabilities
                )) AS access_changed,
                (before_id IS NOT NULL AND after_id IS NOT NULL AND direct_access_comparable
                    AND raw_permission_difference) AS permissions_changed,
                (before_id IS NOT NULL AND after_id IS NOT NULL AND %(content)s
                    AND before_content_complete AND after_content_complete
                    AND before_items IS DISTINCT FROM after_items) AS item_count_changed,
                (before_id IS NOT NULL AND after_id IS NOT NULL
                    AND (before_capability_present OR after_capability_present)
                    AND (
                        NOT %(capability_access)s
                        OR before_capability_present IS DISTINCT FROM after_capability_present
                        OR NOT before_capability_valid
                        OR NOT after_capability_valid
                        OR before_capability_identity IS DISTINCT FROM after_capability_identity
                    )) AS access_indeterminate,
                (before_id IS NOT NULL AND after_id IS NOT NULL
                    AND before_exposure IS DISTINCT FROM after_exposure) AS exposure_indeterminate,
                (before_id IS NOT NULL AND after_id IS NOT NULL AND (
                    (permission_evidence_present AND NOT direct_access_comparable)
                    OR (
                        %(direct_permissions)s
                        AND provider IN ('smb', 'sharepoint')
                        AND NOT permission_evidence_present
                    )
                    OR permission_quality_difference
                    OR (NOT direct_access_comparable AND raw_permission_difference)
                )) AS permissions_indeterminate,
                (before_id IS NOT NULL AND after_id IS NOT NULL
                    AND before_items IS DISTINCT FROM after_items
                    AND (
                        NOT %(content)s
                        OR NOT before_content_complete
                        OR NOT after_content_complete
                    )) AS content_indeterminate
            FROM paired
        ),
        shaped AS (
            SELECT
                *,
                ARRAY_REMOVE(ARRAY[
                    CASE WHEN location_changed THEN 'location' END,
                    CASE WHEN location_indeterminate THEN 'structure_not_comparable' END,
                    CASE WHEN access_changed THEN 'access' END,
                    CASE WHEN permissions_changed THEN 'permission_evidence' END,
                    CASE WHEN item_count_changed THEN 'item_count' END,
                    CASE WHEN access_indeterminate THEN 'access_not_comparable' END,
                    CASE WHEN exposure_indeterminate THEN 'exposure_not_comparable' END,
                    CASE WHEN permissions_indeterminate THEN 'permission_evidence_not_comparable' END,
                    CASE WHEN content_indeterminate THEN 'item_count_not_comparable' END
                ]::text[], NULL) AS categories,
                CASE
                    WHEN before_id IS NULL AND %(structural)s THEN 'appeared'
                    WHEN before_id IS NULL THEN 'indeterminate'
                    WHEN after_id IS NULL AND %(structural)s THEN 'disappeared'
                    WHEN after_id IS NULL THEN 'indeterminate'
                    WHEN location_changed OR access_changed OR permissions_changed OR item_count_changed THEN 'changed'
                    WHEN location_indeterminate OR access_indeterminate OR exposure_indeterminate
                         OR permissions_indeterminate OR content_indeterminate THEN 'indeterminate'
                    ELSE NULL
                END AS change_type
            FROM classified
        )
        INSERT INTO comparison_resource_changes (
            comparison_id, identity_key, change_type, provider, resource_type,
            provider_resource_id, match_basis, match_quality, before_resource_id,
            after_resource_id, endpoint_key_before, endpoint_key_after,
            resource_name_before, resource_name_after, change_categories,
            structural_state, access_state, content_state, access_interpretation,
            item_count_before, item_count_after, before_snapshot, after_snapshot,
            search_text, impact_rank
        )
        SELECT
            %(comparison_id)s,
            identity_key,
            change_type,
            provider,
            resource_type,
            provider_resource_id,
            CASE WHEN provider_resource_id IS NOT NULL THEN 'provider_resource_id' ELSE 'location' END,
            CASE
                WHEN provider_resource_id IS NULL THEN 'weak'
                WHEN provider = 'smb'
                     AND %(identity_scope_exact)s
                     AND identity_strength = 'strong' THEN 'strong'
                WHEN provider = 'smb' AND identity_strength IN ('moderate', 'weak')
                    THEN identity_strength
                WHEN provider = 'smb' THEN 'weak'
                ELSE 'strong'
            END,
            before_id,
            after_id,
            before_endpoint,
            after_endpoint,
            before_name,
            after_name,
            to_jsonb(categories),
            CASE
                WHEN before_id IS NULL AND %(structural)s THEN 'appeared'
                WHEN before_id IS NULL THEN 'indeterminate'
                WHEN after_id IS NULL AND %(structural)s THEN 'disappeared'
                WHEN after_id IS NULL THEN 'indeterminate'
                WHEN location_changed THEN 'changed'
                WHEN location_indeterminate THEN 'indeterminate'
                ELSE 'unchanged'
            END,
            CASE
                WHEN before_id IS NULL OR after_id IS NULL THEN 'not_comparable'
                WHEN access_changed OR permissions_changed THEN 'changed'
                WHEN access_indeterminate OR exposure_indeterminate OR permissions_indeterminate
                THEN 'not_comparable'
                WHEN direct_access_comparable OR capability_access_comparable THEN 'unchanged'
                WHEN NOT access_evidence_present THEN 'not_assessed'
                ELSE 'not_comparable'
            END,
            CASE
                WHEN before_id IS NULL OR after_id IS NULL THEN 'not_computed'
                WHEN item_count_changed THEN 'changed'
                WHEN content_indeterminate THEN 'not_comparable'
                ELSE 'not_computed'
            END,
            CASE
                WHEN permissions_changed AND direct_access_scope_exact
                THEN 'scope-complete permission evidence changed; effective access was not computed'
                WHEN permissions_changed
                THEN 'observed permission evidence changed; negative and effective-access conclusions are unsupported'
                WHEN access_changed
                THEN 'bounded capability observations changed; effective access was not computed'
                WHEN permissions_indeterminate OR exposure_indeterminate
                THEN 'permission or exposure evidence is incomplete or not comparable; no negative conclusion can be drawn'
                WHEN direct_access_comparable
                THEN 'comparable direct-permission evidence was unchanged; effective access was not computed'
                WHEN capability_access_comparable
                THEN 'bounded capability observations were unchanged; effective access was not computed'
                WHEN NOT access_evidence_present THEN 'access was not assessed for either run'
                ELSE 'access evidence was not comparable'
            END,
            before_items,
            after_items,
            CASE WHEN before_id IS NULL THEN '{}'::jsonb ELSE jsonb_build_object(
                'resource_id', before_id,
                'endpoint_key', before_endpoint,
                'name', before_name,
                'access_level', before_access,
                'access_capabilities', before_capabilities,
                'exposure', before_exposure,
                'access_evidence_summary', before_access_summary,
                'item_count', before_items
            ) END,
            CASE WHEN after_id IS NULL THEN '{}'::jsonb ELSE jsonb_build_object(
                'resource_id', after_id,
                'endpoint_key', after_endpoint,
                'name', after_name,
                'access_level', after_access,
                'access_capabilities', after_capabilities,
                'exposure', after_exposure,
                'access_evidence_summary', after_access_summary,
                'item_count', after_items
            ) END,
            lower(concat_ws(
                ' ',
                before_name,
                after_name,
                before_endpoint,
                after_endpoint,
                provider_resource_id
            )),
            CASE
                WHEN after_id IS NULL AND %(structural)s THEN 100
                WHEN before_id IS NULL AND %(structural)s THEN 90
                WHEN access_changed OR permissions_changed THEN 80
                WHEN change_type = 'indeterminate' THEN 70
                WHEN item_count_changed THEN 50
                WHEN location_changed THEN 40
                ELSE 10
            END
        FROM shaped
        WHERE change_type IS NOT NULL
        ON CONFLICT (comparison_id, identity_key) DO NOTHING
        RETURNING id
        """,
        {
            "comparison_id": comparison_id,
            "baseline_run_id": baseline_run_id,
            "current_run_id": current_run_id,
            "identity_keys": identity_keys,
            "structural": structural,
            "content": content,
            "capability_access": capability_access,
            "direct_permissions": direct_permissions_expected,
            "identity_scope_exact": compatibility.get("identity_scope_exact") is True,
        },
    ).fetchall()
    return len(row)


def _next_item_resource_change(
    conn: psycopg.Connection,
    comparison_id: str,
    after_id: int,
) -> tuple[int, int | None, int | None, str] | None:
    row = conn.execute(
        """
        SELECT id, before_resource_id, after_resource_id, provider
        FROM comparison_resource_changes
        WHERE comparison_id = %s
          AND id > %s
          AND (before_resource_id IS NOT NULL OR after_resource_id IS NOT NULL)
        ORDER BY id
        LIMIT 1
        """,
        (comparison_id, after_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1]) if row[1] is not None else None, int(row[2]) if row[2] is not None else None, str(row[3])


def _item_resource_change_by_id(
    conn: psycopg.Connection,
    comparison_id: str,
    resource_change_id: int,
) -> tuple[int, int | None, int | None, str] | None:
    row = conn.execute(
        """
        SELECT id, before_resource_id, after_resource_id, provider
        FROM comparison_resource_changes
        WHERE comparison_id = %s AND id = %s
        """,
        (comparison_id, resource_change_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1]) if row[1] is not None else None, int(row[2]) if row[2] is not None else None, str(row[3])


def _mark_item_identity_indeterminate(
    conn: psycopg.Connection,
    comparison_id: str,
    resource_change_id: int,
) -> None:
    conn.execute(
        """
        UPDATE comparison_resource_changes
        SET change_type = CASE
                WHEN change_categories = '["item_history_candidate"]'::jsonb THEN 'indeterminate'
                ELSE change_type
            END,
            change_categories = CASE
                WHEN change_categories = '["item_history_candidate"]'::jsonb
                THEN '["item_identity_ambiguous"]'::jsonb
                WHEN change_categories ? 'item_identity_ambiguous' THEN change_categories
                ELSE change_categories || '["item_identity_ambiguous"]'::jsonb
            END,
            content_state = 'indeterminate',
            access_interpretation =
                'item history is indeterminate because item identities are missing or duplicated',
            impact_rank = GREATEST(impact_rank, 60)
        WHERE comparison_id = %s AND id = %s
        """,
        (comparison_id, resource_change_id),
    )


def _ensure_item_candidate_resource_changes(
    conn: psycopg.Connection,
    *,
    comparison_id: str,
    baseline_run_id: str,
    current_run_id: str,
    identity_keys: list[str],
) -> int:
    if not identity_keys:
        return 0
    rows = conn.execute(
        """
        WITH baseline AS (
            SELECT resource.*, endpoint.endpoint_key
            FROM resources AS resource
            JOIN endpoints AS endpoint
              ON endpoint.id = resource.endpoint_id AND endpoint.run_id = resource.run_id
            WHERE resource.run_id = %(baseline_run_id)s
              AND resource.identity_key = ANY(%(identity_keys)s)
        ),
        current AS (
            SELECT resource.*, endpoint.endpoint_key
            FROM resources AS resource
            JOIN endpoints AS endpoint
              ON endpoint.id = resource.endpoint_id AND endpoint.run_id = resource.run_id
            WHERE resource.run_id = %(current_run_id)s
              AND resource.identity_key = ANY(%(identity_keys)s)
        )
        INSERT INTO comparison_resource_changes (
            comparison_id, identity_key, change_type, provider, resource_type,
            provider_resource_id, match_basis, match_quality, before_resource_id,
            after_resource_id, endpoint_key_before, endpoint_key_after,
            resource_name_before, resource_name_after, change_categories,
            structural_state, access_state, content_state, access_interpretation,
            item_count_before, item_count_after, before_snapshot, after_snapshot,
            search_text, impact_rank
        )
        SELECT
            %(comparison_id)s,
            current.identity_key,
            'changed',
            COALESCE(current.provider, split_part(current.resource_type::text, '_', 1)),
            current.resource_type::text,
            current.provider_resource_id,
            CASE WHEN current.provider_resource_id IS NOT NULL THEN 'provider_resource_id' ELSE 'location' END,
            CASE WHEN current.provider_resource_id IS NOT NULL THEN 'strong' ELSE 'weak' END,
            baseline.id,
            current.id,
            baseline.endpoint_key,
            current.endpoint_key,
            baseline.name,
            current.name,
            '["item_history_candidate"]'::jsonb,
            'unchanged',
            'not_assessed',
            'pending',
            'resource is temporarily materialized while item-level changes are evaluated',
            NULL,
            NULL,
            jsonb_build_object(
                'resource_id', baseline.id,
                'endpoint_key', baseline.endpoint_key,
                'name', baseline.name,
                'access_level', baseline.access_level,
                'exposure', baseline.exposure
            ),
            jsonb_build_object(
                'resource_id', current.id,
                'endpoint_key', current.endpoint_key,
                'name', current.name,
                'access_level', current.access_level,
                'exposure', current.exposure
            ),
            lower(concat_ws(' ', baseline.name, current.name, baseline.endpoint_key, current.endpoint_key)),
            0
        FROM baseline
        JOIN current USING (identity_key)
        WHERE EXISTS (
            SELECT 1
            FROM items AS item
            WHERE (
                    item.run_id = %(baseline_run_id)s
                    AND item.resource_id = baseline.id
                  )
               OR (
                    item.run_id = %(current_run_id)s
                    AND item.resource_id = current.id
                  )
            LIMIT 1
        )
        ON CONFLICT (comparison_id, identity_key) DO NOTHING
        RETURNING id
        """,
        {
            "comparison_id": comparison_id,
            "baseline_run_id": baseline_run_id,
            "current_run_id": current_run_id,
            "identity_keys": identity_keys,
        },
    ).fetchall()
    return len(rows)


def _finalize_item_candidate_resources(conn: psycopg.Connection, comparison_id: str) -> None:
    conn.execute(
        """
        DELETE FROM comparison_resource_changes AS resource_change
        WHERE resource_change.comparison_id = %s
          AND resource_change.change_categories = '["item_history_candidate"]'::jsonb
          AND resource_change.content_state <> 'indeterminate'
          AND NOT EXISTS (
              SELECT 1
              FROM comparison_item_changes AS item_change
              WHERE item_change.resource_change_id = resource_change.id
          )
        """,
        (comparison_id,),
    )
    conn.execute(
        """
        UPDATE comparison_resource_changes AS resource_change
        SET change_categories = '["item_changes"]'::jsonb,
            content_state = 'changed',
            access_interpretation = 'item-level changes were materialized; access evidence was unchanged or not assessed',
            impact_rank = GREATEST(resource_change.impact_rank, 50)
        WHERE resource_change.comparison_id = %s
          AND resource_change.change_categories = '["item_history_candidate"]'::jsonb
          AND EXISTS (
              SELECT 1
              FROM comparison_item_changes AS item_change
              WHERE item_change.resource_change_id = resource_change.id
          )
        """,
        (comparison_id,),
    )


def _item_identity_is_ambiguous(
    conn: psycopg.Connection,
    before_resource_id: int | None,
    after_resource_id: int | None,
) -> bool:
    resource_ids = [resource_id for resource_id in (before_resource_id, after_resource_id) if resource_id is not None]
    if not resource_ids:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM items
        WHERE resource_id = ANY(%s)
          AND deleted IS FALSE
        GROUP BY resource_id, identity_key
        HAVING identity_key IS NULL OR COUNT(*) > 1
        LIMIT 1
        """,
        (resource_ids,),
    ).fetchone()
    return row is not None


def _item_change_batch_keys(
    conn: psycopg.Connection,
    before_resource_id: int | None,
    after_resource_id: int | None,
    after_key: str | None,
    limit: int,
) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT identity_key
            FROM (
                SELECT identity_key
                FROM items
                WHERE resource_id = %s AND deleted IS FALSE
                UNION
                SELECT identity_key
                FROM items
                WHERE resource_id = %s AND deleted IS FALSE
            ) AS identities
            WHERE identity_key IS NOT NULL
              AND (%s::text IS NULL OR identity_key > %s::text)
            ORDER BY identity_key
            LIMIT %s
            """,
            (before_resource_id, after_resource_id, after_key, after_key, max(1, limit)),
        ).fetchall()
    ]


def _materialize_item_change_batch(
    conn: psycopg.Connection,
    *,
    comparison_id: str,
    resource_change_id: int,
    before_resource_id: int | None,
    after_resource_id: int | None,
    provider: str,
    identity_keys: list[str],
    direct_permissions_comparable: bool,
) -> int:
    if not identity_keys:
        return 0
    rows = conn.execute(
        """
        WITH baseline AS (
            SELECT
                item.id,
                item.identity_key,
                item.provider_item_id,
                item.provider_parent_id,
                item.path,
                item.name,
                item.is_dir,
                item.size_bytes,
                item.allocation_size_bytes,
                item.mtime,
                item.created_at,
                item.accessed_at,
                item.changed_at,
                item.file_attributes,
                item.mime_type,
                item.web_url,
                item.exposure,
                encode(sha256(convert_to(item.provider_metadata::text, 'UTF8')), 'hex')
                    AS provider_metadata_hash,
                item.permission_summary
            FROM items AS item
            WHERE item.resource_id = %(before_resource_id)s
              AND item.identity_key = ANY(%(identity_keys)s)
              AND item.deleted IS FALSE
        ),
        current AS (
            SELECT
                item.id,
                item.identity_key,
                item.provider_item_id,
                item.provider_parent_id,
                item.path,
                item.name,
                item.is_dir,
                item.size_bytes,
                item.allocation_size_bytes,
                item.mtime,
                item.created_at,
                item.accessed_at,
                item.changed_at,
                item.file_attributes,
                item.mime_type,
                item.web_url,
                item.exposure,
                encode(sha256(convert_to(item.provider_metadata::text, 'UTF8')), 'hex')
                    AS provider_metadata_hash,
                item.permission_summary
            FROM items AS item
            WHERE item.resource_id = %(after_resource_id)s
              AND item.identity_key = ANY(%(identity_keys)s)
              AND item.deleted IS FALSE
        ),
        paired AS (
            SELECT
                COALESCE(current.identity_key, baseline.identity_key) AS identity_key,
                baseline.id AS before_id,
                current.id AS after_id,
                COALESCE(current.provider_item_id, baseline.provider_item_id) AS provider_item_id,
                baseline.provider_parent_id AS before_parent_id,
                current.provider_parent_id AS after_parent_id,
                baseline.path AS before_path,
                current.path AS after_path,
                baseline.name AS before_name,
                current.name AS after_name,
                baseline.is_dir AS before_is_dir,
                current.is_dir AS after_is_dir,
                baseline.size_bytes AS before_size,
                current.size_bytes AS after_size,
                baseline.allocation_size_bytes AS before_allocation_size,
                current.allocation_size_bytes AS after_allocation_size,
                baseline.mtime AS before_mtime,
                current.mtime AS after_mtime,
                baseline.created_at AS before_created_at,
                current.created_at AS after_created_at,
                baseline.accessed_at AS before_accessed_at,
                current.accessed_at AS after_accessed_at,
                baseline.changed_at AS before_changed_at,
                current.changed_at AS after_changed_at,
                baseline.file_attributes AS before_attributes,
                current.file_attributes AS after_attributes,
                baseline.mime_type AS before_mime_type,
                current.mime_type AS after_mime_type,
                baseline.web_url AS before_web_url,
                current.web_url AS after_web_url,
                baseline.exposure AS before_exposure,
                current.exposure AS after_exposure,
                baseline.provider_metadata_hash AS before_provider_metadata_hash,
                current.provider_metadata_hash AS after_provider_metadata_hash,
                baseline.permission_summary AS before_permissions,
                current.permission_summary AS after_permissions,
                COALESCE((baseline.permission_summary->>'evidence_available')::boolean, FALSE)
                    AS before_permission_present,
                COALESCE((current.permission_summary->>'evidence_available')::boolean, FALSE)
                    AS after_permission_present,
                COALESCE((baseline.permission_summary->>'comparable')::boolean, FALSE)
                    AS before_permission_comparable,
                COALESCE((current.permission_summary->>'comparable')::boolean, FALSE)
                    AS after_permission_comparable,
                baseline.permission_summary->>'comparison_evidence_hash' AS before_permission_hash,
                current.permission_summary->>'comparison_evidence_hash' AS after_permission_hash,
                baseline.permission_summary->>'comparison_quality_hash' AS before_permission_quality_hash,
                current.permission_summary->>'comparison_quality_hash' AS after_permission_quality_hash
            FROM baseline
            FULL OUTER JOIN current USING (identity_key)
        ),
        classified AS (
            SELECT
                *,
                before_id IS NOT NULL AND after_id IS NOT NULL AND provider_item_id IS NOT NULL
                    AND (
                        before_parent_id IS DISTINCT FROM after_parent_id
                        OR (
                            before_path IS DISTINCT FROM after_path
                            AND before_name IS NOT DISTINCT FROM after_name
                        )
                    ) AS moved,
                before_id IS NOT NULL AND after_id IS NOT NULL AND provider_item_id IS NOT NULL
                    AND before_name IS DISTINCT FROM after_name AS renamed,
                before_id IS NOT NULL AND after_id IS NOT NULL AND provider_item_id IS NOT NULL
                    AND before_path IS DISTINCT FROM after_path AS path_changed,
                before_id IS NOT NULL AND after_id IS NOT NULL AND (
                    before_is_dir IS DISTINCT FROM after_is_dir
                    OR before_size IS DISTINCT FROM after_size
                    OR before_allocation_size IS DISTINCT FROM after_allocation_size
                    OR before_mtime IS DISTINCT FROM after_mtime
                    OR before_created_at IS DISTINCT FROM after_created_at
                    OR before_accessed_at IS DISTINCT FROM after_accessed_at
                    OR before_changed_at IS DISTINCT FROM after_changed_at
                    OR before_attributes IS DISTINCT FROM after_attributes
                    OR before_mime_type IS DISTINCT FROM after_mime_type
                    OR before_web_url IS DISTINCT FROM after_web_url
                    OR before_exposure IS DISTINCT FROM after_exposure
                    OR before_provider_metadata_hash IS DISTINCT FROM after_provider_metadata_hash
                ) AS metadata_changed,
                before_id IS NOT NULL AND after_id IS NOT NULL
                    AND %(direct_permissions_comparable)s
                    AND before_permission_present AND after_permission_present
                    AND before_permission_comparable AND after_permission_comparable
                    AND before_permission_hash IS NOT NULL AND after_permission_hash IS NOT NULL
                    AND before_permission_quality_hash IS NOT NULL
                    AND after_permission_quality_hash IS NOT NULL
                    AND before_permission_quality_hash IS NOT DISTINCT FROM after_permission_quality_hash
                    AND before_permission_hash IS DISTINCT FROM after_permission_hash
                    AS raw_permission_changed,
                before_id IS NOT NULL AND after_id IS NOT NULL
                    AND (before_permission_present OR after_permission_present)
                    AND (
                        NOT %(direct_permissions_comparable)s
                        OR NOT before_permission_comparable
                        OR NOT after_permission_comparable
                        OR before_permission_hash IS NULL
                        OR after_permission_hash IS NULL
                        OR before_permission_quality_hash IS DISTINCT FROM after_permission_quality_hash
                    )
                    AND (
                        (before_permissions - 'observed_at') IS DISTINCT FROM
                            (after_permissions - 'observed_at')
                        OR before_permission_quality_hash IS DISTINCT FROM after_permission_quality_hash
                    ) AS permission_indeterminate
            FROM paired
        ),
        shaped AS (
            SELECT
                *,
                ARRAY_REMOVE(ARRAY[
                    CASE WHEN before_id IS NULL THEN 'added' END,
                    CASE WHEN after_id IS NULL THEN 'removed' END,
                    CASE WHEN moved THEN 'moved' END,
                    CASE WHEN renamed THEN 'renamed' END,
                    CASE WHEN path_changed THEN 'path' END,
                    CASE WHEN metadata_changed THEN 'metadata' END,
                    CASE WHEN raw_permission_changed THEN 'permission' END,
                    CASE WHEN permission_indeterminate THEN 'permission_not_comparable' END
                ]::text[], NULL) AS categories,
                CASE
                    WHEN before_id IS NULL THEN 'added'
                    WHEN after_id IS NULL THEN 'removed'
                    WHEN moved THEN 'moved'
                    WHEN renamed THEN 'renamed'
                    WHEN raw_permission_changed THEN 'permission_changed'
                    WHEN metadata_changed THEN 'metadata_changed'
                    WHEN permission_indeterminate THEN 'indeterminate'
                    ELSE NULL
                END AS change_type
            FROM classified
        )
        INSERT INTO comparison_item_changes (
            comparison_id, resource_change_id, identity_key, change_type, provider,
            before_item_id, after_item_id, match_basis, match_quality,
            change_categories, evidence_state, limitations, before_snapshot,
            after_snapshot, search_text, impact_rank
        )
        SELECT
            %(comparison_id)s,
            %(resource_change_id)s,
            identity_key,
            change_type,
            %(provider)s,
            before_id,
            after_id,
            CASE WHEN provider_item_id IS NOT NULL THEN 'provider_item_id' ELSE 'path' END,
            CASE WHEN provider_item_id IS NOT NULL THEN 'strong' ELSE 'weak' END,
            to_jsonb(categories),
            CASE
                WHEN permission_indeterminate THEN 'indeterminate'
                WHEN provider_item_id IS NOT NULL THEN 'exact'
                ELSE 'bounded'
            END,
            CASE
                WHEN permission_indeterminate
                THEN '["Permission summaries differ, but their evidence planes are not comparable."]'::jsonb
                WHEN provider_item_id IS NULL
                THEN '["Path fallback cannot distinguish a move or rename from removal plus addition."]'::jsonb
                ELSE '[]'::jsonb
            END,
            CASE WHEN before_id IS NULL THEN '{}'::jsonb ELSE jsonb_build_object(
                'item_id', before_id,
                'provider_item_id', provider_item_id,
                'provider_parent_id', before_parent_id,
                'path', before_path,
                'name', before_name,
                'is_dir', before_is_dir,
                'size_bytes', before_size,
                'allocation_size_bytes', before_allocation_size,
                'mtime', before_mtime,
                'created_at', before_created_at,
                'accessed_at', before_accessed_at,
                'changed_at', before_changed_at,
                'file_attributes', before_attributes,
                'mime_type', before_mime_type,
                'web_url', before_web_url,
                'exposure', before_exposure,
                'provider_metadata_hash', before_provider_metadata_hash,
                'permission_evidence_hash', before_permission_hash,
                'permission_quality_hash', before_permission_quality_hash,
                'permission_comparable', before_permission_comparable
            ) END,
            CASE WHEN after_id IS NULL THEN '{}'::jsonb ELSE jsonb_build_object(
                'item_id', after_id,
                'provider_item_id', provider_item_id,
                'provider_parent_id', after_parent_id,
                'path', after_path,
                'name', after_name,
                'is_dir', after_is_dir,
                'size_bytes', after_size,
                'allocation_size_bytes', after_allocation_size,
                'mtime', after_mtime,
                'created_at', after_created_at,
                'accessed_at', after_accessed_at,
                'changed_at', after_changed_at,
                'file_attributes', after_attributes,
                'mime_type', after_mime_type,
                'web_url', after_web_url,
                'exposure', after_exposure,
                'provider_metadata_hash', after_provider_metadata_hash,
                'permission_evidence_hash', after_permission_hash,
                'permission_quality_hash', after_permission_quality_hash,
                'permission_comparable', after_permission_comparable
            ) END,
            lower(concat_ws(' ', before_path, after_path, before_name, after_name, provider_item_id)),
            CASE
                WHEN raw_permission_changed THEN 90
                WHEN before_id IS NULL OR after_id IS NULL THEN 70
                WHEN moved OR renamed THEN 60
                WHEN permission_indeterminate THEN 50
                ELSE 30
            END
        FROM shaped
        WHERE change_type IS NOT NULL
        ON CONFLICT (comparison_id, resource_change_id, identity_key) DO NOTHING
        RETURNING id
        """,
        {
            "comparison_id": comparison_id,
            "resource_change_id": resource_change_id,
            "before_resource_id": before_resource_id,
            "after_resource_id": after_resource_id,
            "provider": provider,
            "identity_keys": identity_keys,
            "direct_permissions_comparable": direct_permissions_comparable,
        },
    ).fetchall()
    return len(rows)


def _item_change_summary(
    conn: psycopg.Connection,
    comparison_id: str,
    *,
    computed: bool,
    limitations: list[str],
) -> dict[str, Any]:
    counts = {
        "added": 0,
        "removed": 0,
        "moved": 0,
        "renamed": 0,
        "metadata_changed": 0,
        "permission_changed": 0,
        "indeterminate": 0,
    }
    bounded = 0
    if computed:
        for change_type, count in conn.execute(
            """
            SELECT change_type, COUNT(*)::bigint
            FROM comparison_item_changes
            WHERE comparison_id = %s
            GROUP BY change_type
            """,
            (comparison_id,),
        ).fetchall():
            if str(change_type) in counts:
                counts[str(change_type)] = int(count)
        bounded = int(
            conn.execute(
                """
                SELECT COUNT(*)::bigint
                FROM comparison_item_changes
                WHERE comparison_id = %s AND evidence_state <> 'exact'
                """,
                (comparison_id,),
            ).fetchone()[0]
        )
    counts["total"] = sum(counts.values())
    return {
        "item_churn_computed": computed,
        "item_summary_exact": computed and bounded == 0 and counts["indeterminate"] == 0 and not limitations,
        "item_changes": counts,
        "item_limitations": list(dict.fromkeys(limitations)),
    }


def _finding_dedupe_key(policy_id: str, source_id: str, subject_key: str) -> str:
    return hashlib.sha256(f"{policy_id}\0{source_id}\0{subject_key}".encode("utf-8")).hexdigest()


def _upsert_finding(
    conn: psycopg.Connection,
    *,
    project_id: str,
    source_id: str,
    policy_id: str,
    subject_key: str,
    resource_identity_key: str | None,
    resource_type: str | None,
    provider: str | None,
    resource_name: str | None,
    run_id: str,
    comparison_id: str | None,
    evidence_state: str,
    evidence: dict[str, Any],
    authoritative_state: bool = True,
) -> tuple[str, bool, str]:
    policy = FINDING_POLICIES[policy_id]
    dedupe_key = _finding_dedupe_key(policy_id, source_id, subject_key)
    occurrence_key = hashlib.sha256(
        f"run={run_id}\0comparison={comparison_id or ''}".encode("utf-8")
    ).hexdigest()
    finding_id = str(uuid.uuid4())
    serialized_evidence = json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    search_text = " ".join(
        str(value).strip()
        for value in (
            policy_id,
            policy["title"],
            policy["description"],
            provider,
            resource_type,
            resource_name,
        )
        if value is not None and str(value).strip()
    ).casefold()
    row = conn.execute(
        """
        INSERT INTO findings (
            id, project_id, source_id, policy_id, policy_version, dedupe_key,
            title, description, severity, status, resource_identity_key,
            resource_type, provider, resource_name, search_text, first_seen_at, last_seen_at,
            resolved_at, latest_run_id, latest_comparison_id, evidence, occurrence_count,
            revision, created_at, updated_at
        ) VALUES (
            %(finding_id)s, %(project_id)s, %(source_id)s, %(policy_id)s,
            %(policy_version)s, %(dedupe_key)s, %(title)s, %(description)s,
            %(severity)s, CASE WHEN %(authoritative_state)s THEN 'open' ELSE 'resolved' END,
            %(resource_identity_key)s, %(resource_type)s, %(provider)s, %(resource_name)s,
            %(search_text)s, (SELECT created_at FROM scan_runs WHERE id = %(run_id)s),
            (SELECT created_at FROM scan_runs WHERE id = %(run_id)s),
            CASE WHEN %(authoritative_state)s THEN NULL
                 ELSE (SELECT created_at FROM scan_runs WHERE id = %(run_id)s) END,
            %(run_id)s, %(comparison_id)s, %(evidence)s::jsonb, 1, 1, NOW(), NOW()
        )
        ON CONFLICT (project_id, dedupe_key) DO UPDATE SET
            policy_version = EXCLUDED.policy_version,
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            severity = EXCLUDED.severity,
            source_id = CASE WHEN %(authoritative_state)s THEN EXCLUDED.source_id ELSE findings.source_id END,
            resource_identity_key = CASE WHEN %(authoritative_state)s
                THEN EXCLUDED.resource_identity_key ELSE findings.resource_identity_key END,
            resource_type = CASE WHEN %(authoritative_state)s
                THEN EXCLUDED.resource_type ELSE findings.resource_type END,
            provider = CASE WHEN %(authoritative_state)s THEN EXCLUDED.provider ELSE findings.provider END,
            resource_name = CASE WHEN %(authoritative_state)s
                THEN EXCLUDED.resource_name ELSE findings.resource_name END,
            search_text = CASE WHEN %(authoritative_state)s THEN EXCLUDED.search_text ELSE findings.search_text END,
            first_seen_at = LEAST(findings.first_seen_at, EXCLUDED.first_seen_at),
            last_seen_at = CASE
                WHEN NOT %(authoritative_state)s THEN findings.last_seen_at
                WHEN EXISTS (
                    SELECT 1 FROM finding_occurrences
                    WHERE finding_id = findings.id AND occurrence_key = %(occurrence_key)s
                ) THEN findings.last_seen_at ELSE GREATEST(findings.last_seen_at, EXCLUDED.last_seen_at)
            END,
            latest_run_id = CASE WHEN %(authoritative_state)s
                THEN EXCLUDED.latest_run_id ELSE findings.latest_run_id END,
            latest_comparison_id = CASE WHEN %(authoritative_state)s
                THEN EXCLUDED.latest_comparison_id ELSE findings.latest_comparison_id END,
            evidence = CASE WHEN %(authoritative_state)s THEN EXCLUDED.evidence ELSE findings.evidence END,
            occurrence_count = findings.occurrence_count + CASE
                WHEN EXISTS (
                    SELECT 1 FROM finding_occurrences
                    WHERE finding_id = findings.id AND occurrence_key = %(occurrence_key)s
                ) THEN 0 ELSE 1
            END,
            revision = findings.revision + CASE
                WHEN EXISTS (
                    SELECT 1 FROM finding_occurrences
                    WHERE finding_id = findings.id AND occurrence_key = %(occurrence_key)s
                ) THEN 0 ELSE 1
            END,
            status = CASE
                WHEN %(authoritative_state)s AND NOT EXISTS (
                    SELECT 1 FROM finding_occurrences
                    WHERE finding_id = findings.id AND occurrence_key = %(occurrence_key)s
                ) AND (
                    findings.status = 'resolved'
                    OR (
                        findings.status = 'accepted_risk'
                        AND findings.accepted_risk_expires_at <= NOW()
                    )
                ) THEN 'open'
                ELSE findings.status
            END,
            resolved_at = CASE
                WHEN %(authoritative_state)s AND NOT EXISTS (
                    SELECT 1 FROM finding_occurrences
                    WHERE finding_id = findings.id AND occurrence_key = %(occurrence_key)s
                ) AND findings.status = 'resolved' THEN NULL
                ELSE findings.resolved_at
            END,
            accepted_risk_expires_at = CASE
                WHEN %(authoritative_state)s
                     AND findings.status = 'accepted_risk'
                     AND findings.accepted_risk_expires_at <= NOW() THEN NULL
                ELSE findings.accepted_risk_expires_at
            END,
            updated_at = CASE
                WHEN EXISTS (
                    SELECT 1 FROM finding_occurrences
                    WHERE finding_id = findings.id AND occurrence_key = %(occurrence_key)s
                ) THEN findings.updated_at ELSE NOW()
            END
        RETURNING id::text, status
        """,
        {
            "finding_id": finding_id,
            "project_id": project_id,
            "source_id": source_id,
            "policy_id": policy_id,
            "policy_version": policy["version"],
            "dedupe_key": dedupe_key,
            "title": policy["title"],
            "description": policy["description"],
            "severity": policy["severity"],
            "resource_identity_key": resource_identity_key,
            "resource_type": resource_type,
            "provider": provider,
            "resource_name": resource_name,
            "search_text": search_text,
            "run_id": run_id,
            "comparison_id": comparison_id,
            "evidence": serialized_evidence,
            "occurrence_key": occurrence_key,
            "authoritative_state": authoritative_state,
        },
    ).fetchone()
    if row is None:
        raise RuntimeError("finding upsert returned no row")
    persisted_finding_id, finding_status = str(row[0]), str(row[1])
    inserted_occurrence = conn.execute(
        """
        INSERT INTO finding_occurrences (
            finding_id, run_id, comparison_id, occurrence_key, policy_id,
            policy_version, evidence_state, evidence, observed_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
            (SELECT created_at FROM scan_runs WHERE id = %s)
        )
        ON CONFLICT (finding_id, occurrence_key) DO NOTHING
        RETURNING id
        """,
        (
            persisted_finding_id,
            run_id,
            comparison_id,
            occurrence_key,
            policy_id,
            policy["version"],
            evidence_state,
            serialized_evidence,
            run_id,
        ),
    ).fetchone()
    if inserted_occurrence is not None:
        write_audit(
            conn,
            project_id,
            "FINDING_OBSERVED",
            "finding",
            persisted_finding_id,
            {
                "worker": CONSUMER_NAME,
                "policy_id": policy_id,
                "policy_version": policy["version"],
                "source_id": source_id,
                "run_id": run_id,
                "comparison_id": comparison_id,
                "evidence_state": evidence_state,
                "status": finding_status,
                "authoritative_state": authoritative_state,
            },
        )
    return persisted_finding_id, inserted_occurrence is not None, dedupe_key


def _resolve_absent_state_findings(
    conn: psycopg.Connection,
    *,
    project_id: str,
    source_id: str,
    policy_id: str,
    run_id: str,
    limit: int = FINDING_RESOLUTION_BATCH_SIZE,
) -> tuple[int, bool]:
    rows = conn.execute(
        """
        WITH candidates AS (
            SELECT finding.id
            FROM findings AS finding
            WHERE finding.project_id = %s
              AND finding.source_id = %s
              AND finding.policy_id = %s
              AND finding.status <> 'resolved'
              AND NOT EXISTS (
                  SELECT 1
                  FROM finding_occurrences AS occurrence
                  WHERE occurrence.finding_id = finding.id
                    AND occurrence.run_id = %s
              )
            ORDER BY finding.id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        )
        UPDATE findings AS finding
        SET status = 'resolved', resolved_at = NOW(), accepted_risk_expires_at = NULL,
            updated_at = NOW(), revision = finding.revision + 1
        FROM candidates
        WHERE finding.id = candidates.id
        RETURNING finding.id::text
        """,
        (project_id, source_id, policy_id, run_id, max(1, min(int(limit), 1000))),
    ).fetchall()
    for row in rows:
        write_audit(
            conn,
            project_id,
            "FINDING_AUTO_RESOLVED",
            "finding",
            str(row[0]),
            {"worker": CONSUMER_NAME, "policy_id": policy_id, "source_id": source_id, "run_id": run_id},
        )
    return len(rows), len(rows) >= max(1, min(int(limit), 1000))


def _persist_finding_evaluation_progress(
    conn: psycopg.Connection,
    run_id: str,
    progress: dict[str, Any],
) -> None:
    conn.execute(
        """
        UPDATE scan_runs
        SET ingest_progress = COALESCE(ingest_progress, '{}'::jsonb)
            || jsonb_build_object(
                'heartbeat_at', NOW(),
                'monitoring_worker', %s::text,
                'monitoring_findings', %s::jsonb
            )
        WHERE id = %s
        """,
        (CONSUMER_NAME, json.dumps(progress, ensure_ascii=True, separators=(",", ":")), run_id),
    )


def evaluate_run_findings(
    conn: psycopg.Connection,
    *,
    project_id: str,
    source_id: str,
    run_id: str,
) -> dict[str, int]:
    run_row = conn.execute(
        "SELECT collection_context, ingest_progress FROM scan_runs WHERE id = %s AND project_id = %s",
        (run_id, project_id),
    ).fetchone()
    context = dict(run_row[0]) if run_row and isinstance(run_row[0], dict) else {}
    ingest_progress = dict(run_row[1]) if run_row and isinstance(run_row[1], dict) else {}
    persisted_progress = (
        dict(ingest_progress.get("monitoring_findings"))
        if isinstance(ingest_progress.get("monitoring_findings"), dict)
        else {}
    )
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    permission_scope_complete = metadata.get("permissions_complete") is True
    structural_scope_complete = metadata.get("structural_complete") is True and context.get("partial") is not True
    context_providers = set(_monitoring_provider(context).split("+"))
    after_resource_id = int(persisted_progress.get("after_resource_id") or 0)
    observed_count = int(persisted_progress.get("observed") or 0)
    resolved_count = int(persisted_progress.get("resolved") or 0)
    write_capabilities = {"create_file", "create_directory", "modify_file", "delete", "write_acl", "write_owner"}
    while True:
        rows = conn.execute(
            """
            SELECT resource.id, resource.identity_key, resource.resource_type::text,
                   COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) AS provider,
                   resource.name, resource.exposure::text, resource.exposure_evidence,
                   resource.access_capabilities
            FROM resources AS resource
            WHERE resource.run_id = %s
              AND resource.id > %s
              AND (
                    (
                        COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) = 'sharepoint'
                        AND resource.exposure::text IN ('ANONYMOUS', 'BROAD_INTERNAL')
                    )
                    OR (
                        COALESCE(resource.provider, split_part(resource.resource_type::text, '_', 1)) = 'smb'
                        AND EXISTS (
                            SELECT 1
                            FROM jsonb_each(COALESCE(resource.access_capabilities, '{}'::jsonb)) AS capability
                            WHERE capability.key = ANY(%s::text[])
                              AND capability.value->>'status' IN ('allowed', 'mixed')
                        )
                    )
              )
            ORDER BY resource.id
            LIMIT %s
            """,
            (run_id, after_resource_id, sorted(write_capabilities), FINDING_EVALUATION_BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        for resource_id, identity_key, resource_type, provider, name, exposure, exposure_evidence, raw_capabilities in rows:
            identity = str(identity_key or hashlib.sha256(f"resource:{resource_id}".encode()).hexdigest())
            policy_id: str | None = None
            evidence_state = "exact"
            summary: dict[str, Any] = {}
            limitations: list[str] = []
            if provider == "sharepoint" and exposure == "ANONYMOUS":
                policy_id = "sharepoint.anonymous_access"
                summary = {"exposure": exposure, "positive_evidence": exposure_evidence or {}}
            elif provider == "sharepoint" and exposure == "BROAD_INTERNAL":
                policy_id = "sharepoint.broad_internal_access"
                summary = {"exposure": exposure, "positive_evidence": exposure_evidence or {}}
            capabilities = raw_capabilities if isinstance(raw_capabilities, dict) else {}
            if provider == "smb":
                capability_metadata = (
                    capabilities.get("_metadata") if isinstance(capabilities.get("_metadata"), dict) else {}
                )
                probe_complete = (
                    capability_metadata.get("complete") is True
                    and capability_metadata.get("degraded") is not True
                )
                allowed_writes = sorted(
                    capability
                    for capability in write_capabilities
                    if isinstance(capabilities.get(capability), dict)
                    and str(capabilities[capability].get("status") or "") in {"allowed", "mixed"}
                )
                if allowed_writes:
                    policy_id = "smb.write_observed"
                    evidence_state = "bounded"
                    summary = {
                        "allowed_capabilities": allowed_writes,
                        "probe_method": capability_metadata.get("probe_method"),
                        "complete": probe_complete,
                    }
                    limitations = ["Capability probes apply only to the assessed identity and tested operations."]
            if policy_id is None:
                continue
            evidence = {
                "state": evidence_state,
                "summary": summary,
                "refs": {"run_id": run_id, "resource_id": int(resource_id)},
                "limitations": limitations,
            }
            _finding_id, inserted, _dedupe_key = _upsert_finding(
                conn,
                project_id=project_id,
                source_id=source_id,
                policy_id=policy_id,
                subject_key=identity,
                resource_identity_key=identity,
                resource_type=str(resource_type),
                provider=str(provider),
                resource_name=str(name),
                run_id=run_id,
                comparison_id=None,
                evidence_state=evidence_state,
                evidence=evidence,
            )
            observed_count += int(inserted)
        after_resource_id = int(rows[-1][0])
        _persist_finding_evaluation_progress(
            conn,
            run_id,
            {
                "state": "evaluating",
                "phase": "candidates",
                "after_resource_id": after_resource_id,
                "observed": observed_count,
                "resolved": resolved_count,
            },
        )
        conn.commit()

    smb_scope_row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(
            BOOL_AND(
                COALESCE(access_capabilities->'_metadata'->>'complete', 'false') = 'true'
                AND COALESCE(access_capabilities->'_metadata'->>'degraded', 'false') <> 'true'
            ),
            TRUE
        )
        FROM resources
        WHERE run_id = %s
          AND COALESCE(provider, split_part(resource_type::text, '_', 1)) = 'smb'
        """,
        (run_id,),
    ).fetchone()
    smb_resource_count = int(smb_scope_row[0]) if smb_scope_row else 0
    smb_probe_scope_complete = bool(
        smb_scope_row
        and (
            (smb_resource_count == 0 and structural_scope_complete)
            or (smb_resource_count > 0 and smb_scope_row[1] is True)
        )
    )
    resolution_policies: list[str] = []
    if "sharepoint" in context_providers and structural_scope_complete and permission_scope_complete:
        resolution_policies.extend(("sharepoint.anonymous_access", "sharepoint.broad_internal_access"))
    if "smb" in context_providers and structural_scope_complete and smb_probe_scope_complete:
        resolution_policies.append("smb.write_observed")
    completed_policy_index = int(persisted_progress.get("completed_policy_index") or 0)
    for policy_index, policy_id in enumerate(resolution_policies):
        if policy_index < completed_policy_index:
            continue
        while True:
            resolved, has_more = _resolve_absent_state_findings(
                conn,
                project_id=project_id,
                source_id=source_id,
                policy_id=policy_id,
                run_id=run_id,
            )
            resolved_count += resolved
            _persist_finding_evaluation_progress(
                conn,
                run_id,
                {
                    "state": "evaluating",
                    "phase": "resolving",
                    "after_resource_id": after_resource_id,
                    "completed_policy_index": policy_index if has_more else policy_index + 1,
                    "policy_id": policy_id,
                    "observed": observed_count,
                    "resolved": resolved_count,
                },
            )
            conn.commit()
            if not has_more:
                break
    _persist_finding_evaluation_progress(
        conn,
        run_id,
        {
            "state": "complete",
            "phase": "complete",
            "after_resource_id": after_resource_id,
            "completed_policy_index": len(resolution_policies),
            "observed": observed_count,
            "resolved": resolved_count,
        },
    )
    conn.commit()
    return {"observed": observed_count, "resolved": resolved_count}


def evaluate_comparison_findings(
    conn: psycopg.Connection,
    *,
    comparison_id: str,
    project_id: str,
    source_id: str | None,
    current_run_id: str,
    after_id: int = 0,
    limit: int = FINDING_EVALUATION_BATCH_SIZE,
    authoritative_state: bool = True,
) -> tuple[int, int, bool]:
    if not source_id:
        return 0, after_id, False
    bounded_limit = max(1, min(int(limit), 5000))
    rows = conn.execute(
        """
        SELECT id, identity_key, change_type, provider, resource_type,
               COALESCE(resource_name_after, resource_name_before),
               change_categories, match_quality, before_snapshot, after_snapshot,
               structural_state, access_state, content_state
        FROM comparison_resource_changes
        WHERE comparison_id = %s
          AND id > %s
          AND (
                (change_type = 'appeared' AND structural_state = 'appeared')
                OR (change_type = 'disappeared' AND structural_state = 'disappeared')
                OR (
                    (change_categories ?| ARRAY['access', 'permission_evidence'])
                    AND change_type <> 'indeterminate'
                    AND access_state = 'changed'
                )
                OR change_type = 'indeterminate'
                OR access_state = 'indeterminate'
          )
        ORDER BY id
        LIMIT %s
        """,
        (comparison_id, after_id, bounded_limit + 1),
    ).fetchall()
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    inserted_count = 0
    for row in rows:
        (
            row_id,
            identity_key,
            change_type,
            provider,
            resource_type,
            resource_name,
            raw_categories,
            match_quality,
            before_snapshot,
            after_snapshot,
            structural_state,
            access_state,
            content_state,
        ) = row
        categories = {str(category) for category in (raw_categories or [])}
        policy_id: str | None = None
        if change_type == "appeared" and structural_state == "appeared":
            policy_id = "resource.appeared"
        elif change_type == "disappeared" and structural_state == "disappeared":
            policy_id = "resource.disappeared"
        elif (
            {"access", "permission_evidence"}.intersection(categories)
            and change_type != "indeterminate"
            and access_state == "changed"
        ):
            policy_id = "permission.evidence_changed"
        elif change_type == "indeterminate" or access_state == "indeterminate":
            policy_id = "comparison.indeterminate"
        if policy_id is None:
            continue
        evidence_state = "indeterminate" if policy_id == "comparison.indeterminate" else (
            "exact" if match_quality == "strong" else "bounded"
        )
        limitations = []
        if evidence_state == "bounded":
            limitations.append("The resource was matched by a location-bound or otherwise non-strong identity.")
        if evidence_state == "indeterminate":
            limitations.append("Collection coverage or evidence compatibility does not support a definitive conclusion.")
        evidence = {
            "state": evidence_state,
            "summary": {
                "change_type": str(change_type),
                "categories": sorted(categories),
                "structural_state": str(structural_state),
                "access_state": str(access_state),
                "content_state": str(content_state),
                "before": before_snapshot or {},
                "after": after_snapshot or {},
            },
            "refs": {"run_id": current_run_id, "comparison_id": comparison_id},
            "limitations": limitations,
        }
        _finding_id, inserted, _dedupe = _upsert_finding(
            conn,
            project_id=project_id,
            source_id=source_id,
            policy_id=policy_id,
            subject_key=str(identity_key),
            resource_identity_key=str(identity_key),
            resource_type=str(resource_type),
            provider=str(provider),
            resource_name=str(resource_name or "resource"),
            run_id=current_run_id,
            comparison_id=comparison_id,
            evidence_state=evidence_state,
            evidence=evidence,
            authoritative_state=authoritative_state,
        )
        inserted_count += int(inserted)
        after_id = int(row_id)
    return inserted_count, after_id, has_more


def _build_comparison_summary(
    summary_rows: list[tuple[str, int]],
    compatibility: dict[str, Any],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "appeared": 0,
        "disappeared": 0,
        "changed": 0,
        "indeterminate": 0,
    }
    for change_type, count in summary_rows:
        if change_type in summary:
            summary[str(change_type)] = int(count)
    summary["total"] = sum(int(summary[key]) for key in ("appeared", "disappeared", "changed", "indeterminate"))
    capability_applicable = compatibility.get("capability_applicable") is not False
    compatibility_dimensions = {
        "structural": compatibility.get("structural_interpretable") is True,
        "content": compatibility.get("content_interpretable") is True,
        # Stable target/name fallbacks are sufficient to compare observations
        # at the same network location, but only strong server identities make
        # the resulting resource summary exact across physical-server changes.
        "identity_scope": compatibility.get("identity_scope_exact") is not False,
        "identity_applicable": compatibility.get("identity_applicable") is not False,
        "access": compatibility.get("access_interpretable") is True,
        "capabilities": (compatibility.get("capability_interpretable") is True if capability_applicable else True),
        "capabilities_applicable": capability_applicable,
        "direct_permissions": compatibility.get("direct_permissions_interpretable") is True,
        "direct_permission_scope": compatibility.get("direct_permissions_scope_exact") is True,
    }
    summary["dimensions"] = compatibility_dimensions
    exact_dimension_values = (
        value
        for key, value in compatibility_dimensions.items()
        if key not in {"capabilities_applicable", "identity_applicable"}
    )
    summary["resource_summary_exact"] = all(exact_dimension_values) and summary["indeterminate"] == 0
    # Resource identity/access/count changes are materialized, but per-item
    # additions, removals, and moves are intentionally not computed.
    summary["item_churn_computed"] = False
    summary["exact"] = False
    return summary


def _comparison_direct_permission_scope_exact(
    conn: psycopg.Connection,
    baseline_run_id: str,
    current_run_id: str,
) -> bool:
    """Return whether both runs support scope-complete negative ACL conclusions.

    Comparable Graph sharing responses and deterministic SMB samples can prove
    that observed evidence changed, but they cannot make the whole comparison
    exact. Keep that stronger claim tied to exhaustive, integrity-checked
    assessment surfaces for every applicable resource in both runs.
    """

    rows = conn.execute(
        """
        SELECT
            resource.run_id::text,
            BOOL_AND(
                COALESCE(resource.permission_summary->>'evidence_available', 'false') = 'true'
                AND COALESCE(resource.permission_summary->>'comparable', 'false') = 'true'
                AND COALESCE(resource.permission_summary->>'scope_exact', 'false') = 'true'
                AND resource.permission_summary->>'comparison_evidence_hash' IS NOT NULL
                AND resource.permission_summary->>'comparison_quality_hash' IS NOT NULL
            ) AS scope_exact
        FROM resources AS resource
        WHERE resource.run_id IN (%s, %s)
          AND COALESCE(
              resource.provider,
              split_part(resource.resource_type::text, '_', 1)
          ) IN ('smb', 'sharepoint')
        GROUP BY resource.run_id
        """,
        (baseline_run_id, current_run_id),
    ).fetchall()
    scope_by_run = {str(run_id): bool(scope_exact) for run_id, scope_exact in rows}
    return scope_by_run.get(baseline_run_id) is True and scope_by_run.get(current_run_id) is True


def _comparison_retry_is_deferred(
    state: str,
    next_retry_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    if state != "queued" or next_retry_at is None:
        return False
    due_at = next_retry_at if next_retry_at.tzinfo is not None else next_retry_at.replace(tzinfo=UTC)
    return due_at > (now or datetime.now(tz=UTC))


def comparison_is_latest_complete_candidate(
    conn: psycopg.Connection,
    *,
    comparison_id: str,
    source_id: str,
    current_run_id: str,
) -> bool:
    row = conn.execute(
        """
        SELECT NOT EXISTS (
            SELECT 1 FROM scan_runs AS newer_run
            JOIN scan_runs AS current_run ON current_run.id = %s
            WHERE newer_run.source_id = %s
              AND newer_run.status = 'COMPLETE'
              AND (newer_run.created_at, newer_run.id) > (current_run.created_at, current_run.id)
        ) AND NOT EXISTS (
            SELECT 1 FROM run_comparisons AS newer_comparison
            JOIN scan_runs AS newer_comparison_run ON newer_comparison_run.id = newer_comparison.current_run_id
            JOIN scan_runs AS current_run ON current_run.id = %s
            WHERE newer_comparison.source_id = %s
              AND newer_comparison.id <> %s
              AND newer_comparison.state = 'complete'
              AND (newer_comparison_run.created_at, newer_comparison_run.id)
                  > (current_run.created_at, current_run.id)
        )
        """,
        (current_run_id, source_id, current_run_id, source_id, comparison_id),
    ).fetchone()
    return bool(row and row[0] is True)


def process_comparison_job(fields: dict[str, str]) -> str:
    comparison_id = _normalize_uuid_str(fields.get("comparison_id"))
    if not comparison_id:
        logger.error("invalid comparison job payload: %s", fields)
        return "ignored"

    with connect_database() as conn:
        lock_key = advisory_lock_key(comparison_id)
        source_lock_key: int | None = None
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,)).fetchone()[0]:
            return "busy"
        project_id: str | None = None
        attempt_count = 0
        try:
            row = conn.execute(
                """
                SELECT project_id::text, source_id::text, baseline_run_id::text, current_run_id::text,
                       state, compatibility, attempt_count, next_retry_at, progress, trigger
                FROM run_comparisons
                WHERE id = %s
                FOR UPDATE
                """,
                (comparison_id,),
            ).fetchone()
            if row is None:
                return "ignored"
            (
                project_id,
                source_id,
                baseline_run_id,
                current_run_id,
                state,
                compatibility,
                attempt_count,
                next_retry_at,
                raw_progress,
                comparison_trigger,
            ) = row
            if state in {"complete", "failed"}:
                return "ignored"
            if _comparison_retry_is_deferred(str(state), next_retry_at):
                logger.info(
                    "comparison retry is not due yet comparison_id=%s next_retry_at=%s",
                    comparison_id,
                    next_retry_at.isoformat(),
                )
                return "deferred"
            if source_id:
                source_lock_key = monitoring_source_advisory_lock_key_from_id(conn, source_id)
                if source_lock_key is not None:
                    conn.execute("SELECT pg_advisory_lock(%s)", (source_lock_key,))
            if (
                str(comparison_trigger) == "automatic"
                and source_id
                and not collection_source_automation_enabled(conn, source_id)
            ):
                conn.execute(
                    """
                    UPDATE run_comparisons
                    SET state = 'failed', completed_at = NOW(), heartbeat_at = NOW(),
                        error_code = 'SOURCE_DISABLED',
                        error_message = 'Automatic monitoring is disabled for this collection source.',
                        progress = jsonb_build_object('phase', 'skipped', 'reason', 'source_disabled')
                    WHERE id = %s
                    """,
                    (comparison_id,),
                )
                write_audit(
                    conn,
                    project_id,
                    "COMPARISON_SKIPPED",
                    "run_comparison",
                    comparison_id,
                    {"worker": CONSUMER_NAME, "reason": "source_disabled", "source_id": source_id},
                )
                conn.commit()
                return "failed"
            progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
            operator_retry = progress.get("operator_retry") is True
            resume_phase = str(progress.get("phase") or "")
            durable_resume = resume_phase in {
                "preparing_identities",
                "materializing",
                "materializing_resources",
                "materializing_items",
                "finalizing",
                "evaluating_findings",
                "yielded",
            }
            if operator_retry:
                conn.execute("DELETE FROM comparison_resource_changes WHERE comparison_id = %s", (comparison_id,))
                progress = {"phase": "preparing_identities", "processed": 0}
                durable_resume = False
            retry_resume = progress.pop("resume_after_error", False) is True
            if not durable_resume or retry_resume:
                attempt_count = int(attempt_count or 0) + 1
            else:
                attempt_count = int(attempt_count or 0)
            run_states = conn.execute(
                """
                SELECT id::text, project_id::text, status::text
                FROM scan_runs
                WHERE id IN (%s, %s)
                """,
                (baseline_run_id, current_run_id),
            ).fetchall()
            if len(run_states) != 2 or any(run[1] != project_id or run[2] != "COMPLETE" for run in run_states):
                raise ValueError("comparison runs must both exist, belong to the project, and be COMPLETE")
            conn.execute(
                """
                UPDATE run_comparisons
                SET state = 'running', started_at = COALESCE(started_at, NOW()),
                    heartbeat_at = NOW(), attempt_count = %s,
                    next_retry_at = NULL, error_code = NULL, error_message = NULL,
                    progress = %s::jsonb
                WHERE id = %s
                """,
                (
                    attempt_count,
                    json.dumps(progress if durable_resume else {"phase": "preparing_identities", "processed": 0}),
                    comparison_id,
                ),
            )
            conn.commit()

            compatibility = dict(compatibility) if isinstance(compatibility, dict) else {}
            smb_declared = compatibility.get("smb_identity_required") is True
            smb_observed = _comparison_observes_smb_resources(conn, baseline_run_id, current_run_id)
            smb_context_conflict = smb_observed and not smb_declared
            smb_identity_applicable = smb_declared or smb_observed
            if smb_identity_applicable:
                smb_identity_stable, smb_identity_scope_exact = _comparison_smb_identity_status(
                    conn,
                    baseline_run_id,
                    current_run_id,
                )
            else:
                smb_identity_stable = True
                smb_identity_scope_exact = True
            compatibility["identity_applicable"] = smb_identity_applicable
            compatibility["identity_scope_exact"] = bool(smb_identity_scope_exact and not smb_context_conflict)
            if smb_context_conflict or not smb_identity_stable:
                compatibility["structural_interpretable"] = False
                compatibility["content_interpretable"] = False
                compatibility["status"] = (
                    "partial"
                    if any(
                        compatibility.get(field) is True
                        for field in (
                            "access_interpretable",
                            "capability_interpretable",
                            "direct_permissions_interpretable",
                        )
                    )
                    else "incompatible"
                )
                reasons = [str(reason) for reason in compatibility.get("reasons", []) if str(reason)]
                if smb_context_conflict:
                    context_reason = (
                        "Persisted SMB resources contradict the declared provider scope; "
                        "structural and content changes are indeterminate."
                    )
                    if context_reason not in reasons:
                        reasons.append(context_reason)
                identity_reason = (
                    "SMB server identity changed or could not be verified consistently; "
                    "structural and content changes are indeterminate."
                )
                if not smb_identity_stable and identity_reason not in reasons:
                    reasons.append(identity_reason)
                compatibility["reasons"] = reasons
            elif smb_identity_applicable and not smb_identity_scope_exact:
                # A stable advertised-name or scan-target identity proves that
                # both runs observed the same requested location. It does not
                # prove that the physical SMB server behind that location is
                # unchanged, so keep resource counts bounded rather than exact.
                if compatibility.get("status") == "compatible":
                    compatibility["status"] = "partial"
                reasons = [str(reason) for reason in compatibility.get("reasons", []) if str(reason)]
                bounded_identity_reason = (
                    "SMB identity continuity is location-bound; it does not prove physical server continuity."
                )
                if bounded_identity_reason not in reasons:
                    reasons.append(bounded_identity_reason)
                compatibility["reasons"] = reasons

            identity_runs = (baseline_run_id, current_run_id)
            later_phase = resume_phase in {
                "materializing",
                "materializing_resources",
                "materializing_items",
                "finalizing",
                "evaluating_findings",
            }
            identity_run_index = 2 if later_phase else int(progress.get("identity_run_index") or 0)
            identity_resource_after = int(progress.get("identity_resource_after_id") or 0)
            identity_item_after = int(progress.get("identity_item_after_id") or 0)
            identity_processed = int(progress.get("identity_processed") or 0)
            identity_started = time.monotonic()
            identity_batches = 0
            while identity_run_index < len(identity_runs):
                identity_result = prepare_run_identity_keys_batch(
                    conn,
                    identity_runs[identity_run_index],
                    resource_after_id=identity_resource_after,
                    item_after_id=identity_item_after,
                )
                identity_resource_after = int(identity_result["resource_after_id"])
                identity_item_after = int(identity_result["item_after_id"])
                identity_processed += int(identity_result["processed"])
                if identity_result["item_complete"] is True:
                    identity_run_index += 1
                    identity_resource_after = 0
                    identity_item_after = 0
                progress = {
                    **progress,
                    "phase": "preparing_identities",
                    "identity_run_index": identity_run_index,
                    "identity_resource_after_id": identity_resource_after,
                    "identity_item_after_id": identity_item_after,
                    "identity_processed": identity_processed,
                }
                conn.execute(
                    "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb WHERE id = %s",
                    (json.dumps(progress), comparison_id),
                )
                conn.commit()
                identity_batches += 1
                if identity_run_index < len(identity_runs) and (
                    identity_batches >= COMPARISON_WORK_QUANTUM_BATCHES
                    or time.monotonic() - identity_started >= COMPARISON_WORK_QUANTUM_SECONDS
                ):
                    progress = {
                        **progress,
                        "yielded_at": now_iso(),
                        "yield_reason": "identity_work_quantum_exhausted",
                    }
                    conn.execute(
                        "UPDATE run_comparisons SET state = 'queued', heartbeat_at = NOW(), progress = %s::jsonb "
                        "WHERE id = %s",
                        (json.dumps(progress), comparison_id),
                    )
                    conn.commit()
                    return "yielded"
            progress = {
                **progress,
                "phase": "materializing_resources",
                "processed": int(progress.get("processed") or 0),
            }
            ambiguous = conn.execute(
                """
                SELECT run_id::text, identity_key, COUNT(*)
                FROM resources
                WHERE run_id IN (%s, %s)
                GROUP BY run_id, identity_key
                HAVING identity_key IS NULL OR COUNT(*) > 1
                LIMIT 1
                """,
                (baseline_run_id, current_run_id),
            ).fetchone()
            if ambiguous is not None:
                raise ValueError("comparison identity is ambiguous; a run contains duplicate or unkeyed resources")
            conn.commit()

            direct_scope_exact = bool(
                compatibility.get("direct_permissions_interpretable") is True
                and _comparison_direct_permission_scope_exact(
                    conn,
                    baseline_run_id,
                    current_run_id,
                )
            )
            compatibility["direct_permissions_scope_exact"] = direct_scope_exact
            if compatibility.get("direct_permissions_interpretable") is True and not direct_scope_exact:
                reasons = [str(reason) for reason in compatibility.get("reasons", []) if str(reason)]
                bounded_reason = (
                    "Provider permission evidence is comparable but does not support "
                    "scope-complete negative conclusions."
                )
                if bounded_reason not in reasons:
                    reasons.append(bounded_reason)
                compatibility["reasons"] = reasons
            conn.execute(
                "UPDATE run_comparisons SET compatibility = %s::jsonb WHERE id = %s",
                (
                    json.dumps(compatibility, ensure_ascii=True, separators=(",", ":")),
                    comparison_id,
                ),
            )
            conn.commit()
            batch_size = 5000
            quantum_started = time.monotonic()
            quantum_batches = 0

            def quantum_exhausted() -> bool:
                return (
                    quantum_batches >= COMPARISON_WORK_QUANTUM_BATCHES
                    or time.monotonic() - quantum_started >= COMPARISON_WORK_QUANTUM_SECONDS
                )

            def yield_comparison(progress_payload: dict[str, Any], reason: str) -> str:
                progress_payload = {
                    **progress_payload,
                    "yielded_at": now_iso(),
                    "yield_reason": reason,
                }
                conn.execute(
                    """
                    UPDATE run_comparisons
                    SET state = 'queued', heartbeat_at = NOW(), progress = %s::jsonb
                    WHERE id = %s
                    """,
                    (json.dumps(progress_payload), comparison_id),
                )
                conn.commit()
                return "yielded"

            resume_phase = str(progress.get("phase") or "")
            resource_phase_complete = resume_phase in {
                "materializing_items",
                "finalizing",
                "evaluating_findings",
            }
            after_key: str | None = (
                str(progress.get("last_identity_key"))
                if resource_phase_complete or resume_phase in {"materializing", "materializing_resources"}
                else None
            )
            processed = int(progress.get("processed") or 0) if after_key else 0
            emitted = int(progress.get("changes_emitted") or 0) if after_key else 0
            if not resource_phase_complete:
                while not _shutdown_event.is_set():
                    keys = _comparison_batch_keys(
                        conn,
                        baseline_run_id,
                        current_run_id,
                        after_key,
                        batch_size,
                    )
                    if not keys:
                        break
                    emitted += _materialize_comparison_batch(
                        conn,
                        comparison_id,
                        baseline_run_id,
                        current_run_id,
                        keys,
                        compatibility,
                    )
                    if compatibility.get("content_interpretable") is True:
                        _ensure_item_candidate_resource_changes(
                            conn,
                            comparison_id=comparison_id,
                            baseline_run_id=baseline_run_id,
                            current_run_id=current_run_id,
                            identity_keys=keys,
                        )
                    processed += len(keys)
                    after_key = keys[-1]
                    progress = {
                        "phase": "materializing_resources",
                        "processed": processed,
                        "changes_emitted": emitted,
                        "last_identity_key": after_key,
                    }
                    conn.execute(
                        "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb WHERE id = %s",
                        (json.dumps(progress), comparison_id),
                    )
                    conn.commit()
                    quantum_batches += 1
                    if quantum_exhausted():
                        return yield_comparison(progress, "work_quantum_exhausted")
                progress = {
                    "phase": "materializing_items",
                    "processed": processed,
                    "changes_emitted": emitted,
                    "last_identity_key": after_key,
                    "completed_resource_change_id": 0,
                    "resource_change_id": None,
                    "last_item_identity_key": None,
                }
                conn.execute(
                    "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb WHERE id = %s",
                    (json.dumps(progress), comparison_id),
                )
                conn.commit()

            if _shutdown_event.is_set():
                conn.execute(
                    """
                    UPDATE run_comparisons
                    SET state = 'queued', heartbeat_at = NOW(),
                        progress = COALESCE(progress, '{}'::jsonb) || jsonb_build_object(
                            'paused_at', NOW(), 'pause_reason', 'worker_shutdown'
                        )
                    WHERE id = %s
                    """,
                    (comparison_id,),
                )
                conn.commit()
                return "shutdown"

            item_limitations: list[str] = []
            item_emitted = int(progress.get("item_changes_emitted") or 0)
            item_processed = int(progress.get("processed_items") or 0)
            item_computed = compatibility.get("content_interpretable") is True
            if item_computed:
                completed_resource_change_id = int(progress.get("completed_resource_change_id") or 0)
                current_resource_change_id = int(progress.get("resource_change_id") or 0)
                resume_item_key = (
                    str(progress.get("last_item_identity_key"))
                    if progress.get("last_item_identity_key")
                    else None
                )
                while not _shutdown_event.is_set():
                    candidate = (
                        _item_resource_change_by_id(conn, comparison_id, current_resource_change_id)
                        if current_resource_change_id
                        else _next_item_resource_change(conn, comparison_id, completed_resource_change_id)
                    )
                    if candidate is None:
                        break
                    resource_change_id, before_resource_id, after_resource_id, provider = candidate
                    if _item_identity_is_ambiguous(conn, before_resource_id, after_resource_id):
                        _mark_item_identity_indeterminate(conn, comparison_id, resource_change_id)
                        completed_resource_change_id = resource_change_id
                        current_resource_change_id = 0
                        resume_item_key = None
                        progress = {
                            **progress,
                            "phase": "materializing_items",
                            "completed_resource_change_id": completed_resource_change_id,
                            "resource_change_id": None,
                            "last_item_identity_key": None,
                            "processed_items": item_processed,
                            "item_changes_emitted": item_emitted,
                        }
                        conn.execute(
                            "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb WHERE id = %s",
                            (json.dumps(progress), comparison_id),
                        )
                        conn.commit()
                        quantum_batches += 1
                        if quantum_exhausted():
                            return yield_comparison(progress, "work_quantum_exhausted")
                        continue
                    after_item_key = resume_item_key if current_resource_change_id == resource_change_id else None
                    current_resource_change_id = resource_change_id
                    while not _shutdown_event.is_set():
                        item_keys = _item_change_batch_keys(
                            conn,
                            before_resource_id,
                            after_resource_id,
                            after_item_key,
                            COMPARISON_ITEM_BATCH_SIZE,
                        )
                        if not item_keys:
                            break
                        item_emitted += _materialize_item_change_batch(
                            conn,
                            comparison_id=comparison_id,
                            resource_change_id=resource_change_id,
                            before_resource_id=before_resource_id,
                            after_resource_id=after_resource_id,
                            provider=provider,
                            identity_keys=item_keys,
                            direct_permissions_comparable=(
                                compatibility.get("direct_permissions_interpretable") is True
                            ),
                        )
                        item_processed += len(item_keys)
                        after_item_key = item_keys[-1]
                        conn.execute(
                            """
                            UPDATE run_comparisons
                            SET heartbeat_at = NOW(),
                                progress = %s::jsonb
                            WHERE id = %s
                            """,
                            (
                                json.dumps(
                                    {
                                        **progress,
                                        "phase": "materializing_items",
                                        "processed": processed,
                                        "processed_items": item_processed,
                                        "item_changes_emitted": item_emitted,
                                        "completed_resource_change_id": completed_resource_change_id,
                                        "resource_change_id": resource_change_id,
                                        "last_item_identity_key": after_item_key,
                                    }
                                ),
                                comparison_id,
                            ),
                        )
                        conn.commit()
                        progress = {
                            **progress,
                            "phase": "materializing_items",
                            "processed": processed,
                            "processed_items": item_processed,
                            "item_changes_emitted": item_emitted,
                            "completed_resource_change_id": completed_resource_change_id,
                            "resource_change_id": resource_change_id,
                            "last_item_identity_key": after_item_key,
                        }
                        quantum_batches += 1
                        if quantum_exhausted():
                            return yield_comparison(progress, "work_quantum_exhausted")
                    completed_resource_change_id = resource_change_id
                    current_resource_change_id = 0
                    resume_item_key = None
                    progress = {
                        **progress,
                        "phase": "materializing_items",
                        "completed_resource_change_id": completed_resource_change_id,
                        "resource_change_id": None,
                        "last_item_identity_key": None,
                        "processed_items": item_processed,
                        "item_changes_emitted": item_emitted,
                    }
                    conn.execute(
                        "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb WHERE id = %s",
                        (json.dumps(progress), comparison_id),
                    )
                    conn.commit()
                    quantum_batches += 1
                    if quantum_exhausted():
                        return yield_comparison(progress, "work_quantum_exhausted")
                ambiguous_item_resources = int(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM comparison_resource_changes
                        WHERE comparison_id = %s AND change_categories ? 'item_identity_ambiguous'
                        """,
                        (comparison_id,),
                    ).fetchone()[0]
                )
                if ambiguous_item_resources:
                    ambiguous_examples = [
                        int(row[0])
                        for row in conn.execute(
                            """
                            SELECT id FROM comparison_resource_changes
                            WHERE comparison_id = %s AND change_categories ? 'item_identity_ambiguous'
                            ORDER BY id LIMIT 20
                            """,
                            (comparison_id,),
                        ).fetchall()
                    ]
                    item_limitations.append(
                        "Item history was skipped for "
                        f"{ambiguous_item_resources} resource change(s) with duplicate or missing item identities; "
                        f"examples={ambiguous_examples}."
                    )
                _finalize_item_candidate_resources(conn, comparison_id)
                conn.execute(
                    """
                    UPDATE run_comparisons
                    SET heartbeat_at = NOW(),
                        progress = COALESCE(progress, '{}'::jsonb) || jsonb_build_object('phase', 'finalizing')
                    WHERE id = %s
                    """,
                    (comparison_id,),
                )
                conn.commit()
            else:
                item_limitations.append(
                    "Item history was not materialized because content scope or completeness was not comparable."
                )

            if _shutdown_event.is_set():
                conn.execute(
                    """
                    UPDATE run_comparisons
                    SET state = 'queued', heartbeat_at = NOW(),
                        progress = COALESCE(progress, '{}'::jsonb) || jsonb_build_object(
                            'paused_at', NOW(), 'pause_reason', 'worker_shutdown'
                        )
                    WHERE id = %s
                    """,
                    (comparison_id,),
                )
                conn.commit()
                return "shutdown"

            summary_rows = conn.execute(
                """
                SELECT change_type, COUNT(*)::integer
                FROM comparison_resource_changes
                WHERE comparison_id = %s
                GROUP BY change_type
                """,
                (comparison_id,),
            ).fetchall()
            summary = _build_comparison_summary(summary_rows, compatibility)
            item_summary = _item_change_summary(
                conn,
                comparison_id,
                computed=item_computed,
                limitations=item_limitations,
            )
            summary.update(item_summary)
            summary["exact"] = bool(summary.get("resource_summary_exact") and summary.get("item_summary_exact"))
            emitted = int(summary.get("total") or 0)
            findings_authoritative = bool(
                not source_id
                or comparison_is_latest_complete_candidate(
                    conn,
                    comparison_id=comparison_id,
                    source_id=source_id,
                    current_run_id=current_run_id,
                )
            )
            finding_cursor = int(progress.get("findings_cursor") or 0)
            finding_count = int(progress.get("findings_observed") or 0)
            progress = {
                **progress,
                "phase": "evaluating_findings",
                "processed": processed,
                "changes_emitted": emitted,
                "processed_items": item_processed,
                "item_changes_emitted": item_emitted,
                "findings_cursor": finding_cursor,
                "findings_observed": finding_count,
            }
            conn.execute(
                "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb WHERE id = %s",
                (json.dumps(progress), comparison_id),
            )
            conn.commit()
            findings_evaluation_state = "complete"
            try:
                while not _shutdown_event.is_set():
                    inserted, finding_cursor, has_more = evaluate_comparison_findings(
                        conn,
                        comparison_id=comparison_id,
                        project_id=project_id,
                        source_id=source_id,
                        current_run_id=current_run_id,
                        after_id=finding_cursor,
                        authoritative_state=findings_authoritative,
                    )
                    finding_count += inserted
                    progress = {
                        **progress,
                        "phase": "complete",
                        "findings_cursor": finding_cursor,
                        "findings_observed": finding_count,
                    }
                    conn.execute(
                        "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb WHERE id = %s",
                        (json.dumps(progress), comparison_id),
                    )
                    conn.commit()
                    quantum_batches += 1
                    if has_more and quantum_exhausted():
                        return yield_comparison(progress, "work_quantum_exhausted")
                    if not has_more:
                        break
            except Exception as findings_exc:
                logger.exception("comparison finding evaluation degraded comparison_id=%s", comparison_id)
                conn.rollback()
                finding_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM finding_occurrences WHERE comparison_id = %s",
                        (comparison_id,),
                    ).fetchone()[0]
                )
                findings_attempt_count = int(progress.get("findings_attempt_count") or 0) + 1
                if findings_attempt_count < 3:
                    retry_delay_seconds = _retry_backoff_seconds(
                        findings_attempt_count,
                        jitter_key=f"comparison-findings:{comparison_id}",
                    )
                    next_retry_at = datetime.now(tz=UTC) + timedelta(seconds=retry_delay_seconds)
                    progress = {
                        **progress,
                        "phase": "evaluating_findings",
                        "findings_cursor": finding_cursor,
                        "findings_observed": finding_count,
                        "findings_attempt_count": findings_attempt_count,
                        "findings_evaluation_state": "retrying",
                        "findings_next_retry_at": next_retry_at.isoformat(),
                        "findings_error_code": "FINDING_EVALUATION_FAILED",
                    }
                    summary["findings_evaluation"] = {
                        "state": "retrying",
                        "attempt_count": findings_attempt_count,
                        "next_retry_at": next_retry_at.isoformat(),
                        "partial_positive_evidence_retained": finding_count > 0,
                    }
                    conn.execute(
                        """
                        UPDATE run_comparisons
                        SET state = 'complete', completed_at = NOW(), heartbeat_at = NOW(), next_retry_at = NULL,
                            progress = %s::jsonb, summary = %s::jsonb
                        WHERE id = %s
                        """,
                        (json.dumps(progress), json.dumps(summary), comparison_id),
                    )
                    if source_id and findings_authoritative:
                        conn.execute(
                            "UPDATE collection_sources SET last_comparison_id = %s, updated_at = NOW() "
                            "WHERE id = %s AND project_id = %s",
                            (comparison_id, source_id, project_id),
                        )
                        if str(comparison_trigger) == "automatic":
                            update_collection_source_monitoring_coverage(
                                conn,
                                source_id=source_id,
                                run_id=current_run_id,
                                baseline={
                                    "state": "established",
                                    "comparison_id": comparison_id,
                                    "baseline_run_id": baseline_run_id,
                                    "findings_evaluation_state": "retrying",
                                    "findings_next_retry_at": next_retry_at.isoformat(),
                                },
                            )
                    write_audit(
                        conn,
                        project_id,
                        "COMPARISON_COMPLETED",
                        "run_comparison",
                        comparison_id,
                        {
                            "worker": CONSUMER_NAME,
                            "summary": summary,
                            "findings_evaluation_state": "retrying",
                        },
                    )
                    write_audit(
                        conn,
                        project_id,
                        "COMPARISON_FINDINGS_EVALUATION_RETRY_SCHEDULED",
                        "run_comparison",
                        comparison_id,
                        {
                            "worker": CONSUMER_NAME,
                            "error_code": "FINDING_EVALUATION_FAILED",
                            "error_type": type(findings_exc).__name__[:120],
                            "findings_observed": finding_count,
                            "attempt_count": findings_attempt_count,
                            "next_retry_at": next_retry_at.isoformat(),
                        },
                    )
                    conn.commit()
                    return "complete"
                findings_evaluation_state = "degraded"
                progress = {
                    **progress,
                    "findings_cursor": finding_cursor,
                    "findings_observed": finding_count,
                    "findings_attempt_count": findings_attempt_count,
                    "findings_evaluation_state": "degraded",
                }
                summary["findings_evaluation"] = {
                    "state": "degraded",
                    "attempt_count": findings_attempt_count,
                    "error_code": "FINDING_EVALUATION_FAILED",
                    "error_type": type(findings_exc).__name__[:120],
                    "partial_positive_evidence_retained": finding_count > 0,
                }
                write_audit(
                    conn,
                    project_id,
                    "COMPARISON_FINDINGS_EVALUATION_FAILED",
                    "run_comparison",
                    comparison_id,
                    {
                        "worker": CONSUMER_NAME,
                        "error_code": "FINDING_EVALUATION_FAILED",
                        "error_type": type(findings_exc).__name__[:120],
                        "findings_observed": finding_count,
                    },
                )
            if _shutdown_event.is_set():
                return yield_comparison(progress, "worker_shutdown")
            summary.setdefault(
                "findings_evaluation",
                {
                    "state": findings_evaluation_state,
                    "authoritative_state": findings_authoritative,
                },
            )
            conn.execute(
                """
                UPDATE run_comparisons
                SET state = 'complete', completed_at = NOW(), heartbeat_at = NOW(),
                    next_retry_at = NULL,
                    progress = jsonb_build_object(
                        'phase', 'complete',
                        'processed', %s::bigint,
                        'changes_emitted', %s::bigint,
                        'processed_items', %s::bigint,
                        'item_changes_emitted', %s::bigint,
                        'findings_observed', %s::bigint,
                        'findings_cursor', %s::bigint,
                        'findings_evaluation_state', %s::text,
                        'findings_attempt_count', %s::integer
                    ),
                    summary = %s::jsonb, error_code = NULL, error_message = NULL
                WHERE id = %s
                """,
                (
                    processed,
                    emitted,
                    item_processed,
                    item_emitted,
                    finding_count,
                    finding_cursor,
                    findings_evaluation_state,
                    int(progress.get("findings_attempt_count") or 0),
                    json.dumps(summary),
                    comparison_id,
                ),
            )
            if source_id and findings_authoritative:
                conn.execute(
                    """
                    UPDATE collection_sources
                    SET last_comparison_id = %s, updated_at = NOW()
                    WHERE id = %s AND project_id = %s
                    """,
                    (comparison_id, source_id, project_id),
                )
                if str(comparison_trigger) == "automatic":
                    update_collection_source_monitoring_coverage(
                        conn,
                        source_id=source_id,
                        run_id=current_run_id,
                        baseline={
                            "state": "established",
                            "comparison_id": comparison_id,
                            "baseline_run_id": baseline_run_id,
                            "findings_evaluation_state": findings_evaluation_state,
                        },
                    )
            write_audit(
                conn,
                project_id,
                "COMPARISON_COMPLETED",
                "run_comparison",
                comparison_id,
                {"worker": CONSUMER_NAME, "summary": summary},
            )
            conn.commit()
            return "complete"
        except Exception as exc:
            logger.exception("comparison failed comparison_id=%s", comparison_id)
            try:
                conn.rollback()
            except psycopg.Error:
                logger.exception("failed to roll back comparison transaction comparison_id=%s", comparison_id)
            retryable = _is_retryable_ingest_error(exc)
            should_retry = retryable and attempt_count < 3
            retry_delay_seconds = (
                _retry_backoff_seconds(attempt_count, jitter_key=comparison_id) if should_retry else None
            )
            next_retry_at = (
                datetime.now(tz=UTC) + timedelta(seconds=retry_delay_seconds)
                if retry_delay_seconds is not None
                else None
            )
            error_code = (
                "COMPARISON_RETRY_SCHEDULED"
                if should_retry
                else ("COMPARISON_INVALID" if isinstance(exc, ValueError) else "COMPARISON_FAILED")
            )
            error_message = _public_comparison_error(exc)
            conn.execute(
                """
                UPDATE run_comparisons
                SET state = %s, heartbeat_at = NOW(),
                    completed_at = CASE WHEN %s = 'failed' THEN NOW() ELSE NULL END,
                    next_retry_at = %s,
                    error_code = %s, error_message = %s,
                    progress = COALESCE(progress, '{}'::jsonb) || %s::jsonb
                WHERE id = %s
                """,
                (
                    "queued" if should_retry else "failed",
                    "queued" if should_retry else "failed",
                    next_retry_at,
                    error_code,
                    error_message,
                    json.dumps(
                        {
                            "last_error_phase": "retry_queued" if should_retry else "failed",
                            "attempt_count": attempt_count,
                            "retryable": retryable,
                            "retry_delay_seconds": retry_delay_seconds,
                            "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                            "resume_after_error": should_retry,
                        }
                    ),
                    comparison_id,
                ),
            )
            if source_id and str(comparison_trigger) == "automatic":
                try:
                    if comparison_is_latest_complete_candidate(
                        conn,
                        comparison_id=comparison_id,
                        source_id=source_id,
                        current_run_id=current_run_id,
                    ):
                        update_collection_source_monitoring_coverage(
                            conn,
                            source_id=source_id,
                            run_id=current_run_id,
                            baseline={
                                "state": "retrying" if should_retry else "failed",
                                "comparison_id": comparison_id,
                                "error_code": error_code,
                                "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                            },
                        )
                except psycopg.Error:
                    logger.exception(
                        "failed updating automatic comparison coverage comparison_id=%s", comparison_id
                    )
            if project_id:
                write_audit(
                    conn,
                    project_id,
                    "COMPARISON_RETRY_SCHEDULED" if should_retry else "COMPARISON_FAILED",
                    "run_comparison",
                    comparison_id,
                    {
                        "worker": CONSUMER_NAME,
                        "error_code": error_code,
                        "attempt_count": attempt_count,
                        "retryable": retryable,
                        "retry_delay_seconds": retry_delay_seconds,
                        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                    },
                )
            conn.commit()
            return "retry_scheduled" if should_retry else "failed"
        finally:
            try:
                # Session advisory locks survive transaction rollback, while an
                # error in status persistence can leave the transaction aborted.
                # Clear that state before attempting the explicit unlock.
                conn.rollback()
                if source_lock_key is not None:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (source_lock_key,))
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            except psycopg.Error:
                logger.exception("failed to release comparison advisory lock comparison_id=%s", comparison_id)


def process_monitoring_evaluation_job(fields: dict[str, str]) -> str:
    """Resume only the durable finding-evaluation phase for a COMPLETE run."""

    run_id = _normalize_uuid_str(fields.get("run_id"))
    if not run_id:
        return "ignored"
    with connect_database() as conn:
        run_lock_key = advisory_lock_key(run_id)
        source_lock_key: int | None = None
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (run_lock_key,)).fetchone()[0]:
            return "busy"
        project_id: str | None = None
        source_id: str | None = None
        try:
            row = conn.execute(
                """
                SELECT project_id::text, source_id::text, status::text, ingest_progress
                FROM scan_runs
                WHERE id = %s
                FOR UPDATE
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return "ignored"
            project_id, source_id, run_status, raw_progress = row
            if run_status != "COMPLETE" or not source_id:
                return "ignored"
            progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
            findings_progress = (
                dict(progress.get("monitoring_findings"))
                if isinstance(progress.get("monitoring_findings"), dict)
                else {}
            )
            if str(findings_progress.get("state") or "") not in {
                "queued",
                "retrying",
                "evaluating",
                "degraded",
            }:
                return "ignored"
            source_lock_key = monitoring_source_advisory_lock_key_from_id(conn, source_id)
            if source_lock_key is not None:
                conn.execute("SELECT pg_advisory_lock(%s)", (source_lock_key,))
            if not collection_source_automation_enabled(conn, source_id):
                skipped = {**findings_progress, "state": "skipped", "phase": "skipped", "reason": "source_disabled"}
                _persist_finding_evaluation_progress(conn, run_id, skipped)
                update_collection_source_monitoring_coverage(
                    conn,
                    source_id=source_id,
                    run_id=run_id,
                    findings=skipped,
                )
                write_audit(
                    conn,
                    project_id,
                    "MONITORING_EVALUATION_SKIPPED",
                    "scan_run",
                    run_id,
                    {"worker": CONSUMER_NAME, "source_id": source_id, "reason": "source_disabled"},
                )
                conn.commit()
                return "complete"
            if not collection_source_run_is_latest_complete_candidate(conn, source_id, run_id):
                skipped = {**findings_progress, "state": "skipped", "phase": "skipped", "reason": "superseded"}
                _persist_finding_evaluation_progress(conn, run_id, skipped)
                write_audit(
                    conn,
                    project_id,
                    "MONITORING_EVALUATION_SKIPPED",
                    "scan_run",
                    run_id,
                    {"worker": CONSUMER_NAME, "source_id": source_id, "reason": "superseded"},
                )
                conn.commit()
                return "complete"
            evaluating = {**findings_progress, "state": "evaluating"}
            evaluating.pop("next_retry_at", None)
            _persist_finding_evaluation_progress(conn, run_id, evaluating)
            update_collection_source_monitoring_coverage(
                conn,
                source_id=source_id,
                run_id=run_id,
                findings=evaluating,
            )
            conn.commit()
            try:
                summary = evaluate_run_findings(
                    conn,
                    project_id=project_id,
                    source_id=source_id,
                    run_id=run_id,
                )
            except Exception as exc:
                logger.exception("monitoring-only evaluation failed run_id=%s", run_id)
                conn.rollback()
                checkpoint = conn.execute(
                    "SELECT ingest_progress->'monitoring_findings' FROM scan_runs WHERE id = %s",
                    (run_id,),
                ).fetchone()
                checkpoint_payload = dict(checkpoint[0]) if checkpoint and isinstance(checkpoint[0], dict) else {}
                attempt_count = int(checkpoint_payload.get("attempt_count") or findings_progress.get("attempt_count") or 0) + 1
                retry_scheduled = attempt_count < 3
                retry_delay_seconds = (
                    _retry_backoff_seconds(attempt_count, jitter_key=f"run-findings:{run_id}")
                    if retry_scheduled
                    else None
                )
                next_retry_at = (
                    datetime.now(tz=UTC) + timedelta(seconds=retry_delay_seconds)
                    if retry_delay_seconds is not None
                    else None
                )
                failed_progress = {
                    **checkpoint_payload,
                    "state": "retrying" if retry_scheduled else "degraded",
                    "phase": "queued" if retry_scheduled else "failed",
                    "attempt_count": attempt_count,
                    "error_code": "FINDING_EVALUATION_FAILED",
                    "error_type": type(exc).__name__[:120],
                    "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                }
                _persist_finding_evaluation_progress(conn, run_id, failed_progress)
                update_collection_source_monitoring_coverage(
                    conn,
                    source_id=source_id,
                    run_id=run_id,
                    findings=failed_progress,
                )
                write_audit(
                    conn,
                    project_id,
                    (
                        "MONITORING_EVALUATION_RETRY_SCHEDULED"
                        if retry_scheduled
                        else "MONITORING_EVALUATION_FAILED"
                    ),
                    "scan_run",
                    run_id,
                    {
                        "worker": CONSUMER_NAME,
                        "source_id": source_id,
                        "attempt_count": attempt_count,
                        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                        "error_code": "FINDING_EVALUATION_FAILED",
                        "error_type": type(exc).__name__[:120],
                    },
                )
                conn.commit()
                return "retry_scheduled" if retry_scheduled else "degraded"
            complete_progress = {
                "state": "complete",
                "phase": "complete",
                "observed": summary["observed"],
                "resolved": summary["resolved"],
                "attempt_count": int(findings_progress.get("attempt_count") or 0),
            }
            _persist_finding_evaluation_progress(conn, run_id, complete_progress)
            update_collection_source_monitoring_coverage(
                conn,
                source_id=source_id,
                run_id=run_id,
                findings=complete_progress,
            )
            write_audit(
                conn,
                project_id,
                "MONITORING_EVALUATION_RECOVERED",
                "scan_run",
                run_id,
                {"worker": CONSUMER_NAME, "source_id": source_id, **summary},
            )
            conn.commit()
            return "complete"
        finally:
            try:
                conn.rollback()
                if source_lock_key is not None:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (source_lock_key,))
                conn.execute("SELECT pg_advisory_unlock(%s)", (run_lock_key,))
            except psycopg.Error:
                    logger.exception("failed to release monitoring evaluation locks run_id=%s", run_id)


def process_comparison_finding_evaluation_job(fields: dict[str, str]) -> str:
    """Resume policy evaluation without hiding or rebuilding a valid materialized diff."""

    comparison_id = _normalize_uuid_str(fields.get("comparison_id"))
    if not comparison_id:
        return "ignored"
    with connect_database() as conn:
        comparison_lock_key = advisory_lock_key(comparison_id)
        source_lock_key: int | None = None
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (comparison_lock_key,)).fetchone()[0]:
            return "busy"
        try:
            row = conn.execute(
                """
                SELECT project_id::text, source_id::text, baseline_run_id::text,
                       current_run_id::text, state, progress, summary, trigger
                FROM run_comparisons
                WHERE id = %s
                FOR UPDATE
                """,
                (comparison_id,),
            ).fetchone()
            if row is None:
                return "ignored"
            project_id, source_id, baseline_run_id, current_run_id, state, raw_progress, raw_summary, trigger = row
            summary = dict(raw_summary) if isinstance(raw_summary, dict) else {}
            evaluation = (
                dict(summary.get("findings_evaluation"))
                if isinstance(summary.get("findings_evaluation"), dict)
                else {}
            )
            if str(state) != "complete" or str(evaluation.get("state") or "") not in {
                "queued",
                "retrying",
                "evaluating",
                "degraded",
            }:
                return "ignored"
            if source_id:
                source_lock_key = monitoring_source_advisory_lock_key_from_id(conn, source_id)
                if source_lock_key is not None:
                    conn.execute("SELECT pg_advisory_lock(%s)", (source_lock_key,))
            authoritative = bool(
                not source_id
                or comparison_is_latest_complete_candidate(
                    conn,
                    comparison_id=comparison_id,
                    source_id=source_id,
                    current_run_id=current_run_id,
                )
            )
            progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
            cursor = int(progress.get("findings_cursor") or 0)
            observed = int(progress.get("findings_observed") or 0)
            attempt_count = int(progress.get("findings_attempt_count") or evaluation.get("attempt_count") or 0)
            batches = 0
            started = time.monotonic()
            try:
                while not _shutdown_event.is_set():
                    inserted, cursor, has_more = evaluate_comparison_findings(
                        conn,
                        comparison_id=comparison_id,
                        project_id=project_id,
                        source_id=source_id,
                        current_run_id=current_run_id,
                        after_id=cursor,
                        authoritative_state=authoritative,
                    )
                    observed += inserted
                    progress = {
                        **progress,
                        "phase": "complete",
                        "findings_cursor": cursor,
                        "findings_observed": observed,
                        "findings_attempt_count": attempt_count,
                        "findings_evaluation_state": "evaluating",
                    }
                    evaluation = {
                        **evaluation,
                        "state": "evaluating",
                        "attempt_count": attempt_count,
                        "next_retry_at": None,
                    }
                    summary["findings_evaluation"] = evaluation
                    conn.execute(
                        "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb, "
                        "summary = %s::jsonb WHERE id = %s",
                        (json.dumps(progress), json.dumps(summary), comparison_id),
                    )
                    conn.commit()
                    batches += 1
                    if not has_more:
                        break
                    if (
                        batches >= COMPARISON_WORK_QUANTUM_BATCHES
                        or time.monotonic() - started >= COMPARISON_WORK_QUANTUM_SECONDS
                    ):
                        evaluation = {**evaluation, "state": "retrying", "next_retry_at": now_iso()}
                        summary["findings_evaluation"] = evaluation
                        conn.execute(
                            "UPDATE run_comparisons SET summary = %s::jsonb, heartbeat_at = NOW() WHERE id = %s",
                            (json.dumps(summary), comparison_id),
                        )
                        conn.commit()
                        return "yielded"
            except Exception as exc:
                logger.exception("comparison finding-only recovery failed comparison_id=%s", comparison_id)
                conn.rollback()
                attempt_count += 1
                retry_scheduled = attempt_count < 3
                delay = (
                    _retry_backoff_seconds(attempt_count, jitter_key=f"comparison-findings:{comparison_id}")
                    if retry_scheduled
                    else None
                )
                next_retry = datetime.now(tz=UTC) + timedelta(seconds=delay) if delay else None
                evaluation = {
                    **evaluation,
                    "state": "retrying" if retry_scheduled else "degraded",
                    "attempt_count": attempt_count,
                    "next_retry_at": next_retry.isoformat() if next_retry else None,
                    "error_code": "FINDING_EVALUATION_FAILED",
                    "error_type": type(exc).__name__[:120],
                    "partial_positive_evidence_retained": observed > 0,
                }
                summary["findings_evaluation"] = evaluation
                progress = {
                    **progress,
                    "phase": "complete",
                    "findings_cursor": cursor,
                    "findings_observed": observed,
                    "findings_attempt_count": attempt_count,
                    "findings_evaluation_state": evaluation["state"],
                }
                conn.execute(
                    "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb, "
                    "summary = %s::jsonb WHERE id = %s",
                    (json.dumps(progress), json.dumps(summary), comparison_id),
                )
                if source_id and authoritative and str(trigger) == "automatic":
                    update_collection_source_monitoring_coverage(
                        conn,
                        source_id=source_id,
                        run_id=current_run_id,
                        baseline={
                            "state": "established",
                            "comparison_id": comparison_id,
                            "baseline_run_id": baseline_run_id,
                            "findings_evaluation_state": evaluation["state"],
                            "findings_next_retry_at": evaluation.get("next_retry_at"),
                        },
                    )
                write_audit(
                    conn,
                    project_id,
                    (
                        "COMPARISON_FINDINGS_EVALUATION_RETRY_SCHEDULED"
                        if retry_scheduled
                        else "COMPARISON_FINDINGS_EVALUATION_FAILED"
                    ),
                    "run_comparison",
                    comparison_id,
                    {
                        "worker": CONSUMER_NAME,
                        "attempt_count": attempt_count,
                        "next_retry_at": evaluation.get("next_retry_at"),
                        "error_code": "FINDING_EVALUATION_FAILED",
                        "error_type": type(exc).__name__[:120],
                    },
                )
                conn.commit()
                return "retry_scheduled" if retry_scheduled else "degraded"
            evaluation = {
                **evaluation,
                "state": "complete",
                "attempt_count": attempt_count,
                "next_retry_at": None,
                "authoritative_state": authoritative,
            }
            evaluation.pop("error_code", None)
            evaluation.pop("error_type", None)
            summary["findings_evaluation"] = evaluation
            progress = {
                **progress,
                "phase": "complete",
                "findings_cursor": cursor,
                "findings_observed": observed,
                "findings_attempt_count": attempt_count,
                "findings_evaluation_state": "complete",
            }
            conn.execute(
                "UPDATE run_comparisons SET heartbeat_at = NOW(), progress = %s::jsonb, "
                "summary = %s::jsonb WHERE id = %s",
                (json.dumps(progress), json.dumps(summary), comparison_id),
            )
            if source_id and authoritative and str(trigger) == "automatic":
                update_collection_source_monitoring_coverage(
                    conn,
                    source_id=source_id,
                    run_id=current_run_id,
                    baseline={
                        "state": "established",
                        "comparison_id": comparison_id,
                        "baseline_run_id": baseline_run_id,
                        "findings_evaluation_state": "complete",
                    },
                )
            write_audit(
                conn,
                project_id,
                "COMPARISON_FINDINGS_EVALUATION_RECOVERED",
                "run_comparison",
                comparison_id,
                {"worker": CONSUMER_NAME, "findings_observed": observed, "authoritative_state": authoritative},
            )
            conn.commit()
            return "complete"
        finally:
            try:
                conn.rollback()
                if source_lock_key is not None:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (source_lock_key,))
                conn.execute("SELECT pg_advisory_unlock(%s)", (comparison_lock_key,))
            except psycopg.Error:
                logger.exception(
                    "failed to release comparison finding evaluation locks comparison_id=%s", comparison_id
                )


def process_job(fields: dict[str, str]) -> str:
    if str(fields.get("job_type") or "").strip().lower() == "comparison_findings_evaluation":
        return process_comparison_finding_evaluation_job(fields)
    if str(fields.get("job_type") or "").strip().lower() == "monitoring_evaluation":
        return process_monitoring_evaluation_job(fields)
    if str(fields.get("job_type") or "").strip().lower() == "comparison" or fields.get("comparison_id"):
        return process_comparison_job(fields)

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
        source_lock_key: int | None = None
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,)).fetchone()[0]
        if not locked:
            logger.info("run is already being processed run_id=%s", run_id)
            return "busy"

        try:
            row = conn.execute(
                """
                SELECT project_id::text, artifact_key, status::text, summary, ingest_progress,
                       artifact_content_type, artifact_size, collection_context, artifact_sha256
                FROM scan_runs
                WHERE id = %s
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                logger.warning("run not found run_id=%s", run_id)
                return "ignored"

            db_project_id, db_artifact_key, status, summary_raw, progress_raw, artifact_content_type, artifact_size = (
                row[:7]
            )
            persisted_collection_context = row[7] if len(row) > 7 and isinstance(row[7], dict) else {}
            artifact_sha256 = row[8] if len(row) > 8 else None
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
            producer_run_end_counts = _validated_producer_inventory_counts(
                progress_raw.get("producer_inventory_counts") if isinstance(progress_raw, dict) else None
            )
            last_line_offset = line_offset
            last_counts = counts.copy()
            last_worker_heartbeat = 0.0
            artifact_integrity = _require_artifact_integrity(artifact_size, artifact_sha256)

            def ingest_progress_extra(**extra: Any) -> dict[str, Any]:
                progress = {"attempt_count": attempt_count}
                if producer_run_end_counts is not None:
                    progress["producer_inventory_counts"] = producer_run_end_counts
                if isinstance(progress_raw, dict) and isinstance(progress_raw.get("monitoring_findings"), dict):
                    progress["monitoring_findings"] = progress_raw["monitoring_findings"]
                progress.update(extra)
                return progress

            def emit_processing_heartbeat(force: bool = False) -> None:
                nonlocal last_worker_heartbeat
                now = time.time()
                if force or now - last_worker_heartbeat >= WORKER_HEARTBEAT_INTERVAL_SECONDS:
                    _write_worker_heartbeat("processing", run_id=run_id, line_offset=line_offset)
                    last_worker_heartbeat = now

            emit_processing_heartbeat(force=True)
            update_run_status(
                conn,
                run_id,
                "INGESTING",
                line_offset,
                counts,
                extra_progress=ingest_progress_extra(),
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
            permission_entry_batch: list[tuple[int, int | None, dict[str, Any]]] = []
            permission_entry_batch_bytes = 0
            permission_record_batch: list[dict[str, Any]] = []
            permission_record_batch_bytes = 0
            permission_assessment_batch_count = 0

            def flush_permission_entries() -> None:
                nonlocal counts, permission_entry_batch_bytes
                if not permission_entry_batch:
                    return
                try:
                    flush_permission_entry_batch(conn, run_id, permission_entry_batch)
                    permission_entry_batch_bytes = 0
                    return
                except ValueError:
                    # A collision is exceptional and normally isolated to one
                    # malformed record. Preserve the established bounded-error
                    # behavior by falling back only for this batch.
                    pending = list(permission_entry_batch)
                    permission_entry_batch.clear()
                    permission_entry_batch_bytes = 0
                for assessment_id, principal_id, entry_rec in pending:
                    try:
                        upsert_permission_entry(conn, run_id, assessment_id, principal_id, entry_rec)
                    except ValueError as exc:
                        error_batch.append(
                            build_ingest_error_row(
                                run_id,
                                "error",
                                "PERMISSION_EVIDENCE_INVALID",
                                str(exc),
                                entry_rec.get("endpoint_key"),
                                entry_rec.get("resource_name"),
                                entry_rec.get("subject_path"),
                            )
                        )
                        counts["errors"] += 1
                if len(error_batch) >= BATCH_SIZE:
                    flush_error_batch(conn, error_batch)

            def checkpoint_shutdown_if_requested() -> None:
                nonlocal last_line_offset, last_counts
                if not _shutdown_event.is_set():
                    return
                if permission_record_batch:
                    flush_permission_records()
                else:
                    flush_item_batch(conn, item_batch)
                flush_permission_entries()
                flush_error_batch(conn, error_batch)
                update_run_status(
                    conn,
                    run_id,
                    "UPLOADED",
                    line_offset,
                    counts,
                    extra_progress=ingest_progress_extra(
                        paused_at=now_iso(),
                        paused_by=CONSUMER_NAME,
                        pause_reason="worker_shutdown",
                    ),
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
                artifact_integrity,
                progress_callback=report_preflight_progress,
            )

            endpoint_cache: dict[str, int] = _BoundedLRUCache[str](INGEST_IDENTITY_CACHE_SIZE)
            resource_cache: dict[tuple[str, str, str], int] = _BoundedLRUCache[tuple[str, str, str]](
                INGEST_IDENTITY_CACHE_SIZE
            )
            if line_offset > 0:
                endpoint_cache, resource_cache = load_resume_caches(conn, run_id)
            persisted_schema_version = persisted_collection_context.get("artifact_schema_version")
            artifact_schema_version: int | None = (
                int(persisted_schema_version)
                if isinstance(persisted_schema_version, int) and persisted_schema_version in {1, 2}
                else None
            )
            persisted_features = persisted_collection_context.get("artifact_features")
            artifact_features: set[str] = (
                {str(feature).strip() for feature in persisted_features if isinstance(feature, str) and feature.strip()}
                if isinstance(persisted_features, list)
                else set()
            )
            if artifact_schema_version is None and "direct_permissions_v1" in artifact_features:
                # Compatibility for a schema-v2 ingest checkpoint created by
                # an earlier worker that persisted features but not the
                # explicit schema version.
                artifact_schema_version = 2
            assessment_cache: dict[str, int] = _BoundedLRUCache[str](INGEST_IDENTITY_CACHE_SIZE)
            principal_cache: dict[tuple[str, str, str], int] = _BoundedLRUCache[tuple[str, str, str]](
                INGEST_IDENTITY_CACHE_SIZE
            )

            def permission_assessment_cache_key(rec: dict[str, Any]) -> str:
                return ":".join(
                    (
                        rec["assessment_key"],
                        rec["provider"],
                        rec["semantics"],
                        rec["permission_surface"],
                    )
                )

            def process_permission_record(rec: dict[str, Any]) -> None:
                nonlocal permission_entry_batch_bytes
                if rec["type"] == "permission_assessment":
                    resource_id, item_id = resolve_permission_subject(
                        conn,
                        run_id,
                        rec,
                        endpoint_cache,
                        resource_cache,
                    )
                    assessment_id = upsert_permission_assessment(
                        conn,
                        run_id,
                        resource_id,
                        item_id,
                        rec,
                    )
                    assessment_cache[permission_assessment_cache_key(rec)] = assessment_id
                    return

                cache_key = permission_assessment_cache_key(rec)
                assessment_id = assessment_cache.get(cache_key)
                if assessment_id is None:
                    row = conn.execute(
                        """
                        SELECT id
                        FROM permission_assessments
                        WHERE run_id = %s
                          AND assessment_key = %s
                          AND provider = %s
                          AND semantics = %s
                          AND permission_surface = %s
                        LIMIT 1
                        """,
                        (
                            run_id,
                            rec["assessment_key"],
                            rec["provider"],
                            rec["semantics"],
                            rec["permission_surface"],
                        ),
                    ).fetchone()
                    if row is None:
                        raise ValueError("permission_entry references an assessment that has not been emitted")
                    assessment_id = int(row[0])
                    assessment_cache[cache_key] = assessment_id

                principal = rec.get("principal")
                principal_id = None
                if principal is not None:
                    presentation_hash = hashlib.sha256(
                        json.dumps(
                            principal,
                            sort_keys=True,
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    principal_cache_key = (
                        principal["provider"],
                        principal["principal_key"],
                        presentation_hash,
                    )
                    principal_id = principal_cache.get(principal_cache_key)
                    if principal_id is None:
                        principal_id = upsert_permission_principal(conn, run_id, principal)
                        if principal_id is not None:
                            principal_cache[principal_cache_key] = principal_id

                entry_size = len(
                    json.dumps(
                        rec,
                        sort_keys=True,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                )
                if (
                    permission_entry_batch
                    and permission_entry_batch_bytes + entry_size > PERMISSION_ENTRY_BATCH_MAX_BYTES
                ):
                    flush_permission_entries()
                permission_entry_batch.append((assessment_id, principal_id, rec))
                permission_entry_batch_bytes += entry_size
                if len(permission_entry_batch) >= PERMISSION_ENTRY_BATCH_SIZE:
                    flush_permission_entries()

            def flush_permission_records() -> None:
                nonlocal counts
                nonlocal permission_record_batch_bytes, permission_assessment_batch_count
                if not permission_record_batch:
                    return
                # SharePoint all-item streams interleave one item with its
                # assessment. Delay subject resolution just enough to retain
                # bulk item inserts, then preserve record order within this
                # bounded pipeline.
                flush_item_batch(conn, item_batch)
                pending = list(permission_record_batch)
                permission_record_batch.clear()
                permission_record_batch_bytes = 0
                permission_assessment_batch_count = 0
                for permission_rec in pending:
                    try:
                        process_permission_record(permission_rec)
                    except ValueError as exc:
                        error_batch.append(
                            build_ingest_error_row(
                                run_id,
                                "error",
                                "PERMISSION_EVIDENCE_INVALID",
                                str(exc),
                                permission_rec.get("endpoint_key"),
                                permission_rec.get("resource_name"),
                                permission_rec.get("subject_path"),
                            )
                        )
                        counts["errors"] += 1
                if len(error_batch) >= BATCH_SIZE:
                    flush_error_batch(conn, error_batch)

            def process_record(rec: dict[str, Any]) -> None:
                nonlocal counts, producer_run_end_counts, artifact_schema_version, artifact_features
                nonlocal permission_record_batch_bytes, permission_assessment_batch_count
                if not isinstance(rec, dict):
                    error_batch.append(
                        build_ingest_error_row(
                            run_id,
                            "error",
                            CONSUMER_UNCLASSIFIED_RECORD_ERROR,
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
                            _record_validation_error_code(rec),
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
                    artifact_schema_version = int(rec.get("schema_version") or 1)
                    raw_features = rec.get("artifact_features")
                    artifact_features = (
                        {
                            str(feature).strip()
                            for feature in raw_features
                            if isinstance(feature, str) and feature.strip()
                        }
                        if isinstance(raw_features, list)
                        else set()
                    )
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
                            json.dumps(rec.get("permission_summary") or {}),
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
                elif rec_type in PERMISSION_RECORD_TYPES:
                    if artifact_schema_version != 2 or "direct_permissions_v1" not in artifact_features:
                        error_batch.append(
                            build_ingest_error_row(
                                run_id,
                                "error",
                                "PERMISSION_EVIDENCE_INVALID",
                                "permission evidence requires schema_version 2 and artifact feature direct_permissions_v1",
                                rec.get("endpoint_key"),
                                rec.get("resource_name"),
                                rec.get("subject_path"),
                            )
                        )
                        counts["errors"] += 1
                        if len(error_batch) >= BATCH_SIZE:
                            flush_error_batch(conn, error_batch)
                        return
                    record_size = len(
                        json.dumps(
                            rec,
                            sort_keys=True,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    )
                    if (
                        permission_record_batch
                        and permission_record_batch_bytes + record_size > PERMISSION_ENTRY_BATCH_MAX_BYTES
                    ):
                        flush_permission_records()
                    permission_record_batch.append(rec)
                    permission_record_batch_bytes += record_size
                    if rec_type == "permission_assessment":
                        permission_assessment_batch_count += 1
                    if permission_assessment_batch_count >= PERMISSION_ASSESSMENT_PIPELINE_SIZE:
                        flush_permission_records()
                elif rec_type == "run_end":
                    producer_run_end_counts = _validated_producer_inventory_counts(rec.get("stats"))

            def persist_periodic_progress() -> None:
                nonlocal last_line_offset, last_counts
                if line_offset % PROGRESS_EVERY_LINES != 0:
                    return
                flush_permission_records()
                flush_item_batch(conn, item_batch)
                flush_permission_entries()
                flush_error_batch(conn, error_batch)
                update_run_status(
                    conn,
                    run_id,
                    "INGESTING",
                    line_offset,
                    counts,
                    extra_progress=ingest_progress_extra(),
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
                    with open_verified_artifact_stream(
                        artifact_key,
                        artifact_integrity,
                        progress_callback=report_preflight_progress,
                    ) as body:
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
                    with open_verified_artifact_stream(
                        artifact_key,
                        artifact_integrity,
                        progress_callback=report_preflight_progress,
                    ) as compat_body:
                        raw_json = _read_json_compat_bytes(
                            compat_body,
                            gzip_input=gzip_input,
                            max_bytes=JSON_COMPAT_MAX_BYTES,
                        )
                    json_records = _load_json_records_from_bytes(raw_json, run_id)
                    if json_records is None:
                        raise ValueError("unsupported JSON artifact format")
            else:
                with open_verified_artifact_stream(
                    artifact_key,
                    artifact_integrity,
                    progress_callback=report_preflight_progress,
                ) as body:
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
            flush_permission_records()
            flush_item_batch(conn, item_batch)
            flush_permission_entries()
            flush_error_batch(conn, error_batch)
            if artifact_schema_version == 2 and "direct_permissions_v1" in artifact_features:
                reconcile_permission_evidence_integrity(conn, run_id)
                reconcile_permission_summaries(conn, run_id)
                permission_integrity = reconcile_permission_collection_context(conn, run_id)
                if permission_integrity.get("status") != "verified_complete":
                    logger.warning(
                        "permission evidence coverage is incomplete run_id=%s diagnostics=%s",
                        run_id,
                        permission_integrity,
                    )
            persisted_counts = load_persisted_summary(conn, run_id)
            persisted_inventory_counts = {
                field: persisted_counts[field] for field in ("endpoints", "resources", "items")
            }
            if producer_run_end_counts != persisted_inventory_counts:
                logger.warning(
                    "producer summary differs from persisted inventory run_id=%s producer=%s persisted=%s",
                    run_id,
                    producer_run_end_counts,
                    persisted_inventory_counts,
                )
            inventory_integrity = reconcile_inventory_collection_context(
                conn,
                run_id,
                producer_counts=producer_run_end_counts,
                persisted_counts=persisted_counts,
            )
            if inventory_integrity.get("status") != "verified":
                logger.warning(
                    "inventory ingest coverage is incomplete run_id=%s diagnostics=%s",
                    run_id,
                    inventory_integrity,
                )
            counts = persisted_counts

            # The processing stream authenticates the exact bytes that drove
            # normalization. Re-open the immutable key immediately before the
            # terminal transition as a fail-closed guard against an external
            # path replacement while reconciliation was running.
            verify_artifact_integrity(
                artifact_key,
                artifact_integrity,
                progress_callback=report_preflight_progress,
            )
            source_lock_key = monitoring_source_advisory_lock_key(conn, run_id, project_id)
            if source_lock_key is not None:
                # Session-scoped by design: findings checkpoints commit each
                # batch, but another worker for the same source must not
                # interleave a newer authoritative snapshot.
                conn.execute("SELECT pg_advisory_lock(%s)", (source_lock_key,))
            source_id = register_collection_source(conn, run_id, project_id)
            finding_summary = {"observed": 0, "resolved": 0}
            automatic_comparison_id = None
            automation_enabled = bool(source_id and collection_source_automation_enabled(conn, source_id))
            source_run_is_latest = bool(
                source_id
                and collection_source_run_is_latest_complete_candidate(conn, source_id, run_id)
            )
            if source_id and automation_enabled and source_run_is_latest:
                # Legacy artifacts without normalized source context do not
                # participate in monitoring. Avoid an unnecessary full-table
                # identity rewrite for those imports; comparisons prepare
                # identities independently when explicitly requested.
                prepare_run_identity_keys(conn, run_id, commit_batches=True)
                existing_monitoring_progress = (
                    progress_raw.get("monitoring_findings")
                    if isinstance(progress_raw, dict) and isinstance(progress_raw.get("monitoring_findings"), dict)
                    else None
                )
                if not existing_monitoring_progress:
                    _persist_finding_evaluation_progress(
                        conn,
                        run_id,
                        {
                            "state": "evaluating",
                            "phase": "candidates",
                            "after_resource_id": 0,
                            "observed": 0,
                            "resolved": 0,
                        },
                    )
                # Persist a durable monitoring checkpoint only after the
                # artifact and normalized inventory have passed integrity
                # reconciliation. A recovered ingest resumes this cursor
                # rather than accumulating an unbounded findings transaction.
                conn.commit()
                try:
                    finding_summary = evaluate_run_findings(
                        conn,
                        project_id=project_id,
                        source_id=source_id,
                        run_id=run_id,
                    )
                    monitoring_progress = {
                        "state": "complete",
                        "phase": "complete",
                        "observed": finding_summary["observed"],
                        "resolved": finding_summary["resolved"],
                    }
                except Exception as monitoring_exc:
                    # Inventory integrity is already verified. Monitoring is a
                    # derived workflow and must not turn a valid collection
                    # into a FAILED parent after durable positive-evidence
                    # batches have committed. Absence resolution starts only
                    # after the complete candidate pass.
                    logger.exception("finding evaluation degraded run_id=%s", run_id)
                    conn.rollback()
                    checkpoint = conn.execute(
                        "SELECT ingest_progress->'monitoring_findings' FROM scan_runs WHERE id = %s",
                        (run_id,),
                    ).fetchone()
                    checkpoint_payload = (
                        dict(checkpoint[0]) if checkpoint and isinstance(checkpoint[0], dict) else {}
                    )
                    finding_summary = {
                        "observed": int(checkpoint_payload.get("observed") or 0),
                        "resolved": int(checkpoint_payload.get("resolved") or 0),
                    }
                    findings_attempt_count = int(checkpoint_payload.get("attempt_count") or 0) + 1
                    retry_scheduled = findings_attempt_count < 3
                    retry_delay_seconds = (
                        _retry_backoff_seconds(
                            findings_attempt_count,
                            jitter_key=f"run-findings:{run_id}",
                        )
                        if retry_scheduled
                        else None
                    )
                    next_retry_at = (
                        datetime.now(tz=UTC) + timedelta(seconds=retry_delay_seconds)
                        if retry_delay_seconds is not None
                        else None
                    )
                    monitoring_progress = {
                        **checkpoint_payload,
                        "state": "retrying" if retry_scheduled else "degraded",
                        "phase": "queued" if retry_scheduled else "failed",
                        "attempt_count": findings_attempt_count,
                        "error_code": "FINDING_EVALUATION_FAILED",
                        "error_type": type(monitoring_exc).__name__[:120],
                        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                    }
                    write_audit(
                        conn,
                        project_id,
                        "MONITORING_EVALUATION_FAILED",
                        "scan_run",
                        run_id,
                        {
                            "worker": CONSUMER_NAME,
                            "source_id": source_id,
                            "error_code": "FINDING_EVALUATION_FAILED",
                            "error_type": type(monitoring_exc).__name__[:120],
                            "attempt_count": findings_attempt_count,
                            "retry_scheduled": retry_scheduled,
                            "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                            "checkpoint": monitoring_progress,
                        },
                    )
                update_run_status(
                    conn,
                    run_id,
                    "COMPLETE",
                    line_offset,
                    counts,
                    extra_progress=ingest_progress_extra(monitoring_findings=monitoring_progress),
                )
                update_collection_source_monitoring_coverage(
                    conn,
                    source_id=source_id,
                    run_id=run_id,
                    findings=monitoring_progress,
                )
                if collection_source_run_is_latest_complete_candidate(conn, source_id, run_id):
                    automatic_comparison_id = create_automatic_comparison(
                        conn,
                        project_id=project_id,
                        source_id=source_id,
                        current_run_id=run_id,
                    )
                else:
                    write_audit(
                        conn,
                        project_id,
                        "MONITORING_AUTOMATION_SKIPPED",
                        "collection_source",
                        source_id,
                        {
                            "worker": CONSUMER_NAME,
                            "run_id": run_id,
                            "reason": "source_superseded_before_comparison",
                        },
                    )
            elif source_id:
                skip_reason = "source_superseded" if not source_run_is_latest else "source_disabled"
                monitoring_progress = {
                    "state": "skipped",
                    "phase": "skipped",
                    "reason": skip_reason,
                    "observed": 0,
                    "resolved": 0,
                }
                update_run_status(
                    conn,
                    run_id,
                    "COMPLETE",
                    line_offset,
                    counts,
                    extra_progress=ingest_progress_extra(monitoring_findings=monitoring_progress),
                )
                if skip_reason == "source_disabled":
                    update_collection_source_monitoring_coverage(
                        conn,
                        source_id=source_id,
                        run_id=run_id,
                        findings=monitoring_progress,
                    )
                write_audit(
                    conn,
                    project_id,
                    "MONITORING_AUTOMATION_SKIPPED",
                    "collection_source",
                    source_id,
                    {
                        "worker": CONSUMER_NAME,
                        "run_id": run_id,
                        "reason": skip_reason,
                        "source_enabled": automation_enabled,
                        "source_run_is_latest": source_run_is_latest,
                    },
                )
            else:
                update_run_status(conn, run_id, "COMPLETE", line_offset, counts)
            write_audit(
                conn,
                project_id,
                "INGEST_COMPLETED",
                "scan_run",
                run_id,
                {
                    "worker": CONSUMER_NAME,
                    "line_offset": line_offset,
                    "counts": counts,
                    "source_id": source_id,
                    "findings": finding_summary,
                    "automatic_comparison_id": automatic_comparison_id,
                    "automation_enabled": automation_enabled,
                    "source_run_is_latest_complete": source_run_is_latest,
                },
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
            failure_code = "ARTIFACT_INTEGRITY_FAILED" if isinstance(exc, ArtifactIntegrityError) else None
            attempt_count = parse_attempt_count(progress_raw) + 1
            try:
                conn.rollback()
            except psycopg.Error:
                logger.exception("failed to rollback aborted ingest transaction run_id=%s", run_id)
            try:
                durable_progress_row = conn.execute(
                    "SELECT ingest_progress, summary FROM scan_runs WHERE id = %s",
                    (run_id,),
                ).fetchone()
                durable_progress = (
                    dict(durable_progress_row[0])
                    if durable_progress_row and isinstance(durable_progress_row[0], dict)
                    else {}
                )
                if durable_progress_row:
                    last_line_offset = max(last_line_offset, parse_offset(durable_progress))
                    last_counts = parse_summary(durable_progress_row[1])
                monitoring_findings_progress = (
                    durable_progress.get("monitoring_findings")
                    if isinstance(durable_progress.get("monitoring_findings"), dict)
                    else None
                )
                if isinstance(exc, (ArtifactFramingError, ArtifactIntegrityError)):
                    clear_persisted_ingest_inventory(conn, run_id)
                    last_line_offset = 0
                    last_counts = {"endpoints": 0, "resources": 0, "items": 0, "errors": 0}
                    monitoring_findings_progress = None
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
                            **(
                                {"monitoring_findings": monitoring_findings_progress}
                                if monitoring_findings_progress
                                else {}
                            ),
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
                        **({"failure_code": failure_code} if failure_code else {}),
                        **(
                            {"monitoring_findings": monitoring_findings_progress}
                            if monitoring_findings_progress
                            else {}
                        ),
                    },
                )
                failed_source_id = None
                if project_id:
                    failed_source_id = register_collection_source(
                        conn,
                        run_id,
                        project_id,
                        succeeded=False,
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
                            **({"failure_code": failure_code} if failure_code else {}),
                            "source_id": failed_source_id,
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
                if source_lock_key is not None:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (source_lock_key,))
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            except psycopg.Error:
                try:
                    conn.rollback()
                    if source_lock_key is not None:
                        conn.execute("SELECT pg_advisory_unlock(%s)", (source_lock_key,))
                    conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                except psycopg.Error:
                    logger.exception("failed to release ingest advisory locks run_id=%s", run_id)


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
            try:
                reopened_findings = reopen_expired_accepted_risk_findings(limit=100)
                if reopened_findings:
                    logger.info("reopened expired accepted-risk findings count=%s", reopened_findings)
            except psycopg.Error as exc:
                now = time.time()
                if _should_log_redis_error(last_database_recovery_error_log, now):
                    logger.warning("finding expiry recovery failed; retrying: %s", exc)
                    last_database_recovery_error_log = now
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
            if not _shutdown_event.is_set():
                try:
                    recoverable_monitoring = discover_recoverable_monitoring_evaluations(limit=1)
                except psycopg.Error as exc:
                    now = time.time()
                    if _should_log_redis_error(last_database_recovery_error_log, now):
                        logger.warning("database monitoring recovery scan failed; retrying: %s", exc)
                        last_database_recovery_error_log = now
                else:
                    if recoverable_monitoring:
                        process_job(recoverable_monitoring[0])
            if not _shutdown_event.is_set():
                try:
                    recoverable_comparison_findings = discover_recoverable_comparison_finding_evaluations(limit=1)
                except psycopg.Error as exc:
                    now = time.time()
                    if _should_log_redis_error(last_database_recovery_error_log, now):
                        logger.warning("database comparison findings recovery scan failed; retrying: %s", exc)
                        last_database_recovery_error_log = now
                else:
                    if recoverable_comparison_findings:
                        process_job(recoverable_comparison_findings[0])
            if not _shutdown_event.is_set():
                try:
                    recoverable_comparisons = discover_recoverable_comparisons(limit=1)
                except psycopg.Error as exc:
                    now = time.time()
                    if _should_log_redis_error(last_database_recovery_error_log, now):
                        logger.warning("database comparison recovery scan failed; retrying: %s", exc)
                        last_database_recovery_error_log = now
                else:
                    if recoverable_comparisons:
                        process_job(recoverable_comparisons[0])
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
