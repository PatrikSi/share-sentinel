import redis
from app.services import operational_metrics


class _Result:
    def __init__(self, *, rows=None, scalar_value=0):
        self._rows = rows or []
        self._scalar = scalar_value

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _Db:
    def __init__(self):
        self.calls = 0

    def execute(self, _query):
        self.calls += 1
        if self.calls == 1:
            return _Result(rows=[("COMPLETE", 4), ("UPLOADED", 2)])
        if self.calls == 2:
            return _Result(rows=[("complete", 3), ("queued", 1)])
        if self.calls == 3:
            return _Result(scalar_value=12.5)
        return _Result(scalar_value=7)


class _Redis:
    def xinfo_groups(self, _stream):
        return [{"pending": 2, "lag": 3}, {"pending": 1, "lag": None}]

    def xlen(self, _stream):
        return 20


def test_render_operational_metrics_covers_jobs_queue_and_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        operational_metrics.storage,
        "artifact_storage_status",
        lambda: {
            "ok": True,
            "total_bytes": 100,
            "used_bytes": 40,
            "free_bytes": 60,
            "free_percent": 60,
        },
    )

    payload = operational_metrics.render_operational_metrics(_Db(), _Redis())

    assert 'share_sentinel_runs{state="complete"} 4' in payload
    assert 'share_sentinel_runs{state="uploaded"} 2' in payload
    assert 'share_sentinel_comparisons{state="queued"} 1' in payload
    assert 'share_sentinel_oldest_active_job_age_seconds{job_type="ingest"} 12.5' in payload
    assert 'share_sentinel_worker_stream_messages{state="pending"} 3' in payload
    assert 'share_sentinel_worker_stream_messages{state="lag"} 3' in payload
    assert 'share_sentinel_artifact_storage_bytes{state="free"} 60' in payload
    assert 'share_sentinel_operational_metrics_collection_success{component="database"} 1' in payload


def test_render_operational_metrics_degrades_without_failing_scrape(monkeypatch) -> None:
    class _BrokenDb:
        def execute(self, _query):
            raise RuntimeError("database unavailable")

    class _BrokenRedis:
        def xinfo_groups(self, _stream):
            raise redis.RedisError("redis unavailable")

    monkeypatch.setattr(
        operational_metrics.storage,
        "artifact_storage_status",
        lambda: (_ for _ in ()).throw(OSError("storage unavailable")),
    )

    payload = operational_metrics.render_operational_metrics(_BrokenDb(), _BrokenRedis())

    for component in ("database", "redis", "artifact_storage"):
        assert (
            f'share_sentinel_operational_metrics_collection_success{{component="{component}"}} 0'
            in payload
        )
