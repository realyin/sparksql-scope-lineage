"""Summarize actionable YELLOW audit findings.

This consumes generated output plus one or more ``audit_scope_output.py`` JSON
reports and classifies the remaining non-RED issues into practical buckets:
schema gaps, final-output star impact, intermediate-only star placeholders, and
field-lineage views with no edges.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_SCHEMA_GAP_RE = re.compile(
    r"schema incomplete:\s+(?P<consumer>[^ ]+)\s+->\s+"
    r"(?P<source_scope>.+)\.(?P<column>[^.\s]+)\s+expr=(?P<expr>.*)$"
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _stmt_dir(out_root: Path, audit_out_dir: str, stmt: str) -> Path:
    audit_dir = Path(audit_out_dir)
    if audit_dir.is_absolute():
        return audit_dir / stmt
    return out_root / audit_dir / stmt


def _load_profile(stmt_dir: Path) -> dict[str, Any]:
    path = stmt_dir / "profile.json"
    if not path.exists():
        return {}
    return _load_json(path)


def build_action_summary(out_root: Path, audit_paths: list[Path]) -> dict[str, Any]:
    severity = Counter()
    issues = Counter()
    warnings = Counter()
    schema_gaps = []
    star_final = []
    star_intermediate = []
    no_edge_views = []

    for audit_path in audit_paths:
        audit = _load_json(audit_path)
        group = Path(audit.get("out_dir") or audit_path.stem).name
        severity.update(audit.get("severity_counts") or {})
        issues.update(audit.get("issue_counts") or {})
        warnings.update(audit.get("warning_counts") or {})

        for record in audit.get("records") or []:
            stmt = record.get("stmt", "")
            stmt_dir = _stmt_dir(out_root, audit.get("out_dir") or "", stmt)
            profile = _load_profile(stmt_dir)
            end_to_end = profile.get("end_to_end_lineage") or []
            incomplete = [item for item in end_to_end if not item.get("trace_complete", True)]
            star_incomplete = [
                item for item in incomplete
                if "star_not_expanded" in (item.get("trace_incomplete_reasons") or [])
            ]

            for detail in record.get("details") or []:
                match = _SCHEMA_GAP_RE.match(detail)
                if match:
                    schema_gaps.append({
                        "group": group,
                        "stmt": stmt,
                        "target": record.get("target", ""),
                        "consumer": match.group("consumer"),
                        "source_scope": match.group("source_scope"),
                        "column": match.group("column"),
                        "expression": match.group("expr"),
                    })

            placeholder_count = (record.get("issues") or {}).get("star_passthrough_placeholder", 0)
            if placeholder_count:
                row = {
                    "group": group,
                    "stmt": stmt,
                    "target": record.get("target", ""),
                    "placeholder_count": placeholder_count,
                    "root_cols": record.get("root_cols", 0),
                    "trace_incomplete_count": len(incomplete),
                    "star_incomplete_count": len(star_incomplete),
                    "affected_columns": [item.get("column") for item in star_incomplete[:12]],
                }
                if star_incomplete:
                    star_final.append(row)
                else:
                    star_intermediate.append(row)

            if (record.get("issues") or {}).get("field_lineage.mmd_no_edges"):
                no_edge_views.append({
                    "group": group,
                    "stmt": stmt,
                    "target": record.get("target", ""),
                    "root_cols": record.get("root_cols", 0),
                    "warnings": record.get("warnings") or {},
                    "details": record.get("details") or [],
                })

    return {
        "summary": {
            "severity_counts": dict(severity),
            "issue_counts": dict(issues),
            "warning_counts": dict(warnings),
            "schema_gap_count": len(schema_gaps),
            "star_final_affected_tasks": len(star_final),
            "star_intermediate_only_tasks": len(star_intermediate),
            "field_lineage_no_edges_count": len(no_edge_views),
        },
        "schema_gaps": schema_gaps,
        "schema_gap_columns": _group_schema_gap_columns(schema_gaps),
        "star_final_affected": sorted(
            star_final,
            key=lambda row: (-row["star_incomplete_count"], -row["placeholder_count"], row["stmt"]),
        ),
        "star_intermediate_only": sorted(
            star_intermediate,
            key=lambda row: (-row["placeholder_count"], row["stmt"]),
        ),
        "field_lineage_no_edges": no_edge_views,
    }


def _group_schema_gap_columns(schema_gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for gap in schema_gaps:
        key = (gap["source_scope"], gap["column"])
        item = grouped.setdefault(
            key,
            {
                "source_scope": gap["source_scope"],
                "column": gap["column"],
                "tasks": [],
                "targets": [],
                "examples": [],
            },
        )
        if gap["stmt"] not in item["tasks"]:
            item["tasks"].append(gap["stmt"])
        if gap["target"] not in item["targets"]:
            item["targets"].append(gap["target"])
        if len(item["examples"]) < 3:
            item["examples"].append({
                "stmt": gap["stmt"],
                "consumer": gap["consumer"],
                "expression": gap["expression"],
            })
    return sorted(
        grouped.values(),
        key=lambda item: (-len(item["tasks"]), item["source_scope"], item["column"]),
    )


def render_markdown(summary: dict[str, Any]) -> str:
    data = summary["summary"]
    lines = ["# YELLOW 可行动报告", ""]
    lines.append("## 概览")
    lines.append("")
    lines.append(f"- severity：`{data['severity_counts']}`")
    lines.append(f"- issue：`{data['issue_counts']}`")
    lines.append(f"- warning top：`{data['warning_counts']}`")
    lines.append(f"- schema gap 明细：{data['schema_gap_count']} 条")
    lines.append(f"- 最终字段受 star 影响任务：{data['star_final_affected_tasks']} 个")
    lines.append(f"- 仅中间 star 占位任务：{data['star_intermediate_only_tasks']} 个")
    lines.append(f"- field_lineage 无边图：{data['field_lineage_no_edges_count']} 个")

    lines.append("")
    lines.append("## 需要补 Schema 的字段")
    lines.append("")
    lines.append("| source_scope | column | tasks | examples |")
    lines.append("|---|---|---:|---|")
    for item in summary["schema_gap_columns"]:
        examples = "<br>".join(
            f"`{ex['stmt']}`: `{ex['consumer']}`"
            for ex in item["examples"]
        )
        lines.append(
            f"| `{item['source_scope']}` | `{item['column']}` | "
            f"{len(item['tasks'])} | {examples} |"
        )

    lines.append("")
    lines.append("## 最终字段仍受 Star 影响")
    lines.append("")
    lines.append("| group | stmt | affected | columns |")
    lines.append("|---|---|---:|---|")
    for row in summary["star_final_affected"][:80]:
        cols = ", ".join(f"`{col}`" for col in row["affected_columns"])
        lines.append(
            f"| `{row['group']}` | `{row['stmt']}` | "
            f"{row['star_incomplete_count']} | {cols} |"
        )

    lines.append("")
    lines.append("## Field Lineage 无边图")
    lines.append("")
    lines.append("| group | stmt | root_cols | notes |")
    lines.append("|---|---|---:|---|")
    for row in summary["field_lineage_no_edges"]:
        lines.append(
            f"| `{row['group']}` | `{row['stmt']}` | {row['root_cols']} | "
            f"`{row['warnings']}` |"
        )

    lines.append("")
    lines.append("## 建议")
    lines.append("")
    lines.append("1. 优先补 `schema_gaps` 中的表字段元数据，然后复跑。")
    lines.append("2. 对 `star_final_affected` 中的任务补 schema 或改 SQL 显式列。")
    lines.append("3. `star_intermediate_only` 一般不影响 ROOT 端到端血缘，可保留为 YELLOW 边界。")
    lines.append("4. `field_lineage.mmd_no_edges` 数量少，建议按任务抽样看是否为纯星号/常量输出。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize actionable YELLOW audit findings")
    parser.add_argument("--out-root", required=True, help="Generated corpus output root")
    parser.add_argument("--audit", action="append", required=True, help="Audit JSON path; repeatable")
    parser.add_argument("--json", dest="json_path", help="Write JSON action summary")
    parser.add_argument("--report", help="Write Markdown action report")
    args = parser.parse_args(argv)

    summary = build_action_summary(
        Path(args.out_root),
        [Path(path) for path in args.audit],
    )

    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(summary), encoding="utf-8")

    data = summary["summary"]
    print(
        "YELLOW action summary: "
        f"schema_gaps={data['schema_gap_count']}, "
        f"star_final_affected_tasks={data['star_final_affected_tasks']}, "
        f"star_intermediate_only_tasks={data['star_intermediate_only_tasks']}, "
        f"field_lineage_no_edges={data['field_lineage_no_edges_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
