from types import SimpleNamespace

import pytest

from app.bootstrap import ensure_artifact_storage_path, validate_seed_admin_settings


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


def test_production_bootstrap_requires_seed_admin() -> None:
    settings = SimpleNamespace(app_env="production", seed_admin_email=None, seed_admin_password=None)

    with pytest.raises(RuntimeError, match="must both be set for production bootstrap"):
        validate_seed_admin_settings(settings)


def test_runtime_settings_can_omit_seed_admin_after_bootstrap() -> None:
    settings = SimpleNamespace(app_env="production", seed_admin_email="admin@example.com", seed_admin_password="secret")

    validate_seed_admin_settings(settings)
