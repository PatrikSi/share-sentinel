import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def reconcile_module():
    script_path = Path(__file__).parents[2] / "scripts" / "reconcile-artifacts.py"
    spec = importlib.util.spec_from_file_location("reconcile_artifacts_tool", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _old(path: Path) -> None:
    timestamp = (datetime.now(tz=UTC) - timedelta(hours=48)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_scan_candidates_separates_references_orphans_and_multipart(tmp_path, reconcile_module) -> None:
    referenced = tmp_path / "projects" / "p" / "runs" / "r" / "artifact.ndjson"
    referenced.parent.mkdir(parents=True)
    referenced.write_bytes(b"referenced")
    orphan = tmp_path / "projects" / "p" / "runs" / "old" / "artifact.ndjson"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    _old(orphan)
    multipart = tmp_path / ".multipart" / "projects" / "p" / "artifact.ndjson" / "id.part"
    multipart.parent.mkdir(parents=True)
    multipart.write_bytes(b"partial")
    _old(multipart)
    recent = tmp_path / "recent.ndjson"
    recent.write_bytes(b"pending commit")

    orphans, multipart_files, missing, recent_count = reconcile_module.scan_candidates(
        tmp_path,
        {"projects/p/runs/r/artifact.ndjson", "missing.ndjson"},
        cutoff=datetime.now(tz=UTC) - timedelta(hours=24),
    )

    assert [item.key for item in orphans] == ["projects/p/runs/old/artifact.ndjson"]
    assert [item.key for item in multipart_files] == [
        ".multipart/projects/p/artifact.ndjson/id.part"
    ]
    assert missing == ["missing.ndjson"]
    assert recent_count == 1


@pytest.mark.parametrize("key", ["../outside", "/absolute", ""])
def test_safe_key_rejects_unsafe_database_values(reconcile_module, key) -> None:
    with pytest.raises(ValueError, match="unsafe artifact key"):
        reconcile_module._safe_key(key)


def test_delete_candidates_rechecks_database_reference(tmp_path, reconcile_module) -> None:
    first = tmp_path / "first.ndjson"
    second = tmp_path / "second.ndjson"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    now = datetime.now(tz=UTC)
    candidates = [
        reconcile_module.Candidate("orphan", "first.ndjson", first, 5, now),
        reconcile_module.Candidate("orphan", "second.ndjson", second, 6, now),
    ]

    class _Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class _Connection:
        def __init__(self):
            self.audit = []
            self.commits = 0

        def execute(self, query, params=None):
            if "SELECT 1" in query:
                return _Result((1,)) if params == ("second.ndjson",) else _Result()
            self.audit.append((query, params))
            return _Result()

        def commit(self):
            self.commits += 1

    connection = _Connection()

    deleted, skipped = reconcile_module.delete_candidates(connection, candidates, limit=10)

    assert deleted == ["first.ndjson"]
    assert skipped == ["second.ndjson"]
    assert not first.exists()
    assert second.exists()
    assert connection.commits == 2
    assert len(connection.audit) == 2
