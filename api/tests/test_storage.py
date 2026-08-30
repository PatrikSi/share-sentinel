from types import SimpleNamespace

import pytest
from app.services import storage


def test_complete_multipart_upload_writes_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))

    key = "projects/p/runs/r/artifact.ndjson"
    upload_id = storage.create_multipart_upload(key)
    etag1 = storage.upload_part(key, upload_id, 1, b'{"type":"run_meta"}\n')
    etag2 = storage.upload_part(key, upload_id, 2, b'{"type":"run_end"}\n')
    storage.complete_multipart_upload(
        key,
        upload_id,
        [{"ETag": etag1, "PartNumber": 1}, {"ETag": etag2, "PartNumber": 2}],
    )

    with storage.get_object_stream(key) as fp:
        assert fp.read() == b'{"type":"run_meta"}\n{"type":"run_end"}\n'


def test_abort_multipart_upload_removes_temp_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))

    key = "projects/p/runs/r/artifact.ndjson"
    upload_id = storage.create_multipart_upload(key)
    storage.upload_part(key, upload_id, 1, b"chunk")
    storage.abort_multipart_upload(key, upload_id)

    with pytest.raises(FileNotFoundError):
        storage.upload_part(key, upload_id, 2, b"more")


def test_storage_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))

    with pytest.raises(ValueError, match="inside artifact storage"):
        storage.create_multipart_upload("../escape")


def test_artifact_storage_ready_requires_existing_accessible_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(
            artifact_storage_path=str(tmp_path),
            artifact_storage_min_free_bytes=0,
            artifact_storage_min_free_percent=0,
        ),
    )

    assert storage.artifact_storage_ready() is True

    missing = tmp_path / "missing"
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(missing)))

    assert storage.artifact_storage_ready() is False


def test_artifact_storage_status_enforces_capacity_and_write_probe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(
            artifact_storage_path=str(tmp_path),
            artifact_storage_min_free_bytes=0,
            artifact_storage_min_free_percent=0,
        ),
    )

    status = storage.artifact_storage_status(verify_write=True)

    assert status["ok"] is True
    assert status["capacity_ok"] is True
    assert status["free_bytes"] >= 0
    assert list(tmp_path.glob(".share-sentinel-health-*")) == []


def test_artifact_storage_status_rejects_low_free_space(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(
            artifact_storage_path=str(tmp_path),
            artifact_storage_min_free_bytes=2**63,
            artifact_storage_min_free_percent=0,
        ),
    )

    status = storage.artifact_storage_status()

    assert status["ok"] is False
    assert status["reason"] == "low_free_space"


def test_multipart_upload_rejects_low_capacity_before_creating_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "_artifact_root", lambda: tmp_path)
    monkeypatch.setattr(
        storage,
        "artifact_storage_status",
        lambda: {"ok": False, "reason": "low_free_space"},
    )

    with pytest.raises(storage.ArtifactStorageUnavailableError) as exc_info:
        storage.create_multipart_upload("project/run/artifact.ndjson")

    assert exc_info.value.reason == "low_free_space"
    assert not (tmp_path / ".multipart").exists()


def test_upload_part_rechecks_capacity_and_preserves_abortability(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "_artifact_root", lambda: tmp_path)
    states = iter(
        (
            {
                "ok": True,
                "reason": None,
                "free_bytes": 1_000_000,
                "total_bytes": 2_000_000,
                "minimum_free_bytes": 0,
                "minimum_free_percent": 0,
            },
            {"ok": False, "reason": "low_free_space"},
        )
    )
    monkeypatch.setattr(storage, "artifact_storage_status", lambda: next(states))
    key = "project/run/artifact.ndjson"
    upload_id = storage.create_multipart_upload(key)

    with pytest.raises(storage.ArtifactStorageUnavailableError):
        storage.upload_part(key, upload_id, 1, b"bounded")

    storage.abort_multipart_upload(key, upload_id)
    assert not storage._multipart_path(key, upload_id).exists()


def test_known_upload_size_must_leave_configured_headroom(monkeypatch) -> None:
    monkeypatch.setattr(
        storage,
        "artifact_storage_status",
        lambda: {
            "ok": True,
            "reason": None,
            "free_bytes": 1_000,
            "total_bytes": 2_000,
            "minimum_free_bytes": 200,
            "minimum_free_percent": 10,
        },
    )

    storage.require_artifact_upload_capacity(additional_bytes=800)
    with pytest.raises(storage.ArtifactStorageUnavailableError) as exc_info:
        storage.require_artifact_upload_capacity(additional_bytes=801)

    assert exc_info.value.reason == "insufficient_capacity_for_upload"


def test_upload_part_does_not_follow_replaced_multipart_symlink(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))
    key = "projects/p/artifact.ndjson"
    upload_id = storage.create_multipart_upload(key)
    multipart_path = storage._multipart_path(key, upload_id)
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    multipart_path.unlink()
    multipart_path.symlink_to(victim)

    with pytest.raises(OSError):
        storage.upload_part(key, upload_id, 1, b"unsafe")

    assert victim.read_bytes() == b"unchanged"
    storage.abort_multipart_upload(key, upload_id)


def test_multipart_upload_rejects_symlinked_parent_component(tmp_path, monkeypatch) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    (root / ".multipart").mkdir(parents=True)
    outside.mkdir()
    (root / ".multipart" / "projects").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(root)))

    with pytest.raises(OSError):
        storage.create_multipart_upload("projects/p/artifact.ndjson")

    assert list(outside.iterdir()) == []


def test_multipart_upload_syncs_each_new_parent_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))
    real_fsync = storage.os.fsync
    fsync_calls: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(storage.os, "fsync", recording_fsync)
    key = "projects/p/runs/r/artifact.ndjson"
    upload_id = storage.create_multipart_upload(key)

    # One parent-directory sync is required for every directory component
    # created beneath the already-existing artifact root.
    assert len(fsync_calls) >= len(storage._multipart_parts(key, upload_id)) - 1
    storage.abort_multipart_upload(key, upload_id)


def test_get_object_stream_rejects_symlink(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))
    key = "projects/p/artifact.ndjson"
    artifact_path = storage._artifact_path(key)
    artifact_path.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"secret")
    artifact_path.symlink_to(victim)

    with pytest.raises(OSError):
        storage.get_object_stream(key)


def test_multipart_upload_rejects_untrusted_upload_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))

    with pytest.raises(ValueError, match="upload_id must be a UUID"):
        storage.upload_part("projects/p/artifact.ndjson", "../../outside", 1, b"payload")


def test_complete_multipart_upload_does_not_replace_immutable_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))
    key = "projects/p/artifact.ndjson"
    first_id = storage.create_multipart_upload(key)
    storage.upload_part(key, first_id, 1, b"first")
    storage.complete_multipart_upload(key, first_id, [])
    second_id = storage.create_multipart_upload(key)
    storage.upload_part(key, second_id, 1, b"second")

    with pytest.raises(FileExistsError):
        storage.complete_multipart_upload(key, second_id, [])

    storage.abort_multipart_upload(key, second_id)
    with storage.get_object_stream(key) as artifact:
        assert artifact.read() == b"first"
