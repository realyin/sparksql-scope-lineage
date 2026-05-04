from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "summarize_audit_reports", TOOLS_DIR / "summarize_audit_reports.py"
)
summarize_audit_reports = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = summarize_audit_reports
SPEC.loader.exec_module(summarize_audit_reports)


def _write_audit(path: Path, *, green: int, yellow: int, red: int) -> None:
    data = {
        "out_dir": f"/tmp/{path.stem}",
        "statement_count": green + yellow + red,
        "lineage_count": green + yellow + red,
        "diagnostics_count": green + yellow + red,
        "severity_counts": {"GREEN": green, "YELLOW": yellow, "RED": red},
        "issue_counts": {"unknown_column_ref": red},
        "warning_counts": {"star_not_expanded": yellow},
        "red_records": [
            {
                "stmt": f"red_{idx}",
                "target": "mart.t",
                "issues": {"unknown_column_ref": 1},
                "warnings": {},
                "details": [],
            }
            for idx in range(red)
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def test_summarize_audit_reports_totals(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_audit(first, green=2, yellow=1, red=0)
    _write_audit(second, green=1, yellow=2, red=3)

    summary = summarize_audit_reports.summarize([first, second])
    markdown = summarize_audit_reports.render_markdown(summary)

    assert summary["totals"]["statement_count"] == 9
    assert summary["totals"]["severity_counts"]["RED"] == 3
    assert summary["totals"]["warning_counts"]["star_not_expanded"] == 3
    assert "TOTAL" in markdown
    assert "RED Records" in markdown


def test_summarize_main_writes_reports_and_fails_on_red(tmp_path):
    audit = tmp_path / "audit.json"
    _write_audit(audit, green=1, yellow=0, red=1)
    report = tmp_path / "summary.md"
    json_path = tmp_path / "summary.json"

    code = summarize_audit_reports.main(
        [
            "--audit",
            str(audit),
            "--report",
            str(report),
            "--json",
            str(json_path),
            "--fail-on-red",
        ]
    )

    assert code == 1
    assert "TOTAL" in report.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["totals"]["severity_counts"]["RED"] == 1

