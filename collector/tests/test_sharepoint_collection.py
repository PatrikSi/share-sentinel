import concurrent.futures
import hashlib
import io
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sharepoint.auth import GraphTokenContext
from sharepoint.collection import (
    ITEM_NAME_MAX_CHARACTERS,
    ITEM_PATH_MAX_CHARACTERS,
    ITEM_SELECT,
    METADATA_TEXT_MAX_CHARACTERS,
    SITE_SELECT,
    SharePointCollectionConfig,
    SharePointCollector,
    SharePointProgress,
    SharePointStats,
    Site,
    collection_context_record,
    discover_drives,
    discover_sites,
    normalize_drive_item,
    resolve_target_site,
)
from sharepoint.graph import GraphAPIError, GraphProtocolError
from sharepoint.state import SharePointStateStore

SITE_ID = "contoso.sharepoint.com,site-guid,web-guid"
SITE_COLLECTION_ID = "contoso.sharepoint.com,site-guid"
DELTA_1 = "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=1"
DELTA_2 = "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=2"
INITIAL_DELTA_1 = f"drives/drive-1/root/delta?$select={ITEM_SELECT}"


def _context(*, delegated: bool = True) -> GraphTokenContext:
    return GraphTokenContext(
        access_token="secret-token",
        auth_mode="token" if delegated else "app",
        auth_type="delegated" if delegated else "application",
        tenant_id="tenant-1",
        client_id="client-1",
        user_id="user-1" if delegated else None,
        user_principal_name="alice@example.com" if delegated else None,
        scopes=("Sites.Read.All",) if delegated else (),
        roles=() if delegated else ("Sites.Read.All",),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )


def _site() -> dict[str, object]:
    return {
        "id": SITE_ID,
        "name": "Finance",
        "displayName": "Finance",
        "webUrl": "https://contoso.sharepoint.com/sites/Finance",
        "siteCollection": {"hostname": "contoso.sharepoint.com"},
    }


def _drive(drive_id: str = "drive-1", name: str = "Documents") -> dict[str, object]:
    return {
        "id": drive_id,
        "name": name,
        "driveType": "documentLibrary",
        "webUrl": f"https://contoso.sharepoint.com/sites/Finance/{name}",
    }


def _file(item_id: str, name: str, parent: str = "/drives/drive-1/root:") -> dict[str, object]:
    return {
        "id": item_id,
        "name": name,
        "size": 42,
        "file": {"mimeType": "text/plain"},
        "parentReference": {"id": "parent-1", "path": parent},
        "webUrl": f"https://contoso.sharepoint.com/{name}",
        "createdDateTime": "2026-01-01T00:00:00Z",
        "lastModifiedDateTime": "2026-01-02T00:00:00Z",
        "eTag": "etag-1",
        "cTag": "ctag-1",
    }


class MemoryWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def emit(self, record: dict[str, object]) -> None:
        self.records.append(record)


class FakeClient:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.max_pages = 100
        self.retry_count = 0
        self.calls: list[tuple[str, str]] = []

    def iter_pages(self, url: str):
        self.calls.append(("pages", url))
        value = self.routes[url]
        if isinstance(value, BaseException):
            raise value
        for page in value:
            if isinstance(page, BaseException):
                raise page
            yield page

    def get(self, url: str):
        self.calls.append(("get", url))
        value = self.routes[url]
        if isinstance(value, BaseException):
            raise value
        return value

    def post(self, url: str, *, json_body):  # noqa: ARG002
        self.calls.append(("post", url))
        value = self.routes[url]
        if isinstance(value, BaseException):
            raise value
        return value

    def validate_continuation_url(self, url: str) -> str:
        if not url.startswith("https://graph.microsoft.com/v1.0/"):
            raise GraphProtocolError(status_code=None, code="unsafe_continuation_url")
        return url


def _routes(delta_pages: object, *, delegated: bool = True, drives=None) -> dict[str, object]:
    discovery = "sites?search=*" if delegated else "sites/getAllSites"
    return {
        discovery: [{"value": [_site()]}],
        f"sites/{SITE_COLLECTION_ID}?$select=id,siteCollection": {
            "id": SITE_COLLECTION_ID,
            "siteCollection": {"hostname": "contoso.sharepoint.com"},
        },
        f"sites/{SITE_ID}/drives?$select=id,name,description,driveType,webUrl,createdDateTime,lastModifiedDateTime": [
            {"value": drives or [_drive()]}
        ],
        INITIAL_DELTA_1: delta_pages,
        DELTA_1: delta_pages,
    }


def _collector(tmp_path, routes, *, run_id="run-1", config=None, delegated=True):
    writer = MemoryWriter()
    state = SharePointStateStore(tmp_path / "state.sqlite3")
    collector = SharePointCollector(
        client=FakeClient(routes),
        state=state,
        writer=writer,
        run_id=run_id,
        context=_context(delegated=delegated),
        config=config or SharePointCollectionConfig(concurrency=1, quiet=True),
    )
    return collector, state, writer


def _commit(state, run_id: str, pending) -> None:
    for drive in pending:
        state.commit_drive(
            session_id=run_id,
            scope_key=drive.scope_key,
            tenant_id=drive.tenant_id,
            site_id=drive.site_id,
            drive_id=drive.drive_id,
        )


def test_app_discovery_uses_paged_get_all_sites() -> None:
    expected = "sites/getAllSites"
    client = FakeClient({expected: [{"value": [_site()]}]})

    sites, truncated = discover_sites(client, _context(delegated=False), SharePointCollectionConfig())

    assert [site.site_id for site in sites] == [SITE_ID]
    assert truncated is False
    assert client.calls == [("pages", expected)]


def test_delegated_discovery_is_explicitly_security_trimmed() -> None:
    expected = "sites?search=*"
    client = FakeClient({expected: [{"value": [_site()]}]})

    sites, _ = discover_sites(client, _context(), SharePointCollectionConfig())
    context = collection_context_record(
        _context(),
        SharePointCollectionConfig(),
        status="success",
        sync_mode="full",
        partial=False,
    )

    assert sites[0].name == "Finance"
    assert context["collection_mode"] == "delegated_user_view"
    assert context["discovery_completeness"] == "security_trimmed"
    assert context["materialized_snapshot"] is True


@pytest.mark.parametrize(
    ("reference", "graph_path"),
    [
        (
            "https://contoso.sharepoint.com/sites/Finance%20Team",
            "sites/contoso.sharepoint.com:/sites/Finance%20Team",
        ),
        (
            "https://contoso.sharepoint.com/sites/Ünicode Team",
            "sites/contoso.sharepoint.com:/sites/%C3%9Cnicode%20Team",
        ),
        (
            "https://contoso.sharepoint.com/sites/R%2FD%3FArchive",
            "sites/contoso.sharepoint.com:/sites/R%2FD%3FArchive",
        ),
    ],
)
def test_target_site_url_is_canonically_encoded_once(reference: str, graph_path: str) -> None:
    selected_path = f"{graph_path}?$select={SITE_SELECT}"
    client = FakeClient({selected_path: _site()})

    site = resolve_target_site(client, reference)

    assert site.site_id == SITE_ID
    assert site.existence_status == "confirmed"
    assert site.archive_status == "not_archived"
    assert site.archive_status_checked is True
    assert site.archive_status_authoritative is False
    assert client.calls == [("get", selected_path)]


def test_target_site_uses_authoritative_graph_archive_status() -> None:
    reference = "https://contoso.sharepoint.com/sites/Finance"
    graph_path = f"sites/contoso.sharepoint.com:/sites/Finance?$select={SITE_SELECT}"
    raw_site = {
        **_site(),
        "siteCollection": {
            "hostname": "contoso.sharepoint.com",
            "archivalDetails": {"archiveStatus": "fullyArchived"},
        },
    }

    site = resolve_target_site(FakeClient({graph_path: raw_site}), reference)

    assert site.requested_target == reference
    assert site.archive_status == "fully_archived"
    assert site.archive_status_checked is True
    assert site.archive_status_authoritative is True


@pytest.mark.parametrize(
    "reference",
    [
        "https://user@contoso.sharepoint.com/sites/Finance",
        "https://contoso.sharepoint.com:443/sites/Finance",
        "https://contoso.sharepoint.com/sites/Finance?view=all",
        "https://contoso.sharepoint.com/sites/Finance#section",
        "https://example.com/sites/Finance",
        "https://contoso.sharepoint.com/sites/Bad%ZZ",
        "https://contoso.sharepoint.com/" + ("x" * 8192),
    ],
)
def test_target_site_url_rejects_unsupported_or_ambiguous_forms(reference: str) -> None:
    with pytest.raises(GraphProtocolError, match="invalid_site_url"):
        resolve_target_site(FakeClient({}), reference)


@pytest.mark.parametrize("reference", ["x" * 513, "site-id\nforged"])
def test_target_site_id_is_bounded_and_control_character_free(reference: str) -> None:
    with pytest.raises(GraphProtocolError, match="invalid_site_id"):
        resolve_target_site(FakeClient({}), reference)


@pytest.mark.parametrize(
    ("reference", "failure", "existence_status", "assessment"),
    [
        ("site-id\nforged", None, "invalid_target", "invalid_target"),
        (
            "https://contoso.sharepoint.com/sites/Missing",
            GraphAPIError(status_code=404, code="itemNotFound"),
            "not_found_or_not_visible",
            "not_found_or_not_visible",
        ),
        (
            "https://contoso.sharepoint.com/sites/Restricted",
            GraphAPIError(status_code=403, code="accessDenied"),
            "permission_denied",
            "inaccessible",
        ),
        (
            "https://contoso.sharepoint.com/sites/Auth",
            GraphAPIError(status_code=401, code="invalidAuthenticationToken"),
            "authentication_failed",
            "authentication_failed",
        ),
        (
            "https://contoso.sharepoint.com/sites/Unavailable",
            GraphAPIError(status_code=503, code="serviceUnavailable", retryable=True),
            "temporarily_unreachable",
            "temporarily_unreachable",
        ),
    ],
)
def test_target_resolution_failures_emit_stable_scoped_endpoint_assessments(
    tmp_path,
    reference: str,
    failure: GraphAPIError | None,
    existence_status: str,
    assessment: str,
) -> None:
    routes: dict[str, object] = {}
    if failure is not None:
        parsed_name = reference.rsplit("/", 1)[-1]
        routes[f"sites/contoso.sharepoint.com:/sites/{parsed_name}?$select={SITE_SELECT}"] = failure
    collector, _, writer = _collector(
        tmp_path,
        routes,
        config=SharePointCollectionConfig(targeted_sites=(reference,), concurrency=1, quiet=True),
    )

    pending, status = collector.collect()

    endpoint = next(record for record in writer.records if record["type"] == "endpoint")
    expected_key = f"sharepoint-target:{hashlib.sha256(reference.strip().encode('utf-8')).hexdigest()[:32]}"
    assert pending == []
    assert status == "failed"
    assert endpoint["endpoint_key"] == expected_key
    assert endpoint["metadata"]["existence_status"] == existence_status
    assert endpoint["metadata"]["assessment"] == assessment
    assert endpoint["metadata"]["lifecycle_state"] == "indeterminate"
    assert "\n" not in endpoint["metadata"]["requested_target"]
    assert collector.stats.endpoints_emitted == 1
    assert collector.stats.sites_failed == 1


def test_invalid_target_evidence_remains_bounded_after_control_character_escaping(tmp_path) -> None:
    reference = ("site\n" * 819) + "x"
    collector, _, writer = _collector(
        tmp_path,
        {},
        config=SharePointCollectionConfig(targeted_sites=(reference,), concurrency=1, quiet=True),
    )

    pending, status = collector.collect()

    endpoint = next(record for record in writer.records if record["type"] == "endpoint")
    requested_target = endpoint["metadata"]["requested_target"]
    assert pending == []
    assert status == "failed"
    assert isinstance(requested_target, str)
    assert len(requested_target) <= METADATA_TEXT_MAX_CHARACTERS
    assert "\n" not in requested_target
    assert "\\u000a" in requested_target
    assert requested_target.endswith("…")


def test_progress_escapes_identity_and_remote_name_control_characters() -> None:
    stream = io.StringIO()
    progress = SharePointProgress(
        SharePointStats(),
        quiet=False,
        verbosity=1,
        interval_seconds=0,
        stream=stream,
    )
    context = _context()
    context = GraphTokenContext(
        access_token=context.access_token,
        auth_mode=context.auth_mode,
        auth_type=context.auth_type,
        tenant_id=context.tenant_id,
        client_id=context.client_id,
        user_id=context.user_id,
        user_principal_name="alice\nforged\x1b[2J@example.com",
        scopes=context.scopes,
        roles=context.roles,
        expires_at=context.expires_at,
    )
    drive = SimpleNamespace(
        name="Documents\rforged",
        site=SimpleNamespace(display_name="Finance\x1b[2J", name="Finance"),
    )

    progress.start(context, "delegated_user_view")
    progress.library_finished(drive, succeeded=True)

    output = stream.getvalue()
    assert "alice\\u000aforged\\u001b[2J@example.com" in output
    assert "Finance\\u001b[2J/Documents\\u000dforged" in output
    assert "\x1b" not in output
    assert "alice\nforged" not in output


def test_zero_progress_interval_disables_periodic_reports_but_keeps_final_summary() -> None:
    stream = io.StringIO()
    progress = SharePointProgress(
        SharePointStats(),
        quiet=False,
        verbosity=0,
        interval_seconds=0,
        stream=stream,
    )

    progress.set_library_total(1)
    progress.report(force=True)
    progress.finish(status="success", graph_retries=0)

    output = stream.getvalue()
    assert "progress:" not in output
    assert "SharePoint collection finished: status=success" in output


def test_periodic_progress_omits_ambiguous_retry_placeholder() -> None:
    stream = io.StringIO()
    progress = SharePointProgress(
        SharePointStats(),
        quiet=False,
        verbosity=0,
        interval_seconds=5,
        stream=stream,
    )

    progress.report(force=True)

    output = stream.getvalue()
    assert "progress:" in output
    assert "elapsed=" in output
    assert "retries_pending" not in output


def test_site_lifecycle_progress_reports_phase_completion() -> None:
    stream = io.StringIO()
    stats = SharePointStats(sites_discovered=2)
    progress = SharePointProgress(
        stats,
        quiet=False,
        verbosity=1,
        interval_seconds=0.001,
        stream=stream,
    )
    site = SimpleNamespace(display_name="Finance", name="Finance")

    progress.set_site_status_total(2)
    progress.site_status_finished(site, succeeded=True)
    progress.site_status_finished(site, succeeded=False)
    progress.report(force=True)

    output = stream.getvalue()
    assert "lifecycle=2/2" in output
    assert "site lifecycle Finance: ok" in output
    assert "site lifecycle Finance: indeterminate" in output


def test_drive_search_supports_graphrunner_and_sharepoint_id_shapes() -> None:
    parent_site_id = "contoso.sharepoint.com,parent-site,parent-web"
    ids_site_id = "contoso.sharepoint.com,collection-guid,web-guid"
    search_response = {
        "value": [
            {
                "hitsContainers": [
                    {
                        "moreResultsAvailable": False,
                        "hits": [
                            {
                                "resource": {
                                    "driveType": "documentLibrary",
                                    "parentReference": {"siteId": parent_site_id},
                                }
                            },
                            {
                                "resource": {
                                    "driveType": "documentLibrary",
                                    "sharePointIds": {
                                        "siteId": "collection-guid",
                                        "webId": "web-guid",
                                        "siteUrl": "https://contoso.sharepoint.com/sites/Finance",
                                    },
                                }
                            },
                            {
                                "resource": {
                                    "driveType": "personal",
                                    "sharePointIds": {
                                        "siteId": "ignored",
                                        "webId": "ignored",
                                        "siteUrl": "https://contoso-my.sharepoint.com/personal/alice",
                                    },
                                }
                            },
                        ],
                    }
                ]
            }
        ]
    }
    client = FakeClient(
        {
            "search/query": search_response,
            f"sites/{parent_site_id}?$select={SITE_SELECT}": {**_site(), "id": parent_site_id},
            f"sites/{ids_site_id}?$select={SITE_SELECT}": {**_site(), "id": ids_site_id},
        }
    )

    sites, truncated = discover_sites(
        client,
        _context(),
        SharePointCollectionConfig(discovery="drive-search"),
    )

    assert {site.site_id for site in sites} == {parent_site_id, ids_site_id}
    assert truncated is False
    assert not any("ignored" in url for _, url in client.calls)


def test_drive_search_reports_document_library_without_site_identity() -> None:
    client = FakeClient(
        {
            "search/query": {
                "value": [
                    {
                        "hitsContainers": [
                            {
                                "moreResultsAvailable": False,
                                "hits": [{"resource": {"driveType": "documentLibrary"}}],
                            }
                        ]
                    }
                ]
            }
        }
    )
    errors: list[GraphAPIError] = []

    sites, truncated = discover_sites(
        client,
        _context(),
        SharePointCollectionConfig(discovery="drive-search"),
        on_site_error=lambda _site, exc: errors.append(exc),
    )

    assert sites == []
    assert truncated is False
    assert errors[0].code == "search_hit_missing_site_identity"


def test_drive_search_stops_immediately_at_site_limit() -> None:
    site_id = "contoso.sharepoint.com,site-one,web-one"
    client = FakeClient(
        {
            "search/query": {
                "value": [
                    {
                        "hitsContainers": [
                            {
                                "moreResultsAvailable": True,
                                "hits": [
                                    {
                                        "resource": {
                                            "driveType": "documentLibrary",
                                            "parentReference": {"siteId": site_id},
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                ]
            },
            f"sites/{site_id}?$select={SITE_SELECT}": {**_site(), "id": site_id},
        }
    )

    sites, truncated = discover_sites(
        client,
        _context(),
        SharePointCollectionConfig(discovery="drive-search", max_sites=1),
    )

    assert [site.site_id for site in sites] == [site_id]
    assert truncated is True
    assert [method for method, _url in client.calls].count("post") == 1


def test_malformed_site_identity_is_skipped_without_inventing_one() -> None:
    expected = "sites?search=*"
    client = FakeClient({expected: [{"value": [{"id": {}, "name": []}, _site()]}]})
    errors: list[GraphAPIError] = []

    sites, _ = discover_sites(
        client,
        _context(),
        SharePointCollectionConfig(),
        on_site_error=lambda _site, exc: errors.append(exc),
    )

    assert [site.site_id for site in sites] == [SITE_ID]
    assert errors[0].code == "site_missing_id"


def test_non_object_site_and_drive_records_are_scoped_and_skipped() -> None:
    site_discovery = "sites?search=*"
    drive_discovery = (
        f"sites/{SITE_ID}/drives?$select=id,name,description,driveType,webUrl,createdDateTime,lastModifiedDateTime"
    )
    client = FakeClient(
        {
            site_discovery: [{"value": ["malformed", _site()]}],
            drive_discovery: [{"value": [[], _drive()]}],
        }
    )
    site_errors: list[GraphAPIError] = []
    drive_errors: list[GraphAPIError] = []

    sites, _ = discover_sites(
        client,
        _context(),
        SharePointCollectionConfig(),
        on_site_error=lambda _site, exc: site_errors.append(exc),
    )
    drives, _ = discover_drives(
        client,
        sites,
        max_libraries=0,
        on_site_error=lambda _site, exc: site_errors.append(exc),
        on_drive_error=lambda _site, exc: drive_errors.append(exc),
    )

    assert [site.site_id for site in sites] == [SITE_ID]
    assert [drive.drive_id for drive in drives] == ["drive-1"]
    assert site_errors[0].code == "malformed_page_item"
    assert drive_errors[0].code == "malformed_page_item"


def test_normalize_drive_items_preserves_stable_ids_paths_and_facets() -> None:
    file_item = normalize_drive_item(
        _file("item-1", "report.txt", "/drives/drive-1/root:/Folder"),
        site_id=SITE_ID,
        drive_id="drive-1",
        exposure="USER_VISIBLE",
        exposure_evidence={"basis": "delegated"},
    )
    folder_item = normalize_drive_item(
        {
            "id": "folder-1",
            "name": "Plans",
            "folder": {"childCount": 2},
            "parentReference": {"path": "/drives/drive-1/root:/Folder"},
        },
        site_id=SITE_ID,
        drive_id="drive-1",
        exposure="USER_VISIBLE",
        exposure_evidence={"basis": "delegated"},
    )

    assert file_item["path"] == "/Folder/report.txt"
    assert file_item["provider_item_id"] == "item-1"
    assert file_item["is_dir"] is False
    assert file_item["mime_type"] == "text/plain"
    assert folder_item["is_dir"] is True
    assert folder_item["path"] == "/Folder/Plans"

    whitespace_item = normalize_drive_item(
        _file("space", " report .txt ", "/drives/drive-1/root:/ Folder "),
        site_id=SITE_ID,
        drive_id="drive-1",
        exposure="USER_VISIBLE",
        exposure_evidence={},
    )
    assert whitespace_item["name"] == " report .txt "
    assert whitespace_item["path"] == "/ Folder / report .txt "


def test_item_name_and_library_relative_path_bounds_are_not_truncated() -> None:
    with pytest.raises(GraphProtocolError) as name_error:
        normalize_drive_item(
            _file("item", "x" * (ITEM_NAME_MAX_CHARACTERS + 1)),
            site_id=SITE_ID,
            drive_id="drive-1",
            exposure="UNKNOWN",
            exposure_evidence={},
        )
    assert name_error.value.code == "item_name_out_of_bounds"

    parent = "/drives/drive-1/root:/" + "p" * ITEM_PATH_MAX_CHARACTERS
    with pytest.raises(GraphProtocolError) as path_error:
        normalize_drive_item(
            _file("item", "x", parent),
            site_id=SITE_ID,
            drive_id="drive-1",
            exposure="UNKNOWN",
            exposure_evidence={},
        )
    assert path_error.value.code == "item_path_out_of_bounds"


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"root": None}, "item_root_facet_invalid"),
        ({"deleted": None}, "item_deleted_facet_invalid"),
        ({"parentReference": []}, "item_parent_reference_invalid"),
        ({"parentReference": {"id": []}}, "item_parent_id_invalid"),
        ({"file": []}, "item_file_facet_invalid"),
        ({"file": {"archiveStatus": []}}, "item_file_archive_status_invalid"),
        ({"folder": []}, "item_folder_facet_invalid"),
        ({"folder": {}, "file": {}}, "item_conflicting_facets"),
    ],
)
def test_malformed_drive_item_facets_are_rejected(changes: dict[str, object], expected_code: str) -> None:
    raw = {**_file("item", "report.txt"), **changes}

    with pytest.raises(GraphProtocolError) as error:
        normalize_drive_item(
            raw,
            site_id=SITE_ID,
            drive_id="drive-1",
            exposure="UNKNOWN",
            exposure_evidence={},
        )

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("fullyArchived", "fully_archived"),
        ("reactivating", "reactivating"),
        ("notArchived", "not_archived"),
        ("unknownFutureValue", "unknown"),
        (None, "not_archived"),
    ],
)
def test_file_archive_status_is_preserved_when_graph_returns_it(raw_status, expected) -> None:
    raw = _file("item", "report.txt")
    raw["file"]["archiveStatus"] = raw_status

    item = normalize_drive_item(
        raw,
        site_id=SITE_ID,
        drive_id="drive-1",
        exposure="UNKNOWN",
        exposure_evidence={},
    )

    assert item is not None
    assert item["metadata"]["file_archive_status"] == expected


def test_missing_file_archive_status_remains_unknown() -> None:
    item = normalize_drive_item(
        _file("item", "report.txt"),
        site_id=SITE_ID,
        drive_id="drive-1",
        exposure="UNKNOWN",
        exposure_evidence={},
    )

    assert item is not None
    assert item["metadata"]["file_archive_status"] == "unknown"


def test_broad_discovery_enriches_archive_lifecycle_and_empty_library_state(tmp_path) -> None:
    routes = _routes([{"value": [], "@odata.deltaLink": DELTA_1}])
    routes[f"sites/{SITE_COLLECTION_ID}?$select=id,siteCollection"] = {
        "id": SITE_COLLECTION_ID,
        "siteCollection": {
            "hostname": "contoso.sharepoint.com",
            "archivalDetails": {"archiveStatus": "fullyArchived"},
        },
    }
    collector, _, writer = _collector(tmp_path, routes)

    pending, status = collector.collect()

    endpoint = next(record for record in writer.records if record["type"] == "endpoint")
    endpoint_metadata = endpoint["metadata"]
    final_resource = [record for record in writer.records if record["type"] == "resource"][-1]
    resource_metadata = final_resource["metadata"]
    assert status == "success"
    assert pending[0].item_count == 0
    assert endpoint_metadata["existence_status"] == "confirmed_from_discovery"
    assert endpoint_metadata["archive_status"] == "fully_archived"
    assert endpoint_metadata["lifecycle_state"] == "archived"
    assert endpoint_metadata["evidence"]["archive_status_scope"] == "site_collection"
    assert endpoint_metadata["evidence"]["archive_status_site_collection_id"] == SITE_COLLECTION_ID
    assert resource_metadata["enumeration_status"] == "complete"
    assert resource_metadata["content_state"] == "empty"
    assert resource_metadata["collection_complete"] is True
    assert resource_metadata["file_count"] == 0
    assert resource_metadata["folder_count"] == 0
    assert resource_metadata["item_count"] == 0
    assert resource_metadata["total_size_bytes"] == 0
    assert final_resource["access_level"] == "list_only"
    assert collector.stats.sites_archived == 1


def test_targeted_unknown_future_lifecycle_marks_run_partial(tmp_path) -> None:
    reference = "https://contoso.sharepoint.com/sites/Finance"
    routes = _routes([{"value": [], "@odata.deltaLink": DELTA_1}])
    routes[f"sites/contoso.sharepoint.com:/sites/Finance?$select={SITE_SELECT}"] = {
        **_site(),
        "siteCollection": {
            "hostname": "contoso.sharepoint.com",
            "archivalDetails": {"archiveStatus": "unknownFutureValue"},
        },
    }
    collector, _, writer = _collector(
        tmp_path,
        routes,
        config=SharePointCollectionConfig(targeted_sites=(reference,), concurrency=1, quiet=True),
    )

    pending, status = collector.collect()

    endpoint = next(record for record in writer.records if record["type"] == "endpoint")
    issue = next(record for record in writer.records if record.get("code") == "SITE_STATUS_INDETERMINATE")
    assert status == "partial"
    assert len(pending) == 1
    assert endpoint["metadata"]["archive_status"] == "unknown"
    assert endpoint["metadata"]["lifecycle_state"] == "indeterminate"
    assert issue["endpoint_key"] == f"sharepoint:{SITE_ID}"
    assert collector.stats.sites_indeterminate == 1


def test_broad_unknown_future_lifecycle_marks_run_partial_without_duplicate_lookup(tmp_path) -> None:
    routes = _routes([{"value": [], "@odata.deltaLink": DELTA_1}])
    routes["sites?search=*"] = [{
        "value": [{
            **_site(),
            "siteCollection": {
                "hostname": "contoso.sharepoint.com",
                "archivalDetails": {"archiveStatus": "unknownFutureValue"},
            },
        }],
    }]
    collector, _, writer = _collector(tmp_path, routes)

    _, status = collector.collect()

    assert status == "partial"
    assert collector.stats.sites_indeterminate == 1
    assert any(record.get("code") == "SITE_STATUS_INDETERMINATE" for record in writer.records)
    assert ("get", f"sites/{SITE_COLLECTION_ID}?$select=id,siteCollection") not in collector.client.calls


def test_archive_enrichment_deduplicates_subsites_in_the_same_site_collection(tmp_path) -> None:
    sites = [
        Site(
            site_id=f"contoso.sharepoint.com,site-guid,web-{index}",
            name=f"Subsite {index}",
            display_name=f"Subsite {index}",
            web_url=f"https://contoso.sharepoint.com/sites/subsite-{index}",
            hostname="contoso.sharepoint.com",
            site_collection_hostname="contoso.sharepoint.com",
            created_at=None,
            modified_at=None,
        )
        for index in range(3)
    ]
    route = f"sites/{SITE_COLLECTION_ID}?$select=id,siteCollection"
    collector, _, _ = _collector(
        tmp_path,
        {
            route: {
                "id": SITE_COLLECTION_ID,
                "siteCollection": {
                    "hostname": "contoso.sharepoint.com",
                    "archivalDetails": {"archiveStatus": "fullyArchived"},
                },
            }
        },
        config=SharePointCollectionConfig(concurrency=3, quiet=True),
    )

    enriched = collector._enrich_site_statuses(sites)

    assert [site.archive_status for site in enriched] == ["fully_archived"] * 3
    assert collector.client.calls.count(("get", route)) == 1


def test_archive_enrichment_reports_unknown_provider_status_as_indeterminate(tmp_path) -> None:
    stream = io.StringIO()
    site = Site(
        site_id=SITE_ID,
        name="Finance",
        display_name="Finance",
        web_url="https://contoso.sharepoint.com/sites/Finance",
        hostname="contoso.sharepoint.com",
        site_collection_hostname="contoso.sharepoint.com",
        created_at=None,
        modified_at=None,
    )
    collector, _, _ = _collector(
        tmp_path,
        {
            f"sites/{SITE_COLLECTION_ID}?$select=id,siteCollection": {
                "id": SITE_COLLECTION_ID,
                "siteCollection": {
                    "hostname": "contoso.sharepoint.com",
                    "archivalDetails": {"archiveStatus": "unknownFutureValue"},
                },
            }
        },
        config=SharePointCollectionConfig(concurrency=1, quiet=False, verbosity=1),
    )
    collector.progress = SharePointProgress(
        collector.stats,
        quiet=False,
        verbosity=1,
        interval_seconds=0,
        stream=stream,
    )

    enriched = collector._enrich_site_statuses([site])

    assert enriched[0].archive_status == "unknown"
    assert "site lifecycle Finance: indeterminate" in stream.getvalue()
    assert "site lifecycle Finance: ok" not in stream.getvalue()


def test_archive_enrichment_keeps_submitted_futures_bounded(monkeypatch, tmp_path) -> None:
    sites = []
    routes = {}
    for index in range(20):
        collection_id = f"contoso.sharepoint.com,collection-{index}"
        sites.append(
            Site(
                site_id=f"{collection_id},web-{index}",
                name=f"Site {index}",
                display_name=f"Site {index}",
                web_url=f"https://contoso.sharepoint.com/sites/site-{index}",
                hostname="contoso.sharepoint.com",
                site_collection_hostname="contoso.sharepoint.com",
                created_at=None,
                modified_at=None,
            )
        )
        routes[f"sites/{collection_id}?$select=id,siteCollection"] = {
            "id": collection_id,
            "siteCollection": {"hostname": "contoso.sharepoint.com"},
        }

    collector, _, _ = _collector(
        tmp_path,
        routes,
        config=SharePointCollectionConfig(concurrency=4, quiet=True),
    )
    original_get = collector.client.get

    def slow_get(url):
        time.sleep(0.01)
        return original_get(url)

    collector.client.get = slow_get
    real_executor = concurrent.futures.ThreadPoolExecutor
    counter_lock = threading.Lock()
    counters = {"outstanding": 0, "maximum": 0}

    class TrackingExecutor:
        def __init__(self, *args, **kwargs):
            self.inner = real_executor(*args, **kwargs)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def submit(self, *args, **kwargs):
            with counter_lock:
                counters["outstanding"] += 1
                counters["maximum"] = max(counters["maximum"], counters["outstanding"])
            future = self.inner.submit(*args, **kwargs)

            def finished(_future):
                with counter_lock:
                    counters["outstanding"] -= 1

            future.add_done_callback(finished)
            return future

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", TrackingExecutor)

    enriched = collector._enrich_site_statuses(sites)

    assert len(enriched) == 20
    assert counters["maximum"] <= 4


def test_library_processing_keeps_submitted_futures_bounded(monkeypatch, tmp_path) -> None:
    drives = [_drive(f"drive-{index}", f"Documents {index}") for index in range(20)]
    routes = _routes([{"value": [], "@odata.deltaLink": DELTA_1}], drives=drives)
    collector, _, _ = _collector(
        tmp_path,
        routes,
        config=SharePointCollectionConfig(concurrency=4, quiet=True),
    )
    real_executor = concurrent.futures.ThreadPoolExecutor
    counter_lock = threading.Lock()
    counters = {"outstanding": 0, "maximum": 0}
    processed: list[str] = []

    class TrackingExecutor:
        def __init__(self, *args, **kwargs):
            self.inner = real_executor(*args, **kwargs)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def submit(self, *args, **kwargs):
            with counter_lock:
                counters["outstanding"] += 1
                counters["maximum"] = max(counters["maximum"], counters["outstanding"])
            future = self.inner.submit(*args, **kwargs)

            def finished(_future):
                with counter_lock:
                    counters["outstanding"] -= 1

            future.add_done_callback(finished)
            return future

    def slow_process(drive):
        time.sleep(0.01)
        with counter_lock:
            processed.append(drive.drive_id)

    collector._process_drive_safely = slow_process
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", TrackingExecutor)

    collector.collect()

    assert len(processed) == 20
    assert counters["maximum"] <= 4


def test_archive_enrichment_failure_is_scoped_and_does_not_block_library_collection(tmp_path) -> None:
    routes = _routes([{"value": [], "@odata.deltaLink": DELTA_1}])
    routes[f"sites/{SITE_COLLECTION_ID}?$select=id,siteCollection"] = GraphAPIError(
        status_code=503,
        code="serviceUnavailable",
        retryable=True,
    )
    collector, _, writer = _collector(tmp_path, routes)

    pending, status = collector.collect()

    endpoint = next(record for record in writer.records if record["type"] == "endpoint")
    issue = next(record for record in writer.records if record.get("code") == "SITE_STATUS_TRANSIENT_FAILURE")
    assert status == "partial"
    assert len(pending) == 1
    assert endpoint["metadata"]["existence_status"] == "confirmed_from_discovery"
    assert endpoint["metadata"]["archive_status"] == "unknown"
    assert endpoint["metadata"]["lifecycle_state"] == "indeterminate"
    assert issue["endpoint_key"] == f"sharepoint:{SITE_ID}"
    assert collector.stats.sites_failed == 0
    assert collector.stats.sites_indeterminate == 1
    assert collector.stats.libraries_succeeded == 1


def test_no_files_collection_marks_content_not_assessed_without_delta_call(tmp_path) -> None:
    routes = _routes([{"value": [_file("unused", "unused.txt")], "@odata.deltaLink": DELTA_1}])
    collector, _, writer = _collector(
        tmp_path,
        routes,
        config=SharePointCollectionConfig(include_files=False, concurrency=1, quiet=True),
    )

    pending, status = collector.collect()

    resource = next(record for record in writer.records if record["type"] == "resource")
    metadata = resource["metadata"]
    assert status == "success"
    assert pending == []
    assert metadata["enumeration_status"] == "not_requested"
    assert metadata["content_state"] == "not_assessed"
    assert metadata["collection_complete"] is False
    assert metadata["item_count"] is None
    assert resource["access_level"] == "unknown"
    assert not any("/root/delta" in url for _method, url in collector.client.calls)


def test_complete_library_reports_specific_items_counts_and_partial_observed_size(tmp_path) -> None:
    known = _file("known", "known.txt")
    known["file"]["archiveStatus"] = "fullyArchived"
    unknown_size = _file("unknown-size", "unknown.bin")
    unknown_size.pop("size")
    unknown_size["file"]["archiveStatus"] = "reactivating"
    folder = {
        "id": "folder-1",
        "name": "Plans",
        "folder": {"childCount": 0},
        "parentReference": {"path": "/drives/drive-1/root:"},
    }
    routes = _routes(
        [
            {
                "value": [known, unknown_size, folder],
                "@odata.deltaLink": DELTA_1,
            }
        ]
    )
    collector, _, writer = _collector(tmp_path, routes)

    _, status = collector.collect()

    items = [record for record in writer.records if record["type"] == "item"]
    final_resource = [record for record in writer.records if record["type"] == "resource"][-1]
    metadata = final_resource["metadata"]
    assert status == "success"
    assert {item["name"] for item in items} == {"known.txt", "unknown.bin", "Plans"}
    assert next(item for item in items if item["provider_item_id"] == "folder-1")["metadata"][
        "folder_child_count"
    ] == 0
    assert metadata["content_state"] == "populated"
    assert metadata["file_count"] == 2
    assert metadata["folder_count"] == 1
    assert metadata["item_count"] == 3
    assert metadata["total_size_bytes"] == 42
    assert metadata["size_observation_complete"] is False
    assert metadata["archived_file_count"] == 1
    assert metadata["reactivating_file_count"] == 1
    assert metadata["active_file_count"] == 0
    assert metadata["unknown_file_archive_count"] == 0
    assert next(item for item in items if item["provider_item_id"] == "known")["metadata"][
        "file_archive_status"
    ] == "fully_archived"


def test_initial_then_incremental_collection_emits_full_materialized_snapshot(tmp_path) -> None:
    first_routes = _routes([{"value": [_file("a", "a.txt"), _file("b", "b.txt")], "@odata.deltaLink": DELTA_1}])
    first, state, first_writer = _collector(tmp_path, first_routes)
    pending, status = first.collect()
    assert status == "success"
    assert len(pending) == 1
    assert pending[0].sync_mode == "full"
    assert ("pages", INITIAL_DELTA_1) in first.client.calls
    _commit(state, "run-1", pending)

    second_routes = _routes(
        [
            {
                "value": [
                    _file("a", "renamed.txt", "/drives/drive-1/root:/Moved"),
                    {"id": "b", "deleted": {}},
                    _file("c", "c.txt"),
                ],
                "@odata.deltaLink": DELTA_2,
            }
        ]
    )
    second, _, second_writer = _collector(tmp_path, second_routes, run_id="run-2")
    pending2, status2 = second.collect()

    items = [record for record in second_writer.records if record["type"] == "item"]
    assert status2 == "success"
    assert pending2[0].sync_mode == "delta"
    assert ("pages", DELTA_1) in second.client.calls
    assert not any(
        url.startswith(DELTA_1) and "$select=" in url for method, url in second.client.calls if method == "pages"
    )
    assert {item["provider_item_id"] for item in items} == {"a", "c"}
    assert next(item for item in items if item["provider_item_id"] == "a")["path"] == "/Moved/renamed.txt"
    resource = [record for record in first_writer.records if record["type"] == "resource"][-1]
    assert resource["access_level"] == "list_only"
    assert resource["exposure"] == "USER_VISIBLE"
    assert resource["exposure_evidence"]["classification_scope"] == "visibility_not_public_exposure"


def test_missing_parent_paths_and_folder_rename_materialize_correct_descendants(
    tmp_path,
) -> None:
    folder = {
        "id": "folder",
        "name": "Folder",
        "folder": {"childCount": 1},
        "parentReference": {"id": "drive-root"},
    }
    child = {
        **_file("child", "report.txt"),
        "parentReference": {"id": "folder"},
    }
    first, state, first_writer = _collector(
        tmp_path,
        _routes([{"value": [folder, child], "@odata.deltaLink": DELTA_1}]),
    )

    pending, status = first.collect()

    assert status == "success"
    first_paths = {
        record["provider_item_id"]: record["path"] for record in first_writer.records if record["type"] == "item"
    }
    assert first_paths == {"folder": "/Folder", "child": "/Folder/report.txt"}
    _commit(state, "run-1", pending)

    renamed_folder = {
        **folder,
        "name": "Renamed",
    }
    second, _, second_writer = _collector(
        tmp_path,
        _routes(
            [
                {
                    "value": [renamed_folder],
                    "@odata.deltaLink": DELTA_2,
                }
            ]
        ),
        run_id="run-2",
    )

    _, second_status = second.collect()

    assert second_status == "success"
    second_paths = {
        record["provider_item_id"]: record["path"] for record in second_writer.records if record["type"] == "item"
    }
    assert second_paths == {
        "folder": "/Renamed",
        "child": "/Renamed/report.txt",
    }


def test_delta_410_recovers_full_without_making_complete_run_partial(tmp_path) -> None:
    first, state, _ = _collector(
        tmp_path,
        _routes([{"value": [_file("a", "a.txt")], "@odata.deltaLink": DELTA_1}]),
    )
    pending, _ = first.collect()
    _commit(state, "run-1", pending)

    reset_url = "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?reset=1"
    routes = _routes(
        GraphAPIError(
            status_code=410,
            code="resyncChangesApplyDifferences",
            reset_url=reset_url,
        )
    )
    routes[reset_url] = [{"value": [_file("new", "new.txt")], "@odata.deltaLink": DELTA_2}]
    recovered, _, writer = _collector(tmp_path, routes, run_id="run-2")

    pending2, status = recovered.collect()

    assert status == "success"
    assert pending2[0].sync_mode == "full"
    assert any(record.get("code") == "DELTA_RESET" for record in writer.records)
    assert recovered.stats.delta_resets == 1


def test_one_inaccessible_library_does_not_abort_other_libraries(tmp_path) -> None:
    routes = _routes(
        [{"value": [_file("a", "a.txt")], "@odata.deltaLink": DELTA_1}],
        drives=[_drive("drive-1", "Documents"), _drive("drive-2", "Restricted")],
    )
    routes[f"drives/drive-2/root/delta?$select={ITEM_SELECT}"] = GraphAPIError(status_code=403, code="accessDenied")
    collector, _, writer = _collector(tmp_path, routes)

    pending, status = collector.collect()

    assert status == "partial"
    assert len(pending) == 1
    assert collector.stats.libraries_failed == 1
    assert any(record.get("code") == "LIBRARY_PERMISSION_DENIED" for record in writer.records)
    restricted = [
        record
        for record in writer.records
        if record.get("type") == "resource" and record.get("name") == "Restricted"
    ][-1]
    assert restricted["access_level"] == "unknown"
    assert restricted["metadata"]["enumeration_status"] == "permission_denied"
    assert restricted["metadata"]["content_state"] == "unknown"
    assert restricted["metadata"]["collection_complete"] is False
    assert restricted["metadata"]["item_count"] is None


def test_malformed_item_is_reported_without_advancing_checkpoint(tmp_path) -> None:
    routes = _routes(
        [
            {
                "value": [_file("valid", "valid.txt"), _file("bad", "x" * 256)],
                "@odata.deltaLink": DELTA_1,
            }
        ]
    )
    collector, state, writer = _collector(tmp_path, routes)

    pending, status = collector.collect()

    assert status == "partial"
    assert pending == []
    assert not any(record.get("provider_item_id") == "valid" for record in writer.records)
    issue = next(record for record in writer.records if record.get("code") == "ITEM_METADATA_LIMIT")
    assert "x" * 256 not in str(issue)
    assert state.count_current_items(collector.scope_key, "tenant-1", SITE_ID, "drive-1") == 0


def test_malformed_item_facet_is_reported_without_advancing_checkpoint(tmp_path) -> None:
    malformed = {**_file("bad", "bad.txt"), "deleted": None}
    routes = _routes([{"value": [_file("valid", "valid.txt"), malformed], "@odata.deltaLink": DELTA_1}])
    collector, state, writer = _collector(tmp_path, routes)

    pending, status = collector.collect()

    assert status == "partial"
    assert pending == []
    assert any(record.get("code") == "ITEM_METADATA_INVALID" for record in writer.records)
    assert state.get_drive_state(collector.scope_key, "tenant-1", SITE_ID, "drive-1").delta_link is None


def test_rejected_library_does_not_consume_run_item_budget(tmp_path) -> None:
    malformed = _file("bad", "x" * (ITEM_NAME_MAX_CHARACTERS + 1))
    routes = _routes(
        [{"value": [_file("discarded", "discarded.txt"), malformed], "@odata.deltaLink": DELTA_1}],
        drives=[_drive("drive-1", "Malformed"), _drive("drive-2", "Healthy")],
    )
    healthy_delta = "https://graph.microsoft.com/v1.0/drives/drive-2/root/delta?token=1"
    routes[f"drives/drive-2/root/delta?$select={ITEM_SELECT}"] = [
        {"value": [_file("healthy", "healthy.txt")], "@odata.deltaLink": healthy_delta}
    ]
    collector, _, writer = _collector(
        tmp_path,
        routes,
        config=SharePointCollectionConfig(max_items=1, concurrency=1, quiet=True),
    )

    pending, status = collector.collect()

    emitted_items = [record for record in writer.records if record["type"] == "item"]
    final_resources = {
        record["name"]: record
        for record in writer.records
        if record["type"] == "resource" and record["metadata"]["enumeration_status"] != "in_progress"
    }
    assert status == "partial"
    assert [drive.drive_id for drive in pending] == ["drive-2"]
    assert [item["provider_item_id"] for item in emitted_items] == ["healthy"]
    assert final_resources["Malformed"]["metadata"]["collection_complete"] is False
    assert final_resources["Healthy"]["metadata"]["collection_complete"] is True
    assert collector.stats.truncated is False


def test_site_limit_marks_app_discovery_non_authoritative() -> None:
    context = collection_context_record(
        _context(delegated=False),
        SharePointCollectionConfig(max_sites=1),
        status="partial",
        sync_mode="full",
        partial=True,
    )

    assert context["discovery_completeness"] == "partial"
    assert context["metadata"]["discovery_authoritative"] is False
