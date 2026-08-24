#!/usr/bin/env python3
"""Container command dispatcher preserving the existing collector CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    application_dir = Path(__file__).resolve().parent
    arguments = list(sys.argv[1:])
    if arguments[:1] == ["sharepoint"]:
        script = application_dir / "share_sentinel_sharepoint.py"
        arguments = arguments[1:]
    else:
        if arguments[:1] in (["--help"], ["-h"]):
            print(
                "Collector modes: SMB/NFS options are the default; "
                "use 'sharepoint --help' for SharePoint Online inventory.\n",
                flush=True,
            )
        script = application_dir / "share_sentinel_collector.py"
    os.execv(sys.executable, [sys.executable, str(script), *arguments])


if __name__ == "__main__":
    main()
