import uuid
from types import SimpleNamespace

import pytest
from app.routers import runs as runs_router


def test_build_run_diff_reports_new_removed_and_changed_shares() -> None:
    baseline_snapshot = {
        ("10.0.0.10:445", "smb_share", "Finance"): {
            "endpoint_key": "10.0.0.10:445",
            "hostname": "fs-01",
            "ip": "10.0.0.10",
            "share_name": "Finance",
            "share_type": "smb",
            "access_level": "read",
            "item_paths": {"\\Budget.xlsx", "\\Policies", "\\Policies\\Readme.txt"},
        },
        ("10.0.0.11:445", "smb_share", "HR"): {
            "endpoint_key": "10.0.0.11:445",
            "hostname": "fs-02",
            "ip": "10.0.0.11",
            "share_name": "HR",
            "share_type": "smb",
            "access_level": "read",
            "item_paths": {"\\Handbook.pdf"},
        },
    }
    current_snapshot = {
        ("10.0.0.10:445", "smb_share", "Finance"): {
            "endpoint_key": "10.0.0.10:445",
            "hostname": "fs-01",
            "ip": "10.0.0.10",
            "share_name": "Finance",
            "share_type": "smb",
            "access_level": "read_write",
            "item_paths": {"\\Budget.xlsx", "\\Policies", "\\Policies\\Q1.xlsx"},
        },
        ("10.0.0.12:445", "nfs_share", "/exports/backups"): {
            "endpoint_key": "10.0.0.12:445",
            "hostname": "nfs-01",
            "ip": "10.0.0.12",
            "share_name": "/exports/backups",
            "share_type": "nfs",
            "access_level": "read",
            "item_paths": {"\\nightly", "\\nightly\\backup-01.zip"},
        },
    }

    payload = runs_router._build_run_diff(current_snapshot, baseline_snapshot, example_limit=3)

    assert payload["summary"] == {
        "new_shares": 1,
        "disappeared_shares": 1,
        "changed_shares": 1,
        "added_items": 1,
        "removed_items": 1,
    }
    assert payload["new_shares"][0]["share_name"] == "/exports/backups"
    assert payload["new_shares"][0]["item_count"] == 2
    assert payload["disappeared_shares"][0]["share_name"] == "HR"
    assert payload["item_churn"][0]["share_name"] == "Finance"
    assert payload["item_churn"][0]["added_examples"] == ["\\Policies\\Q1.xlsx"]
    assert payload["item_churn"][0]["removed_examples"] == ["\\Policies\\Readme.txt"]


def test_build_run_diff_returns_empty_summary_for_identical_snapshots() -> None:
    snapshot = {
        ("10.0.0.10:445", "smb_share", "Finance"): {
            "endpoint_key": "10.0.0.10:445",
            "hostname": "fs-01",
            "ip": "10.0.0.10",
            "share_name": "Finance",
            "share_type": "smb",
            "access_level": "read",
            "item_paths": {"\\Budget.xlsx"},
        }
    }

    payload = runs_router._build_run_diff(snapshot, snapshot, example_limit=3)

    assert payload["summary"] == {
        "new_shares": 0,
        "disappeared_shares": 0,
        "changed_shares": 0,
        "added_items": 0,
        "removed_items": 0,
    }
    assert payload["new_shares"] == []
    assert payload["disappeared_shares"] == []
    assert payload["item_churn"] == []


def test_build_run_diff_reports_access_level_only_change_without_item_churn_counts() -> None:
    resource_key = ("10.0.0.10:445", "smb_share", "Finance")
    baseline = {
        resource_key: {
            "endpoint_key": "10.0.0.10:445",
            "hostname": "fs-01",
            "ip": "10.0.0.10",
            "share_name": "Finance",
            "share_type": "smb",
            "access_level": "list_only",
            "item_paths": {"\\Budget.xlsx"},
        }
    }
    current = {
        resource_key: {
            **baseline[resource_key],
            "access_level": "read_write",
        }
    }

    payload = runs_router._build_run_diff(current, baseline)

    assert payload["summary"] == {
        "new_shares": 0,
        "disappeared_shares": 0,
        "changed_shares": 1,
        "added_items": 0,
        "removed_items": 0,
    }
    assert payload["item_churn"] == [
        {
            "endpoint_key": "10.0.0.10:445",
            "hostname": "fs-01",
            "ip": "10.0.0.10",
            "share_name": "Finance",
            "share_type": "smb",
            "access_level": "read_write",
            "access_level_changed": True,
            "previous_access_level": "list_only",
            "added_items": 0,
            "removed_items": 0,
            "added_examples": [],
            "removed_examples": [],
        }
    ]


def test_build_run_diff_from_iters_matches_snapshot_builder() -> None:
    baseline_snapshot = {
        ("10.0.0.10:445", "smb_share", "Finance"): {
            "endpoint_key": "10.0.0.10:445",
            "hostname": "fs-01",
            "ip": "10.0.0.10",
            "share_name": "Finance",
            "share_type": "smb",
            "access_level": "read",
            "item_paths": {"\\Budget.xlsx", "\\Policies\\Readme.txt"},
        }
    }
    current_snapshot = {
        ("10.0.0.10:445", "smb_share", "Finance"): {
            "endpoint_key": "10.0.0.10:445",
            "hostname": "fs-01",
            "ip": "10.0.0.10",
            "share_name": "Finance",
            "share_type": "smb",
            "access_level": "read_write",
            "item_paths": {"\\Budget.xlsx", "\\Policies\\Q1.xlsx"},
        },
        ("10.0.0.12:445", "nfs_share", "/exports/backups"): {
            "endpoint_key": "10.0.0.12:445",
            "hostname": "nfs-01",
            "ip": "10.0.0.12",
            "share_name": "/exports/backups",
            "share_type": "nfs",
            "access_level": "read",
            "item_paths": {"\\nightly", "\\nightly\\backup-01.zip"},
        },
    }

    payload = runs_router._build_run_diff_from_iters(
        sorted(current_snapshot.items(), key=lambda item: runs_router._normalized_resource_key(item[0])),
        sorted(baseline_snapshot.items(), key=lambda item: runs_router._normalized_resource_key(item[0])),
        example_limit=3,
    )

    assert payload == runs_router._build_run_diff(current_snapshot, baseline_snapshot, example_limit=3)


def test_iter_run_diff_resources_groups_item_rows_per_share() -> None:
    run_id = uuid.uuid4()

    class _FakeDb:
        def execute(self, _stmt):
            return [
                SimpleNamespace(
                    endpoint_key="10.0.0.10:445",
                    hostname="fs-01",
                    ip="10.0.0.10",
                    name="Finance",
                    resource_type="smb_share",
                    access_level="readable",
                    path="\\Budget.xlsx",
                ),
                SimpleNamespace(
                    endpoint_key="10.0.0.10:445",
                    hostname="fs-01",
                    ip="10.0.0.10",
                    name="Finance",
                    resource_type="smb_share",
                    access_level="readable",
                    path="\\Policies\\Readme.txt",
                ),
                SimpleNamespace(
                    endpoint_key="10.0.0.11:445",
                    hostname="fs-02",
                    ip="10.0.0.11",
                    name="HR",
                    resource_type="smb_share",
                    access_level="list_only",
                    path=None,
                ),
            ]

    rows = list(runs_router._iter_run_diff_resources(_FakeDb(), run_id))

    assert rows == [
        (
            ("10.0.0.10:445", "smb_share", "Finance"),
            {
                "endpoint_key": "10.0.0.10:445",
                "hostname": "fs-01",
                "ip": "10.0.0.10",
                "share_name": "Finance",
                "resource_type": "smb_share",
                "share_type": "smb",
                "access_level": "readable",
                "item_paths": {"\\Budget.xlsx", "\\Policies\\Readme.txt"},
            },
        ),
        (
            ("10.0.0.11:445", "smb_share", "HR"),
            {
                "endpoint_key": "10.0.0.11:445",
                "hostname": "fs-02",
                "ip": "10.0.0.11",
                "share_name": "HR",
                "resource_type": "smb_share",
                "share_type": "smb",
                "access_level": "list_only",
                "item_paths": set(),
            },
        ),
    ]


def test_set_difference_summary_counts_all_items_but_bounds_examples() -> None:
    count, examples = runs_router._set_difference_summary(
        {"z.txt", "a.txt", "m.txt", "same.txt"},
        {"same.txt"},
        example_limit=2,
    )

    assert count == 3
    assert examples == ["a.txt", "m.txt"]


def test_run_diff_correlates_sharepoint_move_by_provider_item_id() -> None:
    resource_key = (
        "sharepoint:site-1",
        "sharepoint_library",
        "provider:drive-1",
    )
    baseline = {
        resource_key: {
            "endpoint_key": "sharepoint:site-1",
            "hostname": "contoso.sharepoint.com",
            "ip": None,
            "share_name": "Documents",
            "share_type": "sharepoint",
            "access_level": "list_only",
            "provider_resource_id": "drive-1",
            "item_paths": {"/Finance/Budget.xlsx"},
            "item_identities": {"provider:item-1": "/Finance/Budget.xlsx"},
        }
    }
    current = {
        resource_key: {
            "endpoint_key": "sharepoint:site-1",
            "hostname": "contoso.sharepoint.com",
            "ip": None,
            "share_name": "Renamed Documents",
            "share_type": "sharepoint",
            "access_level": "list_only",
            "provider_resource_id": "drive-1",
            "item_paths": {"/Public/FY26-Budget.xlsx"},
            "item_identities": {"provider:item-1": "/Public/FY26-Budget.xlsx"},
        }
    }

    payload = runs_router._build_run_diff(current, baseline)

    assert payload["summary"] == {
        "new_shares": 0,
        "disappeared_shares": 0,
        "changed_shares": 1,
        "added_items": 0,
        "removed_items": 0,
        "moved_items": 1,
    }
    assert payload["item_churn"][0]["share_name"] == "Renamed Documents"
    assert payload["item_churn"][0]["provider_resource_id"] == "drive-1"
    assert payload["item_churn"][0]["moved_examples"] == [
        {
            "provider_item_id": "item-1",
            "from_path": "/Finance/Budget.xlsx",
            "to_path": "/Public/FY26-Budget.xlsx",
        }
    ]


def test_run_diff_compatibility_rejects_missing_and_cross_tenant_context() -> None:
    run_without_context = SimpleNamespace(collection_context={})
    sharepoint_run = SimpleNamespace(
        collection_context={
            "source": "sharepoint",
            "provider": "sharepoint",
            "collection_mode": "tenant_inventory",
            "auth_mode": "app",
            "auth_type": "application",
            "tenant_id": "tenant-1",
            "client_id": "client-1",
            "roles": ["Sites.Read.All"],
            "discovery_completeness": "complete",
            "materialized_snapshot": True,
            "sync_mode": "full",
            "partial": False,
            "metadata": {
                "files_included": True,
                "discovery_strategy": "getAllSites",
                "discovery_authoritative": True,
            },
        }
    )

    unknown = runs_router._run_diff_compatibility(run_without_context, run_without_context)
    cross_tenant = runs_router._run_diff_compatibility(
        sharepoint_run,
        SimpleNamespace(
            collection_context={
                **sharepoint_run.collection_context,
                "tenant_id": "tenant-2",
            }
        ),
    )
    identical = runs_router._run_diff_compatibility(sharepoint_run, sharepoint_run)

    assert unknown["compatible"] is False
    assert "missing" in unknown["warning"].lower()
    assert cross_tenant["compatible"] is False
    assert "tenant_id" in cross_tenant["mismatched_fields"]
    assert identical == {"compatible": True, "warning": None, "mismatched_fields": []}


def test_run_diff_compatibility_rejects_incomplete_context_and_no_files_mismatch() -> None:
    complete_context = {
        "source": "sharepoint",
        "provider": "sharepoint",
        "collection_mode": "tenant_inventory",
        "auth_mode": "app",
        "auth_type": "application",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "roles": ["Sites.Read.All"],
        "discovery_completeness": "complete_for_granted_scope",
        "materialized_snapshot": True,
        "sync_mode": "full",
        "metadata": {
            "files_included": True,
            "discovery_strategy": "getAllSites",
            "discovery_authoritative": True,
        },
    }
    incomplete = runs_router._run_diff_compatibility(
        SimpleNamespace(collection_context={"source": "sharepoint"}),
        SimpleNamespace(collection_context=complete_context),
    )
    no_files = runs_router._run_diff_compatibility(
        SimpleNamespace(collection_context=complete_context),
        SimpleNamespace(
            collection_context={
                **complete_context,
                "metadata": {**complete_context["metadata"], "files_included": False},
            }
        ),
    )

    assert incomplete["compatible"] is False
    assert any(field.startswith("unknown:current.") for field in incomplete["mismatched_fields"])
    assert "unknown or missing" in incomplete["warning"].lower()
    assert no_files["compatible"] is False
    assert "metadata.files_included" in no_files["mismatched_fields"]


def test_run_diff_treats_full_to_delta_materialized_snapshots_as_compatible() -> None:
    full_context = {
        "source": "sharepoint",
        "provider": "sharepoint",
        "collection_mode": "tenant_inventory",
        "auth_mode": "app",
        "auth_type": "application",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "roles": ["Sites.Read.All"],
        "discovery_completeness": "complete_for_granted_scope",
        "materialized_snapshot": True,
        "sync_mode": "full",
        "metadata": {
            "files_included": True,
            "discovery_strategy": "getAllSites",
            "discovery_authoritative": True,
            "permissions_assessed": False,
        },
    }
    delta_context = {**full_context, "sync_mode": "delta"}

    result = runs_router._run_diff_compatibility(
        SimpleNamespace(collection_context=delta_context),
        SimpleNamespace(collection_context=full_context),
    )

    assert result == {"compatible": True, "warning": None, "mismatched_fields": []}


def test_run_diff_compares_effective_target_sites_not_order_or_safety_caps() -> None:
    base_context = {
        "source": "sharepoint",
        "provider": "sharepoint",
        "collection_mode": "tenant_inventory",
        "auth_mode": "app",
        "auth_type": "application",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "roles": ["Sites.Read.All"],
        "discovery_completeness": "targeted_scope",
        "materialized_snapshot": True,
        "sync_mode": "full",
        "metadata": {
            "files_included": True,
            "discovery_strategy": "targeted",
            "discovery_authoritative": False,
            "permissions_assessed": False,
        },
    }

    def context(sites: list[str], *, max_sites: int) -> dict:
        return {
            **base_context,
            "metadata": {
                **base_context["metadata"],
                "collection": {
                    "target_scope": {
                        "provider": "sharepoint",
                        "targeted_sites": sites,
                        "max_sites": max_sites,
                        "max_libraries": max_sites * 2,
                        "max_items": max_sites * 100,
                    }
                },
            },
        }

    current = context(
        [" SITE-B ", "https://Contoso.SharePoint.com/sites/Finance"],
        max_sites=10,
    )
    baseline = context(
        ["https://contoso.sharepoint.com/sites/finance/", "site-b/"],
        max_sites=100,
    )

    compatible = runs_router._run_diff_compatibility(
        SimpleNamespace(collection_context=current),
        SimpleNamespace(collection_context=baseline),
    )
    different_target = runs_router._run_diff_compatibility(
        SimpleNamespace(collection_context=current),
        SimpleNamespace(collection_context=context(["site-c"], max_sites=10)),
    )

    assert compatible == {"compatible": True, "warning": None, "mismatched_fields": []}
    assert different_target["compatible"] is False
    assert "metadata.collection.target_scope" in different_target["mismatched_fields"]


def test_run_diff_rejects_same_attribution_opaque_token_permissions_as_unverifiable() -> None:
    opaque_context = {
        "source": "sharepoint",
        "provider": "sharepoint",
        "collection_mode": "delegated_user_view",
        "auth_mode": "token",
        "auth_type": "delegated",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "assessed_identity": "alice@example.com",
        "scopes": [],
        "roles": [],
        "jwt_inspection": "opaque_token_context_supplied_by_operator",
        "discovery_completeness": "security_trimmed",
        "materialized_snapshot": True,
        "sync_mode": "full",
        "metadata": {
            "files_included": True,
            "discovery_strategy": "site-search",
            "discovery_authoritative": False,
            "permissions_assessed": False,
        },
    }

    result = runs_router._run_diff_compatibility(
        SimpleNamespace(collection_context=opaque_context),
        SimpleNamespace(collection_context=opaque_context),
    )

    assert result["compatible"] is False
    assert "permissions cannot be verified" in result["warning"].lower()
    assert "unknown:current.permissions" in result["mismatched_fields"]
    assert "unknown:baseline.permissions" in result["mismatched_fields"]


def test_run_diff_preserves_case_sensitive_provider_resource_identity() -> None:
    def record(provider_id: str) -> tuple[tuple[str, str, str], dict]:
        return (
            (
                "sharepoint:site-1",
                "sharepoint_library",
                f"provider:{provider_id}",
            ),
            {
                "endpoint_key": "sharepoint:site-1",
                "hostname": "contoso.sharepoint.com",
                "ip": None,
                "share_name": "Documents",
                "share_type": "sharepoint",
                "access_level": "list_only",
                "provider_resource_id": provider_id,
                "item_paths": set(),
                "item_identities": {},
            },
        )

    current = [record("Drive-A")]
    baseline = [record("drive-a")]
    payload = runs_router._build_run_diff_from_iters(current, baseline)

    assert payload["summary"]["new_shares"] == 1
    assert payload["summary"]["disappeared_shares"] == 1
    assert payload["summary"]["changed_shares"] == 0


def test_run_diff_bounds_each_detail_section_without_losing_summary_counts() -> None:
    current = []
    baseline = []
    for index in range(3):
        current.append(
            (
                (f"new-{index}", "smb_share", "Share"),
                {
                    "endpoint_key": f"new-{index}",
                    "hostname": None,
                    "ip": None,
                    "share_name": "Share",
                    "share_type": "smb",
                    "access_level": "readable",
                    "item_paths": {f"new-{index}.txt"},
                },
            )
        )
        baseline.append(
            (
                (f"old-{index}", "smb_share", "Share"),
                {
                    "endpoint_key": f"old-{index}",
                    "hostname": None,
                    "ip": None,
                    "share_name": "Share",
                    "share_type": "smb",
                    "access_level": "readable",
                    "item_paths": {f"old-{index}.txt"},
                },
            )
        )
        current.append(
            (
                (f"same-{index}", "smb_share", "Share"),
                {
                    "endpoint_key": f"same-{index}",
                    "hostname": None,
                    "ip": None,
                    "share_name": "Share",
                    "share_type": "smb",
                    "access_level": "readable",
                    "item_paths": {f"added-{index}.txt"},
                },
            )
        )
        baseline.append(
            (
                (f"same-{index}", "smb_share", "Share"),
                {
                    "endpoint_key": f"same-{index}",
                    "hostname": None,
                    "ip": None,
                    "share_name": "Share",
                    "share_type": "smb",
                    "access_level": "readable",
                    "item_paths": {f"removed-{index}.txt"},
                },
            )
        )

    payload = runs_router._build_run_diff_from_iters(
        sorted(current, key=lambda entry: runs_router._normalized_resource_key(entry[0])),
        sorted(baseline, key=lambda entry: runs_router._normalized_resource_key(entry[0])),
        detail_limit=1,
    )

    assert payload["summary"] == {
        "new_shares": 3,
        "disappeared_shares": 3,
        "changed_shares": 3,
        "added_items": 3,
        "removed_items": 3,
    }
    assert len(payload["new_shares"]) == 1
    assert len(payload["disappeared_shares"]) == 1
    assert len(payload["item_churn"]) == 1
    assert payload["truncation"] == {
        "detail_limit": 1,
        "truncated": True,
        "sections": {
            "new_shares": True,
            "disappeared_shares": True,
            "item_churn": True,
        },
    }


def test_run_diff_rejects_work_above_synchronous_memory_envelope() -> None:
    current = SimpleNamespace(id=uuid.uuid4(), summary={"items": 0})
    baseline = SimpleNamespace(id=uuid.uuid4(), summary={"items": 0})

    class _CountResult:
        def __init__(self, count: int):
            self.count = count

        def scalar(self):
            return self.count

    class _CountDb:
        def __init__(self):
            self.counts = iter((200_000, 50_001))

        def execute(self, _statement):
            return _CountResult(next(self.counts))

    with pytest.raises(Exception) as exc_info:
        runs_router._enforce_run_diff_item_limit(_CountDb(), current, baseline)

    assert getattr(exc_info.value, "status_code", None) == 422
    assert "250001 items" in exc_info.value.detail
    assert "250000" in exc_info.value.detail


@pytest.mark.parametrize(
    ("label", "run_status"),
    [
        ("current", runs_router.RunStatus.INGESTING),
        ("baseline", runs_router.RunStatus.FAILED),
    ],
)
def test_run_diff_rejects_partial_or_failed_runs(label: str, run_status) -> None:
    run = SimpleNamespace(status=run_status)

    with pytest.raises(Exception) as exc_info:
        runs_router._require_complete_run_for_diff(run, label)

    assert getattr(exc_info.value, "status_code", None) == 409
    assert f"COMPLETE {label} run" in exc_info.value.detail
    assert run_status.value in exc_info.value.detail


def test_run_diff_accepts_completed_runs() -> None:
    runs_router._require_complete_run_for_diff(
        SimpleNamespace(status=runs_router.RunStatus.COMPLETE),
        "current",
    )
