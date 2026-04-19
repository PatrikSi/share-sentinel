#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import concurrent.futures
from contextlib import contextmanager
import gzip
import ipaddress
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import time

import requests

try:
    from impacket.smbconnection import SMBConnection, SessionError
    from impacket.nmb import NetBIOSError, NetBIOSTimeout
except ImportError:
    SMBConnection = None

    class SessionError(Exception):
        """Fallback error type used when impacket is unavailable."""

    class NetBIOSError(Exception):
        """Fallback error type used when impacket is unavailable."""

    class NetBIOSTimeout(Exception):
        """Fallback error type used when impacket is unavailable."""


TOOL_VERSION = "0.2.0"
SENSITIVE_ARGUMENT_FLAGS = {"--password", "--hashes", "--api-token"}
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2


@dataclass
class Stats:
    endpoints: int = 0
    resources: int = 0
    items: int = 0
    errors: int = 0
    error_codes: collections.Counter[str] = field(default_factory=collections.Counter)
    error_samples: dict[str, str] = field(default_factory=dict)


class NDJSONWriter:
    def __init__(self, path: str | None, gzip_output: bool):
        self._lock = threading.Lock()
        self._path = path
        self._gzip = bool(gzip_output and path is not None)
        self._closed = False
        self._buffer_dir = tempfile.mkdtemp(prefix="share-sentinel-buffer-")
        self._endpoint_paths: dict[str, str] = {}
        self._run_meta: dict[str, object] | None = None
        self._run_end: dict[str, object] | None = None
        self._issues: dict[str, dict[str, object]] = {}

    def emit(self, record: dict) -> None:
        with self._lock:
            if self._closed:
                return
            rec_type = str(record.get("type") or "")
            if rec_type == "run_meta":
                self._run_meta = dict(record)
                return
            if rec_type == "run_end":
                self._run_end = dict(record)
                return
            if rec_type == "error":
                self._record_issue(record)
                return
            if rec_type not in {"endpoint", "resource", "item"}:
                return

            endpoint_key = str(record.get("endpoint_key") or "").strip()
            if not endpoint_key:
                return

            endpoint_path = self._endpoint_paths.get(endpoint_key)
            if endpoint_path is None:
                endpoint_path = os.path.join(self._buffer_dir, f"{uuid.uuid4().hex}.jsonl")
                self._endpoint_paths[endpoint_key] = endpoint_path
            with open(endpoint_path, "a", encoding="utf-8") as endpoint_fp:
                endpoint_fp.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")

    def close(self, keep_output: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

        try:
            if not keep_output:
                return
            if self._path is None:
                self._write_document(sys.stdout)
                sys.stdout.flush()
                return
            if self._gzip:
                with gzip.open(self._path, "wt", encoding="utf-8") as target_fp:
                    self._write_document(target_fp)
                return
            with open(self._path, "w", encoding="utf-8") as target_fp:
                self._write_document(target_fp)
        finally:
            shutil.rmtree(self._buffer_dir, ignore_errors=True)

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
                            "access_level": record.get("access_level", "no_access"),
                            "entries": [],
                        }
                        share_states[share_key] = {
                            "doc": share_doc,
                            "index": {},
                        }
                        share_order.append(share_key)
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
                            "access_level": "no_access",
                            "entries": [],
                        }
                        share_state = {
                            "doc": share_doc,
                            "index": {},
                        }
                        share_states[share_key] = share_state
                        share_order.append(share_key)
                    self._insert_item(share_state, record)

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
                if node["is_dir"]:
                    node.setdefault("children", [])
                else:
                    node.pop("children", None)

    def _write_document(self, target_fp) -> None:
        run_meta = dict(self._run_meta or {})
        run_end = dict(self._run_end or {})
        summary = dict((run_end.get("stats") or {})) if isinstance(run_end.get("stats"), dict) else {}
        collection = dict(run_meta.get("collection") or {}) if isinstance(run_meta.get("collection"), dict) else {}
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
        target_fp.write(f"\"schema_version\":{json.dumps(run_meta.get('schema_version', 1))}")
        target_fp.write(",\"format\":\"share_sentinel_compact_json\"")
        target_fp.write(",\"meta\":")
        json.dump(meta, target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(",\"collection\":")
        json.dump(collection, target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(",\"summary\":")
        json.dump(summary, target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(",\"issue_summary\":")
        json.dump(self._serialized_issues(), target_fp, ensure_ascii=True, separators=(",", ":"))
        target_fp.write(",\"endpoints\":[")

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
        description=(
            "Collect SMB and/or NFS share inventory and write a compact JSON artifact.\n\n"
            "Workflow:\n"
            "  1) Select targets with --hosts and/or --cidr\n"
            "  2) Choose --share-types smb|nfs|both\n"
            "  3) Pick SMB auth mode if SMB is enabled\n"
            "  4) Save with --output and optionally upload with --upload"
        ),
        epilog=(
            "Examples:\n"
            "  Authenticated SMB scan:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types smb --username corp\\\\svc_scan --password '***' --output run.json.gz --gzip\n\n"
            "  Domain shell / ticket cache auth:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types smb --use-session-creds --output kerberos.json\n\n"
            "  Anonymous SMB scan:\n"
            "    share_sentinel_collector.py --cidr 10.20.0.0/24 --share-types smb --smb-anonymous --output anon.json\n\n"
            "  NFS + SMB combined scan:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types both --username corp\\\\svc_scan --password '***' --output combined.json.gz --gzip\n\n"
            "  Upload after scan:\n"
            "    share_sentinel_collector.py --hosts hosts.txt --share-types smb --username svc --password '***' --upload --api-base https://api.example --project-id <uuid> --api-token <token>\n\n"
            "Notes:\n"
            "  - SMB authentication modes:\n"
                "      * NTLM: set --username (and password or hashes)\n"
            "      * Kerberos: add --kerberos and --username\n"
            "      * Session credentials: use --use-session-creds to use the active Kerberos ticket cache\n"
            "      * Anonymous: set --smb-anonymous or omit SMB credentials\n"
            "  - NFS export enumeration uses `showmount -e`; if unavailable, only summary issues are recorded.\n"
            "  - When no endpoint/resource/item data is collected, output files are not written."
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
        help="Write compact JSON output to this file. Output is only committed when scan data is collected.",
    )
    common.add_argument("--gzip", action="store_true", help="Gzip-compress output when writing to a file.")
    common.add_argument("--workers", type=int, default=100, help="Concurrent host workers.")
    common.add_argument("--timeout", type=float, default=3.0, help="Per-network-operation timeout in seconds.")
    common.add_argument("--operator-label", type=str, help="Optional operator label stored in run metadata.")

    smb_auth = parser.add_argument_group("SMB Authentication")
    smb_auth.add_argument("--smb-anonymous", action="store_true", help="Force anonymous SMB session.")
    smb_auth.add_argument("--username", type=str, default="", help="SMB username for NTLM/Kerberos.")
    smb_auth.add_argument("--password", type=str, default="", help="SMB password for NTLM/Kerberos.")
    smb_auth.add_argument("--hashes", type=str, help="LM:NT hash pair for NTLM auth.")
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
    upload.add_argument("--api-token", type=str, help="API token with run write scope.")
    upload.add_argument("--run-name", type=str, default="Share Collector Run", help="Run name for uploaded scan.")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def iter_targets(cidrs: list[str], host_inputs: list[str] | None = None):
    seen: set[str] = set()
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        for host in network.hosts():
            target = str(host)
            if target in seen:
                continue
            seen.add(target)
            yield target

    for host in host_inputs or []:
        if host in seen:
            continue
        seen.add(host)
        yield host


def parse_targets(cidrs: list[str], hosts_file: str | None) -> list[str]:
    return list(iter_targets(cidrs, parse_hosts_file(hosts_file)))


def parse_hosts_file(hosts_file: str | None) -> list[str]:
    if not hosts_file:
        return []
    hosts: list[str] = []
    for line in Path(hosts_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            hosts.append(line)
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


def list_share_entries(
    conn: SMBConnection,
    share_name: str,
    max_depth: int,
    max_entries: int,
    exclude_path_regex: re.Pattern[str] | None,
    extensions: set[str] | None,
    on_list_error=None,
    on_limit_reached=None,
):
    queue = collections.deque([("", 0)])
    emitted = 0

    while queue and emitted < max_entries:
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
            continue

        for entry in entries:
            name = entry.get_longname()
            if name in {".", ".."}:
                continue

            full_path = normalize_path(rel_path, name)
            if exclude_path_regex and exclude_path_regex.search(full_path):
                continue

            is_dir = bool(entry.is_directory())
            if extensions and not is_dir:
                suffix = os.path.splitext(name)[1].lower()
                if suffix not in extensions:
                    continue

            emitted += 1
            yield {
                "path": full_path,
                "name": name,
                "is_dir": is_dir,
            }

            if is_dir and depth + 1 < max_depth:
                queue.append((full_path.strip("\\"), depth + 1))
            if emitted >= max_entries:
                break

    if emitted >= max_entries and queue and on_limit_reached is not None:
        try:
            on_limit_reached(emitted)
        except Exception:
            pass


def _dialect_label(raw: str) -> str:
    mapping = {
        "528": "2.0.2",
        "770": "2.1",
        "768": "3.0",
        "785": "3.1.1",
    }
    return mapping.get(str(raw), str(raw))


def _signing_label(conn: SMBConnection) -> str:
    try:
        required = bool(conn.isSigningRequired())
        return "required" if required else "enabled"
    except (AttributeError, OSError, SessionError, TypeError, ValueError):
        return "enabled"


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
    selected_share_types = _selected_share_types(getattr(args, "share_types", "smb"))
    cidr = getattr(args, "cidr", [])
    hosts = getattr(args, "hosts", None)
    kerberos = bool(getattr(args, "kerberos", False))
    smb_anonymous = bool(getattr(args, "smb_anonymous", False))
    use_session_creds = bool(getattr(args, "use_session_creds", False))
    username = str(getattr(args, "username", "") or "")
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
    exclude_path_regex = getattr(args, "exclude_path_regex", None)

    if not cidr and not hosts:
        raise SystemExit("at least one target source is required: --hosts and/or --cidr")

    if kerberos and smb_anonymous:
        raise SystemExit("--kerberos cannot be combined with --smb-anonymous")
    if use_session_creds and smb_anonymous:
        raise SystemExit("--use-session-creds cannot be combined with --smb-anonymous")

    if "smb" in selected_share_types:
        if kerberos and not username and not use_session_creds:
            raise SystemExit("--kerberos requires --username")
        if ccache and not kerberos and not use_session_creds:
            raise SystemExit("--ccache requires --kerberos (or --use-session-creds)")
        if use_session_creds and hashes:
            raise SystemExit("--use-session-creds cannot be combined with --hashes")
        if use_session_creds and password:
            raise SystemExit("--use-session-creds cannot be combined with --password")
        if hashes and ":" not in str(hashes):
            raise SystemExit("--hashes must be in LMHASH:NTHASH format")
        if hashes and not username and not smb_anonymous:
            raise SystemExit("--hashes requires --username unless --smb-anonymous is set")
        if password and not username and not smb_anonymous:
            raise SystemExit("--password requires --username unless --smb-anonymous is set")

    if upload and (not api_base or not project_id or not api_token):
        raise SystemExit("--upload requires --api-base, --project-id, and --api-token")
    if output_path:
        output = Path(output_path).expanduser()
        parent = output.parent if str(output.parent) else Path(".")
        if not parent.exists():
            raise SystemExit(f"--output directory does not exist: {parent}")
        if not parent.is_dir():
            raise SystemExit(f"--output parent is not a directory: {parent}")
        if output.exists() and output.is_dir():
            raise SystemExit(f"--output points to a directory, expected file path: {output}")
    if workers <= 0:
        raise SystemExit("--workers must be greater than zero")
    if timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    if max_depth <= 0:
        raise SystemExit("--max-depth must be greater than zero")
    if max_entries_per_share <= 0:
        raise SystemExit("--max-entries-per-share must be greater than zero")
    if exclude_path_regex:
        try:
            re.compile(exclude_path_regex)
        except re.error as exc:
            raise SystemExit(f"--exclude-path-regex is invalid: {exc}") from exc


def _parse_showmount_exports(output: str) -> list[str]:
    exports: list[str] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("exports list"):
            continue
        if not line.startswith("/"):
            continue
        export_path = line.split()[0]
        if export_path in seen:
            continue
        seen.add(export_path)
        exports.append(export_path)
    return exports


def _discover_nfs_exports(host: str, timeout_seconds: float) -> tuple[list[str], str | None]:
    timeout = max(1.0, timeout_seconds * 4)
    try:
        completed = subprocess.run(
            ["showmount", "-e", host],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return [], "showmount is not available"
    except subprocess.TimeoutExpired:
        return [], "showmount timed out"
    except OSError as exc:
        return [], str(exc)

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"showmount exit code {completed.returncode}"
        return [], detail

    return _parse_showmount_exports(completed.stdout), None


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
        message = (
            "SMB scanning requires the optional dependency `impacket`. "
            "Install with `pip install impacket`."
        )
        if selected_share_types == {"smb"}:
            fatals.append(message)
        else:
            disabled.add("smb")
            warnings.append(f"{message} SMB scanning will be skipped.")

    return disabled, warnings, fatals


def scan_host_smb(host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock) -> bool:
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

    try:
        conn = SMBConnection(host, host, sess_port=445, timeout=args.timeout)
        if attempted_auth == "kerberos":
            ccache_env_value = getattr(args, "ccache_env_value", None)
            use_cache = bool(ccache_env_value)
            conn.kerberosLogin(
                args.username,
                args.password,
                args.domain,
                lmhash="",
                nthash="",
                aesKey=None,
                kdcHost=None,
                TGT=None,
                TGS=None,
                useCache=use_cache,
            )
        elif attempted_auth == "ntlm":
            lmhash = ""
            nthash = ""
            if args.hashes and ":" in args.hashes:
                lmhash, nthash = args.hashes.split(":", 1)
            domain = "" if args.local_auth else args.domain
            conn.login(args.username, args.password, domain=domain, lmhash=lmhash, nthash=nthash)
        else:
            conn.login("", "", domain="", lmhash="", nthash="")

        endpoint_record = {
            "type": "endpoint",
            "run_id": run_id,
            "endpoint_key": endpoint_key,
            "ip": host if _is_ip(host) else None,
            "hostname": host if not _is_ip(host) else None,
            "domain": args.domain or None,
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

        excluded_shares = {s.upper() for s in args.exclude_share}
        include_shares = [str(share).strip() for share in getattr(args, "include_share", []) if str(share).strip()]
        exclude_path_regex = getattr(args, "exclude_path_pattern", None)
        if exclude_path_regex is None and args.exclude_path_regex:
            exclude_path_regex = re.compile(args.exclude_path_regex)
        extensions = None
        if args.extensions_only:
            extensions = {e.strip().lower() for e in args.extensions_only.split(",") if e.strip()}

        if include_shares:
            shares = [{"shi1_netname": f"{name}\x00", "shi1_remark": "user-specified share"} for name in include_shares]
        else:
            try:
                shares = conn.listShares()
            except SessionError as exc:
                detail = _error_detail(exc)
                message = f"SMB share enumeration failed: {detail}"
                hint = (
                    "Anonymous session established but share enumeration is blocked. Use SMB credentials or pass known names with --include-share."
                    if attempted_auth == "anonymous"
                    else "Credentials authenticated but are not allowed to enumerate shares. Use a higher-privilege account or --include-share."
                )
                _emit_error(
                    writer,
                    run_id,
                    severity="warn",
                    code="LIST_SHARES_DENIED",
                    message=message,
                    endpoint_key=endpoint_key,
                    hint=hint,
                )
                _record_error(stats, lock, "LIST_SHARES_DENIED", message)
                try:
                    conn.logoff()
                except (SessionError, OSError):
                    pass
                return True

        eligible_shares = []
        for share in shares:
            share_name = _share_info_value(share, "shi1_netname")
            if not share_name or share_name.upper() in excluded_shares:
                continue
            eligible_shares.append(share)

        if not eligible_shares:
            try:
                conn.logoff()
            except (SessionError, OSError):
                pass
            return True

        def _handle_list_error(share_name: str, denied_path: str, exc: BaseException) -> None:
            detail = _error_detail(exc)
            message = f"SMB share listing failed for {share_name}: {detail}"
            _emit_error(
                writer,
                run_id,
                severity="warn",
                code="LIST_SESSION_ERROR",
                message=message,
                endpoint_key=endpoint_key,
                resource_name=share_name,
                path=denied_path,
                hint=_session_error_hint(detail, attempted_auth),
            )
            _record_error(stats, lock, "LIST_SESSION_ERROR", message)

        def _handle_list_limit(share_name: str, emitted: int) -> None:
            message = f"SMB share listing reached max entries ({emitted}) for {share_name}."
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
            share_name = _share_info_value(share, "shi1_netname")
            remark = _share_info_value(share, "shi1_remark")
            resource_record = {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": endpoint_key,
                "share_type": "smb",
                "resource_type": "smb_share",
                "name": share_name,
                "remark": remark,
                "access_level": "list_only",
            }
            writer.emit(resource_record)
            with lock:
                stats.resources += 1

            try:
                for entry in list_share_entries(
                    conn,
                    share_name,
                    max_depth=max(1, args.max_depth),
                    max_entries=max(1, args.max_entries_per_share),
                    exclude_path_regex=exclude_path_regex,
                    extensions=extensions,
                    on_list_error=lambda denied_path, exc, share_name=share_name: _handle_list_error(share_name, denied_path, exc),
                    on_limit_reached=lambda emitted, share_name=share_name: _handle_list_limit(share_name, emitted),
                ):
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
            except SessionError as exc:
                detail = _error_detail(exc)
                message = f"SMB share listing failed for {share_name}: {detail}"
                _emit_error(
                    writer,
                    run_id,
                    severity="warn",
                    code="LIST_SESSION_ERROR",
                    message=message,
                    endpoint_key=endpoint_key,
                    resource_name=share_name,
                    path="\\",
                    hint=_session_error_hint(detail, attempted_auth),
                )
                _record_error(stats, lock, "LIST_SESSION_ERROR", message)
            except (socket.timeout, TimeoutError):
                message = f"SMB share listing timed out for {share_name}."
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
                detail = _error_detail(exc)
                message = f"SMB share listing IO failure for {share_name}: {detail}"
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
                _handle_list_error(share_name, "\\", exc)

        try:
            conn.logoff()
        except (SessionError, OSError):
            pass
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
        detail = _error_detail(exc)
        code = "SMB_NETWORK_FAILED"
        message = f"SMB network failure: {detail}"
        hint = "Verify route, firewall rules, and SMB service availability."
    except (TypeError, ValueError) as exc:
        detail = _error_detail(exc)
        code = "SMB_INPUT_INVALID"
        message = f"SMB scan input is invalid: {detail}"
        hint = "Verify CLI arguments for credentials and filters."

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


def scan_host_nfs(host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock) -> bool:
    endpoint_key = f"{host}:2049"

    try:
        with socket.create_connection((host, 2049), timeout=args.timeout):
            pass
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
            },
            "auth": {
                "method": "none",
                "success": True,
            },
        }
    )
    with lock:
        stats.endpoints += 1

    exports, export_error = _discover_nfs_exports(host, args.timeout)
    if export_error:
        message = f"Failed to enumerate NFS exports on {host}: {export_error}"
        _emit_error(
            writer,
            run_id,
            severity="warn",
            code="NFS_EXPORT_ENUM_FAILED",
            message=message,
            endpoint_key=endpoint_key,
            hint="Install and verify `showmount`, and ensure rpcbind/mountd access from this host.",
        )
        _record_error(stats, lock, "NFS_EXPORT_ENUM_FAILED", message)

    if not exports:
        return True

    for export_path in exports:
        writer.emit(
            {
                "type": "resource",
                "run_id": run_id,
                "endpoint_key": endpoint_key,
                "share_type": "nfs",
                "resource_type": "nfs_share",
                "name": export_path,
                "remark": "",
                "access_level": "list_only",
            }
        )
        with lock:
            stats.resources += 1
    return True


def scan_host(host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock) -> bool:
    selected_share_types = _selected_share_types(args.share_types)
    disabled_share_types = getattr(args, "disabled_share_types", set())
    succeeded = False

    if "smb" in selected_share_types and "smb" not in disabled_share_types:
        succeeded = scan_host_smb(host, args, run_id, writer, stats, lock) or succeeded
    if "nfs" in selected_share_types and "nfs" not in disabled_share_types:
        succeeded = scan_host_nfs(host, args, run_id, writer, stats, lock) or succeeded
    return succeeded


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def upload_artifact(args: argparse.Namespace, run_id: str, artifact_path: str, hosts: list[str]) -> None:
    if not args.upload:
        return

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
        "target_scope": {
            "cidrs": args.cidr,
            "hosts": hosts,
        },
    }
    create_url = f"{base}/projects/{args.project_id}/runs"
    create_resp = _post_with_retries(
        lambda: requests.post(create_url, json=create_payload, headers=headers, timeout=30),
    )
    create_detail = _response_detail(create_resp)
    if create_resp.status_code != 409 or create_detail != "run already exists":
        create_resp.raise_for_status()

    upload_url = f"{base}/projects/{args.project_id}/runs/{run_id}/artifact"
    normalized_artifact_path = artifact_path.lower()
    if normalized_artifact_path.endswith(".gz"):
        content_type = "application/gzip"
    elif normalized_artifact_path.endswith(".ndjson") or normalized_artifact_path.endswith(".jsonl"):
        content_type = "application/x-ndjson"
    else:
        content_type = "application/json"
    upload_resp = _post_with_retries(
        lambda: _upload_artifact_once(upload_url, headers, content_type, artifact_path),
    )
    upload_detail = _response_detail(upload_resp)
    if upload_resp.status_code != 409 or upload_detail != "run state does not accept upload":
        upload_resp.raise_for_status()
    try:
        upload_payload = upload_resp.json()
    except ValueError:
        upload_payload = None
    queued = upload_payload.get("queued") if isinstance(upload_payload, dict) else None
    if queued is False:
        print(
            "upload warning: artifact stored, but ingest queue handoff fell back to asynchronous recovery; monitor the run until ingestion starts.",
            file=sys.stderr,
        )

    print(f"run_id={run_id}")
    print(f"api_run_url={base}/projects/{args.project_id}/runs/{run_id}")


def _upload_artifact_once(url: str, headers: dict[str, str], content_type: str, artifact_path: str) -> requests.Response:
    with open(artifact_path, "rb") as fp:
        return requests.post(
            url,
            data=fp,
            headers={**headers, "Content-Type": content_type},
            timeout=3600,
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


def _post_with_retries(
    request_fn,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 0.5,
) -> requests.Response:
    retriable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(max_attempts):
        try:
            response = request_fn()
        except requests.RequestException:
            if attempt + 1 >= max_attempts:
                raise
        else:
            if response.status_code not in retriable_statuses or attempt + 1 >= max_attempts:
                return response

        sleep_seconds = min(initial_backoff_seconds * (2**attempt), 4.0)
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
        return 1

    return 0 if bool(future.result()) else 1


def _scan_targets(
    targets,
    args: argparse.Namespace,
    run_id: str,
    writer: NDJSONWriter,
    stats: Stats,
    lock: threading.Lock,
) -> tuple[int, int]:
    max_workers = max(1, args.workers)
    max_pending = max_workers * 2
    submitted = 0
    host_failures = 0
    pending: dict[concurrent.futures.Future, str] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for host in targets:
            future = executor.submit(scan_host, host, args, run_id, writer, stats, lock)
            pending[future] = host
            submitted += 1

            if len(pending) < max_pending:
                continue

            done, _ = concurrent.futures.wait(tuple(pending), return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                host = pending.pop(future)
                host_failures += _handle_scan_result(future, host, run_id, writer, stats, lock, args=args)

        if pending:
            host_failures += _collect_scan_results(pending, run_id, writer, stats, lock, args=args)

    return submitted, host_failures


def _scan_thread_error_code(exc: BaseException) -> str:
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
    disabled_share_types, dependency_warnings, dependency_fatals = _validate_runtime_dependencies(set(selected_share_types))
    args.disabled_share_types = disabled_share_types
    smb_auth_method = _resolve_smb_auth_method(args) if "smb" in selected_share_types else "none"

    if dependency_fatals:
        for message in dependency_fatals:
            print(f"configuration error: {message}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        host_inputs = parse_hosts_file(args.hosts)
        targets = iter_targets(args.cidr, host_inputs)
    except (OSError, ValueError) as exc:
        print(f"input error: {_error_detail(exc)}", file=sys.stderr)
        print("fix target inputs and retry (--hosts file and/or --cidr values).", file=sys.stderr)
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
        suffix = ".json.gz" if args.gzip else ".json"
        fd, temp_artifact = tempfile.mkstemp(prefix="share-sentinel-", suffix=suffix)
        os.close(fd)
        output_path = temp_artifact

    writer = NDJSONWriter(output_path, args.gzip)
    stats = Stats()
    lock = threading.Lock()

    run_meta_record = {
        "type": "run_meta",
        "schema_version": 1,
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
                "target_count": None,
                "share_types": selected_share_types,
                "disabled_share_types": sorted(disabled_share_types),
            },
            "enumeration": {
                "workers": args.workers,
                "timeout_seconds": args.timeout,
                "max_depth": args.max_depth,
                "max_entries_per_share": args.max_entries_per_share,
                "include_share": list(getattr(args, "include_share", []) or []),
                "exclude_share": list(getattr(args, "exclude_share", []) or []),
                "exclude_path_regex": args.exclude_path_regex or None,
                "extensions_only": args.extensions_only or None,
            },
        },
        "auth": {
            "mode": (
                ("local" if args.local_auth else "domain")
                if smb_auth_method in {"ntlm", "kerberos"}
                else "none"
            ),
            "domain": (args.domain or None) if smb_auth_method in {"ntlm", "kerberos"} else None,
            "username": (args.username or None) if smb_auth_method in {"ntlm", "kerberos"} else None,
            "method": smb_auth_method,
        },
    }
    writer.emit(run_meta_record)

    for warning in dependency_warnings:
        _emit_error(
            writer,
            run_id,
            severity="warn",
            code="SCAN_DEPENDENCY_WARNING",
            message=warning,
            hint="Install the missing dependency to enable all requested share types.",
        )
        _record_error(stats, lock, "SCAN_DEPENDENCY_WARNING", warning)

    run_ccache = args.ccache_env_value if smb_auth_method == "kerberos" else None
    with _run_scoped_kerberos_cache(run_ccache):
        targets_scanned, host_failures = _scan_targets(targets, args, run_id, writer, stats, lock)

    run_meta_record["collection"]["target_scope"]["target_count"] = targets_scanned
    writer.emit(run_meta_record)

    writer.emit(
        {
            "type": "run_end",
            "run_id": run_id,
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "stats": {
                    "targets_scanned": targets_scanned,
                    "host_failures": host_failures,
                    "endpoints": stats.endpoints,
                    "resources": stats.resources,
                    "items": stats.items,
                "errors": stats.errors,
            },
        }
    )

    run_has_data = _is_successful_run(stats)
    keep_output = run_has_data
    writer_close_error: OSError | None = None
    try:
        writer.close(keep_output=keep_output)
    except OSError as exc:
        writer_close_error = exc

    upload_error: BaseException | None = None
    keep_local_artifact = False
    try:
        if writer_close_error is None and args.upload and keep_output and output_path is not None:
            upload_artifact(args, run_id, output_path, host_inputs)
    except (requests.RequestException, RuntimeError) as exc:
        upload_error = exc
        keep_local_artifact = output_path is not None and os.path.exists(output_path)
    finally:
        if temp_artifact and os.path.exists(temp_artifact) and not keep_local_artifact:
            os.unlink(temp_artifact)

    if writer_close_error is not None:
        destination = output_path or "stdout"
        print(
            f"output error: failed to write output to {destination}: {_error_detail(writer_close_error)}",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    if upload_error is not None:
        print(
            f"upload error: failed to send artifact to Share Sentinel: {_error_detail(upload_error)}",
            file=sys.stderr,
        )
        if keep_local_artifact and output_path:
            print(f"artifact kept at {output_path}", file=sys.stderr)
            return EXIT_PARTIAL
        print("rerun with --output if you want to keep a local artifact copy for retry.", file=sys.stderr)
        return EXIT_FAILURE

    if not keep_output:
        _print_scan_failure_summary(
            stats,
            host_failures,
            reason="scan did not collect any endpoint/resource/item/error records.",
            output_path=args.output,
        )
        return EXIT_FAILURE
    if host_failures > 0:
        return EXIT_PARTIAL
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
