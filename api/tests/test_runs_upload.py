import asyncio
import uuid
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
        asyncio.run(_run_upload(_FakeUploadFile([b'{\"type\":\"run_meta\"}'])))

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
        asyncio.run(_run_upload(_FakeUploadFile([b'{\"type\":\"run_meta\"}'])))

    assert aborted == [("projects/p/runs/r/artifact.ndjson", "upload-2")]


def test_artifact_suffix_preserves_json_extensions() -> None:
    assert runs_router._artifact_suffix("application/json", "artifact.json") == ".json"
    assert runs_router._artifact_suffix("application/gzip", "artifact.json.gz") == ".json.gz"
    assert runs_router._artifact_suffix("application/x-ndjson", "artifact.ndjson") == ".ndjson"


def test_validate_artifact_upload_headers_rejects_unknown_filename() -> None:
    with pytest.raises(runs_router.HTTPException, match="unsupported artifact filename") as exc:
        runs_router._validate_artifact_upload_headers("application/json", "artifact.exe")

    assert exc.value.status_code == 415


def test_validate_artifact_signature_rejects_invalid_gzip_magic() -> None:
    with pytest.raises(runs_router.HTTPException, match="does not match gzip payload") as exc:
        runs_router._validate_artifact_signature("gzip", b"not-gzip", final=True)

    assert exc.value.status_code == 415


def test_clear_run_ingest_data_resets_summary_and_progress() -> None:
    class _FakeDb:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def execute(self, statement):
            self.calls.append(statement)

    run = SimpleNamespace(
        id=uuid.uuid4(),
        summary={"endpoints": 2, "resources": 3, "items": 5, "errors": 1},
        ingest_progress={"line_offset": 42, "last_error": "boom"},
    )
    fake_db = _FakeDb()

    runs_router._clear_run_ingest_data(fake_db, run)

    assert len(fake_db.calls) == 4
    assert run.summary == runs_router.EMPTY_RUN_SUMMARY
    assert run.ingest_progress == {"line_offset": 0}


def test_delete_artifact_quietly_ignores_missing_files(monkeypatch) -> None:
    monkeypatch.setattr(runs_router, "delete_object", lambda _key: (_ for _ in ()).throw(FileNotFoundError("gone")))

    runs_router._delete_artifact_quietly("projects/p/runs/r/artifact.ndjson")


def test_try_lock_run_for_mutation_returns_boolean() -> None:
    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar(self):
            return self._value

    class _Db:
        def __init__(self, value):
            self._value = value

        def execute(self, _statement, _params):
            return _Result(self._value)

    run_id = uuid.uuid4()
    assert runs_router._try_lock_run_for_mutation(_Db(True), run_id) is True
    assert runs_router._try_lock_run_for_mutation(_Db(False), run_id) is False


def test_delete_run_rejects_locked_ingesting_run(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    auth = SimpleNamespace(user_id=uuid.uuid4(), token_id=None)
    run = SimpleNamespace(id=run_id, artifact_key="projects/p/runs/r/artifact.ndjson")

    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "_get_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(runs_router, "_try_lock_run_for_mutation", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(runs_router, "request_meta", lambda _request: {})

    class _Db:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("delete should not be attempted when the run lock is unavailable")

    with pytest.raises(runs_router.HTTPException) as exc:
        runs_router.delete_run(
            project_id=project_id,
            run_id=run_id,
            request=SimpleNamespace(),
            db=_Db(),
            _=auth,
            auth=auth,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "run is currently ingesting"


def test_upload_artifact_rejects_locked_run(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    auth = SimpleNamespace(user_id=uuid.uuid4(), token_id=None)
    run = SimpleNamespace(
        id=run_id,
        status=runs_router.RunStatus.PENDING_UPLOAD,
        artifact_key=None,
        artifact_size=None,
        artifact_sha256=None,
        artifact_content_type=None,
        ingest_progress={"line_offset": 0},
    )

    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "_get_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(runs_router, "_try_lock_run_for_mutation", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(runs_router, "rate_limiter", SimpleNamespace(check=lambda *_args, **_kwargs: None))

    class _Db:
        def refresh(self, *_args, **_kwargs):
            raise AssertionError("refresh should not happen when the run lock is unavailable")

    with pytest.raises(runs_router.HTTPException) as exc:
        asyncio.run(
            runs_router.upload_artifact(
                project_id=project_id,
                run_id=run_id,
                request=SimpleNamespace(headers={}),
                file=None,
                db=_Db(),
                _=auth,
                auth=auth,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "run is currently ingesting"


def test_upload_artifact_rechecks_status_after_lock(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    auth = SimpleNamespace(user_id=uuid.uuid4(), token_id=None)
    run = SimpleNamespace(
        id=run_id,
        status=runs_router.RunStatus.PENDING_UPLOAD,
        artifact_key=None,
        artifact_size=None,
        artifact_sha256=None,
        artifact_content_type=None,
        ingest_progress={"line_offset": 0},
    )

    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "_get_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(runs_router, "_try_lock_run_for_mutation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runs_router, "rate_limiter", SimpleNamespace(check=lambda *_args, **_kwargs: None))
    monkeypatch.setattr(runs_router, "get_settings", lambda: SimpleNamespace(redis_stream_retries=1))
    monkeypatch.setattr(
        runs_router,
        "_upload_artifact_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("upload should not start after status changes")),
    )

    class _Db:
        def refresh(self, refreshed_run):
            refreshed_run.status = runs_router.RunStatus.INGESTING

    with pytest.raises(runs_router.HTTPException) as exc:
        asyncio.run(
            runs_router.upload_artifact(
                project_id=project_id,
                run_id=run_id,
                request=SimpleNamespace(headers={}),
                file=None,
                db=_Db(),
                _=auth,
                auth=auth,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "run state does not accept upload"
