import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def reconcile_module():
    script_path = Path(__file__).parents[1] / "app" / "maintenance" / "reconcile_artifacts.py"
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
    capacity_lock = tmp_path / ".share-sentinel-capacity.lock"
    capacity_lock.write_bytes(b"")
    _old(capacity_lock)

    orphans, multipart_files, missing, recent_count = reconcile_module.scan_candidates(
        tmp_path,
        {"projects/p/runs/r/artifact.ndjson", "missing.ndjson"},
        cutoff=datetime.now(tz=UTC) - timedelta(hours=24),
    )

    assert [item.key for item in orphans] == ["projects/p/runs/old/artifact.ndjson"]
    assert [item.key for item in multipart_files] == [".multipart/projects/p/artifact.ndjson/id.part"]
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
    first_candidate = reconcile_module._file_candidate(tmp_path, first, "orphan")
    second_candidate = reconcile_module._file_candidate(tmp_path, second, "orphan")
    assert first_candidate is not None
    assert second_candidate is not None
    candidates = [
        first_candidate,
        second_candidate,
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

    deleted, skipped = reconcile_module.delete_candidates(
        connection,
        candidates,
        root=tmp_path,
        limit=10,
    )

    assert deleted == ["first.ndjson"]
    assert skipped == ["second.ndjson"]
    assert not first.exists()
    assert second.exists()
    assert connection.commits == 2
    assert len(connection.audit) == 2


def test_secure_delete_rejects_swapped_symlink_parent(tmp_path, reconcile_module) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "projects" / "p" / "artifact.ndjson"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"original")
    candidate = reconcile_module._file_candidate(root, artifact, "orphan")
    assert candidate is not None

    preserved_tree = tmp_path / "preserved-projects"
    (root / "projects").rename(preserved_tree)
    outside = tmp_path / "outside"
    outside_artifact = outside / "p" / "artifact.ndjson"
    outside_artifact.parent.mkdir(parents=True)
    outside_artifact.write_bytes(b"outside")
    (root / "projects").symlink_to(outside, target_is_directory=True)

    with pytest.raises(reconcile_module.CandidateChangedError):
        reconcile_module._unlink_candidate(root, candidate)

    assert (preserved_tree / "p" / "artifact.ndjson").read_bytes() == b"original"
    assert outside_artifact.read_bytes() == b"outside"


def test_secure_delete_rejects_replaced_terminal_file(tmp_path, reconcile_module) -> None:
    artifact = tmp_path / "artifact.ndjson"
    artifact.write_bytes(b"original")
    candidate = reconcile_module._file_candidate(tmp_path, artifact, "orphan")
    assert candidate is not None
    artifact.unlink()
    artifact.write_bytes(b"replacement")

    with pytest.raises(reconcile_module.CandidateChangedError, match="changed after scan"):
        reconcile_module._unlink_candidate(tmp_path, candidate)

    assert artifact.read_bytes() == b"replacement"
