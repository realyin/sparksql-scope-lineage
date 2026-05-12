"""Render a self-contained offline HTML report from lineage.json."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lineage_parser.html_report import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

