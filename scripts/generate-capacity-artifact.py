#!/usr/bin/env python3
"""Generate a deterministic-shape NDJSON artifact for capacity validation."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

MAX_TOTAL_RECORDS = 10_000_000
MAX_ENDPOINTS = 2 * 256 * 254


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stream a synthetic Share Sentinel schema-v1 NDJSON artifact without "
            "retaining the generated inventory in memory."
        )
    )
    parser.add_argument("--output", required=True, type=Path, help="Destination .ndjson or .ndjson.gz file")
    parser.add_argument("--endpoints", type=positive_int, default=10)
    parser.add_argument("--shares-per-endpoint", type=positive_int, default=10)
    parser.add_argument("--items-per-share", type=non_negative_int, default=1000)
    parser.add_argument("--run-id", type=uuid.UUID, default=None, help="Optional fixed UUID for repeatable fixtures")
    parser.add_argument("--force", action="store_true", help="Replace an existing destination")
    return parser.parse_args()


@contextmanager
def atomic_text_output(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp_path)
    os.close(fd)
    try:
        raw = None
        if path.name.lower().endswith(".gz"):
            raw = temp_path.open("wb")
            compressed = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
            stream: TextIO = io.TextIOWrapper(compressed, encoding="utf-8", newline="\n")
        else:
            stream = temp_path.open("w", encoding="utf-8", newline="\n")
        try:
            yield stream
        finally:
            stream.close()
            if raw is not None:
                raw.close()
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def emit(stream: TextIO, payload: dict[str, object]) -> None:
    json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")


def generate(args: argparse.Namespace) -> tuple[int, int, int, int]:
    endpoint_count = args.endpoints
    if endpoint_count > MAX_ENDPOINTS:
        raise ValueError(f"--endpoints must be {MAX_ENDPOINTS:,} or less")
    resource_count = endpoint_count * args.shares_per_endpoint
    item_count = resource_count * args.items_per_share
    total_records = 2 + endpoint_count + resource_count + item_count
    if total_records > MAX_TOTAL_RECORDS:
        raise ValueError(
            f"requested artifact has {total_records:,} records; the safety limit is {MAX_TOTAL_RECORDS:,}"
        )

    run_id = str(args.run_id or uuid.uuid4())
    timestamp = datetime.now(tz=UTC).replace(microsecond=0).isoformat()
    with atomic_text_output(args.output) as stream:
        emit(
            stream,
            {
                "type": "run_meta",
                "schema_version": 1,
                "tool": "share-sentinel-capacity-generator",
                "tool_version": "1",
                "run_id": run_id,
                "started_at": timestamp,
                "collection": {
                    "synthetic": True,
                    "endpoints": endpoint_count,
                    "shares_per_endpoint": args.shares_per_endpoint,
                    "items_per_share": args.items_per_share,
                },
            },
        )

        for endpoint_index in range(endpoint_count):
            network_index = endpoint_index // 254
            second_octet = 18 + (network_index // 256)
            third_octet = network_index % 256
            endpoint_key = f"198.{second_octet}.{third_octet}.{endpoint_index % 254 + 1}:445"
            emit(
                stream,
                {
                    "type": "endpoint",
                    "run_id": run_id,
                    "endpoint_key": endpoint_key,
                    "ip": endpoint_key.removesuffix(":445"),
                    "hostname": f"capacity-{endpoint_index:05d}.example.test",
                    "domain": "CAPACITY",
                    "smb": {"dialect": "3.1.1", "signing": "required"},
                    "auth": {"method": "synthetic"},
                },
            )
            for share_index in range(args.shares_per_endpoint):
                resource_name = f"Share-{share_index:04d}"
                emit(
                    stream,
                    {
                        "type": "resource",
                        "run_id": run_id,
                        "endpoint_key": endpoint_key,
                        "resource_type": "smb_share",
                        "share_type": "smb",
                        "name": resource_name,
                        "remark": "Synthetic capacity fixture",
                        "access_level": "readable",
                        "access_capabilities": {
                            "tree_connect": {"status": "allowed", "attempted": 1, "allowed": 1},
                            "list": {"status": "allowed", "attempted": 1, "allowed": 1},
                            "read_file": {"status": "allowed", "attempted": 1, "allowed": 1},
                        },
                    },
                )
                for item_index in range(args.items_per_share):
                    directory = item_index // 1000
                    name = f"document-{item_index:08d}.dat"
                    emit(
                        stream,
                        {
                            "type": "item",
                            "run_id": run_id,
                            "endpoint_key": endpoint_key,
                            "resource_type": "smb_share",
                            "share_type": "smb",
                            "resource_name": resource_name,
                            "path": f"\\folder-{directory:05d}\\{name}",
                            "name": name,
                            "is_dir": False,
                            "size_bytes": 1024 + (item_index % 65536),
                            "mtime": timestamp,
                            "file_attributes": ["archive"],
                        },
                    )

        emit(
            stream,
            {
                "type": "run_end",
                "run_id": run_id,
                "finished_at": timestamp,
                "stats": {
                    "endpoints": endpoint_count,
                    "resources": resource_count,
                    "items": item_count,
                    "errors": 0,
                },
            },
        )
    return total_records, endpoint_count, resource_count, item_count


def main() -> int:
    args = parse_args()
    if args.output.exists() and not args.force:
        print(f"FAIL: {args.output} already exists; pass --force to replace it", file=sys.stderr)
        return 2
    if not (args.output.name.lower().endswith(".ndjson") or args.output.name.lower().endswith(".ndjson.gz")):
        print("FAIL: --output must end in .ndjson or .ndjson.gz", file=sys.stderr)
        return 2
    try:
        records, endpoints, resources, items = generate(args)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"Created {args.output} records={records:,} endpoints={endpoints:,} "
        f"shares={resources:,} items={items:,} bytes={args.output.stat().st_size:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
