import importlib.util
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import collector_entrypoint
import pytest
import share_sentinel_sharepoint as cli
from share_sentinel_collector import NDJSONWriter as RealNDJSONWriter
from sharepoint.auth import CertificateCredentialAuthProvider, GraphTokenContext, TokenAcquisitionError
from sharepoint.state import StateConflictError


def _context() -> GraphTokenContext:
    return GraphTokenContext(
        access_token="never-log-this-token",
        auth_mode="token",
        auth_type="delegated",
        tenant_id="tenant-1",
        client_id="client-1",
        user_id="user-1",
        user_principal_name="alice@example.com",
        scopes=("Sites.Read.All",),
        roles=(),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )


def test_sharepoint_cli_requires_no_host_or_cidr_target() -> None:
    args = cli.parse_args(["--auth", "token", "--quiet"])

    assert args.site == []
    assert not hasattr(args, "hosts")
    assert args.output == "-"
    assert args.permissions == "none"
    assert args.max_permission_objects == 10_000
    assert args.max_permission_http_attempts == 25_000
    assert args.max_permission_entries == 100_000
    assert args.permission_concurrency == 2
    assert args.graph_cloud == "global"
    assert args.certificate_path is None


def test_permission_cli_modes_and_hard_bounds_are_explicit() -> None:
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--permissions",
            "library_roots",
            "--max-permission-objects",
            "25",
            "--max-permission-http-attempts",
            "75",
            "--max-permission-entries",
            "100",
            "--permission-concurrency",
            "3",
        ]
    )
    assert args.permissions == "library_roots"
    assert args.max_permission_objects == 25
    assert args.max_permission_http_attempts == 75
    assert args.max_permission_entries == 100
    assert args.permission_concurrency == 3

    with pytest.raises(TokenAcquisitionError, match="cannot be combined"):
        cli.parse_args(["--auth", "token", "--no-files", "--permissions", "all_items"])
    with pytest.raises(TokenAcquisitionError, match="permission-concurrency cannot exceed 8"):
        cli.parse_args(["--auth", "token", "--permission-concurrency", "9"])
    with pytest.raises(TokenAcquisitionError, match="max-permission-objects cannot exceed"):
        cli.parse_args(
            [
                "--auth",
                "token",
                "--max-permission-objects",
                str(cli.MAX_PERMISSION_OBJECTS_HARD_LIMIT + 1),
            ]
        )


@pytest.mark.parametrize("path", ["scan.json", "scan.json.gz", "scan.txt"])
def test_sharepoint_cli_rejects_compact_or_unknown_artifact_formats(path: str) -> None:
    with pytest.raises(TokenAcquisitionError, match="must use .ndjson/.jsonl"):
        cli.parse_args(["--auth", "token", "--output", path])


def test_cli_has_no_password_or_inline_bearer_token_argument() -> None:
    help_text = cli.build_parser().format_help()
    assert "--password" not in help_text
    assert "--access-token" not in help_text
    assert "--token-env" in help_text
    assert "--token-file" in help_text
    assert "--token-stdin" in help_text
    assert "--graph-cloud" in help_text
    assert "--certificate-path" in help_text
    assert "--certificate-passphrase-env" in help_text
    assert "--certificate-passphrase " not in help_text


def test_cloud_and_certificate_cli_options_are_bounded_to_app_authentication(tmp_path) -> None:
    with pytest.raises(TokenAcquisitionError, match="requires --auth app"):
        cli.parse_args(["--auth", "token", "--certificate-path", str(tmp_path / "credential.pem")])
    with pytest.raises(TokenAcquisitionError, match="require --certificate-path"):
        cli.parse_args(["--auth", "app", "--certificate-thumbprint", "A" * 40])
    with pytest.raises(TokenAcquisitionError, match="valid environment variable"):
        cli.parse_args(["--auth", "app", "--certificate-passphrase-env", "BAD-NAME"])

    args = cli.parse_args(["--auth", "token", "--graph-cloud", "dod"])
    provider = cli._build_auth_provider(args)
    assert provider.cloud_profile.name == "dod"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_cli_builds_certificate_provider_without_exposing_passphrase(monkeypatch, tmp_path) -> None:
    credential_file = tmp_path / "credential.pem"
    credential_file.write_text(
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----\n"
        "-----BEGIN CERTIFICATE-----\npublic-material\n-----END CERTIFICATE-----\n",
        encoding="ascii",
    )
    credential_file.chmod(0o600)
    monkeypatch.delenv(cli.DEFAULT_CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setenv(cli.DEFAULT_CERTIFICATE_PASSPHRASE_ENV, "never-print-this-passphrase")
    args = cli.parse_args(
        [
            "--auth",
            "app",
            "--tenant-id",
            "tenant.example",
            "--client-id",
            "client-id",
            "--certificate-path",
            str(credential_file),
            "--graph-cloud",
            "china",
        ]
    )

    provider = cli._build_auth_provider(args)

    assert isinstance(provider, CertificateCredentialAuthProvider)
    assert provider.cloud_profile.name == "china"
    assert "never-print-this-passphrase" not in repr(provider)

    monkeypatch.setenv(cli.DEFAULT_CLIENT_SECRET_ENV, "conflicting-secret")
    with pytest.raises(TokenAcquisitionError, match="both app secret and certificate") as exc:
        cli._build_auth_provider(args)
    assert "conflicting-secret" not in str(exc.value)


def test_cli_help_describes_finite_defaults_explicit_unlimited_flags_and_request_budget() -> None:
    help_text = " ".join(cli.build_parser().format_help().split())

    assert "maximum discovered sites (default: 10000)" in help_text
    assert "maximum discovered document libraries (default: 50000)" in help_text
    assert "maximum materialized items per run (default: 2000000)" in help_text
    assert "--unlimited-sites" in help_text
    assert "--unlimited-libraries" in help_text
    assert "--unlimited-items" in help_text
    assert "run-wide Graph HTTP-attempt budget including retries and permission requests (default: 250000)" in help_text
    assert "maximum pages in each Microsoft Graph paging sequence (default: 100000)" in help_text
    assert "concurrent document-library scans (1-16; default: 4)" in help_text
    assert "connection timeout in seconds (default: 10)" in help_text
    assert "response read timeout in seconds (default: 60)" in help_text
    assert "maximum attempts per retriable Graph request (1-20; default: 5)" in help_text
    assert "maximum acceptable Graph retry delay in seconds (default: 120)" in help_text
    assert "maximum bytes accepted in one Graph JSON response (default: 33554432)" in help_text
    assert "repeat for additional diagnostics" in help_text
    assert "suppress authentication, progress, and final summary output" in help_text
    assert "minimum seconds between progress reports (0 disables; default: 5)" in help_text


def test_cli_defaults_are_finite_and_unlimited_inventory_requires_explicit_flags() -> None:
    defaults = cli.parse_args(["--auth", "token"])
    unlimited = cli.parse_args(["--auth", "token", "--unlimited-sites", "--unlimited-libraries", "--unlimited-items"])

    assert (defaults.max_sites, defaults.max_libraries, defaults.max_items) == (10_000, 50_000, 2_000_000)
    assert defaults.max_graph_http_attempts == 250_000
    assert (unlimited.max_sites, unlimited.max_libraries, unlimited.max_items) == (0, 0, 0)

    with pytest.raises(TokenAcquisitionError, match="--max-graph-http-attempts cannot exceed"):
        cli.parse_args(["--auth", "token", "--max-graph-http-attempts", "10000001"])


@pytest.mark.parametrize("flag", ["--max-sites", "--max-libraries", "--max-items"])
def test_legacy_zero_inventory_limits_are_intentional_hard_failures(flag: str) -> None:
    # Zero used to mean unlimited. It now fails closed so an omitted/mistyped
    # safety bound cannot silently disable protection; use the named
    # --unlimited-* flags when that operator decision is intentional.
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--auth", "token", flag, "0"])

    assert exc.value.code == 2


def test_sharepoint_cli_requires_explicit_gzip_flag_for_gzip_suffix() -> None:
    with pytest.raises(TokenAcquisitionError, match=r"\.gz output requires --gzip"):
        cli.parse_args(["--auth", "token", "--output", "scan.ndjson.gz"])

    with pytest.raises(TokenAcquisitionError, match="must end in .gz"):
        cli.parse_args(["--auth", "token", "--output", "scan.ndjson", "--gzip"])

    args = cli.parse_args(["--auth", "token", "--output", "scan.ndjson.gz", "--gzip"])
    assert args.gzip is True


def test_sharepoint_cli_bounds_explicit_site_target_metadata() -> None:
    with pytest.raises(TokenAcquisitionError, match="repeated at most 128"):
        cli.parse_args(["--auth", "token", *[value for _ in range(129) for value in ("--site", "site-1")]])

    with pytest.raises(TokenAcquisitionError, match="cannot exceed 4096 UTF-8 bytes"):
        cli.parse_args(["--auth", "token", "--site", "x" * 4097])

    references = ["x" * 200 for _ in range(128)]
    with pytest.raises(TokenAcquisitionError, match="combined --site values"):
        cli.parse_args(["--auth", "token", *[value for reference in references for value in ("--site", reference)]])


def test_auth_summary_escapes_token_claim_control_characters(capsys) -> None:
    context = _context()
    unsafe = GraphTokenContext(
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

    cli._print_auth_summary(unsafe, quiet=False)

    output = capsys.readouterr().err
    assert "alice\\u000aforged\\u001b[2J@example.com" in output
    assert "\x1b" not in output
    assert "alice\nforged" not in output


def test_permission_preflight_requires_site_read_and_targets_for_sites_selected() -> None:
    delegated = _context()
    files_only = GraphTokenContext(
        access_token=delegated.access_token,
        auth_mode=delegated.auth_mode,
        auth_type=delegated.auth_type,
        tenant_id=delegated.tenant_id,
        client_id=delegated.client_id,
        user_id=delegated.user_id,
        user_principal_name=delegated.user_principal_name,
        scopes=("Files.Read.All",),
        roles=(),
        expires_at=delegated.expires_at,
    )
    with pytest.raises(TokenAcquisitionError, match="site discovery requires"):
        cli._validate_permissions(files_only, targeted_sites=[])

    selected = GraphTokenContext(
        access_token=delegated.access_token,
        auth_mode="app",
        auth_type="application",
        tenant_id=delegated.tenant_id,
        client_id=delegated.client_id,
        user_id=None,
        user_principal_name=None,
        scopes=(),
        roles=("Sites.Selected",),
        expires_at=delegated.expires_at,
    )
    with pytest.raises(TokenAcquisitionError, match="provide one or more --site"):
        cli._validate_permissions(selected, targeted_sites=[])
    cli._validate_permissions(selected, targeted_sites=["site-1"])


def test_container_dispatcher_preserves_legacy_and_routes_sharepoint(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(collector_entrypoint.os, "execv", lambda _exe, argv: calls.append(argv))

    monkeypatch.setattr(collector_entrypoint.sys, "argv", ["entrypoint", "--hosts", "hosts.txt"])
    collector_entrypoint.main()
    assert calls[-1][1].endswith("share_sentinel_collector.py")
    assert calls[-1][2:] == ["--hosts", "hosts.txt"]

    monkeypatch.setattr(
        collector_entrypoint.sys,
        "argv",
        ["entrypoint", "sharepoint", "--auth", "token"],
    )
    collector_entrypoint.main()
    assert calls[-1][1].endswith("share_sentinel_sharepoint.py")
    assert calls[-1][2:] == ["--auth", "token"]

    monkeypatch.setattr(collector_entrypoint.sys, "argv", ["entrypoint", "--help"])
    collector_entrypoint.main()
    assert calls[-1][1].endswith("share_sentinel_collector.py")
    assert "sharepoint --help" in capsys.readouterr().out


class FakeProvider:
    def acquire_token(self) -> GraphTokenContext:
        return _context()


class FakeClient:
    retry_count = 0

    def __init__(self, *_args, **_kwargs):
        pass


class FakeState:
    def __init__(self, *_args, commit_error: BaseException | None = None, **_kwargs) -> None:
        self.commit_error = commit_error
        self.committed: list[dict[str, object]] = []
        self.discarded: list[str] = []

    def initialize(self) -> None:
        pass

    def commit_drive(self, **kwargs) -> None:
        self.committed.append(kwargs)
        if self.commit_error:
            raise self.commit_error

    def discard_session(self, run_id: str) -> None:
        self.discarded.append(run_id)


class FakeWriter:
    instances: list["FakeWriter"] = []

    def __init__(self, path, _gzip, max_spool_bytes):  # noqa: ARG002
        self.path = path
        self.records: list[dict[str, object]] = []
        self.closed = False
        self.__class__.instances.append(self)

    def emit(self, record: dict[str, object]) -> None:
        self.records.append(record)

    def close(self, keep_output: bool = True) -> None:  # noqa: ARG002
        self.closed = True


class SuccessfulCollector:
    collection_mode = "delegated_user_view"
    sync_mode = "full"

    def __init__(self, **kwargs) -> None:
        self.stats = kwargs["stats"]

    def collect(self):
        return [], "success"


class SuccessfulPermissionCollector(SuccessfulCollector):
    permission_run_summary = {
        "contract_version": 1,
        "requested": True,
        "mode": "library_roots",
        "permission_surface": "sharepoint_graph_permissions",
        "semantics": "sharepoint_graph_permission_v1",
        "classification_policy": "positive_evidence_only_v1",
        "response_scope": "effective_sharing_permissions",
        "provider_visibility": "caller_dependent_unverified",
        "request_coverage": "complete",
        "candidate_objects": 1,
        "attempted_objects": 1,
        "completed_objects": 1,
        "failed_objects": 0,
        "skipped_objects": 0,
        "http_attempts": 2,
        "entries_observed": 1,
        "entries_emitted": 1,
        "entries_omitted": 0,
        "unknown_entries": 0,
        "anonymous_objects": 1,
        "broad_internal_objects": 0,
        "partial_reasons": [],
    }


def _patch_run_dependencies(monkeypatch, *, state=None, collector=SuccessfulCollector) -> FakeState:
    fake_state = state or FakeState()
    FakeWriter.instances.clear()
    monkeypatch.setattr(cli, "_build_auth_provider", lambda _args: FakeProvider())
    monkeypatch.setattr(cli, "GraphClient", FakeClient)
    monkeypatch.setattr(cli, "SharePointStateStore", lambda *_args, **_kwargs: fake_state)
    monkeypatch.setattr(cli, "SharePointCollector", collector)
    monkeypatch.setattr(cli, "NDJSONWriter", FakeWriter)
    return fake_state


def test_failed_direct_upload_preserves_temporary_artifact(monkeypatch, tmp_path, capsys) -> None:
    state = _patch_run_dependencies(monkeypatch)
    monkeypatch.setenv("SHARE_SENTINEL_API_TOKEN", "api-token")
    monkeypatch.setattr(
        cli,
        "upload_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("upload unavailable")),
    )
    state_path = tmp_path / "sharepoint-state.sqlite3"
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--upload",
            "--api-base",
            "https://sentinel.example/api",
            "--project-id",
            "project-1",
            "--state-path",
            str(state_path),
            "--quiet",
        ]
    )

    with pytest.raises(RuntimeError, match="upload unavailable"):
        cli.run(args)

    stderr = capsys.readouterr().err
    match = re.search(r"artifact preserved for upload recovery: (.+)", stderr)
    assert match is not None
    recovery_path = match.group(1).strip()
    assert os.path.exists(recovery_path)
    assert Path(recovery_path).parent == tmp_path / ".sharepoint-state.sqlite3.upload-spool"
    if os.name != "nt":
        assert Path(recovery_path).stat().st_mode & 0o077 == 0
    assert state.discarded
    os.unlink(recovery_path)


def test_checkpoint_conflict_after_upload_keeps_snapshot_success_and_returns_warning(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    pending = SimpleNamespace(
        scope_key="scope",
        tenant_id="tenant-1",
        site_id="site-1",
        drive_id="drive-1",
    )

    class CollectorWithPending(SuccessfulCollector):
        def collect(self):
            return [pending], "success"

    state = FakeState(
        commit_error=StateConflictError("SharePoint state changed concurrently; checkpoint was not advanced")
    )
    _patch_run_dependencies(monkeypatch, state=state, collector=CollectorWithPending)
    monkeypatch.setenv("SHARE_SENTINEL_API_TOKEN", "api-token")
    monkeypatch.setattr(cli, "upload_artifact", lambda *_args, **_kwargs: "accepted")
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--upload",
            "--api-base",
            "https://sentinel.example/api",
            "--project-id",
            "project-1",
            "--output",
            str(tmp_path / "scan.ndjson"),
            "--state-path",
            str(tmp_path / "sharepoint-state.sqlite3"),
            "--quiet",
        ]
    )

    result = cli.run(args)

    assert result == cli.EXIT_PARTIAL
    run_end = next(record for record in FakeWriter.instances[-1].records if record["type"] == "run_end")
    run_meta = [record for record in FakeWriter.instances[-1].records if record["type"] == "run_meta"]
    assert run_end["status"] == "success"
    assert len(run_meta) == 2
    assert run_meta[0]["status"] == "running"
    assert run_meta[-1]["status"] == "success"
    assert "checkpoint warning" in capsys.readouterr().err


def test_stdout_only_snapshot_withholds_delta_checkpoint(monkeypatch, capsys) -> None:
    pending = SimpleNamespace(
        scope_key="scope",
        tenant_id="tenant-1",
        site_id="site-1",
        drive_id="drive-1",
    )

    class CollectorWithPending(SuccessfulCollector):
        def collect(self):
            return [pending], "success"

    state = _patch_run_dependencies(monkeypatch, collector=CollectorWithPending)
    args = cli.parse_args(["--auth", "token", "--quiet"])

    result = cli.run(args)

    assert result == cli.EXIT_PARTIAL
    assert state.committed == []
    assert state.discarded
    run_end = next(record for record in FakeWriter.instances[-1].records if record["type"] == "run_end")
    assert run_end["status"] == "success"
    assert "stdout is not a durable artifact" in capsys.readouterr().err


def test_successful_temporary_upload_removes_spool(monkeypatch, tmp_path, capsys) -> None:
    _patch_run_dependencies(monkeypatch)
    monkeypatch.setenv("SHARE_SENTINEL_API_TOKEN", "api-token")
    uploaded_paths: list[str] = []

    def upload(_args, _run_id, path, hosts):  # noqa: ARG001
        uploaded_paths.append(path)
        assert os.path.exists(path)
        return "accepted"

    monkeypatch.setattr(cli, "upload_artifact", upload)
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--upload",
            "--api-base",
            "https://sentinel.example/api",
            "--project-id",
            "project-1",
            "--state-path",
            str(tmp_path / "sharepoint-state.sqlite3"),
            "--quiet",
        ]
    )

    assert cli.run(args) == cli.EXIT_SUCCESS
    assert len(uploaded_paths) == 1
    assert not os.path.exists(uploaded_paths[0])
    assert "artifact preserved" not in capsys.readouterr().err


def test_upload_scope_records_sharepoint_permission_mode(monkeypatch, tmp_path) -> None:
    _patch_run_dependencies(monkeypatch, collector=SuccessfulPermissionCollector)
    monkeypatch.setenv("SHARE_SENTINEL_API_TOKEN", "api-token")
    captured_scope: dict[str, object] = {}

    def upload(args, _run_id, _path, hosts):  # noqa: ARG001
        captured_scope.update(args.target_scope_override)
        return "accepted"

    monkeypatch.setattr(cli, "upload_artifact", upload)
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--permissions",
            "library_roots",
            "--upload",
            "--api-base",
            "https://sentinel.example/api",
            "--project-id",
            "project-1",
            "--state-path",
            str(tmp_path / "sharepoint-state.sqlite3"),
            "--quiet",
        ]
    )

    assert cli.run(args) == cli.EXIT_SUCCESS
    assert captured_scope["provider"] == "sharepoint"
    assert captured_scope["permissions"] == "library_roots"


def test_real_writer_artifact_has_one_run_meta_and_final_context(monkeypatch, tmp_path) -> None:
    _patch_run_dependencies(monkeypatch)
    monkeypatch.setattr(cli, "NDJSONWriter", RealNDJSONWriter)
    output_path = tmp_path / "sharepoint.ndjson"
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--output",
            str(output_path),
            "--state-path",
            str(tmp_path / "sharepoint-state.sqlite3"),
            "--quiet",
        ]
    )

    assert cli.run(args) == cli.EXIT_SUCCESS

    validator_path = Path(__file__).resolve().parents[2] / "scripts" / "validate-ndjson.py"
    spec = importlib.util.spec_from_file_location("share_sentinel_validate_ndjson", validator_path)
    assert spec is not None and spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    record_count, counts = validator.validate(output_path, summary_only=True)

    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert record_count == 2
    assert counts["run_meta"] == 1
    assert counts["run_end"] == 1
    assert records[0]["type"] == "run_meta"
    assert records[0]["status"] == "success"
    assert records[0]["materialized_snapshot"] is True
    assert "permissions" not in records[0]["collection"]
    assert records[-1]["type"] == "run_end"
    assert "collection_context" not in records[-1]


def test_permission_mode_declares_schema_v2_and_feature_from_initial_header(monkeypatch, tmp_path) -> None:
    _patch_run_dependencies(monkeypatch, collector=SuccessfulPermissionCollector)
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--permissions",
            "library_roots",
            "--output",
            str(tmp_path / "permissions.ndjson"),
            "--state-path",
            str(tmp_path / "sharepoint-state.sqlite3"),
            "--quiet",
        ]
    )

    assert cli.run(args) == cli.EXIT_SUCCESS

    records = FakeWriter.instances[-1].records
    run_meta = [record for record in records if record["type"] == "run_meta"]
    assert len(run_meta) == 2
    assert all(record["schema_version"] == 2 for record in run_meta)
    assert all(record["artifact_features"] == ["direct_permissions_v1"] for record in run_meta)
    assert run_meta[0]["metadata"]["permission_assessment"]["request_coverage"] == "running"
    assert run_meta[-1]["metadata"]["permission_assessment"]["request_coverage"] == "complete"
    assert run_meta[-1]["metadata"]["permissions_assessed"] is True
    assert run_meta[-1]["metadata"]["permissions_complete"] is True
    run_end = next(record for record in records if record["type"] == "run_end")
    assert run_end["stats"]["permission_http_attempts"] == 2
    assert run_end["stats"]["permission_partial"] is False


def test_real_writer_permission_artifact_starts_with_schema_v2_feature(monkeypatch, tmp_path) -> None:
    _patch_run_dependencies(monkeypatch, collector=SuccessfulPermissionCollector)
    monkeypatch.setattr(cli, "NDJSONWriter", RealNDJSONWriter)
    output_path = tmp_path / "sharepoint-permissions.ndjson"
    args = cli.parse_args(
        [
            "--auth",
            "token",
            "--permissions",
            "library_roots",
            "--output",
            str(output_path),
            "--state-path",
            str(tmp_path / "sharepoint-state.sqlite3"),
            "--quiet",
        ]
    )

    assert cli.run(args) == cli.EXIT_SUCCESS

    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["type"] == "run_meta"
    assert records[0]["schema_version"] == 2
    assert records[0]["artifact_features"] == ["direct_permissions_v1"]
    assert records[0]["collection"]["permissions"]["mode"] == "library_roots"
