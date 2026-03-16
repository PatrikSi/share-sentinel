from app.bootstrap import ensure_artifact_storage_path


def test_ensure_artifact_storage_path_creates_missing_directory(tmp_path) -> None:
    storage_path = tmp_path / "artifacts"

    created = ensure_artifact_storage_path(str(storage_path))

    assert created is True
    assert storage_path.is_dir()


def test_ensure_artifact_storage_path_reuses_existing_directory(tmp_path) -> None:
    storage_path = tmp_path / "artifacts"
    storage_path.mkdir()

    created = ensure_artifact_storage_path(str(storage_path))

    assert created is False
    assert storage_path.is_dir()
