import inspect
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import main


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def complete_context(provider="smb"):
    return {
        "provider": provider,
        "source": provider,
        "collection_mode": "inventory",
        "auth_mode": "kerberos",
        "assessed_identity": "EXAMPLE\\analyst",
        "materialized_snapshot": True,
        "discovery_completeness": "complete",
        "partial": False,
        "tool_version": "1.2.0",
        "metadata": {
            "structural_complete": True,
            "files_included": True,
            "content_complete": True,
            "permissions_assessed": True,
            "permissions_complete": True,
            "comparison_contracts": {"capability": "smb_nonmutating_capability_v1"},
            "collection": {"target_scope": {"hosts": ["files.example.test"]}},
        },
    }


def test_source_failure_preserves_prior_success_and_records_failure(monkeypatch):
    class Conn:
        def __init__(self):
            self.last_success = None
            self.last_failure = None
            self.source_id = "11111111-1111-1111-1111-111111111111"

        def execute(self, query, params=None):
            if "SELECT name, target_scope, collection_context" in query:
                return Result(("Nightly", {}, complete_context(), datetime(2026, 1, 1, tzinfo=UTC)))
            if "INSERT INTO collection_sources" in query:
                succeeded = params[8]
                if succeeded:
                    self.last_success = "set"
                else:
                    self.last_failure = "set"
                return Result((self.source_id,))
            if "UPDATE scan_runs SET source_id" in query:
                return Result()
            if "UPDATE collection_sources" in query:
                return Result()
            raise AssertionError(query)

    conn = Conn()
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    assert main.register_collection_source(conn, "run-1", "project-1", succeeded=True) == conn.source_id
    assert main.register_collection_source(conn, "run-2", "project-1", succeeded=False) == conn.source_id
    assert conn.last_success == "set"
    assert conn.last_failure == "set"


def test_source_registration_refuses_to_guess_without_context(monkeypatch):
    class Conn:
        def execute(self, query, params=None):
            assert "SELECT name, target_scope, collection_context" in query
            return Result(("Unattributed", {}, {}, datetime(2026, 1, 1, tzinfo=UTC)))

    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    assert main.register_collection_source(Conn(), "run-1", "project-1") is None


def test_disabled_source_gate_is_explicit():
    class Conn:
        def __init__(self, enabled):
            self.enabled = enabled

        def execute(self, query, params=None):
            assert "SELECT enabled FROM collection_sources" in query
            return Result((self.enabled,))

    assert main.collection_source_automation_enabled(Conn(True), "source") is True
    assert main.collection_source_automation_enabled(Conn(False), "source") is False


def test_disabled_source_only_skips_automatic_comparisons_not_explicit_manual_analysis():
    source = inspect.getsource(main.process_comparison_job)
    assert 'str(comparison_trigger) == "automatic"' in source
    assert "not collection_source_automation_enabled(conn, source_id)" in source
    assert "evaluate_comparison_findings(" in source


def test_smb_findings_are_not_resolved_without_authoritative_structure(monkeypatch):
    context = complete_context()
    context["metadata"]["structural_complete"] = False

    class Conn:
        def execute(self, query, params=None):
            if "SELECT collection_context, ingest_progress FROM scan_runs" in query:
                return Result((context, {}))
            if "SELECT resource.id" in query:
                return Result(rows=[])
            if "SELECT COUNT(*), COALESCE" in query and "FROM resources" in query:
                return Result((1, True))
            if "UPDATE scan_runs" in query:
                return Result()
            raise AssertionError(query)

        def commit(self):
            return None

    resolved = []
    monkeypatch.setattr(main, "_upsert_finding", lambda *_args, **_kwargs: ("id", False, "dedupe"))
    monkeypatch.setattr(
        main,
        "_resolve_absent_state_findings",
        lambda *_args, **kwargs: (resolved.append(kwargs["policy_id"]) or 1, False),
    )
    assert main.evaluate_run_findings(
        Conn(), project_id="project", source_id="source", run_id="run"
    ) == {"observed": 0, "resolved": 0}
    assert resolved == []


def test_authoritative_empty_smb_snapshot_resolves_prior_write_findings(monkeypatch):
    context = complete_context()

    class Conn:
        def execute(self, query, params=None):
            if "SELECT collection_context, ingest_progress FROM scan_runs" in query:
                return Result((context, {}))
            if "SELECT resource.id" in query:
                return Result(rows=[])
            if "SELECT COUNT(*), COALESCE" in query:
                return Result((0, True))
            if "UPDATE scan_runs" in query:
                return Result()
            raise AssertionError(query)

        def commit(self):
            return None

    resolved = []
    monkeypatch.setattr(
        main,
        "_resolve_absent_state_findings",
        lambda *_args, **kwargs: (resolved.append(kwargs["policy_id"]) or 0, False),
    )
    assert main.evaluate_run_findings(
        Conn(), project_id="project", source_id="source", run_id="run"
    ) == {"observed": 0, "resolved": 0}
    assert resolved == ["smb.write_observed"]


def test_sharepoint_findings_require_structural_and_permission_completeness_to_resolve():
    source = inspect.getsource(main.evaluate_run_findings)
    assert (
        '"sharepoint" in context_providers and structural_scope_complete and permission_scope_complete'
        in source
    )


def test_finding_resolution_clears_risk_expiry_and_is_bounded_to_source():
    captured = {}

    class Conn:
        def execute(self, query, params=None):
            captured["query"] = " ".join(query.split())
            captured["params"] = params
            return Result(rows=[])

    assert main._resolve_absent_state_findings(
        Conn(),
        project_id="project",
        source_id="source",
        policy_id="smb.write_observed",
        run_id="run",
    ) == (0, False)
    assert "accepted_risk_expires_at = NULL" in captured["query"]
    assert "source_id = %s" in captured["query"]
    assert "FOR UPDATE SKIP LOCKED" in captured["query"]
    assert "finding_occurrences" in captured["query"]
    assert captured["params"][-1] == main.FINDING_RESOLUTION_BATCH_SIZE


def test_expired_risk_sweep_reopens_and_audits_atomically(monkeypatch):
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            assert "FOR UPDATE SKIP LOCKED" in query
            assert "accepted_risk_expires_at = NULL" in query
            return Result(rows=[("finding-1", "project-1")])

    audits = []
    monkeypatch.setattr(main, "connect_database", lambda: Conn())
    monkeypatch.setattr(main, "write_audit", lambda *args, **kwargs: audits.append(args))
    assert main.reopen_expired_accepted_risk_findings(limit=5) == 1
    assert audits[0][2] == "FINDING_ACCEPTED_RISK_EXPIRED"


def test_automatic_baseline_insert_is_idempotent(monkeypatch):
    context = complete_context()

    class Conn:
        def __init__(self):
            self.inserted = False

        def execute(self, query, params=None):
            if query.startswith("SELECT collection_context"):
                return Result((context, datetime.now(tz=UTC)))
            if "SELECT id::text, collection_context" in query:
                assert "AND (created_at, id) < (%s, %s::uuid)" in query
                return Result(rows=[("baseline", context)])
            if "INSERT INTO run_comparisons" in query:
                assert "ON CONFLICT" in query and "DO NOTHING" in query
                if self.inserted:
                    return Result(None)
                self.inserted = True
                return Result((params[0],))
            if "SELECT source.coverage, run.collection_context" in query:
                return Result(({}, context))
            if query.startswith("UPDATE collection_sources SET coverage"):
                return Result()
            if "SELECT id::text, state" in query:
                return Result((first, "queued"))
            raise AssertionError(query)

    conn = Conn()
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    first = main.create_automatic_comparison(
        conn, project_id="project", source_id="source", current_run_id="current"
    )
    second = main.create_automatic_comparison(
        conn, project_id="project", source_id="source", current_run_id="current"
    )
    assert first is not None
    assert second == first


def test_out_of_order_source_run_cannot_move_health_or_baseline_backward(monkeypatch):
    newer_run = "22222222-2222-2222-2222-222222222222"
    older_run = "11111111-1111-1111-1111-111111111111"

    class Conn:
        def __init__(self):
            self.latest_run_id = None
            self.source_id = "44444444-4444-4444-4444-444444444444"

        def execute(self, query, params=None):
            if "SELECT name, target_scope, collection_context" in query:
                created_at = (
                    datetime(2026, 1, 2, tzinfo=UTC)
                    if self.latest_run_id is None
                    else datetime(2026, 1, 1, tzinfo=UTC)
                )
                return Result(("Nightly", {}, complete_context(), created_at))
            if "INSERT INTO collection_sources" in query:
                assert "(incoming_run.created_at, incoming_run.id)" in query
                assert ">= (previous_run.created_at, previous_run.id)" in query
                incoming = params[7]
                if self.latest_run_id is None or incoming == newer_run:
                    self.latest_run_id = incoming
                    return Result((self.source_id,))
                return Result(None)
            if "SELECT id::text FROM collection_sources" in query:
                return Result((self.source_id,))
            if "UPDATE scan_runs SET source_id" in query:
                return Result()
            if "UPDATE collection_sources" in query:
                return Result()
            if "SELECT NOT EXISTS" in query:
                return Result((params[0] == newer_run,))
            raise AssertionError(query)

    conn = Conn()
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    assert main.register_collection_source(conn, newer_run, "project") == conn.source_id
    assert main.register_collection_source(conn, older_run, "project") == conn.source_id
    assert conn.latest_run_id == newer_run
    assert main.collection_source_run_is_latest_complete_candidate(conn, conn.source_id, newer_run) is True
    assert main.collection_source_run_is_latest_complete_candidate(conn, conn.source_id, older_run) is False
    ingest_source = inspect.getsource(main.process_job)
    assert "automation_enabled and source_run_is_latest" in ingest_source
    assert 'skip_reason = "source_superseded"' in ingest_source


def test_item_history_sql_covers_add_remove_move_rename_and_indeterminate():
    source = inspect.getsource(main._materialize_item_change_batch)
    for fragment in (
        "before_id IS NULL THEN 'added'",
        "after_id IS NULL THEN 'removed'",
        "WHEN moved THEN 'moved'",
        "WHEN renamed THEN 'renamed'",
        "WHEN permission_indeterminate THEN 'indeterminate'",
        "before_path IS DISTINCT FROM after_path",
        "before_accessed_at IS DISTINCT FROM after_accessed_at",
        "before_provider_metadata_hash IS DISTINCT FROM after_provider_metadata_hash",
        "before_permission_hash IS DISTINCT FROM after_permission_hash",
        "(before_permissions - 'observed_at') IS DISTINCT FROM",
        "ON CONFLICT (comparison_id, resource_change_id, identity_key) DO NOTHING",
    ):
        assert fragment in source


def test_comparison_recovery_keeps_phase_and_worker_has_durable_yield_contract():
    recovery_source = inspect.getsource(main.discover_recoverable_comparisons)
    worker_source = inspect.getsource(main.process_comparison_job)
    assert "'phase', 'recovery_claimed'" not in recovery_source
    assert '"completed_resource_change_id"' in worker_source
    assert '"last_item_identity_key"' in worker_source
    assert "work_quantum_exhausted" in worker_source
    assert "if operator_retry" in worker_source
    # Committed results are reset only for an explicit operator retry.
    delete_position = worker_source.index("DELETE FROM comparison_resource_changes")
    operator_position = worker_source.index("if operator_retry")
    assert delete_position > operator_position


def test_comparison_finding_does_not_treat_indeterminate_access_as_changed():
    source = inspect.getsource(main.evaluate_comparison_findings)
    assert 'change_type != "indeterminate"' in source
    assert 'access_state == "changed"' in source
    assert 'access_state == "indeterminate"' in source


def test_source_identity_and_automatic_signature_are_graph_cloud_scoped(monkeypatch):
    keys = []

    class Conn:
        def __init__(self, cloud):
            self.cloud = cloud

        def execute(self, query, params=None):
            if "SELECT name, target_scope, collection_context" in query:
                context = complete_context("sharepoint")
                context["graph_cloud"] = self.cloud
                return Result(("Tenant", {}, context, datetime(2026, 1, 1, tzinfo=UTC)))
            if "INSERT INTO collection_sources" in query:
                keys.append(params[2])
                return Result(("11111111-1111-1111-1111-111111111111",))
            if "UPDATE scan_runs SET source_id" in query:
                return Result()
            if "UPDATE collection_sources" in query:
                return Result()
            raise AssertionError(query)

    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    main.register_collection_source(Conn("global"), "run-1", "project")
    main.register_collection_source(Conn("usgovhigh"), "run-2", "project")
    assert keys[0] != keys[1]
    global_context = complete_context("sharepoint")
    global_context["graph_cloud"] = "global"
    gov_context = complete_context("sharepoint")
    gov_context["graph_cloud"] = "usgovhigh"
    assert main._automatic_comparison_signature(global_context) != main._automatic_comparison_signature(gov_context)


def test_comparison_finding_evaluation_is_keyset_bounded(monkeypatch):
    rows = [
        (
            row_id,
            f"identity-{row_id}",
            "appeared",
            "smb",
            "smb_share",
            f"Share {row_id}",
            ["structure"],
            "strong",
            None,
            {},
            "appeared",
            "not_assessed",
            "not_assessed",
        )
        for row_id in (1, 2, 3)
    ]

    class Conn:
        def execute(self, query, params=None):
            assert "id > %s" in query and "LIMIT %s" in query
            after_id = params[1]
            limit = params[2]
            return Result(rows=[row for row in rows if row[0] > after_id][:limit])

    monkeypatch.setattr(main, "_upsert_finding", lambda *_args, **_kwargs: ("finding", True, "dedupe"))
    inserted, cursor, has_more = main.evaluate_comparison_findings(
        Conn(),
        comparison_id="comparison",
        project_id="project",
        source_id="source",
        current_run_id="run",
        limit=2,
    )
    assert (inserted, cursor, has_more) == (2, 2, True)
    inserted, cursor, has_more = main.evaluate_comparison_findings(
        Conn(),
        comparison_id="comparison",
        project_id="project",
        source_id="source",
        current_run_id="run",
        after_id=cursor,
        limit=2,
    )
    assert (inserted, cursor, has_more) == (1, 3, False)


def test_worker_audit_metadata_is_bounded_redacted_and_labels_are_validated():
    metadata = {
        "graphClientSecret": "never-log-me",
        "nested": {"auth-token": "also-secret", "safe": "x" * 5000},
        "items": list(range(150)),
    }
    sanitized = main.sanitize_audit_metadata(metadata)
    assert sanitized["graphClientSecret"] == "[redacted]"
    assert sanitized["nested"]["auth-token"] == "[redacted]"
    assert "truncated" in sanitized["nested"]["safe"]
    assert sanitized["items"][-1] == {"_truncated_item_count": 50}
    oversized = main.sanitize_audit_metadata({f"safe_{index}": "x" * 4096 for index in range(100)})
    assert oversized["_metadata_truncated"] is True
    for invalid in ("", "contains spaces", "x" * 121):
        try:
            main._validate_audit_label(invalid, name="action", max_length=120)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid audit label was accepted: {invalid!r}")


def test_findings_evaluators_use_durable_bounded_batches():
    run_source = inspect.getsource(main.evaluate_run_findings)
    comparison_source = inspect.getsource(main.process_comparison_job)
    assert "FINDING_EVALUATION_BATCH_SIZE" in run_source
    assert "_persist_finding_evaluation_progress" in run_source
    assert "conn.commit()" in run_source
    assert '"phase": "evaluating_findings"' in comparison_source
    assert '"findings_cursor"' in comparison_source
    checkpoint_source = inspect.getsource(main._persist_finding_evaluation_progress)
    assert "'heartbeat_at', NOW()" in checkpoint_source
    assert "'monitoring_worker'" in checkpoint_source


def test_artifact_open_refuses_symlinks_and_non_regular_files(tmp_path, monkeypatch):
    storage = tmp_path / "artifacts"
    storage.mkdir()
    (storage / "valid.ndjson").write_bytes(b"payload")
    (storage / "target.ndjson").write_bytes(b"secret")
    (storage / "link.ndjson").symlink_to(storage / "target.ndjson")
    (storage / "nested-target").mkdir()
    (storage / "nested-link").symlink_to(storage / "nested-target", target_is_directory=True)
    os.mkfifo(storage / "artifact.fifo")
    monkeypatch.setattr(main, "ARTIFACT_STORAGE_PATH", str(storage))

    with main.open_artifact_stream("valid.ndjson") as body:
        assert body.read() == b"payload"
    for key in ("link.ndjson", "nested-link/missing.ndjson", "artifact.fifo"):
        try:
            main.open_artifact_stream(key)
        except OSError:
            pass
        else:
            raise AssertionError(f"symlink artifact path was accepted: {key}")


def test_identity_preparation_batches_only_unkeyed_rows_and_resumes_by_cursor():
    class Conn:
        def __init__(self):
            self.updated = []

        def execute(self, query, params=None):
            if "SELECT id" in query and "FROM resources" in query:
                assert "identity_key IS NULL" in query and "id > %s" in query and "LIMIT %s" in query
                return Result(rows=[(1,), (2,)] if params[1] == 0 else [])
            if "UPDATE resources AS resource" in query:
                self.updated.append(("resource", list(params[1])))
                return Result()
            if "SELECT id" in query and "FROM items" in query:
                assert "identity_key IS NULL" in query and "id > %s" in query and "LIMIT %s" in query
                return Result(rows=[(10,), (11,)] if params[1] == 0 else [])
            if "UPDATE items AS item" in query:
                self.updated.append(("item", list(params[1])))
                return Result()
            raise AssertionError(query)

    conn = Conn()
    first = main.prepare_run_identity_keys_batch(conn, "run", limit=2)
    assert first == {
        "resource_after_id": 2,
        "item_after_id": 0,
        "resource_complete": False,
        "item_complete": False,
        "processed": 2,
    }
    second = main.prepare_run_identity_keys_batch(conn, "run", resource_after_id=2, limit=2)
    assert second["resource_complete"] is True and second["item_after_id"] == 11
    third = main.prepare_run_identity_keys_batch(
        conn,
        "run",
        resource_after_id=2,
        item_after_id=11,
        limit=2,
    )
    assert third["item_complete"] is True
    assert conn.updated == [("resource", [1, 2]), ("item", [10, 11])]


def test_monitoring_recovery_is_durable_bounded_and_operator_replayable():
    discovery = inspect.getsource(main.discover_recoverable_monitoring_evaluations)
    processor = inspect.getsource(main.process_monitoring_evaluation_job)
    comparison_discovery = inspect.getsource(main.discover_recoverable_comparison_finding_evaluations)
    comparison_processor = inspect.getsource(main.process_comparison_finding_evaluation_job)
    assert "FOR UPDATE SKIP LOCKED" in discovery and "LIMIT %s" in discovery
    assert "monitoring_findings,next_retry_at" in discovery
    assert "pg_advisory_lock" in processor
    assert "collection_source_run_is_latest_complete_candidate" in processor
    assert "MONITORING_EVALUATION_RECOVERED" in processor
    assert "update_collection_source_monitoring_coverage" in processor
    assert "state = 'complete'" in comparison_discovery
    assert "FOR UPDATE SKIP LOCKED" in comparison_discovery
    assert "without hiding or rebuilding" in comparison_processor


def test_source_target_scope_summary_is_bounded_but_full_scope_hash_is_stable():
    scope = {"targeted_sites": [f"https://tenant.example/sites/{index}" for index in range(1000)]}
    bounded = main._bounded_source_target_scope(scope)
    assert len(bounded["targeted_sites"]) == 20
    assert bounded["_scope_summary"]["list_counts"]["targeted_sites"] == 1000
    assert bounded["_scope_summary"]["truncated"] is True
    assert bounded["_scope_summary"]["scope_hash"] == main._bounded_source_target_scope(scope)["_scope_summary"][
        "scope_hash"
    ]


def test_monitoring_source_scope_is_credential_free_and_secret_rotation_does_not_split_history():
    context = complete_context()
    first_scope = {
        "hosts": ["files.example.test"],
        "clientSecret": "first",
        "nested": {"auth-token": "first", "site": "operations"},
    }
    second_scope = {
        "hosts": ["files.example.test"],
        "clientSecret": "second",
        "nested": {"auth-token": "second", "site": "operations"},
    }
    context["metadata"]["collection"]["target_scope"] = first_scope
    sanitized = main._monitoring_target_scope(context, {})
    first_key = main._monitoring_source_key(context, {})
    context["metadata"]["collection"]["target_scope"] = second_scope
    assert sanitized == {
        "hosts": ["files.example.test"],
        "nested": {"site": "operations"},
    }
    assert main._monitoring_target_scope(context, {}) == sanitized
    assert main._monitoring_source_key(context, {}) == first_key


def test_finding_observation_timestamps_follow_run_chronology():
    source = inspect.getsource(main._upsert_finding)
    assert "SELECT created_at FROM scan_runs WHERE id = %(run_id)s" in source
    assert "GREATEST(findings.last_seen_at, EXCLUDED.last_seen_at)" in source
    assert "first_seen_at = LEAST(findings.first_seen_at, EXCLUDED.first_seen_at)" in source


def test_item_permission_comparison_ignores_observation_time_and_bounds_metadata():
    summary_source = inspect.getsource(main.reconcile_permission_summaries)
    item_source = inspect.getsource(main._materialize_item_change_batch)
    assert "'comparison_evidence_hash', evidence.comparison_evidence_hash" in summary_source
    assert "'comparison_quality_hash', evidence.comparison_quality_hash" in summary_source
    assert "(before_permissions - 'observed_at') IS DISTINCT FROM" in item_source
    assert "before_permission_hash IS DISTINCT FROM after_permission_hash" in item_source
    assert "before_provider_metadata_hash" in item_source
    assert "item.provider_metadata::text" in item_source
    assert "before_permission_quality_hash IS NOT DISTINCT FROM after_permission_quality_hash" in item_source
    assert "'file_attributes', before_attributes" in item_source
    assert "before_web_url IS DISTINCT FROM after_web_url" in item_source


def test_item_candidate_queries_match_enterprise_comparison_index():
    candidate_source = inspect.getsource(main._ensure_item_candidate_resource_changes)
    assert "item.run_id = %(baseline_run_id)s" in candidate_source
    assert "item.run_id = %(current_run_id)s" in candidate_source
    for function in (
        main._item_identity_is_ambiguous,
        main._item_change_batch_keys,
        main._materialize_item_change_batch,
    ):
        source = inspect.getsource(function)
        assert "deleted IS FALSE" in source
