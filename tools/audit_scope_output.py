"""Audit scope_v2 output directories for structural lineage consistency.

Usage:
    python tools/audit_scope_output.py \
      --task-dir ../examples/tasks \
      --out-dir /tmp/scope_output/tasks \
      --report ../dwd_scope_v2_audit.md \
      --json /tmp/dwd_scope_v2_audit.json

The audit reads generated lineage.json / diagnostics.json files. It does not
re-parse SQL. RED findings are limited to structural issues that can directly
break field-level lineage, such as UNKNOWN sources, dangling internal column
references, empty ROOT outputs, or missing core view files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STRUCTURAL_ISSUES = {
    "scope_graph_dangling_edge",
    "unknown_column_ref",
    "dangling_column_ref",
    "dangling_scope_ref",
    "root_empty_columns",
    "duplicate_root_columns",
    "missing_scope_overview",
    "missing_field_lineage",
    "field_lineage.mmd_contains_unknown",
}

WARNING_IMPACT = {
    "star_not_expanded": (
        "影响完整字段覆盖。没有 schema 时，未被下游引用的 `*` 字段无法展开；"
        "已被引用字段通常可按需补齐。"
    ),
    "ambiguous_unqualified": "影响来源选择。无表别名字段在多输入场景下可能绑定到错误表。",
    "column_not_found": "高风险，常伴随 UNKNOWN 或断链。",
    "unresolved_unqualified_no_schema": "中高风险。无 schema 且字段未限定，无法确定来源表。",
    "duplicate_alias": "高风险。主路径解析失败后 fallback，复杂查询别名绑定可能不稳定。",
    "complex_aggregate_with_case": "中风险。来源可解析但业务语义复杂。",
    "filter_in_join_on_clause": "低到中风险，一般不影响 SELECT 字段血缘。",
    "duplicate_table_in_union": "低到中风险，需要关注 UNION 分支列对齐。",
    "magic_number": "低风险，通常不影响列来源。",
    "unresolved_alias": "中高风险。别名未解析，需看是否影响命名字段。",
    "merge_delete_ignored": "中风险。MERGE DELETE 是行级操作，不产生字段输出列。",
}

RISKY_WARNINGS = {
    "column_not_found",
    "unresolved_unqualified_no_schema",
    "ambiguous_unqualified",
    "duplicate_alias",
    "unresolved_alias",
}

TERMINAL_SOURCE_SCOPES = {"CONSTANT", "SYSTEM"}


@dataclass
class AuditRecord:
    stmt: str
    task: str
    severity: str
    target: str
    root_cols: int
    issues: Counter[str] = field(default_factory=Counter)
    warnings: Counter[str] = field(default_factory=Counter)
    details: list[str] = field(default_factory=list)


@dataclass
class AuditResult:
    task_dir: Path | None
    out_dir: Path
    source_task_count: int | None
    statement_count: int
    lineage_count: int
    diagnostics_count: int
    severity_counts: Counter[str]
    issue_counts: Counter[str]
    warning_counts: Counter[str]
    warning_task_counts: dict[str, int]
    records: list[AuditRecord]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _column_names(scope: dict[str, Any]) -> set[str]:
    return {
        col.get("name")
        for col in (scope.get("columns") or [])
        if col.get("name") is not None
    }


def _is_constant_expr(expr: str | None, transform: str | None) -> bool:
    text = (expr or "").strip()
    if transform == "CONSTANT":
        return True
    if not text:
        return False
    return bool(
        re.fullmatch(
            r"(?is)(null|true|false|current_date\(\)|current_timestamp\(\)|now\(\)|"
            r"[-+]?\d+(\.\d+)?|'[^']*'|\"[^\"]*\")",
            text,
        )
    )


def _task_from_stmt(stmt: str, task_dir: Path | None) -> str:
    if task_dir is None:
        return stmt
    match = re.match(r"^(.*)_\d+$", stmt)
    if match and (task_dir / f"{match.group(1)}.json").exists():
        return match.group(1)
    return stmt


def _is_star_column(name: str | None) -> bool:
    return bool(name == "*" or (name and name.endswith(".*")))


def _has_star_source(col: dict[str, Any]) -> bool:
    if _is_star_column(col.get("name")):
        return True
    for source in col.get("sources") or []:
        if _is_star_column(source.get("column")):
            return True
    return False


def _scope_looks_schema_expanded_from_physical(scope: dict[str, Any], src_col: str | None) -> bool:
    """Return True when a missing internal column likely means incomplete metadata.

    With schema metadata, ``SELECT * FROM physical_table`` becomes a finite list
    of DIRECT columns. If downstream SQL references another column on that
    subquery, the audit should report "metadata coverage is incomplete" rather
    than a parser-internal dangling edge. This is still a YELLOW accuracy risk,
    but not a RED structural graph break.
    """

    if not src_col:
        return False
    columns = scope.get("columns") or []
    if not columns:
        return False

    physical_sources = set()
    direct_from_physical = 0
    for col in columns:
        sources = col.get("sources") or []
        if not sources:
            continue
        for source in sources:
            source_scope = source.get("scope") or ""
            if source_scope.startswith(("cte:", "subq:", "union:", "udtf:", "ROOT", "UNKNOWN")):
                continue
            physical_sources.add(source_scope)
            if (
                col.get("transform") == "DIRECT"
                and col.get("name") == source.get("column")
                and col.get("expression") in (None, "", col.get("name"))
            ):
                direct_from_physical += 1

    if not physical_sources:
        return False
    return direct_from_physical >= max(3, len(columns) // 2)


def _classify_red(record: AuditRecord) -> str:
    issues = set(record.issues)
    if "unknown_column_ref" in issues or "field_lineage.mmd_contains_unknown" in issues:
        return "UNKNOWN 来源"
    if "dangling_column_ref" in issues or "dangling_scope_ref" in issues:
        return "内部列/Scope 断链"
    if "root_empty_columns" in issues:
        return "ROOT 无输出列"
    if "missing_field_lineage" in issues or "missing_scope_overview" in issues:
        return "视图产物缺失"
    return "其他结构错误"


def _record_issues(
    stmt_dir: Path,
    task_dir: Path | None,
    lineage: dict[str, Any],
    diagnostics: dict[str, Any],
) -> AuditRecord:
    scopes = lineage.get("scopes") or {}
    graph = lineage.get("scope_graph") or {}
    nodes = set(graph.get("nodes") or [])
    columns_by_scope = {sid: _column_names(scope) for sid, scope in scopes.items()}

    issues: Counter[str] = Counter()
    details: list[str] = []

    for edge in graph.get("edges") or []:
        if edge.get("from") not in nodes or edge.get("to") not in nodes:
            issues["scope_graph_dangling_edge"] += 1
            details.append(
                f"scope_graph edge {edge.get('from')} -> {edge.get('to')} references missing node"
            )

    for sid, scope in scopes.items():
        for col in scope.get("columns") or []:
            col_name = col.get("name")
            expression = col.get("expression", "")
            for source in col.get("sources") or []:
                src_scope = source.get("scope")
                src_col = source.get("column")
                if src_scope == "UNKNOWN":
                    issues["unknown_column_ref"] += 1
                    details.append(
                        f"UNKNOWN: {sid}.{col_name} -> UNKNOWN.{src_col} expr={expression}"
                    )
                elif (
                    src_scope in scopes
                    and src_col not in columns_by_scope[src_scope]
                    and src_col != "*"
                    and col_name != "*"
                ):
                    if _scope_looks_schema_expanded_from_physical(scopes[src_scope], src_col):
                        issues["schema_incomplete_column_ref"] += 1
                        details.append(
                            f"schema incomplete: {sid}.{col_name} -> {src_scope}.{src_col} "
                            f"expr={expression}"
                        )
                    else:
                        issues["dangling_column_ref"] += 1
                        details.append(
                            f"dangling: {sid}.{col_name} -> {src_scope}.{src_col} expr={expression}"
                        )
                elif src_scope in TERMINAL_SOURCE_SCOPES:
                    continue
                elif src_scope not in scopes and src_scope not in nodes and src_scope != "UNKNOWN":
                    issues["dangling_scope_ref"] += 1
                    details.append(f"dangling scope: {sid}.{col_name} -> {src_scope}.{src_col}")

    root = scopes.get("ROOT") or {}
    root_cols = len(root.get("columns") or [])
    if lineage.get("target_table") and not root_cols:
        issues["root_empty_columns"] += 1

    duplicate_cols = Counter(
        (col.get("name"), col.get("merge_branch"))
        for col in root.get("columns") or []
    )
    duplicate_names = [key for key, count in duplicate_cols.items() if count > 1]
    if duplicate_names:
        issues["duplicate_root_columns"] += len(duplicate_names)
        details.append(
            "duplicate ROOT columns: "
            + ", ".join(str(name) for name, _branch in duplicate_names[:10])
        )

    star_placeholders = 0
    nonconstant_no_sources = 0
    for scope in scopes.values():
        for col in scope.get("columns") or []:
            if _has_star_source(col):
                star_placeholders += 1
            if not (col.get("sources") or []) and not _is_constant_expr(
                col.get("expression"), col.get("transform")
            ):
                nonconstant_no_sources += 1

    if star_placeholders:
        issues["star_passthrough_placeholder"] += star_placeholders
        details.append(f"star passthrough placeholders: {star_placeholders}")
    if nonconstant_no_sources:
        issues["nonconstant_no_sources"] += nonconstant_no_sources
        details.append(f"nonconstant no-source expressions: {nonconstant_no_sources}")

    views_dir = stmt_dir / "views"
    if not (views_dir / "scope_overview.mmd").exists():
        issues["missing_scope_overview"] += 1

    field_lineage = views_dir / "field_lineage.mmd"
    if field_lineage.exists():
        text = field_lineage.read_text(encoding="utf-8", errors="ignore")
        if "UNKNOWN" in text:
            issues["field_lineage.mmd_contains_unknown"] += 1
        if "-->" not in text and root_cols and lineage.get("source_tables"):
            issues["field_lineage.mmd_no_edges"] += 1
    else:
        issues["missing_field_lineage"] += 1

    per_column = views_dir / "per_column"
    per_column_count = len(list(per_column.glob("*.mmd"))) if per_column.exists() else 0
    if root_cols and per_column_count < root_cols:
        issues["per_column_missing_some"] += 1
        details.append(f"per-column mmd count {per_column_count}/{root_cols}")

    warnings = Counter(w.get("type", "") for w in diagnostics.get("warnings") or [])
    severity = "RED" if any(issue in STRUCTURAL_ISSUES for issue in issues) else (
        "YELLOW" if issues or warnings else "GREEN"
    )

    return AuditRecord(
        stmt=stmt_dir.name,
        task=_task_from_stmt(stmt_dir.name, task_dir),
        severity=severity,
        target=lineage.get("target_table", ""),
        root_cols=root_cols,
        issues=issues,
        warnings=warnings,
        details=details,
    )


def audit_output(task_dir: Path | None, out_dir: Path) -> AuditResult:
    if task_dir is not None and not task_dir.exists():
        raise FileNotFoundError(f"task dir not found: {task_dir}")
    if not out_dir.exists():
        raise FileNotFoundError(f"output dir not found: {out_dir}")

    stmt_dirs = sorted(
        path for path in out_dir.iterdir()
        if path.is_dir() and (path / "lineage.json").exists()
    )

    records: list[AuditRecord] = []
    severity_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()
    warning_tasks: dict[str, set[str]] = defaultdict(set)

    for stmt_dir in stmt_dirs:
        lineage = _load_json(stmt_dir / "lineage.json")
        diagnostics_path = stmt_dir / "diagnostics.json"
        diagnostics = _load_json(diagnostics_path) if diagnostics_path.exists() else {"warnings": []}
        record = _record_issues(stmt_dir, task_dir, lineage, diagnostics)
        records.append(record)
        severity_counts[record.severity] += 1
        issue_counts.update(record.issues)
        warning_counts.update(record.warnings)
        for warning_type in record.warnings:
            warning_tasks[warning_type].add(record.stmt)

    source_count = len(list(task_dir.glob("*.json"))) if task_dir is not None else None
    lineage_count = len(list(out_dir.glob("*/lineage.json")))
    diagnostics_count = len(list(out_dir.glob("*/diagnostics.json")))

    return AuditResult(
        task_dir=task_dir,
        out_dir=out_dir,
        source_task_count=source_count,
        statement_count=len(records),
        lineage_count=lineage_count,
        diagnostics_count=diagnostics_count,
        severity_counts=severity_counts,
        issue_counts=issue_counts,
        warning_counts=warning_counts,
        warning_task_counts={key: len(value) for key, value in warning_tasks.items()},
        records=records,
    )


def _format_counter(counter: Counter[str]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in counter.items()) or "{}"


def render_markdown(result: AuditResult, title: str | None = None) -> str:
    title = title or f"{result.out_dir.name} scope_v2 输出验证报告"
    red_records = [record for record in result.records if record.severity == "RED"]
    yellow_records = [record for record in result.records if record.severity == "YELLOW"]
    red_classes = Counter(_classify_red(record) for record in red_records)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    if result.task_dir is not None:
        lines.append(f"- 任务目录：`{result.task_dir}`")
        lines.append(f"- 源任务 JSON：{result.source_task_count} 个")
    lines.append(f"- 输出目录：`{result.out_dir}`")
    lines.append(f"- 语句级输出：{result.statement_count} 个")
    lines.append(
        f"- lineage/diagnostics 完整性：lineage={result.lineage_count}，"
        f"diagnostics={result.diagnostics_count}"
    )
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    if red_records:
        lines.append(
            f"发现 {len(red_records)} 个 RED 级语句，存在命名字段断链、UNKNOWN 来源"
            "或核心视图结构错误；需要优先修复。"
        )
    else:
        lines.append(
            "没有发现 RED 级结构错误：命名字段断链、UNKNOWN 来源、"
            "field lineage UNKNOWN 均为 0。"
        )
    lines.append("")
    lines.append("| 等级 | 语句数 | 说明 |")
    lines.append("|---|---:|---|")
    severity_desc = {
        "RED": "存在会直接影响单字段血缘可信度的结构错误。",
        "YELLOW": "产物可用，但有完整性或准确性边界，需要按场景抽样。",
        "GREEN": "未发现结构错误或显著告警。",
    }
    for severity in ("RED", "YELLOW", "GREEN"):
        lines.append(
            f"| {severity} | {result.severity_counts.get(severity, 0)} | "
            f"{severity_desc[severity]} |"
        )

    lines.append("")
    lines.append("## 检查方法")
    lines.append("")
    lines.append("1. 对齐源任务、语句级输出、`lineage.json`、`diagnostics.json`。")
    lines.append("2. 校验 `scope_graph` node/edge 完整性。")
    lines.append(
        "3. 反查每个 column 的 `sources`：内部来源 scope 和来源列必须存在；"
        "物理表因缺 schema 只校验表节点存在；由 schema 展开的 `SELECT *` scope "
        "若缺少下游引用列，归为 metadata 覆盖不足的 YELLOW。"
    )
    lines.append("4. 扫描 `UNKNOWN` 来源、`field_lineage.mmd` 中的 UNKNOWN、ROOT 输出为空/重复。")
    lines.append("5. 区分 `* -> *` 星号占位和真实命名字段断链：前者作为 YELLOW，后者作为 RED。")
    lines.append("6. 汇总 diagnostics warning，并按准确性风险分层。")

    if red_records:
        lines.append("")
        lines.append("## RED 问题归类")
        lines.append("")
        lines.append("| 类别 | 语句数 | 任务 | 说明 |")
        lines.append("|---|---:|---|---|")
        class_desc = {
            "UNKNOWN 来源": "字段来源落到 UNKNOWN，单字段血缘不可直接信任。",
            "内部列/Scope 断链": "来源 scope 或来源列在内部产物中不存在，需要修解析。",
            "ROOT 无输出列": "写入目标没有解析出输出字段。",
            "视图产物缺失": "核心 Mermaid 视图缺失。",
            "其他结构错误": "需要单独定位。",
        }
        for class_name, count in red_classes.items():
            tasks = ", ".join(
                record.stmt for record in red_records if _classify_red(record) == class_name
            )
            lines.append(
                f"| {class_name} | {count} | `{tasks}` | "
                f"{class_desc.get(class_name, '需要单独定位。')} |"
            )

        lines.append("")
        lines.append("### RED 明细")
        lines.append("")
        for record in red_records:
            lines.append(f"#### `{record.stmt}`")
            lines.append("")
            lines.append(f"- target：`{record.target}`")
            lines.append(f"- 问题类型：{_format_counter(record.issues)}")
            if record.warnings:
                lines.append(f"- warnings：{dict(record.warnings)}")
            lines.append("- 关键证据：")
            for detail in record.details[:12]:
                lines.append(f"  - `{detail}`")
            lines.append("")

    lines.append("")
    lines.append("## 非 RED 结构信号")
    lines.append("")
    lines.append("| 信号 | 语句数/条数 | 说明 |")
    lines.append("|---|---:|---|")
    signal_desc = {
        "star_passthrough_placeholder": (
            "内部 `SELECT *` 透传留下占位。若最终命名字段已展开，通常不等同于结果错误，"
            "但说明缺 schema 下无法完整列覆盖。"
        ),
        "field_lineage.mmd_no_edges": (
            "field_lineage 图没有边。常见于常量/配置输出；若业务上应有源字段，需要抽样。"
        ),
        "nonconstant_no_sources": (
            "如 `MAP()`、`current_timestamp()` 等无输入表达式。多数是合理构造表达式，不直接判错。"
        ),
        "per_column_missing_some": "per-column 图数量少于 ROOT 输出列，需要关注视图覆盖。",
        "schema_incomplete_column_ref": (
            "下游引用了 schema 展开 scope 中不存在的字段。通常说明当前导入的表字段元数据不完整，"
            "需要补 schema 后复跑。"
        ),
    }
    for signal, desc in signal_desc.items():
        lines.append(f"| `{signal}` | {result.issue_counts.get(signal, 0)} | {desc} |")

    lines.append("")
    lines.append("## Warning 分布")
    lines.append("")
    if result.warning_counts:
        lines.append("| warning | 条数 | 涉及语句 | 准确性影响 |")
        lines.append("|---|---:|---:|---|")
        for warning_type, count in result.warning_counts.most_common():
            lines.append(
                f"| `{warning_type}` | {count} | "
                f"{result.warning_task_counts.get(warning_type, 0)} | "
                f"{WARNING_IMPACT.get(warning_type, '需要结合具体 SQL 判断。')} |"
            )
    else:
        lines.append("未发现 diagnostics warning。")

    lines.append("")
    lines.append("## 高风险 YELLOW 样本")
    lines.append("")
    risky = sorted(
        [
            record for record in yellow_records
            if RISKY_WARNINGS & set(record.warnings)
        ],
        key=lambda record: (
            -sum(record.warnings.get(warning, 0) for warning in RISKY_WARNINGS),
            record.stmt,
        ),
    )[:25]
    if risky:
        lines.append("| 语句 | warnings | 建议 |")
        lines.append("|---|---|---|")
        for record in risky:
            warning_subset = {
                key: value for key, value in record.warnings.items()
                if key in RISKY_WARNINGS
            }
            lines.append(
                f"| `{record.stmt}` | `{warning_subset}` | "
                "优先抽样核对字段绑定；若是缺 schema 或 SQL 未限定字段，建议保留 YELLOW 风险。 |"
            )
    else:
        lines.append("未发现包含高风险 warning 的 YELLOW 样本。")

    lines.append("")
    lines.append("## 总体判断")
    lines.append("")
    if red_records:
        lines.append(
            "当前还有 RED 结构错误，建议先修 RED，再复跑同一套检查；"
            "YELLOW 多数属于 schema/SQL 歧义边界。"
        )
    else:
        lines.append(
            "当前没有发现会直接破坏单字段血缘的结构性错误。剩余 warning 不建议强行绿化，"
            "重点是补 schema 或对高风险 YELLOW 做业务抽样。"
        )

    return "\n".join(lines) + "\n"


def to_jsonable(result: AuditResult) -> dict[str, Any]:
    return {
        "task_dir": str(result.task_dir) if result.task_dir is not None else None,
        "out_dir": str(result.out_dir),
        "source_task_count": result.source_task_count,
        "statement_count": result.statement_count,
        "lineage_count": result.lineage_count,
        "diagnostics_count": result.diagnostics_count,
        "severity_counts": dict(result.severity_counts),
        "issue_counts": dict(result.issue_counts),
        "warning_counts": dict(result.warning_counts),
        "warning_task_counts": result.warning_task_counts,
        "red_records": [
            {
                "stmt": record.stmt,
                "task": record.task,
                "target": record.target,
                "issues": dict(record.issues),
                "warnings": dict(record.warnings),
                "details": record.details,
            }
            for record in result.records
            if record.severity == "RED"
        ],
        "records": [
            {
                "stmt": record.stmt,
                "task": record.task,
                "severity": record.severity,
                "target": record.target,
                "root_cols": record.root_cols,
                "issues": dict(record.issues),
                "warnings": dict(record.warnings),
                "details": record.details,
            }
            for record in result.records
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit generated scope_v2 lineage output for structural consistency"
    )
    parser.add_argument("--task-dir", help="Source task JSON directory")
    parser.add_argument("--out-dir", required=True, help="Generated scope_v2 output directory")
    parser.add_argument("--report", help="Write Markdown report to this path")
    parser.add_argument("--json", dest="json_path", help="Write machine-readable JSON summary")
    parser.add_argument("--title", help="Markdown report title")
    parser.add_argument(
        "--fail-on-red",
        action="store_true",
        help="Exit with code 1 when any RED records are found",
    )
    args = parser.parse_args(argv)

    task_dir = Path(args.task_dir).resolve() if args.task_dir else None
    out_dir = Path(args.out_dir).resolve()
    result = audit_output(task_dir, out_dir)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(result, args.title), encoding="utf-8")

    if args.json_path:
        json_path = Path(args.json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(to_jsonable(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(
        "Audit complete: "
        f"statements={result.statement_count}, "
        f"RED={result.severity_counts.get('RED', 0)}, "
        f"YELLOW={result.severity_counts.get('YELLOW', 0)}, "
        f"GREEN={result.severity_counts.get('GREEN', 0)}"
    )
    if args.report:
        print(f"Markdown report: {args.report}")
    if args.json_path:
        print(f"JSON summary: {args.json_path}")

    if args.fail_on_red and result.severity_counts.get("RED", 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
