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
