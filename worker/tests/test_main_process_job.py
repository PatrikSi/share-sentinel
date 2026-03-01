from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker import main


def test_process_job_returns_early_for_invalid_run_id(monkeypatch) -> None:
    calls: list[str] = []

    def _unexpected_connect(*_args, **_kwargs):
        calls.append("connect")
        raise AssertionError("process_job should not connect for invalid run_id")

    monkeypatch.setattr(main.psycopg, "connect", _unexpected_connect)
    main.process_job({"run_id": "not-a-uuid", "project_id": "also-not-a-uuid"})
    assert calls == []
