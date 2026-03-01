import asyncio
from types import SimpleNamespace

import pytest

from app.routers import runs as runs_router


class _FakeFileReader:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def read(self, _size: int):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeUploadFile:
    def __init__(self, chunks: list[bytes]):
        self.file = _FakeFileReader(chunks)
        self.filename = "artifact.ndjson"
        self.content_type = "application/x-ndjson"


async def _run_upload(file_obj):
    request = SimpleNamespace(stream=lambda: (_ for _ in ()).throw(RuntimeError("stream should not be used")))
    return await runs_router._upload_artifact_stream(
        request=request,
        file=file_obj,
        key="projects/p/runs/r/artifact.ndjson",
        content_type="application/x-ndjson",
    )


def test_upload_stream_preserves_original_error_when_abort_fails(monkeypatch) -> None:
    aborted: list[tuple[str, str]] = []

    async def _fake_run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(runs_router, "run_in_threadpool", _fake_run_in_threadpool)
    monkeypatch.setattr(runs_router, "get_settings", lambda: SimpleNamespace(upload_chunk_bytes=8 * 1024 * 1024, upload_max_bytes=1024 * 1024))
    monkeypatch.setattr(runs_router, "create_multipart_upload", lambda *_args, **_kwargs: "upload-1")
    monkeypatch.setattr(runs_router, "upload_part", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("upload failed")))

    def _abort(key, upload_id):
        aborted.append((key, upload_id))
        raise RuntimeError("abort failed")

    monkeypatch.setattr(runs_router, "abort_multipart_upload", _abort)
    monkeypatch.setattr(runs_router, "complete_multipart_upload", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="upload failed"):
        asyncio.run(_run_upload(_FakeUploadFile([b"abc"])))

    assert aborted == [("projects/p/runs/r/artifact.ndjson", "upload-1")]


def test_upload_stream_aborts_and_raises_original_error(monkeypatch) -> None:
    aborted: list[tuple[str, str]] = []

    async def _fake_run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(runs_router, "run_in_threadpool", _fake_run_in_threadpool)
    monkeypatch.setattr(runs_router, "get_settings", lambda: SimpleNamespace(upload_chunk_bytes=8 * 1024 * 1024, upload_max_bytes=1024 * 1024))
    monkeypatch.setattr(runs_router, "create_multipart_upload", lambda *_args, **_kwargs: "upload-2")
    monkeypatch.setattr(runs_router, "upload_part", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("upload exploded")))
    monkeypatch.setattr(runs_router, "abort_multipart_upload", lambda key, upload_id: aborted.append((key, upload_id)))
    monkeypatch.setattr(runs_router, "complete_multipart_upload", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="upload exploded"):
        asyncio.run(_run_upload(_FakeUploadFile([b"xyz"])))

    assert aborted == [("projects/p/runs/r/artifact.ndjson", "upload-2")]
