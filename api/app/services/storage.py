from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import get_settings


def _artifact_root() -> Path:
    return Path(get_settings().artifact_storage_path)


def _capacity_status(root: Path) -> dict[str, Any]:
    settings = get_settings()
    minimum_free_bytes = max(0, int(getattr(settings, "artifact_storage_min_free_bytes", 0)))
    minimum_free_percent = max(0.0, float(getattr(settings, "artifact_storage_min_free_percent", 0.0)))
    usage = shutil.disk_usage(root)
    free_percent = (usage.free / usage.total * 100.0) if usage.total else 0.0
    enough_bytes = usage.free >= minimum_free_bytes
    enough_percent = free_percent >= minimum_free_percent
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": round(free_percent, 3),
        "minimum_free_bytes": minimum_free_bytes,
        "minimum_free_percent": minimum_free_percent,
        "capacity_ok": enough_bytes and enough_percent,
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_probe(root: Path) -> None:
    probe_path: Path | None = None
    renamed_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=".share-sentinel-health-", suffix=".tmp", dir=root)
        probe_path = Path(raw_path)
        with os.fdopen(descriptor, "wb") as probe:
            probe.write(b"share-sentinel-storage-probe\n")
            probe.flush()
            os.fsync(probe.fileno())
        renamed_path = probe_path.with_suffix(".ready")
        os.replace(probe_path, renamed_path)
        probe_path = None
        _fsync_directory(root)
    finally:
        for candidate in (probe_path, renamed_path):
            if candidate is not None:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass


def artifact_storage_status(*, verify_write: bool = False) -> dict[str, Any]:
    root = _artifact_root()
    if not root.exists() or not root.is_dir():
        return {"ok": False, "state": "error", "reason": "path_missing"}
    try:
        next(root.iterdir(), None)
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            return {"ok": False, "state": "error", "reason": "path_not_accessible"}
        capacity = _capacity_status(root)
        if not capacity["capacity_ok"]:
            return {"ok": False, "state": "error", "reason": "low_free_space", **capacity}
        if verify_write:
            _write_probe(root)
    except OSError:
        return {"ok": False, "state": "error", "reason": "storage_io_error"}
    return {"ok": True, "state": "ok", "reason": None, **capacity}


def artifact_storage_ready() -> bool:
    return bool(artifact_storage_status()["ok"])


def _key_parts(key: str) -> tuple[str, ...]:
    pure_path = PurePosixPath(str(key or ""))
    if pure_path.is_absolute():
        raise ValueError("artifact key must be relative")
    parts = tuple(part for part in pure_path.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise ValueError("artifact key must remain inside artifact storage")
    return parts


def _artifact_path(key: str) -> Path:
    return _artifact_root().joinpath(*_key_parts(key))


def _validated_upload_id(upload_id: str) -> str:
    try:
        return str(uuid.UUID(str(upload_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("upload_id must be a UUID") from exc


def _multipart_path(key: str, upload_id: str) -> Path:
    return _artifact_root().joinpath(".multipart", *_key_parts(key), f"{_validated_upload_id(upload_id)}.part")


def upload_fileobj(fileobj, key: str, content_type: str | None = None) -> None:
    upload_id = create_multipart_upload(key, content_type)
    try:
        while True:
            chunk = fileobj.read(8 * 1024 * 1024)
            if not chunk:
                break
            upload_part(key, upload_id, 1, chunk)
        complete_multipart_upload(key, upload_id, [])
    except Exception:
        abort_multipart_upload(key, upload_id)
        raise


def create_multipart_upload(key: str, content_type: str | None = None) -> str:
    del content_type
    upload_id = str(uuid.uuid4())
    multipart_path = _multipart_path(key, upload_id)
    multipart_path.parent.mkdir(parents=True, exist_ok=True)
    with open(multipart_path, "xb"):
        pass
    return upload_id


def upload_part(key: str, upload_id: str, part_number: int, body: bytes) -> str:
    if part_number < 1:
        raise ValueError("part_number must be greater than zero")
    multipart_path = _multipart_path(key, upload_id)
    if not multipart_path.exists():
        raise FileNotFoundError(multipart_path)
    with open(multipart_path, "ab") as fp:
        fp.write(body)
    return hashlib.sha256(body).hexdigest()


def complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> None:
    del parts
    multipart_path = _multipart_path(key, upload_id)
    target_path = _artifact_path(key)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(multipart_path, "rb") as pending_artifact:
        os.fsync(pending_artifact.fileno())
    # Artifact keys are immutable. A hard-link publish is atomic and refuses
    # to replace an already referenced object when two completions race.
    os.link(multipart_path, target_path)
    multipart_path.unlink()
    _fsync_directory(target_path.parent)


def abort_multipart_upload(key: str, upload_id: str) -> None:
    multipart_path = _multipart_path(key, upload_id)
    if multipart_path.exists():
        multipart_path.unlink()


def get_object_stream(key: str):
    return open(_artifact_path(key), "rb")


def delete_object(key: str) -> None:
    artifact_path = _artifact_path(key)
    if artifact_path.exists():
        artifact_path.unlink()
        for parent in artifact_path.parents:
            if parent == _artifact_root():
                break
            try:
                parent.rmdir()
            except OSError:
                break
