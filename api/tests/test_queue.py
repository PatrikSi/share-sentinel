from types import SimpleNamespace

from app.services import queue


class _FakeRedis:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def xadd(self, stream_name, payload, **kwargs):
        self.calls.append({"stream_name": stream_name, "payload": payload, "kwargs": kwargs})
        return "1-0"


def test_enqueue_ingest_job_trims_by_default(monkeypatch) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(queue, "_redis", fake_redis)
    monkeypatch.setattr(queue, "get_settings", lambda: SimpleNamespace(redis_stream_maxlen=250000))

    message_id = queue.enqueue_ingest_job({"run_id": "abc", "meta": {"a": 1}})

    assert message_id == "1-0"
    assert fake_redis.calls == [
        {
            "stream_name": queue.STREAM_NAME,
            "payload": {"run_id": "abc", "meta": '{"a": 1}'},
            "kwargs": {"maxlen": 250000, "approximate": True},
        }
    ]


def test_enqueue_ingest_job_applies_configured_stream_trim(monkeypatch) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(queue, "_redis", fake_redis)
    monkeypatch.setattr(queue, "get_settings", lambda: SimpleNamespace(redis_stream_maxlen=250000))

    queue.enqueue_ingest_job({"run_id": "abc"})

    assert fake_redis.calls == [
        {
            "stream_name": queue.STREAM_NAME,
            "payload": {"run_id": "abc"},
            "kwargs": {"maxlen": 250000, "approximate": True},
        }
    ]
