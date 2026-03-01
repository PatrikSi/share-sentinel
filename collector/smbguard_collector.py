#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import gzip
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import time

import requests

try:
    from impacket.smbconnection import SMBConnection, SessionError
except ImportError:
    SMBConnection = None

    class SessionError(Exception):
        """Fallback error type used when impacket is unavailable."""


@dataclass
class Stats:
    endpoints: int = 0
    resources: int = 0
    items: int = 0
    errors: int = 0


class NDJSONWriter:
    def __init__(self, path: str | None, gzip_output: bool):
        self._lock = threading.Lock()
        self._path = path
        self._gzip = gzip_output
        self._closed = False

        if path is None:
            self._fp = sys.stdout
            self._is_binary = False
        else:
            if gzip_output:
                self._fp = gzip.open(path, "wt", encoding="utf-8")
                self._is_binary = False
            else:
                self._fp = open(path, "w", encoding="utf-8")
                self._is_binary = False

    def emit(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=True)
        with self._lock:
            if self._closed:
                return
            self._fp.write(line + "\n")
            if self._path is None:
                self._fp.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._path is not None:
                self._fp.close()


class _CollectorHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect SMB and/or NFS share inventory and output NDJSON records.\n\n"
            "Most common workflow:\n"
            "  1) Pick targets with --hosts and/or --cidr\n"
            "  2) Choose --share-types smb|nfs|both\n"
            "  3) For SMB choose authenticated credentials or anonymous mode\n"
            "  4) Save with --output and optionally upload with --upload"
        ),
        epilog=(
            "Examples:\n"
            "  Authenticated SMB scan:\n"
            "    smbguard_collector.py --hosts hosts.txt --share-types smb --username corp\\\\svc_scan --password '***' --output run.ndjson.gz --gzip\n\n"
            "  Anonymous SMB scan:\n"
            "    smbguard_collector.py --cidr 10.20.0.0/24 --share-types smb --smb-anonymous --output anon.ndjson\n\n"
            "  NFS + SMB combined scan:\n"
            "    smbguard_collector.py --hosts hosts.txt --share-types both --username corp\\\\svc_scan --password '***' --output combined.ndjson.gz --gzip\n\n"
            "  Upload after scan:\n"
            "    smbguard_collector.py --hosts hosts.txt --share-types smb --username svc --password '***' --upload --api-base https://api.example --project-id <uuid> --api-token <token>\n\n"
            "Notes:\n"
            "  - SMB authentication modes:\n"
            "      * NTLM: set --username (and password or hashes)\n"
            "      * Kerberos: add --kerberos and --username\n"
            "      * Anonymous: set --smb-anonymous or omit SMB credentials\n"
            "  - NFS export enumeration uses `showmount -e`; if unavailable, host reachability is still recorded."
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
    common.add_argument("--output", type=str, help="Write NDJSON output to this file. Defaults to stdout unless --upload needs a temp file.")
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

    tuning = parser.add_argument_group("Enumeration Tuning")
    tuning.add_argument("--max-depth", type=int, default=1, help="Max directory traversal depth per share.")
    tuning.add_argument("--max-entries-per-share", type=int, default=5000, help="Cap listed entries per share/export.")
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


def parse_targets(cidrs: list[str], hosts_file: str | None) -> list[str]:
    targets: set[str] = set()
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        for host in network.hosts():
            targets.add(str(host))

    if hosts_file:
        for line in Path(hosts_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                targets.add(line)

    return sorted(targets)


def parse_hosts_file(hosts_file: str | None) -> list[str]:
    if not hosts_file:
        return []
    hosts: list[str] = []
    for line in Path(hosts_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            hosts.append(line)
    return hosts


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
):
    queue = collections.deque([("", 0)])
    emitted = 0

    while queue and emitted < max_entries:
        rel_path, depth = queue.popleft()
        wildcard = f"{rel_path}\\*" if rel_path else "*"

        try:
            entries = conn.listPath(share_name, wildcard)
        except SessionError:
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

            if emitted >= max_entries:
                break
            if is_dir and depth + 1 < max_depth:
                queue.append((full_path.strip("\\"), depth + 1))


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
    if args.kerberos:
        return "kerberos"
    if args.username:
        return "ntlm"
    return "anonymous"


def _validate_args(args: argparse.Namespace) -> None:
    selected_share_types = _selected_share_types(args.share_types)
    if not args.cidr and not args.hosts:
        raise SystemExit("at least one target source is required: --hosts and/or --cidr")

    if args.kerberos and args.smb_anonymous:
        raise SystemExit("--kerberos cannot be combined with --smb-anonymous")

    if "smb" in selected_share_types:
        if args.kerberos and not args.username:
            raise SystemExit("--kerberos requires --username")
        if args.hashes and not args.username and not args.smb_anonymous:
            raise SystemExit("--hashes requires --username unless --smb-anonymous is set")
        if args.password and not args.username and not args.smb_anonymous:
            raise SystemExit("--password requires --username unless --smb-anonymous is set")

    if args.upload and (not args.api_base or not args.project_id or not args.api_token):
        raise SystemExit("--upload requires --api-base, --project-id, and --api-token")


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


def scan_host_smb(host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock) -> bool:
    endpoint_key = f"{host}:445"
    attempted_auth = _resolve_smb_auth_method(args)
    if SMBConnection is None:
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "IMPACKET_NOT_AVAILABLE",
                "message": "impacket is not installed; install it with `pip install impacket` to scan SMB shares",
                "endpoint_key": endpoint_key,
            }
        )
        with lock:
            stats.errors += 1
        return False

    try:
        conn = SMBConnection(host, host, sess_port=445, timeout=args.timeout)
        if attempted_auth == "kerberos":
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
                useCache=bool(args.ccache),
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
        exclude_path_regex = re.compile(args.exclude_path_regex) if args.exclude_path_regex else None
        extensions = None
        if args.extensions_only:
            extensions = {e.strip().lower() for e in args.extensions_only.split(",") if e.strip()}

        shares = conn.listShares()
        for share in shares:
            share_name = share["shi1_netname"].rstrip("\x00")
            if share_name.upper() in excluded_shares:
                continue

            remark = share.get("shi1_remark", "")
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
                writer.emit(
                    {
                        "type": "error",
                        "run_id": run_id,
                        "severity": "warn",
                        "code": "LIST_SESSION_ERROR",
                        "message": f"failed listing SMB share {share_name}: {exc}",
                        "endpoint_key": endpoint_key,
                        "resource_name": share_name,
                        "path": "\\",
                    }
                )
                with lock:
                    stats.errors += 1
            except (socket.timeout, TimeoutError):
                writer.emit(
                    {
                        "type": "error",
                        "run_id": run_id,
                        "severity": "warn",
                        "code": "LIST_TIMEOUT",
                        "message": f"listing SMB share {share_name} timed out",
                        "endpoint_key": endpoint_key,
                        "resource_name": share_name,
                        "path": "\\",
                    }
                )
                with lock:
                    stats.errors += 1
            except OSError as exc:
                writer.emit(
                    {
                        "type": "error",
                        "run_id": run_id,
                        "severity": "warn",
                        "code": "LIST_IO_ERROR",
                        "message": f"io error listing SMB share {share_name}: {exc}",
                        "endpoint_key": endpoint_key,
                        "resource_name": share_name,
                        "path": "\\",
                    }
                )
                with lock:
                    stats.errors += 1

        try:
            conn.logoff()
        except (SessionError, OSError):
            pass
        return True

    except (socket.timeout, TimeoutError):
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "SMB_TIMEOUT",
                "message": "SMB connection to tcp/445 timed out",
                "endpoint_key": endpoint_key,
            }
        )
    except SessionError as exc:
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "SMB_AUTH_FAILED",
                "message": f"SMB {attempted_auth} session failed: {exc}",
                "endpoint_key": endpoint_key,
            }
        )
    except (OSError, ConnectionError) as exc:
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "SMB_NETWORK_FAILED",
                "message": f"SMB network failure: {exc}",
                "endpoint_key": endpoint_key,
            }
        )
    except (TypeError, ValueError) as exc:
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "SMB_INPUT_INVALID",
                "message": f"SMB scan input is invalid: {exc}",
                "endpoint_key": endpoint_key,
            }
        )

    with lock:
        stats.errors += 1
    return False


def scan_host_nfs(host: str, args: argparse.Namespace, run_id: str, writer: NDJSONWriter, stats: Stats, lock: threading.Lock) -> bool:
    endpoint_key = f"{host}:2049"

    try:
        with socket.create_connection((host, 2049), timeout=args.timeout):
            pass
    except (socket.timeout, TimeoutError):
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "NFS_TIMEOUT",
                "message": "NFS connection to tcp/2049 timed out",
                "endpoint_key": endpoint_key,
            }
        )
        with lock:
            stats.errors += 1
        return False
    except OSError as exc:
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "error",
                "code": "NFS_CONNECT_FAILED",
                "message": f"NFS connectivity failure: {exc}",
                "endpoint_key": endpoint_key,
            }
        )
        with lock:
            stats.errors += 1
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
        writer.emit(
            {
                "type": "error",
                "run_id": run_id,
                "severity": "warn",
                "code": "NFS_EXPORT_ENUM_FAILED",
                "message": f"failed to enumerate NFS exports on {host}: {export_error}",
                "endpoint_key": endpoint_key,
            }
        )
        with lock:
            stats.errors += 1

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
    succeeded = False

    if "smb" in selected_share_types:
        succeeded = scan_host_smb(host, args, run_id, writer, stats, lock) or succeeded
    if "nfs" in selected_share_types:
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
    content_type = "application/gzip" if artifact_path.endswith(".gz") else "application/x-ndjson"
    upload_resp = _post_with_retries(
        lambda: _upload_artifact_once(upload_url, headers, content_type, artifact_path),
    )
    upload_detail = _response_detail(upload_resp)
    if upload_resp.status_code != 409 or upload_detail != "run state does not accept upload":
        upload_resp.raise_for_status()

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
) -> int:
    host_failures = 0
    for future in concurrent.futures.as_completed(futures_by_host):
        host = futures_by_host[future]
        try:
            exc = future.exception()
        except concurrent.futures.CancelledError as cancelled_error:
            exc = cancelled_error

        if exc is not None:
            message = str(exc) or type(exc).__name__
            writer.emit(
                {
                    "type": "error",
                    "run_id": run_id,
                    "severity": "error",
                    "code": _scan_thread_error_code(exc),
                    "message": message,
                    "endpoint_key": f"{host}:445",
                }
            )
            with lock:
                stats.errors += 1
            host_failures += 1
            continue

        ok = bool(future.result())
        if not ok:
            host_failures += 1
    return host_failures


def _scan_thread_error_code(exc: BaseException) -> str:
    if isinstance(exc, concurrent.futures.CancelledError):
        return "SCAN_THREAD_CANCELLED"
    if isinstance(exc, SessionError):
        return "SCAN_SESSION_ERROR"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "SCAN_TIMEOUT"
    if isinstance(exc, OSError):
        return "SCAN_IO_ERROR"
    if isinstance(exc, (TypeError, ValueError)):
        return "SCAN_INPUT_ERROR"
    return "SCAN_THREAD_FAILED"


def main() -> int:
    args = parse_args()
    _validate_args(args)
    selected_share_types = sorted(_selected_share_types(args.share_types))
    smb_auth_method = _resolve_smb_auth_method(args) if "smb" in selected_share_types else "none"

    targets = parse_targets(args.cidr, args.hosts)
    host_inputs = parse_hosts_file(args.hosts)
    run_id = str(uuid.uuid4())
    started_at = datetime.now(tz=UTC)

    temp_artifact: str | None = None
    output_path = args.output

    if args.upload and output_path is None:
        suffix = ".ndjson.gz" if args.gzip else ".ndjson"
        fd, temp_artifact = tempfile.mkstemp(prefix="smbguard-", suffix=suffix)
        os.close(fd)
        output_path = temp_artifact

    writer = NDJSONWriter(output_path, args.gzip)
    stats = Stats()
    lock = threading.Lock()

    writer.emit(
        {
            "type": "run_meta",
            "schema_version": 1,
            "tool": "smbguard-collector",
            "tool_version": "0.1.0",
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "operator_label": args.operator_label,
            "target_scope": {
                "cidrs": args.cidr,
                "hosts": host_inputs,
                "share_types": selected_share_types,
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
    )

    host_failures = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures_by_host = {executor.submit(scan_host, host, args, run_id, writer, stats, lock): host for host in targets}
        host_failures = _collect_scan_results(futures_by_host, run_id, writer, stats, lock)

    writer.emit(
        {
            "type": "run_end",
            "run_id": run_id,
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "stats": {
                "endpoints": stats.endpoints,
                "resources": stats.resources,
                "items": stats.items,
                "errors": stats.errors,
            },
        }
    )
    writer.close()

    try:
        if args.upload and output_path is not None:
            upload_artifact(args, run_id, output_path, host_inputs)
    finally:
        if temp_artifact and os.path.exists(temp_artifact):
            os.unlink(temp_artifact)

    if stats.endpoints == 0 and stats.resources == 0 and stats.items == 0 and stats.errors == 0:
        return 3
    if host_failures > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
