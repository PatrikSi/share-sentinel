import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


def _load_collector_module():
    module_path = Path(__file__).resolve().parents[1] / "share_sentinel_collector.py"
    spec = importlib.util.spec_from_file_location("share_sentinel_collector", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_compact_gzip_upload_uses_raw_stream_with_filename_contract(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.json.gz"
    artifact_bytes = os.urandom(2 * 1024 * 1024 + 17)
    artifact.write_bytes(artifact_bytes)
    captured = {}

    def _post(_url, **kwargs):
        stream = kwargs["data"]
        assert not isinstance(stream, (bytes, bytearray))
        chunks = []
        while chunk := stream.read(1024 * 1024):
            chunks.append(chunk)
        captured.update(headers=kwargs["headers"], chunks=chunks)
        return _FakeResponse(200, {})

    monkeypatch.setattr(collector.requests, "post", _post)

    collector._upload_artifact_once(
        "http://api/upload",
        {"Authorization": "Bearer token"},
        "application/gzip",
        str(artifact),
    )

    assert captured["headers"]["Content-Type"] == "application/gzip"
    assert captured["headers"]["X-Artifact-Filename"] == "artifact.json.gz"
    assert b"".join(captured["chunks"]) == artifact_bytes
    assert max(len(chunk) for chunk in captured["chunks"]) <= 1024 * 1024


def test_ndjson_gzip_upload_remains_raw_and_streamed(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson.gz"
    artifact.write_bytes(b"gzip-payload")
    captured = {}

    def _post(_url, **kwargs):
        captured.update(headers=kwargs["headers"], body=kwargs["data"].read())
        return _FakeResponse(200, {})

    monkeypatch.setattr(collector.requests, "post", _post)

    collector._upload_artifact_once(
        "http://api/upload",
        {"Authorization": "Bearer token"},
        "application/gzip",
        str(artifact),
    )

    assert captured["headers"]["Content-Type"] == "application/gzip"
    assert captured["headers"]["X-Artifact-Filename"] == "artifact.ndjson.gz"
    assert captured["body"] == b"gzip-payload"


def test_upload_filename_header_is_safe_and_preserves_exact_suffix() -> None:
    collector = _load_collector_module()

    filename = collector._artifact_upload_filename("/tmp/réport\n.JSON.GZ")

    assert filename == "r_port_.json.gz"
    assert filename.isascii()
    assert len(filename) <= 255


def test_upload_artifact_selects_gzip_contract_for_compact_gzip(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.json.gz"
    artifact.write_bytes(b"compressed-compact-json")
    captured = {}

    monkeypatch.setattr(collector.requests, "post", lambda *_args, **_kwargs: _FakeResponse(200, {}))

    def _upload_once(_url, _headers, content_type, artifact_path, *, timeout):
        captured.update(
            content_type=content_type,
            artifact_path=artifact_path,
            timeout=timeout,
        )
        return _FakeResponse(
            200,
            {
                "queued": True,
                "artifact_sha256": collector._sha256_file(str(artifact)),
            },
        )

    monkeypatch.setattr(collector, "_upload_artifact_once", _upload_once)
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
        upload_timeout=30.0,
        upload_attempts=1,
    )

    assert collector.upload_artifact(args, "run-id", str(artifact), hosts=[]) == "accepted"
    assert captured == {
        "content_type": "application/gzip",
        "artifact_path": str(artifact),
        "timeout": (10.0, 30.0),
    }


def test_post_with_retries_retries_retriable_status(monkeypatch) -> None:
    collector = _load_collector_module()
    calls: list[int] = []
    responses = [_FakeResponse(503), _FakeResponse(200)]

    monkeypatch.setattr(collector.time, "sleep", lambda *_args, **_kwargs: None)

    def _request():
        calls.append(1)
        return responses.pop(0)

    result = collector._post_with_retries(_request, max_attempts=3)

    assert result.status_code == 200
    assert len(calls) == 2


def test_upload_artifact_reconciles_ambiguous_conflict_by_status_and_digest(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")

    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
    )

    create_resp = _FakeResponse(409, {"detail": "run already exists"})
    upload_resp = _FakeResponse(409, {"detail": "run is currently ingesting"})
    run_resp = _FakeResponse(
        200,
        {
            "status": "INGESTING",
            "artifact_sha256": collector._sha256_file(str(artifact)),
        },
    )
    responses = [create_resp, upload_resp, run_resp]

    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    result = collector.upload_artifact(args, "run-id", str(artifact), hosts=["10.0.0.5"])

    assert result == "recovered"


def test_upload_artifact_reconciles_final_connection_error_by_status_and_digest(
    monkeypatch, tmp_path
) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    artifact_sha256 = collector._sha256_file(str(artifact))
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
        upload_attempts=3,
    )
    outcomes = [
        _FakeResponse(200, {}),
        requests.ConnectionError("response lost after request body was sent"),
        _FakeResponse(200, {"status": "UPLOADED", "artifact_sha256": artifact_sha256}),
    ]

    def _request_with_retries(_request_fn, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(collector, "_post_with_retries", _request_with_retries)

    result = collector.upload_artifact(args, "run-id", str(artifact), hosts=[])

    assert result == "recovered"
    assert outcomes == []


@pytest.mark.parametrize("transient_status", sorted({408, 429, 500, 502, 503, 504}))
def test_upload_artifact_reconciles_final_transient_http_response_by_status_and_digest(
    monkeypatch, tmp_path, transient_status
) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    artifact_sha256 = collector._sha256_file(str(artifact))
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
        upload_attempts=3,
    )
    outcomes = [
        _FakeResponse(200, {}),
        _FakeResponse(transient_status, {"detail": "transient upload failure"}),
        _FakeResponse(200, {"status": "INGESTING", "artifact_sha256": artifact_sha256}),
    ]

    monkeypatch.setattr(
        collector,
        "_post_with_retries",
        lambda _request_fn, **_kwargs: outcomes.pop(0),
    )

    result = collector.upload_artifact(args, "run-id", str(artifact), hosts=[])

    assert result == "recovered"
    assert outcomes == []


def test_upload_artifact_reports_ambiguous_outcome_when_final_transient_http_cannot_reconcile(
    monkeypatch, tmp_path
) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
        upload_attempts=3,
    )
    outcomes = [
        _FakeResponse(200, {}),
        _FakeResponse(503, {"detail": "transient upload failure"}),
        requests.Timeout("run status timed out"),
    ]

    def _request_with_retries(_request_fn, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(collector, "_post_with_retries", _request_with_retries)

    with pytest.raises(RuntimeError, match="upload outcome is ambiguous") as exc_info:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])

    assert "transient HTTP 503" in str(exc_info.value)
    assert "run reconciliation failed" in str(exc_info.value)
    assert outcomes == []


def test_upload_artifact_reports_ambiguous_outcome_when_final_transient_http_digest_differs(
    monkeypatch, tmp_path
) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
        upload_attempts=3,
    )
    outcomes = [
        _FakeResponse(200, {}),
        _FakeResponse(504, {"detail": "gateway response lost"}),
        _FakeResponse(200, {"status": "COMPLETE", "artifact_sha256": "0" * 64}),
    ]

    monkeypatch.setattr(
        collector,
        "_post_with_retries",
        lambda _request_fn, **_kwargs: outcomes.pop(0),
    )

    with pytest.raises(RuntimeError, match="upload outcome is ambiguous") as exc_info:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])

    assert "transient HTTP 504" in str(exc_info.value)
    assert "sha256_match=False" in str(exc_info.value)
    assert outcomes == []


def test_upload_artifact_reports_ambiguous_outcome_when_reconciliation_is_unavailable(
    monkeypatch, tmp_path
) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
        upload_attempts=3,
    )
    outcomes = [
        _FakeResponse(200, {}),
        requests.Timeout("response timed out after request body was sent"),
        requests.ConnectionError("run status unavailable"),
    ]

    def _request_with_retries(_request_fn, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(collector, "_post_with_retries", _request_with_retries)

    try:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])
    except RuntimeError as exc:
        message = str(exc)
        assert "upload outcome is ambiguous" in message
        assert "run reconciliation failed" in message
        assert "run status unavailable" in message
    else:
        raise AssertionError("expected an unavailable reconciliation to remain ambiguous")
    assert outcomes == []


def test_upload_artifact_reports_ambiguous_outcome_when_final_error_digest_differs(
    monkeypatch, tmp_path
) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
        upload_attempts=3,
    )
    outcomes = [
        _FakeResponse(200, {}),
        requests.exceptions.ChunkedEncodingError("response ended early"),
        _FakeResponse(200, {"status": "COMPLETE", "artifact_sha256": "0" * 64}),
    ]

    def _request_with_retries(_request_fn, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(collector, "_post_with_retries", _request_with_retries)

    try:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])
    except RuntimeError as exc:
        message = str(exc)
        assert "upload outcome is ambiguous" in message
        assert "does not confirm the same artifact" in message
        assert "sha256_match=False" in message
    else:
        raise AssertionError("expected a digest mismatch to remain ambiguous")
    assert outcomes == []


def test_upload_artifact_rejects_ambiguous_conflict_when_digest_differs(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
    )
    responses = [
        _FakeResponse(200, {}),
        _FakeResponse(409, {"detail": "run state does not accept upload"}),
        _FakeResponse(200, {"status": "COMPLETE", "artifact_sha256": "0" * 64}),
    ]
    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    try:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])
    except RuntimeError as exc:
        assert "does not confirm the same artifact" in str(exc)
        return
    raise AssertionError("expected digest mismatch to keep the upload outcome ambiguous")


def test_upload_artifact_rejects_mismatched_digest_in_success_response(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")
    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
    )
    responses = [
        _FakeResponse(200, {}),
        _FakeResponse(200, {"queued": True, "artifact_sha256": "f" * 64}),
    ]
    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    try:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])
    except RuntimeError as exc:
        assert "digest does not match" in str(exc)
        return
    raise AssertionError("expected mismatched upload response digest to fail")


def test_upload_artifact_raises_for_non_idempotent_conflict(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")

    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
    )

    create_resp = _FakeResponse(200, {})
    upload_resp = _FakeResponse(409, {"detail": "other conflict"})
    responses = [create_resp, upload_resp]

    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    try:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])
    except requests.HTTPError:
        return
    raise AssertionError("expected HTTPError for non-idempotent upload conflict")


def test_upload_artifact_raises_for_non_idempotent_create_conflict(monkeypatch, tmp_path) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")

    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
    )

    create_resp = _FakeResponse(409, {"detail": "project is locked"})
    upload_resp = _FakeResponse(200, {})
    responses = [create_resp, upload_resp]

    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    try:
        collector.upload_artifact(args, "run-id", str(artifact), hosts=[])
    except requests.HTTPError:
        return
    raise AssertionError("expected HTTPError for non-idempotent create conflict")


def test_upload_artifact_warns_when_queue_fallback_is_used(monkeypatch, tmp_path, capsys) -> None:
    collector = _load_collector_module()
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_text('{"type":"run_meta"}\n', encoding="utf-8")

    args = SimpleNamespace(
        upload=True,
        api_base="http://api",
        project_id="project-id",
        api_token="token-value",
        run_name="run-name",
        cidr=[],
    )

    create_resp = _FakeResponse(200, {})
    upload_resp = _FakeResponse(
        200,
        {
            "ok": True,
            "queued": False,
            "artifact_sha256": collector._sha256_file(str(artifact)),
        },
    )
    responses = [create_resp, upload_resp]

    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    collector.upload_artifact(args, "run-id", str(artifact), hosts=[])

    captured = capsys.readouterr()
    assert "upload warning: artifact stored" in captured.err
