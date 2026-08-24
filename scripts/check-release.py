#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _match_version(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    if not match:
        raise ValueError(f"could not find version in {path.relative_to(ROOT)}")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify release versions and changelog state.")
    parser.add_argument("--tag", help="Optional release tag, for example v1.0.0.")
    args = parser.parse_args()

    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    versions = {
        "api/app/main.py": _match_version(ROOT / "api" / "app" / "main.py", r'version="([^"]+)"'),
        "collector/share_sentinel_collector.py": _match_version(
            ROOT / "collector" / "share_sentinel_collector.py", r'^TOOL_VERSION = "([^"]+)"'
        ),
        "ui/package.json": json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))["version"],
    }
    mismatches = {path: version for path, version in versions.items() if version != expected}
    if mismatches:
        details = ", ".join(f"{path}={version}" for path, version in mismatches.items())
        raise ValueError(f"VERSION is {expected}, but component versions differ: {details}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{expected}]" not in changelog:
        raise ValueError(f"CHANGELOG.md has no [{expected}] release section")

    if args.tag and args.tag != f"v{expected}":
        raise ValueError(f"tag {args.tag} does not match VERSION v{expected}")

    print(f"Release metadata is consistent for {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
