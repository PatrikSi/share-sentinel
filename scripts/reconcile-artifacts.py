#!/usr/bin/env python3
"""Source-tree wrapper for the artifact reconciliation maintenance command."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.maintenance.reconcile_artifacts import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
