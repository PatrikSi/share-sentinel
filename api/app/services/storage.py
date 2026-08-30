from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import get_settings

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported container runtime is POSIX.
    fcntl = None  # type: ignore[assignment]


ARTIFACT_CAPACITY_LOCK_FILE = ".share-sentinel-capacity.lock"
ARTIFACT_CAPACITY_LOCK_TIMEOUT_SECONDS = 10.0


class ArtifactStorageUnavailableError(OSError):
    """Artifact storage cannot safely accept more upload data."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


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
    linked_path: Path | None = None
    renamed_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=".share-sentinel-health-", suffix=".tmp", dir=root)
        probe_path = Path(raw_path)
        with os.fdopen(descriptor, "wb") as probe:
            probe.write(b"share-sentinel-storage-probe\n")
            probe.flush()
            os.fsync(probe.fileno())
        linked_path = probe_path.with_suffix(".linked")
        os.link(probe_path, linked_path)
        try:
            os.link(probe_path, linked_path)
        except FileExistsError:
            pass
        else:  # pragma: no cover - a filesystem violating link semantics is exceptional.
            raise OSError("artifact storage replaced an existing hard-link target")
        renamed_path = probe_path.with_suffix(".ready")
        os.replace(linked_path, renamed_path)
        linked_path = None
        probe_path.unlink()
        probe_path = None
        _fsync_directory(root)
    finally:
        for candidate in (probe_path, linked_path, renamed_path):
            if candidate is not None:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            _fsync_directory(root)
        except OSError:
            pass


def artifact_storage_status(*, verify_write: bool = False) -> dict[str, Any]:
    root = _artifact_root()
    if not root.exists() or not root.is_dir() or root.is_symlink():
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


def require_artifact_upload_capacity(*, additional_bytes: int = 0) -> None:
    """Fail before or between upload parts when storage loses headroom.

    This is deliberately rechecked while streaming. A preflight-only check
    cannot account for concurrent uploads or another process consuming the
    shared filesystem after request admission.
    """

    status = artifact_storage_status()
    if not status.get("ok"):
        raise ArtifactStorageUnavailableError(str(status.get("reason") or "storage_unavailable"))
    expected = max(0, int(additional_bytes))
    if not expected:
        return
    free_bytes = max(0, int(status.get("free_bytes") or 0))
    total_bytes = max(0, int(status.get("total_bytes") or 0))
    minimum_free_bytes = max(0, int(status.get("minimum_free_bytes") or 0))
    minimum_free_percent = max(0.0, float(status.get("minimum_free_percent") or 0.0))
    projected_free = free_bytes - expected
    projected_percent = (projected_free / total_bytes * 100.0) if total_bytes else 0.0
    if projected_free < minimum_free_bytes or projected_percent < minimum_free_percent:
        raise ArtifactStorageUnavailableError("insufficient_capacity_for_upload")


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


def _multipart_parts(key: str, upload_id: str) -> tuple[str, ...]:
    return (".multipart", *_key_parts(key), f"{_validated_upload_id(upload_id)}.part")


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_storage_parent(parts: tuple[str, ...], *, create: bool) -> int:
    """Open a storage directory without following any path component."""

    current_fd = os.open(_artifact_root(), _directory_open_flags())
    try:
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise OSError("artifact storage root is not a directory")
        for part in parts:
            created = False
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                else:
                    created = True
            if created:
                # Persist every new directory entry, not only the deepest
                # artifact directory. Otherwise a successful publish can be
                # lost after power failure when one of its ancestors was
                # still only present in the filesystem cache.
                os.fsync(current_fd)
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
                raise OSError("artifact storage path component is not a directory")
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_storage_regular(parts: tuple[str, ...], flags: int, *, mode: int = 0o600):
    parent_fd = _open_storage_parent(parts[:-1], create=bool(flags & os.O_CREAT))
    try:
        descriptor = os.open(
            parts[-1],
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            mode,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("artifact path is not a regular file")
        file_mode = "ab" if flags & (os.O_WRONLY | os.O_RDWR) else "rb"
        return os.fdopen(descriptor, file_mode)
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def _artifact_capacity_lock():
    """Serialize capacity admission and writes across API replicas.

    POSIX advisory locks are released by the kernel if a process exits. The
    shared filesystem used by multiple replicas must therefore support flock
    semantics in addition to the link/fsync contract checked by deep health.
    """

    if fcntl is None:
        raise ArtifactStorageUnavailableError("capacity_lock_unsupported")
    with _open_storage_regular(
        (ARTIFACT_CAPACITY_LOCK_FILE,),
        os.O_CREAT | os.O_RDWR,
    ) as lock_file:
        deadline = time.monotonic() + ARTIFACT_CAPACITY_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ArtifactStorageUnavailableError("capacity_lock_timeout") from exc
                time.sleep(0.05)
            except OSError as exc:
                raise ArtifactStorageUnavailableError("capacity_lock_unavailable") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _remove_empty_storage_parents(parts: tuple[str, ...], *, preserve: int = 0) -> None:
    for depth in range(len(parts), preserve, -1):
        try:
            parent_fd = _open_storage_parent(parts[: depth - 1], create=False)
        except OSError:
            return
        try:
            os.rmdir(parts[depth - 1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError:
            return
        finally:
            os.close(parent_fd)


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
    require_artifact_upload_capacity()
    upload_id = str(uuid.uuid4())
    with _open_storage_regular(_multipart_parts(key, upload_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY):
        pass
    return upload_id


def upload_part(key: str, upload_id: str, part_number: int, body: bytes) -> str:
    if part_number < 1:
        raise ValueError("part_number must be greater than zero")
    # Keep the projected-capacity check and allocation in one shared critical
    # section. Without this, every replica can admit a full part from the same
    # stale free-space observation and collectively cross the configured floor.
    with _artifact_capacity_lock():
        require_artifact_upload_capacity(additional_bytes=len(body))
        with _open_storage_regular(_multipart_parts(key, upload_id), os.O_APPEND | os.O_WRONLY) as fp:
            fp.write(body)
    return hashlib.sha256(body).hexdigest()


def complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> None:
    del parts
    multipart_parts = _multipart_parts(key, upload_id)
    target_parts = _key_parts(key)
    with _open_storage_regular(multipart_parts, os.O_RDONLY) as pending_artifact:
        os.fsync(pending_artifact.fileno())
    # Artifact keys are immutable. A hard-link publish is atomic and refuses
    # to replace an already referenced object when two completions race.
    multipart_parent_fd = _open_storage_parent(multipart_parts[:-1], create=False)
    target_parent_fd = _open_storage_parent(target_parts[:-1], create=True)
    try:
        os.link(
            multipart_parts[-1],
            target_parts[-1],
            src_dir_fd=multipart_parent_fd,
            dst_dir_fd=target_parent_fd,
            follow_symlinks=False,
        )
        os.unlink(multipart_parts[-1], dir_fd=multipart_parent_fd)
        os.fsync(target_parent_fd)
        os.fsync(multipart_parent_fd)
    finally:
        os.close(target_parent_fd)
        os.close(multipart_parent_fd)
    _remove_empty_storage_parents(multipart_parts[:-1], preserve=1)


def abort_multipart_upload(key: str, upload_id: str) -> None:
    parts = _multipart_parts(key, upload_id)
    try:
        parent_fd = _open_storage_parent(parts[:-1], create=False)
    except FileNotFoundError:
        return
    try:
        os.unlink(parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        return
    finally:
        os.close(parent_fd)
    _remove_empty_storage_parents(parts[:-1], preserve=1)


def get_object_stream(key: str):
    return _open_storage_regular(_key_parts(key), os.O_RDONLY)


def delete_object(key: str) -> None:
    parts = _key_parts(key)
    try:
        parent_fd = _open_storage_parent(parts[:-1], create=False)
    except FileNotFoundError:
        return
    try:
        os.unlink(parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        return
    finally:
        os.close(parent_fd)
    _remove_empty_storage_parents(parts[:-1])
