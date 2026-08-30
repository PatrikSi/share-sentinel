from __future__ import annotations

import concurrent.futures
import hashlib
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, Protocol
from urllib.parse import quote, unquote, urlparse

from .auth import GraphTokenContext
from .graph import GraphAPIError, GraphAttemptBudget, GraphClient, GraphProtocolError
from .permissions import (
    DirectPermissionCollector,
    PermissionAssessmentResult,
    PermissionSubject,
    not_requested_permission_summary,
)
from .state import DriveState, SharePointStateStore, StateStoreError, state_scope_key

CORE_DRIVE_SELECT = "id,name,description,driveType,webUrl,createdDateTime,lastModifiedDateTime"
DRIVE_SELECT = f"{CORE_DRIVE_SELECT},createdBy,lastModifiedBy,owner,quota,system"
CORE_SITE_SELECT = "id,name,displayName,webUrl,createdDateTime,lastModifiedDateTime,siteCollection"
SITE_SELECT = f"{CORE_SITE_SELECT},root,isPersonalSite"
CORE_ITEM_SELECT = (
    "id,name,parentReference,file,folder,root,size,createdDateTime,lastModifiedDateTime,webUrl,eTag,cTag,deleted"
)
ITEM_SELECT = f"{CORE_ITEM_SELECT},createdBy,lastModifiedBy"
GOVERNANCE_SELECT_LIMITATION = "optional_graph_governance_select_rejected"
ITEM_NAME_MAX_CHARACTERS = 255
ITEM_PATH_MAX_CHARACTERS = 400
ITEM_PATH_MAX_BYTES = 2000
RESOURCE_NAME_MAX_CHARACTERS = 255
URL_MAX_BYTES = 8192
SITE_TARGET_MAX_BYTES = 4096
PROVIDER_ID_MAX_CHARACTERS = 512
METADATA_TEXT_MAX_CHARACTERS = 4096
SHAREPOINT_HOST_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.sharepoint\.com$")
INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
MAX_PATH_DECODE_PASSES = 16
GRAPH_ARCHIVE_STATUS = {
    "recentlyArchived": "recently_archived",
    "fullyArchived": "fully_archived",
    "reactivating": "reactivating",
}
GRAPH_FILE_ARCHIVE_STATUS = {
    "notArchived": "not_archived",
    "fullyArchived": "fully_archived",
    "reactivating": "reactivating",
}
SHAREPOINT_STRUCTURAL_COMPARISON_CONTRACT = "sharepoint_resource_inventory_v1"
SHAREPOINT_CONTENT_COMPARISON_CONTRACT = "sharepoint_drive_inventory_v1"
DEFAULT_MAX_SITES = 10_000
DEFAULT_MAX_LIBRARIES = 50_000
DEFAULT_MAX_ITEMS = 2_000_000
DEFAULT_MAX_GRAPH_HTTP_ATTEMPTS = 250_000


def _terminal_safe(value: object, maximum: int = 4096) -> str:
    raw = str(value)
    limit = max(0, int(maximum))
    output: list[str] = []
    output_length = 0
    truncated = False
    for character in raw:
        if character.isprintable():
            safe_character = character
        else:
            codepoint = ord(character)
            width = 4 if codepoint <= 0xFFFF else 8
            prefix = "u" if width == 4 else "U"
            safe_character = f"\\{prefix}{codepoint:0{width}x}"
        if output_length + len(safe_character) > limit:
            truncated = True
            break
        output.append(safe_character)
        output_length += len(safe_character)
    if truncated and limit:
        while output and output_length + 1 > limit:
            output_length -= len(output.pop())
        if output_length < limit:
            output.append("…")
    return "".join(output)


class ArtifactWriter(Protocol):
    def emit(self, record: dict[str, object]) -> None: ...


@dataclass(frozen=True)
class Site:
    site_id: str
    name: str
    display_name: str | None
    web_url: str | None
    hostname: str | None
    site_collection_hostname: str | None
    created_at: str | None
    modified_at: str | None
    existence_status: str = "confirmed_from_discovery"
    archive_status: str = "unknown"
    archive_status_checked: bool = False
    archive_status_authoritative: bool = False
    requested_target: str | None = None
    data_location_code: str | None = None
    is_root_site: bool | None = None
    is_personal_site: bool | None = None
    governance_observation: str = "best_effort_provider_response"
    governance_limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Drive:
    site: Site
    drive_id: str
    name: str
    web_url: str | None
    drive_type: str | None
    description: str | None
    created_at: str | None
    modified_at: str | None
    owner: dict[str, object] | None = None
    owner_observation: str = "not_returned"
    created_by: dict[str, object] | None = None
    created_by_observation: str = "not_returned"
    last_modified_by: dict[str, object] | None = None
    last_modified_by_observation: str = "not_returned"
    quota: dict[str, object] | None = None
    quota_observation: str = "not_returned"
    system_managed: bool | None = None
    system_observation: str = "not_returned"
    governance_observation: str = "selected"
    governance_limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingDrive:
    scope_key: str
    tenant_id: str
    site_id: str
    drive_id: str
    sync_mode: str
    item_count: int
    file_count: int
    folder_count: int
    total_size_bytes: int | None
    size_observation_complete: bool
    archived_file_count: int
    reactivating_file_count: int
    active_file_count: int
    unknown_file_archive_count: int
    item_governance_observation: str = "selected"
    descendant_permission_summary: dict[str, object] | None = None


@dataclass(frozen=True)
class LibraryObservation:
    enumeration_status: str
    content_state: str
    file_count: int | None
    folder_count: int | None
    item_count: int | None
    total_size_bytes: int | None
    collection_complete: bool
    sync_mode: str
    size_observation_complete: bool | None = None
    enumeration_error_code: str | None = None
    archived_file_count: int | None = None
    reactivating_file_count: int | None = None
    active_file_count: int | None = None
    unknown_file_archive_count: int | None = None
    item_governance_observation: str | None = None
    permission_result: PermissionAssessmentResult | None = None
    descendant_permission_summary: dict[str, object] | None = None


@dataclass
class SharePointStats:
    sites_discovered: int = 0
    sites_failed: int = 0
    endpoints_emitted: int = 0
    sites_archived: int = 0
    sites_not_found: int = 0
    sites_not_found_or_not_visible: int = 0
    sites_inaccessible: int = 0
    sites_indeterminate: int = 0
    libraries_discovered: int = 0
    libraries_succeeded: int = 0
    libraries_failed: int = 0
    items_emitted: int = 0
    items_changed: int = 0
    items_deleted: int = 0
    files: int = 0
    folders: int = 0
    archived_files: int = 0
    reactivating_files: int = 0
    delta_drives: int = 0
    full_drives: int = 0
    delta_resets: int = 0
    errors: int = 0
    truncated: bool = False
    error_codes: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def increment(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field_name, int(getattr(self, field_name)) + amount)

    def record_error(self, code: str) -> None:
        with self._lock:
            self.errors += 1
            self.error_codes[code] = self.error_codes.get(code, 0) + 1

    def mark_truncated(self) -> None:
        with self._lock:
            self.truncated = True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "sites_discovered": self.sites_discovered,
                "sites_failed": self.sites_failed,
                "endpoints_emitted": self.endpoints_emitted,
                "sites_archived": self.sites_archived,
                "sites_not_found": self.sites_not_found,
                "sites_not_found_or_not_visible": self.sites_not_found_or_not_visible,
                "sites_inaccessible": self.sites_inaccessible,
                "sites_indeterminate": self.sites_indeterminate,
                "libraries_discovered": self.libraries_discovered,
                "libraries_succeeded": self.libraries_succeeded,
                "libraries_failed": self.libraries_failed,
                "items": self.items_emitted,
                "items_changed": self.items_changed,
                "items_deleted": self.items_deleted,
                "files": self.files,
                "folders": self.folders,
                "archived_files": self.archived_files,
                "reactivating_files": self.reactivating_files,
                "delta_drives": self.delta_drives,
                "full_drives": self.full_drives,
                "delta_resets": self.delta_resets,
                "graph_errors": self.errors,
                "truncated": self.truncated,
                "error_codes": dict(sorted(self.error_codes.items())),
            }


@dataclass(frozen=True)
class SharePointCollectionConfig:
    discovery: str = "site-search"
    targeted_sites: tuple[str, ...] = ()
    include_files: bool = True
    full_sync: bool = False
    reset_delta: bool = False
    max_sites: int = DEFAULT_MAX_SITES
    max_libraries: int = DEFAULT_MAX_LIBRARIES
    max_items: int = DEFAULT_MAX_ITEMS
    max_graph_http_attempts: int = DEFAULT_MAX_GRAPH_HTTP_ATTEMPTS
    concurrency: int = 4
    permissions: str = "none"
    max_permission_objects: int = 10_000
    max_permission_http_attempts: int = 25_000
    max_permission_entries: int = 100_000
    permission_concurrency: int = 2
    quiet: bool = False
    verbosity: int = 0
    progress_interval: float = 5.0


class _ItemBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = max(0, int(maximum))
        self._reservations: dict[str, int] = {}
        self._used = 0
        self._lock = threading.Lock()

    def resize(self, key: str, count: int) -> bool:
        requested = max(0, int(count))
        with self._lock:
            previous = self._reservations.get(key, 0)
            new_total = self._used - previous + requested
            if self.maximum and new_total > self.maximum:
                return False
            self._reservations[key] = requested
            self._used = new_total
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._used -= self._reservations.pop(key, 0)


class SharePointProgress:
    def __init__(
        self,
        stats: SharePointStats,
        *,
        quiet: bool,
        verbosity: int,
        interval_seconds: float,
        stream=None,
    ) -> None:
        self.stats = stats
        self.quiet = quiet
        self.verbosity = verbosity
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.stream = stream if stream is not None else sys.stderr
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._last_report = 0.0
        self._total_libraries: int | None = None
        self._processed_libraries = 0
        self._total_site_statuses: int | None = None
        self._processed_site_statuses = 0

    def start(self, context: GraphTokenContext, collection_mode: str) -> None:
        if self.quiet:
            return
        identity = context.assessed_identity or "application"
        self._write(
            f"SharePoint collection started: mode={collection_mode} auth={context.auth_mode}/"
            f"{context.auth_type} identity={identity}"
        )

    def set_library_total(self, total: int) -> None:
        with self._lock:
            self._total_libraries = total
        self.report(force=True)

    def set_site_status_total(self, total: int) -> None:
        with self._lock:
            self._total_site_statuses = max(0, total)
            self._processed_site_statuses = 0
        self.report(force=True)

    def site_status_finished(self, site: Site, *, succeeded: bool) -> None:
        with self._lock:
            self._processed_site_statuses += 1
        if self.verbosity >= 1 and not self.quiet:
            self._write(f"site lifecycle {site.display_name or site.name}: {'ok' if succeeded else 'indeterminate'}")
        self.report()

    def detail(self, message: str, *, level: int = 1) -> None:
        if not self.quiet and self.verbosity >= level:
            self._write(message)

    def library_finished(self, drive: Drive, *, succeeded: bool) -> None:
        with self._lock:
            self._processed_libraries += 1
        if self.verbosity >= 1 and not self.quiet:
            self._write(
                f"library {drive.site.display_name or drive.site.name}/{drive.name}: {'ok' if succeeded else 'failed'}"
            )
        self.report()

    def report(self, *, force: bool = False) -> None:
        if self.quiet or self.interval_seconds == 0:
            return
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_report < self.interval_seconds:
                return
            self._last_report = now
            processed = self._processed_libraries
            total = self._total_libraries
            processed_site_statuses = self._processed_site_statuses
            total_site_statuses = self._total_site_statuses
        stats = self.stats.snapshot()
        remaining = "unknown" if total is None else str(max(0, total - processed))
        site_status = (
            "" if total_site_statuses is None else f" lifecycle={processed_site_statuses}/{total_site_statuses}"
        )
        elapsed = max(0.0, now - self.started)
        self._write(
            "progress: "
            f"sites={stats['sites_discovered']}{site_status} libraries={processed}/"
            f"{total if total is not None else 'unknown'} remaining={remaining} "
            f"items={stats['items']} failures={stats['libraries_failed']} "
            f"elapsed={elapsed:.1f}s"
        )

    def finish(self, *, status: str, graph_retries: int) -> None:
        if self.quiet:
            return
        stats = self.stats.snapshot()
        self._write(
            f"SharePoint collection finished: status={status} sites={stats['sites_discovered']} "
            f"libraries={stats['libraries_succeeded']}/{stats['libraries_discovered']} "
            f"items={stats['items']} files={stats['files']} folders={stats['folders']} "
            f"errors={stats['graph_errors']} graph_retries={graph_retries}"
        )

    def _write(self, message: str) -> None:
        with self._lock:
            print(_terminal_safe(message), file=self.stream, flush=True)


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if "\x00" in normalized:
        return None
    return normalized if len(normalized) <= maximum else None


def _bounded_exact_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or not value.strip() or "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if len(value) <= maximum else None


def _bounded_url(value: object) -> str | None:
    normalized = _bounded_text(value, URL_MAX_BYTES)
    if normalized is None or len(normalized.encode("utf-8")) > URL_MAX_BYTES:
        return None
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return normalized


def _archive_status_from_graph(
    raw_site_collection: object,
    *,
    explicitly_selected: bool,
) -> tuple[str, bool, bool]:
    if raw_site_collection is None:
        # The siteCollection facet is only present on a site-collection root.
        # A caller can resolve that root separately when assessing a subsite.
        return "unknown", False, False
    if not isinstance(raw_site_collection, dict):
        raise GraphProtocolError(status_code=None, code="site_collection_facet_invalid")

    raw_details = raw_site_collection.get("archivalDetails")
    if raw_details is None:
        if explicitly_selected:
            # archiveStatus has no active value. When the siteCollection facet
            # was explicitly selected, absence of archivalDetails is consistent
            # with a collection that is not archived. Graph does not explicitly
            # guarantee that absence across every cloud, so record the check as
            # an inference rather than authoritative provider evidence.
            return "not_archived", True, False
        return "unknown", False, False
    if not isinstance(raw_details, dict):
        raise GraphProtocolError(status_code=None, code="site_archival_details_invalid")

    raw_status = raw_details.get("archiveStatus")
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise GraphProtocolError(status_code=None, code="site_archive_status_invalid")
    if raw_status == "unknownFutureValue":
        return "unknown", True, False
    normalized = GRAPH_ARCHIVE_STATUS.get(raw_status)
    if normalized is None:
        # Preserve forward compatibility without treating an unknown provider
        # value as active or archived.
        return "unknown", True, False
    return normalized, True, True


def _identity_set_metadata(raw: object) -> tuple[dict[str, object] | None, str]:
    if raw is None:
        return None, "not_returned"
    if not isinstance(raw, dict):
        return None, "invalid"

    normalized: dict[str, object] = {}
    invalid_member = False
    for identity_type in ("user", "application", "device", "group", "siteUser", "siteGroup"):
        member = raw.get(identity_type)
        if member is None:
            continue
        if not isinstance(member, dict):
            invalid_member = True
            continue
        identity: dict[str, object] = {}
        native_id = _bounded_exact_text(member.get("id"), PROVIDER_ID_MAX_CHARACTERS)
        display_name = _bounded_text(member.get("displayName"), RESOURCE_NAME_MAX_CHARACTERS)
        tenant_id = _bounded_exact_text(member.get("tenantId"), PROVIDER_ID_MAX_CHARACTERS)
        if native_id:
            identity["id"] = native_id
        if display_name:
            identity["display_name"] = display_name
        if tenant_id:
            identity["tenant_id"] = tenant_id
        if identity:
            normalized[identity_type] = identity
    if normalized:
        return normalized, "partial" if invalid_member else "observed"
    return None, "invalid" if raw or invalid_member else "observed_empty"


def _quota_metadata(raw: object) -> tuple[dict[str, object] | None, str]:
    if raw is None:
        return None, "not_returned"
    if not isinstance(raw, dict):
        return None, "invalid"
    normalized: dict[str, object] = {}
    invalid_value = False
    for field_name in ("deleted", "remaining", "total", "used"):
        value = raw.get(field_name)
        if value is None:
            continue
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
            normalized[field_name] = value
        else:
            invalid_value = True
    state = _bounded_text(raw.get("state"), 64)
    if state:
        normalized["state"] = state
    elif raw.get("state") is not None:
        invalid_value = True
    if normalized:
        return normalized, "partial" if invalid_value else "observed"
    return None, "invalid" if raw or invalid_value else "observed_empty"


def _site_from_graph(
    raw: dict[str, object],
    *,
    archive_status_selected: bool = False,
    existence_status: str = "confirmed_from_discovery",
    requested_target: str | None = None,
    governance_available: bool | None = None,
) -> Site:
    site_id = _bounded_exact_text(raw.get("id"), PROVIDER_ID_MAX_CHARACTERS)
    if not site_id:
        raise GraphProtocolError(status_code=None, code="site_missing_id")
    web_url = _bounded_url(raw.get("webUrl"))
    site_collection = raw.get("siteCollection")
    collection_hostname = None
    if isinstance(site_collection, dict):
        collection_hostname = _bounded_text(
            site_collection.get("hostname") or site_collection.get("hostName"),
            255,
        )
    data_location_code = (
        _bounded_text(site_collection.get("dataLocationCode"), 64)
        if governance_available is not False and isinstance(site_collection, dict)
        else None
    )
    archive_status, archive_status_checked, archive_status_authoritative = _archive_status_from_graph(
        site_collection,
        explicitly_selected=archive_status_selected,
    )
    web_hostname = urlparse(web_url).hostname if web_url else None
    name = _bounded_exact_text(raw.get("name"), RESOURCE_NAME_MAX_CHARACTERS)
    display_name = _bounded_exact_text(raw.get("displayName"), RESOURCE_NAME_MAX_CHARACTERS)
    if not name and not display_name:
        raise GraphProtocolError(status_code=None, code="site_missing_name")
    return Site(
        site_id=site_id,
        name=name or display_name,
        display_name=display_name,
        web_url=web_url,
        hostname=collection_hostname or web_hostname,
        site_collection_hostname=collection_hostname,
        created_at=_bounded_text(raw.get("createdDateTime"), 128),
        modified_at=_bounded_text(raw.get("lastModifiedDateTime"), 128),
        existence_status=existence_status,
        archive_status=archive_status,
        archive_status_checked=archive_status_checked,
        archive_status_authoritative=archive_status_authoritative,
        requested_target=requested_target,
        data_location_code=data_location_code,
        is_root_site=(True if isinstance(raw.get("root"), dict) else None)
        if governance_available is not False
        else None,
        is_personal_site=(raw.get("isPersonalSite") if isinstance(raw.get("isPersonalSite"), bool) else None)
        if governance_available is not False
        else None,
        governance_observation=(
            "selected"
            if governance_available is True
            else (
                "unavailable_unsupported_select" if governance_available is False else "best_effort_provider_response"
            )
        ),
        governance_limitations=((GOVERNANCE_SELECT_LIMITATION,) if governance_available is False else ()),
    )


def _drive_from_graph(site: Site, raw: dict[str, object], *, governance_available: bool = True) -> Drive:
    drive_id = _bounded_exact_text(raw.get("id"), PROVIDER_ID_MAX_CHARACTERS)
    name = _bounded_exact_text(raw.get("name"), RESOURCE_NAME_MAX_CHARACTERS)
    if not drive_id or not name:
        raise GraphProtocolError(status_code=None, code="library_missing_identity")
    if governance_available:
        owner, owner_observation = _identity_set_metadata(raw.get("owner"))
        created_by, created_by_observation = _identity_set_metadata(raw.get("createdBy"))
        last_modified_by, last_modified_by_observation = _identity_set_metadata(raw.get("lastModifiedBy"))
        quota, quota_observation = _quota_metadata(raw.get("quota"))
        raw_system = raw.get("system")
        system_managed = True if isinstance(raw_system, dict) else None
        system_observation = (
            "observed" if system_managed is True else "not_returned" if raw_system is None else "invalid"
        )
    else:
        owner = created_by = last_modified_by = quota = None
        owner_observation = created_by_observation = last_modified_by_observation = quota_observation = (
            "unavailable_unsupported_select"
        )
        system_managed = None
        system_observation = "unavailable_unsupported_select"
    return Drive(
        site=site,
        drive_id=drive_id,
        name=name,
        web_url=_bounded_url(raw.get("webUrl")),
        drive_type=_bounded_text(raw.get("driveType"), 64),
        description=_bounded_text(raw.get("description"), METADATA_TEXT_MAX_CHARACTERS),
        created_at=_bounded_text(raw.get("createdDateTime"), 128),
        modified_at=_bounded_text(raw.get("lastModifiedDateTime"), 128),
        owner=owner,
        owner_observation=owner_observation,
        created_by=created_by,
        created_by_observation=created_by_observation,
        last_modified_by=last_modified_by,
        last_modified_by_observation=last_modified_by_observation,
        quota=quota,
        quota_observation=quota_observation,
        system_managed=system_managed,
        system_observation=system_observation,
        governance_observation="selected" if governance_available else "unavailable_unsupported_select",
        governance_limitations=((GOVERNANCE_SELECT_LIMITATION,) if not governance_available else ()),
    )


def _graph_id_path(identifier: str) -> str:
    return quote(identifier, safe=",")


def _encoded_site_path(raw_path: str) -> str:
    path = raw_path.rstrip("/") or "/"
    if INVALID_PERCENT_ESCAPE.search(path):
        raise GraphProtocolError(status_code=None, code="invalid_site_url")
    encoded_segments: list[str] = []
    try:
        for raw_segment in path.split("/"):
            decoded = unquote(raw_segment, encoding="utf-8", errors="strict")
            safety_candidate = raw_segment
            for _ in range(MAX_PATH_DECODE_PASSES):
                safety_decoded = unquote(safety_candidate, encoding="utf-8", errors="strict")
                normalized = safety_decoded.replace("\\", "/")
                if any(segment in {".", ".."} for segment in normalized.split("/")) or any(
                    ord(character) < 32 or ord(character) == 127 for character in safety_decoded
                ):
                    raise GraphProtocolError(status_code=None, code="invalid_site_url")
                if safety_decoded == safety_candidate:
                    break
                safety_candidate = safety_decoded
            else:
                raise GraphProtocolError(status_code=None, code="invalid_site_url")
            # Encode each segment independently so an escaped reserved slash
            # remains data (%2F), while literal path separators stay slashes.
            encoded_segments.append(quote(decoded, safe=""))
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise GraphProtocolError(status_code=None, code="invalid_site_url") from exc
    encoded = "/".join(encoded_segments)
    if len(encoded.encode("ascii")) > URL_MAX_BYTES:
        raise GraphProtocolError(status_code=None, code="invalid_site_url")
    return encoded


def _sharepoint_hostname_allowed(client: GraphClient, hostname: str) -> bool:
    validator = getattr(client, "sharepoint_hostname_allowed", None)
    if callable(validator):
        return bool(validator(hostname))
    # Compatibility for narrow test/client adapters that predate cloud
    # profiles. The production GraphClient always supplies the validator.
    return bool(SHAREPOINT_HOST_PATTERN.fullmatch(hostname))


def _optional_select_unsupported(exc: BaseException) -> bool:
    return isinstance(exc, GraphAPIError) and exc.status_code == 400 and exc.code == "unsupported_select"


def _get_with_optional_select_fallback(
    client: GraphClient,
    selected_url: str,
    core_url: str,
) -> tuple[dict[str, object], bool]:
    """Retry once without optional governance fields on an explicit select rejection."""

    try:
        return client.get(selected_url), True
    except GraphAPIError as exc:
        if not _optional_select_unsupported(exc):
            raise
    return client.get(core_url), False


def _iter_pages_with_optional_select_fallback(
    client: GraphClient,
    selected_url: str,
    core_url: str,
) -> Iterator[tuple[dict[str, object], bool]]:
    """Fall back only before any selected page has been accepted."""

    yielded_selected_page = False
    try:
        for page in client.iter_pages(selected_url):
            yielded_selected_page = True
            yield page, True
        return
    except GraphAPIError as exc:
        if yielded_selected_page or not _optional_select_unsupported(exc):
            raise
    for page in client.iter_pages(core_url):
        yield page, False


def resolve_target_site(client: GraphClient, reference: str) -> Site:
    normalized = str(reference or "").strip()
    if not normalized:
        raise GraphProtocolError(status_code=None, code="empty_site_target")
    try:
        if len(normalized.encode("utf-8")) > SITE_TARGET_MAX_BYTES:
            raise GraphProtocolError(status_code=None, code="invalid_site_url")
    except UnicodeEncodeError as exc:
        raise GraphProtocolError(status_code=None, code="invalid_site_url") from exc
    parsed = urlparse(normalized)
    if parsed.scheme:
        try:
            port = parsed.port
            hostname = (parsed.hostname or "").casefold()
        except ValueError as exc:
            raise GraphProtocolError(status_code=None, code="invalid_site_url") from exc
        if (
            parsed.scheme.casefold() != "https"
            or not _sharepoint_hostname_allowed(client, hostname)
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise GraphProtocolError(status_code=None, code="invalid_site_url")
        encoded_path = _encoded_site_path(parsed.path)
        site_path = f"sites/{hostname}:{encoded_path}"
        raw, governance_available = _get_with_optional_select_fallback(
            client,
            f"{site_path}?$select={SITE_SELECT}",
            f"{site_path}?$select={CORE_SITE_SELECT}",
        )
    else:
        if (
            len(normalized) > PROVIDER_ID_MAX_CHARACTERS
            or "\x00" in normalized
            or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        ):
            raise GraphProtocolError(status_code=None, code="invalid_site_id")
        site_path = f"sites/{_graph_id_path(normalized)}"
        raw, governance_available = _get_with_optional_select_fallback(
            client,
            f"{site_path}?$select={SITE_SELECT}",
            f"{site_path}?$select={CORE_SITE_SELECT}",
        )
    return _site_from_graph(
        raw,
        archive_status_selected=True,
        existence_status="confirmed",
        requested_target=normalized,
        governance_available=governance_available,
    )


def _discover_drive_search_sites(
    client: GraphClient,
    *,
    max_sites: int,
    on_site_error=None,
    search_page_size: int = 200,
) -> tuple[list[Site], bool]:
    site_targets: list[str] = []
    seen: set[str] = set()
    offset = 0
    truncated = False
    for _page_number in range(client.max_pages):
        payload = client.post(
            "search/query",
            json_body={
                "requests": [
                    {
                        "entityTypes": ["drive"],
                        "query": {"queryString": "*"},
                        "from": offset,
                        "size": search_page_size,
                        "fields": ["sharePointIds", "driveType", "webUrl", "parentReference"],
                    }
                ]
            },
        )
        responses = payload.get("value")
        if not isinstance(responses, list) or not responses or not isinstance(responses[0], dict):
            raise GraphProtocolError(status_code=None, code="malformed_search_response")
        containers = responses[0].get("hitsContainers")
        if not isinstance(containers, list):
            raise GraphProtocolError(status_code=None, code="malformed_search_response")
        page_hits = 0
        more = False
        for container in containers:
            if not isinstance(container, dict):
                continue
            more = more or bool(container.get("moreResultsAvailable"))
            hits = container.get("hits")
            if not isinstance(hits, list):
                continue
            page_hits += len(hits)
            for hit in hits:
                resource = hit.get("resource") if isinstance(hit, dict) else None
                if not isinstance(resource, dict):
                    continue
                if resource.get("driveType") != "documentLibrary":
                    continue
                parent = resource.get("parentReference") if isinstance(resource, dict) else None
                site_id = _bounded_exact_text(
                    parent.get("siteId") if isinstance(parent, dict) else None,
                    PROVIDER_ID_MAX_CHARACTERS,
                )
                site_target = site_id
                if not site_target:
                    sharepoint_ids = resource.get("sharePointIds")
                    if isinstance(sharepoint_ids, dict):
                        collection_id = _bounded_exact_text(
                            sharepoint_ids.get("siteId"),
                            PROVIDER_ID_MAX_CHARACTERS,
                        )
                        web_id = _bounded_exact_text(
                            sharepoint_ids.get("webId"),
                            PROVIDER_ID_MAX_CHARACTERS,
                        )
                        site_url = _bounded_url(sharepoint_ids.get("siteUrl"))
                        hostname = urlparse(site_url).hostname if site_url else None
                        if collection_id and web_id and hostname:
                            site_target = f"{hostname},{collection_id},{web_id}"
                        elif site_url:
                            site_target = site_url
                if not site_target:
                    if on_site_error is not None:
                        on_site_error(
                            None,
                            GraphProtocolError(
                                status_code=None,
                                code="search_hit_missing_site_identity",
                            ),
                        )
                    continue
                if site_target in seen:
                    continue
                if max_sites and len(site_targets) >= max_sites:
                    truncated = True
                    more = False
                    break
                seen.add(site_target)
                site_targets.append(site_target)
            if truncated:
                break
        if truncated:
            break
        if not more:
            break
        if page_hits == 0:
            raise GraphProtocolError(status_code=None, code="search_pagination_stalled")
        offset += search_page_size
    else:
        raise GraphProtocolError(status_code=None, code="page_limit_reached")

    sites: list[Site] = []
    for site_target in site_targets:
        try:
            if urlparse(site_target).scheme == "https":
                sites.append(replace(resolve_target_site(client, site_target), requested_target=None))
            else:
                site_path = f"sites/{_graph_id_path(site_target)}"
                raw_site, governance_available = _get_with_optional_select_fallback(
                    client,
                    f"{site_path}?$select={SITE_SELECT}",
                    f"{site_path}?$select={CORE_SITE_SELECT}",
                )
                sites.append(
                    _site_from_graph(
                        raw_site,
                        archive_status_selected=True,
                        existence_status="confirmed",
                        governance_available=governance_available,
                    )
                )
        except GraphAPIError as exc:
            if on_site_error is None:
                raise
            on_site_error(None, exc)
    return sites, truncated


def discover_sites(
    client: GraphClient,
    context: GraphTokenContext,
    config: SharePointCollectionConfig,
    *,
    on_site_error=None,
    on_target_error=None,
) -> tuple[list[Site], bool]:
    if config.targeted_sites:
        sites: list[Site] = []
        seen: set[str] = set()
        truncated = False
        for reference in config.targeted_sites:
            try:
                site = resolve_target_site(client, reference)
            except (GraphAPIError, StateStoreError) as exc:
                callback = on_target_error or on_site_error
                if callback is None:
                    raise
                if on_target_error is not None:
                    on_target_error(reference, exc)
                else:
                    on_site_error(None, exc)
                continue
            if site.site_id in seen:
                continue
            if config.max_sites and len(sites) >= config.max_sites:
                truncated = True
                break
            seen.add(site.site_id)
            sites.append(site)
        return sites, truncated

    if context.auth_type == "application":
        # getAllSites documents only the opaque paging token on v1.0. Avoid
        # unsupported OData query options and normalize only the fields used.
        discovery_url = "sites/getAllSites"
    elif config.discovery == "drive-search":
        return _discover_drive_search_sites(
            client,
            max_sites=config.max_sites,
            on_site_error=on_site_error,
        )
    else:
        # The v1.0 Sites Search method documents only its required search
        # expression. Default site fields contain the metadata normalized here.
        discovery_url = "sites?search=*"

    sites = []
    seen: set[str] = set()
    truncated = False
    for page in client.iter_pages(discovery_url):
        values = page.get("value")
        if not isinstance(values, list):
            raise GraphProtocolError(status_code=None, code="missing_page_values")
        for raw in values:
            if not isinstance(raw, dict):
                exc = GraphProtocolError(status_code=None, code="malformed_page_item")
                if on_site_error is None:
                    raise exc
                on_site_error(None, exc)
                continue
            try:
                site = _site_from_graph(raw)
            except GraphAPIError as exc:
                if on_site_error is None:
                    raise
                on_site_error(None, exc)
                continue
            if site.site_id in seen:
                continue
            if config.max_sites and len(sites) >= config.max_sites:
                truncated = True
                break
            seen.add(site.site_id)
            sites.append(site)
        if truncated:
            break
    return sites, truncated


def _site_collection_id(site: Site) -> str:
    identity_parts = site.site_id.split(",")
    return ",".join(identity_parts[:2]) if len(identity_parts) >= 2 else site.site_id


def enrich_site_archive_status(client: GraphClient, site: Site) -> Site:
    """Resolve authoritative site-collection archival details for one site."""

    if site.archive_status_checked:
        return site
    collection_id = _site_collection_id(site)
    raw = client.get(f"sites/{_graph_id_path(collection_id)}?$select=id,siteCollection")
    archive_status, checked, authoritative = _archive_status_from_graph(
        raw.get("siteCollection"),
        explicitly_selected=True,
    )
    if not checked:
        raise GraphProtocolError(status_code=None, code="site_archive_status_unavailable")
    return replace(
        site,
        archive_status=archive_status,
        archive_status_checked=checked,
        archive_status_authoritative=authoritative,
    )


def discover_drives(
    client: GraphClient,
    sites: Iterable[Site],
    *,
    max_libraries: int,
    on_site_error,
    on_drive_error,
) -> tuple[list[Drive], bool]:
    drives: list[Drive] = []
    seen: set[tuple[str, str]] = set()
    truncated = False
    for site in sites:
        try:
            drive_path = f"sites/{_graph_id_path(site.site_id)}/drives"
            pages = _iter_pages_with_optional_select_fallback(
                client,
                f"{drive_path}?$select={DRIVE_SELECT}",
                f"{drive_path}?$select={CORE_DRIVE_SELECT}",
            )
            for page, governance_available in pages:
                values = page.get("value")
                if not isinstance(values, list):
                    raise GraphProtocolError(status_code=None, code="missing_page_values")
                for raw in values:
                    if not isinstance(raw, dict):
                        on_drive_error(
                            site,
                            GraphProtocolError(status_code=None, code="malformed_page_item"),
                        )
                        continue
                    try:
                        drive = _drive_from_graph(site, raw, governance_available=governance_available)
                    except GraphAPIError as exc:
                        on_drive_error(site, exc)
                        continue
                    key = (site.site_id, drive.drive_id)
                    if key in seen:
                        continue
                    if max_libraries and len(drives) >= max_libraries:
                        truncated = True
                        return drives, truncated
                    seen.add(key)
                    drives.append(drive)
        except (GraphAPIError, StateStoreError) as exc:
            on_site_error(site, exc)
    return drives, truncated


def _canonical_parent_path(raw_parent_path: object) -> str:
    if raw_parent_path is None:
        raw = ""
    elif isinstance(raw_parent_path, str):
        raw = raw_parent_path
    else:
        raise GraphProtocolError(status_code=None, code="item_parent_path_invalid")
    if "\x00" in raw:
        raise GraphProtocolError(status_code=None, code="item_parent_path_invalid")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GraphProtocolError(
            status_code=None,
            code="item_parent_path_invalid",
        ) from exc
    marker = "root:"
    if marker in raw:
        raw = raw.split(marker, 1)[1]
    raw = raw.replace("\\", "/")
    if not raw or raw == "/":
        return "/"
    return "/" + "/".join(part for part in raw.split("/") if part)


def _file_archive_status(file_facet: dict[str, object] | None) -> str | None:
    if file_facet is None:
        return None
    if "archiveStatus" not in file_facet:
        # Graph v1.0 documentation has not yet made this facet contractual in
        # every cloud. Preserve uncertainty when the provider omits the field.
        return "unknown"
    raw_status = file_facet.get("archiveStatus")
    if raw_status is None or (isinstance(raw_status, str) and not raw_status.strip()):
        # Microsoft 365 Archive documents a blank value for active files.
        return "not_archived"
    if not isinstance(raw_status, str):
        raise GraphProtocolError(status_code=None, code="item_file_archive_status_invalid")
    if raw_status == "unknownFutureValue":
        return "unknown"
    return GRAPH_FILE_ARCHIVE_STATUS.get(raw_status, "unknown")


def normalize_drive_item(
    raw: dict[str, object],
    *,
    site_id: str,
    drive_id: str,
    exposure: str,
    exposure_evidence: dict[str, object],
    governance_available: bool = True,
) -> dict[str, object] | None:
    provider_item_id = _bounded_exact_text(raw.get("id"), PROVIDER_ID_MAX_CHARACTERS)
    if not provider_item_id:
        raise GraphProtocolError(status_code=None, code="item_missing_id")
    if "root" in raw:
        if not isinstance(raw.get("root"), dict):
            raise GraphProtocolError(status_code=None, code="item_root_facet_invalid")
        return None
    if "deleted" in raw:
        if not isinstance(raw.get("deleted"), dict):
            raise GraphProtocolError(status_code=None, code="item_deleted_facet_invalid")
        return {
            "provider": "sharepoint",
            "provider_resource_id": drive_id,
            "provider_item_id": provider_item_id,
            "deleted": True,
            "metadata": {"site_id": site_id, "drive_id": drive_id},
        }

    name = _bounded_exact_text(raw.get("name"), ITEM_NAME_MAX_CHARACTERS)
    if not name:
        raise GraphProtocolError(status_code=None, code="item_name_out_of_bounds")
    raw_parent = raw.get("parentReference")
    if raw_parent is not None and not isinstance(raw_parent, dict):
        raise GraphProtocolError(status_code=None, code="item_parent_reference_invalid")
    parent = raw_parent if isinstance(raw_parent, dict) else {}
    parent_path = _canonical_parent_path(parent.get("path"))
    full_path = f"/{name}" if parent_path == "/" else f"{parent_path.rstrip('/')}/{name}"
    if len(full_path) > ITEM_PATH_MAX_CHARACTERS or len(full_path.encode("utf-8")) > ITEM_PATH_MAX_BYTES:
        raise GraphProtocolError(status_code=None, code="item_path_out_of_bounds")

    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > 2**63 - 1:
        size = None
    file_facet = raw.get("file")
    folder_facet = raw.get("folder")
    if file_facet is not None and not isinstance(file_facet, dict):
        raise GraphProtocolError(status_code=None, code="item_file_facet_invalid")
    if folder_facet is not None and not isinstance(folder_facet, dict):
        raise GraphProtocolError(status_code=None, code="item_folder_facet_invalid")
    if isinstance(file_facet, dict) and isinstance(folder_facet, dict):
        raise GraphProtocolError(status_code=None, code="item_conflicting_facets")
    folder_child_count = folder_facet.get("childCount") if isinstance(folder_facet, dict) else None
    if (
        not isinstance(folder_child_count, int)
        or isinstance(folder_child_count, bool)
        or folder_child_count < 0
        or folder_child_count > 2**31 - 1
    ):
        folder_child_count = None
    raw_parent_id = parent.get("id")
    parent_id = _bounded_exact_text(raw_parent_id, PROVIDER_ID_MAX_CHARACTERS)
    if raw_parent_id is not None and parent_id is None:
        raise GraphProtocolError(status_code=None, code="item_parent_id_invalid")
    normalized_file_facet = file_facet if isinstance(file_facet, dict) else None
    mime_type = _bounded_text(normalized_file_facet.get("mimeType"), 255) if normalized_file_facet else None
    file_archive_status = _file_archive_status(normalized_file_facet)
    if governance_available:
        created_by, created_by_observation = _identity_set_metadata(raw.get("createdBy"))
        last_modified_by, last_modified_by_observation = _identity_set_metadata(raw.get("lastModifiedBy"))
    else:
        created_by = last_modified_by = None
        created_by_observation = last_modified_by_observation = "unavailable_unsupported_select"
    return {
        "provider": "sharepoint",
        "provider_resource_id": drive_id,
        "provider_item_id": provider_item_id,
        "provider_parent_id": parent_id,
        "path": full_path,
        "name": name,
        "is_dir": isinstance(folder_facet, dict),
        "size": size,
        "created_at": _bounded_text(raw.get("createdDateTime"), 128),
        "modified_at": _bounded_text(raw.get("lastModifiedDateTime"), 128),
        "web_url": _bounded_url(raw.get("webUrl")),
        "mime_type": mime_type,
        "deleted": False,
        "exposure": exposure,
        "exposure_evidence": exposure_evidence,
        "metadata": {
            "site_id": site_id,
            "drive_id": drive_id,
            "etag": _bounded_text(raw.get("eTag"), METADATA_TEXT_MAX_CHARACTERS),
            "ctag": _bounded_text(raw.get("cTag"), METADATA_TEXT_MAX_CHARACTERS),
            "folder_child_count": folder_child_count,
            "file_archive_status": file_archive_status,
            "created_by": created_by,
            "created_by_observation": created_by_observation,
            "last_modified_by": last_modified_by,
            "last_modified_by_observation": last_modified_by_observation,
            **(
                {
                    "governance_observation": "unavailable_unsupported_select",
                    "governance_limitation": GOVERNANCE_SELECT_LIMITATION,
                }
                if not governance_available
                else {}
            ),
        },
    }


def _error_code(exc: BaseException, *, prefix: str = "GRAPH") -> str:
    if isinstance(exc, GraphAPIError):
        if exc.status_code == 403:
            return f"{prefix}_PERMISSION_DENIED"
        if exc.status_code == 404:
            return f"{prefix}_NOT_FOUND"
        if exc.status_code == 429 or exc.code in {"throttled", "retry_after_exceeds_budget"}:
            return f"{prefix}_THROTTLED"
        if exc.retryable:
            return f"{prefix}_TRANSIENT_FAILURE"
        return f"{prefix}_{exc.code.upper()[:64]}"
    if isinstance(exc, StateStoreError):
        return "SHAREPOINT_STATE_FAILURE"
    return f"{prefix}_FAILURE"


def _site_lifecycle_state(site: Site) -> str:
    if site.archive_status in {"recently_archived", "fully_archived"}:
        return "archived"
    if site.archive_status == "reactivating":
        return "reactivating"
    if site.archive_status == "not_archived":
        return "available"
    return "indeterminate"


def _target_endpoint_key(reference: str) -> str:
    normalized = str(reference or "").strip()
    fingerprint = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"sharepoint-target:{fingerprint}"


def _safe_requested_target(reference: str) -> str:
    return _terminal_safe(str(reference or "").strip(), METADATA_TEXT_MAX_CHARACTERS)


def _target_hostname(reference: str) -> str | None:
    normalized = str(reference or "").strip()
    try:
        parsed = urlparse(normalized)
        if parsed.scheme:
            return (parsed.hostname or "").casefold() or None
    except ValueError:
        return None
    candidate = normalized.split(",", 1)[0].casefold()
    return candidate if SHAREPOINT_HOST_PATTERN.fullmatch(candidate) else None


def _target_failure_state(exc: BaseException) -> tuple[str, str, str]:
    if isinstance(exc, GraphAPIError):
        if exc.code in {"empty_site_target", "invalid_site_url", "invalid_site_id"}:
            return "invalid_target", "indeterminate", "invalid_target"
        if exc.status_code == 404:
            # Graph can security-trim a valid target as 404. Do not turn that
            # observation into a definitive deletion or stale-link finding.
            return "not_found_or_not_visible", "indeterminate", "not_found_or_not_visible"
        if exc.status_code == 401:
            return "authentication_failed", "indeterminate", "authentication_failed"
        if exc.status_code == 403:
            return "permission_denied", "indeterminate", "inaccessible"
        if exc.retryable or exc.status_code in {408, 429, 500, 502, 503, 504}:
            return "temporarily_unreachable", "indeterminate", "temporarily_unreachable"
    return "unknown", "indeterminate", "indeterminate"


def _library_failure_status(exc: BaseException) -> str:
    if isinstance(exc, GraphAPIError):
        if exc.status_code == 401:
            return "authentication_failed"
        if exc.status_code == 403:
            return "permission_denied"
        if exc.status_code == 404:
            return "not_found_or_not_visible"
        if exc.retryable or exc.status_code in {408, 429, 500, 502, 503, 504}:
            return "temporarily_unreachable"
    return "failed"


class SharePointCollector:
    def __init__(
        self,
        *,
        client: GraphClient,
        state: SharePointStateStore,
        writer: ArtifactWriter,
        run_id: str,
        context: GraphTokenContext,
        config: SharePointCollectionConfig,
        stats: SharePointStats | None = None,
        progress: SharePointProgress | None = None,
        permission_attempt_budget: GraphAttemptBudget | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.writer = writer
        self.run_id = run_id
        self.context = context
        self.config = config
        self.stats = stats or SharePointStats()
        self.progress = progress or SharePointProgress(
            self.stats,
            quiet=config.quiet,
            verbosity=config.verbosity,
            interval_seconds=config.progress_interval,
        )
        self.scope_key = state_scope_key(context)
        self.collection_mode = "tenant_inventory" if context.auth_type == "application" else "delegated_user_view"
        self._item_budget = _ItemBudget(config.max_items)
        self._pending_lock = threading.Lock()
        self.pending_drives: list[PendingDrive] = []
        self._sync_modes: set[str] = set()
        self._root_permission_results: dict[tuple[str, str], PermissionAssessmentResult] = {}
        self._permission_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._permission_submission_slots: threading.BoundedSemaphore | None = None
        self._permissions = (
            DirectPermissionCollector(
                client=client,
                run_id=run_id,
                tenant_id=context.tenant_id,
                mode=config.permissions,
                max_objects=config.max_permission_objects,
                max_http_attempts=config.max_permission_http_attempts,
                max_entries=config.max_permission_entries,
                concurrency=config.permission_concurrency,
                attempt_budget=permission_attempt_budget,
                on_error=self._permission_error,
            )
            if config.permissions != "none"
            else None
        )

    def emit_error(
        self,
        code: str,
        message: str,
        *,
        endpoint_key: str | None = None,
        resource_name: str | None = None,
        hint: str | None = None,
    ) -> None:
        record: dict[str, object] = {
            "type": "error",
            "run_id": self.run_id,
            "severity": "warn",
            "code": code[:128],
            "message": message[:4096],
        }
        if endpoint_key:
            record["endpoint_key"] = endpoint_key
        if resource_name:
            record["resource_name"] = resource_name
        if hint:
            record["hint"] = hint[:4096]
        self.writer.emit(record)
        self.stats.record_error(code)

    def _permission_error(
        self,
        code: str,
        exc: BaseException,
        subject: PermissionSubject,
    ) -> None:
        self.emit_error(
            code,
            str(exc),
            endpoint_key=subject.endpoint_key,
            resource_name=subject.resource_name,
            hint=(
                "Direct permission evidence is incomplete for at least one object; content inventory continues "
                "and no missing permission is interpreted as revoked or restricted access."
            ),
        )

    def _endpoint_key(self, site: Site) -> str:
        return f"sharepoint:{site.site_id}"

    def _exposure(self) -> tuple[str, dict[str, object]]:
        if self.context.auth_type == "delegated":
            return (
                "USER_VISIBLE",
                {
                    "basis": "graph_delegated_read_context",
                    "assessed_identity": self.context.assessed_identity,
                    "classification_scope": "visibility_not_public_exposure",
                },
            )
        return (
            "UNKNOWN",
            {
                "basis": "exposure_not_assessed",
                "classification_scope": "inventory_only",
            },
        )

    def _pending_permission_summary(self) -> dict[str, object] | None:
        if self._permissions is None:
            return None
        return {
            "assessment_key": None,
            "assessment_state": "pending",
            "selection_scope": self.config.permissions,
            "selection_coverage": "pending",
            "retrieval_coverage": "pending",
            "provider_visibility": "caller_dependent_unverified",
            "semantic_coverage": "pending",
            "principal_resolution": "pending",
            "effective_access_status": "not_computed",
            "negative_conclusion_supported": False,
            "entries_observed": 0,
            "entries_emitted": 0,
            "entries_omitted": 0,
            "unknown_entries": 0,
            "entry_set_hash": None,
            "exposure": "UNKNOWN",
            "positive_evidence": [],
            "limitations": ["permission_assessment_pending"],
        }

    def _emit_permission_records(self, result: PermissionAssessmentResult | None) -> None:
        if result is None or result.assessment_record is None:
            return
        self.writer.emit(result.assessment_record)
        for entry in result.entry_records:
            self.writer.emit(entry)

    def _permission_subject(self, drive: Drive, item: dict[str, object] | None = None) -> PermissionSubject:
        item_id = None
        subject_kind = "resource"
        subject_path = None
        if item is not None:
            raw_item_id = item.get("provider_item_id")
            item_id = raw_item_id if isinstance(raw_item_id, str) else None
            subject_kind = "item"
            raw_path = item.get("path")
            subject_path = raw_path if isinstance(raw_path, str) else None
        return PermissionSubject(
            endpoint_key=self._endpoint_key(drive.site),
            resource_name=drive.name,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
            item_id=item_id,
            subject_kind=subject_kind,
            subject_path=subject_path,
        )

    def _assess_root_permissions(self, drive: Drive) -> PermissionAssessmentResult | None:
        if self._permissions is None:
            return None
        exposure, evidence = self._exposure()
        return self._permissions.assess_root(
            self._permission_subject(drive),
            base_exposure=exposure,
            base_evidence=evidence,
        )

    def _assess_item_permissions(
        self,
        drive: Drive,
        item: dict[str, object],
    ) -> PermissionAssessmentResult | None:
        if self._permissions is None or self.config.permissions != "all_items":
            return None
        exposure, evidence = self._exposure()
        return self._permissions.assess_item(
            self._permission_subject(drive, item),
            base_exposure=exposure,
            base_evidence=evidence,
        )

    def _submit_permission(self, callback, *args) -> concurrent.futures.Future:
        executor = self._permission_executor
        slots = self._permission_submission_slots
        if executor is None or slots is None:
            raise RuntimeError("permission executor is not active")
        slots.acquire()
        try:
            future = executor.submit(callback, *args)
        except BaseException:
            slots.release()
            raise
        future.add_done_callback(lambda _future: slots.release())
        return future

    def _iter_items_with_permissions(
        self,
        drive: Drive,
        items: Iterable[dict[str, object]],
    ) -> Iterable[tuple[dict[str, object], PermissionAssessmentResult | None]]:
        if self._permissions is None or self.config.permissions != "all_items":
            for item in items:
                yield item, None
            return

        pending: deque[tuple[dict[str, object], concurrent.futures.Future]] = deque()
        iterator = iter(items)
        window = max(1, int(self.config.permission_concurrency))

        def fill() -> None:
            while len(pending) < window:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                pending.append((item, self._submit_permission(self._assess_item_permissions, drive, item)))

        fill()
        while pending:
            item, future = pending.popleft()
            yield item, future.result()
            fill()

    def _iter_roots_with_permissions(
        self,
        drives: Iterable[Drive],
    ) -> Iterable[tuple[Drive, PermissionAssessmentResult | None]]:
        if self._permissions is None:
            for drive in drives:
                yield drive, None
            return
        pending: deque[tuple[Drive, concurrent.futures.Future]] = deque()
        iterator = iter(drives)
        window = max(1, int(self.config.permission_concurrency))

        def fill() -> None:
            while len(pending) < window:
                try:
                    drive = next(iterator)
                except StopIteration:
                    return
                pending.append((drive, self._submit_permission(self._assess_root_permissions, drive)))

        fill()
        while pending:
            drive, future = pending.popleft()
            yield drive, future.result()
            fill()

    def _emit_endpoint(self, site: Site) -> None:
        lifecycle_state = _site_lifecycle_state(site)
        evidence: dict[str, object] = {
            "basis": "graph_target_resolution" if site.requested_target else "graph_site_metadata",
            "archive_status_checked": site.archive_status_checked,
            "archive_status_authoritative": site.archive_status_authoritative,
        }
        if site.archive_status_checked:
            identity_parts = site.site_id.split(",")
            evidence["archive_status_scope"] = "site_collection"
            evidence["archive_status_site_collection_id"] = (
                ",".join(identity_parts[:2]) if len(identity_parts) >= 2 else site.site_id
            )
        if site.archive_status_checked:
            evidence["archive_status_source"] = (
                "siteCollection.archivalDetails.archiveStatus"
                if site.archive_status != "not_archived"
                else "inferred from siteCollection.archivalDetails absence after explicit selection"
            )
        self.writer.emit(
            {
                "type": "endpoint",
                "run_id": self.run_id,
                "endpoint_key": self._endpoint_key(site),
                "hostname": site.hostname,
                "provider": "sharepoint",
                "metadata": {
                    "tenant_id": self.context.tenant_id,
                    "site_id": site.site_id,
                    "site_name": site.name,
                    "display_name": site.display_name,
                    "web_url": site.web_url,
                    "site_collection_hostname": site.site_collection_hostname,
                    "data_location_code": site.data_location_code,
                    "is_root_site": site.is_root_site,
                    "is_personal_site": site.is_personal_site,
                    "governance_observation": site.governance_observation,
                    "governance_limitations": list(site.governance_limitations),
                    "created_at": site.created_at,
                    "modified_at": site.modified_at,
                    "collection_mode": self.collection_mode,
                    "auth_type": self.context.auth_type,
                    "graph_cloud": self.context.cloud,
                    "assessed_identity": self.context.assessed_identity,
                    "existence_status": site.existence_status,
                    "archive_status": site.archive_status,
                    "lifecycle_state": lifecycle_state,
                    "assessment": "resolved" if site.existence_status == "confirmed" else "discovered",
                    "evidence": evidence,
                    "requested_target": site.requested_target,
                },
            }
        )
        self.stats.increment("endpoints_emitted")
        if lifecycle_state == "archived":
            self.stats.increment("sites_archived")

    def _emit_target_endpoint(self, reference: str, exc: BaseException) -> str:
        existence_status, lifecycle_state, assessment = _target_failure_state(exc)
        endpoint_key = _target_endpoint_key(reference)
        evidence: dict[str, object] = {
            "basis": "graph_target_resolution",
            "archive_status_checked": False,
            "archive_status_authoritative": False,
        }
        if isinstance(exc, GraphAPIError):
            evidence["graph_status_code"] = exc.status_code
            evidence["graph_error_code"] = exc.code
        self.writer.emit(
            {
                "type": "endpoint",
                "run_id": self.run_id,
                "endpoint_key": endpoint_key,
                "hostname": _target_hostname(reference),
                "provider": "sharepoint",
                "metadata": {
                    "tenant_id": self.context.tenant_id,
                    "graph_cloud": self.context.cloud,
                    "collection_mode": self.collection_mode,
                    "auth_type": self.context.auth_type,
                    "assessed_identity": self.context.assessed_identity,
                    "requested_target": _safe_requested_target(reference),
                    "existence_status": existence_status,
                    "archive_status": "unknown",
                    "lifecycle_state": lifecycle_state,
                    "assessment": assessment,
                    "evidence": evidence,
                },
            }
        )
        self.stats.increment("endpoints_emitted")
        if assessment == "not_found_or_not_visible":
            self.stats.increment("sites_not_found_or_not_visible")
        elif assessment == "inaccessible":
            self.stats.increment("sites_inaccessible")
        else:
            self.stats.increment("sites_indeterminate")
        return endpoint_key

    def _emit_resource(self, drive: Drive, observation: LibraryObservation) -> None:
        exposure, evidence = self._exposure()
        permission_summary = None
        if self._permissions is not None:
            if observation.permission_result is not None:
                exposure = observation.permission_result.exposure
                evidence = observation.permission_result.exposure_evidence
                permission_summary = observation.permission_result.permission_summary
            else:
                # The initial resource row exists only so following permission
                # records have a stable parent. It must not contain a stronger
                # exposure that a later failed assessment cannot replace.
                exposure = "UNKNOWN"
                permission_summary = self._pending_permission_summary()
                evidence = {
                    "basis": "permission_assessment_pending",
                    "classification_scope": "positive_exposure_evidence_only",
                    "permission_summary": permission_summary,
                }
        access_level = "list_only" if observation.enumeration_status == "complete" else "unknown"
        metadata: dict[str, object] = {
            "tenant_id": self.context.tenant_id,
            "graph_cloud": self.context.cloud,
            "site_id": drive.site.site_id,
            "drive_id": drive.drive_id,
            "drive_type": drive.drive_type,
            "description": drive.description,
            "owner": drive.owner,
            "owner_observation": drive.owner_observation,
            "created_by": drive.created_by,
            "created_by_observation": drive.created_by_observation,
            "last_modified_by": drive.last_modified_by,
            "last_modified_by_observation": drive.last_modified_by_observation,
            "quota": drive.quota,
            "quota_observation": drive.quota_observation,
            "system_managed": drive.system_managed,
            "system_observation": drive.system_observation,
            "governance_observation": drive.governance_observation,
            "governance_limitations": list(drive.governance_limitations),
            "created_at": drive.created_at,
            "modified_at": drive.modified_at,
            "content_read_tested": False,
            "access_observation": (
                "graph_item_metadata_enumeration"
                if observation.enumeration_status == "complete"
                else "graph_library_metadata_only"
            ),
            "enumeration_status": observation.enumeration_status,
            "content_state": observation.content_state,
            "file_count": observation.file_count,
            "folder_count": observation.folder_count,
            "item_count": observation.item_count,
            "total_size_bytes": observation.total_size_bytes,
            "collection_complete": observation.collection_complete,
            "sync_mode": observation.sync_mode,
            "size_observation_complete": observation.size_observation_complete,
            "archived_file_count": observation.archived_file_count,
            "reactivating_file_count": observation.reactivating_file_count,
            "active_file_count": observation.active_file_count,
            "unknown_file_archive_count": observation.unknown_file_archive_count,
        }
        if observation.item_governance_observation is not None:
            metadata["item_governance_observation"] = observation.item_governance_observation
            if observation.item_governance_observation == "unavailable_unsupported_select":
                metadata["item_governance_limitations"] = [GOVERNANCE_SELECT_LIMITATION]
        if permission_summary is not None:
            metadata["permission_summary"] = permission_summary
        if observation.descendant_permission_summary is not None:
            metadata["descendant_permission_summary"] = observation.descendant_permission_summary
        if observation.enumeration_error_code:
            metadata["enumeration_error_code"] = observation.enumeration_error_code
        self.writer.emit(
            {
                "type": "resource",
                "run_id": self.run_id,
                "endpoint_key": self._endpoint_key(drive.site),
                "name": drive.name,
                "share_type": "sharepoint",
                "resource_type": "sharepoint_library",
                "provider": "sharepoint",
                "provider_resource_id": drive.drive_id,
                "web_url": drive.web_url,
                "access_level": access_level,
                "exposure": exposure,
                "exposure_evidence": evidence,
                **({"permission_summary": permission_summary} if permission_summary is not None else {}),
                "metadata": metadata,
            }
        )

    def _enrich_site_statuses(self, sites: list[Site]) -> list[Site]:
        pending_by_collection: dict[str, list[tuple[int, Site]]] = {}
        for index, site in enumerate(sites):
            if not site.archive_status_checked:
                pending_by_collection.setdefault(_site_collection_id(site), []).append((index, site))
        pending_groups = list(pending_by_collection.values())
        if not pending_groups:
            return sites

        enriched = list(sites)
        pending_site_count = sum(len(group) for group in pending_groups)
        self.progress.set_site_status_total(pending_site_count)

        def handle_result(group: list[tuple[int, Site]]) -> None:
            representative = group[0][1]
            try:
                resolved = enrich_site_archive_status(self.client, representative)
            except GraphAPIError as exc:
                self.stats.increment("sites_indeterminate", len(group))
                self.emit_error(
                    _error_code(exc, prefix="SITE_STATUS"),
                    str(exc),
                    endpoint_key=self._endpoint_key(representative),
                    hint=(
                        "The site collection was discovered, but its archive lifecycle could not be confirmed for "
                        f"{len(group)} site(s); library enumeration continues."
                    ),
                )
                for _, site in group:
                    self.progress.site_status_finished(site, succeeded=False)
            else:
                for index, site in group:
                    enriched[index] = replace(
                        site,
                        archive_status=resolved.archive_status,
                        archive_status_checked=resolved.archive_status_checked,
                        archive_status_authoritative=resolved.archive_status_authoritative,
                    )
                    self.progress.site_status_finished(
                        site,
                        succeeded=resolved.archive_status != "unknown",
                    )

        max_workers = max(1, min(int(self.config.concurrency), 16, len(pending_groups)))
        if max_workers == 1:
            for group in pending_groups:
                handle_result(group)
            return enriched

        # Keep only one request per worker in flight. Large tenants can expose
        # hundreds of thousands of sites, so submitting one Future per site
        # would turn a bounded network phase into unbounded coordinator memory.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="sharepoint-site-status",
        ) as executor:
            pending_iterator = iter(pending_groups)
            futures: dict[concurrent.futures.Future[Site], list[tuple[int, Site]]] = {}

            def submit_next() -> bool:
                try:
                    group = next(pending_iterator)
                except StopIteration:
                    return False
                futures[executor.submit(enrich_site_archive_status, self.client, group[0][1])] = group
                return True

            for _ in range(max_workers):
                if not submit_next():
                    break

            while futures:
                completed, _ = concurrent.futures.wait(
                    tuple(futures),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in completed:
                    group = futures.pop(future)
                    representative = group[0][1]
                    try:
                        resolved = future.result()
                    except GraphAPIError as exc:
                        self.stats.increment("sites_indeterminate", len(group))
                        self.emit_error(
                            _error_code(exc, prefix="SITE_STATUS"),
                            str(exc),
                            endpoint_key=self._endpoint_key(representative),
                            hint=(
                                "The site collection was discovered, but its archive lifecycle could not be confirmed "
                                f"for {len(group)} site(s); library enumeration continues."
                            ),
                        )
                        for _, site in group:
                            self.progress.site_status_finished(site, succeeded=False)
                    else:
                        for index, site in group:
                            enriched[index] = replace(
                                site,
                                archive_status=resolved.archive_status,
                                archive_status_checked=resolved.archive_status_checked,
                                archive_status_authoritative=resolved.archive_status_authoritative,
                            )
                            self.progress.site_status_finished(
                                site,
                                succeeded=resolved.archive_status != "unknown",
                            )
                    submit_next()
        return enriched

    def _record_unknown_site_statuses(self, sites: list[Site]) -> None:
        unknown_by_collection: dict[str, list[Site]] = {}
        for site in sites:
            if site.archive_status_checked and site.archive_status == "unknown":
                unknown_by_collection.setdefault(_site_collection_id(site), []).append(site)
        for group in unknown_by_collection.values():
            representative = group[0]
            self.stats.increment("sites_indeterminate", len(group))
            self.emit_error(
                "SITE_STATUS_INDETERMINATE",
                "Microsoft Graph returned an unrecognized or future archive lifecycle value for "
                f"{len(group)} site(s) in this site collection.",
                endpoint_key=self._endpoint_key(representative),
                hint=(
                    "The sites remain in inventory and library enumeration continues, but this run is partial until "
                    "the collector understands the provider lifecycle value."
                ),
            )

    def collect(self) -> tuple[list[PendingDrive], str]:
        if self._permissions is None:
            return self._collect_impl()
        worker_count = max(1, min(int(self.config.permission_concurrency), 8))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="sharepoint-permissions",
        ) as executor:
            self._permission_executor = executor
            self._permission_submission_slots = threading.BoundedSemaphore(worker_count * 2)
            try:
                return self._collect_impl()
            finally:
                self._permission_executor = None
                self._permission_submission_slots = None

    def _collect_impl(self) -> tuple[list[PendingDrive], str]:
        self.state.initialize()
        self.state.cleanup_stale_sessions()
        self.progress.start(self.context, self.collection_mode)

        def _site_discovery_error(site: Site | None, exc: BaseException) -> None:
            if self._permissions is not None:
                self._permissions.mark_selection_incomplete("site_discovery_failed")
            self.stats.increment("sites_failed")
            self.emit_error(
                _error_code(exc, prefix="SITE_DISCOVERY"),
                str(exc),
                endpoint_key=self._endpoint_key(site) if site else None,
                hint="Other site targets continue; verify the target and Graph read permissions.",
            )

        def _target_discovery_error(reference: str, exc: BaseException) -> None:
            if self._permissions is not None:
                self._permissions.mark_selection_incomplete("target_resolution_failed")
            self.stats.increment("sites_failed")
            endpoint_key = self._emit_target_endpoint(reference, exc)
            hint = "Other site targets continue; verify the target syntax, credentials, and Graph read permissions."
            if isinstance(exc, GraphAPIError) and exc.status_code == 404:
                hint = (
                    "Graph returned 404, which can mean the target does not exist or is hidden from this identity; "
                    "confirm with an independently authorized account before treating the link as stale."
                )
            self.emit_error(
                _error_code(exc, prefix="SITE_DISCOVERY"),
                str(exc),
                endpoint_key=endpoint_key,
                hint=hint,
            )

        try:
            sites, sites_truncated = discover_sites(
                self.client,
                self.context,
                self.config,
                on_site_error=_site_discovery_error,
                on_target_error=_target_discovery_error,
            )
        except GraphAPIError as exc:
            if self._permissions is not None:
                self._permissions.mark_selection_incomplete("site_discovery_failed")
            self.emit_error(
                _error_code(exc, prefix="SITE_DISCOVERY"),
                str(exc),
                hint="Verify Graph read permissions and the selected discovery strategy.",
            )
            self.stats.increment("sites_failed")
            return [], "failed"

        if sites_truncated:
            if self._permissions is not None:
                self._permissions.mark_selection_incomplete("site_limit_reached")
            self.stats.mark_truncated()
            self.emit_error(
                "SITE_LIMIT_REACHED",
                "SharePoint site discovery reached the configured safety limit.",
                hint="Increase --max-sites after reviewing tenant scale.",
            )
        self.stats.sites_discovered = len(sites)
        sites = self._enrich_site_statuses(sites)
        self._record_unknown_site_statuses(sites)
        for site in sites:
            self._emit_endpoint(site)

        def _site_error(site: Site, exc: BaseException) -> None:
            if self._permissions is not None:
                self._permissions.mark_selection_incomplete("library_discovery_failed")
            self.stats.increment("sites_failed")
            self.emit_error(
                _error_code(exc, prefix="LIBRARY_DISCOVERY"),
                str(exc),
                endpoint_key=self._endpoint_key(site),
                hint="The site remains in the artifact; verify the token can enumerate its drives.",
            )

        def _drive_error(site: Site, exc: BaseException) -> None:
            if self._permissions is not None:
                self._permissions.mark_selection_incomplete("library_record_invalid")
            self.stats.increment("libraries_failed")
            self.emit_error(
                _error_code(exc, prefix="LIBRARY_DISCOVERY"),
                str(exc),
                endpoint_key=self._endpoint_key(site),
                hint="A malformed library record was skipped; other libraries continue.",
            )

        drives, libraries_truncated = discover_drives(
            self.client,
            sites,
            max_libraries=self.config.max_libraries,
            on_site_error=_site_error,
            on_drive_error=_drive_error,
        )
        if libraries_truncated:
            if self._permissions is not None:
                self._permissions.mark_selection_incomplete("library_limit_reached")
            self.stats.mark_truncated()
            self.emit_error(
                "LIBRARY_LIMIT_REACHED",
                "SharePoint library discovery reached the configured safety limit.",
                hint="Increase --max-libraries after reviewing tenant scale.",
            )
        self.stats.libraries_discovered = len(drives)
        self.progress.set_library_total(len(drives))

        if not self.config.include_files:
            if self._permissions is not None:
                for drive in drives:
                    self._emit_resource(
                        drive,
                        LibraryObservation(
                            enumeration_status="not_requested",
                            content_state="not_assessed",
                            file_count=None,
                            folder_count=None,
                            item_count=None,
                            total_size_bytes=None,
                            collection_complete=False,
                            sync_mode="metadata_only",
                        ),
                    )
            for drive, permission_result in self._iter_roots_with_permissions(drives):
                self._emit_permission_records(permission_result)
                self._emit_resource(
                    drive,
                    LibraryObservation(
                        enumeration_status="not_requested",
                        content_state="not_assessed",
                        file_count=None,
                        folder_count=None,
                        item_count=None,
                        total_size_bytes=None,
                        collection_complete=False,
                        sync_mode="metadata_only",
                        permission_result=permission_result,
                        descendant_permission_summary=(
                            {
                                "assessment_state": "not_requested",
                                "selection_scope": "items",
                                "selection_coverage": "not_requested",
                                "exposure": "UNKNOWN",
                                "anonymous_items": 0,
                                "broad_internal_items": 0,
                            }
                            if self._permissions is not None
                            else None
                        ),
                    ),
                )
                self.stats.increment("libraries_succeeded")
                self.progress.library_finished(drive, succeeded=True)
            return [], self._status()

        for drive in drives:
            self._emit_resource(
                drive,
                LibraryObservation(
                    enumeration_status="in_progress",
                    content_state="unknown",
                    file_count=None,
                    folder_count=None,
                    item_count=None,
                    total_size_bytes=None,
                    collection_complete=False,
                    sync_mode="pending",
                ),
            )

        if self._permissions is not None:
            for drive, permission_result in self._iter_roots_with_permissions(drives):
                if permission_result is None:
                    continue
                self._root_permission_results[(drive.site.site_id, drive.drive_id)] = permission_result
                self._emit_permission_records(permission_result)

        max_workers = max(1, min(int(self.config.concurrency), 16, len(drives) or 1))
        if max_workers == 1:
            for drive in drives:
                self._process_drive_safely(drive)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="sharepoint-graph",
            ) as executor:
                drive_iterator = iter(drives)
                futures: set[concurrent.futures.Future[None]] = set()

                def submit_next() -> bool:
                    try:
                        drive = next(drive_iterator)
                    except StopIteration:
                        return False
                    futures.add(executor.submit(self._process_drive_safely, drive))
                    return True

                for _ in range(max_workers):
                    if not submit_next():
                        break

                while futures:
                    completed, _ = concurrent.futures.wait(
                        tuple(futures),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in completed:
                        futures.remove(future)
                        # Writer/state failures not handled as per-drive Graph
                        # errors intentionally abort finalization so no
                        # checkpoint advances.
                        future.result()
                        submit_next()
        return list(self.pending_drives), self._status()

    def _status(self) -> str:
        snapshot = self.stats.snapshot()
        permission_summary = self.permission_run_summary
        permission_incomplete = permission_summary.get("request_coverage") not in {"not_requested", "complete"}
        incomplete = bool(
            snapshot["sites_failed"]
            or snapshot["sites_indeterminate"]
            or snapshot["libraries_failed"]
            or snapshot["truncated"]
            or permission_incomplete
        )
        if incomplete:
            if snapshot["sites_discovered"] or snapshot["libraries_succeeded"]:
                return "partial"
            return "failed"
        return "success"

    def _process_drive_safely(self, drive: Drive) -> None:
        permission_result = self._root_permission_results.get((drive.site.site_id, drive.drive_id))
        budget_key = f"{drive.site.site_id}\x00{drive.drive_id}"
        try:
            pending = self._process_drive(drive, budget_key=budget_key)
            self._emit_resource(
                drive,
                LibraryObservation(
                    enumeration_status="complete",
                    content_state="empty" if pending.item_count == 0 else "populated",
                    file_count=pending.file_count,
                    folder_count=pending.folder_count,
                    item_count=pending.item_count,
                    total_size_bytes=pending.total_size_bytes,
                    collection_complete=True,
                    sync_mode=pending.sync_mode,
                    size_observation_complete=pending.size_observation_complete,
                    archived_file_count=pending.archived_file_count,
                    reactivating_file_count=pending.reactivating_file_count,
                    active_file_count=pending.active_file_count,
                    unknown_file_archive_count=pending.unknown_file_archive_count,
                    item_governance_observation=pending.item_governance_observation,
                    permission_result=permission_result,
                    descendant_permission_summary=pending.descendant_permission_summary,
                ),
            )
            with self._pending_lock:
                self.pending_drives.append(pending)
            self.stats.increment("libraries_succeeded")
            self.stats.increment("delta_drives" if pending.sync_mode == "delta" else "full_drives")
            self.progress.library_finished(drive, succeeded=True)
        except (GraphAPIError, StateStoreError) as exc:
            self._item_budget.release(budget_key)
            if self._permissions is not None and self.config.permissions == "all_items":
                self._permissions.mark_selection_incomplete("content_enumeration_failed")
            try:
                self.state.discard_drive(
                    session_id=self.run_id,
                    scope_key=self.scope_key,
                    tenant_id=self.context.tenant_id,
                    site_id=drive.site.site_id,
                    drive_id=drive.drive_id,
                )
            except StateStoreError:
                pass
            self.stats.increment("libraries_failed")
            error_code = _error_code(exc, prefix="LIBRARY")
            self._emit_resource(
                drive,
                LibraryObservation(
                    enumeration_status=_library_failure_status(exc),
                    content_state="unknown",
                    file_count=None,
                    folder_count=None,
                    item_count=None,
                    total_size_bytes=None,
                    collection_complete=False,
                    sync_mode="none",
                    enumeration_error_code=error_code,
                    permission_result=permission_result,
                    descendant_permission_summary=(
                        {
                            "assessment_state": "not_assessed",
                            "selection_scope": "items",
                            "selection_coverage": "content_enumeration_failed",
                            "exposure": "UNKNOWN",
                        }
                        if self.config.permissions == "all_items"
                        else None
                    ),
                ),
            )
            self.emit_error(
                error_code,
                str(exc),
                endpoint_key=self._endpoint_key(drive.site),
                resource_name=drive.name,
                hint="Other libraries continue; retry this library after correcting the reported condition.",
            )
            self.progress.library_finished(drive, succeeded=False)

    def _process_drive(self, drive: Drive, *, budget_key: str) -> PendingDrive:
        state = self.state.get_drive_state(
            self.scope_key,
            self.context.tenant_id,
            drive.site.site_id,
            drive.drive_id,
        )
        force_full = self.config.full_sync or self.config.reset_delta
        sync_mode = "delta" if state.delta_link and state.status == "ok" and not force_full else "full"
        if sync_mode == "delta":
            current_count = self.state.count_current_items(
                self.scope_key,
                self.context.tenant_id,
                drive.site.site_id,
                drive.drive_id,
            )
            if not self._item_budget.resize(budget_key, current_count):
                self.stats.mark_truncated()
                raise StateStoreError("materialized library snapshot exceeds the remaining --max-items safety limit")
        elif not self._item_budget.resize(budget_key, 0):
            self.stats.mark_truncated()
            raise StateStoreError("materialized library snapshot exceeds the remaining --max-items safety limit")

        reset_happened = False
        try:
            changed, deleted, invalid_items, item_governance_observation = self._stage_drive(
                drive,
                state,
                sync_mode=sync_mode,
                budget_key=budget_key,
            )
        except GraphAPIError as exc:
            if sync_mode != "delta" or exc.status_code != 410:
                raise
            reset_happened = True
            self.stats.increment("delta_resets")
            self.emit_error(
                "DELTA_RESET",
                "Microsoft Graph invalidated this library's delta checkpoint; a full resync was performed.",
                endpoint_key=self._endpoint_key(drive.site),
                resource_name=drive.name,
            )
            self.state.discard_drive(
                session_id=self.run_id,
                scope_key=self.scope_key,
                tenant_id=self.context.tenant_id,
                site_id=drive.site.site_id,
                drive_id=drive.drive_id,
            )
            # Preserve the last working state until this replacement stage is
            # emitted and finalized. Graph's Location is opaque and validated
            # by GraphClient before any Authorization header is sent.
            changed, deleted, invalid_items, item_governance_observation = self._stage_drive(
                drive,
                state,
                sync_mode="full",
                initial_url=exc.reset_url,
                budget_key=budget_key,
            )
            sync_mode = "full"

        with self._pending_lock:
            self._sync_modes.add(sync_mode)

        if invalid_items:
            # Reject a known-incomplete snapshot before it can claim capacity
            # from the run-wide item budget. No item records have been emitted
            # for this library at this point, so later healthy libraries may
            # still use the reviewed limit.
            raise StateStoreError(
                "one or more item metadata records exceeded supported bounds; "
                "the delta checkpoint was intentionally withheld"
            )

        count = self.state.count_materialized_items(
            session_id=self.run_id,
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
        )
        if not self._item_budget.resize(budget_key, count):
            self.stats.mark_truncated()
            raise StateStoreError("materialized library snapshot exceeds the configured --max-items safety limit")

        exposure, evidence = self._exposure()
        file_count = 0
        folder_count = 0
        total_size_bytes = 0
        size_observation_complete = True
        archived_file_count = 0
        reactivating_file_count = 0
        active_file_count = 0
        unknown_file_archive_count = 0
        descendant_permission_summary: dict[str, object] | None = None
        if self.config.permissions == "all_items":
            descendant_permission_summary = {
                "assessment_state": "complete",
                "selection_scope": "all_items",
                "selection_coverage": "exhaustive_for_declared_scope",
                "items_candidate": count,
                "items_assessed": 0,
                "items_complete": 0,
                "items_failed": 0,
                "items_skipped": 0,
                "anonymous_items": 0,
                "broad_internal_items": 0,
                "unknown_or_user_visible_items": 0,
                "highest_supported_exposure": "UNKNOWN",
                "root_exposure_included": False,
            }
        elif self.config.permissions == "library_roots":
            descendant_permission_summary = {
                "assessment_state": "not_requested",
                "selection_scope": "items",
                "selection_coverage": "not_requested",
                "highest_supported_exposure": "UNKNOWN",
                "root_exposure_included": False,
            }
        materialized_items = self.state.iter_materialized_items(
            session_id=self.run_id,
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
        )
        for item, permission_result in self._iter_items_with_permissions(drive, materialized_items):
            record = {
                "type": "item",
                "run_id": self.run_id,
                "endpoint_key": self._endpoint_key(drive.site),
                "resource_name": drive.name,
                "share_type": "sharepoint",
                "resource_type": "sharepoint_library",
                **item,
            }
            # Older state rows can predate exposure fields; collection context
            # remains authoritative for this run.
            if permission_result is not None:
                record["exposure"] = permission_result.exposure
                record["exposure_evidence"] = permission_result.exposure_evidence
                raw_metadata = record.get("metadata")
                item_metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
                item_metadata["permission_summary"] = permission_result.permission_summary
                record["metadata"] = item_metadata
                record["permission_summary"] = permission_result.permission_summary
                assert descendant_permission_summary is not None
                descendant_permission_summary["items_assessed"] = int(
                    descendant_permission_summary["items_assessed"]
                ) + (1 if permission_result.assessment_record is not None else 0)
                if permission_result.complete:
                    descendant_permission_summary["items_complete"] = (
                        int(descendant_permission_summary["items_complete"]) + 1
                    )
                elif permission_result.assessment_record is None:
                    descendant_permission_summary["items_skipped"] = (
                        int(descendant_permission_summary["items_skipped"]) + 1
                    )
                else:
                    descendant_permission_summary["items_failed"] = (
                        int(descendant_permission_summary["items_failed"]) + 1
                    )
                if permission_result.exposure == "ANONYMOUS":
                    descendant_permission_summary["anonymous_items"] = (
                        int(descendant_permission_summary["anonymous_items"]) + 1
                    )
                    descendant_permission_summary["highest_supported_exposure"] = "ANONYMOUS"
                elif permission_result.exposure == "BROAD_INTERNAL":
                    descendant_permission_summary["broad_internal_items"] = (
                        int(descendant_permission_summary["broad_internal_items"]) + 1
                    )
                    if descendant_permission_summary["highest_supported_exposure"] != "ANONYMOUS":
                        descendant_permission_summary["highest_supported_exposure"] = "BROAD_INTERNAL"
                else:
                    descendant_permission_summary["unknown_or_user_visible_items"] = (
                        int(descendant_permission_summary["unknown_or_user_visible_items"]) + 1
                    )
            else:
                record["exposure"] = exposure
                record["exposure_evidence"] = evidence
            self.writer.emit(record)
            self._emit_permission_records(permission_result)
            self.stats.increment("items_emitted")
            if bool(item.get("is_dir")):
                folder_count += 1
                self.stats.increment("folders")
            else:
                file_count += 1
                self.stats.increment("files")
                item_metadata = item.get("metadata")
                file_archive_status = (
                    item_metadata.get("file_archive_status") if isinstance(item_metadata, dict) else None
                )
                if file_archive_status == "fully_archived":
                    archived_file_count += 1
                    self.stats.increment("archived_files")
                elif file_archive_status == "reactivating":
                    reactivating_file_count += 1
                    self.stats.increment("reactivating_files")
                elif file_archive_status == "not_archived":
                    active_file_count += 1
                else:
                    unknown_file_archive_count += 1
                item_size = item.get("size")
                if isinstance(item_size, int) and not isinstance(item_size, bool) and item_size >= 0:
                    total_size_bytes += item_size
                else:
                    size_observation_complete = False

        if descendant_permission_summary is not None and self.config.permissions == "all_items":
            if (
                int(descendant_permission_summary["items_complete"]) != count
                or int(descendant_permission_summary["items_failed"])
                or int(descendant_permission_summary["items_skipped"])
            ):
                descendant_permission_summary["assessment_state"] = "partial"
                descendant_permission_summary["selection_coverage"] = "partial"

        self.stats.increment("items_changed", changed)
        self.stats.increment("items_deleted", deleted)
        if reset_happened:
            self.progress.detail(
                f"library {drive.name}: delta reset completed with a replacement full snapshot",
                level=1,
            )
        return PendingDrive(
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
            sync_mode=sync_mode,
            item_count=count,
            file_count=file_count,
            folder_count=folder_count,
            total_size_bytes=total_size_bytes,
            size_observation_complete=size_observation_complete,
            archived_file_count=archived_file_count,
            reactivating_file_count=reactivating_file_count,
            active_file_count=active_file_count,
            unknown_file_archive_count=unknown_file_archive_count,
            item_governance_observation=item_governance_observation,
            descendant_permission_summary=descendant_permission_summary,
        )

    def _stage_drive(
        self,
        drive: Drive,
        state: DriveState,
        *,
        sync_mode: str,
        budget_key: str,
        initial_url: str | None = None,
    ) -> tuple[int, int, int, str]:
        self.state.begin_drive_stage(
            session_id=self.run_id,
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
            base_version=state.version,
            sync_mode=sync_mode,
        )
        selected_initial_url: str | None = None
        core_initial_url: str | None = None
        if initial_url:
            url = initial_url
        elif sync_mode == "delta" and state.delta_link:
            url = state.delta_link
        else:
            drive_path = f"drives/{quote(drive.drive_id, safe='')}/root/delta"
            selected_initial_url = f"{drive_path}?$select={ITEM_SELECT}"
            core_initial_url = f"{drive_path}?$select={CORE_ITEM_SELECT}"
            url = selected_initial_url

        exposure, evidence = self._exposure()
        changed = 0
        deleted = 0
        invalid_items = 0
        final_delta_link: str | None = None
        item_governance_observation = (
            "selected" if selected_initial_url is not None else state.item_governance_observation
        )
        if selected_initial_url is not None and core_initial_url is not None:
            pages: Iterable[tuple[dict[str, object], bool]] = _iter_pages_with_optional_select_fallback(
                self.client,
                selected_initial_url,
                core_initial_url,
            )
        else:
            governance_available = item_governance_observation != "unavailable_unsupported_select"
            pages = ((page, governance_available) for page in self.client.iter_pages(url))
        for page, governance_available in pages:
            if not governance_available:
                item_governance_observation = "unavailable_unsupported_select"
            raw_values = page.get("value")
            if not isinstance(raw_values, list):
                raise GraphProtocolError(status_code=None, code="missing_page_values")
            normalized_items: list[dict[str, object]] = []
            for raw in raw_values:
                if not isinstance(raw, dict):
                    raise GraphProtocolError(status_code=None, code="malformed_page_item")
                try:
                    item = normalize_drive_item(
                        raw,
                        site_id=drive.site.site_id,
                        drive_id=drive.drive_id,
                        exposure=exposure,
                        exposure_evidence=evidence,
                        governance_available=governance_available,
                    )
                except GraphProtocolError as exc:
                    metadata_bound_codes = {
                        "item_missing_id",
                        "item_name_out_of_bounds",
                        "item_path_out_of_bounds",
                        "item_parent_path_invalid",
                    }
                    malformed_metadata_codes = {
                        "item_conflicting_facets",
                        "item_deleted_facet_invalid",
                        "item_file_facet_invalid",
                        "item_folder_facet_invalid",
                        "item_file_archive_status_invalid",
                        "item_parent_id_invalid",
                        "item_parent_reference_invalid",
                        "item_root_facet_invalid",
                    }
                    if exc.code not in metadata_bound_codes | malformed_metadata_codes:
                        raise
                    invalid_items += 1
                    raw_id = str(raw.get("id") or "")
                    safe_fingerprint = hashlib.sha256(raw_id.encode("utf-8", errors="replace")).hexdigest()[:16]
                    issue_code = "ITEM_METADATA_LIMIT" if exc.code in metadata_bound_codes else "ITEM_METADATA_INVALID"
                    self.emit_error(
                        issue_code,
                        "A SharePoint item contained invalid or unsupported metadata "
                        f"(item fingerprint {safe_fingerprint}).",
                        endpoint_key=self._endpoint_key(drive.site),
                        resource_name=drive.name,
                        hint="The item was omitted and this library checkpoint will not advance.",
                    )
                    continue
                if item is None:
                    continue
                if item.get("deleted"):
                    deleted += 1
                else:
                    changed += 1
                normalized_items.append(item)
            self.state.stage_items(
                session_id=self.run_id,
                scope_key=self.scope_key,
                tenant_id=self.context.tenant_id,
                site_id=drive.site.site_id,
                drive_id=drive.drive_id,
                items=normalized_items,
            )
            self.progress.report()
            if self.config.max_items:
                if sync_mode == "full":
                    staged_count = self.state.count_staged_items(
                        session_id=self.run_id,
                        scope_key=self.scope_key,
                        tenant_id=self.context.tenant_id,
                        site_id=drive.site.site_id,
                        drive_id=drive.drive_id,
                    )
                else:
                    staged_count = self.state.count_materialized_items(
                        session_id=self.run_id,
                        scope_key=self.scope_key,
                        tenant_id=self.context.tenant_id,
                        site_id=drive.site.site_id,
                        drive_id=drive.drive_id,
                        require_complete=False,
                    )
                if not self._item_budget.resize(budget_key, staged_count):
                    self.stats.mark_truncated()
                    raise StateStoreError("library snapshot exceeds the configured --max-items safety limit")
            raw_delta_link = page.get("@odata.deltaLink")
            if raw_delta_link is not None:
                if not isinstance(raw_delta_link, str) or not raw_delta_link:
                    raise GraphProtocolError(status_code=None, code="invalid_delta_link")
                final_delta_link = raw_delta_link

        if not final_delta_link:
            raise GraphProtocolError(status_code=None, code="missing_delta_link")
        # Validate before persisting an opaque server-provided URL. This also
        # prevents a tampered local state DB from becoming a bearer-token SSRF.
        self.client.validate_continuation_url(final_delta_link)
        self.state.complete_drive_stage(
            session_id=self.run_id,
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
            delta_link=final_delta_link,
            item_governance_observation=item_governance_observation,
        )
        return changed, deleted, invalid_items, item_governance_observation

    @property
    def sync_mode(self) -> str:
        if not self.config.include_files:
            return "metadata_only"
        with self._pending_lock:
            if not self._sync_modes:
                return "none"
            if len(self._sync_modes) == 1:
                return next(iter(self._sync_modes))
            return "mixed"

    @property
    def permission_run_summary(self) -> dict[str, object]:
        if self._permissions is None:
            return not_requested_permission_summary()
        return self._permissions.snapshot()


def collection_context_record(
    context: GraphTokenContext,
    config: SharePointCollectionConfig,
    *,
    status: str,
    sync_mode: str,
    partial: bool,
    permission_summary: dict[str, object] | None = None,
    graph_attempt_summary: dict[str, object] | None = None,
    structural_complete: bool | None = None,
    content_complete: bool | None = None,
) -> dict[str, object]:
    collection_mode = "tenant_inventory" if context.auth_type == "application" else "delegated_user_view"
    authoritative = context.auth_type == "application" and not config.targeted_sites
    structural_partial = partial if structural_complete is None else not structural_complete
    discovery = (
        "targeted"
        if config.targeted_sites
        else ("getAllSites" if context.auth_type == "application" else config.discovery)
    )
    if status == "failed":
        completeness = "failed"
    elif structural_partial:
        completeness = "partial"
    elif config.targeted_sites:
        completeness = "targeted_scope"
    elif context.auth_type == "delegated":
        completeness = "security_trimmed"
    else:
        completeness = "complete_for_granted_scope"
    metadata: dict[str, object] = {
        "sync_mode": sync_mode,
        "snapshot_materialized": True,
        "comparison_contracts": {
            "structural": SHAREPOINT_STRUCTURAL_COMPARISON_CONTRACT,
            "content": SHAREPOINT_CONTENT_COMPARISON_CONTRACT,
        },
        "discovery_strategy": discovery,
        "discovery_authoritative": authoritative and not structural_partial,
        "files_included": config.include_files,
        "permissions_assessed": False,
        "content_downloaded": False,
        "delta_checkpoint_policy": "after_artifact_finalization_and_upload",
        "graph_attempt_budget": graph_attempt_summary
        or {
            "max_http_attempts": config.max_graph_http_attempts,
            "http_attempts": 0,
            "remaining_http_attempts": config.max_graph_http_attempts,
            "exhausted": False,
            "attempts_by_surface": {},
            "exhausted_surfaces": [],
            "surface_limits": (
                {"permissions": config.max_permission_http_attempts} if config.permissions != "none" else {}
            ),
        },
    }
    if structural_complete is not None:
        metadata["structural_complete"] = structural_complete
    if content_complete is not None:
        metadata["content_complete"] = content_complete
    if config.permissions != "none":
        effective_permission_summary = permission_summary or {
            "contract_version": 1,
            "requested": True,
            "mode": config.permissions,
            "permission_surface": "sharepoint_graph_permissions",
            "semantics": "sharepoint_graph_permission_v1",
            "classification_policy": "positive_evidence_only_v1",
            "response_scope": "effective_sharing_permissions",
            "provider_visibility": "caller_dependent_unverified",
            "request_coverage": "running",
            "candidate_objects": 0,
            "attempted_objects": 0,
            "completed_objects": 0,
            "failed_objects": 0,
            "skipped_objects": 0,
            "http_attempts": 0,
            "entries_observed": 0,
            "entries_emitted": 0,
            "entries_omitted": 0,
            "unknown_entries": 0,
            "partial_reasons": [],
            "budgets": {
                "max_objects": config.max_permission_objects,
                "max_http_attempts": config.max_permission_http_attempts,
                "max_entries": config.max_permission_entries,
                "concurrency": config.permission_concurrency,
            },
        }
        completed_objects = effective_permission_summary.get("completed_objects")
        metadata["permissions_assessed"] = (
            isinstance(completed_objects, int) and not isinstance(completed_objects, bool) and completed_objects > 0
        )
        metadata["permissions_complete"] = bool(
            structural_complete is True and effective_permission_summary.get("request_coverage") == "complete"
        )
        metadata["permission_assessment"] = effective_permission_summary
    return {
        "source": "sharepoint",
        "provider": "sharepoint",
        "collection_mode": collection_mode,
        "auth_context": context.public_metadata(),
        "assessed_identity": context.assessed_identity,
        "collection_status": status,
        "status": status,
        "partial": partial,
        "sync_mode": sync_mode,
        "materialized_snapshot": True,
        "discovery_completeness": completeness,
        "metadata": metadata,
    }


def collection_dimension_completeness(
    stats: dict[str, object],
    config: SharePointCollectionConfig,
    *,
    status: str,
) -> tuple[bool, bool]:
    """Separate inventory/content truth from permission and lifecycle failures."""

    error_codes = stats.get("error_codes") if isinstance(stats.get("error_codes"), dict) else {}
    structural_error = any(
        int(count or 0) > 0
        and (
            str(code) in {"SITE_LIMIT_REACHED", "LIBRARY_LIMIT_REACHED"}
            or str(code).startswith("SITE_DISCOVERY")
            or str(code).startswith("LIBRARY_DISCOVERY")
        )
        for code, count in error_codes.items()
    )
    structural_complete = bool(status != "failed" and int(stats.get("sites_failed") or 0) == 0 and not structural_error)
    content_complete = bool(
        structural_complete
        and config.include_files
        and int(stats.get("libraries_failed") or 0) == 0
        and stats.get("truncated") is not True
    )
    return structural_complete, content_complete
