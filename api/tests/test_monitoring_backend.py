import importlib.util
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.deps import AuthContext
from app.models import CollectionSource, ComparisonItemChange, Finding, FindingOccurrence, RunComparison, ScanRun
from app.routers import comparisons, monitoring, runs
from app.schemas import CollectionSourceUpdateIn, FindingBulkUpdateIn, RunCreateIn
from app.services import monitoring as monitoring_service
from fastapi import HTTPException


def _load_migration(filename: str, module_name: str):
    path = Path(__file__).parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_monitoring_model_metadata_matches_online_migrations():
    assert "ix_scan_runs_source_created_id" in {index.name for index in ScanRun.__table__.indexes}
    assert "ix_run_comparisons_source_created_id" in {
        index.name for index in RunComparison.__table__.indexes
    }
    assert "ix_comparison_item_changes_search_trgm" in {
        index.name for index in ComparisonItemChange.__table__.indexes
    }
    assert "ix_findings_project_status_updated_id" in {index.name for index in Finding.__table__.indexes}
    assert "ix_findings_status_risk_expiry_id" in {index.name for index in Finding.__table__.indexes}
    assert "ix_findings_search_trgm" in {index.name for index in Finding.__table__.indexes}
    from app.models import Item, Resource

    assert "ix_resources_run_unkeyed_id" in {index.name for index in Resource.__table__.indexes}
    assert "ix_items_run_unkeyed_id" in {index.name for index in Item.__table__.indexes}
    assert "ix_items_active_resource_identity_id" in {index.name for index in Item.__table__.indexes}
    source_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in CollectionSource.__table__.constraints
        if hasattr(constraint, "sqltext")
    }
    assert "31536000" in source_checks["ck_collection_sources_expected_interval"]
    finding_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Finding.__table__.constraints
        if hasattr(constraint, "sqltext")
    }
    assert "accepted_risk_expires_at IS NOT NULL" in finding_checks["ck_findings_accepted_risk_expiry"]
    assert {
        "ck_findings_severity",
        "ck_findings_status",
        "ck_findings_counts",
        "ck_findings_policy_version",
    }.issubset(finding_checks)
    item_checks = {
        constraint.name
        for constraint in ComparisonItemChange.__table__.constraints
        if hasattr(constraint, "sqltext")
    }
    assert {"ck_comparison_item_changes_type", "ck_comparison_item_changes_evidence_state"}.issubset(
        item_checks
    )


def test_monitoring_migration_is_online_safe_and_downgrade_breaks_circular_fk_first():
    migration = _load_migration("0014_monitoring_findings.py", "monitoring_0014")
    online = _load_migration("0015_monitoring_online_indexes.py", "monitoring_0015")
    source = inspect.getsource(migration)
    downgrade = inspect.getsource(migration.downgrade)

    assert "NOT VALID" in source
    assert "ix_scan_runs_source_created_id" not in source
    assert "ix_run_comparisons_source_created_id" not in source
    assert "ix_comparison_item_changes_search_trgm" in source
    assert "ix_findings_search_trgm" in source
    assert "ix_findings_status_risk_expiry_id" in source
    assert "CHECK (trigger IN ('manual', 'automatic')) NOT VALID" in source
    assert "postgresql_using=\"gin\"" in source
    assert downgrade.index('drop_constraint("fk_scan_runs_source_id"') < downgrade.index(
        'drop_table("collection_sources")'
    )

    statements = dict(online.INDEXES)
    assert "CREATE INDEX CONCURRENTLY" in statements["ix_scan_runs_source_created_id"]
    assert "CREATE INDEX CONCURRENTLY" in statements["ix_run_comparisons_source_created_id"]
    assert "WHERE identity_key IS NULL" in statements["ix_resources_run_unkeyed_id"]
    assert "WHERE identity_key IS NULL" in statements["ix_items_run_unkeyed_id"]
    assert "WHERE deleted IS FALSE" in statements["ix_items_active_resource_identity_id"]
    assert ("scan_runs", "fk_scan_runs_source_id") in online.FOREIGN_KEYS
    assert ("run_comparisons", "fk_run_comparisons_source_id") in online.FOREIGN_KEYS
    assert ("run_comparisons", "ck_run_comparisons_trigger") in online.CHECK_CONSTRAINTS


def _finding(status="open", expiry=None):
    return SimpleNamespace(
        status=status,
        assignee_user_id=None,
        accepted_risk_expires_at=expiry,
        resolved_at=None,
        updated_at=None,
        revision=3,
    )


def test_finding_lifecycle_rejects_null_status_and_expiry_on_non_risk_state():
    with pytest.raises(HTTPException, match="status cannot be null"):
        monitoring._apply_finding_update(
            _finding(),
            fields_set={"status"},
            new_status=None,
            assignee_user_id=None,
            accepted_risk_expires_at=None,
        )
    with pytest.raises(HTTPException, match="expiry can only be updated"):
        monitoring._apply_finding_update(
            _finding(),
            fields_set={"accepted_risk_expires_at"},
            new_status=None,
            assignee_user_id=None,
            accepted_risk_expires_at=datetime.now(tz=UTC) + timedelta(days=1),
        )


def test_finding_lifecycle_requires_future_expiry_and_clears_it_when_resolved():
    expires = datetime.now(tz=UTC) + timedelta(days=1)
    finding = _finding(status="open")
    monitoring._apply_finding_update(
        finding,
        fields_set={"status", "accepted_risk_expires_at"},
        new_status="accepted_risk",
        assignee_user_id=None,
        accepted_risk_expires_at=expires,
    )
    assert finding.status == "accepted_risk"
    assert finding.accepted_risk_expires_at == expires
    monitoring._apply_finding_update(
        finding,
        fields_set={"status"},
        new_status="resolved",
        assignee_user_id=None,
        accepted_risk_expires_at=None,
    )
    assert finding.status == "resolved"
    assert finding.accepted_risk_expires_at is None
    assert finding.resolved_at is not None


def test_finding_assignment_does_not_retimestamp_existing_resolution():
    resolved_at = datetime.now(tz=UTC) - timedelta(days=2)
    finding = _finding(status="resolved")
    finding.resolved_at = resolved_at
    monitoring._apply_finding_update(
        finding,
        fields_set={"assignee_user_id"},
        new_status=None,
        assignee_user_id=uuid.uuid4(),
        accepted_risk_expires_at=None,
    )
    assert finding.resolved_at == resolved_at


def test_finding_noop_detection_rejects_same_assignment_and_same_expiry():
    finding = _finding()
    assignee = uuid.uuid4()
    finding.assignee_user_id = assignee
    assert monitoring._finding_update_would_change(
        finding,
        fields_set={"assignee_user_id"},
        new_status=None,
        assignee_user_id=assignee,
        accepted_risk_expires_at=None,
    ) is False
    expiry = datetime.now(tz=UTC) + timedelta(days=2)
    risk = _finding(status="accepted_risk", expiry=expiry)
    assert monitoring._finding_update_would_change(
        risk,
        fields_set={"status", "accepted_risk_expires_at"},
        new_status="accepted_risk",
        assignee_user_id=None,
        accepted_risk_expires_at=expiry,
    ) is False


def test_bulk_lifecycle_writes_indexed_per_finding_audits():
    source = inspect.getsource(monitoring.bulk_update_findings)
    assert 'object_type="finding"' in source
    assert 'action="FINDING_BULK_UPDATED"' in source
    assert '"batch_action_id": batch_action_id' in source
    assert '"old_accepted_risk_expires_at"' in source
    assert '"new_accepted_risk_expires_at"' in source
    assert "_finding_update_would_change" in source
    assert "if not any(state_changes.values()) and not has_note" in source
    assert 'mutable_fields == {"note"}' in source
    assert "max_length=100" not in source  # bounded in schema by max list length, not a query string


def test_bulk_lifecycle_requires_exact_optimistic_revisions():
    first, second = uuid.uuid4(), uuid.uuid4()
    payload = FindingBulkUpdateIn(
        finding_ids=[first, second],
        expected_revisions={first: 1, second: 4},
        status="acknowledged",
    )
    assert payload.expected_revisions == {first: 1, second: 4}
    with pytest.raises(ValueError, match="exactly one revision"):
        FindingBulkUpdateIn(
            finding_ids=[first, second],
            expected_revisions={first: 1},
            status="acknowledged",
        )
    source = inspect.getsource(monitoring.bulk_update_findings)
    assert "FINDING_BULK_REVISION_CONFLICT" in source
    assert ".with_for_update()" in source


def test_effective_access_response_is_bounded_and_never_page_aggregated_as_global():
    source = inspect.getsource(runs.resource_effective_access)
    assert runs.EFFECTIVE_ACCESS_ENTRY_RESPONSE_MAX == 1000
    assert runs.EFFECTIVE_ACCESS_ENTRY_PRINCIPAL_MAX == 100
    assert runs.EFFECTIVE_ACCESS_ASSESSMENT_MAX == 100
    assert ".limit(EFFECTIVE_ACCESS_ASSESSMENT_MAX + 1)" in source
    assert ".limit(EFFECTIVE_ACCESS_ENTRY_RESPONSE_MAX + 1)" in source
    assert "PermissionAssessment.item_id.is_(None)" in source
    assert "func.bool_or" in source
    assert "known_expired_at_observation" in source
    assert "expiration_indeterminate" in source
    assert '"expired_entry_count"' in source
    assert "run.created_at" in source
    assert 'page_decision_scope": "returned_principals_only"' in source
    assert 'decision = "unknown"' in source
    assert '"subject_scope": "assessed_identity_only"' in source
    assert '"state": "provider_computed" if provider_computed_decision != "unknown" else "not_computed"' in source
    assert "_provider_computed_effective_decision" in source
    assert '"state": "provider_computed"' in source
    assert '"decision_scope": "single_declared_assessment_subject"' in source


def test_provider_computed_effective_access_requires_complete_unambiguous_evidence():
    complete = SimpleNamespace(
        effective_access_status="allowed",
        assessment_state="complete",
        retrieval_coverage="complete",
        semantic_coverage="effective_access",
        principal_resolution="complete",
        entries_omitted=0,
        unknown_entries=0,
        negative_conclusion_supported=False,
    )
    assert runs._provider_computed_effective_decision(
        [complete], assessments_truncated=False, principal_filtered=False
    ) == ("allow", complete)
    for retrieval_coverage in ("full", "all_returned"):
        alias = SimpleNamespace(**{**vars(complete), "retrieval_coverage": retrieval_coverage})
        assert runs._provider_computed_effective_decision(
            [alias], assessments_truncated=False, principal_filtered=False
        ) == ("allow", alias)
    denied_without_negative_support = SimpleNamespace(**{**vars(complete), "effective_access_status": "denied"})
    assert runs._provider_computed_effective_decision(
        [denied_without_negative_support], assessments_truncated=False, principal_filtered=False
    ) == ("unknown", None)
    supported_deny = SimpleNamespace(
        **{
            **vars(denied_without_negative_support),
            "negative_conclusion_supported": True,
        }
    )
    assert runs._provider_computed_effective_decision(
        [supported_deny], assessments_truncated=False, principal_filtered=False
    ) == ("deny", supported_deny)
    incomplete = SimpleNamespace(**{**vars(complete), "retrieval_coverage": "partial"})
    for assessments, truncated, filtered in (
        ([incomplete], False, False),
        ([complete], True, False),
        ([complete], False, True),
        ([complete, complete], False, False),
    ):
        assert runs._provider_computed_effective_decision(
            assessments,
            assessments_truncated=truncated,
            principal_filtered=filtered,
        ) == ("unknown", None)


def test_retry_is_operator_authorized_idempotent_and_resets_only_failed_work():
    source = inspect.getsource(comparisons.retry_comparison)
    assert "ProjectRole.OPERATOR" in source
    assert '.with_for_update()' in source
    assert '"comparison_create"' in source
    assert "pg_advisory_xact_lock" in source
    assert "api_comparison_max_active_per_project" in source
    assert '"COMPARISON_CAPACITY_REACHED"' in source
    assert 'headers={"Retry-After": "5"}' in source
    assert 'if comparison.state in {"queued", "running"}' in source
    assert 'if comparison.state == "complete"' in source
    assert 'if comparison.state != "failed"' in source
    assert '"operator_retry": True' in source
    assert 'action="COMPARISON_RETRY_REQUESTED"' in source
    assert source.index("pg_advisory_xact_lock") < source.index('comparison.state = "queued"')
    assert source.index("active_count") < source.index('comparison.state = "queued"')
    assert source.index("lock_worker_job") < source.index("lock_monitoring_source") < source.index(
        "pg_advisory_xact_lock"
    )


def test_source_provider_filter_matches_single_components_and_exact_compounds():
    single = str(monitoring._source_provider_filter(" SMB "))
    assert "collection_sources.provider =" in single
    assert single.count("collection_sources.provider LIKE") == 3

    compound = str(monitoring._source_provider_filter("nfs+smb"))
    assert "collection_sources.provider =" in compound
    assert "LIKE" not in compound

    with pytest.raises(HTTPException) as caught:
        monitoring._source_provider_filter("smb%")
    assert caught.value.status_code == 400


@pytest.mark.parametrize(
    ("current_interval", "requested_interval"),
    ((3600, 7200), (3600, None), (None, 3600)),
)
def test_source_update_applies_set_change_and_clear_cadence(
    monkeypatch, current_interval, requested_interval
):
    project_id = uuid.uuid4()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        display_name="Finance",
        enabled=True,
        expected_interval_seconds=current_interval,
        updated_at=datetime.now(tz=UTC),
    )
    db = SimpleNamespace(commit=lambda: None, refresh=lambda _row: None)
    monkeypatch.setattr(monitoring, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(monitoring, "_get_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(monitoring, "request_meta", lambda _request: {})
    monkeypatch.setattr(monitoring, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        monitoring,
        "source_payload",
        lambda row: {"expected_interval_seconds": row.expected_interval_seconds},
    )

    result = monitoring.update_source(
        project_id,
        source.id,
        CollectionSourceUpdateIn(
            expected_display_name="Finance",
            expected_enabled=True,
            expected_current_interval_seconds=current_interval,
            expected_interval_seconds=requested_interval,
        ),
        SimpleNamespace(),
        db,
        AuthContext(None, None, None, None, None),
        AuthContext(uuid.uuid4(), None, None, None, None),
    )

    assert result["expected_interval_seconds"] == requested_interval


def test_source_update_combines_fields_and_rejects_stale_configuration(monkeypatch):
    project_id = uuid.uuid4()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        display_name="Finance",
        enabled=True,
        expected_interval_seconds=3600,
        updated_at=datetime.now(tz=UTC),
    )
    db = SimpleNamespace(commit=lambda: None, refresh=lambda _row: None)
    monkeypatch.setattr(monitoring, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(monitoring, "_get_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(monitoring, "request_meta", lambda _request: {})
    monkeypatch.setattr(monitoring, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        monitoring,
        "source_payload",
        lambda row: {
            "display_name": row.display_name,
            "enabled": row.enabled,
            "expected_interval_seconds": row.expected_interval_seconds,
        },
    )
    auth = AuthContext(uuid.uuid4(), None, None, None, None)

    updated = monitoring.update_source(
        project_id,
        source.id,
        CollectionSourceUpdateIn(
            expected_display_name="Finance",
            expected_enabled=True,
            expected_current_interval_seconds=3600,
            display_name="Finance production",
            enabled=False,
            expected_interval_seconds=7200,
        ),
        SimpleNamespace(),
        db,
        auth,
        auth,
    )
    assert updated == {
        "display_name": "Finance production",
        "enabled": False,
        "expected_interval_seconds": 7200,
    }

    with pytest.raises(HTTPException) as caught:
        monitoring.update_source(
            project_id,
            source.id,
            CollectionSourceUpdateIn(
                expected_display_name="Finance",
                expected_enabled=True,
                expected_current_interval_seconds=3600,
                display_name="Stale write",
            ),
            SimpleNamespace(),
            db,
            auth,
            auth,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "SOURCE_REVISION_CONFLICT"


def test_resource_change_contract_uses_materialized_item_history_counts():
    source = inspect.getsource(comparisons.list_resource_changes)
    assert "item_counts_by_resource" in source
    assert '"state": "computed"' in source
    assert '"state": "not_computed"' in source
    assert "item_churn_computed" in source
    assert 'action="COMPARISON_RESOURCE_CHANGES_LISTED"' in source
    item_source = inspect.getsource(comparisons.list_item_changes)
    assert '"query_applied"' in item_source
    assert '"cursor_applied"' in item_source
    assert '"has_next_page"' in item_source


def test_monitoring_routes_are_bounded_and_expose_assignee_candidates():
    paths = {route.path for route in monitoring.router.routes}
    assert "/projects/{project_id}/findings/assignee-candidates" in paths
    assert "/projects/{project_id}/findings/bulk" in paths
    assert "/projects/{project_id}/sources" in paths
    assert "/projects/{project_id}/runs/{run_id}/monitoring/retry" in paths
    assert "/projects/{project_id}/comparisons/{comparison_id}/findings/retry" in paths
    assignee_source = inspect.getsource(monitoring.list_finding_assignee_candidates)
    assert "le=100" in assignee_source
    assert "ProjectRole.OPERATOR" in assignee_source


def test_source_health_filter_normalizes_coverage_like_payload_health():
    source = inspect.getsource(monitoring._source_health_filter)
    assert "func.lower(func.coalesce" in source
    assert '"findings_evaluation_state"' in source
    assert '"unknown"' in source


def test_source_health_fails_closed_when_baseline_finding_state_is_missing():
    now = datetime.now(tz=UTC)
    source = SimpleNamespace(
        enabled=True,
        last_success_at=now,
        last_failure_at=None,
        expected_interval_seconds=3600,
        coverage={
            "state": "complete",
            "monitoring_findings": {"state": "complete"},
            "automatic_baseline": {"state": "established"},
        },
    )

    health = monitoring_service.source_health(source, now=now)

    assert health["health_status"] == "degraded"


def test_finding_history_cursor_and_search_match_new_indexes():
    assert [column.key for column in monitoring.OCCURRENCE_CURSOR] == ["observed_at", "id"]
    source = inspect.getsource(monitoring._finding_filters)
    assert "Finding.search_text.ilike" in source
    assert "Finding.description.ilike" not in source


def test_finding_evidence_reads_are_audited_without_auditing_list_polls():
    assert 'action="FINDING_EVIDENCE_VIEWED"' in inspect.getsource(monitoring.get_finding)
    assert 'action="FINDING_OCCURRENCES_LISTED"' in inspect.getsource(monitoring.list_finding_occurrences)
    assert 'action="FINDING_ACTIVITY_LISTED"' in inspect.getsource(monitoring.list_finding_activity)
    activity_source = inspect.getsource(monitoring.list_finding_activity)
    assert "FINDING_ACTIVITY_ACTIONS" in activity_source
    assert "FINDING_ACTIVITY_LISTED" not in monitoring.FINDING_ACTIVITY_ACTIONS
    assert "write_audit_event" not in inspect.getsource(monitoring.list_findings)


def test_finding_queue_payload_withholds_detail_until_audited_read():
    now = datetime.now(tz=UTC)
    finding = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_id=None,
        policy_id="policy-a",
        policy_version=1,
        title="Finding",
        description="Description",
        severity="high",
        status="open",
        resource_identity_key="resource-a",
        resource_type="smb_share",
        provider="smb",
        resource_name="Finance",
        first_seen_at=now,
        last_seen_at=now,
        resolved_at=None,
        accepted_risk_expires_at=None,
        assignee_user_id=None,
        latest_run_id=None,
        latest_comparison_id=None,
        evidence={"state": "exact", "summary": {"sensitive_path": "/finance/private"}},
        occurrence_count=1,
        revision=1,
        created_at=now,
        updated_at=now,
    )

    queue_payload = monitoring_service.finding_payload(finding, include_evidence=False)
    detail_payload = monitoring_service.finding_payload(finding)

    assert queue_payload["evidence"] == {"state": "exact"}
    assert detail_payload["evidence"]["summary"]["sensitive_path"] == "/finance/private"


def test_finding_activity_projection_withholds_audit_only_request_context():
    assert "FINDING_AUTO_REOPENED" in monitoring.FINDING_ACTIVITY_ACTIONS
    projected = monitoring._public_finding_activity_metadata(
        {
            "old_status": "open",
            "new_status": "acknowledged",
            "note": "Reviewed with the owner",
            "reason": "authoritative_recurrence",
            "ip": "192.0.2.10",
            "user_agent": "sensitive workstation fingerprint",
            "request_id": "internal-correlation-id",
            "future_internal_field": {"secret": "must not escape"},
        }
    )

    assert projected == {
        "old_status": "open",
        "new_status": "acknowledged",
        "note": "Reviewed with the owner",
        "reason": "authoritative_recurrence",
    }


def test_finding_activity_labels_api_token_without_exposing_token_or_owner_ids():
    actor = monitoring._public_finding_activity_actor(
        SimpleNamespace(actor_user_id=uuid.uuid4(), actor_token_id=uuid.uuid4())
    )

    assert actor == {"actor_user_id": None, "actor_token_id": None, "actor_kind": "api_token"}

    retained_actor = monitoring._public_finding_activity_actor(
        SimpleNamespace(
            actor_user_id=None,
            actor_user_ref=uuid.uuid4(),
            actor_token_id=None,
            actor_token_ref=uuid.uuid4(),
        )
    )
    assert retained_actor == {"actor_user_id": None, "actor_token_id": None, "actor_kind": "api_token"}


def test_finding_occurrence_retains_snapshot_when_run_is_deleted():
    run_column = FindingOccurrence.__table__.c.run_id
    assert run_column.nullable is True
    foreign_key = next(iter(run_column.foreign_keys))
    assert foreign_key.ondelete == "SET NULL"
    route_source = inspect.getsource(monitoring.list_finding_occurrences)
    assert 'str(row.run_id) if row.run_id else None' in route_source


def test_run_create_scope_and_description_are_bounded():
    assert RunCreateIn(name="bounded", target_scope={"hosts": ["a", "b"]}).target_scope["hosts"] == ["a", "b"]
    with pytest.raises(ValueError, match="lists must not exceed 512"):
        RunCreateIn(name="too-many", target_scope={"hosts": [str(i) for i in range(513)]})
    with pytest.raises(ValueError, match="64 KiB"):
        RunCreateIn(name="too-large", target_scope={"value": "x" * 66_000})
    with pytest.raises(ValueError, match="JSON values"):
        RunCreateIn(name="non-finite", target_scope={"value": float("nan")})
    for secret_scope in (
        {"clientSecret": "secret"},
        {"nested": {"auth-token": "secret"}},
        {"sites": [{"db.password.backup": "secret"}]},
    ):
        with pytest.raises(ValueError, match="credentials or secret-labeled fields"):
            RunCreateIn(name="credential-bearing", target_scope=secret_scope)
    with pytest.raises(ValueError):
        RunCreateIn(name="description", description="x" * 4001)


def test_monitoring_retry_contract_is_rate_limited_and_supersession_safe():
    run_retry = inspect.getsource(monitoring.retry_run_monitoring_evaluation)
    comparison_retry = inspect.getsource(monitoring.retry_comparison_finding_evaluation)
    assert "MONITORING_RUN_SUPERSEDED" in run_retry
    assert "MONITORING_SOURCE_DISABLED" in run_retry
    assert "monitoring_evaluation_retry" in run_retry
    assert "comparison_findings_retry" in comparison_retry
    assert "MONITORING_RETRY_RATE_LIMITED" in run_retry
