#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from worker.main import records_from_json_document, validate_record  # noqa: E402


def main() -> int:
    sample_path = ROOT / "examples" / "sample-artifact.json"
    document = json.loads(sample_path.read_text(encoding="utf-8"))
    run_id = str(document["meta"]["run_id"])
    records = records_from_json_document(document, run_id)

    for record in records:
        valid, reason = validate_record(record)
        if not valid:
            raise ValueError(f"invalid {record.get('type', 'unknown')} record: {reason}")

    counts = Counter(str(record.get("type")) for record in records)
    expected = {"run_meta": 1, "endpoint": 2, "resource": 2, "item": 4, "error": 1, "run_end": 1}
    for record_type, expected_count in expected.items():
        if counts[record_type] != expected_count:
            raise ValueError(f"expected {expected_count} {record_type} records, found {counts[record_type]}")

    print(f"Validated {sample_path.relative_to(ROOT)} ({len(records)} normalized records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
