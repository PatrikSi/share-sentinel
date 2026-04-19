import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_upload_artifact_allows_idempotent_conflicts(monkeypatch, tmp_path) -> None:
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
    upload_resp = _FakeResponse(409, {"detail": "run state does not accept upload"})
    responses = [create_resp, upload_resp]

    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    collector.upload_artifact(args, "run-id", str(artifact), hosts=["10.0.0.5"])


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
    upload_resp = _FakeResponse(200, {"ok": True, "queued": False})
    responses = [create_resp, upload_resp]

    monkeypatch.setattr(collector, "_post_with_retries", lambda request_fn, **_kwargs: responses.pop(0))

    collector.upload_artifact(args, "run-id", str(artifact), hosts=[])

    captured = capsys.readouterr()
    assert "upload warning: artifact stored" in captured.err
