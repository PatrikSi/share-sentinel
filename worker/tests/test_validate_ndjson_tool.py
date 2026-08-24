import gzip
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def validator_module():
    script_path = Path(__file__).parents[2] / "scripts" / "validate-ndjson.py"
    spec = importlib.util.spec_from_file_location("validate_ndjson_tool", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _records() -> list[dict[str, object]]:
    run_id = str(uuid.uuid4())
    return [
        {
            "type": "run_meta",
            "schema_version": 1,
            "tool": "test",
            "tool_version": "1",
            "run_id": run_id,
            "started_at": "2026-08-24T00:00:00+00:00",
        },
        {
            "type": "run_end",
            "run_id": run_id,
            "finished_at": "2026-08-24T00:00:01+00:00",
            "stats": {"endpoints": 0, "resources": 0, "items": 0, "errors": 0},
        },
    ]


def _write_artifact(path: Path, records: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":")))
            stream.write("\n")


def test_json_output_is_machine_readable(tmp_path, monkeypatch, capsys, validator_module) -> None:
    artifact = tmp_path / "artifact.ndjson.gz"
    _write_artifact(artifact, _records())
    monkeypatch.setattr(sys, "argv", ["validate-ndjson.py", "--json", str(artifact)])

    assert validator_module.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "artifact": str(artifact),
        "records": 2,
        "endpoints": 0,
        "resources": 0,
        "items": 0,
        "errors": 0,
    }


def test_default_decompressed_limit_matches_ten_gibibyte_artifact_envelope(validator_module) -> None:
    assert validator_module.DEFAULT_MAX_DECOMPRESSED_BYTES == 10 * 1024 * 1024 * 1024


def test_decompressed_byte_limit_stops_gzip_expansion(tmp_path, validator_module) -> None:
    artifact = tmp_path / "artifact.ndjson.gz"
    _write_artifact(artifact, _records())

    with pytest.raises(ValueError, match="decompressed artifact exceeds"):
        validator_module.validate(artifact, max_record_bytes=1024, max_decompressed_bytes=32)


def test_summary_only_cli_uses_only_standard_library(tmp_path) -> None:
    artifact = tmp_path / "artifact.ndjson.gz"
    _write_artifact(artifact, _records())
    script = Path(__file__).parents[2] / "scripts" / "validate-ndjson.py"

    result = subprocess.run(
        [sys.executable, "-S", str(script), "--summary-only", "--json", str(artifact)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["records"] == 2


def test_summary_only_rejects_run_id_mismatch(tmp_path, validator_module) -> None:
    records = _records()
    records[-1]["run_id"] = str(uuid.uuid4())
    artifact = tmp_path / "mismatched.ndjson.gz"
    _write_artifact(artifact, records)

    with pytest.raises(ValueError, match="run_id differs"):
        validator_module.validate(artifact, summary_only=True)


def test_summary_only_rejects_duplicate_stream_markers(tmp_path, validator_module) -> None:
    records = _records()
    records.insert(1, dict(records[0]))
    artifact = tmp_path / "duplicate-meta.ndjson.gz"
    _write_artifact(artifact, records)

    with pytest.raises(ValueError, match="exactly one run_meta"):
        validator_module.validate(artifact, summary_only=True)


def test_summary_only_rejects_incorrect_terminal_counts(tmp_path, validator_module) -> None:
    records = _records()
    records[-1]["stats"] = {"endpoints": 0, "resources": 0, "items": 1, "errors": 0}
    artifact = tmp_path / "incorrect-count.ndjson.gz"
    _write_artifact(artifact, records)

    with pytest.raises(ValueError, match=r"stats\.items is 1"):
        validator_module.validate(artifact, summary_only=True)
