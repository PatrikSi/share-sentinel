#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import gzip
import hashlib
import io
import ipaddress
import itertools
import json
import math
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

try:
    from impacket.nmb import NetBIOSError, NetBIOSTimeout
    from impacket.smb3structs import (
        DACL_SECURITY_INFORMATION,
        DELETE,
        FILE_ADD_FILE,
        FILE_ADD_SUBDIRECTORY,
        FILE_DELETE_CHILD,
        FILE_DIRECTORY_FILE,
        FILE_NON_DIRECTORY_FILE,
        FILE_OPEN,
        FILE_READ_DATA,
        FILE_SHARE_DELETE,
        FILE_SHARE_READ,
        FILE_SHARE_WRITE,
        FILE_WRITE_DATA,
        GROUP_SECURITY_INFORMATION,
        OWNER_SECURITY_INFORMATION,
        READ_CONTROL,
        SMB2_0_INFO_SECURITY,
        SMB2_SEC_INFO_00,
        WRITE_DAC,
        WRITE_OWNER,
    )
    from impacket.smbconnection import SessionError, SMBConnection
except ImportError:
    SMBConnection = None

    # Keep tests, help, and dependency diagnostics usable without Impacket. The
    # values come from MS-SMB2 and match Impacket's smb3structs constants.
    FILE_READ_DATA = 0x00000001
    FILE_WRITE_DATA = 0x00000002
    FILE_ADD_FILE = 0x00000002
    FILE_ADD_SUBDIRECTORY = 0x00000004
    FILE_DELETE_CHILD = 0x00000040
    DELETE = 0x00010000
    WRITE_DAC = 0x00040000
    WRITE_OWNER = 0x00080000
    FILE_DIRECTORY_FILE = 0x00000001
    FILE_NON_DIRECTORY_FILE = 0x00000040
    FILE_OPEN = 0x00000001
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    READ_CONTROL = 0x00020000
    OWNER_SECURITY_INFORMATION = 0x00000001
    GROUP_SECURITY_INFORMATION = 0x00000002
    DACL_SECURITY_INFORMATION = 0x00000004
    SMB2_0_INFO_SECURITY = 0x03
    SMB2_SEC_INFO_00 = 0

    class SessionError(Exception):
        """Fallback error type used when impacket is unavailable."""

    class NetBIOSError(Exception):
        """Fallback error type used when impacket is unavailable."""

    class NetBIOSTimeout(Exception):
        """Fallback error type used when impacket is unavailable."""


TOOL_VERSION = "1.3.0"
NETWORK_STRUCTURAL_COMPARISON_CONTRACT = "network_share_inventory_v1"
SMB_CONTENT_COMPARISON_CONTRACT = "smb_tree_inventory_v1"
SMB_CAPABILITY_COMPARISON_CONTRACT = "smb_nonmutating_capability_v1"
SENSITIVE_ARGUMENT_FLAGS = {"--password", "--hashes", "--api-token"}
SMB_PASSWORD_ENV = "SHARE_SENTINEL_SMB_PASSWORD"
SMB_HASHES_ENV = "SHARE_SENTINEL_SMB_HASHES"
API_TOKEN_ENV = "SHARE_SENTINEL_API_TOKEN"
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2
EXIT_INTERRUPTED = 130
ARTIFACT_FORMAT_COMPACT_JSON = "compact_json"
ARTIFACT_FORMAT_NDJSON = "ndjson"
COMPACT_JSON_MAX_BUFFER_BYTES = 40 * 1024 * 1024
COMPACT_JSON_MAX_ENDPOINT_BUFFER_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024 * 1024
HOST_INPUT_MAX_LINE_CHARACTERS = 1024
HOST_TARGET_MAX_CHARACTERS = 255
SUPPORTED_ARTIFACT_SUFFIXES = (
    ".json",
    ".json.gz",
    ".ndjson",
    ".ndjson.gz",
    ".jsonl",
    ".jsonl.gz",
)
UPLOAD_RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
SMB_CAPABILITY_NAMES = (
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
SMB_CAPABILITY_OUTCOMES = frozenset({"allowed", "denied", "inconclusive"})
SMB1_DENIED_ERROR_PAIRS = frozenset(
    {
        (0x01, 0x0005),  # ERRDOS/ERRnoaccess
        (0x02, 0x0004),  # ERRSRV/ERRaccess
    }
)
SMB1_STATUS_REASONS = {
    (0x01, 0x0002): "object_not_found",  # ERRDOS/ERRbadfile
    (0x01, 0x0003): "object_not_found",  # ERRDOS/ERRbadpath
    (0x01, 0x0043): "share_unavailable",  # ERRDOS/ERRnosuchshare
    (0x02, 0x0005): "tree_session_invalid",  # ERRSRV/ERRinvnid
    (0x02, 0x0006): "share_unavailable",  # ERRSRV/ERRinvnetname
    (0x02, 0x0043): "invalid_request",  # ERRSRV/ERRfilespecs
}
SMB1_ROOT_PATH_FALLBACK_ERROR_PAIRS = frozenset(
    {
        (0x01, 0x0002),  # ERRDOS/ERRbadfile
        (0x01, 0x0003),  # ERRDOS/ERRbadpath
        (0x02, 0x0043),  # ERRSRV/ERRfilespecs
    }
)
SMB1_FLAGS2_NT_STATUS = 0x4000
SMB_DENIED_STATUS_CODES = frozenset(
    {
        0xC0000022,  # STATUS_ACCESS_DENIED
        0xC00000CA,  # STATUS_NETWORK_ACCESS_DENIED
        0xC0000061,  # STATUS_PRIVILEGE_NOT_HELD
        0xC00000A2,  # STATUS_MEDIA_WRITE_PROTECTED
        0xC0000121,  # STATUS_CANNOT_DELETE
    }
)
SMB_DENIED_STATUS_LABELS = (
    "STATUS_ACCESS_DENIED",
    "STATUS_NETWORK_ACCESS_DENIED",
    "STATUS_PRIVILEGE_NOT_HELD",
    "STATUS_MEDIA_WRITE_PROTECTED",
    "STATUS_CANNOT_DELETE",
)
SMB_SHARE_ENUMERATION_DENIAL_REASONS = frozenset(
    {
        "access_denied",
        "network_access_denied",
        "privilege_not_held",
        "legacy_operation_refused",
    }
)
SMB_STATUS_REASONS = {
    0xC0000022: "access_denied",  # STATUS_ACCESS_DENIED
    0xC00000CA: "network_access_denied",  # STATUS_NETWORK_ACCESS_DENIED
    0xC0000061: "privilege_not_held",  # STATUS_PRIVILEGE_NOT_HELD
    0xC00000A2: "write_protected",  # STATUS_MEDIA_WRITE_PROTECTED
    0xC0000121: "cannot_delete",  # STATUS_CANNOT_DELETE
    0xC00000CC: "share_unavailable",  # STATUS_BAD_NETWORK_NAME
    0xC0000034: "object_not_found",  # STATUS_OBJECT_NAME_NOT_FOUND
    0xC000003A: "object_not_found",  # STATUS_OBJECT_PATH_NOT_FOUND
    0xC000003B: "invalid_request",  # STATUS_OBJECT_PATH_SYNTAX_BAD
    0xC0000043: "sharing_violation",  # STATUS_SHARING_VIOLATION
    0xC0000056: "object_state_changed",  # STATUS_DELETE_PENDING
    0xC0000123: "object_state_changed",  # STATUS_FILE_DELETED
    0xC00000BA: "object_type_mismatch",  # STATUS_FILE_IS_A_DIRECTORY
    0xC0000103: "object_type_mismatch",  # STATUS_NOT_A_DIRECTORY
    0xC0000024: "object_type_mismatch",  # STATUS_OBJECT_TYPE_MISMATCH
    0xC00000BB: "unsupported_request",  # STATUS_NOT_SUPPORTED
    0xC0000002: "unsupported_request",  # STATUS_NOT_IMPLEMENTED
    0xC0000010: "unsupported_request",  # STATUS_INVALID_DEVICE_REQUEST
    0xC0000257: "dfs_referral_required",  # STATUS_PATH_NOT_COVERED
    0xC000000D: "invalid_request",  # STATUS_INVALID_PARAMETER
    0xC000007F: "capacity_constraint",  # STATUS_DISK_FULL
    0xC0000044: "capacity_constraint",  # STATUS_QUOTA_EXCEEDED
    0xC00000A3: "storage_unavailable",  # STATUS_DEVICE_NOT_READY
    0xC00000C9: "transport_failure",  # STATUS_NETWORK_NAME_DELETED
    0xC00000B5: "transport_failure",  # STATUS_IO_TIMEOUT
    0xC00000C4: "transport_failure",  # STATUS_UNEXPECTED_NETWORK_ERROR
    0xC000020C: "transport_failure",  # STATUS_CONNECTION_DISCONNECTED
    0xC000020D: "transport_failure",  # STATUS_CONNECTION_RESET
    0xC0000241: "transport_failure",  # STATUS_CONNECTION_ABORTED
    0xC000035C: "transport_failure",  # STATUS_NETWORK_SESSION_EXPIRED
    0xC0000203: "transport_failure",  # STATUS_USER_SESSION_DELETED
}
SMB_TRANSPORT_STATUS_CODES = frozenset(
    status_code for status_code, reason_code in SMB_STATUS_REASONS.items() if reason_code == "transport_failure"
)
SMB_ROOT_PATH_FALLBACK_STATUS_CODES = frozenset(
    {
        0xC000000D,  # STATUS_INVALID_PARAMETER
        0xC0000010,  # STATUS_INVALID_DEVICE_REQUEST
        0xC0000034,  # STATUS_OBJECT_NAME_NOT_FOUND
        0xC000003A,  # STATUS_OBJECT_PATH_NOT_FOUND
        0xC000003B,  # STATUS_OBJECT_PATH_SYNTAX_BAD
        0xC00000BB,  # STATUS_NOT_SUPPORTED
    }
)
SMB_PERMISSION_SEMANTICS = "smb_windows_acl_v1"
SMB_PERMISSION_SURFACE = "smb_filesystem_dacl"
SMB_PERMISSION_METHOD = "smb_query_security_info_read_control"
SMB_PERMISSION_DEFAULT_SAMPLE_LIMIT = 2
SMB_PERMISSION_MAX_SAMPLE_LIMIT = 10
SMB_PERMISSION_MAX_DESCRIPTOR_BYTES = 65_535
SMB_PERMISSION_MAX_ACES_PER_DESCRIPTOR = 64
SMB_PERMISSION_MAX_ACE_HEADERS = 4_096
SMB_PERMISSION_MAX_ENTRIES_PER_SHARE = 256
SMB_PERMISSION_MAX_LIMITATIONS = 8
SMB_PERMISSION_MAX_ERRORS = 8
SMB_PERMISSION_REQUESTED_INFORMATION = (
    OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
)
SMB_SECURITY_DESCRIPTOR_SELF_RELATIVE = 0x8000
SMB_SECURITY_DESCRIPTOR_DACL_PRESENT = 0x0004
SMB_SECURITY_DESCRIPTOR_DACL_DEFAULTED = 0x0008
SMB_SECURITY_DESCRIPTOR_DACL_AUTO_INHERITED = 0x0400
SMB_SECURITY_DESCRIPTOR_DACL_PROTECTED = 0x1000
SMB_SECURITY_DESCRIPTOR_CONTROL_FLAGS = {
    0x0001: "owner_defaulted",
    0x0002: "group_defaulted",
    SMB_SECURITY_DESCRIPTOR_DACL_PRESENT: "dacl_present",
    SMB_SECURITY_DESCRIPTOR_DACL_DEFAULTED: "dacl_defaulted",
    0x0100: "dacl_auto_inherit_required",
    SMB_SECURITY_DESCRIPTOR_DACL_AUTO_INHERITED: "dacl_auto_inherited",
    SMB_SECURITY_DESCRIPTOR_DACL_PROTECTED: "dacl_protected",
    SMB_SECURITY_DESCRIPTOR_SELF_RELATIVE: "self_relative",
}
SMB_SECURITY_DESCRIPTOR_RETAINED_CONTROL_MASK = sum(SMB_SECURITY_DESCRIPTOR_CONTROL_FLAGS)
SMB_ACE_FLAG_NAMES = {
    0x01: "object_inherit",
    0x02: "container_inherit",
    0x04: "no_propagate",
    0x08: "inherit_only",
    0x10: "inherited",
    0x40: "successful_access",
    0x80: "failed_access",
}
SMB_ACE_TYPES = {
    0x00: ("access_allowed", "allow", "simple"),
    0x01: ("access_denied", "deny", "simple"),
    0x02: ("system_audit", "audit", "simple"),
    0x03: ("system_alarm", "unknown", "simple"),
    0x04: ("access_allowed_compound", "allow", "compound"),
    0x05: ("access_allowed_object", "allow", "object"),
    0x06: ("access_denied_object", "deny", "object"),
    0x07: ("system_audit_object", "audit", "object"),
    0x08: ("system_alarm_object", "unknown", "object"),
    0x09: ("access_allowed_callback", "allow", "callback"),
    0x0A: ("access_denied_callback", "deny", "callback"),
    0x0B: ("access_allowed_callback_object", "allow", "callback_object"),
    0x0C: ("access_denied_callback_object", "deny", "callback_object"),
    0x0D: ("system_audit_callback", "audit", "callback"),
    0x0E: ("system_alarm_callback", "unknown", "callback"),
    0x0F: ("system_audit_callback_object", "audit", "callback_object"),
    0x10: ("system_alarm_callback_object", "unknown", "callback_object"),
    0x11: ("system_mandatory_label", "label", "simple"),
    0x12: ("system_resource_attribute", "label", "callback"),
    0x13: ("system_scoped_policy_id", "label", "simple"),
    0x14: ("system_process_trust_label", "label", "simple"),
    0x15: ("system_access_filter", "label", "callback"),
}
SMB_FILE_RIGHTS = (
    (0x00000001, "read_data_or_list_directory"),
    (0x00000002, "write_data_or_add_file"),
    (0x00000004, "append_data_or_add_subdirectory"),
    (0x00000008, "read_extended_attributes"),
    (0x00000010, "write_extended_attributes"),
    (0x00000020, "execute_or_traverse"),
    (0x00000040, "delete_child"),
    (0x00000080, "read_attributes"),
    (0x00000100, "write_attributes"),
    (0x00010000, "delete"),
    (0x00020000, "read_control"),
    (0x00040000, "write_dacl"),
    (0x00080000, "write_owner"),
    (0x00100000, "synchronize"),
    (0x01000000, "access_system_security"),
    (0x02000000, "maximum_allowed"),
    (0x10000000, "generic_all"),
    (0x20000000, "generic_execute"),
    (0x40000000, "generic_write"),
    (0x80000000, "generic_read"),
)
SMB_WELL_KNOWN_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-3-0": "Creator Owner",
    "S-1-3-1": "Creator Group",
    "S-1-5-7": "Anonymous Logon",
    "S-1-5-10": "Self",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-18": "Local System",
    "S-1-5-32-544": "Administrators",
    "S-1-5-32-545": "Users",
}


def _fsync_directory(directory: Path) -> None:
    """Make a completed atomic replace durable before callers advance state."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(os.fspath(directory), flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@dataclass
class Stats:
    endpoints: int = 0
    resources: int = 0
    items: int = 0
    permission_assessments: int = 0
    permission_entries: int = 0
    errors: int = 0
    error_codes: collections.Counter[str] = field(default_factory=collections.Counter)
    error_samples: dict[str, str] = field(default_factory=dict)
    structural_coverage_gaps: int = 0


@dataclass(frozen=True)
class ScanOutcome:
    targets_submitted: int
    targets_completed: int
    host_failures: int
    interrupted: bool = False
    targets_cancelled: int = 0


@dataclass
class _SMBProbeCircuit:
    """Share-scoped circuit breaker for explicit handle probes."""

    transport_failed: bool = False
    probes_aborted: bool = False
    reason_code: str | None = None
    root_path: str | None = None

    @property
    def blocked(self) -> bool:
        return self.transport_failed or self.probes_aborted


@dataclass(frozen=True)
class _SMBProbeClassification:
    outcome: str
    reason_code: str
    protocol_status: str | None = None
    transport_fatal: bool = False
    abort_remaining_probes: bool = False
    root_path_fallback: bool = False


class _ScanCancelled:
    """Sentinel returned when a submitted target stops before completing its scope."""


SCAN_CANCELLED = _ScanCancelled()


class ProgressReporter:
    """Thread-safe, stderr-only scan status suitable for humans and automation."""

    def __init__(
        self,
        *,
        total_targets: int | None,
        stats: Stats,
        stats_lock: threading.Lock,
        quiet: bool = False,
        verbosity: int = 0,
        interval_seconds: float = 5.0,
        stream=None,
    ) -> None:
        self.total_targets = total_targets
        self.stats = stats
        self.stats_lock = stats_lock
        self.quiet = quiet
        self.verbosity = verbosity
        self.interval_seconds = interval_seconds
        self.stream = stream if stream is not None else sys.stderr
        self.started_monotonic = time.monotonic()
        self._state_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._submitted = 0
        self._started = 0
        self._completed = 0
        self._succeeded = 0
        self._failed = 0
        self._cancelled = 0
        self._active_hosts: set[str] = set()
        self._completed_without_start = 0
        self._cancelled_without_start = 0

    def start(self, *, workers: int, share_types: list[str]) -> None:
        if self.quiet:
            return
        total = str(self.total_targets) if self.total_targets is not None else "unknown"
        self._write(f"scan started: targets={total} workers={workers} protocols={','.join(share_types)}")
        if self.interval_seconds > 0:
            self._thread = threading.Thread(
                target=self._periodic_loop,
                name="collector-progress",
                daemon=True,
            )
            try:
                self._thread.start()
            except (OSError, RuntimeError) as exc:
                self._thread = None
                self._write(
                    "progress warning: periodic reporter could not start; "
                    f"continuing without periodic updates ({_error_detail(exc)})"
                )

    def target_submitted(self) -> None:
        with self._state_lock:
            self._submitted += 1

    def target_started(self, host: str) -> None:
        with self._state_lock:
            if host not in self._active_hosts:
                self._started += 1
                self._active_hosts.add(host)

    def target_completed(self, host: str, *, succeeded: bool) -> None:
        with self._state_lock:
            self._completed += 1
            if host in self._active_hosts:
                self._active_hosts.discard(host)
            else:
                self._completed_without_start += 1
            if succeeded:
                self._succeeded += 1
            else:
                self._failed += 1
        if self.verbosity >= 1 and not self.quiet:
            self._write_progress(prefix=f"host {host}: {'ok' if succeeded else 'failed'}")

    def target_cancelled(self, host: str) -> None:
        with self._state_lock:
            self._cancelled += 1
            if host in self._active_hosts:
                self._active_hosts.discard(host)
            else:
                self._cancelled_without_start += 1
        if self.verbosity >= 1 and not self.quiet:
            self._write_progress(prefix=f"host {host}: cancelled")

    def detail(self, message: str, *, level: int = 2) -> None:
        if not self.quiet and self.verbosity >= level:
            self._write(message)

    def interruption_requested(self) -> None:
        if not self.quiet:
            self._write("interrupt received: stopping new work and draining in-flight network operations")

    def finish(
        self,
        *,
        status: str,
        artifact: str | None = None,
        upload_status: str = "not requested",
    ) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, min(self.interval_seconds + 0.1, 1.0)))
        if self.quiet:
            if status in {"partial", "failure", "interrupted"}:
                self._write_quiet_failure_summary(status)
            return
        self._write_progress(prefix=f"collector finished ({status})")
        artifact_label = artifact or "not written"
        self._write(f"result: artifact={artifact_label} upload={upload_status}")
        with self.stats_lock:
            error_codes = list(self.stats.error_codes.most_common(5))
            error_samples = dict(self.stats.error_samples)
        if error_codes:
            self._write("issue summary:")
            for code, count in error_codes:
                sample = error_samples.get(code, "")
                suffix = f": {sample}" if sample else ""
                self._write(f"  - {code} ({count}){suffix}")

    def _write_quiet_failure_summary(self, status: str) -> None:
        with self._state_lock:
            failed = self._failed
            cancelled = self._cancelled
        with self.stats_lock:
            error_codes = list(self.stats.error_codes.most_common(5))
        issue_label = ",".join(f"{code}={count}" for code, count in error_codes) or "none"
        self._write(
            f"collector finished ({status}): failed_targets={failed} cancelled_targets={cancelled} issues={issue_label}"
        )

    def _periodic_loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._write_progress(prefix="progress")

    def _write_progress(self, *, prefix: str) -> None:
        with self._state_lock:
            submitted = self._submitted
            started = self._started
            completed = self._completed
            succeeded = self._succeeded
            failed = self._failed
            cancelled = self._cancelled
            active = len(self._active_hosts)
            completed_without_start = self._completed_without_start
            cancelled_without_start = self._cancelled_without_start
        with self.stats_lock:
            endpoints = self.stats.endpoints
            resources = self.stats.resources
            items = self.stats.items
            errors = self.stats.errors
        elapsed = max(0.0, time.monotonic() - self.started_monotonic)
        rate = completed / elapsed if elapsed > 0 else 0.0
        if self.total_targets is None:
            discovered = submitted
            remaining = "unknown"
        else:
            discovered = self.total_targets
            remaining = str(max(0, self.total_targets - completed))
        pending = max(
            0,
            submitted - started - completed_without_start - cancelled_without_start,
        )
        self._write(
            f"{prefix}: discovered={discovered} submitted={submitted} processed={completed} "
            f"active={active} pending={pending} remaining={remaining} "
            f"succeeded={succeeded} failed={failed} cancelled={cancelled} endpoints={endpoints} "
            f"resources={resources} items={items} issues={errors} "
            f"elapsed={_format_duration(elapsed)} rate={rate:.1f}/s"
        )

    def _write(self, message: str) -> None:
        with self._output_lock:
            print(message, file=self.stream, flush=True)


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes:d}m{secs:02d}s"
    return f"{secs:d}s"


def _artifact_format_for_path(path: str | None) -> str:
    """Select the on-disk contract without inspecting or buffering its contents."""

    if path is None:
        return ARTIFACT_FORMAT_NDJSON
    normalized = str(path).lower()
    if normalized.endswith((".ndjson", ".ndjson.gz", ".jsonl", ".jsonl.gz")):
        return ARTIFACT_FORMAT_NDJSON
    return ARTIFACT_FORMAT_COMPACT_JSON


def _artifact_upload_filename(path: str) -> str:
    """Return an ASCII header value while preserving the format-defining suffix."""

    basename = os.path.basename(path)
    lowered = basename.lower()
    suffix = next(
        (
            candidate
            for candidate in sorted(SUPPORTED_ARTIFACT_SUFFIXES, key=len, reverse=True)
            if lowered.endswith(candidate)
        ),
        ".json",
    )
    stem = basename[: -len(suffix)] if basename.lower().endswith(suffix) else basename
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip(".") or "artifact"
    return f"{safe_stem[: 255 - len(suffix)]}{suffix}"


class CompactArtifactTooLargeError(ValueError):
    """Raised before compact output assembly could exceed its memory budget."""


class ArtifactSpoolLimitError(RuntimeError):
    """Raised when streaming output reaches the configured local disk budget."""


class NDJSONWriter:
    def __init__(
        self,
        path: str | None,
        gzip_output: bool,
        max_spool_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ):
        self._lock = threading.Lock()
        self._path = path
        self._gzip = bool(gzip_output and path is not None)
        self._artifact_format = _artifact_format_for_path(path)
        self._closed = False
        buffer_parent = str(Path(path).expanduser().parent) if path is not None else None
        self._buffer_dir = tempfile.mkdtemp(
            prefix=".share-sentinel-buffer-",
            dir=buffer_parent,
        )
        self._endpoint_paths: dict[str, str] = {}
        self._run_meta: dict[str, object] | None = None
        self._run_end: dict[str, object] | None = None
        self._issues: dict[str, dict[str, object]] = {}
        self._spool_error: BaseException | None = None
        self._max_spool_bytes = max(0, int(max_spool_bytes))
        self._spool_bytes = 0
        self._run_meta_bytes = 0
        self._run_end_bytes = 0
        self._ndjson_spool_path: str | None = None
        self._ndjson_spool_fp = None
        if self._artifact_format == ARTIFACT_FORMAT_NDJSON:
            self._ndjson_spool_path = os.path.join(self._buffer_dir, "records.ndjson")
            try:
                self._ndjson_spool_fp = open(self._ndjson_spool_path, "w", encoding="utf-8")
            except OSError:
                shutil.rmtree(self._buffer_dir, ignore_errors=True)
                raise

    def emit(self, record: dict) -> None:
        with self._lock:
            if self._closed or self._spool_error is not None:
                return
            rec_type = str(record.get("type") or "")
            if rec_type == "run_meta":
                serialized_bytes = self._serialized_line_size(record)
                try:
                    self._check_spool_limit(serialized_bytes - self._run_meta_bytes)
                except ArtifactSpoolLimitError as exc:
                    self._spool_error = exc
                    raise
                self._run_meta = dict(record)
                self._run_meta_bytes = serialized_bytes
                return
            if rec_type == "run_end":
                serialized_bytes = self._serialized_line_size(record)
                try:
                    self._check_spool_limit(serialized_bytes - self._run_end_bytes)
                except ArtifactSpoolLimitError as exc:
                    self._spool_error = exc
                    raise
                self._run_end = dict(record)
                self._run_end_bytes = serialized_bytes
                return
            if self._artifact_format == ARTIFACT_FORMAT_NDJSON:
                if rec_type in {
                    "endpoint",
                    "resource",
                    "item",
                    "error",
                    "permission_assessment",
                    "permission_entry",
                }:
                    try:
                        serialized = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
                        serialized_bytes = len(serialized.encode("utf-8"))
                        self._check_spool_limit(serialized_bytes)
                        self._ndjson_spool_fp.write(serialized)
                        self._spool_bytes += serialized_bytes
                    except (OSError, ArtifactSpoolLimitError) as exc:
                        self._spool_error = exc
                        raise
                return
            if rec_type == "error":
                self._record_issue(record)
                return
            if rec_type not in {
                "endpoint",
                "resource",
                "item",
                "permission_assessment",
                "permission_entry",
            }:
                return

            endpoint_key = str(record.get("endpoint_key") or "").strip()
            if not endpoint_key:
                return

            endpoint_path = self._endpoint_paths.get(endpoint_key)
            if endpoint_path is None:
                endpoint_path = os.path.join(self._buffer_dir, f"{uuid.uuid4().hex}.jsonl")
                self._endpoint_paths[endpoint_key] = endpoint_path
            try:
                with open(endpoint_path, "a", encoding="utf-8") as endpoint_fp:
                    endpoint_fp.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
            except OSError as exc:
                self._spool_error = exc
                raise

    @property
    def write_failed(self) -> bool:
        with self._lock:
            return self._spool_error is not None

    def close(self, keep_output: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

        cleanup_buffers = True
        try:
            if self._ndjson_spool_fp is not None:
                self._ndjson_spool_fp.flush()
                self._ndjson_spool_fp.close()
                self._ndjson_spool_fp = None
            if self._spool_error is not None:
                if isinstance(self._spool_error, ArtifactSpoolLimitError):
                    raise self._spool_error
                raise OSError(f"collector buffer write failed: {self._spool_error}") from self._spool_error
            if not keep_output:
                return
            if self._artifact_format == ARTIFACT_FORMAT_COMPACT_JSON:
                self._validate_compact_output_bounds()
            if self._path is None:
                self._write_payload(sys.stdout)
                sys.stdout.flush()
                return
            self._write_file_atomically()
        except KeyboardInterrupt:
            with self._lock:
                self._closed = False
            cleanup_buffers = False
            raise
        finally:
            if self._ndjson_spool_fp is not None:
                try:
                    self._ndjson_spool_fp.close()
                except OSError:
                    pass
                self._ndjson_spool_fp = None
            if cleanup_buffers:
                shutil.rmtree(self._buffer_dir, ignore_errors=True)

    def _write_file_atomically(self) -> None:
        destination = Path(str(self._path)).expanduser()
        fd, temporary_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        os.close(fd)
        committed = False
        try:
            if self._gzip:
                with gzip.open(temporary_path, "wt", encoding="utf-8") as target_fp:
                    self._write_payload(target_fp)
                # Windows maps fsync to _commit, which requires a writable
                # descriptor even though the gzip stream is already closed.
                with open(temporary_path, "r+b") as target_fp:
                    os.fsync(target_fp.fileno())
            else:
                with open(temporary_path, "w", encoding="utf-8") as target_fp:
                    self._write_payload(target_fp)
                    target_fp.flush()
                    os.fsync(target_fp.fileno())
            os.replace(temporary_path, destination)
            _fsync_directory(destination.parent)
            committed = True
        finally:
            if not committed:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def _write_payload(self, target_fp) -> None:
        if self._artifact_format == ARTIFACT_FORMAT_NDJSON:
            self._write_ndjson(target_fp)
            return
        self._write_document(target_fp)

    @staticmethod
    def _serialized_size(value: object) -> int:
        return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))

    @classmethod
    def _serialized_line_size(cls, value: object) -> int:
        return cls._serialized_size(value) + 1

    def _check_spool_limit(self, additional_bytes: int) -> None:
        if self._artifact_format != ARTIFACT_FORMAT_NDJSON or self._max_spool_bytes == 0:
            return
        projected = self._spool_bytes + self._run_meta_bytes + self._run_end_bytes + additional_bytes
        if projected > self._max_spool_bytes:
            raise ArtifactSpoolLimitError(
                "collector artifact exceeds --max-artifact-bytes "
                f"({self._max_spool_bytes} bytes); narrow the scan or raise the reviewed limit"
            )

    def _validate_compact_output_bounds(self) -> None:
        total_bytes = 0
        for endpoint_key, endpoint_path in self._endpoint_paths.items():
            endpoint_bytes = os.path.getsize(endpoint_path)
            if endpoint_bytes > COMPACT_JSON_MAX_ENDPOINT_BUFFER_BYTES:
                raise CompactArtifactTooLargeError(
                    "compact JSON endpoint buffer exceeds the safe 8 MiB assembly limit "
                    f"for {endpoint_key}; write to .ndjson or .ndjson.gz for large scans"
                )
            total_bytes += endpoint_bytes

        total_bytes += self._serialized_size(self._run_meta or {})
        total_bytes += self._serialized_size(self._run_end or {})
        total_bytes += self._serialized_size(self._serialized_issues())
        if total_bytes > COMPACT_JSON_MAX_BUFFER_BYTES:
            raise CompactArtifactTooLargeError(
                "compact JSON buffers exceed the safe 40 MiB assembly limit; "
                "write to .ndjson or .ndjson.gz for large scans"
            )

    @staticmethod
    def _write_json_line(target_fp, record: dict[str, object]) -> None:
        target_fp.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
        target_fp.write("\n")

    def _write_ndjson(self, target_fp) -> None:
        if self._run_meta is not None:
            self._write_json_line(target_fp, self._run_meta)

        if self._ndjson_spool_path is not None:
            with open(self._ndjson_spool_path, "r", encoding="utf-8") as endpoint_fp:
                for raw_line in endpoint_fp:
                    target_fp.write(raw_line)

        if self._run_end is not None:
            self._write_json_line(target_fp, self._run_end)

    def _record_issue(self, record: dict[str, object]) -> None:
        code = str(record.get("code") or "UNKNOWN")
        severity = str(record.get("severity") or "error")
        key = f"{severity}:{code}"
        issue = self._issues.setdefault(
            key,
            {
                "severity": severity,
                "code": code,
                "count": 0,
                "sample_message": str(record.get("message") or ""),
                "sample_hint": str(record.get("hint") or "") or None,
            },
        )
        issue["count"] = int(issue.get("count", 0)) + 1
        if not issue.get("sample_message") and record.get("message"):
            issue["sample_message"] = str(record.get("message"))
        if not issue.get("sample_hint") and record.get("hint"):
            issue["sample_hint"] = str(record.get("hint"))

    def _build_endpoint_document(self, endpoint_path: str) -> dict[str, object] | None:
        endpoint: dict[str, object] | None = None
        share_states: dict[tuple[str, str], dict[str, object]] = {}
        share_order: list[tuple[str, str]] = []

        with open(endpoint_path, "r", encoding="utf-8") as endpoint_fp:
            for raw_line in endpoint_fp:
                line = raw_line.strip()
                if not line:
                    continue
                record = json.loads(line)
                rec_type = str(record.get("type") or "")
                if rec_type == "endpoint":
                    endpoint = {
                        "endpoint_key": record.get("endpoint_key"),
                        "ip": record.get("ip"),
                        "hostname": record.get("hostname"),
                        "domain": record.get("domain"),
                    }
                    if isinstance(record.get("auth"), dict):
                        endpoint["auth"] = record["auth"]
                    if isinstance(record.get("smb"), dict):
                        endpoint["smb"] = record["smb"]
                    if isinstance(record.get("nfs"), dict):
                        endpoint["nfs"] = record["nfs"]
                    for field_name in ("provider", "provider_endpoint_id", "metadata"):
                        if record.get(field_name) is not None:
                            endpoint[field_name] = record[field_name]
                    continue

                if rec_type == "resource":
                    share_name = str(record.get("name") or "").strip()
                    if not share_name:
                        continue
                    share_type = str(record.get("share_type") or "smb")
                    share_key = (share_name, share_type)
                    if share_key not in share_states:
                        share_doc = {
                            "name": share_name,
                            "share_type": share_type,
                            "resource_type": record.get("resource_type"),
                            "remark": record.get("remark"),
                            "access_level": record.get("access_level", "unknown"),
                            "entries": [],
                        }
                        if isinstance(record.get("access_capabilities"), dict):
                            share_doc["access_capabilities"] = record["access_capabilities"]
                        for field_name in (
                            "provider",
                            "provider_resource_id",
                            "web_url",
                            "metadata",
                            "permission_summary",
                        ):
                            if record.get(field_name) is not None:
                                share_doc[field_name] = record[field_name]
                        share_states[share_key] = {
                            "doc": share_doc,
                            "index": {},
                            "permission_index": {},
                        }
                        share_order.append(share_key)
                    else:
                        share_doc = share_states[share_key]["doc"]
                        for field_name in (
                            "resource_type",
                            "remark",
                            "access_level",
                            "access_capabilities",
                            "provider",
                            "provider_resource_id",
                            "web_url",
                            "metadata",
                            "permission_summary",
                        ):
                            if record.get(field_name) is not None:
                                share_doc[field_name] = record[field_name]
                    continue

                if rec_type == "item":
                    share_name = str(record.get("resource_name") or "").strip()
                    if not share_name:
                        continue
                    share_type = str(record.get("share_type") or "smb")
                    share_key = (share_name, share_type)
                    share_state = share_states.get(share_key)
                    if share_state is None:
                        share_doc = {
                            "name": share_name,
                            "share_type": share_type,
                            "resource_type": record.get("resource_type"),
                            "remark": None,
                            "access_level": "unknown",
                            "entries": [],
                        }
                        share_state = {
                            "doc": share_doc,
                            "index": {},
                            "permission_index": {},
                        }
                        share_states[share_key] = share_state
                        share_order.append(share_key)
                    self._insert_item(share_state, record)
                    continue

                if rec_type == "permission_assessment":
                    share_name = str(record.get("resource_name") or "").strip()
                    if not share_name:
                        continue
                    share_type = str(record.get("share_type") or record.get("provider") or "smb")
                    share_key = (share_name, share_type)
                    share_state = share_states.get(share_key)
                    if share_state is None:
                        share_doc = {
                            "name": share_name,
                            "share_type": share_type,
                            "resource_type": record.get("resource_type") or "smb_share",
                            "remark": None,
                            "access_level": "unknown",
                            "entries": [],
                        }
                        share_state = {
                            "doc": share_doc,
                            "index": {},
                            "permission_index": {},
                        }
                        share_states[share_key] = share_state
                        share_order.append(share_key)
                    assessment = {
                        key: value
                        for key, value in record.items()
                        if key
                        not in {
                            "type",
                            "run_id",
                            "endpoint_key",
                            "resource_name",
                            "resource_type",
                            "share_type",
                        }
                    }
                    assessment["entries"] = []
                    share_state["doc"].setdefault("permission_assessments", []).append(assessment)
                    assessment_key = str(record.get("assessment_key") or "")
                    if assessment_key:
                        share_state["permission_index"][assessment_key] = assessment
                    continue

                if rec_type == "permission_entry":
                    share_name = str(record.get("resource_name") or "").strip()
                    share_type = str(record.get("share_type") or record.get("provider") or "smb")
                    share_state = share_states.get((share_name, share_type))
                    if share_state is None:
                        continue
                    assessment_key = str(record.get("assessment_key") or "")
                    assessment = share_state["permission_index"].get(assessment_key)
                    if assessment is None:
                        # The stream contract requires assessment-before-entry.
                        # Ignore an orphan instead of fabricating assessment state.
                        continue
                    entry = {
                        key: value
                        for key, value in record.items()
                        if key
                        not in {
                            "type",
                            "run_id",
                            "endpoint_key",
                            "resource_name",
                            "resource_type",
                            "share_type",
                        }
                    }
                    assessment["entries"].append(entry)

        if endpoint is None:
            return None

        endpoint["shares"] = [share_states[key]["doc"] for key in share_order]
        return endpoint

    @staticmethod
    def _insert_item(share_state: dict[str, object], record: dict[str, object]) -> None:
        share_doc = share_state["doc"]
        index = share_state["index"]
        full_path = str(record.get("path") or "\\").replace("/", "\\").strip() or "\\"
        if not full_path.startswith("\\"):
            full_path = f"\\{full_path}"
        leaf_name = str(record.get("name") or "").strip()
        if not leaf_name:
            return

        parts = [part for part in full_path.split("\\") if part]
        parent_children = share_doc["entries"]
        parent_path = "\\"

        for offset, part in enumerate(parts):
            is_last = offset == len(parts) - 1
            current_path = parent_path if parent_path == "\\" else parent_path.rstrip("\\")
            candidate_full_path = f"{current_path}\\{part}" if current_path != "\\" else f"\\{part}"
            node = index.get(candidate_full_path)
            if node is None:
                node = {
                    "path": parent_path,
                    "name": part,
                    "is_dir": (not is_last) or bool(record.get("is_dir", False)),
                }
                if node["is_dir"]:
                    node["children"] = []
                parent_children.append(node)
                index[candidate_full_path] = node

            if not is_last:
                if not node.get("is_dir"):
                    node["is_dir"] = True
                    node["children"] = []
                parent_children = node.setdefault("children", [])
                parent_path = candidate_full_path
                continue

            if part == leaf_name:
                node["path"] = parent_path
                node["name"] = leaf_name
                node["is_dir"] = bool(record.get("is_dir", False))
                for metadata_field in (
                    "size_bytes",
                    "allocation_size_bytes",
                    "mtime",
                    "created_at",
                    "accessed_at",
                    "changed_at",
                    "file_attributes",
                    "provider",
                    "provider_item_id",
                    "web_url",
                    "mime_type",
                    "deleted_state",
                    "metadata",
                    "permission_summary",
                ):
                    if record.get(metadata_field) is not None:
                        node[metadata_field] = record[metadata_field]
                if node["is_dir"]:
                    node.setdefault("children", [])
                else:
                    node.pop("children", None)

    def _write_document(self, target_fp) -> None:
        run_meta = dict(self._run_meta or {})
        run_end = dict(self._run_end or {})
        summary = dict((run_end.get("stats") or {})) if isinstance(run_end.get("stats"), dict) else {}
        collection = dict(run_meta.get("collection") or {}) if isinstance(run_meta.get("collection"), dict) else {}
        collection_context = (
            dict(run_meta.get("collection_context") or {})
            if isinstance(run_meta.get("collection_context"), dict)
            else {}
        )
        meta = {
            "tool": run_meta.get("tool"),
            "tool_version": run_meta.get("tool_version"),
            "run_id": run_meta.get("run_id"),
            "started_at": run_meta.get("started_at"),
            "finished_at": run_end.get("finished_at"),
            "operator_label": run_meta.get("operator_label"),
            "auth": run_meta.get("auth"),
        }
        target_fp.write("{")
        target_fp.write(f'"schema_version":{json.dumps(run_meta.get("schema_version", 1))}')
        target_fp.write(',"artifact_features":')
        json.dump(run_meta.get("artifact_features") or [], target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(',"format":"share_sentinel_compact_json"')
        target_fp.write(',"meta":')
        json.dump(meta, target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(',"collection":')
        json.dump(collection, target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(',"collection_context":')
        json.dump(collection_context, target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(',"summary":')
        json.dump(summary, target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(',"issue_summary":')
        json.dump(self._serialized_issues(), target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(',"endpoints":[')

        wrote_endpoint = False
        for endpoint_key in sorted(self._endpoint_paths):
            endpoint_doc = self._build_endpoint_document(self._endpoint_paths[endpoint_key])
            if endpoint_doc is None:
                continue
            if wrote_endpoint:
                target_fp.write(",")
            json.dump(endpoint_doc, target_fp, ensure_ascii=True, separators=(",", ":"))
            wrote_endpoint = True

        target_fp.write("]}")

    def _serialized_issues(self) -> list[dict[str, object]]:
        issues = list(self._issues.values())
        issues.sort(key=lambda issue: (-int(issue.get("count", 0)), str(issue.get("code") or "")))
        return issues


class _CollectorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Collect SMB and/or NFS share inventory and write a streaming NDJSON or compact JSON artifact.\n\n"
            "Workflow:\n"
            "  1) Select targets with --hosts and/or --cidr\n"
            "  2) Choose --share-types smb|nfs|both\n"
            "  3) Pick SMB auth mode if SMB is enabled\n"
            "  4) Save streaming NDJSON (.ndjson/.jsonl) or compatibility JSON (.json), then optionally upload"
        ),
        epilog=(
            "Examples:\n"
            "  Authenticated SMB scan:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types smb --domain CORP --username svc_scan --password '***' --output run.ndjson.gz --gzip\n\n"
            "  Domain-qualified username (quote backslashes, or use an unquoted forward slash):\n"
            "    share_sentinel_collector.py --hosts hosts.txt --username 'CORP\\svc_scan' --password '***' --output run.ndjson\n"
            "    share_sentinel_collector.py --hosts hosts.txt --username CORP/svc_scan --password '***' --output run.ndjson\n\n"
            "  Domain shell / ticket cache auth:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types smb --use-session-creds --output kerberos.ndjson\n\n"
            "  Anonymous SMB scan:\n"
            "    share_sentinel_collector.py --cidr 10.20.0.0/24 --share-types smb --smb-anonymous --output anon.ndjson\n\n"
            "  NFS + SMB combined scan:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types both --username corp\\\\svc_scan --password '***' --output combined.ndjson.gz --gzip\n\n"
            "  Upload after scan:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types smb --username svc --password '***' --upload --api-base https://api.example --project-id <uuid> --api-token <token>\n\n"
            "Notes:\n"
            "  - SMB authentication modes:\n"
            "      * NTLM: set --username (and password or hashes)\n"
            "      * Usernames: USER, DOMAIN\\USER, DOMAIN/USER, and USER@REALM are accepted\n"
            "      * In POSIX shells, quote DOMAIN\\USER; an unquoted single backslash is removed by the shell\n"
            "      * Kerberos: add --kerberos and use a domain-qualified username or --domain\n"
            "      * Session credentials: use --use-session-creds to use the active Kerberos ticket cache\n"
            "      * Anonymous: set --smb-anonymous or omit SMB credentials\n"
            "  - NFS uses a non-mutating v4 NULL probe plus `showmount -e`; neither operation proves filesystem access.\n"
            "  - Progress and diagnostics are written to stderr; NDJSON written to stdout remains clean.\n"
            "  - Ctrl-C drains bounded in-flight work, preserves a partial artifact, and exits 130.\n"
            "  - When no endpoint/resource/item/error data is collected, output files are not written."
        ),
        formatter_class=_CollectorHelpFormatter,
    )

    common = parser.add_argument_group("Common Options")
    common.add_argument("--hosts", type=str, help="Path to newline-separated hosts (IPs or hostnames).")
    common.add_argument("--cidr", action="append", default=[], help="Target CIDR range. Can be repeated.")
    common.add_argument(
        "--share-types",
        choices=["smb", "nfs", "both"],
        default="smb",
        help="Share protocols to scan.",
    )
    common.add_argument(
        "--output",
        type=str,
        help=(
            "Write streaming .ndjson/.jsonl or bounded compatibility .json output. "
            "Output is only committed when scan data is collected."
        ),
    )
    common.add_argument(
        "--gzip",
        action="store_true",
        help="Gzip-compress file output; requires a matching .gz output suffix.",
    )
    common.add_argument("--workers", type=int, default=100, help="Concurrent host workers.")
    common.add_argument("--timeout", type=float, default=3.0, help="Per-network-operation timeout in seconds.")
    common.add_argument(
        "--max-targets",
        type=int,
        default=65536,
        help="Fail before scanning more than this many unique targets (0 disables the guard).",
    )
    common.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
        help=(
            "Maximum uncompressed NDJSON bytes buffered locally before aborting safely "
            f"(default: {DEFAULT_MAX_ARTIFACT_BYTES}; 0 disables the guard)."
        ),
    )
    common.add_argument("--operator-label", type=str, help="Optional operator label stored in run metadata.")
    output_controls = common.add_mutually_exclusive_group()
    output_controls.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase stderr detail (-v reports each host; -vv also reports protocol/share activity).",
    )
    output_controls.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress routine progress and completion messages; errors still use stderr.",
    )
    common.add_argument(
        "--progress-interval",
        type=float,
        default=5.0,
        help="Seconds between stderr progress reports (0 disables periodic reports).",
    )

    smb_auth = parser.add_argument_group("SMB Authentication")
    smb_auth.add_argument("--smb-anonymous", action="store_true", help="Force anonymous SMB session.")
    smb_auth.add_argument(
        "--username",
        type=str,
        default="",
        help="SMB identity as USER, DOMAIN\\USER, DOMAIN/USER, or USER@REALM.",
    )
    smb_auth.add_argument(
        "--password",
        type=str,
        default=os.getenv(SMB_PASSWORD_ENV, ""),
        help=f"SMB password for NTLM/Kerberos (prefer {SMB_PASSWORD_ENV}).",
    )
    smb_auth.add_argument(
        "--hashes",
        type=str,
        default=os.getenv(SMB_HASHES_ENV),
        help=f"LM:NT hash pair for NTLM auth (prefer {SMB_HASHES_ENV}).",
    )
    smb_auth.add_argument("--domain", type=str, default="", help="SMB domain for NTLM/Kerberos.")
    smb_auth.add_argument("--local-auth", action="store_true", help="Use local SAM auth (empty domain) for NTLM.")
    smb_auth.add_argument("--kerberos", action="store_true", help="Use Kerberos authentication for SMB.")
    smb_auth.add_argument("--ccache", type=str, help="Use Kerberos credential cache.")
    smb_auth.add_argument(
        "--use-session-creds",
        action="store_true",
        help="Use current Kerberos session credentials from KRB5CCNAME/default cache (implies --kerberos).",
    )

    tuning = parser.add_argument_group("Enumeration Tuning")
    tuning.add_argument("--max-depth", type=int, default=1, help="Max directory traversal depth per share.")
    tuning.add_argument("--max-entries-per-share", type=int, default=5000, help="Cap listed entries per share/export.")
    tuning.add_argument(
        "--access-probe-limit",
        type=int,
        default=3,
        help=(
            "Maximum discovered directories and files sampled per SMB share for non-mutating access probes "
            "(0 disables explicit handle probes; directory listing evidence is still recorded)."
        ),
    )
    tuning.add_argument(
        "--smb-permissions",
        choices=("none", "root", "sampled"),
        default="root",
        help=(
            "Collect non-mutating SMB owner/group/DACL evidence using READ_CONTROL: "
            "none disables it, root assesses only the share root (default), and sampled "
            "also assesses a deterministic bounded set of visible directories and files."
        ),
    )
    tuning.add_argument(
        "--smb-permission-sample-limit",
        type=int,
        default=SMB_PERMISSION_DEFAULT_SAMPLE_LIMIT,
        help=(
            "Deterministic directory and file samples per SMB share in sampled mode "
            f"(default: {SMB_PERMISSION_DEFAULT_SAMPLE_LIMIT}; maximum: {SMB_PERMISSION_MAX_SAMPLE_LIMIT})."
        ),
    )
    tuning.add_argument(
        "--include-share",
        action="append",
        default=[],
        help="Known SMB share name to scan. Can be repeated; skips share enumeration when provided.",
    )
    tuning.add_argument("--exclude-share", action="append", default=[], help="SMB share name to skip. Can be repeated.")
    tuning.add_argument("--exclude-path-regex", type=str, help="Regex for paths to exclude.")
    tuning.add_argument("--extensions-only", type=str, help="Comma-separated file extensions filter, e.g. .docx,.pdf")

    upload = parser.add_argument_group("Upload")
    upload.add_argument("--upload", action="store_true", help="Upload artifact to Share Sentinel API after scan.")
    upload.add_argument("--api-base", type=str, help="API base URL, e.g. https://api.example")
    upload.add_argument("--project-id", type=str, help="Destination project UUID.")
    upload.add_argument(
        "--api-token",
        type=str,
        default=os.getenv(API_TOKEN_ENV),
        help=f"API token with run write scope (prefer {API_TOKEN_ENV}).",
    )
    upload.add_argument("--run-name", type=str, default="Share Collector Run", help="Run name for uploaded scan.")
    upload.add_argument(
        "--upload-timeout",
        type=float,
        default=600.0,
        help="Timeout in seconds for each API create/upload attempt.",
    )
    upload.add_argument(
        "--upload-attempts",
        type=int,
        default=3,
        help="Maximum attempts for transient API/network failures.",
    )

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def _normalize_host_target(raw_host: str) -> tuple[str, str]:
    host = str(raw_host).strip()
    try:
        normalized = str(ipaddress.ip_address(host))
    except ValueError:
        return host, host.casefold()
    return normalized, normalized


def iter_targets(cidrs: list[str], host_inputs: list[str] | None = None):
    seen: set[str] = set()
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        for host in network.hosts():
            target = str(host)
            key = target
            if key in seen:
                continue
            seen.add(key)
            yield target

    for host in host_inputs or []:
        target, key = _normalize_host_target(host)
        if key in seen:
            continue
        seen.add(key)
        yield target


def _cidr_host_range(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> tuple[int, int]:
    start = int(network.network_address)
    end = int(network.broadcast_address)
    if network.version == 4 and network.prefixlen < 31:
        start += 1
        end -= 1
    elif network.version == 6 and network.prefixlen < 127:
        start += 1
    return start, end


def _merged_target_ranges(cidrs: list[str]) -> dict[int, list[tuple[int, int]]]:
    ranges: dict[int, list[tuple[int, int]]] = {4: [], 6: []}
    for raw_cidr in cidrs:
        network = ipaddress.ip_network(raw_cidr, strict=False)
        start, end = _cidr_host_range(network)
        if start <= end:
            ranges[network.version].append((start, end))

    merged: dict[int, list[tuple[int, int]]] = {4: [], 6: []}
    for version, version_ranges in ranges.items():
        for start, end in sorted(version_ranges):
            if not merged[version] or start > merged[version][-1][1] + 1:
                merged[version].append((start, end))
                continue
            previous_start, previous_end = merged[version][-1]
            merged[version][-1] = (previous_start, max(previous_end, end))
    return merged


def count_targets(cidrs: list[str], host_inputs: list[str] | None = None) -> int:
    """Count iter_targets output without expanding large CIDRs into memory."""

    merged = _merged_target_ranges(cidrs)
    total = sum(end - start + 1 for ranges in merged.values() for start, end in ranges)
    seen_host_keys: set[str] = set()
    for raw_host in host_inputs or []:
        target, key = _normalize_host_target(raw_host)
        if key in seen_host_keys:
            continue
        seen_host_keys.add(key)
        try:
            address = ipaddress.ip_address(target)
        except ValueError:
            total += 1
            continue
        address_value = int(address)
        if not any(start <= address_value <= end for start, end in merged[address.version]):
            total += 1
    return total


def parse_targets(cidrs: list[str], hosts_file: str | None) -> list[str]:
    return list(iter_targets(cidrs, parse_hosts_file(hosts_file)))


def parse_hosts_file(hosts_file: str | None, max_hosts: int | None = None) -> list[str]:
    if not hosts_file:
        return []
    hosts: list[str] = []
    seen: set[str] = set()
    with Path(hosts_file).open("r", encoding="utf-8-sig") as hosts_fp:
        line_number = 0
        while True:
            raw_line = hosts_fp.readline(HOST_INPUT_MAX_LINE_CHARACTERS + 2)
            if raw_line == "":
                break
            line_number += 1
            if len(raw_line.rstrip("\r\n")) > HOST_INPUT_MAX_LINE_CHARACTERS:
                raise ValueError(f"hosts file line {line_number} exceeds {HOST_INPUT_MAX_LINE_CHARACTERS} characters")
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "\x00" in line or any(character.isspace() for character in line):
                raise ValueError(f"hosts file line {line_number} is not a valid single host target")
            if len(line) > HOST_TARGET_MAX_CHARACTERS:
                raise ValueError(
                    f"hosts file line {line_number} exceeds the {HOST_TARGET_MAX_CHARACTERS}-character host limit"
                )
            target, key = _normalize_host_target(line)
            if key in seen:
                continue
            seen.add(key)
            hosts.append(target)
            if max_hosts is not None and max_hosts > 0 and len(hosts) > max_hosts:
                raise ValueError(f"hosts file contains more than the reviewed --max-targets limit ({max_hosts})")
    return hosts


def _redact_cli_arguments(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for raw_arg in argv:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue

        arg = str(raw_arg)
        flag, separator, value = arg.partition("=")
        if flag in SENSITIVE_ARGUMENT_FLAGS:
            if separator:
                redacted.append(f"{flag}=<redacted>")
            else:
                redacted.append(flag)
                skip_next = True
            continue

        redacted.append(arg)
    return redacted


def normalize_path(base: str, child: str) -> str:
    base = base.replace("/", "\\")
    child = child.replace("/", "\\")
    if base.endswith("\\"):
        joined = f"{base}{child}"
    elif base:
        joined = f"{base}\\{child}"
    else:
        joined = f"\\{child}"
    return joined if joined.startswith("\\") else f"\\{joined}"


def _new_access_capabilities() -> dict[str, dict[str, object]]:
    return {
        capability: {
            "status": "not_tested",
            "attempted": 0,
            "allowed": 0,
            "denied": 0,
            "inconclusive": 0,
        }
        for capability in SMB_CAPABILITY_NAMES
    }


def _capability_status(evidence: dict[str, object]) -> str:
    allowed = int(evidence.get("allowed", 0))
    denied = int(evidence.get("denied", 0))
    inconclusive = int(evidence.get("inconclusive", 0))
    if allowed and denied:
        return "mixed"
    if allowed:
        return "allowed"
    if denied:
        return "denied"
    if inconclusive:
        return "inconclusive"
    return "not_tested"


def _record_capability(
    capabilities: dict[str, dict[str, object]],
    capability: str,
    outcome: str,
    *,
    reason_code: str | None = None,
    protocol_status: str | None = None,
    method: str | None = None,
    scope: str | None = None,
) -> None:
    if capability not in capabilities or outcome not in SMB_CAPABILITY_OUTCOMES:
        return
    evidence = capabilities[capability]
    evidence["attempted"] = int(evidence.get("attempted", 0)) + 1
    evidence[outcome] = int(evidence.get(outcome, 0)) + 1
    evidence["status"] = _capability_status(evidence)

    for field_name, raw_value, mixed_value in (
        ("reason_code", reason_code, "multiple_outcomes"),
        ("protocol_status", protocol_status, "multiple"),
        ("method", method, "multiple"),
        ("scope", scope, "mixed_sample"),
    ):
        value = str(raw_value or "").strip()
        if not value:
            continue
        existing = str(evidence.get(field_name) or "").strip()
        if existing and existing != value:
            evidence[field_name] = mixed_value
        else:
            evidence[field_name] = value


def _mark_capability_not_tested(
    capabilities: dict[str, dict[str, object]],
    capability: str,
    reason_code: str,
) -> None:
    evidence = capabilities.get(capability)
    if evidence is None or int(evidence.get("attempted", 0)) > 0:
        return
    if not str(evidence.get("not_tested_reason") or "").strip():
        evidence["not_tested_reason"] = reason_code


def _access_capability_snapshot(
    capabilities: dict[str, dict[str, object]],
    *,
    probe_limit: int = 0,
    partial: bool = True,
    complete: bool = False,
    directory_samples: int = 0,
    file_samples: int = 0,
    directory_candidates_seen: int = 0,
    file_candidates_seen: int = 0,
    listing_truncated: bool = False,
    assessment_summary: str = "not_assessed",
    assessment_reason: str = "pending",
    finalized: bool = False,
    degraded: bool = False,
    transport_failed: bool = False,
    probes_aborted: bool = False,
    probe_abort_reason: str | None = None,
    share_presence: str = "unverified",
    assessed_identity_fingerprint: str | None = None,
    session_kind: str | None = None,
    identity_source: str | None = None,
) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for capability, evidence in capabilities.items():
        capability_snapshot: dict[str, object] = {
            "status": str(evidence.get("status") or "not_tested"),
            "attempted": int(evidence.get("attempted", 0)),
            "allowed": int(evidence.get("allowed", 0)),
            "denied": int(evidence.get("denied", 0)),
            "inconclusive": int(evidence.get("inconclusive", 0)),
        }
        for field_name in ("reason_code", "protocol_status", "not_tested_reason", "method", "scope"):
            value = str(evidence.get(field_name) or "").strip()
            if value:
                capability_snapshot[field_name] = value
        snapshot[capability] = capability_snapshot
    snapshot["_metadata"] = {
        "probe_method": "non_mutating_handle_open",
        "coverage": "bounded_sample" if probe_limit > 0 else "disabled",
        "probe_limit": max(0, probe_limit),
        "partial": bool(partial),
        "complete": bool(complete),
        "directory_samples": max(0, directory_samples),
        "file_samples": max(0, file_samples),
        "directory_candidates_seen": max(0, directory_candidates_seen),
        "file_candidates_seen": max(0, file_candidates_seen),
        "listing_truncated": bool(listing_truncated),
        "assessment_summary": assessment_summary,
        "assessment_reason": assessment_reason,
        "finalized": bool(finalized),
        "degraded": bool(degraded),
        "transport_failed": bool(transport_failed),
        "probes_aborted": bool(probes_aborted),
        "share_presence": share_presence,
    }
    if probe_abort_reason:
        snapshot["_metadata"]["probe_abort_reason"] = probe_abort_reason
    if assessed_identity_fingerprint:
        snapshot["_metadata"]["assessed_identity_fingerprint"] = assessed_identity_fingerprint
    if session_kind:
        snapshot["_metadata"]["session_kind"] = session_kind
    if identity_source:
        snapshot["_metadata"]["identity_source"] = identity_source
    return snapshot


def _legacy_access_level(capabilities: dict[str, dict[str, object]]) -> str:
    if int(capabilities["read_file"].get("allowed", 0)) > 0:
        return "readable"
    if int(capabilities["list"].get("allowed", 0)) > 0:
        return "list_only"

    any_allowed = any(int(capabilities[name].get("allowed", 0)) > 0 for name in SMB_CAPABILITY_NAMES)
    tree_connect_denied = int(capabilities["tree_connect"].get("denied", 0)) > 0
    if tree_connect_denied and not any_allowed:
        return "no_access"
    return "unknown"


def _capability_observed(capabilities: dict[str, dict[str, object]], capability: str) -> bool:
    evidence = capabilities.get(capability, {})
    return int(evidence.get("allowed", 0)) > 0 or evidence.get("status") in {"allowed", "mixed"}


def _capability_reason(capabilities: dict[str, dict[str, object]], capability: str) -> str | None:
    evidence = capabilities.get(capability, {})
    for field_name in ("reason_code", "not_tested_reason"):
        value = str(evidence.get(field_name) or "").strip()
        if value and value not in {"granted", "multiple_outcomes"}:
            return value
    return None


def _smb_access_assessment(
    capabilities: dict[str, dict[str, object]],
    *,
    probe_limit: int,
    workflow_finished: bool,
    interrupted: bool,
    workflow_reason: str | None,
    transport_failed: bool,
    listing_truncated: bool,
    file_samples: int,
) -> tuple[str, str, bool]:
    read_observed = _capability_observed(capabilities, "read_file")
    list_observed = _capability_observed(capabilities, "list")
    write_observed = any(
        _capability_observed(capabilities, capability)
        for capability in ("create_file", "create_directory", "modify_file", "delete")
    )
    control_observed = any(
        _capability_observed(capabilities, capability) for capability in ("write_acl", "write_owner")
    )
    tree_observed = _capability_observed(capabilities, "tree_connect")
    tree_denied = int(capabilities["tree_connect"].get("denied", 0)) > 0
    list_denied = int(capabilities["list"].get("denied", 0)) > 0
    any_inconclusive = any(
        int(capabilities[capability].get("inconclusive", 0)) > 0 for capability in SMB_CAPABILITY_NAMES
    )

    if read_observed and write_observed:
        summary = "read_write_observed"
    elif read_observed:
        summary = "read_observed"
    elif list_observed and write_observed:
        summary = "list_write_observed"
    elif write_observed:
        summary = "write_observed"
    elif list_observed:
        summary = "list_observed"
    elif control_observed:
        summary = "control_observed"
    elif tree_denied:
        summary = "tree_denied"
    elif tree_observed and list_denied:
        summary = "connected_list_denied"
    elif tree_observed:
        summary = "connected_only"
    elif any_inconclusive:
        summary = "inconclusive"
    else:
        summary = "not_assessed"

    if transport_failed:
        reason = (
            "partial_transport_failure"
            if any((read_observed, list_observed, write_observed, control_observed, tree_observed))
            else "transport_failure"
        )
    elif interrupted:
        reason = (
            "cancelled_after_observation"
            if any((read_observed, list_observed, write_observed, control_observed, tree_observed))
            else "cancelled"
        )
    elif workflow_reason and (not workflow_finished or any_inconclusive):
        reason = workflow_reason
    elif listing_truncated:
        reason = "listing_truncated"
    elif summary == "tree_denied":
        reason = _capability_reason(capabilities, "tree_connect") or "access_denied"
    elif summary == "connected_list_denied":
        reason = _capability_reason(capabilities, "list") or "access_denied"
    elif summary in {"connected_only", "inconclusive"}:
        reason = (
            _capability_reason(capabilities, "tree_connect")
            or _capability_reason(capabilities, "list")
            or "no_conclusive_evidence"
        )
    elif probe_limit <= 0 and not read_observed:
        reason = "probes_disabled"
    elif not read_observed and file_samples <= 0:
        reason = "no_visible_file_candidate"
    else:
        reason = "bounded_observation"

    degraded = bool(not workflow_finished or transport_failed or listing_truncated or any_inconclusive)
    return summary, reason, degraded


def _smb_share_presence(
    capabilities: dict[str, dict[str, object]],
    *,
    enumerated: bool,
) -> str:
    if _capability_observed(capabilities, "tree_connect") or _capability_observed(capabilities, "list"):
        return "confirmed"
    tree_reason = _capability_reason(capabilities, "tree_connect")
    if tree_reason == "share_unavailable":
        return "unavailable"
    if enumerated:
        return "advertised"
    if int(capabilities["tree_connect"].get("attempted", 0)) > 0:
        return "indeterminate"
    return "unverified"


def _smb_error_identity(exc: BaseException) -> tuple[int | None, tuple[int, int] | None]:
    status_code = None
    get_error_code = getattr(exc, "getErrorCode", None)
    if callable(get_error_code):
        try:
            status_code = int(get_error_code())
        except Exception:
            status_code = None
    legacy_error_pair: tuple[int, int] | None = None
    get_error_packet = getattr(exc, "getErrorPacket", None)
    if callable(get_error_packet):
        try:
            error_packet = get_error_packet()
        except Exception:
            error_packet = None
        if error_packet is not None:
            packet_flags2: int | None = None
            try:
                packet_flags2 = int(error_packet["Flags2"])  # type: ignore[index]
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                get_flags2 = getattr(error_packet, "get_flags2", None)
                if callable(get_flags2):
                    try:
                        packet_flags2 = int(get_flags2())
                    except Exception:
                        packet_flags2 = None

            # In SMB1's NT-status mode ErrorClass/_reserved/ErrorCode are the
            # byte layout of one 32-bit NTSTATUS, not a DOS class/code pair.
            # Impacket exposes both views on the same packet; never publish the
            # split bytes as fabricated legacy evidence.
            uses_nt_status = bool(packet_flags2 is not None and packet_flags2 & SMB1_FLAGS2_NT_STATUS) or bool(
                status_code is not None and status_code > 0xFFFF
            )
            if uses_nt_status:
                return status_code, None
            try:
                # Impacket's SMB1 NewSMBPacket is a Structure with mapping
                # fields, not the accessor methods exposed by SessionError.
                legacy_error_pair = (
                    int(error_packet["ErrorClass"]),  # type: ignore[index]
                    int(error_packet["ErrorCode"]),  # type: ignore[index]
                )
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                get_error_class = getattr(error_packet, "get_error_class", None)
                get_legacy_error_code = getattr(error_packet, "get_error_code", None)
                if callable(get_error_class) and callable(get_legacy_error_code):
                    try:
                        legacy_error_pair = (
                            int(get_error_class()),
                            int(get_legacy_error_code()),
                        )
                    except Exception:
                        legacy_error_pair = None
                else:
                    legacy_error_pair = None

    return status_code, legacy_error_pair


def _smb_protocol_status(
    status_code: int | None,
    legacy_error_pair: tuple[int, int] | None,
) -> str | None:
    if status_code is not None and status_code > 0xFFFF:
        return f"0x{status_code & 0xFFFFFFFF:08X}"
    if legacy_error_pair is not None:
        return f"SMB1:{legacy_error_pair[0]:02X}:{legacy_error_pair[1]:04X}"
    if status_code is not None:
        return f"0x{status_code & 0xFFFFFFFF:08X}"
    return None


def _classify_smb_probe_failure(exc: BaseException) -> _SMBProbeClassification:
    status_code, legacy_error_pair = _smb_error_identity(exc)
    protocol_status = _smb_protocol_status(status_code, legacy_error_pair)

    if isinstance(exc, (NetBIOSError, NetBIOSTimeout, socket.timeout, TimeoutError, OSError)) and not isinstance(
        exc, SessionError
    ):
        return _SMBProbeClassification(
            outcome="inconclusive",
            reason_code="transport_failure",
            protocol_status=protocol_status,
            transport_fatal=True,
        )

    if legacy_error_pair in SMB1_DENIED_ERROR_PAIRS:
        return _SMBProbeClassification(
            outcome="denied",
            reason_code="legacy_operation_refused",
            protocol_status=protocol_status,
        )

    if legacy_error_pair in SMB1_STATUS_REASONS:
        reason_code = SMB1_STATUS_REASONS[legacy_error_pair]
        return _SMBProbeClassification(
            outcome="inconclusive",
            reason_code=reason_code,
            protocol_status=protocol_status,
            abort_remaining_probes=reason_code == "tree_session_invalid",
            root_path_fallback=legacy_error_pair in SMB1_ROOT_PATH_FALLBACK_ERROR_PAIRS,
        )

    if status_code in SMB_DENIED_STATUS_CODES:
        return _SMBProbeClassification(
            outcome="denied",
            reason_code=SMB_STATUS_REASONS.get(status_code, "access_denied"),
            protocol_status=protocol_status,
        )

    if status_code in SMB_STATUS_REASONS:
        return _SMBProbeClassification(
            outcome="inconclusive",
            reason_code=SMB_STATUS_REASONS[status_code],
            protocol_status=protocol_status,
            transport_fatal=status_code in SMB_TRANSPORT_STATUS_CODES,
            root_path_fallback=status_code in SMB_ROOT_PATH_FALLBACK_STATUS_CODES,
        )

    detail = _error_detail(exc).upper()
    if any(label in detail for label in SMB_DENIED_STATUS_LABELS):
        return _SMBProbeClassification(
            outcome="denied",
            reason_code="access_denied",
            protocol_status=protocol_status,
        )
    return _SMBProbeClassification(
        outcome="inconclusive",
        reason_code="protocol_error",
        protocol_status=protocol_status,
    )


def _smb_probe_outcome(exc: BaseException) -> str:
    """Compatibility wrapper retained for callers and older focused tests."""

    return _classify_smb_probe_failure(exc).outcome


def _is_smb_transport_failure(exc: BaseException) -> bool:
    # Impacket reports server-side SMB status errors as SessionError. Its other
    # network exceptions mean the session itself is no longer reliable enough
    # to multiply the timeout across every remaining access-mask probe.
    return _classify_smb_probe_failure(exc).transport_fatal


def _smb_handle_path(path: str) -> str:
    return str(path or "").replace("/", "\\").strip("\\")


def _smb_dfs_namespace_evidence(
    conn: object,
    tree_id: object | None,
    *,
    referral_required_observed: bool = False,
    referral_protocol_status: str | None = None,
    observed_tree_connect_capability: str | None = None,
) -> dict[str, object]:
    """Describe bounded DFS evidence without resolving or following referrals.

    Impacket exposes the SMB2/3 tree-connect DFS capability only through its
    connection state. Treat that implementation detail as optional evidence:
    missing or malformed state remains indeterminate and never becomes a
    negative DFS conclusion.
    """

    tree_connect_capability = observed_tree_connect_capability or "unavailable"
    if tree_connect_capability not in {"unavailable", "malformed", "dfs", "non_dfs"}:
        tree_connect_capability = "malformed"
    if observed_tree_connect_capability is None and tree_id is not None:
        get_smb_server = getattr(conn, "getSMBServer", None)
        if callable(get_smb_server):
            try:
                smb_server = get_smb_server()
            except Exception:
                smb_server = None
            session = getattr(smb_server, "_Session", None)
            if isinstance(session, dict):
                tree_table = session.get("TreeConnectTable")
                if isinstance(tree_table, dict):
                    tree_entry = tree_table.get(tree_id)
                    if isinstance(tree_entry, dict):
                        is_dfs_share = tree_entry.get("IsDfsShare")
                        if isinstance(is_dfs_share, bool):
                            tree_connect_capability = "dfs" if is_dfs_share else "non_dfs"
                        else:
                            tree_connect_capability = "malformed"
                    elif tree_entry is not None:
                        tree_connect_capability = "malformed"
                elif tree_table is not None:
                    tree_connect_capability = "malformed"

    namespace_detected = tree_connect_capability == "dfs" or referral_required_observed
    if namespace_detected:
        namespace_status = "detected"
        coverage = "logical_namespace_only"
        physical_target_status = "not_resolved"
        target_following_reason = "disabled_no_referral_target_trust_policy"
        limitations = [
            "referral_targets_not_requested_or_followed",
            "physical_target_coverage_not_assessed",
        ]
    elif tree_connect_capability == "non_dfs":
        namespace_status = "not_detected"
        coverage = "connected_share_only"
        physical_target_status = "not_applicable"
        target_following_reason = "not_applicable_no_dfs_evidence"
        limitations = ["dfs_detection_limited_to_smb2_tree_connect_and_path_not_covered_evidence"]
    else:
        namespace_status = "indeterminate"
        coverage = "indeterminate"
        physical_target_status = "not_resolved"
        target_following_reason = "disabled_no_referral_target_trust_policy"
        limitations = [
            "smb2_tree_connect_dfs_capability_unavailable",
            "referral_targets_not_requested_or_followed",
        ]

    evidence: dict[str, object] = {
        "namespace_status": namespace_status,
        "tree_connect_capability": tree_connect_capability,
        "referral_status": "required_not_followed" if referral_required_observed else "not_observed",
        "referral_required_observed": referral_required_observed,
        "target_following": False,
        "target_following_reason": target_following_reason,
        "logical_resource_identity_preserved": True,
        "physical_target_status": physical_target_status,
        "coverage": coverage,
        "limitations": limitations,
    }
    if referral_required_observed and referral_protocol_status:
        evidence["referral_protocol_status"] = referral_protocol_status
    return evidence


def _normalized_identity_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "").strip()).casefold()


def _stable_fingerprint(namespace: str, *parts: object) -> str:
    payload = "\x00".join(_normalized_identity_text(part) for part in parts)
    return f"{namespace}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _stable_case_preserving_fingerprint(namespace: str, *parts: object) -> str:
    payload = "\x00".join(unicodedata.normalize("NFKC", str(part or "").strip()) for part in parts)
    return f"{namespace}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _smb_server_identity(conn: SMBConnection, target: str) -> dict[str, object]:
    """Return the strongest stable, non-secret server identity Impacket exposes."""

    low_level = None
    try:
        low_level = conn.getSMBServer()
    except Exception:
        pass

    server_guid: bytes | None = None
    connection_state = getattr(low_level, "_Connection", None)
    if connection_state is not None:
        try:
            candidate = connection_state["ServerGuid"]
        except (IndexError, KeyError, TypeError):
            candidate = None
        if isinstance(candidate, (bytes, bytearray, memoryview)) and any(bytes(candidate)):
            server_guid = bytes(candidate)

    if server_guid is not None:
        native_id = server_guid.hex()
        return {
            "provider_endpoint_id": _stable_fingerprint("smb-server-guid:v1", native_id),
            "identity_source": "server_guid",
            "identity_strength": "strong",
            "server_guid": native_id,
        }

    advertised_names: list[str] = []
    for owner in (conn, low_level):
        if owner is None:
            continue
        for method_name in ("getServerName", "getRemoteName"):
            method = getattr(owner, method_name, None)
            if not callable(method):
                continue
            try:
                value = str(method() or "").strip()
            except Exception:
                continue
            if value and _normalized_identity_text(value) not in {
                _normalized_identity_text(candidate) for candidate in advertised_names
            }:
                advertised_names.append(value)
    native_id = advertised_names[0] if advertised_names else target
    return {
        "provider_endpoint_id": _stable_fingerprint("smb-server-name:v1", native_id),
        "identity_source": "advertised_name" if advertised_names else "scan_target",
        "identity_strength": "moderate" if advertised_names else "weak",
        "advertised_names": advertised_names[:4],
    }


def _smb_assessed_identity(
    conn: SMBConnection,
    args: argparse.Namespace,
    attempted_auth: str,
) -> dict[str, str]:
    session_kind = attempted_auth
    is_guest = getattr(conn, "isGuestSession", None)
    if callable(is_guest):
        try:
            if bool(is_guest()):
                session_kind = "guest"
        except Exception:
            pass
    if session_kind in {"anonymous", "guest"}:
        native_identity = session_kind
    else:
        username = str(getattr(args, "username", "") or "")
        domain = str(getattr(args, "domain", "") or "")
        native_identity = f"{domain}\\{username}" if domain else username
    return {
        "assessed_identity_fingerprint": _stable_fingerprint("smb-session-identity:v1", session_kind, native_identity),
        "session_kind": session_kind,
        "identity_source": ("server_session" if session_kind != attempted_auth else "requested_identity"),
    }


@dataclass
class _SMBPermissionCandidateSelector:
    resource_id: str
    limit: int
    directories: dict[str, tuple[str, str]] = field(default_factory=dict)
    files: dict[str, tuple[str, str]] = field(default_factory=dict)

    def consider(self, path: str, is_directory: bool) -> None:
        if self.limit <= 0:
            return
        normalized_path = _smb_handle_path(path)
        if not normalized_path:
            return
        # Case-sensitive Samba namespaces can legally expose names that differ
        # only by case. Preserve case so deterministic sampling never merges
        # two distinct provider objects.
        canonical_path = unicodedata.normalize("NFKC", normalized_path.replace("/", "\\"))
        subject_kind = "directory" if is_directory else "file"
        score = hashlib.sha256(f"{self.resource_id}\x00{subject_kind}\x00{canonical_path}".encode("utf-8")).hexdigest()
        candidates = self.directories if is_directory else self.files
        candidates.setdefault(canonical_path, (score, normalized_path))
        if len(candidates) > self.limit:
            worst_key = max(candidates, key=lambda key: (candidates[key][0], key))
            del candidates[worst_key]

    def selected(self) -> list[tuple[str, bool]]:
        selected: list[tuple[str, bool, str]] = []
        selected.extend((path, True, score) for score, path in self.directories.values())
        selected.extend((path, False, score) for score, path in self.files.values())
        return [(path, is_directory) for path, is_directory, _ in sorted(selected, key=lambda row: (row[2], row[0]))]


def _parse_smb_sid(data: bytes, offset: int, end: int) -> tuple[dict[str, object], int]:
    if offset < 0 or end > len(data) or offset + 8 > end:
        raise ValueError("SID header is outside the bounded record")
    revision = data[offset]
    subauthority_count = data[offset + 1]
    if subauthority_count > 15:
        raise ValueError("SID sub-authority count exceeds the Windows limit")
    sid_size = 8 + (subauthority_count * 4)
    if offset + sid_size > end:
        raise ValueError("SID extends beyond the bounded record")
    identifier_authority = int.from_bytes(data[offset + 2 : offset + 8], "big")
    subauthorities = [
        struct.unpack_from("<L", data, offset + 8 + (index * 4))[0] for index in range(subauthority_count)
    ]
    native_id = f"S-{revision}-{identifier_authority}"
    if subauthorities:
        native_id += "-" + "-".join(str(value) for value in subauthorities)
    principal_key = _stable_fingerprint("smb-principal:v1", native_id)
    known_name = SMB_WELL_KNOWN_SIDS.get(native_id)
    principal: dict[str, object] = {
        "provider": "smb",
        "identifier_namespace": "windows_sid",
        "principal_key": principal_key,
        "kind": "well_known" if known_name else "unknown",
        "native_id": native_id,
        "authority": "windows_sid",
        "resolution": "well_known" if known_name else "unresolved_sid",
    }
    if known_name:
        principal["display_name"] = known_name
        principal["aliases"] = [known_name]
    return principal, sid_size


def _decoded_smb_rights(mask: int) -> list[str]:
    return [name for bit, name in SMB_FILE_RIGHTS if mask & bit]


def _smb_ace_sid_offset(ace: bytes, layout: str) -> tuple[int | None, dict[str, object]]:
    details: dict[str, object] = {}
    if layout in {"simple", "callback"}:
        return 8, details
    if layout == "compound":
        if len(ace) < 12:
            raise ValueError("compound ACE header is truncated")
        compound_type, reserved = struct.unpack_from("<HH", ace, 8)
        details.update({"compound_type": compound_type, "compound_reserved": reserved})
        return 12, details
    if layout in {"object", "callback_object"}:
        if len(ace) < 12:
            raise ValueError("object ACE header is truncated")
        object_flags = struct.unpack_from("<L", ace, 8)[0]
        details["object_flags"] = object_flags
        sid_offset = 12
        if object_flags & 0x1:
            if sid_offset + 16 > len(ace):
                raise ValueError("object ACE object GUID is truncated")
            details["object_type_guid"] = str(uuid.UUID(bytes_le=ace[sid_offset : sid_offset + 16]))
            sid_offset += 16
        if object_flags & 0x2:
            if sid_offset + 16 > len(ace):
                raise ValueError("object ACE inherited GUID is truncated")
            details["inherited_object_type_guid"] = str(uuid.UUID(bytes_le=ace[sid_offset : sid_offset + 16]))
            sid_offset += 16
        return sid_offset, details
    return None, details


def _parse_smb_security_descriptor(raw_descriptor: object) -> dict[str, object]:
    """Parse a bounded self-relative SD without retaining raw or SACL data."""

    try:
        descriptor = bytes(raw_descriptor)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("security descriptor response is not bytes") from exc
    if len(descriptor) < 20:
        raise ValueError("security descriptor header is truncated")
    if len(descriptor) > SMB_PERMISSION_MAX_DESCRIPTOR_BYTES:
        raise ValueError("security descriptor exceeds the reviewed byte limit")

    revision, reserved, control, owner_offset, group_offset, _sacl_offset, dacl_offset = struct.unpack_from(
        "<BBHLLLL", descriptor, 0
    )
    if not control & SMB_SECURITY_DESCRIPTOR_SELF_RELATIVE:
        raise ValueError("security descriptor is not self-relative")
    details: dict[str, object] = {
        "descriptor_revision": revision,
        "descriptor_control_retained": control & SMB_SECURITY_DESCRIPTOR_RETAINED_CONTROL_MASK,
        "descriptor_control_flags": [
            label for bit, label in SMB_SECURITY_DESCRIPTOR_CONTROL_FLAGS.items() if control & bit
        ],
        "descriptor_size": len(descriptor),
        "reserved_byte": reserved,
        "sacl_requested": False,
        "sacl_retained": False,
    }
    limitations: list[str] = [
        "raw_security_descriptor_not_retained",
        "sacl_not_requested",
        "effective_access_not_computed",
        "sid_names_not_resolved_except_well_known",
    ]
    errors: list[dict[str, str]] = []
    unknown_entries = 0
    if revision != 1:
        errors.append({"code": "unsupported_descriptor_revision", "message": f"revision {revision} is unsupported"})
    if reserved != 0:
        errors.append({"code": "invalid_descriptor_reserved_byte", "message": "reserved descriptor byte is non-zero"})

    for label, offset in (("owner", owner_offset), ("group", group_offset)):
        if offset == 0:
            details[f"{label}_state"] = "not_returned"
            continue
        try:
            principal, _ = _parse_smb_sid(descriptor, offset, len(descriptor))
        except ValueError as exc:
            details[f"{label}_state"] = "malformed"
            errors.append({"code": f"malformed_{label}_sid", "message": str(exc)})
        else:
            details[label] = principal
            details[f"{label}_state"] = "observed"

    dacl_present = bool(control & SMB_SECURITY_DESCRIPTOR_DACL_PRESENT)
    entries: list[dict[str, object]] = []
    entry_set_hasher = hashlib.sha256()
    entries_parsed = 0
    entries_expected = 0
    truncated = False
    if not dacl_present:
        details["dacl_state"] = "absent"
    elif dacl_offset == 0:
        details["dacl_state"] = "null"
        limitations.append("null_dacl_observed_no_effective_access_conclusion")
    elif dacl_offset < 20 or dacl_offset + 8 > len(descriptor):
        details["dacl_state"] = "malformed"
        errors.append({"code": "malformed_dacl_offset", "message": "DACL offset is outside the descriptor"})
    else:
        acl_revision, acl_reserved, acl_size, ace_count, acl_reserved2 = struct.unpack_from(
            "<BBHHH", descriptor, dacl_offset
        )
        details.update(
            {
                "dacl_revision": acl_revision,
                "dacl_reserved_byte": acl_reserved,
                "dacl_reserved_word": acl_reserved2,
                "dacl_size": acl_size,
                "dacl_ace_count": ace_count,
            }
        )
        if acl_revision not in {2, 4}:
            errors.append(
                {"code": "unsupported_acl_revision", "message": f"ACL revision {acl_revision} is unsupported"}
            )
        entries_expected = ace_count
        acl_end = dacl_offset + acl_size
        if acl_size < 8 or acl_end > len(descriptor):
            details["dacl_state"] = "malformed"
            unknown_entries = ace_count
            truncated = ace_count > 0
            errors.append({"code": "malformed_dacl_size", "message": "DACL size is outside the descriptor"})
        else:
            details["dacl_state"] = "empty" if ace_count == 0 else "present"
            ace_offset = dacl_offset + 8
            scan_count = min(ace_count, SMB_PERMISSION_MAX_ACE_HEADERS)
            if ace_count > scan_count:
                truncated = True
                unknown_entries += ace_count - scan_count
                limitations.append("ace_header_scan_limit_reached")
            for ordinal in range(scan_count):
                if ace_offset + 4 > acl_end:
                    errors.append({"code": "truncated_ace_header", "message": f"ACE {ordinal} header is truncated"})
                    unknown_entries += max(1, scan_count - ordinal)
                    break
                ace_type, ace_flags, ace_size = struct.unpack_from("<BBH", descriptor, ace_offset)
                if ace_size < 4 or ace_offset + ace_size > acl_end:
                    errors.append({"code": "invalid_ace_size", "message": f"ACE {ordinal} has an invalid size"})
                    unknown_entries += max(1, scan_count - ordinal)
                    break
                ace = descriptor[ace_offset : ace_offset + ace_size]
                entries_parsed += 1
                ace_name, effect, layout = SMB_ACE_TYPES.get(ace_type, ("unknown", "unknown", "unknown"))
                provider_details: dict[str, object] = {
                    "ace_type": ace_name,
                    "ace_type_code": ace_type,
                    "ace_flags": ace_flags,
                    "ace_flag_names": [label for bit, label in SMB_ACE_FLAG_NAMES.items() if ace_flags & bit],
                    "ace_size": ace_size,
                }
                mask: int | None = None
                principal: dict[str, object] | None = None
                application_data_present = False
                parse_error: str | None = None
                if ace_size >= 8:
                    mask = struct.unpack_from("<L", ace, 4)[0]
                    provider_details["access_mask"] = f"0x{mask:08X}"
                try:
                    sid_offset, layout_details = _smb_ace_sid_offset(ace, layout)
                    provider_details.update(layout_details)
                    if sid_offset is not None:
                        principal, sid_size = _parse_smb_sid(ace, sid_offset, len(ace))
                        application_data_present = sid_offset + sid_size < len(ace)
                except ValueError as exc:
                    parse_error = str(exc)
                if application_data_present:
                    provider_details["application_data_present"] = True
                    provider_details["application_data_retained"] = False
                if application_data_present or parse_error or ace_name == "unknown" or principal is None:
                    unknown_entries += 1
                    if parse_error:
                        provider_details["parse_error"] = parse_error
                normalized_rights = _decoded_smb_rights(mask) if mask is not None else []
                evidence_payload = {
                    "ordinal": ordinal,
                    "type": ace_type,
                    "flags": ace_flags,
                    "mask": mask,
                    "sid": principal.get("native_id") if principal else None,
                    "compound_type": provider_details.get("compound_type"),
                    "object_flags": provider_details.get("object_flags"),
                    "object_type_guid": provider_details.get("object_type_guid"),
                    "inherited_object_type_guid": provider_details.get("inherited_object_type_guid"),
                    "application_data_present": application_data_present,
                    "parse_error": parse_error,
                }
                evidence_hash = hashlib.sha256(
                    json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                entry_set_hasher.update(evidence_hash.encode("ascii"))
                entry_set_hasher.update(b"\n")
                if len(entries) < SMB_PERMISSION_MAX_ACES_PER_DESCRIPTOR:
                    entries.append(
                        {
                            "ordinal": ordinal,
                            "principal_key": principal.get("principal_key") if principal else None,
                            "principal": principal,
                            "entry_kind": "ace",
                            "effect": effect,
                            "normalized_rights": normalized_rights,
                            "inherited_state": "inherited" if ace_flags & 0x10 else "not_inherited",
                            "expiration_at": None,
                            "evidence_hash": evidence_hash,
                            "provider_details": provider_details,
                        }
                    )
                else:
                    truncated = True
                ace_offset += ace_size

    if entries_parsed > SMB_PERMISSION_MAX_ACES_PER_DESCRIPTOR:
        limitations.append("ace_emission_limit_reached")
    entries_emitted = len(entries)
    entries_omitted = max(0, entries_expected - entries_emitted)
    if entries_omitted:
        truncated = True
    if unknown_entries:
        limitations.append("one_or_more_aces_not_fully_interpreted")
    # A DACL's zero-entry states are not interchangeable: a NULL DACL, an
    # empty DACL, and a descriptor with no DACL present have materially
    # different Windows authorization semantics even though all three contain
    # no ACE bytes.  Bind the ordered ACE digest to the stable descriptor facts
    # that define the observed permission surface.  Owner/group SIDs are also
    # included because they were explicitly requested and ownership affects who
    # can administer the DACL.  No raw descriptor or SACL material is retained.
    entry_set_hash = hashlib.sha256(
        json.dumps(
            {
                "contract": "smb_windows_acl_v1",
                "descriptor_revision": details.get("descriptor_revision"),
                "descriptor_control_retained": details.get("descriptor_control_retained"),
                "owner_sid": (
                    details.get("owner", {}).get("native_id") if isinstance(details.get("owner"), dict) else None
                ),
                "group_sid": (
                    details.get("group", {}).get("native_id") if isinstance(details.get("group"), dict) else None
                ),
                "dacl_state": details.get("dacl_state", "unknown"),
                "dacl_revision": details.get("dacl_revision"),
                "dacl_ace_count": details.get("dacl_ace_count"),
                "ordered_ace_digest": entry_set_hasher.hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    descriptor_evidence = {
        "descriptor_revision": details.get("descriptor_revision"),
        "descriptor_control_retained": details.get("descriptor_control_retained"),
        "owner_sid": (details.get("owner", {}).get("native_id") if isinstance(details.get("owner"), dict) else None),
        "group_sid": (details.get("group", {}).get("native_id") if isinstance(details.get("group"), dict) else None),
        "dacl_state": details.get("dacl_state", "unknown"),
        "dacl_ace_count": details.get("dacl_ace_count"),
        "entry_set_hash": entry_set_hash,
        "error_codes": [error["code"] for error in errors],
        "truncated": truncated,
    }
    evidence_hash = hashlib.sha256(
        json.dumps(descriptor_evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "provider_details": details,
        "permission_summary": {
            "dacl_state": details.get("dacl_state", "unknown"),
            "owner_observed": details.get("owner_state") == "observed",
            "group_observed": details.get("group_state") == "observed",
            "entries_observed": entries_expected,
            "entries_parsed": entries_parsed,
            "entries_emitted": entries_emitted,
            "entries_omitted": entries_omitted,
            "unknown_entries": unknown_entries,
            "truncated": truncated,
            "entry_set_hash": entry_set_hash,
            "evidence_hash": evidence_hash,
        },
        "entries": entries,
        "entries_observed": entries_expected,
        "entries_emitted": entries_emitted,
        "entries_omitted": entries_omitted,
        "unknown_entries": unknown_entries,
        "entry_set_hash": entry_set_hash,
        "evidence_hash": evidence_hash,
        "truncated": truncated,
        "limitations": list(dict.fromkeys(limitations))[:SMB_PERMISSION_MAX_LIMITATIONS],
        "errors": errors[:SMB_PERMISSION_MAX_ERRORS],
    }


def _smb_permission_assessment_base(
    *,
    run_id: str,
    endpoint_key: str,
    resource_name: str,
    provider_resource_id: str,
    subject_kind: str,
    subject_path: str,
    assessed_identity: dict[str, str],
    selection_scope: str,
) -> dict[str, object]:
    subject_id = _stable_case_preserving_fingerprint(
        "smb-object:v1", provider_resource_id, subject_kind, _smb_handle_path(subject_path)
    )
    assessment_key = _stable_fingerprint(
        "permission-assessment:v1",
        provider_resource_id,
        subject_id,
        assessed_identity["assessed_identity_fingerprint"],
        SMB_PERMISSION_SEMANTICS,
        SMB_PERMISSION_METHOD,
        selection_scope,
    )
    record: dict[str, object] = {
        "type": "permission_assessment",
        "run_id": run_id,
        "assessment_key": assessment_key,
        "provider": "smb",
        "semantics": SMB_PERMISSION_SEMANTICS,
        "permission_surface": SMB_PERMISSION_SURFACE,
        "endpoint_key": endpoint_key,
        "share_type": "smb",
        "resource_type": "smb_share",
        "resource_name": resource_name,
        "provider_resource_id": provider_resource_id,
        "subject_id": subject_id,
        "subject_kind": subject_kind,
        "subject_path": "\\" if not _smb_handle_path(subject_path) else f"\\{_smb_handle_path(subject_path)}",
        "assessed_identity_fingerprint": assessed_identity["assessed_identity_fingerprint"],
        "selection_scope": selection_scope,
        "selection_coverage": ("exhaustive_for_scope" if selection_scope == "share_root" else "deterministic_sample"),
        "method": SMB_PERMISSION_METHOD,
        "non_mutating": True,
        "effective_access_status": "not_computed",
        "negative_conclusion_supported": False,
        "observed_at": datetime.now(tz=UTC).isoformat(),
        "limitations": [],
        "errors": [],
        "provider_details": {
            "subject_path": "\\" if not _smb_handle_path(subject_path) else f"\\{_smb_handle_path(subject_path)}",
            "assessed_identity_fingerprint": assessed_identity["assessed_identity_fingerprint"],
            "session_kind": assessed_identity["session_kind"],
            "identity_source": assessed_identity["identity_source"],
            "requested_access": "READ_CONTROL",
            "requested_security_information": ["owner", "group", "dacl"],
            "sacl_requested": False,
        },
    }
    return record


def _query_smb_security_descriptor(
    conn: SMBConnection,
    tree_id: object,
    path: str,
    *,
    is_directory: bool,
    root_path_hint: str | None,
    cancel_event: threading.Event | None,
) -> dict[str, object]:
    if cancel_event is not None and cancel_event.is_set():
        return {"error_code": "cancelled", "stage": "before_open"}

    open_file = getattr(conn, "openFile", None)
    if not callable(open_file):
        return {"error_code": "probe_method_unavailable", "stage": "open"}
    normalized_path = _smb_handle_path(path)
    candidate_paths = [root_path_hint] if not normalized_path and root_path_hint is not None else [normalized_path]
    if not normalized_path and root_path_hint is None:
        candidate_paths.append("\\")

    file_id = None
    used_path = normalized_path
    result: dict[str, object] = {}
    try:
        for candidate_index, candidate_path in enumerate(candidate_paths):
            used_path = str(candidate_path or "")
            try:
                file_id = open_file(
                    tree_id,
                    used_path,
                    desiredAccess=READ_CONTROL,
                    shareMode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    creationOption=FILE_DIRECTORY_FILE if is_directory else FILE_NON_DIRECTORY_FILE,
                    creationDisposition=FILE_OPEN,
                )
            except (SessionError, NetBIOSError, NetBIOSTimeout, socket.timeout, TimeoutError, OSError) as exc:
                classification = _classify_smb_probe_failure(exc)
                if (
                    candidate_index == 0
                    and len(candidate_paths) > 1
                    and classification.root_path_fallback
                    and not classification.transport_fatal
                ):
                    continue
                return {
                    "error": exc,
                    "classification": classification,
                    "error_code": (
                        "permission_read_denied" if classification.outcome == "denied" else classification.reason_code
                    ),
                    "stage": "open",
                    "handle_path": used_path,
                }
            break

        if file_id is None:
            return {"error_code": "handle_open_failed", "stage": "open", "handle_path": used_path}
        if cancel_event is not None and cancel_event.is_set():
            result.update({"error_code": "cancelled", "stage": "before_query", "handle_path": used_path})
            return result

        try:
            low_level = conn.getSMBServer()
        except Exception as exc:
            result.update(
                {
                    "error": exc,
                    "classification": _classify_smb_probe_failure(exc),
                    "error_code": "protocol_adapter_unavailable",
                    "stage": "query",
                    "handle_path": used_path,
                }
            )
            return result
        query_sec_info = getattr(low_level, "query_sec_info", None)
        query_info = getattr(low_level, "queryInfo", None)
        try:
            if callable(query_sec_info):
                descriptor = query_sec_info(
                    tree_id,
                    file_id,
                    additional_information=SMB_PERMISSION_REQUESTED_INFORMATION,
                )
                protocol = "smb1_nt_trans_query_security_desc"
            elif callable(query_info):
                descriptor = query_info(
                    tree_id,
                    file_id,
                    inputBlob="",
                    infoType=SMB2_0_INFO_SECURITY,
                    fileInfoClass=SMB2_SEC_INFO_00,
                    additionalInformation=SMB_PERMISSION_REQUESTED_INFORMATION,
                    flags=0,
                )
                protocol = "smb2_query_info_security"
            else:
                result.update(
                    {
                        "error_code": "security_query_method_unavailable",
                        "stage": "query",
                        "handle_path": used_path,
                    }
                )
                return result
        except (SessionError, NetBIOSError, NetBIOSTimeout, socket.timeout, TimeoutError, OSError) as exc:
            classification = _classify_smb_probe_failure(exc)
            result.update(
                {
                    "error": exc,
                    "classification": classification,
                    "error_code": (
                        "permission_read_denied" if classification.outcome == "denied" else classification.reason_code
                    ),
                    "stage": "query",
                    "handle_path": used_path,
                }
            )
            return result
        except Exception as exc:
            result.update(
                {
                    "error": exc,
                    "classification": _classify_smb_probe_failure(exc),
                    "error_code": "security_descriptor_response_invalid",
                    "stage": "query",
                    "handle_path": used_path,
                }
            )
            return result
        result.update({"descriptor": descriptor, "protocol": protocol, "handle_path": used_path})
        return result
    finally:
        if file_id is not None:
            close_file = getattr(conn, "closeFile", None)
            if callable(close_file):
                try:
                    close_file(tree_id, file_id)
                except Exception as exc:
                    result["close_error"] = exc


def _build_smb_permission_records(
    conn: SMBConnection,
    tree_id: object | None,
    *,
    run_id: str,
    endpoint_key: str,
    resource_name: str,
    provider_resource_id: str,
    subject_path: str,
    is_directory: bool,
    assessed_identity: dict[str, str],
    selection_scope: str,
    entry_budget: int,
    root_path_hint: str | None,
    cancel_event: threading.Event | None,
    unavailable_reason: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], bool]:
    subject_kind = "share_root" if selection_scope == "share_root" else ("directory" if is_directory else "file")
    assessment = _smb_permission_assessment_base(
        run_id=run_id,
        endpoint_key=endpoint_key,
        resource_name=resource_name,
        provider_resource_id=provider_resource_id,
        subject_kind=subject_kind,
        subject_path=subject_path,
        assessed_identity=assessed_identity,
        selection_scope=selection_scope,
    )
    assessment_key = str(assessment["assessment_key"])
    if tree_id is None:
        query_result: dict[str, object] = {
            "error_code": unavailable_reason or "tree_unavailable",
            "stage": "tree_connect",
        }
    else:
        query_result = _query_smb_security_descriptor(
            conn,
            tree_id,
            subject_path,
            is_directory=is_directory,
            root_path_hint=root_path_hint,
            cancel_event=cancel_event,
        )

    error_code = str(query_result.get("error_code") or "")
    if error_code:
        classification = query_result.get("classification")
        denied = isinstance(classification, _SMBProbeClassification) and classification.outcome == "denied"
        transport_fatal = bool(isinstance(classification, _SMBProbeClassification) and classification.transport_fatal)
        error = query_result.get("error")
        message = _error_detail(error)[:512] if isinstance(error, BaseException) else error_code.replace("_", " ")
        error_record: dict[str, object] = {
            "code": error_code,
            "stage": str(query_result.get("stage") or "query"),
            "message": message,
        }
        if isinstance(classification, _SMBProbeClassification) and classification.protocol_status:
            error_record["protocol_status"] = classification.protocol_status
        errors = [error_record]
        limitations = ["direct_acl_not_retrieved", "effective_access_not_computed"]
        close_error = query_result.get("close_error")
        if isinstance(close_error, BaseException):
            close_classification = _classify_smb_probe_failure(close_error)
            transport_fatal = transport_fatal or close_classification.transport_fatal
            errors.append(
                {
                    "code": "handle_close_failed",
                    "stage": "cleanup",
                    "message": _error_detail(close_error)[:512],
                }
            )
            limitations.append("handle_cleanup_failed_session_will_be_closed")
        assessment.update(
            {
                "outcome": "denied" if denied else "inconclusive",
                "assessment_state": "failed",
                "retrieval_coverage": "failed",
                "provider_visibility": "denied" if denied else "unknown",
                "semantic_coverage": "not_observed",
                "principal_resolution": "not_attempted",
                "entries_observed": 0,
                "entries_emitted": 0,
                "entries_omitted": 0,
                "unknown_entries": 0,
                "entry_set_hash": None,
                "truncated": False,
                "error_code": error_code,
                "errors": errors[:SMB_PERMISSION_MAX_ERRORS],
                "limitations": limitations[:SMB_PERMISSION_MAX_LIMITATIONS],
                "permission_summary": {
                    "assessment_state": "failed",
                    "entries_observed": 0,
                    "entries_emitted": 0,
                    "truncated": False,
                },
            }
        )
        assessment["provider_details"].update(
            {
                "failure_stage": str(query_result.get("stage") or "query"),
                "handle_path_variant": "root_marker" if query_result.get("handle_path") == "\\" else "relative",
            }
        )
        return assessment, [], transport_fatal

    try:
        parsed = _parse_smb_security_descriptor(query_result.get("descriptor"))
    except ValueError as exc:
        errors: list[dict[str, object]] = [
            {
                "code": "security_descriptor_parse_failed",
                "stage": "parse",
                "message": str(exc)[:512],
            }
        ]
        limitations = ["direct_acl_not_parsed", "effective_access_not_computed"]
        close_error = query_result.get("close_error")
        close_transport_fatal = False
        if isinstance(close_error, BaseException):
            close_transport_fatal = _classify_smb_probe_failure(close_error).transport_fatal
            errors.append(
                {
                    "code": "handle_close_failed",
                    "stage": "cleanup",
                    "message": _error_detail(close_error)[:512],
                }
            )
            limitations.append("handle_cleanup_failed_session_will_be_closed")
        assessment.update(
            {
                "outcome": "inconclusive",
                "assessment_state": "failed",
                "retrieval_coverage": "failed",
                "provider_visibility": "provider_visible",
                "semantic_coverage": "not_observed",
                "principal_resolution": "not_attempted",
                "entries_observed": 0,
                "entries_emitted": 0,
                "entries_omitted": 0,
                "unknown_entries": 0,
                "entry_set_hash": None,
                "truncated": False,
                "error_code": "security_descriptor_parse_failed",
                "errors": errors[:SMB_PERMISSION_MAX_ERRORS],
                "limitations": limitations[:SMB_PERMISSION_MAX_LIMITATIONS],
                "permission_summary": {
                    "assessment_state": "failed",
                    "entries_observed": 0,
                    "entries_emitted": 0,
                    "truncated": False,
                },
            }
        )
        return assessment, [], close_transport_fatal

    all_entries = list(parsed["entries"])
    allowed_entries = all_entries[: max(0, entry_budget)]
    budget_omitted = len(all_entries) - len(allowed_entries)
    entries_omitted = int(parsed["entries_omitted"]) + budget_omitted
    truncated = bool(parsed["truncated"] or budget_omitted)
    limitations = list(parsed["limitations"])
    if budget_omitted:
        limitations.append("share_permission_entry_limit_reached")
    errors = list(parsed["errors"])
    close_error = query_result.get("close_error")
    if isinstance(close_error, BaseException):
        errors.append(
            {
                "code": "handle_close_failed",
                "stage": "cleanup",
                "message": _error_detail(close_error)[:512],
            }
        )
        limitations.append("handle_cleanup_failed_session_will_be_closed")
    assessment_state = "partial" if truncated or errors or int(parsed["unknown_entries"]) > 0 else "complete"
    assessment.update(
        {
            "outcome": "observed",
            "assessment_state": assessment_state,
            "retrieval_coverage": "partial" if assessment_state == "partial" else "complete",
            "provider_visibility": "provider_visible",
            "semantic_coverage": "acl_structure_only",
            "principal_resolution": "well_known_only",
            "negative_conclusion_supported": assessment_state == "complete",
            "entries_observed": int(parsed["entries_observed"]),
            "entries_emitted": len(allowed_entries),
            "entries_omitted": entries_omitted,
            "unknown_entries": int(parsed["unknown_entries"]),
            "entry_set_hash": parsed["entry_set_hash"],
            "evidence_hash": parsed["evidence_hash"],
            "truncated": truncated,
            "limitations": list(dict.fromkeys(limitations))[:SMB_PERMISSION_MAX_LIMITATIONS],
            "errors": errors[:SMB_PERMISSION_MAX_ERRORS],
            "error_code": errors[0]["code"] if errors else None,
            "permission_summary": {
                **parsed["permission_summary"],
                "assessment_state": assessment_state,
                "entries_emitted": len(allowed_entries),
                "entries_omitted": entries_omitted,
                "truncated": truncated,
            },
        }
    )
    assessment["provider_details"].update(parsed["provider_details"])
    assessment["provider_details"].update(
        {
            "query_protocol": query_result.get("protocol"),
            "handle_path_variant": "root_marker" if query_result.get("handle_path") == "\\" else "relative",
        }
    )

    entry_records: list[dict[str, object]] = []
    for entry in allowed_entries:
        evidence_hash = str(entry["evidence_hash"])
        entry_key = _stable_fingerprint("permission-entry:v1", assessment_key, entry["ordinal"], evidence_hash)
        entry_records.append(
            {
                "type": "permission_entry",
                "run_id": run_id,
                "assessment_key": assessment_key,
                "entry_key": entry_key,
                "provider": "smb",
                "semantics": SMB_PERMISSION_SEMANTICS,
                "permission_surface": SMB_PERMISSION_SURFACE,
                "endpoint_key": endpoint_key,
                "share_type": "smb",
                "resource_type": "smb_share",
                "resource_name": resource_name,
                "provider_resource_id": provider_resource_id,
                **entry,
            }
        )
    close_error = query_result.get("close_error")
    close_transport_fatal = bool(
        isinstance(close_error, BaseException) and _classify_smb_probe_failure(close_error).transport_fatal
    )
    return assessment, entry_records, close_transport_fatal


def _probe_smb_handle_access(
    conn: SMBConnection,
    tree_id: object,
    path: str,
    *,
    is_directory: bool,
    desired_access: int,
    capability: str,
    capabilities: dict[str, dict[str, object]],
    cancel_event: threading.Event | None,
    probe_circuit: _SMBProbeCircuit | None = None,
) -> None:
    if (cancel_event is not None and cancel_event.is_set()) or (probe_circuit is not None and probe_circuit.blocked):
        return
    open_file = getattr(conn, "openFile", None)
    if not callable(open_file):
        _mark_capability_not_tested(capabilities, capability, "probe_method_unavailable")
        return

    file_id = None
    normalized_path = _smb_handle_path(path)
    candidate_paths = [normalized_path]
    # SMB2 accepts an empty share-root path. Some SMB1/Samba/NAS servers expect
    # the explicit root marker instead. Retrying only path/compatibility failures
    # remains non-mutating because both attempts use FILE_OPEN.
    if is_directory and not normalized_path:
        if probe_circuit is not None and probe_circuit.root_path is not None:
            candidate_paths = [probe_circuit.root_path]
        else:
            candidate_paths.append("\\")

    for candidate_index, candidate_path in enumerate(candidate_paths):
        try:
            file_id = open_file(
                tree_id,
                candidate_path,
                desiredAccess=desired_access,
                shareMode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                creationOption=FILE_DIRECTORY_FILE if is_directory else FILE_NON_DIRECTORY_FILE,
                creationDisposition=FILE_OPEN,
            )
        except (SessionError, NetBIOSError, NetBIOSTimeout, socket.timeout, TimeoutError, OSError) as exc:
            classification = _classify_smb_probe_failure(exc)
            should_retry_root = (
                candidate_index == 0
                and len(candidate_paths) > 1
                and classification.root_path_fallback
                and not classification.transport_fatal
            )
            if should_retry_root:
                continue
            if (
                is_directory
                and not normalized_path
                and candidate_index > 0
                and not classification.root_path_fallback
                and probe_circuit is not None
            ):
                probe_circuit.root_path = candidate_path
            _record_capability(
                capabilities,
                capability,
                classification.outcome,
                reason_code=classification.reason_code,
                protocol_status=classification.protocol_status,
                method="non_mutating_handle_open",
                scope="directory" if is_directory else "file",
            )
            if probe_circuit is not None:
                if classification.transport_fatal:
                    probe_circuit.transport_failed = True
                if classification.abort_remaining_probes:
                    probe_circuit.probes_aborted = True
                if classification.transport_fatal or classification.abort_remaining_probes:
                    probe_circuit.reason_code = classification.reason_code
            return
        else:
            if is_directory and not normalized_path and probe_circuit is not None:
                probe_circuit.root_path = candidate_path
            _record_capability(
                capabilities,
                capability,
                "allowed",
                reason_code="granted",
                method="non_mutating_handle_open",
                scope="directory" if is_directory else "file",
            )
            break

    close_file = getattr(conn, "closeFile", None)
    if callable(close_file) and file_id is not None:
        try:
            close_file(tree_id, file_id)
        except Exception:
            # The requested access was conclusively granted. Session cleanup and
            # the final logoff still provide bounded recovery if handle close fails.
            pass


def _probe_smb_directory_access(
    conn: SMBConnection,
    tree_id: object,
    path: str,
    capabilities: dict[str, dict[str, object]],
    cancel_event: threading.Event | None,
    probe_circuit: _SMBProbeCircuit | None = None,
) -> None:
    for capability, desired_access in (
        ("create_file", FILE_ADD_FILE),
        ("create_directory", FILE_ADD_SUBDIRECTORY),
        ("delete", FILE_DELETE_CHILD),
        ("write_acl", WRITE_DAC),
        ("write_owner", WRITE_OWNER),
    ):
        _probe_smb_handle_access(
            conn,
            tree_id,
            path,
            is_directory=True,
            desired_access=desired_access,
            capability=capability,
            capabilities=capabilities,
            cancel_event=cancel_event,
            probe_circuit=probe_circuit,
        )
        if probe_circuit is not None and probe_circuit.blocked:
            break


def _probe_smb_file_access(
    conn: SMBConnection,
    tree_id: object,
    path: str,
    capabilities: dict[str, dict[str, object]],
    cancel_event: threading.Event | None,
    probe_circuit: _SMBProbeCircuit | None = None,
) -> None:
    for capability, desired_access in (
        ("read_file", FILE_READ_DATA),
        ("modify_file", FILE_WRITE_DATA),
        ("delete", DELETE),
    ):
        _probe_smb_handle_access(
            conn,
            tree_id,
            path,
            is_directory=False,
            desired_access=desired_access,
            capability=capability,
            capabilities=capabilities,
            cancel_event=cancel_event,
            probe_circuit=probe_circuit,
        )
        if probe_circuit is not None and probe_circuit.blocked:
            break


def _entry_timestamp(entry: object, *method_names: str) -> str | None:
    """Return the first usable timestamp exposed by an Impacket entry.

    Current Impacket ``SharedFile`` objects expose ``get_mtime_epoch`` as the
    last-write time.  Some older/adapted objects expose ``get_wtime_epoch``
    instead.  Trying explicit aliases in semantic order keeps this compatible
    without relabelling modification time as the distinct NTFS change time.
    """

    for method_name in method_names:
        method = getattr(entry, method_name, None)
        if not callable(method):
            continue
        try:
            epoch = float(method())
            # A zero Windows FILETIME converts to 1601-01-01. Directory
            # listings commonly use it to mean "unset", so do not publish it
            # as real data.
            if epoch <= -11_644_473_600:
                continue
            value = datetime.fromtimestamp(epoch, tz=UTC)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            continue
        return value.isoformat()
    return None


def _entry_metadata(entry: object, *, is_dir: bool) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if not is_dir:
        try:
            size_bytes = int(entry.get_filesize())  # type: ignore[attr-defined]
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            pass
        else:
            if size_bytes >= 0:
                metadata["size_bytes"] = size_bytes
        try:
            allocation_size = int(entry.get_allocsize())  # type: ignore[attr-defined]
        except (AttributeError, OverflowError, TypeError, ValueError):
            pass
        else:
            if allocation_size >= 0:
                metadata["allocation_size_bytes"] = allocation_size

    timestamp_methods = (
        ("mtime", ("get_mtime_epoch", "get_wtime_epoch")),
        ("created_at", ("get_ctime_epoch", "get_creation_time_epoch")),
        ("accessed_at", ("get_atime_epoch", "get_access_time_epoch")),
        # NTFS change time is not the last-write/mtime field. Only emit it
        # when an entry implementation provides an explicit change-time API.
        ("changed_at", ("get_changetime_epoch", "get_change_time_epoch", "get_chtime_epoch")),
    )
    for field_name, method_names in timestamp_methods:
        value = _entry_timestamp(entry, *method_names)
        if value is not None:
            metadata[field_name] = value

    attribute_methods = (
        ("archive", "is_archive"),
        ("compressed", "is_compressed"),
        ("hidden", "is_hidden"),
        ("read_only", "is_readonly"),
        ("system", "is_system"),
        ("temporary", "is_temporary"),
    )
    attributes: list[str] = []
    for label, method_name in attribute_methods:
        method = getattr(entry, method_name, None)
        if not callable(method):
            continue
        try:
            if bool(method()):
                attributes.append(label)
        except (AttributeError, OSError, TypeError, ValueError):
            continue
    if attributes:
        metadata["file_attributes"] = attributes
    return metadata


def _discover_smb_probe_candidates(
    conn: SMBConnection,
    share_name: str,
    *,
    directory_seeds: list[str],
    directory_samples: list[str],
    file_samples: list[str],
    probe_limit: int,
    max_entries: int,
    exclude_path_regex: re.Pattern[str] | None,
    already_listed_paths: set[str],
    on_list_error=None,
    on_list_success=None,
    cancel_event: threading.Event | None = None,
    probe_circuit: _SMBProbeCircuit | None = None,
) -> tuple[int, int, bool, int]:
    """Find bounded file samples below directories excluded by inventory depth.

    The helper never emits inventory records and never opens or mutates an
    object. Both directory listing attempts and inspected entries are capped so
    sparse or cyclic-looking namespaces cannot turn capability sampling into an
    unbounded traversal.
    """

    if probe_limit <= 0 or max_entries <= 0 or len(file_samples) >= probe_limit:
        return 0, 0, False, 0

    queue: collections.deque[str] = collections.deque()
    queued_paths: set[str] = set()
    for raw_path in directory_seeds:
        path = normalize_path("", _smb_handle_path(raw_path))
        path_key = _smb_handle_path(path).casefold()
        if (
            not path_key
            or path_key in already_listed_paths
            or path_key in queued_paths
            or (exclude_path_regex is not None and exclude_path_regex.search(path))
        ):
            continue
        queue.append(path)
        queued_paths.add(path_key)

    directory_sample_keys = {_smb_handle_path(path).casefold() for path in directory_samples}
    file_sample_keys = {_smb_handle_path(path).casefold() for path in file_samples}
    directory_candidates_seen = 0
    file_candidates_seen = 0
    directory_attempts = 0
    inspected = 0
    limit_reached = False

    while queue and directory_attempts < probe_limit and inspected < max_entries and len(file_samples) < probe_limit:
        if (cancel_event is not None and cancel_event.is_set()) or (
            probe_circuit is not None and probe_circuit.blocked
        ):
            break

        directory_path = queue.popleft()
        directory_key = _smb_handle_path(directory_path).casefold()
        if directory_key in already_listed_paths:
            continue
        directory_attempts += 1
        wildcard = f"{_smb_handle_path(directory_path)}\\*"

        try:
            entries = conn.listPath(share_name, wildcard)
        except (SessionError, NetBIOSError, NetBIOSTimeout, socket.timeout, TimeoutError, OSError) as exc:
            if on_list_error is not None:
                try:
                    on_list_error(directory_path, exc)
                except Exception:
                    pass
            classification = _classify_smb_probe_failure(exc)
            if classification.transport_fatal:
                if probe_circuit is not None:
                    probe_circuit.transport_failed = True
                    probe_circuit.reason_code = classification.reason_code
                break
            # Impacket listPath() connects and disconnects its own temporary
            # tree. An invalid TID from that call does not invalidate the
            # separate tree_id used by non-mutating handle probes.
            continue

        already_listed_paths.add(directory_key)
        if on_list_success is not None:
            try:
                on_list_success(directory_path)
            except Exception:
                pass

        for entry in entries:
            if cancel_event is not None and cancel_event.is_set():
                break
            name = entry.get_longname()
            if name in {".", ".."}:
                continue
            if inspected >= max_entries:
                limit_reached = True
                break
            inspected += 1

            full_path = normalize_path(directory_path, name)
            if exclude_path_regex is not None and exclude_path_regex.search(full_path):
                continue

            is_directory = bool(entry.is_directory())
            path_key = _smb_handle_path(full_path).casefold()
            if is_directory:
                directory_candidates_seen += 1
                if len(directory_samples) < probe_limit and path_key not in directory_sample_keys:
                    directory_samples.append(full_path)
                    directory_sample_keys.add(path_key)
                if (
                    path_key not in already_listed_paths
                    and path_key not in queued_paths
                    and directory_attempts + len(queue) < probe_limit
                ):
                    queue.append(full_path)
                    queued_paths.add(path_key)
                continue

            file_candidates_seen += 1
            if path_key not in file_sample_keys:
                file_samples.append(full_path)
                file_sample_keys.add(path_key)
            if len(file_samples) >= probe_limit:
                break

    if not limit_reached and inspected >= max_entries and queue and len(file_samples) < probe_limit:
        limit_reached = True
    return directory_candidates_seen, file_candidates_seen, limit_reached, inspected


def list_share_entries(
    conn: SMBConnection,
    share_name: str,
    max_depth: int,
    max_entries: int,
    exclude_path_regex: re.Pattern[str] | None,
    extensions: set[str] | None,
    on_list_error=None,
    on_limit_reached=None,
    on_list_success=None,
    on_probe_candidate=None,
    on_probe_directory_seed=None,
    cancel_event: threading.Event | None = None,
):
    queue = collections.deque([("", 0)])
    inspected = 0
    emitted = 0
    limit_reached = False

    while queue and inspected < max_entries:
        if cancel_event is not None and cancel_event.is_set():
            break
        rel_path, depth = queue.popleft()
        wildcard = f"{rel_path}\\*" if rel_path else "*"

        try:
            entries = conn.listPath(share_name, wildcard)
        except (SessionError, NetBIOSError, NetBIOSTimeout, socket.timeout, TimeoutError, OSError) as exc:
            if on_list_error is not None:
                try:
                    on_list_error(rel_path or "\\", exc)
                except Exception:
                    pass
            if _is_smb_transport_failure(exc):
                break
            continue

        if on_list_success is not None:
            try:
                on_list_success(rel_path or "\\")
            except Exception:
                pass

        for entry_index, entry in enumerate(entries):
            if cancel_event is not None and cancel_event.is_set():
                break
            name = entry.get_longname()
            if name in {".", ".."}:
                continue

            inspected += 1
            at_inspection_cap = inspected >= max_entries

            def _finish_at_cap() -> bool:
                nonlocal limit_reached
                if not at_inspection_cap:
                    return False
                try:
                    remaining_count = len(entries) - entry_index - 1
                    if remaining_count <= 0:
                        unseen_in_listing = False
                    elif remaining_count > 2:
                        unseen_in_listing = True
                    else:
                        unseen_in_listing = any(
                            entries[offset].get_longname() not in {".", ".."}
                            for offset in range(entry_index + 1, len(entries))
                        )
                except (AttributeError, IndexError, TypeError):
                    # Impacket returns a list, but conservatively report truncation
                    # for an unsized custom iterator because we cannot safely peek.
                    unseen_in_listing = True
                limit_reached = unseen_in_listing or bool(queue)
                return True

            full_path = normalize_path(rel_path, name)
            if exclude_path_regex and exclude_path_regex.search(full_path):
                if _finish_at_cap():
                    break
                continue

            is_dir = bool(entry.is_directory())
            if on_probe_candidate is not None:
                try:
                    on_probe_candidate(full_path, is_dir)
                except Exception:
                    pass
            if extensions and not is_dir:
                suffix = os.path.splitext(name)[1].lower()
                if suffix not in extensions:
                    if _finish_at_cap():
                        break
                    continue

            emitted += 1
            yield {
                "path": full_path,
                "name": name,
                "is_dir": is_dir,
                **_entry_metadata(entry, is_dir=is_dir),
            }

            if is_dir:
                if depth + 1 < max_depth:
                    queue.append((full_path.strip("\\"), depth + 1))
                elif on_probe_directory_seed is not None:
                    try:
                        on_probe_directory_seed(full_path)
                    except Exception:
                        pass
            if _finish_at_cap():
                break

    if limit_reached and on_limit_reached is not None:
        try:
            on_limit_reached(inspected, emitted)
        except Exception:
            pass


def _dialect_label(raw: str) -> str:
    mapping = {
        "514": "2.0.2",
        "528": "2.1",
        "768": "3.0",
        "770": "3.0.2",
        "785": "3.1.1",
    }
    return mapping.get(str(raw), str(raw))


def _signing_label(conn: SMBConnection) -> str:
    try:
        required = bool(conn.isSigningRequired())
        return "required" if required else "not_required"
    except (AttributeError, OSError, SessionError, TypeError, ValueError):
        return "unknown"


def _selected_share_types(raw_value: str) -> set[str]:
    normalized = (raw_value or "smb").strip().lower()
    if normalized == "both":
        return {"smb", "nfs"}
    if normalized in {"smb", "nfs"}:
        return {normalized}
    return {"smb"}


def _resolve_smb_auth_method(args: argparse.Namespace) -> str:
    if args.smb_anonymous:
        return "anonymous"
    if getattr(args, "use_session_creds", False):
        return "kerberos"
    if args.kerberos:
        return "kerberos"
    if args.username:
        return "ntlm"
    return "anonymous"


def _normalize_smb_identity(args: argparse.Namespace) -> None:
    """Normalize supported AD identity forms into Impacket's user/domain fields.

    Impacket's SMBConnection API accepts the username and domain separately. The
    collector accepts Windows' down-level and UPN forms as well as Impacket's
    domain/user convention, but rejects ambiguous or contradictory combinations.
    """

    username = str(getattr(args, "username", "") or "").strip()
    domain = str(getattr(args, "domain", "") or "").strip()
    local_auth = bool(getattr(args, "local_auth", False))

    if any(separator in domain for separator in ("\\", "/", "@")):
        raise SystemExit("--domain must be a domain or realm name without \\, /, or @ separators")

    if domain == ".":
        domain = ""
        local_auth = True

    detected_separators = [separator for separator in ("\\", "/", "@") if separator in username]
    if len(detected_separators) > 1:
        raise SystemExit("ambiguous --username; use USER, DOMAIN\\USER, DOMAIN/USER, or USER@REALM")

    qualified_domain = ""
    if detected_separators:
        separator = detected_separators[0]
        if username.count(separator) != 1:
            raise SystemExit(
                "invalid --username; use exactly one identity separator in DOMAIN\\USER, DOMAIN/USER, or USER@REALM"
            )
        left, right = (part.strip() for part in username.split(separator, 1))
        if not left or not right:
            raise SystemExit("invalid --username; both the user and domain/realm components must be non-empty")
        if separator == "@":
            username, qualified_domain = left, right
        else:
            qualified_domain, username = left, right

        if qualified_domain == ".":
            if domain:
                raise SystemExit("local identity .\\USER cannot be combined with --domain")
            local_auth = True
            qualified_domain = ""

    if local_auth:
        if qualified_domain:
            raise SystemExit("--local-auth cannot be combined with a domain-qualified --username")
        if domain:
            raise SystemExit("--local-auth cannot be combined with --domain")
        domain = ""
    elif qualified_domain:
        if domain and domain.casefold() != qualified_domain.casefold():
            raise SystemExit(
                f"conflicting SMB domains: --domain specifies {domain!r}, but --username specifies {qualified_domain!r}"
            )
        domain = qualified_domain

    args.username = username
    args.domain = domain
    args.local_auth = local_auth


def _resolve_ccache_env_value(raw_path: str | None) -> str | None:
    if raw_path:
        value = str(raw_path).strip()
        if not value:
            return None
        if value.upper().startswith("FILE:"):
            value = value[5:]
        expanded = os.path.abspath(os.path.expanduser(value))
        return expanded

    fallback = str(os.getenv("KRB5CCNAME", "") or "").strip()
    if fallback.upper().startswith("FILE:"):
        fallback = fallback[5:]
    return fallback or None


def _principal_from_ccache_env(ccache_env_value: str | None) -> tuple[str | None, str | None, str | None]:
    if not ccache_env_value:
        return None, None, "no Kerberos credential cache configured"

    try:
        from impacket.krb5.ccache import CCache
    except Exception as exc:  # pragma: no cover - dependency import error path
        return None, None, f"unable to read Kerberos cache: {exc}"

    previous = os.environ.get("KRB5CCNAME")
    try:
        os.environ["KRB5CCNAME"] = ccache_env_value
        domain, username, _tgt, _tgs = CCache.parseFile()
    except Exception as exc:
        return None, None, f"unable to parse Kerberos cache {ccache_env_value}: {exc}"
    finally:
        if previous is None:
            os.environ.pop("KRB5CCNAME", None)
        else:
            os.environ["KRB5CCNAME"] = previous

    if not username:
        return None, None, f"Kerberos cache {ccache_env_value} has no default username"
    return str(username), str(domain or ""), None


@contextmanager
def _run_scoped_kerberos_cache(ccache_env_value: str | None):
    if not ccache_env_value:
        yield
        return

    previous = os.environ.get("KRB5CCNAME")
    os.environ["KRB5CCNAME"] = ccache_env_value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("KRB5CCNAME", None)
        else:
            os.environ["KRB5CCNAME"] = previous


def _validate_args(args: argparse.Namespace) -> None:
    _normalize_smb_identity(args)
    selected_share_types = _selected_share_types(getattr(args, "share_types", "smb"))
    cidr = getattr(args, "cidr", [])
    hosts = getattr(args, "hosts", None)
    kerberos = bool(getattr(args, "kerberos", False))
    smb_anonymous = bool(getattr(args, "smb_anonymous", False))
    use_session_creds = bool(getattr(args, "use_session_creds", False))
    username = str(getattr(args, "username", "") or "")
    domain = str(getattr(args, "domain", "") or "")
    local_auth = bool(getattr(args, "local_auth", False))
    password = str(getattr(args, "password", "") or "")
    hashes = getattr(args, "hashes", None)
    ccache = getattr(args, "ccache", None)
    upload = bool(getattr(args, "upload", False))
    api_base = getattr(args, "api_base", None)
    project_id = getattr(args, "project_id", None)
    api_token = getattr(args, "api_token", None)
    output_path = str(getattr(args, "output", "") or "").strip()
    workers = int(getattr(args, "workers", 1))
    timeout = float(getattr(args, "timeout", 1.0))
    max_depth = int(getattr(args, "max_depth", 1))
    max_entries_per_share = int(getattr(args, "max_entries_per_share", 1))
    access_probe_limit = int(getattr(args, "access_probe_limit", 3))
    smb_permission_sample_limit = int(getattr(args, "smb_permission_sample_limit", SMB_PERMISSION_DEFAULT_SAMPLE_LIMIT))
    max_targets = int(getattr(args, "max_targets", 65536))
    max_artifact_bytes = int(getattr(args, "max_artifact_bytes", DEFAULT_MAX_ARTIFACT_BYTES))
    progress_interval = float(getattr(args, "progress_interval", 5.0))
    upload_timeout = float(getattr(args, "upload_timeout", 600.0))
    upload_attempts = int(getattr(args, "upload_attempts", 3))
    exclude_path_regex = getattr(args, "exclude_path_regex", None)

    if not cidr and not hosts:
        raise SystemExit("at least one target source is required: --hosts and/or --cidr")

    if kerberos and smb_anonymous:
        raise SystemExit("--kerberos cannot be combined with --smb-anonymous")
    if use_session_creds and smb_anonymous:
        raise SystemExit("--use-session-creds cannot be combined with --smb-anonymous")

    if "smb" in selected_share_types:
        if smb_anonymous and (username or password or hashes or domain or local_auth or ccache):
            raise SystemExit(
                "--smb-anonymous cannot be combined with SMB identity, password, domain, hashes, ccache, or local auth options"
            )
        if local_auth and not username:
            raise SystemExit("--local-auth requires --username")
        if local_auth and (kerberos or use_session_creds):
            raise SystemExit("--local-auth cannot be combined with Kerberos authentication")
        if domain and not username and not use_session_creds:
            raise SystemExit("--domain requires --username")
        if kerberos and not username and not use_session_creds:
            raise SystemExit("--kerberos requires --username")
        if kerberos and username and not domain and not ccache and not use_session_creds:
            raise SystemExit("--kerberos requires --domain or a domain-qualified --username")
        if ccache and not kerberos and not use_session_creds:
            raise SystemExit("--ccache requires --kerberos (or --use-session-creds)")
        if use_session_creds and hashes:
            raise SystemExit("--use-session-creds cannot be combined with --hashes")
        if use_session_creds and password:
            raise SystemExit("--use-session-creds cannot be combined with --password")
        if password and hashes:
            raise SystemExit("--password and --hashes are mutually exclusive SMB credential sources")
        if hashes:
            hash_parts = str(hashes).split(":")
            if len(hash_parts) != 2:
                raise SystemExit("--hashes must be in LMHASH:NTHASH format")
            lmhash, nthash = hash_parts
            if (lmhash and re.fullmatch(r"[0-9a-fA-F]{32}", lmhash) is None) or re.fullmatch(
                r"[0-9a-fA-F]{32}", nthash
            ) is None:
                raise SystemExit(
                    "--hashes requires an optional 32-character hexadecimal LM hash and a 32-character hexadecimal NT hash"
                )
        if hashes and not username and not smb_anonymous:
            raise SystemExit("--hashes requires --username unless --smb-anonymous is set")
        if password and not username and not smb_anonymous:
            raise SystemExit("--password requires --username unless --smb-anonymous is set")

    if upload and (not api_base or not project_id or not api_token):
        raise SystemExit("--upload requires --api-base, --project-id, and --api-token")
    if output_path:
        output = Path(output_path).expanduser()
        normalized_output = str(output).lower()
        if not normalized_output.endswith(SUPPORTED_ARTIFACT_SUFFIXES):
            raise SystemExit("--output must end in .ndjson, .jsonl, .json, or the corresponding .gz suffix")
        gzip_output = bool(getattr(args, "gzip", False))
        if gzip_output and not normalized_output.endswith(".gz"):
            raise SystemExit("--gzip requires an --output filename ending in .gz")
        if normalized_output.endswith(".gz") and not gzip_output:
            raise SystemExit("an --output filename ending in .gz requires --gzip")
        parent = output.parent if str(output.parent) else Path(".")
        if not parent.exists():
            raise SystemExit(f"--output directory does not exist: {parent}")
        if not parent.is_dir():
            raise SystemExit(f"--output parent is not a directory: {parent}")
        if output.exists() and output.is_dir():
            raise SystemExit(f"--output points to a directory, expected file path: {output}")
        preflight_path: str | None = None
        try:
            preflight_fd, preflight_path = tempfile.mkstemp(
                prefix=".share-sentinel-write-check-",
                dir=str(parent),
            )
            os.close(preflight_fd)
        except OSError as exc:
            raise SystemExit(f"--output directory is not writable: {parent}: {_error_detail(exc)}") from exc
        finally:
            if preflight_path:
                try:
                    os.unlink(preflight_path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise SystemExit(f"unable to clean output write check in {parent}: {_error_detail(exc)}") from exc
        args.output = str(output)
    if bool(getattr(args, "gzip", False)) and not output_path and not upload:
        raise SystemExit("--gzip requires --output unless --upload is enabled")
    if workers <= 0:
        raise SystemExit("--workers must be greater than zero")
    if workers > 1024:
        raise SystemExit("--workers must be 1024 or fewer")
    if not math.isfinite(timeout):
        raise SystemExit("--timeout must be finite")
    if timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    if max_depth <= 0:
        raise SystemExit("--max-depth must be greater than zero")
    if max_entries_per_share <= 0:
        raise SystemExit("--max-entries-per-share must be greater than zero")
    if access_probe_limit < 0 or access_probe_limit > 100:
        raise SystemExit("--access-probe-limit must be between 0 and 100")
    if smb_permission_sample_limit < 0 or smb_permission_sample_limit > SMB_PERMISSION_MAX_SAMPLE_LIMIT:
        raise SystemExit(f"--smb-permission-sample-limit must be between 0 and {SMB_PERMISSION_MAX_SAMPLE_LIMIT}")
    if (
        "smb" in selected_share_types
        and getattr(args, "smb_permissions", "root") == "sampled"
        and smb_permission_sample_limit <= 0
    ):
        raise SystemExit("--smb-permission-sample-limit must be greater than zero in sampled mode")
    if max_targets < 0:
        raise SystemExit("--max-targets must be zero or greater")
    if max_artifact_bytes < 0:
        raise SystemExit("--max-artifact-bytes must be zero or greater")
    if not math.isfinite(progress_interval):
        raise SystemExit("--progress-interval must be finite")
    if progress_interval < 0:
        raise SystemExit("--progress-interval must be zero or greater")
    if not math.isfinite(upload_timeout):
        raise SystemExit("--upload-timeout must be finite")
    if upload_timeout <= 0:
        raise SystemExit("--upload-timeout must be greater than zero")
    if upload_attempts <= 0 or upload_attempts > 10:
        raise SystemExit("--upload-attempts must be between 1 and 10")
    if exclude_path_regex:
        try:
            re.compile(exclude_path_regex)
        except re.error as exc:
            raise SystemExit(f"--exclude-path-regex is invalid: {exc}") from exc


def _parse_showmount_exports(output: str) -> list[str]:
    return list(_parse_showmount_exports_bounded(output).exports)


@dataclass(frozen=True)
class _NFSExportParseResult:
    exports: tuple[str, ...]
    observed_export_lines: int
    truncated: bool
    limitations: tuple[str, ...]


NFS_SHOWMOUNT_MAX_STDOUT_BYTES = 4 * 1024 * 1024
NFS_SHOWMOUNT_MAX_STDERR_BYTES = 64 * 1024
NFS_SHOWMOUNT_MAX_EXPORTS = 10_000
NFS_SHOWMOUNT_MAX_EXPORT_PATH_BYTES = 4096
NFS_SHOWMOUNT_MAX_ERROR_CHARACTERS = 4096
NFS_SHOWMOUNT_PROCESS_EXIT_GRACE_SECONDS = 1.0
NFS_SHOWMOUNT_PIPE_DRAIN_GRACE_SECONDS = 1.0


def _parse_showmount_exports_bounded(
    output: str,
    *,
    max_exports: int = NFS_SHOWMOUNT_MAX_EXPORTS,
    max_path_bytes: int = NFS_SHOWMOUNT_MAX_EXPORT_PATH_BYTES,
) -> _NFSExportParseResult:
    exports: list[str] = []
    seen: set[str] = set()
    observed_export_lines = 0
    truncated = False
    limitations: set[str] = set()
    for raw_line in io.StringIO(output):
        line = raw_line.strip()
        if not line or line.lower().startswith("exports list"):
            continue
        if not line.startswith("/"):
            continue
        export_path = line.split(maxsplit=1)[0]
        observed_export_lines += 1
        if len(export_path.encode("utf-8", errors="replace")) > max_path_bytes:
            truncated = True
            limitations.add("nfs_export_path_limit_reached")
            continue
        if export_path in seen:
            continue
        if len(exports) >= max_exports:
            truncated = True
            limitations.add("nfs_export_count_limit_reached")
            continue
        seen.add(export_path)
        exports.append(export_path)
    return _NFSExportParseResult(
        tuple(exports),
        observed_export_lines,
        truncated,
        tuple(sorted(limitations)),
    )


@dataclass(frozen=True)
class NFSExportDiscovery:
    exports: tuple[str, ...]
    status: str
    detail: str | None = None
    observed_export_lines: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    exports_truncated: bool = False
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class _BoundedProcessDrainError(RuntimeError):
    """Raised when child output pipes cannot be closed within the safety bound."""


@dataclass(frozen=True)
class NFSV4NullProbe:
    transport_status: str
    service_status: str
    status: str
    supported_version_min: int | None = None
    supported_version_max: int | None = None

    def public_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "method": "onc_rpc_null",
            "program": 100003,
            "version": 4,
            "procedure": 0,
            "credential_flavor": "AUTH_NONE",
            "mutating": False,
            "status": self.status,
        }
        if self.supported_version_min is not None:
            metadata["supported_version_min"] = self.supported_version_min
        if self.supported_version_max is not None:
            metadata["supported_version_max"] = self.supported_version_max
        return metadata


NFS_RPC_MAX_RECORD_BYTES = 64 * 1024
NFS_RPC_MAX_FRAGMENTS = 8


class _NFSRPCProtocolError(RuntimeError):
    pass


def _recv_exact(connection: object, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)  # type: ignore[attr-defined]
        if not chunk:
            raise _NFSRPCProtocolError("rpc_connection_closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_rpc_record(connection: object) -> bytes:
    payload = bytearray()
    for _fragment in range(NFS_RPC_MAX_FRAGMENTS):
        marker = struct.unpack("!I", _recv_exact(connection, 4))[0]
        final_fragment = bool(marker & 0x80000000)
        fragment_size = marker & 0x7FFFFFFF
        if fragment_size > NFS_RPC_MAX_RECORD_BYTES or len(payload) + fragment_size > NFS_RPC_MAX_RECORD_BYTES:
            raise _NFSRPCProtocolError("rpc_response_too_large")
        payload.extend(_recv_exact(connection, fragment_size))
        if final_fragment:
            return bytes(payload)
    raise _NFSRPCProtocolError("rpc_fragment_limit_reached")


def _parse_nfs_v4_null_reply(payload: bytes, *, expected_xid: int) -> NFSV4NullProbe:
    if len(payload) < 12:
        raise _NFSRPCProtocolError("rpc_reply_truncated")
    xid, message_type, reply_status = struct.unpack_from("!III", payload, 0)
    if xid != expected_xid:
        raise _NFSRPCProtocolError("rpc_xid_mismatch")
    if message_type != 1:
        raise _NFSRPCProtocolError("rpc_message_type_invalid")

    if reply_status == 1:  # MSG_DENIED
        if len(payload) < 16:
            raise _NFSRPCProtocolError("rpc_denied_reply_truncated")
        reject_status = struct.unpack_from("!I", payload, 12)[0]
        status = "rpc_version_mismatch" if reject_status == 0 else "rpc_auth_rejected"
        return NFSV4NullProbe("reachable", "rpc_responded", status)
    if reply_status != 0:  # MSG_ACCEPTED
        raise _NFSRPCProtocolError("rpc_reply_status_invalid")
    if len(payload) < 20:
        raise _NFSRPCProtocolError("rpc_accepted_reply_truncated")

    _verifier_flavor, verifier_length = struct.unpack_from("!II", payload, 12)
    if verifier_length > 4096:
        raise _NFSRPCProtocolError("rpc_verifier_too_large")
    padded_verifier_length = (verifier_length + 3) & ~3
    accept_offset = 20 + padded_verifier_length
    if len(payload) < accept_offset + 4:
        raise _NFSRPCProtocolError("rpc_verifier_truncated")
    accept_status = struct.unpack_from("!I", payload, accept_offset)[0]
    if accept_status == 0:  # SUCCESS
        return NFSV4NullProbe("reachable", "nfs_v4_confirmed", "supported")
    if accept_status == 2:  # PROG_MISMATCH
        if len(payload) < accept_offset + 12:
            raise _NFSRPCProtocolError("rpc_version_range_truncated")
        low, high = struct.unpack_from("!II", payload, accept_offset + 4)
        if low > high:
            raise _NFSRPCProtocolError("rpc_version_range_invalid")
        return NFSV4NullProbe(
            "reachable",
            "nfs_service_confirmed",
            "version_not_supported",
            supported_version_min=low,
            supported_version_max=high,
        )
    status_by_accept_code = {
        1: "nfs_program_unavailable",
        3: "null_procedure_unavailable",
        4: "rpc_garbage_arguments",
        5: "rpc_system_error",
    }
    return NFSV4NullProbe(
        "reachable",
        "rpc_responded",
        status_by_accept_code.get(accept_status, "rpc_accept_status_unknown"),
    )


def _probe_nfs_v4_null(host: str, timeout_seconds: float) -> NFSV4NullProbe:
    """Issue a bounded, non-mutating NFSv4 NULL call directly to tcp/2049."""

    connection = socket.create_connection((host, 2049), timeout=timeout_seconds)
    try:
        settimeout = getattr(connection, "settimeout", None)
        if callable(settimeout):
            settimeout(timeout_seconds)
        xid = random.getrandbits(32)
        call = struct.pack(
            "!10I",
            xid,
            0,  # CALL
            2,  # RPC version
            100003,  # NFS program
            4,  # NFSv4
            0,  # NULL procedure
            0,
            0,  # AUTH_NONE credential
            0,
            0,  # AUTH_NONE verifier
        )
        record = struct.pack("!I", 0x80000000 | len(call)) + call
        try:
            connection.sendall(record)
            payload = _recv_rpc_record(connection)
            return _parse_nfs_v4_null_reply(payload, expected_xid=xid)
        except (socket.timeout, TimeoutError):
            return NFSV4NullProbe("reachable", "indeterminate", "response_timeout")
        except _NFSRPCProtocolError as exc:
            return NFSV4NullProbe("reachable", "indeterminate", str(exc))
        except OSError:
            return NFSV4NullProbe("reachable", "indeterminate", "connection_lost")
    finally:
        try:
            connection.close()
        except (AttributeError, OSError):
            pass


def _classify_showmount_failure(detail: str) -> str:
    normalized = detail.casefold()
    if "program not registered" in normalized or "prognotregistered" in normalized:
        return "mount_protocol_unavailable"
    if "access denied" in normalized or "permission denied" in normalized or "authentication error" in normalized:
        return "permission_denied"
    if "timed out" in normalized or "timeout" in normalized:
        return "timed_out"
    if "name or service not known" in normalized or "temporary failure in name resolution" in normalized:
        return "name_resolution_failed"
    if "no route to host" in normalized or "network is unreachable" in normalized:
        return "transport_unreachable"
    return "failed"


def _run_bounded_process(
    argv: list[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> _BoundedProcessResult:
    """Drain both child pipes concurrently while retaining only bounded bytes."""

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    results: dict[str, tuple[bytes, bool]] = {}

    def drain(name: str, stream: object, limit: int) -> None:
        retained = bytearray()
        truncated = False
        try:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    break
                remaining = max(0, limit - len(retained))
                if remaining:
                    retained.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
        except (OSError, ValueError):
            truncated = True
        finally:
            try:
                stream.close()  # type: ignore[attr-defined]
            except (AttributeError, OSError, ValueError):
                pass
            results[name] = (bytes(retained), truncated)

    stdout_thread = threading.Thread(
        target=drain,
        args=("stdout", process.stdout, max(0, int(stdout_limit))),
        name="showmount-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=("stderr", process.stderr, max(0, int(stderr_limit))),
        name="showmount-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    termination_failed = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.kill()
        except OSError:
            pass
        try:
            returncode = process.wait(timeout=NFS_SHOWMOUNT_PROCESS_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            termination_failed = True
            returncode = -1

    drain_threads = (stdout_thread, stderr_thread)
    drain_deadline = time.monotonic() + NFS_SHOWMOUNT_PIPE_DRAIN_GRACE_SECONDS
    for thread in drain_threads:
        thread.join(timeout=max(0.0, drain_deadline - time.monotonic()))

    if any(thread.is_alive() for thread in drain_threads):
        # A grandchild can inherit the pipe writer after showmount exits. Close
        # our read handles to release blocked readers, but perform close in
        # daemon helpers because BufferedReader.close() can itself wait on a
        # read lock held by the drain thread.
        close_threads: list[threading.Thread] = []

        def close_stream(stream: object) -> None:
            try:
                stream.close()  # type: ignore[attr-defined]
            except (AttributeError, OSError, ValueError):
                pass

        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            close_thread = threading.Thread(
                target=close_stream,
                args=(stream,),
                name=f"showmount-{name}-closer",
                daemon=True,
            )
            close_thread.start()
            close_threads.append(close_thread)

        close_deadline = time.monotonic() + NFS_SHOWMOUNT_PIPE_DRAIN_GRACE_SECONDS
        for thread in (*close_threads, *drain_threads):
            thread.join(timeout=max(0.0, close_deadline - time.monotonic()))

    if any(thread.is_alive() for thread in drain_threads):
        raise _BoundedProcessDrainError("showmount output pipes did not close within the safety bound")
    if termination_failed:
        raise _BoundedProcessDrainError("showmount did not exit after termination")
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    stdout, stdout_truncated = results.get("stdout", (b"", True))
    stderr, stderr_truncated = results.get("stderr", (b"", True))
    return _BoundedProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _bounded_showmount_detail(raw: bytes, *, truncated: bool) -> str:
    detail = raw.decode("utf-8", errors="replace").strip()
    if len(detail) > NFS_SHOWMOUNT_MAX_ERROR_CHARACTERS:
        detail = detail[: NFS_SHOWMOUNT_MAX_ERROR_CHARACTERS - 1] + "…"
        truncated = True
    if truncated:
        suffix = " [output truncated by collector safety limit]"
        detail = f"{detail}{suffix}" if detail else suffix.strip()
    return detail


def _discover_nfs_exports(host: str, timeout_seconds: float) -> NFSExportDiscovery:
    timeout = max(1.0, timeout_seconds * 4)
    try:
        completed = _run_bounded_process(
            ["showmount", "-e", host],
            timeout=timeout,
            stdout_limit=NFS_SHOWMOUNT_MAX_STDOUT_BYTES,
            stderr_limit=NFS_SHOWMOUNT_MAX_STDERR_BYTES,
        )
    except FileNotFoundError:
        return NFSExportDiscovery((), "tool_unavailable", "showmount is not available")
    except subprocess.TimeoutExpired:
        return NFSExportDiscovery((), "timed_out", "showmount timed out")
    except _BoundedProcessDrainError:
        return NFSExportDiscovery(
            (),
            "output_drain_failed",
            "showmount output pipes did not close within the collector safety bound",
            limitations=("showmount_output_drain_failed",),
        )
    except OSError as exc:
        detail = _error_detail(exc)
        return NFSExportDiscovery((), _classify_showmount_failure(detail), detail)

    if completed.returncode != 0:
        detail = (
            _bounded_showmount_detail(
                completed.stderr or completed.stdout,
                truncated=completed.stderr_truncated if completed.stderr else completed.stdout_truncated,
            )
            or f"showmount exit code {completed.returncode}"
        )
        return NFSExportDiscovery((), _classify_showmount_failure(detail), detail)

    stdout = completed.stdout.decode("utf-8", errors="replace")
    parsed = _parse_showmount_exports_bounded(stdout)
    limitations = set(parsed.limitations)
    if completed.stdout_truncated:
        limitations.add("showmount_stdout_limit_reached")
    if completed.stderr_truncated:
        limitations.add("showmount_stderr_limit_reached")
    truncated = bool(parsed.truncated or completed.stdout_truncated or completed.stderr_truncated)
    return NFSExportDiscovery(
        parsed.exports,
        "truncated" if truncated else "complete",
        "showmount export evidence exceeded a collector safety limit" if truncated else None,
        observed_export_lines=parsed.observed_export_lines,
        stdout_truncated=completed.stdout_truncated,
        stderr_truncated=completed.stderr_truncated,
        exports_truncated=parsed.truncated,
        limitations=tuple(sorted(limitations)),
    )


def _error_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    return detail if detail else type(exc).__name__


def _share_info_value(share: object, key: str, default: str = "") -> str:
    try:
        value = share[key]  # type: ignore[index]
    except Exception:
        if isinstance(share, dict):
            value = share.get(key, default)
        else:
            value = getattr(share, key, default)
    text = str(value or default)
    return text.rstrip("\x00")


def _is_disk_share(share: object) -> bool:
    try:
        raw_value = share["shi1_type"]  # type: ignore[index]
    except Exception:
        if isinstance(share, dict):
            raw_value = share.get("shi1_type")
        else:
            raw_value = getattr(share, "shi1_type", None)
    if raw_value is None:
        return True
    try:
        return int(raw_value) & 0xFFFF == 0
    except (TypeError, ValueError):
        return True


def _normalized_extensions(raw_value: str | None) -> set[str] | None:
    if not raw_value:
        return None
    extensions: set[str] = set()
    for raw_extension in raw_value.split(","):
        extension = raw_extension.strip().lower()
        if not extension:
            continue
        extensions.add(extension if extension.startswith(".") else f".{extension}")
    return extensions or None


def _report_detail(args: argparse.Namespace, message: str, *, level: int = 2) -> None:
    reporter = getattr(args, "progress_reporter", None)
    if isinstance(reporter, ProgressReporter):
        reporter.detail(message, level=level)


def _report_notice(args: argparse.Namespace, message: str) -> None:
    if bool(getattr(args, "quiet", False)):
        return
    reporter = getattr(args, "progress_reporter", None)
    if isinstance(reporter, ProgressReporter):
        reporter.detail(message, level=0)
    else:
        print(message, file=sys.stderr)


def _record_error(stats: Stats, lock: threading.Lock, code: str, message: str) -> None:
    with lock:
        stats.errors += 1
        stats.error_codes[code] += 1
        if code not in stats.error_samples:
            stats.error_samples[code] = message


def _emit_error(
    writer: NDJSONWriter,
    run_id: str,
    *,
    severity: str,
    code: str,
    message: str,
    endpoint_key: str | None = None,
    resource_name: str | None = None,
    path: str | None = None,
    hint: str | None = None,
) -> None:
    record: dict[str, object] = {
        "type": "error",
        "run_id": run_id,
        "severity": severity,
        "code": code,
        "message": message,
    }
    if endpoint_key is not None:
        record["endpoint_key"] = endpoint_key
    if resource_name is not None:
        record["resource_name"] = resource_name
    if path is not None:
        record["path"] = path
    if hint is not None:
        record["hint"] = hint
    writer.emit(record)


def _session_error_hint(raw_error: str, auth_method: str) -> str | None:
    upper = raw_error.upper()
    if "STATUS_LOGON_FAILURE" in upper:
        return "Check SMB username/password or hash values and confirm domain/local auth mode."
    if "STATUS_ACCESS_DENIED" in upper:
        if auth_method == "anonymous":
            return "Anonymous session is allowed but not authorized to enumerate this share/path."
        return "Credentials are valid but not authorized for this share or path."
    if "STATUS_BAD_NETWORK_NAME" in upper:
        return "Share name was not found on target. Verify share spelling/casing and include-share values."
    if "STATUS_CONNECTION_REFUSED" in upper:
        return "Target refused tree connect for this share/path. System shares like IPC$ are commonly restricted."
    if "STATUS_ACCOUNT_DISABLED" in upper:
        return "Enable the account or use a different service account."
    if "STATUS_PASSWORD_EXPIRED" in upper:
        return "Rotate password for the scanning account and retry."
    if "STATUS_ACCOUNT_LOCKED_OUT" in upper:
        return "Wait for lockout reset or use an unlocked account."
    if "STATUS_MORE_PROCESSING_REQUIRED" in upper:
        return "Kerberos/NTLM negotiation failed; verify auth flags and credential type."
    if auth_method == "anonymous":
        return "Target blocks anonymous SMB sessions; rerun with authenticated credentials."
    return None


def _is_successful_run(stats: Stats) -> bool:
    return stats.endpoints > 0 or stats.resources > 0 or stats.items > 0 or stats.errors > 0


def _print_scan_failure_summary(
    stats: Stats,
    host_failures: int,
    *,
    reason: str,
    output_path: str | None,
) -> None:
    print(reason, file=sys.stderr)
    if output_path:
        print(f"output not written: {output_path}", file=sys.stderr)
    if host_failures > 0:
        print(f"failed hosts: {host_failures}", file=sys.stderr)
    if stats.error_codes:
        print("error summary:", file=sys.stderr)
        for code, count in stats.error_codes.most_common(10):
            sample = stats.error_samples.get(code, "")
            if sample:
                print(f"  - {code} ({count}): {sample}", file=sys.stderr)
            else:
                print(f"  - {code}: {count}", file=sys.stderr)


def _validate_runtime_dependencies(selected_share_types: set[str]) -> tuple[set[str], list[str], list[str]]:
    disabled: set[str] = set()
    warnings: list[str] = []
    fatals: list[str] = []

    if "smb" in selected_share_types and SMBConnection is None:
        message = "SMB scanning requires the optional dependency `impacket`. Install with `pip install impacket`."
        if selected_share_types == {"smb"}:
            fatals.append(message)
        else:
            disabled.add("smb")
            warnings.append(f"{message} SMB scanning will be skipped.")

    return disabled, warnings, fatals


def scan_host_smb(
    host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock
) -> bool:
    endpoint_key = f"{host}:445"
    attempted_auth = _resolve_smb_auth_method(args)
    if SMBConnection is None:
        message = "SMB scanner unavailable because impacket is not installed."
        _emit_error(
            writer,
            run_id,
            severity="error",
            code="SMB_SCANNER_UNAVAILABLE",
            message=message,
            endpoint_key=endpoint_key,
            hint="Install impacket with `pip install impacket` and rerun.",
        )
        _record_error(stats, lock, "SMB_SCANNER_UNAVAILABLE", message)
        return False

    conn = None
    authenticated = False
    try:
        if getattr(args, "cancel_event", None) is not None and args.cancel_event.is_set():
            return False
        _report_detail(args, f"host {host}: starting SMB ({attempted_auth})")
        conn = SMBConnection(host, host, sess_port=445, timeout=args.timeout)
        lmhash = ""
        nthash = ""
        if args.hashes and ":" in args.hashes:
            lmhash, nthash = args.hashes.split(":", 1)
        if attempted_auth == "kerberos":
            ccache_env_value = getattr(args, "ccache_env_value", None)
            use_cache = bool(ccache_env_value)
            conn.kerberosLogin(
                args.username,
                args.password,
                args.domain,
                lmhash=lmhash,
                nthash=nthash,
                aesKey=None,
                kdcHost=None,
                TGT=None,
                TGS=None,
                useCache=use_cache,
            )
        elif attempted_auth == "ntlm":
            domain = "" if args.local_auth else args.domain
            conn.login(args.username, args.password, domain=domain, lmhash=lmhash, nthash=nthash)
        else:
            conn.login("", "", domain="", lmhash="", nthash="")
        authenticated = True

        if getattr(args, "cancel_event", None) is not None and args.cancel_event.is_set():
            return False

        server_identity = _smb_server_identity(conn, host)
        assessed_identity = _smb_assessed_identity(conn, args, attempted_auth)
        endpoint_record = {
            "type": "endpoint",
            "run_id": run_id,
            "endpoint_key": endpoint_key,
            "ip": host if _is_ip(host) else None,
            "hostname": host if not _is_ip(host) else None,
            "domain": args.domain or None,
            "provider": "smb",
            "provider_endpoint_id": server_identity["provider_endpoint_id"],
            "metadata": {
                "identity_source": server_identity["identity_source"],
                "identity_strength": server_identity["identity_strength"],
                "assessed_identity_fingerprint": assessed_identity["assessed_identity_fingerprint"],
                "session_kind": assessed_identity["session_kind"],
                "session_identity_source": assessed_identity["identity_source"],
                **(
                    {"server_guid": server_identity["server_guid"]}
                    if server_identity.get("server_guid")
                    else {"advertised_names": server_identity.get("advertised_names", [])}
                ),
            },
            "smb": {
                "dialect": _dialect_label(str(conn.getDialect())),
                "signing": _signing_label(conn),
            },
            "auth": {
                "method": attempted_auth,
                "success": True,
            },
        }

        writer.emit(endpoint_record)
        with lock:
            stats.endpoints += 1

        # Some SMB servers accept invalid or unauthorized credentials by
        # silently downgrading the connection to a guest session.  Treating
        # that observation as the requested identity can turn shares hidden
        # from guest into false, confirmed removals during run comparison.
        if attempted_auth in {"ntlm", "kerberos"} and assessed_identity["session_kind"] == "guest":
            message = (
                f"SMB {attempted_auth} authentication was downgraded to a guest session; "
                "results do not represent the requested identity."
            )
            _emit_error(
                writer,
                run_id,
                severity="warn",
                code="SMB_AUTH_GUEST_FALLBACK",
                message=message,
                endpoint_key=endpoint_key,
                hint=("Verify the credentials and server guest-fallback policy, then rerun before comparing coverage."),
            )
            _record_error(stats, lock, "SMB_AUTH_GUEST_FALLBACK", message)
            with lock:
                stats.structural_coverage_gaps += 1

        excluded_shares = {s.upper() for s in args.exclude_share}
        include_shares = list(
            dict.fromkeys(str(share).strip() for share in getattr(args, "include_share", []) if str(share).strip())
        )
        exclude_path_regex = getattr(args, "exclude_path_pattern", None)
        if exclude_path_regex is None and args.exclude_path_regex:
            exclude_path_regex = re.compile(args.exclude_path_regex)
        extensions = _normalized_extensions(args.extensions_only)

        if include_shares:
            shares = [{"shi1_netname": f"{name}\x00", "shi1_remark": "user-specified share"} for name in include_shares]
        else:
            try:
                shares = conn.listShares()
            except SessionError as exc:
                detail = _error_detail(exc)
                classification = _classify_smb_probe_failure(exc)
                access_denied = (
                    classification.outcome == "denied"
                    and classification.reason_code in SMB_SHARE_ENUMERATION_DENIAL_REASONS
                )
                protocol_suffix = f", status={classification.protocol_status}" if classification.protocol_status else ""
                message = (
                    "SMB share enumeration denied"
                    if access_denied
                    else f"SMB share enumeration failed ({classification.reason_code}{protocol_suffix})"
                )
                message = f"{message}: {detail}"
                if access_denied:
                    hint = (
                        "Anonymous session established but share enumeration is blocked. Use SMB credentials or pass known names with --include-share."
                        if attempted_auth == "anonymous"
                        else "Credentials authenticated but are not allowed to enumerate shares. Use a higher-privilege account or --include-share."
                    )
                elif classification.transport_fatal:
                    hint = "The authenticated SMB session became unusable; verify network/server health and retry the host."
                else:
                    hint = "The SMB server rejected or could not process share enumeration; verify server compatibility and retry, or pass known names with --include-share."
                code = "LIST_SHARES_DENIED" if access_denied else "LIST_SHARES_FAILED"
                _emit_error(
                    writer,
                    run_id,
                    severity="warn" if access_denied else "error",
                    code=code,
                    message=message,
                    endpoint_key=endpoint_key,
                    hint=hint,
                )
                _record_error(stats, lock, code, message)
                if access_denied:
                    with lock:
                        stats.structural_coverage_gaps += 1
                # A valid authenticated endpoint with explicitly denied share
                # enumeration is useful partial evidence. Session, transport,
                # and other protocol failures leave the host scan incomplete.
                return access_denied

        eligible_shares = []
        seen_shares: set[str] = set()
        for share in shares:
            share_name = _share_info_value(share, "shi1_netname")
            share_key = share_name.casefold()
            if (
                not share_name
                or share_name.upper() in excluded_shares
                or share_key in seen_shares
                or (not include_shares and not _is_disk_share(share))
            ):
                continue
            seen_shares.add(share_key)
            eligible_shares.append(share)

        if not eligible_shares:
            return True

        def _handle_list_error(
            share_name: str,
            denied_path: str,
            exc: BaseException,
            capabilities: dict[str, dict[str, object]],
        ) -> None:
            detail = _error_detail(exc)
            message = f"SMB share listing failed for {share_name}: {detail}"
            classification = _classify_smb_probe_failure(exc)
            _record_capability(
                capabilities,
                "list",
                classification.outcome,
                reason_code=classification.reason_code,
                protocol_status=classification.protocol_status,
                method="directory_listing",
                scope="directory",
            )
            hint = _session_error_hint(detail, attempted_auth)
            if classification.reason_code == "dfs_referral_required":
                hint = (
                    "The path requires a DFS referral. This collector preserves the logical namespace but does not "
                    "forward credentials to referral targets; assess those targets explicitly under an approved "
                    "trust policy."
                )
            _emit_error(
                writer,
                run_id,
                severity="warn",
                code="LIST_SESSION_ERROR",
                message=message,
                endpoint_key=endpoint_key,
                resource_name=share_name,
                path=denied_path,
                hint=hint,
            )
            _record_error(stats, lock, "LIST_SESSION_ERROR", message)

        def _handle_list_limit(share_name: str, inspected: int, emitted: int) -> None:
            message = (
                f"SMB share listing reached inspection cap for {share_name} (inspected={inspected}, emitted={emitted})."
            )
            _emit_error(
                writer,
                run_id,
                severity="warn",
                code="LIST_LIMIT_REACHED",
                message=message,
                endpoint_key=endpoint_key,
                resource_name=share_name,
                path="\\",
                hint="Increase --max-entries-per-share for deeper coverage of large shares.",
            )
            _record_error(stats, lock, "LIST_LIMIT_REACHED", message)

        for share in eligible_shares:
            if getattr(args, "cancel_event", None) is not None and args.cancel_event.is_set():
                break
            share_name = _share_info_value(share, "shi1_netname")
            remark = _share_info_value(share, "shi1_remark")
            _report_detail(args, f"host {host}: listing SMB share {share_name}")
            capabilities = _new_access_capabilities()
            probe_limit = max(0, int(getattr(args, "access_probe_limit", 3)))
            # parse_args always supplies the CLI default (root). Treat a
            # programmatic Namespace created by older integrations as legacy
            # mode until it explicitly opts into schema-v2 permission records.
            permission_mode = str(getattr(args, "smb_permissions", "none") or "none")
            permission_sample_limit = max(
                0,
                min(
                    SMB_PERMISSION_MAX_SAMPLE_LIMIT,
                    int(
                        getattr(
                            args,
                            "smb_permission_sample_limit",
                            SMB_PERMISSION_DEFAULT_SAMPLE_LIMIT,
                        )
                    ),
                ),
            )
            cancel_event = getattr(args, "cancel_event", None)
            share_was_enumerated = not bool(include_shares)
            provider_resource_id = _stable_fingerprint(
                "smb-share:v1", server_identity["provider_endpoint_id"], share_name
            )
            permission_summary: dict[str, object] | None = None
            if permission_mode != "none":
                permission_summary = {
                    "semantics": SMB_PERMISSION_SEMANTICS,
                    "permission_surface": SMB_PERMISSION_SURFACE,
                    "mode": permission_mode,
                    "assessment_state": "in_progress",
                    "effective_access_status": "not_computed",
                    "negative_conclusion_supported": False,
                    "assessments": 0,
                    "entries": 0,
                    "truncated": False,
                }
            resource_record = {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": endpoint_key,
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": share_name,
                "remark": remark,
                "provider": "smb",
                "provider_resource_id": provider_resource_id,
                "metadata": {
                    "server_identity": server_identity["provider_endpoint_id"],
                    "server_identity_strength": server_identity["identity_strength"],
                    "dfs": _smb_dfs_namespace_evidence(conn, None),
                },
                "access_level": "unknown",
                "access_capabilities": _access_capability_snapshot(
                    capabilities,
                    probe_limit=probe_limit,
                    partial=True,
                    complete=False,
                    assessment_summary="not_assessed",
                    assessment_reason="pending",
                    finalized=False,
                    share_presence="advertised" if share_was_enumerated else "unverified",
                    assessed_identity_fingerprint=assessed_identity["assessed_identity_fingerprint"],
                    session_kind=assessed_identity["session_kind"],
                    identity_source=assessed_identity["identity_source"],
                ),
            }
            if permission_summary is not None:
                resource_record["permission_summary"] = permission_summary
            writer.emit(resource_record)
            with lock:
                stats.resources += 1

            directory_samples: list[str] = []
            file_samples: list[str] = []
            probe_directory_seeds: list[str] = []
            probe_directory_seed_keys: set[str] = set()
            directory_candidates_seen = 0
            file_candidates_seen = 0
            listing_truncated = False
            workflow_finished = False
            workflow_reason: str | None = None
            tree_id = None
            dfs_referral_required_observed = False
            dfs_referral_protocol_status: str | None = None
            dfs_evidence = _smb_dfs_namespace_evidence(conn, tree_id)
            listed_directory_paths: set[str] = set()
            probe_circuit = _SMBProbeCircuit()
            permission_selector = _SMBPermissionCandidateSelector(
                provider_resource_id,
                permission_sample_limit if permission_mode == "sampled" else 0,
            )

            def _handle_list_success(listed_path: str) -> None:
                listed_directory_paths.add(_smb_handle_path(listed_path).casefold())
                _record_capability(
                    capabilities,
                    "list",
                    "allowed",
                    reason_code="granted",
                    method="directory_listing",
                    scope="directory",
                )

            def _handle_share_list_error(denied_path: str, exc: BaseException) -> None:
                nonlocal dfs_referral_protocol_status, dfs_referral_required_observed
                classification = _classify_smb_probe_failure(exc)
                if classification.reason_code == "dfs_referral_required":
                    dfs_referral_required_observed = True
                    dfs_referral_protocol_status = classification.protocol_status
                if classification.transport_fatal:
                    probe_circuit.transport_failed = True
                    probe_circuit.reason_code = classification.reason_code
                _handle_list_error(
                    share_name,
                    denied_path,
                    exc,
                    capabilities,
                )

            def _capture_probe_candidate(path: str, is_directory: bool) -> None:
                nonlocal directory_candidates_seen, file_candidates_seen
                if is_directory:
                    directory_candidates_seen += 1
                    if len(directory_samples) < probe_limit:
                        directory_samples.append(path)
                    return
                file_candidates_seen += 1
                if len(file_samples) < probe_limit:
                    file_samples.append(path)

            def _capture_probe_directory_seed(path: str) -> None:
                path_key = _smb_handle_path(path).casefold()
                if len(probe_directory_seeds) < probe_limit and path_key not in probe_directory_seed_keys:
                    probe_directory_seeds.append(path)
                    probe_directory_seed_keys.add(path_key)

            def _handle_share_list_limit(inspected: int, emitted: int) -> None:
                nonlocal listing_truncated
                listing_truncated = True
                _handle_list_limit(share_name, inspected, emitted)

            try:
                connect_tree = getattr(conn, "connectTree", None)
                if callable(connect_tree) and not (cancel_event is not None and cancel_event.is_set()):
                    try:
                        tree_id = connect_tree(share_name)
                    except (
                        SessionError,
                        NetBIOSError,
                        NetBIOSTimeout,
                        socket.timeout,
                        TimeoutError,
                        OSError,
                    ) as exc:
                        classification = _classify_smb_probe_failure(exc)
                        if classification.reason_code == "dfs_referral_required":
                            dfs_referral_required_observed = True
                            dfs_referral_protocol_status = classification.protocol_status
                        outcome = classification.outcome
                        _record_capability(
                            capabilities,
                            "tree_connect",
                            outcome,
                            reason_code=classification.reason_code,
                            protocol_status=classification.protocol_status,
                            method="tree_connect",
                            scope="share",
                        )
                        if classification.transport_fatal:
                            probe_circuit.transport_failed = True
                        if classification.abort_remaining_probes:
                            probe_circuit.probes_aborted = True
                        if classification.transport_fatal or classification.abort_remaining_probes:
                            probe_circuit.reason_code = classification.reason_code
                        message = f"SMB tree connection failed for {share_name}: {_error_detail(exc)}"
                        code = "TREE_CONNECT_DENIED" if outcome == "denied" else "TREE_CONNECT_FAILED"
                        hint = _session_error_hint(_error_detail(exc), attempted_auth)
                        if classification.reason_code == "dfs_referral_required":
                            hint = (
                                "The logical share requires a DFS referral. Referral targets are not followed or "
                                "given credentials automatically; assess approved physical targets explicitly."
                            )
                        _emit_error(
                            writer,
                            run_id,
                            severity="warn",
                            code=code,
                            message=message,
                            endpoint_key=endpoint_key,
                            resource_name=share_name,
                            path="\\",
                            hint=hint,
                        )
                        _record_error(stats, lock, code, message)
                    else:
                        dfs_evidence = _smb_dfs_namespace_evidence(conn, tree_id)
                        _record_capability(
                            capabilities,
                            "tree_connect",
                            "allowed",
                            reason_code="granted",
                            method="tree_connect",
                            scope="share",
                        )
                elif not callable(connect_tree):
                    _mark_capability_not_tested(capabilities, "tree_connect", "probe_method_unavailable")

                # Request access masks on existing objects only. FILE_OPEN plus
                # these narrow masks cannot create, modify, take ownership, or
                # delete anything; it asks the server whether the right exists.
                if tree_id is not None and probe_limit > 0:
                    _probe_smb_directory_access(
                        conn,
                        tree_id,
                        "",
                        capabilities,
                        cancel_event,
                        probe_circuit,
                    )

                for entry in list_share_entries(
                    conn,
                    share_name,
                    max_depth=max(1, args.max_depth),
                    max_entries=max(1, args.max_entries_per_share),
                    exclude_path_regex=exclude_path_regex,
                    extensions=extensions,
                    on_list_error=_handle_share_list_error,
                    on_limit_reached=_handle_share_list_limit,
                    on_list_success=_handle_list_success,
                    on_probe_candidate=_capture_probe_candidate,
                    on_probe_directory_seed=_capture_probe_directory_seed,
                    cancel_event=cancel_event,
                ):
                    permission_selector.consider(entry["path"], bool(entry["is_dir"]))
                    writer.emit(
                        {
                            "type": "item",
                            "run_id": run_id,
                            "endpoint_key": endpoint_key,
                            "share_type": "smb",
                            "resource_type": "smb_share",
                            "resource_name": share_name,
                            **entry,
                        }
                    )
                    with lock:
                        stats.items += 1
                if (
                    tree_id is not None
                    and probe_limit > 0
                    and len(file_samples) < probe_limit
                    and not probe_circuit.blocked
                    and not (cancel_event is not None and cancel_event.is_set())
                ):
                    (
                        nested_directory_candidates,
                        nested_file_candidates,
                        nested_listing_truncated,
                        nested_inspected,
                    ) = _discover_smb_probe_candidates(
                        conn,
                        share_name,
                        directory_seeds=list(probe_directory_seeds),
                        directory_samples=directory_samples,
                        file_samples=file_samples,
                        probe_limit=probe_limit,
                        max_entries=max(1, args.max_entries_per_share),
                        exclude_path_regex=exclude_path_regex,
                        already_listed_paths=listed_directory_paths,
                        on_list_error=_handle_share_list_error,
                        on_list_success=_handle_list_success,
                        cancel_event=cancel_event,
                        probe_circuit=probe_circuit,
                    )
                    directory_candidates_seen += nested_directory_candidates
                    file_candidates_seen += nested_file_candidates
                    if nested_listing_truncated and not listing_truncated:
                        _handle_share_list_limit(nested_inspected, 0)
                if tree_id is not None and probe_limit > 0:
                    for directory_path in directory_samples:
                        if probe_circuit.blocked:
                            break
                        _probe_smb_directory_access(
                            conn,
                            tree_id,
                            directory_path,
                            capabilities,
                            cancel_event,
                            probe_circuit,
                        )
                    for file_path in file_samples:
                        if probe_circuit.blocked:
                            break
                        _probe_smb_file_access(
                            conn,
                            tree_id,
                            file_path,
                            capabilities,
                            cancel_event,
                            probe_circuit,
                        )
                workflow_finished = not (cancel_event is not None and cancel_event.is_set())
            except ArtifactSpoolLimitError:
                # Output exhaustion is run-fatal. Do not relabel it as a share
                # protocol failure or keep probing after records cannot be kept.
                raise
            except SessionError as exc:
                classification = _classify_smb_probe_failure(exc)
                workflow_reason = classification.reason_code
                if classification.transport_fatal:
                    probe_circuit.transport_failed = True
                if classification.abort_remaining_probes:
                    probe_circuit.probes_aborted = True
                if classification.transport_fatal or classification.abort_remaining_probes:
                    probe_circuit.reason_code = classification.reason_code
                _handle_list_error(share_name, "\\", exc, capabilities)
            except (socket.timeout, TimeoutError) as exc:
                message = f"SMB share listing timed out for {share_name}."
                classification = _classify_smb_probe_failure(exc)
                workflow_reason = classification.reason_code
                probe_circuit.transport_failed = True
                probe_circuit.reason_code = classification.reason_code
                _record_capability(
                    capabilities,
                    "list",
                    "inconclusive",
                    reason_code=classification.reason_code,
                    method="directory_listing",
                    scope="directory",
                )
                _emit_error(
                    writer,
                    run_id,
                    severity="warn",
                    code="LIST_TIMEOUT",
                    message=message,
                    endpoint_key=endpoint_key,
                    resource_name=share_name,
                    path="\\",
                    hint="Increase --timeout or reduce --workers for congested networks.",
                )
                _record_error(stats, lock, "LIST_TIMEOUT", message)
            except OSError as exc:
                if bool(getattr(writer, "write_failed", False)):
                    raise
                detail = _error_detail(exc)
                message = f"SMB share listing IO failure for {share_name}: {detail}"
                classification = _classify_smb_probe_failure(exc)
                workflow_reason = classification.reason_code
                probe_circuit.transport_failed = True
                probe_circuit.reason_code = classification.reason_code
                _record_capability(
                    capabilities,
                    "list",
                    "inconclusive",
                    reason_code=classification.reason_code,
                    method="directory_listing",
                    scope="directory",
                )
                _emit_error(
                    writer,
                    run_id,
                    severity="warn",
                    code="LIST_IO_ERROR",
                    message=message,
                    endpoint_key=endpoint_key,
                    resource_name=share_name,
                    path="\\",
                    hint="Check share permissions and target SMB server health.",
                )
                _record_error(stats, lock, "LIST_IO_ERROR", message)
            except (NetBIOSError, NetBIOSTimeout) as exc:
                classification = _classify_smb_probe_failure(exc)
                workflow_reason = classification.reason_code
                probe_circuit.transport_failed = True
                probe_circuit.reason_code = classification.reason_code
                _handle_list_error(share_name, "\\", exc, capabilities)
            except Exception as exc:
                if bool(getattr(writer, "write_failed", False)):
                    raise
                # Isolate a malformed appliance response or unexpected Impacket
                # parsing failure to this share. The final resource record below
                # preserves the degraded assessment and later shares still run.
                detail = _error_detail(exc)
                workflow_reason = "collector_error"
                message = f"SMB share assessment failed for {share_name}: {detail}"
                _emit_error(
                    writer,
                    run_id,
                    severity="warn",
                    code="SMB_SHARE_ASSESSMENT_FAILED",
                    message=message,
                    endpoint_key=endpoint_key,
                    resource_name=share_name,
                    path="\\",
                    hint="Retry with higher verbosity; verify SMB server compatibility if the failure repeats.",
                )
                _record_error(stats, lock, "SMB_SHARE_ASSESSMENT_FAILED", message)
            finally:
                try:
                    if permission_mode != "none" and not bool(getattr(writer, "write_failed", False)):
                        permission_targets: list[tuple[str, bool, str]] = [("", True, "share_root")]
                        if permission_mode == "sampled":
                            permission_targets.extend(
                                (path, is_directory, "deterministic_sample")
                                for path, is_directory in permission_selector.selected()
                            )
                        selected_count = len(permission_targets)
                        assessment_records: list[dict[str, object]] = []
                        permission_entries_emitted = 0
                        transport_aborted = False
                        remaining_entry_budget = SMB_PERMISSION_MAX_ENTRIES_PER_SHARE
                        for subject_path, is_directory, selection_scope in permission_targets:
                            if transport_aborted:
                                break
                            unavailable_reason = None
                            permission_tree_id = tree_id
                            if cancel_event is not None and cancel_event.is_set():
                                permission_tree_id = None
                                unavailable_reason = "cancelled"
                            elif probe_circuit.transport_failed:
                                permission_tree_id = None
                                unavailable_reason = "transport_aborted"
                            elif probe_circuit.probes_aborted:
                                permission_tree_id = None
                                unavailable_reason = probe_circuit.reason_code or "tree_session_invalid"
                            assessment, permission_entries, transport_fatal = _build_smb_permission_records(
                                conn,
                                permission_tree_id,
                                run_id=run_id,
                                endpoint_key=endpoint_key,
                                resource_name=share_name,
                                provider_resource_id=provider_resource_id,
                                subject_path=subject_path,
                                is_directory=is_directory,
                                assessed_identity=assessed_identity,
                                selection_scope=selection_scope,
                                entry_budget=remaining_entry_budget,
                                root_path_hint=probe_circuit.root_path,
                                cancel_event=cancel_event,
                                unavailable_reason=unavailable_reason,
                            )
                            writer.emit(assessment)
                            for permission_entry in permission_entries:
                                writer.emit(permission_entry)
                            assessment_records.append(assessment)
                            remaining_entry_budget -= len(permission_entries)
                            permission_entries_emitted += len(permission_entries)
                            with lock:
                                stats.permission_assessments += 1
                                stats.permission_entries += len(permission_entries)
                            transport_aborted = transport_fatal

                        complete_count = sum(
                            record.get("assessment_state") == "complete" for record in assessment_records
                        )
                        partial_count = sum(
                            record.get("assessment_state") == "partial" for record in assessment_records
                        )
                        failed_count = sum(record.get("assessment_state") == "failed" for record in assessment_records)
                        if assessment_records and complete_count == len(assessment_records) == selected_count:
                            permission_state = "complete"
                        elif assessment_records and (complete_count or partial_count):
                            permission_state = "partial"
                        else:
                            permission_state = "failed"
                        permission_summary_hash = hashlib.sha256(
                            "\n".join(
                                f"{record.get('assessment_key')}:{record.get('evidence_hash') or ''}:"
                                f"{record.get('assessment_state')}"
                                for record in assessment_records
                            ).encode("utf-8")
                        ).hexdigest()
                        permission_summary = {
                            "semantics": SMB_PERMISSION_SEMANTICS,
                            "permission_surface": SMB_PERMISSION_SURFACE,
                            "mode": permission_mode,
                            "assessment_state": permission_state,
                            "selection_coverage": (
                                "exhaustive_for_scope" if permission_mode == "root" else "deterministic_sample"
                            ),
                            "retrieval_coverage": (
                                "complete"
                                if permission_state == "complete"
                                else ("partial" if complete_count or partial_count else "failed")
                            ),
                            "effective_access_status": "not_computed",
                            "negative_conclusion_supported": (
                                permission_state == "complete" and permission_mode == "root"
                            ),
                            "subjects_selected": selected_count,
                            "assessments": len(assessment_records),
                            "complete_assessments": complete_count,
                            "partial_assessments": partial_count,
                            "failed_assessments": failed_count,
                            "entries": permission_entries_emitted,
                            "truncated": bool(
                                len(assessment_records) < selected_count
                                or any(bool(record.get("truncated")) for record in assessment_records)
                            ),
                            "evidence_hash": permission_summary_hash,
                        }
                        if permission_state != "complete":
                            permission_message = (
                                f"SMB direct ACL evidence is {permission_state} for {share_name}: "
                                f"selected={selected_count}, assessed={len(assessment_records)}, "
                                f"partial={partial_count}, failed={failed_count}."
                            )
                            _emit_error(
                                writer,
                                run_id,
                                severity="warn",
                                code="SMB_PERMISSION_ASSESSMENT_INCOMPLETE",
                                message=permission_message,
                                endpoint_key=endpoint_key,
                                resource_name=share_name,
                                path="\\",
                                hint=(
                                    "Review permission_assessment errors and coverage; a failed ACL read "
                                    "does not prove content access is denied."
                                ),
                            )
                            _record_error(
                                stats,
                                lock,
                                "SMB_PERMISSION_ASSESSMENT_INCOMPLETE",
                                permission_message,
                            )
                finally:
                    if tree_id is not None:
                        disconnect_tree = getattr(conn, "disconnectTree", None)
                        if callable(disconnect_tree):
                            try:
                                disconnect_tree(tree_id)
                            except Exception:
                                pass

            interrupted = bool(cancel_event is not None and cancel_event.is_set())
            if int(capabilities["tree_connect"].get("attempted", 0)) <= 0:
                _mark_capability_not_tested(
                    capabilities,
                    "tree_connect",
                    "cancelled" if interrupted else workflow_reason or "not_reached",
                )
            if int(capabilities["list"].get("attempted", 0)) <= 0:
                if interrupted:
                    list_not_tested_reason = "cancelled"
                elif probe_circuit.transport_failed:
                    list_not_tested_reason = "transport_aborted"
                elif probe_circuit.probes_aborted:
                    list_not_tested_reason = probe_circuit.reason_code or "probe_aborted"
                else:
                    list_not_tested_reason = workflow_reason or "not_reached"
                _mark_capability_not_tested(
                    capabilities,
                    "list",
                    list_not_tested_reason,
                )

            explicit_probe_capabilities = tuple(
                capability for capability in SMB_CAPABILITY_NAMES if capability not in {"tree_connect", "list"}
            )
            for capability in explicit_probe_capabilities:
                if probe_limit <= 0:
                    reason = "probe_disabled"
                elif interrupted:
                    reason = "cancelled"
                elif probe_circuit.transport_failed:
                    reason = "transport_aborted"
                elif probe_circuit.probes_aborted:
                    reason = probe_circuit.reason_code or "probe_aborted"
                elif workflow_reason:
                    reason = workflow_reason
                elif tree_id is None:
                    reason = "tree_unavailable"
                elif capability in {"read_file", "modify_file"} or (capability == "delete" and len(file_samples) <= 0):
                    reason = "no_visible_file_candidate"
                else:
                    reason = "no_directory_candidate"
                _mark_capability_not_tested(capabilities, capability, reason)

            assessment_summary, assessment_reason, assessment_degraded = _smb_access_assessment(
                capabilities,
                probe_limit=probe_limit,
                workflow_finished=workflow_finished,
                interrupted=interrupted,
                workflow_reason=workflow_reason or probe_circuit.reason_code,
                transport_failed=probe_circuit.transport_failed,
                listing_truncated=listing_truncated,
                file_samples=len(file_samples),
            )
            share_presence = _smb_share_presence(capabilities, enumerated=share_was_enumerated)
            final_access_level = _legacy_access_level(capabilities)
            dfs_evidence = _smb_dfs_namespace_evidence(
                conn,
                None,
                referral_required_observed=dfs_referral_required_observed,
                referral_protocol_status=dfs_referral_protocol_status,
                observed_tree_connect_capability=str(dfs_evidence.get("tree_connect_capability") or "unavailable"),
            )
            # These are observations from a bounded sample, never a complete
            # effective-permissions calculation for every object in the share.
            capability_partial = True
            writer.emit(
                {
                    **resource_record,
                    "metadata": {**resource_record["metadata"], "dfs": dfs_evidence},
                    "access_level": final_access_level,
                    **({"permission_summary": permission_summary} if permission_summary is not None else {}),
                    "access_capabilities": _access_capability_snapshot(
                        capabilities,
                        probe_limit=probe_limit,
                        partial=capability_partial,
                        complete=workflow_finished,
                        directory_samples=len(directory_samples),
                        file_samples=len(file_samples),
                        directory_candidates_seen=directory_candidates_seen,
                        file_candidates_seen=file_candidates_seen,
                        listing_truncated=listing_truncated,
                        assessment_summary=assessment_summary,
                        assessment_reason=assessment_reason,
                        finalized=True,
                        degraded=assessment_degraded,
                        transport_failed=probe_circuit.transport_failed,
                        probes_aborted=probe_circuit.probes_aborted,
                        probe_abort_reason=(probe_circuit.reason_code if probe_circuit.probes_aborted else None),
                        share_presence=share_presence,
                        assessed_identity_fingerprint=assessed_identity["assessed_identity_fingerprint"],
                        session_kind=assessed_identity["session_kind"],
                        identity_source=assessed_identity["identity_source"],
                    ),
                }
            )
            _report_detail(
                args,
                f"host {host}: finished SMB share {share_name} (access={final_access_level}, probes={probe_limit})",
            )
        return True

    except socket.gaierror as exc:
        detail = _error_detail(exc)
        code = "SMB_DNS_FAILED"
        message = f"SMB target name resolution failed: {detail}"
        hint = "Use an IP address or fix DNS records for the host."
    except ConnectionRefusedError as exc:
        detail = _error_detail(exc)
        code = "SMB_PORT_CLOSED"
        message = f"SMB tcp/445 refused connection: {detail}"
        hint = "Ensure SMB service is listening and firewall rules allow tcp/445."
    except ConnectionResetError as exc:
        detail = _error_detail(exc)
        code = "SMB_CONNECTION_RESET"
        message = f"SMB connection reset by peer: {detail}"
        hint = "Target reset the session; retry and verify network middleboxes."
    except (socket.timeout, TimeoutError):
        code = "SMB_TIMEOUT"
        message = f"SMB connection to {host}:445 timed out."
        hint = "Host may be down or filtered; increase --timeout for slow networks."
    except SessionError as exc:
        detail = _error_detail(exc)
        code = "SMB_AUTH_FAILED"
        message = f"SMB {attempted_auth} session failed: {detail}"
        hint = _session_error_hint(detail, attempted_auth)
    except NetBIOSTimeout as exc:
        detail = _error_detail(exc)
        code = "SMB_TIMEOUT"
        message = f"SMB NETBIOS session timed out: {detail}"
        hint = "Increase --timeout, reduce --workers, and verify target SMB responsiveness."
    except NetBIOSError as exc:
        detail = _error_detail(exc)
        code = "SMB_NETWORK_FAILED"
        message = f"SMB NETBIOS transport error: {detail}"
        hint = "Verify SMB connectivity and check for transport-level resets or middlebox interference."
    except (OSError, ConnectionError) as exc:
        if bool(getattr(writer, "write_failed", False)):
            raise
        detail = _error_detail(exc)
        code = "SMB_NETWORK_FAILED"
        message = f"SMB network failure: {detail}"
        hint = "Verify route, firewall rules, and SMB service availability."
    except (TypeError, ValueError) as exc:
        detail = _error_detail(exc)
        code = "SMB_INPUT_INVALID"
        message = f"SMB scan input is invalid: {detail}"
        hint = "Verify CLI arguments for credentials and filters."
    finally:
        if conn is not None:
            if authenticated:
                try:
                    conn.logoff()
                except Exception:
                    pass
            close_connection = getattr(conn, "close", None)
            if callable(close_connection):
                try:
                    close_connection()
                except Exception:
                    pass

    _emit_error(
        writer,
        run_id,
        severity="error",
        code=code,
        message=message,
        endpoint_key=endpoint_key,
        hint=hint,
    )
    _record_error(stats, lock, code, message)
    return False


def scan_host_nfs(
    host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock
) -> bool:
    endpoint_key = f"{host}:2049"

    if getattr(args, "cancel_event", None) is not None and args.cancel_event.is_set():
        return False
    _report_detail(args, f"host {host}: starting NFS discovery")
    try:
        protocol_probe = _probe_nfs_v4_null(host, args.timeout)
    except socket.gaierror as exc:
        detail = _error_detail(exc)
        message = f"NFS target name resolution failed: {detail}"
        _emit_error(
            writer,
            run_id,
            severity="error",
            code="NFS_DNS_FAILED",
            message=message,
            endpoint_key=endpoint_key,
            hint="Use an IP address or fix DNS for this host.",
        )
        _record_error(stats, lock, "NFS_DNS_FAILED", message)
        return False
    except (socket.timeout, TimeoutError):
        message = f"NFS connection to {host}:2049 timed out."
        _emit_error(
            writer,
            run_id,
            severity="error",
            code="NFS_TIMEOUT",
            message=message,
            endpoint_key=endpoint_key,
            hint="Host may be down or filtered; increase --timeout for slow links.",
        )
        _record_error(stats, lock, "NFS_TIMEOUT", message)
        return False
    except ConnectionRefusedError as exc:
        detail = _error_detail(exc)
        message = f"NFS tcp/2049 refused connection: {detail}"
        _emit_error(
            writer,
            run_id,
            severity="error",
            code="NFS_PORT_CLOSED",
            message=message,
            endpoint_key=endpoint_key,
            hint="Ensure NFS service is enabled and firewall allows tcp/2049.",
        )
        _record_error(stats, lock, "NFS_PORT_CLOSED", message)
        return False
    except OSError as exc:
        detail = _error_detail(exc)
        message = f"NFS connectivity failure: {detail}"
        _emit_error(
            writer,
            run_id,
            severity="error",
            code="NFS_CONNECT_FAILED",
            message=message,
            endpoint_key=endpoint_key,
            hint="Verify network path and NFS daemon availability.",
        )
        _record_error(stats, lock, "NFS_CONNECT_FAILED", message)
        return False

    if getattr(args, "cancel_event", None) is not None and args.cancel_event.is_set():
        return False

    discovery = _discover_nfs_exports(host, args.timeout)
    # Retain compatibility with private test/integration shims written for the
    # previous tuple contract while all collector-produced results now carry a
    # concrete status.
    if isinstance(discovery, tuple):
        exports, legacy_error = discovery
        discovery = NFSExportDiscovery(
            tuple(exports),
            _classify_showmount_failure(str(legacy_error)) if legacy_error else "complete",
            str(legacy_error) if legacy_error else None,
        )

    service_status = protocol_probe.service_status
    if discovery.status == "complete" and service_status != "nfs_v4_confirmed":
        service_status = "nfs_advertisement_confirmed"

    # Only an authoritative NFS program version range that excludes v4 lets
    # legacy export enumeration stand as the available namespace view. Every
    # supported or indeterminate v4 response leaves its pseudo-filesystem
    # unenumerated and must keep structural comparison partial.
    v4_namespace_unassessed = protocol_probe.status != "version_not_supported"
    structural_coverage = "advertised_exports_only" if discovery.status == "complete" else "partial"
    limitations = ["access_not_assessed", "content_not_enumerated", "exports_are_advertisements_only"]
    if v4_namespace_unassessed:
        structural_coverage = "partial"
        limitations.append("nfs_v4_pseudofilesystem_not_enumerated")
    limitations.extend(discovery.limitations)

    writer.emit(
        {
            "type": "endpoint",
            "run_id": run_id,
            "endpoint_key": endpoint_key,
            "ip": host if _is_ip(host) else None,
            "hostname": host if not _is_ip(host) else None,
            "domain": args.domain or None,
            "nfs": {
                "port": 2049,
                "transport_status": protocol_probe.transport_status,
                "service_status": service_status,
                "protocol_probe": protocol_probe.public_metadata(),
                "export_discovery": {
                    "method": "showmount_exports",
                    "status": discovery.status,
                    "export_count": len(discovery.exports),
                    "observed_export_lines": discovery.observed_export_lines,
                    "stdout_truncated": discovery.stdout_truncated,
                    "stderr_truncated": discovery.stderr_truncated,
                    "exports_truncated": discovery.exports_truncated,
                    "limits": {
                        "stdout_bytes": NFS_SHOWMOUNT_MAX_STDOUT_BYTES,
                        "stderr_bytes": NFS_SHOWMOUNT_MAX_STDERR_BYTES,
                        "exports": NFS_SHOWMOUNT_MAX_EXPORTS,
                        "export_path_bytes": NFS_SHOWMOUNT_MAX_EXPORT_PATH_BYTES,
                    },
                },
                "structural_coverage": structural_coverage,
                "limitations": list(dict.fromkeys(limitations)),
            },
            "auth": {
                "method": "not_assessed",
                "success": None,
                "reason": "NFS NULL and export-discovery calls do not authenticate filesystem access",
            },
        }
    )
    with lock:
        stats.endpoints += 1

    if protocol_probe.status not in {"supported", "version_not_supported"}:
        message = (
            f"NFSv4 protocol confirmation on {host}:2049 was indeterminate "
            f"({protocol_probe.status}); TCP reachability is retained without claiming NFS access."
        )
        _emit_error(
            writer,
            run_id,
            severity="warn",
            code="NFS_V4_PROBE_INDETERMINATE",
            message=message,
            endpoint_key=endpoint_key,
            hint="Verify the service on tcp/2049; legacy NFS exports may still be discovered through mountd.",
        )
        _record_error(stats, lock, "NFS_V4_PROBE_INDETERMINATE", message)

    if discovery.status != "complete":
        if discovery.status == "mount_protocol_unavailable" and protocol_probe.status == "supported":
            code = "NFS_V4_NAMESPACE_NOT_ENUMERATED"
            message = (
                f"NFSv4 responded on {host}:2049, but the legacy mount protocol did not advertise exports; "
                "the NFSv4 pseudo-filesystem remains unenumerated."
            )
            hint = "Supply known export paths to a separate read-only mount assessment; showmount cannot enumerate NFSv4-only namespaces."
        elif discovery.status == "truncated":
            code = "NFS_EXPORT_ENUM_TRUNCATED"
            message = (
                f"Advertised NFS export enumeration on {host} exceeded a collector safety limit; "
                f"{len(discovery.exports)} bounded export record(s) were retained."
            )
            hint = "Split assessment scope or review the reported showmount/export limits before raising them in code."
        else:
            code = "NFS_EXPORT_ENUM_FAILED"
            detail = discovery.detail or discovery.status
            message = f"Failed to enumerate advertised NFS exports on {host}: {detail}"
            hint = "Install and verify `showmount`, and ensure rpcbind/mountd access from this host."
        _emit_error(
            writer,
            run_id,
            severity="warn",
            code=code,
            message=message,
            endpoint_key=endpoint_key,
            hint=hint,
        )
        _record_error(stats, lock, code, message)
        with lock:
            stats.structural_coverage_gaps += 1
    elif v4_namespace_unassessed:
        if protocol_probe.status == "supported":
            message = (
                f"NFSv4 is available on {host}:2049; showmount covers only advertised legacy exports and "
                "cannot prove the NFSv4 namespace is complete."
            )
        else:
            message = (
                f"The NFSv4 probe on {host}:2049 was indeterminate; successful legacy export discovery "
                "cannot rule out an unenumerated NFSv4 pseudo-filesystem."
            )
        _emit_error(
            writer,
            run_id,
            severity="warn",
            code="NFS_V4_NAMESPACE_NOT_ENUMERATED",
            message=message,
            endpoint_key=endpoint_key,
            hint="Treat this run as partial for NFSv4; use explicit known exports until read-only namespace enumeration is configured.",
        )
        _record_error(stats, lock, "NFS_V4_NAMESPACE_NOT_ENUMERATED", message)
        with lock:
            stats.structural_coverage_gaps += 1

    if not discovery.exports:
        return True

    for export_path in discovery.exports:
        if getattr(args, "cancel_event", None) is not None and args.cancel_event.is_set():
            break
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": endpoint_key,
                "share_type": "nfs",
                "resource_type": "nfs_share",
                "name": export_path,
                "remark": "",
                "access_level": "unknown",
                "metadata": {
                    "discovery_method": "showmount_exports",
                    "presence": "advertised",
                    "access_assessment": "not_assessed",
                    "content_assessment": "not_assessed",
                },
            }
        )
        with lock:
            stats.resources += 1
    _report_detail(args, f"host {host}: discovered {len(discovery.exports)} advertised NFS export(s)")
    return True


def scan_host(
    host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock
):
    selected_share_types = _selected_share_types(args.share_types)
    disabled_share_types = getattr(args, "disabled_share_types", set())
    active_share_types = selected_share_types - set(disabled_share_types)
    succeeded = False

    cancel_event = getattr(args, "cancel_event", None)
    if cancel_event is not None and cancel_event.is_set():
        return SCAN_CANCELLED
    reporter = getattr(args, "progress_reporter", None)
    if isinstance(reporter, ProgressReporter):
        reporter.target_started(host)
    if "smb" in active_share_types:
        smb_succeeded = scan_host_smb(host, args, run_id, writer, stats, lock)
        succeeded = smb_succeeded or succeeded
        if cancel_event is not None and cancel_event.is_set():
            return SCAN_CANCELLED
        if not smb_succeeded:
            with lock:
                stats.structural_coverage_gaps += 1
    if "nfs" in active_share_types and not (cancel_event is not None and cancel_event.is_set()):
        nfs_succeeded = scan_host_nfs(host, args, run_id, writer, stats, lock)
        succeeded = nfs_succeeded or succeeded
        if cancel_event is not None and cancel_event.is_set():
            return SCAN_CANCELLED
        if not nfs_succeeded:
            with lock:
                stats.structural_coverage_gaps += 1
    return succeeded


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def upload_artifact(
    args: argparse.Namespace,
    run_id: str,
    artifact_path: str,
    hosts: list[str],
) -> str | None:
    if not args.upload:
        return None

    if not args.api_base or not args.project_id or not args.api_token:
        raise RuntimeError("--upload requires --api-base --project-id --api-token")

    base = args.api_base.rstrip("/")
    headers = {
        "Authorization": f"Bearer {args.api_token}",
    }

    create_payload = {
        "run_id": run_id,
        "name": args.run_name,
        "description": "collector upload",
        "target_scope": getattr(args, "target_scope_override", None)
        or {
            "cidrs": args.cidr,
            "hosts": hosts,
        },
    }
    create_url = f"{base}/projects/{args.project_id}/runs"
    upload_timeout = float(getattr(args, "upload_timeout", 600.0))
    request_timeout = (min(10.0, upload_timeout), upload_timeout)
    upload_attempts = int(getattr(args, "upload_attempts", 3))
    reporter = getattr(args, "progress_reporter", None)

    def _report_retry(message: str) -> None:
        if isinstance(reporter, ProgressReporter):
            reporter.detail(message, level=1)

    _report_detail(args, "upload: creating or resuming API run", level=1)
    create_resp = _post_with_retries(
        lambda: requests.post(create_url, json=create_payload, headers=headers, timeout=request_timeout),
        max_attempts=upload_attempts,
        on_retry=_report_retry,
    )
    create_detail = _response_detail(create_resp)
    if create_resp.status_code != 409 or create_detail != "run already exists":
        create_resp.raise_for_status()

    upload_url = f"{base}/projects/{args.project_id}/runs/{run_id}/artifact"
    local_artifact_sha256 = _sha256_file(artifact_path)
    normalized_artifact_path = artifact_path.lower()
    if normalized_artifact_path.endswith(".gz"):
        content_type = "application/gzip"
    elif normalized_artifact_path.endswith(".ndjson") or normalized_artifact_path.endswith(".jsonl"):
        content_type = "application/x-ndjson"
    else:
        content_type = "application/json"
    try:
        upload_resp = _post_with_retries(
            lambda: _upload_artifact_once(
                upload_url,
                headers,
                content_type,
                artifact_path,
                timeout=request_timeout,
            ),
            max_attempts=upload_attempts,
            on_retry=_report_retry,
        )
    except (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
    ) as upload_exc:
        try:
            return _reconcile_uploaded_artifact(
                args=args,
                base=base,
                headers=headers,
                request_timeout=request_timeout,
                upload_attempts=upload_attempts,
                local_artifact_sha256=local_artifact_sha256,
                run_id=run_id,
                context=(f"the artifact request ended without a definitive response ({_error_detail(upload_exc)})"),
            )
        except RuntimeError as reconciliation_exc:
            raise reconciliation_exc from upload_exc
    upload_detail = _response_detail(upload_resp)
    if upload_resp.status_code in UPLOAD_RETRIABLE_STATUSES:
        return _reconcile_uploaded_artifact(
            args=args,
            base=base,
            headers=headers,
            request_timeout=request_timeout,
            upload_attempts=upload_attempts,
            local_artifact_sha256=local_artifact_sha256,
            run_id=run_id,
            context=f"the artifact endpoint returned transient HTTP {upload_resp.status_code}",
        )
    accepted_conflict_details = {
        "run is currently ingesting",
        "run state does not accept upload",
    }
    if upload_resp.status_code == 409 and upload_detail in accepted_conflict_details:
        return _reconcile_uploaded_artifact(
            args=args,
            base=base,
            headers=headers,
            request_timeout=request_timeout,
            upload_attempts=upload_attempts,
            local_artifact_sha256=local_artifact_sha256,
            run_id=run_id,
            context=f"the artifact endpoint returned HTTP 409 ({upload_detail})",
        )

    if upload_resp.status_code == 409:
        upload_resp.raise_for_status()
    upload_resp.raise_for_status()
    try:
        upload_payload = upload_resp.json()
    except ValueError as exc:
        raise RuntimeError("upload API returned invalid JSON after accepting the artifact") from exc
    if not isinstance(upload_payload, dict):
        raise RuntimeError("upload API returned an invalid payload after accepting the artifact")
    queued = upload_payload.get("queued")
    response_sha256 = str(upload_payload.get("artifact_sha256") or "").lower()
    if response_sha256 != local_artifact_sha256:
        raise RuntimeError("upload response artifact digest does not match the local artifact")
    if queued is False:
        print(
            "upload warning: artifact stored, but ingest queue handoff fell back to asynchronous recovery; monitor the run until ingestion starts.",
            file=sys.stderr,
        )

    _report_notice(
        args,
        f"upload accepted: run_id={run_id} api_run_url={base}/projects/{args.project_id}/runs/{run_id}",
    )
    return "accepted"


def _reconcile_uploaded_artifact(
    *,
    args: argparse.Namespace,
    base: str,
    headers: dict[str, str],
    request_timeout: tuple[float, float],
    upload_attempts: int,
    local_artifact_sha256: str,
    run_id: str,
    context: str,
) -> str:
    reporter = getattr(args, "progress_reporter", None)

    def _report_retry(message: str) -> None:
        if isinstance(reporter, ProgressReporter):
            reporter.detail(message, level=1)

    run_url = f"{base}/projects/{args.project_id}/runs/{run_id}"
    try:
        run_resp = _post_with_retries(
            lambda: requests.get(run_url, headers=headers, timeout=request_timeout),
            max_attempts=upload_attempts,
            on_retry=_report_retry,
        )
        run_resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"upload outcome is ambiguous: {context}; run reconciliation failed ({_error_detail(exc)})"
        ) from exc

    try:
        run_payload = run_resp.json()
    except ValueError as exc:
        raise RuntimeError(f"upload outcome is ambiguous: {context}; run reconciliation returned invalid JSON") from exc
    if not isinstance(run_payload, dict):
        raise RuntimeError(f"upload outcome is ambiguous: {context}; run reconciliation returned an invalid payload")
    remote_status = str(run_payload.get("status") or "").upper()
    remote_sha256 = str(run_payload.get("artifact_sha256") or "").lower()
    if remote_status not in {"UPLOADED", "INGESTING", "COMPLETE"} or remote_sha256 != local_artifact_sha256:
        raise RuntimeError(
            "upload outcome is ambiguous: "
            f"{context}; the API run does not confirm the same artifact "
            f"(status={remote_status or 'unknown'}, sha256_match={remote_sha256 == local_artifact_sha256})"
        )
    _report_notice(
        args,
        f"upload recovered after ambiguous response: run_id={run_id} status={remote_status}",
    )
    return "recovered"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as artifact_fp:
        for chunk in iter(lambda: artifact_fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upload_artifact_once(
    url: str,
    headers: dict[str, str],
    content_type: str,
    artifact_path: str,
    *,
    timeout: float | tuple[float, float] = (10.0, 600.0),
) -> requests.Response:
    with open(artifact_path, "rb") as fp:
        return requests.post(
            url,
            data=fp,
            headers={
                **headers,
                "Content-Type": content_type,
                "X-Artifact-Filename": _artifact_upload_filename(artifact_path),
            },
            timeout=timeout,
        )


def _response_detail(response: requests.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    return str(detail) if detail is not None else None


def _retry_after_seconds(raw_value: object, *, now: datetime | None = None) -> float:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return 0.0
    try:
        seconds = float(raw_text)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw_text)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - (now or datetime.now(tz=UTC))).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return 0.0
    return seconds


def _post_with_retries(
    request_fn,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 0.5,
    on_retry=None,
) -> requests.Response:
    for attempt in range(max_attempts):
        response = None
        retry_reason = ""
        try:
            response = request_fn()
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            retry_reason = _error_detail(exc)
            if attempt + 1 >= max_attempts:
                raise
        else:
            if response.status_code not in UPLOAD_RETRIABLE_STATUSES or attempt + 1 >= max_attempts:
                return response
            retry_reason = f"HTTP {response.status_code}"

        base_delay = min(initial_backoff_seconds * (2**attempt), 4.0)
        retry_after = 0.0
        if response is not None:
            try:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After", "0"))
            except AttributeError:
                retry_after = 0.0
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()
        sleep_seconds = min(30.0, max(retry_after, base_delay + random.uniform(0.0, base_delay)))
        if on_retry is not None:
            on_retry(
                f"upload: retrying after {retry_reason} "
                f"(attempt {attempt + 2}/{max_attempts}, delay {sleep_seconds:.1f}s)"
            )
        time.sleep(sleep_seconds)
    raise RuntimeError("post retry loop failed unexpectedly")


def _collect_scan_results(
    futures_by_host: dict[concurrent.futures.Future, str],
    run_id: str,
    writer: NDJSONWriter,
    stats: Stats,
    lock: threading.Lock,
    args: argparse.Namespace | None = None,
) -> int:
    host_failures = 0
    for future in concurrent.futures.as_completed(futures_by_host):
        host = futures_by_host[future]
        host_failures += _handle_scan_result(future, host, run_id, writer, stats, lock, args=args)
    return host_failures


def _handle_scan_result(
    future: concurrent.futures.Future,
    host: str,
    run_id: str,
    writer: NDJSONWriter,
    stats: Stats,
    lock: threading.Lock,
    *,
    args: argparse.Namespace | None = None,
) -> int:
    try:
        exc = future.exception()
    except concurrent.futures.CancelledError as cancelled_error:
        exc = cancelled_error

    if exc is not None:
        was_cancelled = isinstance(exc, concurrent.futures.CancelledError)
        reporter = getattr(args, "progress_reporter", None) if args is not None else None
        if was_cancelled:
            if isinstance(reporter, ProgressReporter):
                reporter.target_cancelled(host)
            return 0
        message = f"scan worker failed for host {host}: {_error_detail(exc)}"
        code = _scan_thread_error_code(exc)
        endpoint_key = _scan_thread_endpoint_key(host, args)
        _emit_error(
            writer,
            run_id,
            severity="error",
            code=code,
            message=message,
            endpoint_key=endpoint_key,
            hint="Unhandled worker exception; inspect traceback/logs for root cause.",
        )
        _record_error(stats, lock, code, message)
        if isinstance(reporter, ProgressReporter):
            reporter.detail(f"{code}: {message}", level=1)
            reporter.target_completed(host, succeeded=False)
        return 1

    result = future.result()
    reporter = getattr(args, "progress_reporter", None) if args is not None else None
    if result is SCAN_CANCELLED:
        if isinstance(reporter, ProgressReporter):
            reporter.target_cancelled(host)
        return 0
    succeeded = bool(result)
    if isinstance(reporter, ProgressReporter):
        reporter.target_completed(host, succeeded=succeeded)
    return 0 if succeeded else 1


def _scan_targets(
    targets,
    args: argparse.Namespace,
    run_id: str,
    writer: NDJSONWriter,
    stats: Stats,
    lock: threading.Lock,
) -> ScanOutcome:
    max_workers = max(1, args.workers)
    max_pending = max_workers * 2
    submitted = 0
    completed = 0
    cancelled = 0
    host_failures = 0
    pending: dict[concurrent.futures.Future, str] = {}
    cancel_event = getattr(args, "cancel_event", None)
    if not isinstance(cancel_event, threading.Event):
        cancel_event = threading.Event()
        args.cancel_event = cancel_event
    reporter = getattr(args, "progress_reporter", None)
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    interrupted = False
    stop_submissions = False

    def _consume(done_futures) -> None:
        nonlocal cancelled, completed, host_failures, stop_submissions
        for completed_future in done_futures:
            host = pending.pop(completed_future)
            result_was_cancelled = completed_future.cancelled()
            if not result_was_cancelled:
                try:
                    result_was_cancelled = (
                        completed_future.exception() is None and completed_future.result() is SCAN_CANCELLED
                    )
                except concurrent.futures.CancelledError:
                    result_was_cancelled = True
            host_failures += _handle_scan_result(
                completed_future,
                host,
                run_id,
                writer,
                stats,
                lock,
                args=args,
            )
            if result_was_cancelled:
                cancelled += 1
            else:
                completed += 1
            if bool(getattr(writer, "write_failed", False)):
                stop_submissions = True
                cancel_event.set()
                for queued_future in pending:
                    queued_future.cancel()

    try:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        for host in targets:
            if stop_submissions:
                break
            future = executor.submit(scan_host, host, args, run_id, writer, stats, lock)
            pending[future] = host
            submitted += 1
            if isinstance(reporter, ProgressReporter):
                reporter.target_submitted()

            if len(pending) < max_pending:
                continue

            done, _ = concurrent.futures.wait(tuple(pending), return_when=concurrent.futures.FIRST_COMPLETED)
            _consume(done)

        while pending:
            done, _ = concurrent.futures.wait(
                tuple(pending),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            _consume(done)
    except KeyboardInterrupt:
        interrupted = True
        cancel_event.set()
        if isinstance(reporter, ProgressReporter):
            reporter.interruption_requested()
        for future in pending:
            future.cancel()
    except Exception as exc:
        cancel_event.set()
        message = f"scan orchestration failed: {_error_detail(exc)}"
        _emit_error(
            writer,
            run_id,
            severity="error",
            code="SCAN_ORCHESTRATION_FAILED",
            message=message,
            hint="Reduce --workers, verify target inputs, and retry. Existing partial results are preserved.",
        )
        _record_error(stats, lock, "SCAN_ORCHESTRATION_FAILED", message)
        host_failures += 1
        for future in pending:
            future.cancel()
    finally:
        if executor is not None:
            try:
                executor.shutdown(wait=True, cancel_futures=cancel_event.is_set())
            except (OSError, RuntimeError) as exc:
                message = f"scan executor shutdown failed: {_error_detail(exc)}"
                _emit_error(
                    writer,
                    run_id,
                    severity="error",
                    code="SCAN_ORCHESTRATION_FAILED",
                    message=message,
                    hint="Partial results were retained; rerun the original scope.",
                )
                _record_error(stats, lock, "SCAN_ORCHESTRATION_FAILED", message)
                host_failures += 1

    if pending:
        _consume(tuple(pending))

    return ScanOutcome(
        targets_submitted=submitted,
        targets_completed=completed,
        host_failures=host_failures,
        interrupted=interrupted,
        targets_cancelled=cancelled,
    )


def _scan_thread_error_code(exc: BaseException) -> str:
    if isinstance(exc, ArtifactSpoolLimitError):
        return "SCAN_OUTPUT_LIMIT"
    if isinstance(exc, concurrent.futures.CancelledError):
        return "SCAN_THREAD_CANCELLED"
    if isinstance(exc, (NetBIOSTimeout,)):
        return "SCAN_TIMEOUT"
    if isinstance(exc, (NetBIOSError,)):
        return "SCAN_IO_ERROR"
    if isinstance(exc, SessionError):
        return "SCAN_SESSION_ERROR"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "SCAN_TIMEOUT"
    if isinstance(exc, OSError):
        return "SCAN_IO_ERROR"
    if isinstance(exc, (TypeError, ValueError)):
        return "SCAN_INPUT_ERROR"
    return "SCAN_THREAD_FAILED"


def _scan_thread_endpoint_key(host: str, args: argparse.Namespace | None) -> str | None:
    if args is None:
        return f"{host}:445"

    selected = _selected_share_types(getattr(args, "share_types", "smb"))
    disabled = set(getattr(args, "disabled_share_types", set()) or set())
    active = selected - disabled
    if active == {"smb"}:
        return f"{host}:445"
    if active == {"nfs"}:
        return f"{host}:2049"
    return None


def _abort_collection_output(
    writer: NDJSONWriter,
    *,
    temp_artifact: str | None,
    error: BaseException,
) -> int:
    """Discard incomplete buffers after an output failure and report it once."""

    cleanup_interrupt: KeyboardInterrupt | SystemExit | None = None
    try:
        writer.close(keep_output=False)
    except (KeyboardInterrupt, SystemExit) as exc:
        cleanup_interrupt = exc
        # NDJSONWriter intentionally remains retryable when finalization is
        # interrupted. Give discard one bounded second attempt, then preserve
        # the original process-control exception for the caller.
        try:
            writer.close(keep_output=False)
        except BaseException:
            pass
    except Exception:
        # The original emit failure is the useful operator-facing cause. The
        # close call still gets a chance to release every temporary buffer.
        pass
    if temp_artifact:
        try:
            os.unlink(temp_artifact)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(
                f"cleanup warning: unable to remove temporary artifact: {_error_detail(exc)}",
                file=sys.stderr,
            )
    print(f"output error: {_error_detail(error)}", file=sys.stderr)
    if cleanup_interrupt is not None:
        raise cleanup_interrupt
    return EXIT_FAILURE


def main() -> int:
    args = parse_args()
    if getattr(args, "use_session_creds", False):
        args.kerberos = True
    preflight_ccache = _resolve_ccache_env_value(getattr(args, "ccache", None))
    if getattr(args, "kerberos", False) and not getattr(args, "username", ""):
        if preflight_ccache:
            username, domain, principal_error = _principal_from_ccache_env(preflight_ccache)
            if principal_error and not getattr(args, "use_session_creds", False):
                print(f"configuration error: {principal_error}", file=sys.stderr)
                print(
                    "pass --username with --kerberos, or provide a valid --ccache/KRB5CCNAME with a default principal.",
                    file=sys.stderr,
                )
                return EXIT_FAILURE
            if username:
                args.username = username
            if not getattr(args, "domain", "") and domain:
                args.domain = domain
    try:
        _validate_args(args)
    except SystemExit as exc:
        print(f"configuration error: {_error_detail(exc)}", file=sys.stderr)
        return EXIT_FAILURE

    args.ccache_env_value = preflight_ccache
    if getattr(args, "use_session_creds", False):
        username, domain, principal_error = _principal_from_ccache_env(args.ccache_env_value)
        if principal_error:
            print(f"configuration error: {principal_error}", file=sys.stderr)
            print(
                "set KRB5CCNAME or pass --ccache pointing to a valid Kerberos ticket cache, or provide explicit SMB credentials.",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        args.username = str(getattr(args, "username", "") or username or "")
        if not getattr(args, "domain", "") and domain:
            args.domain = domain

    args.exclude_path_pattern = re.compile(args.exclude_path_regex) if args.exclude_path_regex else None
    selected_share_types = sorted(_selected_share_types(args.share_types))
    disabled_share_types, dependency_warnings, dependency_fatals = _validate_runtime_dependencies(
        set(selected_share_types)
    )
    args.disabled_share_types = disabled_share_types
    smb_auth_method = _resolve_smb_auth_method(args) if "smb" in selected_share_types else "none"

    if dependency_fatals:
        for message in dependency_fatals:
            print(f"configuration error: {message}", file=sys.stderr)
        return EXIT_FAILURE

    max_targets = int(getattr(args, "max_targets", 65536))
    try:
        host_inputs = parse_hosts_file(args.hosts, max_hosts=max_targets or None)
        target_count = count_targets(args.cidr, host_inputs)
        targets = iter_targets(args.cidr, host_inputs)
    except (OSError, ValueError) as exc:
        print(f"input error: {_error_detail(exc)}", file=sys.stderr)
        print("fix target inputs and retry (--hosts file and/or --cidr values).", file=sys.stderr)
        return EXIT_FAILURE

    if max_targets and target_count > max_targets:
        print(
            f"input error: resolved {target_count} unique targets, exceeding --max-targets {max_targets}.",
            file=sys.stderr,
        )
        print("narrow the scope or explicitly raise --max-targets after review.", file=sys.stderr)
        return EXIT_FAILURE

    targets = iter(targets)
    try:
        first_target = next(targets)
    except StopIteration:
        print("input error: no targets resolved from --hosts/--cidr.", file=sys.stderr)
        return EXIT_FAILURE
    targets = itertools.chain([first_target], targets)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(tz=UTC)

    temp_artifact: str | None = None
    output_path = args.output

    if args.upload and output_path is None:
        suffix = ".ndjson.gz" if args.gzip else ".ndjson"
        fd, temp_artifact = tempfile.mkstemp(prefix="share-sentinel-", suffix=suffix)
        os.close(fd)
        output_path = temp_artifact

    try:
        writer = NDJSONWriter(
            output_path,
            args.gzip,
            max_spool_bytes=int(getattr(args, "max_artifact_bytes", DEFAULT_MAX_ARTIFACT_BYTES)),
        )
    except OSError as exc:
        if temp_artifact:
            try:
                os.unlink(temp_artifact)
            except OSError:
                pass
        print(f"output error: unable to initialize collector buffers: {_error_detail(exc)}", file=sys.stderr)
        return EXIT_FAILURE
    # A requested provider that cannot be loaded leaves every target outside
    # the declared observation plane, even when another provider can still be
    # collected successfully in a mixed scan.
    stats = Stats(structural_coverage_gaps=len(disabled_share_types) * target_count)
    lock = threading.Lock()
    reporter = ProgressReporter(
        total_targets=target_count,
        stats=stats,
        stats_lock=lock,
        quiet=bool(getattr(args, "quiet", False)),
        verbosity=int(getattr(args, "verbose", 0) or 0),
        interval_seconds=float(getattr(args, "progress_interval", 5.0)),
    )
    args.progress_reporter = reporter
    args.cancel_event = threading.Event()

    provider_scope = "+".join(sorted(selected_share_types))
    requested_session_kind = smb_auth_method if "smb" in selected_share_types else "not_applicable"
    if requested_session_kind in {"anonymous", "guest", "not_applicable"}:
        requested_native_identity = requested_session_kind
    else:
        requested_username = str(getattr(args, "username", "") or "")
        requested_domain = str(getattr(args, "domain", "") or "")
        requested_native_identity = (
            f"{requested_domain}\\{requested_username}" if requested_domain else requested_username
        )
    requested_identity_fingerprint = _stable_fingerprint(
        "network-collector-session:v1",
        requested_session_kind,
        requested_native_identity,
    )
    comparison_contracts = {"structural": NETWORK_STRUCTURAL_COMPARISON_CONTRACT}
    if "smb" in selected_share_types:
        comparison_contracts.update(
            {
                "content": SMB_CONTENT_COMPARISON_CONTRACT,
                "capability": SMB_CAPABILITY_COMPARISON_CONTRACT,
            }
        )
    run_meta_record = {
        "type": "run_meta",
        "schema_version": (
            2 if "smb" in selected_share_types and str(getattr(args, "smb_permissions", "none")) != "none" else 1
        ),
        "tool": "share-sentinel-collector",
        "tool_version": TOOL_VERSION,
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "operator_label": args.operator_label,
        "collection": {
            "command": Path(__file__).name,
            "arguments": _redact_cli_arguments(sys.argv[1:]),
            "target_scope": {
                "cidrs": args.cidr,
                "hosts": host_inputs,
                "target_count": target_count,
                "share_types": selected_share_types,
                "disabled_share_types": sorted(disabled_share_types),
            },
            "enumeration": {
                "workers": args.workers,
                "timeout_seconds": args.timeout,
                "max_depth": args.max_depth,
                "max_entries_per_share": args.max_entries_per_share,
                "max_artifact_bytes": int(getattr(args, "max_artifact_bytes", DEFAULT_MAX_ARTIFACT_BYTES)),
                "access_probe_limit": int(getattr(args, "access_probe_limit", 3)),
                "smb_permissions": str(getattr(args, "smb_permissions", "none")),
                "smb_permission_sample_limit": int(
                    getattr(
                        args,
                        "smb_permission_sample_limit",
                        SMB_PERMISSION_DEFAULT_SAMPLE_LIMIT,
                    )
                ),
                "include_share": list(getattr(args, "include_share", []) or []),
                "exclude_share": list(getattr(args, "exclude_share", []) or []),
                "exclude_path_regex": args.exclude_path_regex or None,
                "extensions_only": args.extensions_only or None,
            },
        },
        "auth": {
            "mode": (("local" if args.local_auth else "domain") if smb_auth_method in {"ntlm", "kerberos"} else "none"),
            "domain": (args.domain or None) if smb_auth_method in {"ntlm", "kerberos"} else None,
            "username": (args.username or None) if smb_auth_method in {"ntlm", "kerberos"} else None,
            "method": smb_auth_method,
        },
        "collection_context": {
            "source": "network_share_collector",
            "provider": provider_scope,
            "collection_mode": "declared_target_inventory",
            "assessed_identity": requested_identity_fingerprint,
            "auth_context": {
                "auth_type": smb_auth_method,
                "auth_mode": (
                    ("local" if args.local_auth else "domain") if smb_auth_method in {"ntlm", "kerberos"} else "none"
                ),
            },
            "status": "running",
            "partial": True,
            "sync_mode": "full_snapshot",
            "materialized_snapshot": True,
            "discovery_completeness": "running",
            "metadata": {
                "snapshot_materialized": True,
                "comparison_contracts": comparison_contracts,
                # This collector discovers NFS exports but does not mount and
                # enumerate their file trees. Mixed runs therefore cannot make
                # a global item/content completeness claim.
                "files_included": "nfs" not in selected_share_types,
                "permissions_assessed": str(getattr(args, "smb_permissions", "none")) != "none",
                "permissions_complete": False,
                "structural_complete": False,
                "content_complete": False,
            },
        },
    }
    if int(run_meta_record["schema_version"]) >= 2:
        run_meta_record["artifact_features"] = ["direct_permissions_v1"]
    try:
        writer.emit(run_meta_record)
    except (OSError, ArtifactSpoolLimitError) as exc:
        return _abort_collection_output(writer, temp_artifact=temp_artifact, error=exc)

    for warning in dependency_warnings:
        try:
            _emit_error(
                writer,
                run_id,
                severity="warn",
                code="SCAN_DEPENDENCY_WARNING",
                message=warning,
                hint="Install the missing dependency to enable all requested share types.",
            )
        except (OSError, ArtifactSpoolLimitError) as exc:
            return _abort_collection_output(writer, temp_artifact=temp_artifact, error=exc)
        _record_error(stats, lock, "SCAN_DEPENDENCY_WARNING", warning)

    reporter.start(workers=args.workers, share_types=selected_share_types)
    run_ccache = args.ccache_env_value if smb_auth_method == "kerberos" else None
    try:
        with _run_scoped_kerberos_cache(run_ccache):
            scan_outcome = _scan_targets(targets, args, run_id, writer, stats, lock)
    except (OSError, ArtifactSpoolLimitError) as exc:
        if isinstance(exc, ArtifactSpoolLimitError) or writer.write_failed:
            reporter.finish(status="failure")
            return _abort_collection_output(writer, temp_artifact=temp_artifact, error=exc)
        raise

    if scan_outcome.interrupted:
        interruption_message = (
            "Collection interrupted by operator; the artifact contains only completed and drained in-flight work."
        )
        try:
            _emit_error(
                writer,
                run_id,
                severity="warn",
                code="SCAN_INTERRUPTED",
                message=interruption_message,
                hint="Review the partial artifact and rerun the original scope when ready.",
            )
        except (OSError, ArtifactSpoolLimitError) as exc:
            reporter.finish(status="failure")
            return _abort_collection_output(writer, temp_artifact=temp_artifact, error=exc)
        _record_error(stats, lock, "SCAN_INTERRUPTED", interruption_message)

    structural_coverage_complete = stats.structural_coverage_gaps == 0
    structural_complete = bool(
        not scan_outcome.interrupted
        and scan_outcome.host_failures == 0
        and scan_outcome.targets_cancelled == 0
        and scan_outcome.targets_completed == target_count
        and structural_coverage_complete
    )
    permission_requested = str(getattr(args, "smb_permissions", "none")) != "none"
    permission_incomplete = int(stats.error_codes.get("SMB_PERMISSION_ASSESSMENT_INCOMPLETE", 0)) > 0
    collection_partial = bool(scan_outcome.interrupted or scan_outcome.host_failures > 0 or stats.errors > 0)
    final_context = dict(run_meta_record["collection_context"])
    final_metadata = dict(final_context.get("metadata") or {})
    final_metadata.update(
        {
            "permissions_assessed": permission_requested and stats.permission_assessments > 0,
            "permissions_complete": permission_requested
            and stats.permission_assessments > 0
            and not permission_incomplete
            and structural_complete,
            "structural_complete": structural_complete,
            "structural_coverage_gaps": stats.structural_coverage_gaps,
            "content_complete": not collection_partial and "nfs" not in selected_share_types,
        }
    )
    final_context.update(
        {
            "status": "interrupted" if scan_outcome.interrupted else ("partial" if collection_partial else "complete"),
            "partial": collection_partial,
            "discovery_completeness": "authoritative" if structural_complete else "partial",
            "metadata": final_metadata,
        }
    )
    run_meta_record["collection_context"] = final_context
    try:
        writer.emit(run_meta_record)
    except (OSError, ArtifactSpoolLimitError) as exc:
        reporter.finish(status="failure")
        return _abort_collection_output(writer, temp_artifact=temp_artifact, error=exc)

    try:
        writer.emit(
            {
                "type": "run_end",
                "run_id": run_id,
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "stats": {
                    "targets_scanned": scan_outcome.targets_completed,
                    "targets_submitted": scan_outcome.targets_submitted,
                    "targets_cancelled": scan_outcome.targets_cancelled,
                    "targets_total": target_count,
                    "targets_remaining": max(0, target_count - scan_outcome.targets_completed),
                    "host_failures": scan_outcome.host_failures,
                    "interrupted": scan_outcome.interrupted,
                    "endpoints": stats.endpoints,
                    "resources": stats.resources,
                    "items": stats.items,
                    "permission_assessments": stats.permission_assessments,
                    "permission_entries": stats.permission_entries,
                    "structural_coverage_gaps": stats.structural_coverage_gaps,
                    "errors": stats.errors,
                },
            }
        )
    except ArtifactSpoolLimitError:
        # The writer retains the original typed failure and reports it during
        # the normal atomic finalization path below.
        pass

    run_has_data = _is_successful_run(stats)
    keep_output = run_has_data
    writer_close_error: BaseException | None = None
    finalization_interrupted = False
    try:
        writer.close(keep_output=keep_output)
    except KeyboardInterrupt:
        finalization_interrupted = True
        if output_path is not None:
            print(
                "interrupt received while finalizing output; completing one atomic retry before exit.",
                file=sys.stderr,
            )
            try:
                writer.close(keep_output=keep_output)
            except BaseException as exc:
                writer_close_error = exc
        else:
            try:
                writer.close(keep_output=False)
            except BaseException:
                pass
    except Exception as exc:
        writer_close_error = exc

    upload_error: BaseException | None = None
    upload_interrupted = False
    keep_local_artifact = bool(
        (scan_outcome.interrupted or finalization_interrupted)
        and writer_close_error is None
        and output_path is not None
        and os.path.exists(output_path)
    )
    upload_status = "not requested" if not args.upload else "skipped"
    try:
        if (
            writer_close_error is None
            and args.upload
            and keep_output
            and output_path is not None
            and not scan_outcome.interrupted
            and not finalization_interrupted
        ):
            upload_status = upload_artifact(args, run_id, output_path, host_inputs) or "accepted"
    except (requests.RequestException, RuntimeError, OSError) as exc:
        upload_error = exc
        upload_status = "failed"
        keep_local_artifact = output_path is not None and os.path.exists(output_path)
    except KeyboardInterrupt:
        upload_interrupted = True
        upload_status = "outcome unknown (interrupted)"
        keep_local_artifact = output_path is not None and os.path.exists(output_path)
    finally:
        if temp_artifact and os.path.exists(temp_artifact) and not keep_local_artifact:
            try:
                os.unlink(temp_artifact)
            except OSError as exc:
                print(f"cleanup warning: unable to remove temporary artifact: {_error_detail(exc)}", file=sys.stderr)

    if finalization_interrupted:
        if writer_close_error is not None:
            print(
                f"output error: interrupted finalization could not be completed: {_error_detail(writer_close_error)}",
                file=sys.stderr,
            )
        artifact_label = output_path if keep_local_artifact else None
        if artifact_label:
            print(f"artifact kept at {artifact_label}", file=sys.stderr)
        reporter.finish(
            status="interrupted",
            artifact=artifact_label,
            upload_status=upload_status,
        )
        return EXIT_INTERRUPTED

    if writer_close_error is not None:
        destination = output_path or "stdout"
        print(
            f"output error: failed to write output to {destination}: {_error_detail(writer_close_error)}",
            file=sys.stderr,
        )
        reporter.finish(status="failure", artifact=None, upload_status=upload_status)
        return EXIT_FAILURE

    if upload_interrupted:
        print(
            "upload interrupted: delivery outcome is unknown; inspect the run before retrying.",
            file=sys.stderr,
        )
        if keep_local_artifact and output_path:
            print(f"artifact kept at {output_path}", file=sys.stderr)
        reporter.finish(
            status="interrupted",
            artifact=output_path if keep_local_artifact else None,
            upload_status=upload_status,
        )
        return EXIT_INTERRUPTED

    if upload_error is not None:
        print(
            f"upload error: failed to send artifact to Share Sentinel: {_error_detail(upload_error)}",
            file=sys.stderr,
        )
        if keep_local_artifact and output_path:
            print(f"artifact kept at {output_path}", file=sys.stderr)
            reporter.finish(
                status="partial",
                artifact=output_path,
                upload_status=upload_status,
            )
            return EXIT_PARTIAL
        print("rerun with --output if you want to keep a local artifact copy for retry.", file=sys.stderr)
        reporter.finish(status="failure", artifact=None, upload_status=upload_status)
        return EXIT_FAILURE

    if scan_outcome.interrupted:
        artifact_label = output_path if keep_output and output_path else None
        if artifact_label:
            print(f"partial artifact kept at {artifact_label}", file=sys.stderr)
        reporter.finish(
            status="interrupted",
            artifact=artifact_label,
            upload_status=upload_status,
        )
        return EXIT_INTERRUPTED

    if not keep_output:
        _print_scan_failure_summary(
            stats,
            scan_outcome.host_failures,
            reason="scan did not collect any endpoint/resource/item/error records.",
            output_path=args.output,
        )
        reporter.finish(status="failure", artifact=None, upload_status=upload_status)
        return EXIT_FAILURE
    if output_path is None:
        artifact_label = "stdout"
    elif os.path.exists(output_path):
        artifact_label = output_path
    elif temp_artifact and upload_status in {"accepted", "recovered"}:
        artifact_label = "uploaded (temporary copy removed)"
    else:
        artifact_label = "not retained"
    if scan_outcome.host_failures > 0 or stats.errors > 0:
        reporter.finish(
            status="partial",
            artifact=artifact_label,
            upload_status=upload_status,
        )
        return EXIT_PARTIAL
    reporter.finish(
        status="success",
        artifact=artifact_label,
        upload_status=upload_status,
    )
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
