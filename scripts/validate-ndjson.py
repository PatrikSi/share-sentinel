#!/usr/bin/env python3
"""Stream-validate a Share Sentinel schema-v1 NDJSON artifact."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

DEFAULT_MAX_RECORD_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DECOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_MAX_EXPANSION_RATIO = 200
DEFAULT_MIN_GZIP_DECOMPRESSED_BYTES = 50 * 1024 * 1024
RECORD_TYPES = frozenset({"run_meta", "endpoint", "resource", "item", "error", "run_end"})
_record_validator: Callable[[dict[str, Any]], tuple[bool, str | None]] | None = None


def validate_record(record: dict[str, Any]) -> tuple[bool, str | None]:
    global _record_validator  # noqa: PLW0603
    if _record_validator is None:
        try:
            from worker.main import validate_record as worker_validate_record
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "worker validation dependencies are unavailable; install worker/requirements.txt"
            ) from exc
        _record_validator = worker_validate_record
    return _record_validator(record)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate an NDJSON/JSONL artifact one record at a time")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--max-record-bytes", type=int, default=DEFAULT_MAX_RECORD_BYTES)
    parser.add_argument("--max-decompressed-bytes", type=int, default=DEFAULT_MAX_DECOMPRESSED_BYTES)
    parser.add_argument("--max-expansion-ratio", type=int, default=DEFAULT_MAX_EXPANSION_RATIO)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Validate bounded NDJSON framing and exact summary counts without worker dependencies",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit a machine-readable summary")
    return parser.parse_args()


@contextmanager
def open_text(path: Path):
    if path.name.lower().endswith(".gz"):
        stream: BinaryIO = gzip.open(path, "rb")
    else:
        stream = path.open("rb")
    try:
        yield stream
    finally:
        stream.close()


def validate(
    path: Path,
    *,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_expansion_ratio: int = DEFAULT_MAX_EXPANSION_RATIO,
    summary_only: bool = False,
) -> tuple[int, Counter[str]]:
    counts: Counter[str] = Counter()
    first_type: str | None = None
    last_record: dict[str, object] | None = None
    run_id: str | None = None
    line_number = 0

    decompressed_limit = max_decompressed_bytes
    if path.name.lower().endswith(".gz"):
        ratio_limit = max(
            DEFAULT_MIN_GZIP_DECOMPRESSED_BYTES,
            path.stat().st_size * max_expansion_ratio,
        )
        decompressed_limit = min(decompressed_limit, ratio_limit)

    decompressed_bytes = 0
    with open_text(path) as stream:
        while True:
            raw_line = stream.readline(max_record_bytes + 1)
            if not raw_line:
                break
            line_number += 1
            decompressed_bytes += len(raw_line)
            if decompressed_bytes > decompressed_limit:
                raise ValueError(
                    f"decompressed artifact exceeds the {decompressed_limit:,}-byte validation limit"
                )
            if len(raw_line) > max_record_bytes:
                raise ValueError(f"line {line_number}: record exceeds {max_record_bytes:,} bytes")
            if not raw_line.strip():
                continue
            try:
                decoded_line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid UTF-8 at byte offset {exc.start}") from exc
            try:
                record = json.loads(decoded_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON at column {exc.colno}: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"line {line_number}: record must be a JSON object")
            record_type = record.get("type")
            if not isinstance(record_type, str) or record_type not in RECORD_TYPES:
                raise ValueError(f"line {line_number}: unknown record type")
            record_run_id = record.get("run_id")
            if (
                not isinstance(record_run_id, str)
                or not record_run_id.strip()
                or len(record_run_id) > 36
            ):
                raise ValueError(f"line {line_number}: run_id must be a non-empty string of at most 36 characters")
            if not summary_only:
                valid, reason = validate_record(record)
                if not valid:
                    raise ValueError(f"line {line_number}: {reason or 'invalid record'}")

            if first_type is None:
                first_type = record_type
            if run_id is None:
                run_id = record_run_id
            elif record_run_id != run_id:
                raise ValueError(f"line {line_number}: run_id differs from the first record")
            counts[record_type] += 1
            last_record = record

    if line_number == 0 or last_record is None:
        raise ValueError("artifact is empty")
    if first_type != "run_meta":
        raise ValueError("first non-empty record must be run_meta")
    if last_record.get("type") != "run_end":
        raise ValueError("last non-empty record must be run_end")
    if counts["run_meta"] != 1 or counts["run_end"] != 1:
        raise ValueError("artifact must contain exactly one run_meta and one run_end record")

    stats = last_record.get("stats")
    if not isinstance(stats, dict):
        raise ValueError("run_end must contain a stats object")
    expected = {
        "endpoints": counts["endpoint"],
        "resources": counts["resource"],
        "items": counts["item"],
        "errors": counts["error"],
    }
    for field, actual_count in expected.items():
        raw_expected = stats.get(field)
        if isinstance(raw_expected, bool) or not isinstance(raw_expected, int):
            raise ValueError(f"run_end stats.{field} must be an integer")
        if raw_expected != actual_count:
            raise ValueError(
                f"run_end stats.{field} is {raw_expected:,}, but the stream contains {actual_count:,} records"
            )
    return sum(counts.values()), counts


def main() -> int:
    args = parse_args()
    if not args.artifact.is_file():
        print(f"FAIL: artifact does not exist: {args.artifact}", file=sys.stderr)
        return 2
    if args.max_record_bytes <= 0 or args.max_decompressed_bytes <= 0 or args.max_expansion_ratio <= 0:
        print("FAIL: validation limits must be greater than zero", file=sys.stderr)
        return 2
    try:
        record_count, counts = validate(
            args.artifact,
            max_record_bytes=args.max_record_bytes,
            max_decompressed_bytes=args.max_decompressed_bytes,
            max_expansion_ratio=args.max_expansion_ratio,
            summary_only=args.summary_only,
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"FAIL: {args.artifact}: {exc}", file=sys.stderr)
        return 1
    summary = {
        "artifact": str(args.artifact),
        "records": record_count,
        "endpoints": counts["endpoint"],
        "resources": counts["resource"],
        "items": counts["item"],
        "errors": counts["error"],
    }
    if args.json_output:
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        print(
            f"Validated {args.artifact} records={record_count:,} endpoints={counts['endpoint']:,} "
            f"shares={counts['resource']:,} items={counts['item']:,} errors={counts['error']:,}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
