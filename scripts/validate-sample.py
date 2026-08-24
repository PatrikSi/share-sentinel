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
    expected = {"run_meta": 1, "endpoint": 3, "resource": 3, "item": 6, "error": 1, "run_end": 1}
    for record_type, expected_count in expected.items():
        if counts[record_type] != expected_count:
            raise ValueError(f"expected {expected_count} {record_type} records, found {counts[record_type]}")

    retention_record = next(record for record in records if record.get("name") == "retention.pdf")
    if retention_record.get("size_bytes") != 2048:
        raise ValueError("sample item size metadata was not preserved")
    if retention_record.get("mtime").isoformat() != "2026-01-15T09:30:00+00:00":
        raise ValueError("sample item modification time was not normalized")

    smb_endpoint = next(record for record in records if record.get("endpoint_key") == "192.0.2.10:445")
    if smb_endpoint.get("smb", {}).get("signing") != "required":
        raise ValueError("sample SMB signing metadata was not preserved")

    sharepoint_resource = next(
        record for record in records if record.get("provider_resource_id") == "b!synthetic-drive-id"
    )
    if sharepoint_resource.get("resource_type") != "sharepoint_library":
        raise ValueError("sample SharePoint resource type was not normalized")
    if sharepoint_resource.get("exposure") != "USER_VISIBLE":
        raise ValueError("sample SharePoint exposure evidence was not preserved")

    sharepoint_item = next(
        record for record in records if record.get("provider_item_id") == "synthetic-file-id"
    )
    if sharepoint_item.get("path") != "/Records/quarterly-review.docx":
        raise ValueError("sample SharePoint item path was not normalized")
    if sharepoint_item.get("mime_type") != (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        raise ValueError("sample SharePoint item MIME type was not preserved")
    if sharepoint_item.get("web_url") != (
        "https://contoso.sharepoint.com/sites/Records/Shared%20Documents/Records/quarterly-review.docx"
    ):
        raise ValueError("sample SharePoint item URL was not preserved")

    print(f"Validated {sample_path.relative_to(ROOT)} ({len(records)} normalized records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
