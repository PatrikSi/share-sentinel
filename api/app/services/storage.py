from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path, PurePosixPath

from app.config import get_settings


def _artifact_root() -> Path:
    return Path(get_settings().artifact_storage_path)


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


def _multipart_path(key: str, upload_id: str) -> Path:
    return _artifact_root().joinpath(".multipart", *_key_parts(key), f"{upload_id}.part")


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
    del part_number
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
    os.replace(multipart_path, target_path)


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
