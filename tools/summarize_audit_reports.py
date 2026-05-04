"""Summarize multiple audit_scope_output JSON reports."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def summarize(paths: list[Path]) -> dict[str, Any]:
    reports = []
    totals = {
        "statement_count": 0,
        "lineage_count": 0,
        "diagnostics_count": 0,
        "severity_counts": Counter(),
        "issue_counts": Counter(),
        "warning_counts": Counter(),
        "red_records": [],
    }

    for path in paths:
        data = _load(path)
        severity_counts = Counter(data.get("severity_counts") or {})
        issue_counts = Counter(data.get("issue_counts") or {})
        warning_counts = Counter(data.get("warning_counts") or {})
        red_records = data.get("red_records") or []
        report = {
            "path": str(path),
            "name": path.stem,
            "out_dir": data.get("out_dir"),
            "statement_count": data.get("statement_count", 0),
            "lineage_count": data.get("lineage_count", 0),
            "diagnostics_count": data.get("diagnostics_count", 0),
            "severity_counts": dict(severity_counts),
            "issue_counts": dict(issue_counts),
            "warning_counts": dict(warning_counts),
            "red_records": red_records,
        }
        reports.append(report)

        totals["statement_count"] += report["statement_count"]
        totals["lineage_count"] += report["lineage_count"]
        totals["diagnostics_count"] += report["diagnostics_count"]
        totals["severity_counts"].update(severity_counts)
        totals["issue_counts"].update(issue_counts)
        totals["warning_counts"].update(warning_counts)
        for record in red_records:
            totals["red_records"].append({"report": path.stem, **record})

    return {
        "reports": reports,
        "totals": {
            "statement_count": totals["statement_count"],
            "lineage_count": totals["lineage_count"],
            "diagnostics_count": totals["diagnostics_count"],
            "severity_counts": dict(totals["severity_counts"]),
            "issue_counts": dict(totals["issue_counts"]),
            "warning_counts": dict(totals["warning_counts"]),
            "red_records": totals["red_records"],
        },
    }


def render_markdown(summary: dict[str, Any]) -> str:
    totals = summary["totals"]
    lines = [
        "# Audit Report Summary",
        "",
        "## Corpus Results",
        "",
        "| Report | Statements | GREEN | YELLOW | RED | RED Rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    for report in summary["reports"]:
        sev = report["severity_counts"]
        statements = report["statement_count"]
        red = sev.get("RED", 0)
        red_rate = red / statements * 100 if statements else 0.0
        lines.append(
            f"| `{report['name']}` | {statements} | {sev.get('GREEN', 0)} | "
            f"{sev.get('YELLOW', 0)} | {red} | {red_rate:.1f}% |"
        )

    sev = totals["severity_counts"]
    statements = totals["statement_count"]
    red = sev.get("RED", 0)
    red_rate = red / statements * 100 if statements else 0.0
    lines.extend(
        [
            f"| **TOTAL** | {statements} | {sev.get('GREEN', 0)} | "
            f"{sev.get('YELLOW', 0)} | {red} | {red_rate:.1f}% |",
            "",
            "## Top Issues",
            "",
            "| Issue | Count |",
            "|---|---:|",
        ]
    )
    for issue, count in Counter(totals["issue_counts"]).most_common(20):
        lines.append(f"| `{issue}` | {count} |")

    lines.extend(["", "## Top Warnings", "", "| Warning | Count |", "|---|---:|"])
    for warning, count in Counter(totals["warning_counts"]).most_common(20):
        lines.append(f"| `{warning}` | {count} |")

    red_records = totals["red_records"]
    lines.extend(["", "## RED Records", ""])
    if not red_records:
        lines.append("No RED records.")
    else:
        lines.extend(["| Report | Statement | Target | Issues |", "|---|---|---|---|"])
        for record in red_records[:200]:
            lines.append(
                f"| `{record.get('report', '')}` | `{record.get('stmt', '')}` | "
                f"`{record.get('target', '')}` | `{record.get('issues', {})}` |"
            )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize audit_scope_output JSON reports")
    parser.add_argument("--audit", action="append", required=True, help="Audit JSON path")
    parser.add_argument("--report", help="Write Markdown summary")
    parser.add_argument("--json", dest="json_path", help="Write JSON summary")
    parser.add_argument("--fail-on-red", action="store_true", help="Exit 1 when any RED exists")
    args = parser.parse_args(argv)

    summary = summarize([Path(path) for path in args.audit])

    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_markdown(summary), encoding="utf-8")

    if args.json_path:
        json_path = Path(args.json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    totals = summary["totals"]
    severity = totals["severity_counts"]
    print(
        "Summary complete: "
        f"reports={len(summary['reports'])}, "
        f"statements={totals['statement_count']}, "
        f"RED={severity.get('RED', 0)}, "
        f"YELLOW={severity.get('YELLOW', 0)}, "
        f"GREEN={severity.get('GREEN', 0)}"
    )
    if args.report:
        print(f"Markdown report: {args.report}")
    if args.json_path:
        print(f"JSON summary: {args.json_path}")

    return 1 if args.fail_on_red and severity.get("RED", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())

