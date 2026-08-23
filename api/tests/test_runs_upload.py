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


def test_upload_stream_aborts_when_request_task_is_cancelled(monkeypatch) -> None:
    aborted: list[tuple[str, str]] = []

    async def _fake_run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    class _CancelledRequest:
        async def stream(self):
            yield b'{"type":"run_meta"}\n'
            raise asyncio.CancelledError

    monkeypatch.setattr(runs_router, "run_in_threadpool", _fake_run_in_threadpool)
    monkeypatch.setattr(
        runs_router,
        "get_settings",
        lambda: SimpleNamespace(upload_chunk_bytes=8 * 1024 * 1024, upload_max_bytes=1024 * 1024),
    )
    monkeypatch.setattr(runs_router, "create_multipart_upload", lambda *_args, **_kwargs: "upload-cancelled")
    monkeypatch.setattr(runs_router, "abort_multipart_upload", lambda key, upload_id: aborted.append((key, upload_id)))
    monkeypatch.setattr(runs_router, "complete_multipart_upload", lambda *_args, **_kwargs: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runs_router._upload_artifact_stream(
                request=_CancelledRequest(),
                file=None,
                key="projects/p/runs/r/artifact.ndjson",
                content_type="application/x-ndjson",
            )
        )

    assert aborted == [("projects/p/runs/r/artifact.ndjson", "upload-cancelled")]


@pytest.mark.parametrize("chunks", [[b" " * 64, b"not-json"], [b" \r\n\t" * 20]])
def test_upload_stream_rejects_payloads_hidden_behind_leading_whitespace(monkeypatch, chunks) -> None:
    aborted: list[tuple[str, str]] = []

    async def _fake_run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(runs_router, "run_in_threadpool", _fake_run_in_threadpool)
    monkeypatch.setattr(
        runs_router,
        "get_settings",
        lambda: SimpleNamespace(upload_chunk_bytes=8 * 1024 * 1024, upload_max_bytes=1024 * 1024),
    )
    monkeypatch.setattr(runs_router, "create_multipart_upload", lambda *_args, **_kwargs: "upload-invalid")
    monkeypatch.setattr(runs_router, "abort_multipart_upload", lambda key, upload_id: aborted.append((key, upload_id)))
    monkeypatch.setattr(runs_router, "complete_multipart_upload", lambda *_args, **_kwargs: None)

    with pytest.raises(runs_router.HTTPException, match="does not look like JSON") as exc:
        asyncio.run(_run_upload(_FakeUploadFile(chunks)))

    assert exc.value.status_code == 415
    assert aborted == [("projects/p/runs/r/artifact.ndjson", "upload-invalid")]


def test_artifact_suffix_preserves_json_extensions() -> None:
    assert runs_router._artifact_suffix("application/json", "artifact.json") == ".json"
    assert runs_router._artifact_suffix("application/gzip", "artifact.json.gz") == ".json.gz"
    assert runs_router._artifact_suffix("application/x-ndjson", "artifact.ndjson") == ".ndjson"


def test_raw_artifact_filename_preserves_unambiguous_gzip_format() -> None:
    request = SimpleNamespace(headers={"x-artifact-filename": "scan.JSON.GZ"})

    filename = runs_router._raw_artifact_filename(request)

    assert filename == "scan.JSON.GZ"
    assert runs_router._artifact_suffix("application/gzip", filename) == ".json.gz"


@pytest.mark.parametrize(
    "filename",
    ["", "../artifact.json", r"folder\artifact.json", "artifact.json\n", "x" * 256],
)
def test_raw_artifact_filename_rejects_unsafe_or_ambiguous_values(filename: str) -> None:
    request = SimpleNamespace(headers={"x-artifact-filename": filename})

    with pytest.raises(runs_router.HTTPException, match="invalid x-artifact-filename header") as exc:
        runs_router._raw_artifact_filename(request)

    assert exc.value.status_code == 400


def test_new_artifact_keys_are_unique_and_keep_run_scope_and_suffix() -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()

    first = runs_router._new_artifact_key(project_id, run_id, ".json.gz")
    second = runs_router._new_artifact_key(project_id, run_id, ".json.gz")

    prefix = f"projects/{project_id}/runs/{run_id}/artifact-"
    assert first.startswith(prefix)
    assert first.endswith(".json.gz")
    assert second.startswith(prefix)
    assert second.endswith(".json.gz")
    assert first != second


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


def test_delete_superseded_artifact_only_removes_replaced_key(monkeypatch) -> None:
    deleted: list[str | None] = []
    monkeypatch.setattr(runs_router, "_delete_artifact_quietly", deleted.append)

    runs_router._delete_superseded_artifact("old.json", "new.ndjson")
    runs_router._delete_superseded_artifact("same.json", "same.json")
    runs_router._delete_superseded_artifact(None, "new.ndjson")

    assert deleted == ["old.json"]


def test_run_activity_includes_scheduled_ingest_retries() -> None:
    assert "INGEST_RETRY_SCHEDULED" in runs_router.RUN_ACTIVITY_ACTIONS


def test_enqueue_retries_run_blocking_redis_calls_off_event_loop(monkeypatch) -> None:
    threadpool_calls: list[tuple] = []
    enqueue_attempts: list[dict] = []

    def _enqueue(payload):
        enqueue_attempts.append(payload)
        if len(enqueue_attempts) < 3:
            raise RuntimeError("redis unavailable")
        return "1-0"

    async def _threadpool(func, *args):
        threadpool_calls.append((func, args))
        return func(*args)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(runs_router, "enqueue_ingest_job", _enqueue)
    monkeypatch.setattr(runs_router, "run_in_threadpool", _threadpool)
    monkeypatch.setattr(runs_router.asyncio, "sleep", _no_sleep)

    queued = asyncio.run(runs_router._enqueue_with_retries({"run_id": "run-1"}, retries=3))

    assert queued is True
    assert len(threadpool_calls) == 3
    assert enqueue_attempts == [{"run_id": "run-1"}] * 3


def test_upload_rate_limit_runs_blocking_redis_call_off_event_loop(monkeypatch) -> None:
    threadpool_calls: list[tuple[object, tuple, dict]] = []
    limiter_calls: list[tuple] = []

    def _check(*args, **kwargs):
        limiter_calls.append((args, kwargs))

    async def _threadpool(func, *args, **kwargs):
        threadpool_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    request = SimpleNamespace()
    monkeypatch.setattr(runs_router, "rate_limiter", SimpleNamespace(check=_check))
    monkeypatch.setattr(runs_router, "run_in_threadpool", _threadpool)

    asyncio.run(runs_router._check_upload_rate_limit(request, "actor-1"))

    assert len(threadpool_calls) == 1
    assert limiter_calls == [
        (
            (request, "artifact_upload"),
            {"limit": 30, "window_seconds": 60, "actor_key": "upload:actor-1"},
        )
    ]


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
    events: list[str] = []

    async def _upload(*_args, **_kwargs):
        assert events == ["rollback"]
        events.append("upload")
        return 24, "a" * 64

    monkeypatch.setattr(runs_router, "rate_limiter", SimpleNamespace(check=lambda *_args, **_kwargs: None))
    monkeypatch.setattr(runs_router, "_new_artifact_key", lambda *_args, **_kwargs: "new-artifact.ndjson")
    monkeypatch.setattr(runs_router, "_upload_artifact_stream", _upload)
    monkeypatch.setattr(runs_router, "_delete_artifact_quietly", lambda key: events.append(f"delete:{key}"))

    class _Db:
        def rollback(self):
            events.append("rollback")

        def refresh(self, *_args, **_kwargs):
            raise AssertionError("refresh should not happen when the run lock is unavailable")

    with pytest.raises(runs_router.HTTPException) as exc:
        asyncio.run(
            runs_router.upload_artifact(
                project_id=project_id,
                run_id=run_id,
                request=SimpleNamespace(headers={"content-type": "application/x-ndjson"}),
                file=None,
                db=_Db(),
                _=auth,
                auth=auth,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "run is currently ingesting"
    assert events == ["rollback", "upload", "rollback", "delete:new-artifact.ndjson"]


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
    events: list[str] = []

    async def _upload(*_args, **_kwargs):
        assert events == ["rollback"]
        events.append("upload")
        return 24, "a" * 64

    monkeypatch.setattr(runs_router, "_new_artifact_key", lambda *_args, **_kwargs: "new-artifact.ndjson")
    monkeypatch.setattr(runs_router, "_upload_artifact_stream", _upload)
    monkeypatch.setattr(runs_router, "_delete_artifact_quietly", lambda key: events.append(f"delete:{key}"))

    class _Db:
        def rollback(self):
            events.append("rollback")

        def refresh(self, refreshed_run):
            events.append("refresh")
            refreshed_run.status = runs_router.RunStatus.INGESTING

    with pytest.raises(runs_router.HTTPException) as exc:
        asyncio.run(
            runs_router.upload_artifact(
                project_id=project_id,
                run_id=run_id,
                request=SimpleNamespace(headers={"content-type": "application/x-ndjson"}),
                file=None,
                db=_Db(),
                _=auth,
                auth=auth,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "run state does not accept upload"
    assert events == ["rollback", "upload", "refresh", "rollback", "delete:new-artifact.ndjson"]


def test_upload_artifact_cancellation_after_stream_deletes_uncommitted_object(monkeypatch) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    auth = SimpleNamespace(user_id=uuid.uuid4(), token_id=None)
    run = SimpleNamespace(id=run_id, status=runs_router.RunStatus.PENDING_UPLOAD)
    events: list[str] = []

    async def _upload(*_args, **_kwargs):
        assert events == ["rollback"]
        events.append("upload")
        return 24, "a" * 64

    class _Db:
        def rollback(self):
            events.append("rollback")

    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "_get_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(runs_router, "_try_lock_run_for_mutation", lambda *_args, **_kwargs: (_ for _ in ()).throw(asyncio.CancelledError()))
    monkeypatch.setattr(runs_router, "_check_upload_rate_limit", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(runs_router, "_new_artifact_key", lambda *_args, **_kwargs: "new-artifact.ndjson")
    monkeypatch.setattr(runs_router, "_upload_artifact_stream", _upload)
    monkeypatch.setattr(runs_router, "_delete_artifact_quietly", lambda key: events.append(f"delete:{key}"))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runs_router.upload_artifact(
                project_id=project_id,
                run_id=run_id,
                request=SimpleNamespace(headers={"content-type": "application/x-ndjson"}),
                file=None,
                db=_Db(),
                _=auth,
                auth=auth,
            )
        )

    assert events == ["rollback", "upload", "rollback", "delete:new-artifact.ndjson"]


@pytest.mark.parametrize(
    ("previous_status", "previous_artifact_key"),
    [
        (runs_router.RunStatus.UPLOADED, "old-artifact.ndjson"),
        (runs_router.RunStatus.FAILED, None),
    ],
)
def test_replacement_upload_clears_partial_inventory_before_selecting_new_artifact(
    monkeypatch,
    previous_status,
    previous_artifact_key,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    auth = SimpleNamespace(user_id=uuid.uuid4(), token_id=None)
    run = SimpleNamespace(
        id=run_id,
        status=previous_status,
        artifact_key=previous_artifact_key,
        artifact_size=12,
        artifact_sha256="b" * 64,
        artifact_content_type="application/x-ndjson",
        ingest_progress={"line_offset": 10, "attempt_count": 1},
        summary={"items": 10},
    )
    events: list[object] = []

    class _Db:
        def refresh(self, _run):
            events.append("refresh")

        def add(self, _run):
            events.append("add")

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

    async def _upload(*_args, **_kwargs):
        return 24, "a" * 64

    async def _queued(*_args, **_kwargs):
        return True

    async def _rate_limit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runs_router, "require_project_role", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "_check_upload_rate_limit", _rate_limit)
    monkeypatch.setattr(runs_router, "_get_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(runs_router, "_try_lock_run_for_mutation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runs_router, "_new_artifact_key", lambda *_args, **_kwargs: "new-artifact.ndjson")
    monkeypatch.setattr(runs_router, "_upload_artifact_stream", _upload)
    monkeypatch.setattr(runs_router, "_clear_run_ingest_data", lambda *_args, **_kwargs: events.append("clear"))
    monkeypatch.setattr(runs_router, "_delete_superseded_artifact", lambda *_args: events.append("delete-old"))
    monkeypatch.setattr(runs_router, "write_audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs_router, "request_meta", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runs_router, "_enqueue_with_retries", _queued)
    monkeypatch.setattr(runs_router, "get_settings", lambda: SimpleNamespace(redis_stream_retries=1))

    result = asyncio.run(
        runs_router.upload_artifact(
            project_id=project_id,
            run_id=run_id,
            request=SimpleNamespace(headers={"content-type": "application/x-ndjson"}),
            file=None,
            db=_Db(),
            _=auth,
            auth=auth,
        )
    )

    assert result["artifact_key"] == "new-artifact.ndjson"
    assert run.artifact_key == "new-artifact.ndjson"
    assert events[0] == "rollback"
    assert events.index("clear") < events.index("commit") < events.index("delete-old")
