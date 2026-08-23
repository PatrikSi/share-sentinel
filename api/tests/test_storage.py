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
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))

    assert storage.artifact_storage_ready() is True

    missing = tmp_path / "missing"
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(artifact_storage_path=str(missing)))

    assert storage.artifact_storage_ready() is False
