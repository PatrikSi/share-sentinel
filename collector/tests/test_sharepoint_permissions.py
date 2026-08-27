from __future__ import annotations

import json

from sharepoint.graph import GraphAPIError
from sharepoint.permissions import DirectPermissionCollector, PermissionSubject


class FakePermissionClient:
    def __init__(self, *, root_id: str = "root-1", pages=None, failure: BaseException | None = None) -> None:
        self.root_id = root_id
        self.pages = pages if pages is not None else [{"value": []}]
        self.failure = failure
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _charge(attempt_budget) -> None:
        if attempt_budget is not None and not attempt_budget.reserve_attempt():
            raise GraphAPIError(status_code=None, code="request_budget_exhausted")

    def get(self, url: str, *, attempt_budget=None):
        self.calls.append(("get", url))
        self._charge(attempt_budget)
        if self.failure:
            raise self.failure
        return {"id": self.root_id}

    def iter_pages(self, url: str, *, attempt_budget=None):
        self.calls.append(("pages", url))
        if self.failure:
            self._charge(attempt_budget)
            raise self.failure
        for page in self.pages:
            self._charge(attempt_budget)
            yield page


def _subject(
    *,
    item_id: str | None = "item-1",
    path: str = "/Folder/report.docx",
    site_id: str = "contoso.sharepoint.com,site-1,web-1",
) -> PermissionSubject:
    return PermissionSubject(
        endpoint_key="sharepoint:site-1",
        resource_name="Documents",
        site_id=site_id,
        drive_id="drive-1",
        item_id=item_id,
        subject_kind="item" if item_id else "resource",
        subject_path=path if item_id else None,
    )


def _collector(client, **overrides) -> DirectPermissionCollector:
    options = {
        "max_objects": 100,
        "max_http_attempts": 100,
        "max_entries": 100,
        "concurrency": 2,
    }
    options.update(overrides)
    return DirectPermissionCollector(
        client=client,
        run_id="run-1",
        tenant_id="tenant-1",
        mode="all_items",
        **options,
    )


def _permission(permission_id: str, **fields):
    return {"id": permission_id, "roles": ["read"], **fields}


def test_anonymous_link_is_positive_evidence_and_link_secrets_are_never_emitted() -> None:
    client = FakePermissionClient(
        pages=[
            {
                "value": [
                    _permission(
                        "permission-1",
                        shareId="u!bearer-token",
                        link={
                            "scope": "anonymous",
                            "type": "view",
                            "webUrl": "https://contoso.sharepoint.com/:w:/token",
                            "webHtml": "<iframe src='token'>",
                            "preventsDownload": True,
                        },
                        hasPassword=True,
                    )
                ]
            }
        ]
    )

    result = _collector(client).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )

    assert result.complete is True
    assert result.exposure == "ANONYMOUS"
    assert result.permission_summary["positive_evidence"] == ["anonymous_link"]
    assert len(result.entry_records) == 1
    assert result.entry_records[0].get("principal") is None
    serialized = json.dumps([result.assessment_record, *result.entry_records])
    assert "bearer-token" not in serialized
    assert "webUrl" not in serialized
    assert "webHtml" not in serialized
    assert "shareId" not in serialized
    assert result.entry_records[0]["provider_details"] == {
        "permission_kind": "link",
        "roles": ["read"],
        "link_scope": "anonymous",
        "link_type": "view",
        "prevents_download": True,
        "has_password": True,
        "invitation_sign_in_required": None,
        "inherited_from": None,
        "expiration_state": "not_set",
    }
    assert result.entry_records[0]["inherited_state"] == "unknown"


def test_email_less_invitation_is_complete_principal_less_permission_evidence() -> None:
    result = _collector(
        FakePermissionClient(
            pages=[
                {
                    "value": [
                        _permission(
                            "permission-1",
                            invitation={"signInRequired": True},
                        )
                    ]
                }
            ]
        )
    ).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )

    assert result.complete is True
    assert len(result.entry_records) == 1
    assert result.entry_records[0]["entry_kind"] == "invitation"
    assert result.entry_records[0].get("principal") is None
    assert result.entry_records[0]["provider_details"]["invitation_sign_in_required"] is True


def test_empty_or_existing_access_permission_evidence_never_claims_restricted_access() -> None:
    empty = _collector(FakePermissionClient()).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )
    existing_access = _collector(
        FakePermissionClient(
            pages=[
                {
                    "value": [
                        _permission(
                            "permission-1",
                            link={"scope": "existingAccess", "type": "view"},
                        )
                    ]
                }
            ]
        )
    ).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )

    assert empty.complete is True
    assert empty.exposure == "UNKNOWN"
    assert empty.permission_summary["negative_conclusion_supported"] is False
    assert existing_access.complete is True
    assert existing_access.exposure == "UNKNOWN"
    assert existing_access.entry_records[0]["effect"] == "no_new_access"
    assert "RESTRICTED" not in json.dumps([empty.permission_summary, existing_access.permission_summary])


def test_specific_people_identity_does_not_guess_externality_and_combines_user_facets() -> None:
    identity = {
        "user": {"id": "entra-user-1", "displayName": "Alex Guest #EXT#"},
        "siteUser": {
            "id": "42",
            "displayName": "Alex Guest #EXT#",
            "loginName": "i:0#.f|membership|alex_external#ext#@contoso.onmicrosoft.com",
        },
    }
    client = FakePermissionClient(
        pages=[
            {
                "value": [
                    _permission(
                        "permission-1",
                        grantedToIdentitiesV2=[identity],
                        link={"scope": "users", "type": "edit"},
                    )
                ]
            }
        ]
    )

    result = _collector(client).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )

    assert result.complete is True
    assert result.exposure == "UNKNOWN"
    assert len(result.entry_records) == 1
    principal = result.entry_records[0]["principal"]
    assert principal["kind"] == "user"
    assert principal["native_id"] == "entra-user-1"
    assert principal["authority"].startswith("entra_id:v1:")
    assert principal["aliases"] == [
        "Alex Guest #EXT#",
        "i:0#.f|membership|alex_external#ext#@contoso.onmicrosoft.com",
    ]
    assert principal["resolution"] == "provider_identifiers_no_group_expansion"
    assert len(principal["resolution"]) <= 40
    assert "externality" not in principal
    assert "EXTERNAL" not in json.dumps(result.permission_summary)


def test_site_local_principal_authority_prevents_cross_site_identity_collision() -> None:
    identity = {"siteUser": {"id": "5", "displayName": "Site Owners"}}
    first_client = FakePermissionClient(pages=[{"value": [_permission("permission-1", grantedToV2=identity)]}])
    second_client = FakePermissionClient(pages=[{"value": [_permission("permission-1", grantedToV2=identity)]}])

    first = _collector(first_client).assess_item(
        _subject(site_id="site-a"),
        base_exposure="UNKNOWN",
        base_evidence={},
    )
    second = _collector(second_client).assess_item(
        _subject(site_id="site-b"),
        base_exposure="UNKNOWN",
        base_evidence={},
    )

    first_principal = first.entry_records[0]["principal"]
    second_principal = second.entry_records[0]["principal"]
    assert first_principal["native_id"] == second_principal["native_id"] == "5"
    assert first_principal["authority"].startswith("sharepoint:v1:")
    assert first_principal["authority"] != second_principal["authority"]
    assert first_principal["principal_key"] != second_principal["principal_key"]


def test_graph_inheritance_source_is_normalized_without_mutable_path_or_share_secret() -> None:
    client = FakePermissionClient(
        pages=[
            {
                "value": [
                    _permission(
                        "permission-1",
                        grantedToV2={"group": {"id": "group-1", "displayName": "Reviewers"}},
                        inheritedFrom={
                            "driveId": "drive-1",
                            "id": "folder-1",
                            "path": "/drive/root:/Renamable Folder",
                            "shareId": "never-store-this",
                        },
                    )
                ]
            }
        ]
    )

    result = _collector(client).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={},
    )

    assert result.complete is True
    entry = result.entry_records[0]
    assert entry["inherited_state"] == "inherited"
    assert entry["provider_details"]["inherited_from"] == {
        "provider_item_id": "folder-1",
        "provider_drive_id": "drive-1",
    }
    serialized = json.dumps(entry)
    assert "Renamable Folder" not in serialized
    assert "never-store-this" not in serialized


def test_unknown_link_semantics_make_object_and_run_partial_without_losing_entry() -> None:
    client = FakePermissionClient(
        pages=[
            {
                "value": [
                    _permission(
                        "permission-1",
                        link={"scope": "unknownFutureValue", "type": "view"},
                    )
                ]
            }
        ]
    )
    collector = _collector(client)

    result = collector.assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )

    assert result.assessment_state == "partial"
    assert result.complete is False
    assert result.entry_records[0]["provider_details"]["link_scope"] == "unknown"
    assert result.permission_summary["semantic_coverage"] == "partial_unknown_semantics"
    assert collector.snapshot()["request_coverage"] == "partial"
    assert collector.snapshot()["unknown_entries"] == 1


def test_invalid_provider_expiration_is_omitted_and_marks_semantics_partial() -> None:
    result = _collector(
        FakePermissionClient(
            pages=[
                {
                    "value": [
                        _permission(
                            "permission-1",
                            expirationDateTime="not-a-timestamp",
                            link={"scope": "organization", "type": "view"},
                        )
                    ]
                }
            ]
        )
    ).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={},
    )

    assert result.assessment_state == "partial"
    assert result.exposure == "UNKNOWN"
    assert result.entry_records[0]["expiration_at"] is None
    assert result.entry_records[0]["effect"] == "unknown"


def test_expired_link_is_retained_but_does_not_support_current_exposure() -> None:
    result = _collector(
        FakePermissionClient(
            pages=[
                {
                    "value": [
                        _permission(
                            "permission-1",
                            expirationDateTime="2000-01-01T00:00:00Z",
                            link={"scope": "anonymous", "type": "view"},
                        ),
                        _permission(
                            "permission-2",
                            expirationDateTime="0001-01-01T00:00:00Z",
                            link={"scope": "organization", "type": "view"},
                        ),
                    ]
                }
            ]
        )
    ).assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={},
    )

    assert result.complete is True
    entries = {entry["provider_entry_id"]: entry for entry in result.entry_records}
    assert entries["permission-1"]["effect"] == "expired"
    assert entries["permission-1"]["provider_details"]["expiration_state"] == "expired"
    assert entries["permission-2"]["effect"] == "allow"
    assert entries["permission-2"]["provider_details"]["expiration_state"] == "not_set"
    assert result.exposure == "BROAD_INTERNAL"


def test_later_page_failure_preserves_positive_observations_as_explicitly_partial() -> None:
    class PartialPageClient(FakePermissionClient):
        def iter_pages(self, url: str, *, attempt_budget=None):
            self.calls.append(("pages", url))
            self._charge(attempt_budget)
            yield {
                "value": [
                    _permission(
                        "permission-1",
                        link={"scope": "anonymous", "type": "view"},
                    )
                ]
            }
            self._charge(attempt_budget)
            raise GraphAPIError(status_code=503, code="serviceUnavailable", retryable=True)

    collector = _collector(PartialPageClient())

    result = collector.assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )

    assert result.complete is False
    assert result.assessment_state == "partial"
    assert result.exposure == "ANONYMOUS"
    assert len(result.entry_records) == 1
    assert result.permission_summary["retrieval_coverage"] == "partial"
    assert result.permission_summary["semantic_coverage"] == "complete_for_observed_subset"
    assert result.permission_summary["entry_set_hash"] is None
    assert result.assessment_record["entries_emitted"] == 1
    assert result.assessment_record["error_code"] == "PERMISSION_TEMPORARILY_UNREACHABLE"
    assert collector.snapshot()["request_coverage"] == "partial"


def test_entry_budget_is_global_bounded_and_reports_omission_explicitly() -> None:
    identities = [
        {"user": {"id": "user-1", "displayName": "One"}},
        {"user": {"id": "user-2", "displayName": "Two"}},
    ]
    client = FakePermissionClient(pages=[{"value": [_permission("permission-1", grantedToIdentitiesV2=identities)]}])
    collector = _collector(client, max_entries=1)

    result = collector.assess_item(
        _subject(),
        base_exposure="UNKNOWN",
        base_evidence={"basis": "exposure_not_assessed"},
    )

    assert len(result.entry_records) == 1
    assert result.permission_summary["entries_observed"] == 2
    assert result.permission_summary["entries_emitted"] == 1
    assert result.permission_summary["entries_omitted"] == 1
    assert result.permission_summary["entry_set_hash"] is None
    assert result.permission_summary["assessment_state"] == "partial"
    assert collector.snapshot()["entries_observed"] == 2
    assert collector.snapshot()["request_coverage"] == "partial"


def test_object_and_http_attempt_budgets_stop_new_graph_work() -> None:
    object_client = FakePermissionClient()
    object_collector = _collector(object_client, max_objects=1)
    first = object_collector.assess_item(
        _subject(item_id="item-1"),
        base_exposure="UNKNOWN",
        base_evidence={},
    )
    second = object_collector.assess_item(
        _subject(item_id="item-2"),
        base_exposure="UNKNOWN",
        base_evidence={},
    )

    assert first.complete is True
    assert second.assessment_record is None
    assert second.assessment_state == "not_assessed"
    assert second.permission_summary["failure_reason"] == "budget_exhausted"
    assert len(object_client.calls) == 1
    assert object_collector.snapshot()["request_coverage"] == "budget_exhausted"

    http_client = FakePermissionClient()
    http_collector = _collector(http_client, max_http_attempts=1)
    root = http_collector.assess_root(
        _subject(item_id=None),
        base_exposure="UNKNOWN",
        base_evidence={},
    )

    assert root.assessment_state == "failed"
    assert root.permission_summary["failure_reason"] == "budget_exhausted"
    assert root.permission_summary["error_code"] == "PERMISSION_HTTP_BUDGET_EXHAUSTED"
    assert http_collector.snapshot()["http_attempts"] == 1
    assert http_collector.snapshot()["request_coverage"] == "partial"


def test_upstream_selection_failure_makes_run_coverage_partial_without_fabricated_objects() -> None:
    collector = _collector(FakePermissionClient())

    collector.mark_selection_incomplete("content_enumeration_failed")

    snapshot = collector.snapshot()
    assert snapshot["candidate_objects"] == 0
    assert snapshot["selection_incomplete_scopes"] == 1
    assert snapshot["request_coverage"] == "partial"
    assert snapshot["partial_reasons"] == ["selection_content_enumeration_failed"]


def test_authentication_failure_trips_run_circuit_and_errors_are_deduplicated() -> None:
    reported: list[str] = []
    client = FakePermissionClient(failure=GraphAPIError(status_code=401, code="InvalidAuthenticationToken"))
    collector = DirectPermissionCollector(
        client=client,
        run_id="run-1",
        tenant_id="tenant-1",
        mode="all_items",
        max_objects=100,
        max_http_attempts=100,
        max_entries=100,
        concurrency=2,
        on_error=lambda code, _exc, _subject: reported.append(code),
    )

    first = collector.assess_item(_subject(item_id="item-1"), base_exposure="UNKNOWN", base_evidence={})
    second = collector.assess_item(_subject(item_id="item-2"), base_exposure="UNKNOWN", base_evidence={})

    assert first.assessment_state == "failed"
    assert first.permission_summary["failure_reason"] == "authentication_failed"
    assert second.assessment_record is None
    assert second.assessment_state == "not_assessed"
    assert second.permission_summary["failure_reason"] == "authentication_failed"
    assert reported == ["PERMISSION_AUTHENTICATION_FAILED"]
    assert collector.snapshot()["attempted_objects"] == 1
    assert collector.snapshot()["skipped_objects"] == 1


def test_hash_identity_ignores_path_and_display_alias_but_detects_right_change() -> None:
    def assess(path: str, display: str, role: str):
        client = FakePermissionClient(
            pages=[
                {
                    "value": [
                        {
                            "id": "permission-1",
                            "roles": [role],
                            "grantedToV2": {"user": {"id": "user-1", "displayName": display}},
                        }
                    ]
                }
            ]
        )
        return _collector(client).assess_item(
            _subject(path=path),
            base_exposure="UNKNOWN",
            base_evidence={},
        )

    baseline = assess("/Old/report.docx", "Old display", "read")
    moved = assess("/New/renamed.docx", "New display", "read")
    changed = assess("/New/renamed.docx", "New display", "write")

    assert baseline.assessment_record["assessment_key"] == moved.assessment_record["assessment_key"]
    assert baseline.assessment_record["subject_key"] == moved.assessment_record["subject_key"]
    assert baseline.entry_records[0]["entry_key"] == moved.entry_records[0]["entry_key"]
    assert baseline.entry_records[0]["evidence_hash"] == moved.entry_records[0]["evidence_hash"]
    assert baseline.entry_records[0]["evidence_hash"] != changed.entry_records[0]["evidence_hash"]
