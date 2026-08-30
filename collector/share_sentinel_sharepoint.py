#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from share_sentinel_collector import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    TOOL_VERSION,
    NDJSONWriter,
    upload_artifact,
)
from sharepoint.auth import (
    GRAPH_CLOUD_PROFILES,
    AppCredentialAuthProvider,
    CertificateCredentialAuthProvider,
    ExistingTokenAuthProvider,
    PublicClientAuthProvider,
    TokenAcquisitionError,
    certificate_credential_from_file,
    resolve_graph_cloud,
    token_reader_from_env,
    token_reader_from_file,
    token_reader_from_stdin,
)
from sharepoint.collection import (
    DEFAULT_MAX_GRAPH_HTTP_ATTEMPTS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_LIBRARIES,
    DEFAULT_MAX_SITES,
    SITE_TARGET_MAX_BYTES,
    SharePointCollectionConfig,
    SharePointCollector,
    SharePointProgress,
    SharePointStats,
    collection_context_record,
    collection_dimension_completeness,
)
from sharepoint.graph import GraphAPIError, GraphClient, GraphRunAttemptBudget
from sharepoint.state import (
    SharePointStateStore,
    StateConflictError,
    StateStoreError,
    default_state_path,
)

EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2
EXIT_INTERRUPTED = 130
DEFAULT_TOKEN_ENV = "GRAPH_ACCESS_TOKEN"
DEFAULT_CLIENT_SECRET_ENV = "SHARE_SENTINEL_GRAPH_CLIENT_SECRET"
DEFAULT_CERTIFICATE_PASSPHRASE_ENV = "SHARE_SENTINEL_GRAPH_CERTIFICATE_PASSPHRASE"
DEFAULT_API_TOKEN_ENV = "SHARE_SENTINEL_API_TOKEN"
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_TARGETED_SITES = 128
MAX_TARGETED_SITE_BYTES = 24 * 1024
MAX_PERMISSION_OBJECTS_HARD_LIMIT = 1_000_000
MAX_PERMISSION_HTTP_ATTEMPTS_HARD_LIMIT = 5_000_000
MAX_PERMISSION_ENTRIES_HARD_LIMIT = 5_000_000
MAX_GRAPH_HTTP_ATTEMPTS_HARD_LIMIT = 10_000_000


def _terminal_safe(value: object, maximum: int = 4096) -> str:
    raw = str(value)
    clipped = raw[:maximum]
    output: list[str] = []
    for character in clipped:
        if character.isprintable():
            output.append(character)
        else:
            codepoint = ord(character)
            width = 4 if codepoint <= 0xFFFF else 8
            prefix = "u" if width == 4 else "U"
            output.append(f"\\{prefix}{codepoint:0{width}x}")
    if len(raw) > maximum:
        output.append("…")
    return "".join(output)


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least one")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Inventory SharePoint Online sites, document libraries, filenames, folders, and metadata\n"
            "through Microsoft Graph without downloading document content.\n\n"
            "App authentication provides scheduled tenant inventory. Delegated authentication provides\n"
            "a security-trimmed view of what the assessed user can discover; it is not authoritative\n"
            "tenant inventory."
        ),
    )
    parser.add_argument(
        "--auth",
        required=True,
        choices=("app", "interactive", "wam", "token", "iwa"),
        help="Microsoft Graph authentication provider",
    )
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get("SHARE_SENTINEL_GRAPH_TENANT_ID"),
        help="Entra tenant ID (or SHARE_SENTINEL_GRAPH_TENANT_ID)",
    )
    parser.add_argument(
        "--graph-cloud",
        choices=tuple(GRAPH_CLOUD_PROFILES),
        default=os.environ.get("SHARE_SENTINEL_GRAPH_CLOUD", "global"),
        help=("Microsoft Graph deployment: global (including Microsoft 365 GCC), gcc-high, dod, or china"),
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("SHARE_SENTINEL_GRAPH_CLIENT_ID"),
        help="Entra application/public-client ID (or SHARE_SENTINEL_GRAPH_CLIENT_ID)",
    )
    parser.add_argument(
        "--client-secret-env",
        default=DEFAULT_CLIENT_SECRET_ENV,
        help=f"environment variable containing app secret (default: {DEFAULT_CLIENT_SECRET_ENV})",
    )
    parser.add_argument(
        "--certificate-path",
        default=os.environ.get("SHARE_SENTINEL_GRAPH_CERTIFICATE_PATH"),
        help="user-protected PEM private-key/certificate bundle for app authentication",
    )
    parser.add_argument(
        "--certificate-thumbprint",
        default=os.environ.get("SHARE_SENTINEL_GRAPH_CERTIFICATE_THUMBPRINT"),
        help="optional SHA-1 certificate thumbprint; prefer a bundle with its public certificate",
    )
    parser.add_argument(
        "--certificate-passphrase-env",
        default=DEFAULT_CERTIFICATE_PASSPHRASE_ENV,
        help=(
            "environment variable containing an encrypted PEM key passphrase "
            f"(default: {DEFAULT_CERTIFICATE_PASSPHRASE_ENV})"
        ),
    )
    parser.add_argument(
        "--certificate-send-x5c",
        action="store_true",
        help="send the configured public certificate chain for Subject Name/Issuer authentication",
    )
    parser.add_argument(
        "--login-hint",
        default=None,
        help="delegated account UPN hint; required for IWA",
    )
    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument(
        "--token-env",
        default=None,
        help=f"environment variable containing an imported token (default: {DEFAULT_TOKEN_ENV})",
    )
    token_group.add_argument(
        "--token-file",
        default=None,
        help="user-protected file containing an imported token",
    )
    token_group.add_argument(
        "--token-stdin",
        action="store_true",
        help="read an imported token from stdin",
    )
    parser.add_argument(
        "--token-type",
        choices=("auto", "delegated", "application"),
        default="auto",
        help="token context for opaque imported tokens (JWT tokens are inspected automatically)",
    )
    parser.add_argument(
        "--assessed-identity",
        default=None,
        help="stable user UPN/label required for opaque delegated token attribution",
    )

    parser.add_argument(
        "--site",
        action="append",
        default=[],
        help="target a Graph site ID or HTTPS SharePoint site URL (repeatable)",
    )
    parser.add_argument(
        "--discovery",
        choices=("site-search", "drive-search"),
        default="site-search",
        help="delegated security-trimmed discovery strategy (default: site-search)",
    )
    parser.add_argument("--no-files", action="store_true", help="inventory sites/libraries only")
    sync_group = parser.add_mutually_exclusive_group()
    sync_group.add_argument(
        "--full-sync",
        action="store_true",
        help="ignore checkpoints for this run; replace state only after successful finalization",
    )
    sync_group.add_argument(
        "--reset-delta",
        action="store_true",
        help="perform a safe replacement full sync without deleting working state up front",
    )
    site_limit_group = parser.add_mutually_exclusive_group()
    site_limit_group.add_argument(
        "--max-sites",
        type=_positive_int,
        default=DEFAULT_MAX_SITES,
        help=f"maximum discovered sites (default: {DEFAULT_MAX_SITES})",
    )
    site_limit_group.add_argument(
        "--unlimited-sites",
        action="store_const",
        const=0,
        default=DEFAULT_MAX_SITES,
        dest="max_sites",
        help="explicitly disable the site-count guard; the Graph request budget still applies",
    )
    library_limit_group = parser.add_mutually_exclusive_group()
    library_limit_group.add_argument(
        "--max-libraries",
        type=_positive_int,
        default=DEFAULT_MAX_LIBRARIES,
        help=f"maximum discovered document libraries (default: {DEFAULT_MAX_LIBRARIES})",
    )
    library_limit_group.add_argument(
        "--unlimited-libraries",
        action="store_const",
        const=0,
        default=DEFAULT_MAX_LIBRARIES,
        dest="max_libraries",
        help="explicitly disable the library-count guard; the Graph request budget still applies",
    )
    item_limit_group = parser.add_mutually_exclusive_group()
    item_limit_group.add_argument(
        "--max-items",
        type=_positive_int,
        default=DEFAULT_MAX_ITEMS,
        help=f"maximum materialized items per run (default: {DEFAULT_MAX_ITEMS})",
    )
    item_limit_group.add_argument(
        "--unlimited-items",
        action="store_const",
        const=0,
        default=DEFAULT_MAX_ITEMS,
        dest="max_items",
        help="explicitly disable the item-count guard; artifact and Graph request guards still apply",
    )
    parser.add_argument(
        "--permissions",
        choices=("none", "library_roots", "all_items"),
        default="none",
        help=(
            "collect direct Graph permission evidence: none, library roots only, or every materialized item "
            "(default: none)"
        ),
    )
    parser.add_argument(
        "--max-permission-objects",
        type=_positive_int,
        default=10_000,
        help="maximum library roots/items sent to the Graph permissions endpoint (default: 10000)",
    )
    parser.add_argument(
        "--max-permission-http-attempts",
        type=_positive_int,
        default=25_000,
        help="maximum permission HTTP attempts including pages and retries (default: 25000)",
    )
    parser.add_argument(
        "--max-permission-entries",
        type=_positive_int,
        default=100_000,
        help="maximum normalized permission-entry records emitted (default: 100000)",
    )
    parser.add_argument(
        "--permission-concurrency",
        type=_positive_int,
        default=2,
        help="concurrent direct-permission requests (1-8; default: 2)",
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=100_000,
        help="maximum pages in each Microsoft Graph paging sequence (default: 100000)",
    )
    parser.add_argument(
        "--max-graph-http-attempts",
        type=_positive_int,
        default=DEFAULT_MAX_GRAPH_HTTP_ATTEMPTS,
        help=(
            "run-wide Graph HTTP-attempt budget including retries and permission requests "
            f"(default: {DEFAULT_MAX_GRAPH_HTTP_ATTEMPTS})"
        ),
    )
    parser.add_argument(
        "--graph-concurrency",
        type=_positive_int,
        default=4,
        help="concurrent document-library scans (1-16; default: 4)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=_positive_float,
        default=10.0,
        help="Microsoft Graph connection timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--read-timeout",
        type=_positive_float,
        default=60.0,
        help="Microsoft Graph response read timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--graph-attempts",
        type=_positive_int,
        default=5,
        help="maximum attempts per retriable Graph request (1-20; default: 5)",
    )
    parser.add_argument(
        "--max-retry-delay",
        type=_positive_float,
        default=120.0,
        help="maximum acceptable Graph retry delay in seconds (default: 120)",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=_positive_int,
        default=32 * 1024 * 1024,
        help="maximum bytes accepted in one Graph JSON response (default: 33554432)",
    )
    parser.add_argument(
        "--state-path",
        default=str(default_state_path()),
        help="local SQLite metadata/delta state path",
    )

    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="schema-v1 NDJSON path, .ndjson.gz path, or '-' for stdout (default: -)",
    )
    parser.add_argument("--gzip", action="store_true", help="gzip file output")
    parser.add_argument(
        "--max-artifact-bytes",
        type=_non_negative_int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
    )
    parser.add_argument("--operator-label", default=None)
    parser.add_argument("--run-id", default=None, help="UUID run ID (generated by default)")
    parser.add_argument("--run-name", default="SharePoint assessment")

    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--api-base", default=os.environ.get("SHARE_SENTINEL_API_BASE"))
    parser.add_argument("--project-id", default=os.environ.get("SHARE_SENTINEL_PROJECT_ID"))
    parser.add_argument("--api-token-env", default=DEFAULT_API_TOKEN_ENV)
    parser.add_argument("--upload-timeout", type=_positive_float, default=600.0)
    parser.add_argument("--upload-attempts", type=_positive_int, default=3)

    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="show library details; repeat for additional diagnostics",
    )
    output_group.add_argument(
        "--quiet",
        action="store_true",
        help="suppress authentication, progress, and final summary output",
    )
    parser.add_argument(
        "--progress-interval",
        type=_non_negative_int,
        default=5,
        help="minimum seconds between progress reports (0 disables; default: 5)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if args.graph_concurrency > 16:
        raise TokenAcquisitionError("--graph-concurrency cannot exceed 16")
    if args.graph_attempts > 20:
        raise TokenAcquisitionError("--graph-attempts cannot exceed 20")
    if args.permission_concurrency > 8:
        raise TokenAcquisitionError("--permission-concurrency cannot exceed 8")
    if args.max_permission_objects > MAX_PERMISSION_OBJECTS_HARD_LIMIT:
        raise TokenAcquisitionError(f"--max-permission-objects cannot exceed {MAX_PERMISSION_OBJECTS_HARD_LIMIT}")
    if args.max_permission_http_attempts > MAX_PERMISSION_HTTP_ATTEMPTS_HARD_LIMIT:
        raise TokenAcquisitionError(
            f"--max-permission-http-attempts cannot exceed {MAX_PERMISSION_HTTP_ATTEMPTS_HARD_LIMIT}"
        )
    if args.max_permission_entries > MAX_PERMISSION_ENTRIES_HARD_LIMIT:
        raise TokenAcquisitionError(f"--max-permission-entries cannot exceed {MAX_PERMISSION_ENTRIES_HARD_LIMIT}")
    if args.max_graph_http_attempts > MAX_GRAPH_HTTP_ATTEMPTS_HARD_LIMIT:
        raise TokenAcquisitionError(f"--max-graph-http-attempts cannot exceed {MAX_GRAPH_HTTP_ATTEMPTS_HARD_LIMIT}")
    if args.no_files and args.permissions == "all_items":
        raise TokenAcquisitionError("--permissions all_items cannot be combined with --no-files")
    if len(args.site) > MAX_TARGETED_SITES:
        raise TokenAcquisitionError(f"--site can be repeated at most {MAX_TARGETED_SITES} times")
    targeted_site_bytes = 0
    for reference in args.site:
        try:
            encoded_reference = reference.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TokenAcquisitionError("--site contains invalid Unicode") from exc
        if len(encoded_reference) > SITE_TARGET_MAX_BYTES:
            raise TokenAcquisitionError(f"--site values cannot exceed {SITE_TARGET_MAX_BYTES} UTF-8 bytes")
        targeted_site_bytes += len(encoded_reference)
    if targeted_site_bytes > MAX_TARGETED_SITE_BYTES:
        raise TokenAcquisitionError("combined --site values exceed the safe artifact metadata limit")
    if args.output != "-" and not str(args.output).lower().endswith((".ndjson", ".ndjson.gz", ".jsonl", ".jsonl.gz")):
        raise TokenAcquisitionError(
            "SharePoint output must use .ndjson/.jsonl (optionally .gz) to preserve provider metadata"
        )
    if args.gzip and args.output == "-":
        raise TokenAcquisitionError("--gzip requires file output")
    if args.gzip and not str(args.output).lower().endswith(".gz"):
        raise TokenAcquisitionError("--gzip output path must end in .gz")
    if args.output != "-" and str(args.output).lower().endswith(".gz") and not args.gzip:
        raise TokenAcquisitionError(".gz output requires --gzip")
    if args.upload and (not args.api_base or not args.project_id):
        raise TokenAcquisitionError("--upload requires --api-base and --project-id (or their environment variables)")
    if args.auth != "token" and (args.token_env or args.token_file or args.token_stdin):
        raise TokenAcquisitionError("token input options require --auth token")
    if args.auth != "token" and (args.token_type != "auto" or args.assessed_identity is not None):
        raise TokenAcquisitionError("--token-type and --assessed-identity require --auth token")
    if args.certificate_path and args.auth != "app":
        raise TokenAcquisitionError("--certificate-path requires --auth app")
    if (args.certificate_thumbprint or args.certificate_send_x5c) and not args.certificate_path:
        raise TokenAcquisitionError("certificate thumbprint/x5c options require --certificate-path")
    for field_name, value in (
        ("--client-secret-env", args.client_secret_env),
        ("--certificate-passphrase-env", args.certificate_passphrase_env),
        ("--api-token-env", args.api_token_env),
    ):
        if not ENV_NAME_PATTERN.fullmatch(str(value or "")):
            raise TokenAcquisitionError(f"{field_name} must be a valid environment variable name")
    return args


def _build_auth_provider(args: argparse.Namespace):
    if args.auth == "app":
        secret = os.environ.get(args.client_secret_env, "")
        if args.certificate_path:
            if secret:
                raise TokenAcquisitionError(
                    "both app secret and certificate credentials are configured; unset one credential source"
                )
            passphrase = os.environ.get(args.certificate_passphrase_env, "")
            credential = certificate_credential_from_file(
                args.certificate_path,
                thumbprint=args.certificate_thumbprint,
                passphrase=passphrase or None,
                send_certificate_chain=args.certificate_send_x5c,
            )
            return CertificateCredentialAuthProvider(
                tenant_id=args.tenant_id,
                client_id=args.client_id,
                client_credential=credential,
                cloud=args.graph_cloud,
            )
        return AppCredentialAuthProvider(
            tenant_id=args.tenant_id,
            client_id=args.client_id,
            client_secret=secret,
            cloud=args.graph_cloud,
        )
    if args.auth in {"interactive", "wam", "iwa"}:
        return PublicClientAuthProvider(
            auth_mode=args.auth,
            tenant_id=args.tenant_id,
            client_id=args.client_id,
            login_hint=args.login_hint,
            cloud=args.graph_cloud,
        )
    if args.token_file:
        reader = token_reader_from_file(args.token_file)
    elif args.token_stdin:
        reader = token_reader_from_stdin()
    else:
        reader = token_reader_from_env(args.token_env or DEFAULT_TOKEN_ENV)
    return ExistingTokenAuthProvider(
        reader,
        opaque_auth_type=None if args.token_type == "auto" else args.token_type,
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        assessed_identity=args.assessed_identity,
        cloud=args.graph_cloud,
    )


def _validate_permissions(context, *, targeted_sites: list[str] | tuple[str, ...]) -> None:
    if context.jwt_inspection == "opaque_token_context_supplied_by_operator":
        return
    permissions = tuple(context.scopes) + tuple(context.roles)
    if not any(permission.startswith("Sites.Read") or permission == "Sites.Selected" for permission in permissions):
        raise TokenAcquisitionError(
            "SharePoint site discovery requires a Microsoft Graph Sites.Read permission "
            "(or Sites.Selected with explicit --site targets)"
        )
    tenant_site_read = any(permission.startswith("Sites.Read") for permission in permissions)
    if (
        context.auth_type == "application"
        and not targeted_sites
        and not tenant_site_read
        and "Sites.Selected" in permissions
    ):
        raise TokenAcquisitionError(
            "application Sites.Selected permission is not tenant-discovery capable; "
            "provide one or more --site targets or grant Sites.Read.All"
        )


def _run_id(value: str | None) -> str:
    if not value:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise TokenAcquisitionError("--run-id must be a valid UUID") from exc


def _print_auth_summary(context, *, quiet: bool) -> None:
    if quiet:
        return
    opaque = context.jwt_inspection == "opaque_token_context_supplied_by_operator"
    permissions = ",".join(context.scopes or context.roles)
    identity = context.assessed_identity or "application"
    expiration = context.expires_at.isoformat() if context.expires_at else "unknown"
    audience = "Microsoft Graph (operator asserted)" if opaque else "Microsoft Graph"
    permission_summary = permissions or ("not locally inspectable" if opaque else "none")
    summary = (
        "authentication: "
        f"mode={context.auth_mode} type={context.auth_type} cloud={context.cloud} tenant={context.tenant_id} "
        f"identity={identity} audience={audience} permissions={permission_summary} "
        f"expires={expiration}"
    )
    print(_terminal_safe(summary), file=sys.stderr, flush=True)


def _upload_spool_directory(state_path: str) -> Path:
    """Return a private, persistent upload spool beside the metadata state."""

    state_file = Path(state_path).expanduser()
    directory = state_file.parent / f".{state_file.name}.upload-spool"
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("upload spool path is not a directory")
        if os.name != "nt":
            if info.st_uid != os.geteuid():
                raise OSError("upload spool directory is owned by another user")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.chmod(directory, 0o700)
    except OSError as exc:
        raise StateStoreError("unable to create a private SharePoint upload spool") from exc
    return directory


def _writer_path(args: argparse.Namespace, *, run_id: str) -> tuple[str | None, str | None]:
    if args.upload and args.output == "-":
        spool_directory = _upload_spool_directory(args.state_path)
        fd, temporary_path = tempfile.mkstemp(
            prefix=f"share-sentinel-sharepoint-{run_id}-",
            suffix=".ndjson",
            dir=spool_directory,
        )
        os.close(fd)
        return temporary_path, temporary_path
    return (None if args.output == "-" else str(args.output), None)


def _commit_pending(
    state: SharePointStateStore,
    run_id: str,
    pending_drives,
) -> list[str]:
    failures: list[str] = []
    for pending in pending_drives:
        try:
            state.commit_drive(
                session_id=run_id,
                scope_key=pending.scope_key,
                tenant_id=pending.tenant_id,
                site_id=pending.site_id,
                drive_id=pending.drive_id,
            )
        except StateConflictError:
            failures.append("state changed concurrently for one library; its checkpoint was not advanced")
        except StateStoreError:
            failures.append("unable to commit one library checkpoint")
    return failures


def run(args: argparse.Namespace) -> int:
    run_id = _run_id(args.run_id)
    cloud_profile = resolve_graph_cloud(args.graph_cloud)
    provider = _build_auth_provider(args)
    context = provider.acquire_token()
    if context.cloud != cloud_profile.name:
        raise TokenAcquisitionError("acquired token context does not match the selected Microsoft Graph cloud")
    if args.tenant_id and args.tenant_id not in {"common", "organizations"}:
        if str(args.tenant_id).casefold() != context.tenant_id.casefold():
            raise TokenAcquisitionError("configured tenant ID does not match the acquired token")
    _validate_permissions(context, targeted_sites=args.site)
    _print_auth_summary(context, quiet=args.quiet)

    config = SharePointCollectionConfig(
        discovery=args.discovery,
        targeted_sites=tuple(args.site),
        include_files=not args.no_files,
        full_sync=args.full_sync,
        reset_delta=args.reset_delta,
        max_sites=args.max_sites,
        max_libraries=args.max_libraries,
        max_items=args.max_items,
        max_graph_http_attempts=args.max_graph_http_attempts,
        concurrency=args.graph_concurrency,
        permissions=args.permissions,
        max_permission_objects=args.max_permission_objects,
        max_permission_http_attempts=args.max_permission_http_attempts,
        max_permission_entries=args.max_permission_entries,
        permission_concurrency=args.permission_concurrency,
        quiet=args.quiet,
        verbosity=args.verbose,
        progress_interval=args.progress_interval,
    )
    graph_attempt_budget = GraphRunAttemptBudget(args.max_graph_http_attempts)
    permission_attempt_budget = (
        graph_attempt_budget.scoped("permissions", args.max_permission_http_attempts)
        if args.permissions != "none"
        else None
    )
    client = GraphClient(
        provider,
        cloud=cloud_profile,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_attempts=args.graph_attempts,
        max_retry_delay=args.max_retry_delay,
        max_response_bytes=args.max_response_bytes,
        max_pages=args.max_pages,
        initial_token_context=context,
        attempt_budget=graph_attempt_budget,
    )
    state = SharePointStateStore(args.state_path)
    state.initialize()
    stats = SharePointStats()
    progress = SharePointProgress(
        stats,
        quiet=args.quiet,
        verbosity=args.verbose,
        interval_seconds=args.progress_interval,
    )
    output_path, temporary_artifact = _writer_path(args, run_id=run_id)
    try:
        writer = NDJSONWriter(
            output_path,
            bool(args.gzip or (output_path and output_path.lower().endswith(".gz"))),
            max_spool_bytes=args.max_artifact_bytes,
        )
    except BaseException:
        if temporary_artifact:
            try:
                os.unlink(temporary_artifact)
            except FileNotFoundError:
                pass
        raise
    started_at = datetime.now(tz=UTC).isoformat()
    schema_version = 2 if args.permissions != "none" else 1
    artifact_features = ["direct_permissions_v1"] if schema_version == 2 else None
    initial_context = collection_context_record(
        context,
        config,
        status="running",
        sync_mode="none",
        partial=False,
        graph_attempt_summary=graph_attempt_budget.snapshot().public_metadata(),
    )
    collection_details = {
        "target_scope": {
            "provider": "sharepoint",
            "graph_cloud": cloud_profile.name,
            "targeted_sites": list(args.site),
            "max_sites": args.max_sites,
            "max_libraries": args.max_libraries,
            "max_items": args.max_items,
            "max_graph_http_attempts": args.max_graph_http_attempts,
        },
        "enumeration": {
            "graph_concurrency": args.graph_concurrency,
            "connect_timeout_seconds": args.connect_timeout,
            "read_timeout_seconds": args.read_timeout,
            "max_pages": args.max_pages,
            "include_files": not args.no_files,
        },
    }
    if args.permissions != "none":
        collection_details["permissions"] = {
            "mode": args.permissions,
            "permission_surface": "sharepoint_graph_permissions",
            "semantics": "sharepoint_graph_permission_v1",
            "max_objects": args.max_permission_objects,
            "max_http_attempts": args.max_permission_http_attempts,
            "max_entries": args.max_permission_entries,
            "concurrency": args.permission_concurrency,
        }
    try:
        writer.emit(
            {
                "type": "run_meta",
                "schema_version": schema_version,
                "tool": "share-sentinel-sharepoint-collector",
                "tool_version": TOOL_VERSION,
                "run_id": run_id,
                "started_at": started_at,
                "operator_label": args.operator_label,
                **({"artifact_features": artifact_features} if artifact_features else {}),
                **initial_context,
                "collection": collection_details,
            }
        )
    except BaseException:
        try:
            writer.close(keep_output=False)
        except BaseException:
            pass
        if temporary_artifact:
            try:
                os.unlink(temporary_artifact)
            except FileNotFoundError:
                pass
        raise

    collector = SharePointCollector(
        client=client,
        state=state,
        writer=writer,
        run_id=run_id,
        context=context,
        config=config,
        stats=stats,
        progress=progress,
        permission_attempt_budget=permission_attempt_budget,
    )
    finalized = False
    staging_resolved = False
    upload_confirmed = False
    try:
        pending_drives, status = collector.collect()
        final_stats = stats.snapshot()
        graph_attempt_summary = graph_attempt_budget.snapshot().public_metadata()
        final_stats["graph_http_attempts"] = graph_attempt_summary["http_attempts"]
        final_stats["graph_attempt_budget_exhausted"] = graph_attempt_summary["exhausted"]
        structural_complete, content_complete = collection_dimension_completeness(
            final_stats,
            config,
            status=status,
        )
        final_context = collection_context_record(
            context,
            config,
            status=status,
            sync_mode=collector.sync_mode,
            partial=status == "partial",
            permission_summary=getattr(collector, "permission_run_summary", None),
            graph_attempt_summary=graph_attempt_summary,
            structural_complete=structural_complete,
            content_complete=content_complete,
        )
        # NDJSONWriter retains run metadata out-of-band and replaces the
        # provisional header with this finalized header before atomically
        # writing exactly one run_meta record at the start of the artifact.
        writer.emit(
            {
                "type": "run_meta",
                "schema_version": schema_version,
                "tool": "share-sentinel-sharepoint-collector",
                "tool_version": TOOL_VERSION,
                "run_id": run_id,
                "started_at": started_at,
                "operator_label": args.operator_label,
                **({"artifact_features": artifact_features} if artifact_features else {}),
                **final_context,
                "collection": collection_details,
            }
        )
        final_stats["graph_retries"] = client.retry_count
        permission_stats = getattr(collector, "permission_run_summary", None)
        if isinstance(permission_stats, dict) and args.permissions != "none":
            for field in (
                "candidate_objects",
                "attempted_objects",
                "completed_objects",
                "failed_objects",
                "skipped_objects",
                "http_attempts",
                "entries_observed",
                "entries_emitted",
                "entries_omitted",
                "unknown_entries",
                "anonymous_objects",
                "broad_internal_objects",
                "selection_incomplete_scopes",
            ):
                value = permission_stats.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    final_stats[f"permission_{field}"] = value
            final_stats["permission_partial"] = permission_stats.get("request_coverage") != "complete"
        writer.emit(
            {
                "type": "run_end",
                "run_id": run_id,
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "status": status,
                "stats": {
                    "endpoints": final_stats["endpoints_emitted"],
                    "resources": final_stats["libraries_discovered"],
                    "items": final_stats["items"],
                    "errors": final_stats["graph_errors"],
                    **final_stats,
                },
            }
        )
        writer.close(keep_output=True)
        finalized = True

        upload_status = None
        if args.upload:
            args.api_token = os.environ.get(args.api_token_env, "")
            if not args.api_token:
                raise RuntimeError(f"API token environment variable {args.api_token_env} is empty or unset")
            args.cidr = []
            args.progress_reporter = None
            args.target_scope_override = {
                "provider": "sharepoint",
                "collection_mode": collector.collection_mode,
                "tenant_id": context.tenant_id,
                "targeted_sites": list(args.site),
                **({"permissions": args.permissions} if args.permissions != "none" else {}),
            }
            upload_status = upload_artifact(args, run_id, str(output_path), hosts=[])
            upload_confirmed = upload_status in {"accepted", "recovered"}
            if not upload_confirmed:
                raise RuntimeError("upload did not return a confirmed acceptance state")

        if output_path is None and pending_drives:
            commit_failures = ["stdout is not a durable artifact; delta checkpoints were not advanced"]
        else:
            commit_failures = _commit_pending(state, run_id, pending_drives)
        try:
            state.discard_session(run_id)
        except StateStoreError:
            commit_failures.append("unable to clean completed staging state")
        staging_resolved = True
        if commit_failures:
            for failure in commit_failures:
                print(f"checkpoint warning: {failure}", file=sys.stderr)
        progress.finish(status=status, graph_retries=client.retry_count)
        if upload_status and not args.quiet:
            print(f"upload: {upload_status}", file=sys.stderr)
        if commit_failures:
            # The uploaded/local artifact is still a complete snapshot. Exit
            # partial only to make the local replay/checkpoint condition visible.
            return EXIT_PARTIAL
        if status == "success":
            return EXIT_SUCCESS
        if status == "partial":
            return EXIT_PARTIAL
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("SharePoint collection interrupted; no delta checkpoints were advanced.", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        if not finalized:
            try:
                writer.close(keep_output=False)
            except BaseException:
                pass
        if not staging_resolved:
            try:
                state.discard_session(run_id)
            except BaseException:
                pass
        if temporary_artifact:
            if finalized and not upload_confirmed:
                print(
                    _terminal_safe(f"artifact preserved for upload recovery: {temporary_artifact}"),
                    file=sys.stderr,
                )
            else:
                try:
                    os.unlink(temporary_artifact)
                except FileNotFoundError:
                    pass


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return run(args)
    except (TokenAcquisitionError, GraphAPIError, StateStoreError, RuntimeError, OSError) as exc:
        print(_terminal_safe(f"SharePoint collector error: {exc}"), file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
