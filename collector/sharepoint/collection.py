from __future__ import annotations

import concurrent.futures
import hashlib
import re
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Iterable, Protocol
from urllib.parse import quote, unquote, urlparse

from .auth import GraphTokenContext
from .graph import GraphAPIError, GraphClient, GraphProtocolError
from .state import DriveState, SharePointStateStore, StateStoreError, state_scope_key

DRIVE_SELECT = "id,name,description,driveType,webUrl,createdDateTime,lastModifiedDateTime"
SITE_SELECT = "id,name,displayName,webUrl,createdDateTime,lastModifiedDateTime,siteCollection"
ITEM_SELECT = (
    "id,name,parentReference,file,folder,root,size,createdDateTime,lastModifiedDateTime,webUrl,eTag,cTag,deleted"
)
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
GRAPH_ARCHIVE_STATUS = {
    "recentlyArchived": "recently_archived",
    "fullyArchived": "fully_archived",
    "reactivating": "reactivating",
}


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


@dataclass
class SharePointStats:
    sites_discovered: int = 0
    sites_failed: int = 0
    endpoints_emitted: int = 0
    sites_archived: int = 0
    sites_not_found: int = 0
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
    max_sites: int = 0
    max_libraries: int = 0
    max_items: int = 0
    concurrency: int = 4
    quiet: bool = False
    verbosity: int = 0
    progress_interval: float = 5.0


class _ItemBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = max(0, int(maximum))
        self.used = 0
        self._lock = threading.Lock()

    def remaining(self) -> int | None:
        with self._lock:
            return None if self.maximum == 0 else max(0, self.maximum - self.used)

    def reserve(self, count: int) -> bool:
        with self._lock:
            if self.maximum and self.used + count > self.maximum:
                return False
            self.used += count
            return True


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
        stats = self.stats.snapshot()
        remaining = "unknown" if total is None else str(max(0, total - processed))
        elapsed = max(0.0, now - self.started)
        self._write(
            "progress: "
            f"sites={stats['sites_discovered']} libraries={processed}/"
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
            # was explicitly selected, absence of archivalDetails is Graph's
            # representation of a collection that is not archived.
            return "not_archived", True, True
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


def _site_from_graph(
    raw: dict[str, object],
    *,
    archive_status_selected: bool = False,
    existence_status: str = "confirmed_from_discovery",
    requested_target: str | None = None,
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
    )


def _drive_from_graph(site: Site, raw: dict[str, object]) -> Drive:
    drive_id = _bounded_exact_text(raw.get("id"), PROVIDER_ID_MAX_CHARACTERS)
    name = _bounded_exact_text(raw.get("name"), RESOURCE_NAME_MAX_CHARACTERS)
    if not drive_id or not name:
        raise GraphProtocolError(status_code=None, code="library_missing_identity")
    return Drive(
        site=site,
        drive_id=drive_id,
        name=name,
        web_url=_bounded_url(raw.get("webUrl")),
        drive_type=_bounded_text(raw.get("driveType"), 64),
        description=_bounded_text(raw.get("description"), METADATA_TEXT_MAX_CHARACTERS),
        created_at=_bounded_text(raw.get("createdDateTime"), 128),
        modified_at=_bounded_text(raw.get("lastModifiedDateTime"), 128),
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
            if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
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
            or not SHAREPOINT_HOST_PATTERN.fullmatch(hostname)
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise GraphProtocolError(status_code=None, code="invalid_site_url")
        encoded_path = _encoded_site_path(parsed.path)
        raw = client.get(f"sites/{hostname}:{encoded_path}?$select={SITE_SELECT}")
    else:
        if (
            len(normalized) > PROVIDER_ID_MAX_CHARACTERS
            or "\x00" in normalized
            or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        ):
            raise GraphProtocolError(status_code=None, code="invalid_site_id")
        raw = client.get(f"sites/{_graph_id_path(normalized)}?$select={SITE_SELECT}")
    return _site_from_graph(
        raw,
        archive_status_selected=True,
        existence_status="confirmed",
        requested_target=normalized,
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
                if max_sites and len(site_targets) >= max_sites:
                    truncated = True
                    more = False
                    break
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
                sites.append(
                    _site_from_graph(
                        client.get(f"sites/{_graph_id_path(site_target)}?$select={SITE_SELECT}"),
                        archive_status_selected=True,
                        existence_status="confirmed",
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


def enrich_site_archive_status(client: GraphClient, site: Site) -> Site:
    """Resolve authoritative site-collection archival details for one site."""

    if site.archive_status_checked:
        return site
    identity_parts = site.site_id.split(",")
    collection_id = ",".join(identity_parts[:2]) if len(identity_parts) >= 2 else site.site_id
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
            pages = client.iter_pages(f"sites/{_graph_id_path(site.site_id)}/drives?$select={DRIVE_SELECT}")
            for page in pages:
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
                        drive = _drive_from_graph(site, raw)
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


def normalize_drive_item(
    raw: dict[str, object],
    *,
    site_id: str,
    drive_id: str,
    exposure: str,
    exposure_evidence: dict[str, object],
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
    mime_type = _bounded_text(file_facet.get("mimeType"), 255) if isinstance(file_facet, dict) else None
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
    return _terminal_safe(str(reference or "").strip(), SITE_TARGET_MAX_BYTES)


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
        self._limited_collection_lock = threading.Lock()
        self.pending_drives: list[PendingDrive] = []
        self._sync_modes: set[str] = set()

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
        if site.archive_status_authoritative:
            evidence["archive_status_source"] = (
                "siteCollection.archivalDetails.archiveStatus"
                if site.archive_status != "not_archived"
                else "siteCollection.archivalDetails absent after explicit selection"
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
                    "created_at": site.created_at,
                    "modified_at": site.modified_at,
                    "collection_mode": self.collection_mode,
                    "auth_type": self.context.auth_type,
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
            self.stats.increment("sites_not_found")
        elif assessment == "inaccessible":
            self.stats.increment("sites_inaccessible")
        else:
            self.stats.increment("sites_indeterminate")
        return endpoint_key

    def _emit_resource(self, drive: Drive, observation: LibraryObservation) -> None:
        exposure, evidence = self._exposure()
        access_level = "list_only" if observation.enumeration_status == "complete" else "unknown"
        metadata: dict[str, object] = {
            "tenant_id": self.context.tenant_id,
            "site_id": drive.site.site_id,
            "drive_id": drive.drive_id,
            "drive_type": drive.drive_type,
            "description": drive.description,
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
        }
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
                "metadata": metadata,
            }
        )

    def _enrich_site_statuses(self, sites: list[Site]) -> list[Site]:
        pending = [(index, site) for index, site in enumerate(sites) if not site.archive_status_checked]
        if not pending:
            return sites

        enriched = list(sites)

        def handle_result(index: int, site: Site) -> None:
            try:
                enriched[index] = enrich_site_archive_status(self.client, site)
            except GraphAPIError as exc:
                self.stats.increment("sites_indeterminate")
                self.emit_error(
                    _error_code(exc, prefix="SITE_STATUS"),
                    str(exc),
                    endpoint_key=self._endpoint_key(site),
                    hint=(
                        "The site was discovered, but its archive lifecycle could not be confirmed; "
                        "library enumeration continues."
                    ),
                )

        max_workers = max(1, min(int(self.config.concurrency), 16, len(pending)))
        if max_workers == 1:
            for index, site in pending:
                handle_result(index, site)
            return enriched

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="sharepoint-site-status",
        ) as executor:
            futures = {
                executor.submit(enrich_site_archive_status, self.client, site): (index, site)
                for index, site in pending
            }
            for future in concurrent.futures.as_completed(futures):
                index, site = futures[future]
                try:
                    enriched[index] = future.result()
                except GraphAPIError as exc:
                    self.stats.increment("sites_indeterminate")
                    self.emit_error(
                        _error_code(exc, prefix="SITE_STATUS"),
                        str(exc),
                        endpoint_key=self._endpoint_key(site),
                        hint=(
                            "The site was discovered, but its archive lifecycle could not be confirmed; "
                            "library enumeration continues."
                        ),
                    )
        return enriched

    def collect(self) -> tuple[list[PendingDrive], str]:
        self.state.initialize()
        self.state.cleanup_stale_sessions()
        self.progress.start(self.context, self.collection_mode)

        def _site_discovery_error(site: Site | None, exc: BaseException) -> None:
            self.stats.increment("sites_failed")
            self.emit_error(
                _error_code(exc, prefix="SITE_DISCOVERY"),
                str(exc),
                endpoint_key=self._endpoint_key(site) if site else None,
                hint="Other site targets continue; verify the target and Graph read permissions.",
            )

        def _target_discovery_error(reference: str, exc: BaseException) -> None:
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
            self.emit_error(
                _error_code(exc, prefix="SITE_DISCOVERY"),
                str(exc),
                hint="Verify Graph read permissions and the selected discovery strategy.",
            )
            self.stats.increment("sites_failed")
            return [], "failed"

        if sites_truncated:
            self.stats.mark_truncated()
            self.emit_error(
                "SITE_LIMIT_REACHED",
                "SharePoint site discovery reached the configured safety limit.",
                hint="Increase --max-sites after reviewing tenant scale.",
            )
        sites = self._enrich_site_statuses(sites)
        self.stats.sites_discovered = len(sites)
        for site in sites:
            self._emit_endpoint(site)

        def _site_error(site: Site, exc: BaseException) -> None:
            self.stats.increment("sites_failed")
            self.emit_error(
                _error_code(exc, prefix="LIBRARY_DISCOVERY"),
                str(exc),
                endpoint_key=self._endpoint_key(site),
                hint="The site remains in the artifact; verify the token can enumerate its drives.",
            )

        def _drive_error(site: Site, exc: BaseException) -> None:
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
            self.stats.mark_truncated()
            self.emit_error(
                "LIBRARY_LIMIT_REACHED",
                "SharePoint library discovery reached the configured safety limit.",
                hint="Increase --max-libraries after reviewing tenant scale.",
            )
        self.stats.libraries_discovered = len(drives)
        self.progress.set_library_total(len(drives))

        if not self.config.include_files:
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

        max_workers = max(1, min(int(self.config.concurrency), 16, len(drives) or 1))
        if max_workers == 1:
            for drive in drives:
                self._process_drive_safely(drive)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="sharepoint-graph",
            ) as executor:
                futures = [executor.submit(self._process_drive_safely, drive) for drive in drives]
                for future in concurrent.futures.as_completed(futures):
                    # Writer/state failures not handled as per-drive Graph errors
                    # intentionally abort finalization so no checkpoint advances.
                    future.result()
        return list(self.pending_drives), self._status()

    def _status(self) -> str:
        snapshot = self.stats.snapshot()
        incomplete = bool(snapshot["sites_failed"] or snapshot["libraries_failed"] or snapshot["truncated"])
        if incomplete:
            if snapshot["sites_discovered"] or snapshot["libraries_succeeded"]:
                return "partial"
            return "failed"
        return "success"

    def _process_drive_safely(self, drive: Drive) -> None:
        try:
            if self.config.max_items:
                with self._limited_collection_lock:
                    pending = self._process_drive(drive)
            else:
                pending = self._process_drive(drive)
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
                ),
            )
            with self._pending_lock:
                self.pending_drives.append(pending)
            self.stats.increment("libraries_succeeded")
            self.stats.increment("delta_drives" if pending.sync_mode == "delta" else "full_drives")
            self.progress.library_finished(drive, succeeded=True)
        except (GraphAPIError, StateStoreError) as exc:
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

    def _process_drive(self, drive: Drive) -> PendingDrive:
        state = self.state.get_drive_state(
            self.scope_key,
            self.context.tenant_id,
            drive.site.site_id,
            drive.drive_id,
        )
        force_full = self.config.full_sync or self.config.reset_delta
        sync_mode = "delta" if state.delta_link and state.status == "ok" and not force_full else "full"
        if sync_mode == "delta":
            remaining = self._item_budget.remaining()
            current_count = self.state.count_current_items(
                self.scope_key,
                self.context.tenant_id,
                drive.site.site_id,
                drive.drive_id,
            )
            if remaining is not None and current_count > remaining:
                raise StateStoreError("materialized library snapshot exceeds the remaining --max-items safety limit")

        reset_happened = False
        try:
            changed, deleted, invalid_items = self._stage_drive(
                drive,
                state,
                sync_mode=sync_mode,
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
            changed, deleted, invalid_items = self._stage_drive(
                drive,
                state,
                sync_mode="full",
                initial_url=exc.reset_url,
            )
            sync_mode = "full"

        with self._pending_lock:
            self._sync_modes.add(sync_mode)

        count = self.state.count_materialized_items(
            session_id=self.run_id,
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
        )
        if not self._item_budget.reserve(count):
            self.stats.mark_truncated()
            raise StateStoreError("materialized library snapshot exceeds the configured --max-items safety limit")

        exposure, evidence = self._exposure()
        file_count = 0
        folder_count = 0
        total_size_bytes = 0
        size_observation_complete = True
        for item in self.state.iter_materialized_items(
            session_id=self.run_id,
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
        ):
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
            record["exposure"] = exposure
            record["exposure_evidence"] = evidence
            self.writer.emit(record)
            self.stats.increment("items_emitted")
            if bool(item.get("is_dir")):
                folder_count += 1
                self.stats.increment("folders")
            else:
                file_count += 1
                self.stats.increment("files")
                item_size = item.get("size")
                if isinstance(item_size, int) and not isinstance(item_size, bool) and item_size >= 0:
                    total_size_bytes += item_size
                else:
                    size_observation_complete = False

        self.stats.increment("items_changed", changed)
        self.stats.increment("items_deleted", deleted)
        if invalid_items:
            raise StateStoreError(
                "one or more item metadata records exceeded supported bounds; "
                "the delta checkpoint was intentionally withheld"
            )
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
        )

    def _stage_drive(
        self,
        drive: Drive,
        state: DriveState,
        *,
        sync_mode: str,
        initial_url: str | None = None,
    ) -> tuple[int, int, int]:
        self.state.begin_drive_stage(
            session_id=self.run_id,
            scope_key=self.scope_key,
            tenant_id=self.context.tenant_id,
            site_id=drive.site.site_id,
            drive_id=drive.drive_id,
            base_version=state.version,
            sync_mode=sync_mode,
        )
        if initial_url:
            url = initial_url
        elif sync_mode == "delta" and state.delta_link:
            url = state.delta_link
        else:
            url = f"drives/{quote(drive.drive_id, safe='')}/root/delta?$select={ITEM_SELECT}"

        exposure, evidence = self._exposure()
        changed = 0
        deleted = 0
        invalid_items = 0
        final_delta_link: str | None = None
        for page in self.client.iter_pages(url):
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
            remaining = self._item_budget.remaining()
            if sync_mode == "full" and remaining is not None:
                staged_count = self.state.count_staged_items(
                    session_id=self.run_id,
                    scope_key=self.scope_key,
                    tenant_id=self.context.tenant_id,
                    site_id=drive.site.site_id,
                    drive_id=drive.drive_id,
                )
                if staged_count > remaining:
                    self.stats.mark_truncated()
                    raise StateStoreError("full library snapshot exceeds the remaining --max-items safety limit")
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
        )
        return changed, deleted, invalid_items

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


def collection_context_record(
    context: GraphTokenContext,
    config: SharePointCollectionConfig,
    *,
    status: str,
    sync_mode: str,
    partial: bool,
) -> dict[str, object]:
    collection_mode = "tenant_inventory" if context.auth_type == "application" else "delegated_user_view"
    authoritative = context.auth_type == "application" and not config.targeted_sites
    discovery = (
        "targeted"
        if config.targeted_sites
        else ("getAllSites" if context.auth_type == "application" else config.discovery)
    )
    if status == "failed":
        completeness = "failed"
    elif partial:
        completeness = "partial"
    elif config.targeted_sites:
        completeness = "targeted_scope"
    elif context.auth_type == "delegated":
        completeness = "security_trimmed"
    else:
        completeness = "complete_for_granted_scope"
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
        "metadata": {
            "sync_mode": sync_mode,
            "snapshot_materialized": True,
            "discovery_strategy": discovery,
            "discovery_authoritative": authoritative and not partial,
            "files_included": config.include_files,
            "permissions_assessed": False,
            "content_downloaded": False,
            "delta_checkpoint_policy": "after_artifact_finalization_and_upload",
        },
    }
