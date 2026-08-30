import os
import sqlite3
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sharepoint.auth import GraphTokenContext
from sharepoint.state import (
    STALE_STAGE_SECONDS,
    STATE_SCHEMA_VERSION,
    DriveState,
    SharePointStateStore,
    StateConflictError,
    StateStoreError,
    state_scope_key,
)


def _context(user_id: str = "user-1") -> GraphTokenContext:
    return GraphTokenContext(
        access_token="redacted",
        auth_mode="token",
        auth_type="delegated",
        tenant_id="tenant-1",
        client_id="client-1",
        user_id=user_id,
        user_principal_name=f"{user_id}@example.com",
        scopes=("Sites.Read.All",),
        roles=(),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )


def _item(
    item_id: str,
    path: str,
    *,
    name: str | None = None,
    parent_id: str | None = "parent-1",
    is_dir: bool = False,
) -> dict[str, object]:
    return {
        "provider": "sharepoint",
        "provider_resource_id": "drive-1",
        "provider_item_id": item_id,
        "provider_parent_id": parent_id,
        "path": path,
        "name": name or path.rsplit("/", 1)[-1],
        "is_dir": is_dir,
        "deleted": False,
        "metadata": {"site_id": "site-1", "drive_id": "drive-1"},
    }


def _begin(store: SharePointStateStore, session: str, scope: str, version: int, mode: str) -> None:
    store.begin_drive_stage(
        session_id=session,
        scope_key=scope,
        tenant_id="tenant-1",
        site_id="site-1",
        drive_id="drive-1",
        base_version=version,
        sync_mode=mode,
    )


def _stage(store: SharePointStateStore, session: str, scope: str, items) -> None:
    store.stage_items(
        session_id=session,
        scope_key=scope,
        tenant_id="tenant-1",
        site_id="site-1",
        drive_id="drive-1",
        items=list(items),
    )


def _complete(store: SharePointStateStore, session: str, scope: str, link: str) -> None:
    store.complete_drive_stage(
        session_id=session,
        scope_key=scope,
        tenant_id="tenant-1",
        site_id="site-1",
        drive_id="drive-1",
        delta_link=link,
    )


def _commit(store: SharePointStateStore, session: str, scope: str) -> None:
    store.commit_drive(
        session_id=session,
        scope_key=scope,
        tenant_id="tenant-1",
        site_id="site-1",
        drive_id="drive-1",
    )


def _materialized(store: SharePointStateStore, session: str, scope: str):
    return list(
        store.iter_materialized_items(
            session_id=session,
            scope_key=scope,
            tenant_id="tenant-1",
            site_id="site-1",
            drive_id="drive-1",
        )
    )


def test_full_stage_is_invisible_until_atomic_commit(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "run-1", scope, 0, "full")
    _stage(store, "run-1", scope, [_item("a", "/a.txt"), _item("b", "/b.txt")])
    _complete(store, "run-1", scope, "https://graph.microsoft.com/v1.0/delta?token=1")

    assert store.count_current_items(scope, "tenant-1", "site-1", "drive-1") == 0
    assert [item["provider_item_id"] for item in _materialized(store, "run-1", scope)] == ["a", "b"]

    _commit(store, "run-1", scope)
    state = store.get_drive_state(scope, "tenant-1", "site-1", "drive-1")
    assert state.version == 1
    assert state.status == "ok"
    assert state.delta_link.endswith("token=1")
    assert store.count_current_items(scope, "tenant-1", "site-1", "drive-1") == 2


def test_state_preserves_exact_identity_and_path_and_never_truncates(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "run-1", scope, 0, "full")
    exact = _item(" item-id ", "/ Folder / report .txt ")

    _stage(store, "run-1", scope, [exact])
    _complete(store, "run-1", scope, "https://graph.microsoft.com/v1.0/delta?token=1")
    materialized = _materialized(store, "run-1", scope)

    assert materialized[0]["provider_item_id"] == " item-id "
    assert materialized[0]["path"] == "/ Folder / report .txt "

    _begin(store, "run-2", scope, 0, "full")
    with pytest.raises(StateStoreError, match="path exceeds"):
        _stage(
            store,
            "run-2",
            scope,
            [_item("too-long", "/" + "x" * 400, name="x")],
        )


def test_opaque_tokens_partition_state_without_persisting_bearer_value() -> None:
    first = _context()
    first = replace(
        first,
        access_token="opaque-secret-one",
        jwt_inspection="opaque_token_context_supplied_by_operator",
    )
    second = replace(first, access_token="opaque-secret-two")

    first_scope = state_scope_key(first)
    second_scope = state_scope_key(second)

    assert first_scope != second_scope
    assert "opaque-secret" not in first_scope
    assert len(first_scope) == 64


def test_graph_cloud_partitions_delta_state() -> None:
    global_context = _context()
    government_context = replace(global_context, cloud="gcc-high")

    assert state_scope_key(global_context) != state_scope_key(government_context)


@pytest.mark.skipif(os.name == "nt", reason="POSIX state ownership and mode contract")
def test_state_rejects_symlink_database_without_chmodding_target(tmp_path) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"not collector state")
    target.chmod(0o640)
    state_path = tmp_path / "state.sqlite3"
    state_path.symlink_to(target)

    with pytest.raises(StateStoreError, match="must not be a symlink"):
        SharePointStateStore(state_path).initialize()

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_state_rejects_symlink_parent(tmp_path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(StateStoreError, match="parent must be a real directory"):
        SharePointStateStore(linked_parent / "state.sqlite3").initialize()


def test_state_rejects_symlink_in_existing_parent_ancestor(tmp_path) -> None:
    real_ancestor = tmp_path / "real"
    nested_parent = real_ancestor / "nested"
    nested_parent.mkdir(parents=True)
    linked_ancestor = tmp_path / "linked"
    try:
        linked_ancestor.symlink_to(real_ancestor, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(StateStoreError, match="ancestors must not be symlinks"):
        SharePointStateStore(linked_ancestor / "nested" / "state.sqlite3").initialize()

    assert not (nested_parent / "state.sqlite3").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX state ownership and mode contract")
def test_state_rejects_symlink_sidecar_without_touching_target(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    state_path.touch(mode=0o600)
    target = tmp_path / "unrelated"
    target.write_text("leave me alone", encoding="utf-8")
    target.chmod(0o640)
    Path(f"{state_path}-wal").symlink_to(target)

    with pytest.raises(StateStoreError, match="sidecars must be regular files"):
        SharePointStateStore(state_path).initialize()

    assert target.read_text(encoding="utf-8") == "leave me alone"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX state ownership and mode contract")
def test_state_rejects_other_writable_parent_and_hardens_existing_database_mode(tmp_path) -> None:
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir(mode=0o700)
    state_path = protected_parent / "state.sqlite3"
    state_path.touch(mode=0o644)

    SharePointStateStore(state_path).initialize()

    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    try:
        with pytest.raises(StateStoreError, match="must not be writable by group or other users"):
            SharePointStateStore(unsafe_parent / "state.sqlite3").initialize()
    finally:
        unsafe_parent.chmod(0o700)


def test_version_one_state_is_invalidated_for_hierarchy_safe_full_resync(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as conn:
        conn.executescript(
            """
            CREATE TABLE state_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO state_metadata (key, value) VALUES ('schema_version', '1');
            CREATE TABLE items (
                scope_key TEXT NOT NULL, tenant_id TEXT NOT NULL, site_id TEXT NOT NULL,
                drive_id TEXT NOT NULL, item_id TEXT NOT NULL, sort_path TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (scope_key, tenant_id, site_id, drive_id, item_id)
            );
            INSERT INTO items VALUES (
                'scope', 'tenant', 'site', 'drive', 'item', '/old.txt', '{}', 0
            );
            CREATE TABLE staged_items (
                session_id TEXT NOT NULL, scope_key TEXT NOT NULL, tenant_id TEXT NOT NULL,
                site_id TEXT NOT NULL, drive_id TEXT NOT NULL, item_id TEXT NOT NULL,
                sort_path TEXT NOT NULL, payload TEXT, deleted INTEGER NOT NULL,
                PRIMARY KEY (
                    session_id, scope_key, tenant_id, site_id, drive_id, item_id
                )
            );
            """
        )

    migrated_store = SharePointStateStore(state_path)
    migrated_store.initialize()

    with sqlite3.connect(state_path) as conn:
        version = conn.execute("SELECT value FROM state_metadata WHERE key = 'schema_version'").fetchone()[0]
        item_columns = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert version == str(STATE_SCHEMA_VERSION)
    assert {"parent_id", "item_name"}.issubset(item_columns)
    assert item_count == 0


def test_version_two_state_is_invalidated_to_backfill_file_archive_metadata(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    store = SharePointStateStore(state_path)
    scope = state_scope_key(_context())
    _begin(store, "full", scope, 0, "full")
    _stage(store, "full", scope, [_item("a", "/a.txt")])
    _complete(store, "full", scope, "https://graph.microsoft.com/v1.0/delta?token=1")
    _commit(store, "full", scope)

    with sqlite3.connect(state_path) as conn:
        conn.execute("UPDATE state_metadata SET value = '2' WHERE key = 'schema_version'")
        conn.commit()

    migrated_store = SharePointStateStore(state_path)
    migrated_store.initialize()

    with sqlite3.connect(state_path) as conn:
        version = conn.execute("SELECT value FROM state_metadata WHERE key = 'schema_version'").fetchone()[0]
    assert version == str(STATE_SCHEMA_VERSION)
    assert migrated_store.count_current_items(scope, "tenant-1", "site-1", "drive-1") == 0
    assert migrated_store.get_drive_state(scope, "tenant-1", "site-1", "drive-1") == DriveState()


def test_version_three_state_is_invalidated_to_backfill_governance_metadata(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    store = SharePointStateStore(state_path)
    scope = state_scope_key(_context())
    _begin(store, "full", scope, 0, "full")
    _stage(store, "full", scope, [_item("a", "/a.txt")])
    _complete(store, "full", scope, "https://graph.microsoft.com/v1.0/delta?token=1")
    _commit(store, "full", scope)

    with sqlite3.connect(state_path) as conn:
        conn.execute("UPDATE state_metadata SET value = '3' WHERE key = 'schema_version'")
        conn.commit()

    migrated_store = SharePointStateStore(state_path)
    migrated_store.initialize()

    assert migrated_store.count_current_items(scope, "tenant-1", "site-1", "drive-1") == 0
    assert migrated_store.get_drive_state(scope, "tenant-1", "site-1", "drive-1") == DriveState()


def test_delta_materializes_add_move_and_tombstone_by_stable_id(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "full", scope, 0, "full")
    _stage(store, "full", scope, [_item("a", "/old/a.txt"), _item("b", "/b.txt")])
    _complete(store, "full", scope, "https://graph.microsoft.com/v1.0/delta?token=1")
    _commit(store, "full", scope)

    _begin(store, "delta", scope, 1, "delta")
    _stage(
        store,
        "delta",
        scope,
        [
            _item("a", "/new/renamed.txt", name="renamed.txt"),
            {"provider_item_id": "b", "deleted": True},
            _item("c", "/c.txt"),
        ],
    )
    _complete(store, "delta", scope, "https://graph.microsoft.com/v1.0/delta?token=2")

    materialized = _materialized(store, "delta", scope)
    assert {item["provider_item_id"] for item in materialized} == {"a", "c"}
    assert next(item for item in materialized if item["provider_item_id"] == "a")["path"] == "/new/renamed.txt"

    _commit(store, "delta", scope)
    assert store.count_current_items(scope, "tenant-1", "site-1", "drive-1") == 2
    assert store.get_drive_state(scope, "tenant-1", "site-1", "drive-1").version == 2


def test_materialized_hierarchy_cascades_folder_rename_by_stable_parent_id(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "full", scope, 0, "full")
    _stage(
        store,
        "full",
        scope,
        [
            _item(
                "folder",
                "/Folder",
                name="Folder",
                parent_id="drive-root",
                is_dir=True,
            ),
            # Graph delta can omit parentReference.path. The seed path is flat,
            # but the stable parent ID still reconstructs the hierarchy.
            _item("child", "/report.txt", name="report.txt", parent_id="folder"),
        ],
    )
    _complete(store, "full", scope, "https://graph.microsoft.com/v1.0/delta?token=1")
    assert {item["provider_item_id"]: item["path"] for item in _materialized(store, "full", scope)} == {
        "folder": "/Folder",
        "child": "/Folder/report.txt",
    }
    _commit(store, "full", scope)

    _begin(store, "delta", scope, 1, "delta")
    _stage(
        store,
        "delta",
        scope,
        [
            _item(
                "folder",
                "/Renamed",
                name="Renamed",
                parent_id="drive-root",
                is_dir=True,
            )
        ],
    )
    _complete(store, "delta", scope, "https://graph.microsoft.com/v1.0/delta?token=2")

    assert {item["provider_item_id"]: item["path"] for item in _materialized(store, "delta", scope)} == {
        "folder": "/Renamed",
        "child": "/Renamed/report.txt",
    }


def test_folder_tombstone_with_live_child_withholds_delta_checkpoint(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    first_link = "https://graph.microsoft.com/v1.0/delta?token=1"
    _begin(store, "full", scope, 0, "full")
    _stage(
        store,
        "full",
        scope,
        [
            _item("folder", "/Folder", name="Folder", parent_id="drive-root", is_dir=True),
            _item("child", "/Folder/report.txt", name="report.txt", parent_id="folder"),
        ],
    )
    _complete(store, "full", scope, first_link)
    _commit(store, "full", scope)

    _begin(store, "delta", scope, 1, "delta")
    _stage(store, "delta", scope, [{"provider_item_id": "folder", "deleted": True}])

    with pytest.raises(StateStoreError, match="deletion still has live children"):
        _complete(store, "delta", scope, "https://graph.microsoft.com/v1.0/delta?token=2")

    # The working snapshot and checkpoint remain intact until Graph supplies a
    # consistent child move/deletion in a later full or delta response.
    state = store.get_drive_state(scope, "tenant-1", "site-1", "drive-1")
    assert state.version == 1
    assert state.delta_link == first_link
    assert store.count_current_items(scope, "tenant-1", "site-1", "drive-1") == 2


def test_hierarchy_cycle_is_rejected_instead_of_emitting_incomplete_snapshot(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "run", scope, 0, "full")
    _stage(
        store,
        "run",
        scope,
        [
            _item("a", "/a", parent_id="b", is_dir=True),
            _item("b", "/b", parent_id="a", is_dir=True),
        ],
    )
    _complete(store, "run", scope, "https://graph.microsoft.com/v1.0/delta?token=1")

    with pytest.raises(StateStoreError, match="cycle or unresolved parent"):
        _materialized(store, "run", scope)


def test_failed_replacement_preserves_working_snapshot_and_delta(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "full", scope, 0, "full")
    _stage(store, "full", scope, [_item("a", "/a.txt")])
    first_link = "https://graph.microsoft.com/v1.0/delta?token=working"
    _complete(store, "full", scope, first_link)
    _commit(store, "full", scope)

    _begin(store, "replacement", scope, 1, "full")
    _stage(store, "replacement", scope, [_item("new", "/new.txt")])
    _complete(store, "replacement", scope, "https://graph.microsoft.com/v1.0/delta?token=new")
    store.discard_session("replacement")

    state = store.get_drive_state(scope, "tenant-1", "site-1", "drive-1")
    assert state.delta_link == first_link
    assert store.count_current_items(scope, "tenant-1", "site-1", "drive-1") == 1


def test_optimistic_version_prevents_concurrent_checkpoint_rollback(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    for session, path, link in (
        ("run-a", "/a.txt", "https://graph.microsoft.com/v1.0/delta?token=a"),
        ("run-b", "/b.txt", "https://graph.microsoft.com/v1.0/delta?token=b"),
    ):
        _begin(store, session, scope, 0, "full")
        _stage(store, session, scope, [_item(session, path)])
        _complete(store, session, scope, link)

    _commit(store, "run-a", scope)
    with pytest.raises(StateConflictError, match="changed concurrently"):
        _commit(store, "run-b", scope)

    assert store.get_drive_state(scope, "tenant-1", "site-1", "drive-1").delta_link.endswith("token=a")


def test_staging_activity_prevents_live_long_scan_cleanup(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "active-run", scope, 0, "full")
    old = time.time() - STALE_STAGE_SECONDS - 10
    with store._connection() as conn:
        conn.execute("UPDATE pending_syncs SET created_at = ?", (old,))

    _stage(store, "active-run", scope, [_item("a", "/a.txt")])
    assert store.cleanup_stale_sessions(now=time.time()) == 0

    with store._connection() as conn:
        conn.execute("UPDATE pending_syncs SET created_at = ?", (old,))
    assert store.cleanup_stale_sessions(now=time.time()) == 1


def test_empty_delta_page_also_refreshes_staging_heartbeat(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "active-run", scope, 0, "full")
    old = time.time() - STALE_STAGE_SECONDS - 10
    with store._connection() as conn:
        conn.execute("UPDATE pending_syncs SET created_at = ?", (old,))

    _stage(store, "active-run", scope, [])

    assert store.cleanup_stale_sessions(now=time.time()) == 0


def test_state_is_partitioned_by_assessment_identity(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    alice_scope = state_scope_key(_context("alice"))
    bob_scope = state_scope_key(_context("bob"))
    assert alice_scope != bob_scope
    _begin(store, "alice-run", alice_scope, 0, "full")
    _stage(store, "alice-run", alice_scope, [_item("a", "/alice.txt")])
    _complete(store, "alice-run", alice_scope, "https://graph.microsoft.com/v1.0/delta?a")
    _commit(store, "alice-run", alice_scope)

    assert store.count_current_items(alice_scope, "tenant-1", "site-1", "drive-1") == 1
    assert store.count_current_items(bob_scope, "tenant-1", "site-1", "drive-1") == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_state_database_and_parent_are_user_protected(tmp_path) -> None:
    state_path = tmp_path / "nested" / "state.sqlite3"
    store = SharePointStateStore(state_path)
    store.initialize()

    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_parent_permissions_are_not_mutated_and_sidecars_are_protected(tmp_path) -> None:
    shared_parent = tmp_path / "shared"
    shared_parent.mkdir(mode=0o755)
    shared_parent.chmod(0o755)
    state_path = shared_parent / "state.sqlite3"
    store = SharePointStateStore(state_path)
    store.initialize()

    with store._connection():
        assert stat.S_IMODE(shared_parent.stat().st_mode) == 0o755
        for suffix in ("-wal", "-shm"):
            sidecar = tmp_path / "shared" / f"state.sqlite3{suffix}"
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_provider_item_id_bound_matches_ingest_contract(tmp_path) -> None:
    store = SharePointStateStore(tmp_path / "state.sqlite3")
    scope = state_scope_key(_context())
    _begin(store, "run", scope, 0, "full")
    oversized = _item("x" * 513, "/x")

    with pytest.raises(Exception, match="bounded provider item ID"):
        _stage(store, "run", scope, [oversized])
