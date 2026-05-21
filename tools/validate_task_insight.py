"""Validate task_insight.json/html against lineage/profile artifacts."""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any


OBJECT_GROUPS = ("scopes", "columns", "tables", "rules", "sections", "diagnostics", "knowledge")


def validate_task_dir(task_dir: str | Path) -> dict[str, Any]:
    task_dir = Path(task_dir)
    findings: list[dict[str, Any]] = []
    lineage = _load_json_if_exists(task_dir / "lineage.json", findings, "lineage")
    profile = _load_json_if_exists(task_dir / "profile.json", findings, "profile")
    insight = _load_json_if_exists(task_dir / "task_insight.json", findings, "task_insight")
    html_text = _load_text_if_exists(task_dir / "task_insight.html", findings, "task_insight_html")

    if not insight:
        return _result(task_dir, findings)

    object_ids = _object_ids(insight)
    _check_task_facts(lineage, profile, insight, findings)
    _check_object_identity(insight, findings)
    _check_links(insight, object_ids, findings)
    _check_scope_objects(lineage, profile, insight, object_ids, findings)
    _check_rules(insight, object_ids, findings)
    _check_sections(insight, object_ids, findings)
    _check_columns(profile, insight, object_ids, findings)
    _check_related_metadata(profile, insight, findings)
    _check_html_payload(insight, html_text, findings)
    return _result(task_dir, findings)


def validate_root(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    task_dirs = sorted({path.parent for path in root.rglob("task_insight.json")})
    results = [validate_task_dir(path) for path in task_dirs]
    return {
        "root": str(root),
        "task_count": len(results),
        "ok": all(item["ok"] for item in results),
        "error_count": sum(item["error_count"] for item in results),
        "warning_count": sum(item["warning_count"] for item in results),
        "results": results,
        "finding_counts": _finding_counts(results),
    }


def _load_json_if_exists(path: Path, findings: list[dict[str, Any]], artifact: str) -> dict[str, Any]:
    if not path.exists():
        findings.append(_finding("error", "missing_file", f"Missing {artifact}: {path.name}"))
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        findings.append(_finding("error", "invalid_json", f"Invalid {artifact} JSON: {exc}"))
    return {}


def _load_text_if_exists(path: Path, findings: list[dict[str, Any]], artifact: str) -> str:
    if not path.exists():
        findings.append(_finding("error", "missing_file", f"Missing {artifact}: {path.name}"))
        return ""
    return path.read_text(encoding="utf-8")


def _result(task_dir: Path, findings: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [item for item in findings if item["severity"] == "error"]
    warnings = [item for item in findings if item["severity"] == "warning"]
    return {
        "task_dir": str(task_dir),
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "findings": findings,
    }


def _object_ids(insight: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for group in OBJECT_GROUPS:
        ids.update((insight.get("objects") or {}).get(group, {}).keys())
    return ids


def _object_type(insight: dict[str, Any], object_id: str) -> str | None:
    for group in OBJECT_GROUPS:
        if object_id in ((insight.get("objects") or {}).get(group) or {}):
            return group[:-1] if group.endswith("s") else group
    return None


def _check_task_facts(
    lineage: dict[str, Any],
    profile: dict[str, Any],
    insight: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    task = insight.get("task") or {}
    lineage_scopes = lineage.get("scopes") or {}
    profile_lineage = profile.get("end_to_end_lineage") or []
    scopes = (insight.get("objects") or {}).get("scopes") or {}
    tables = (insight.get("objects") or {}).get("tables") or {}
    visible_scopes = [scope for scope in scopes.values() if not scope.get("hidden_in_business_view")]
    hidden_scopes = [scope for scope in scopes.values() if scope.get("hidden_in_business_view")]
    input_tables = [table for table in tables.values() if table.get("role") == "input"]

    expected_task_name = profile.get("task_name") or lineage.get("task_id")
    if expected_task_name and task.get("task_id") != expected_task_name:
        _add(findings, "error", "task_id_mismatch", f"task.task_id={task.get('task_id')} does not match {expected_task_name}")

    expected_target = profile.get("target_table") or lineage.get("target_table")
    if expected_target and task.get("target_table") != expected_target:
        _add(findings, "error", "target_table_mismatch", "task.target_table does not match profile/lineage target")

    if lineage_scopes and task.get("lineage_scope_count") != len(lineage_scopes):
        _add(findings, "error", "lineage_scope_count_mismatch", "task.lineage_scope_count must equal len(lineage.scopes)")

    if lineage_scopes and len(scopes) != len(lineage_scopes):
        _add(findings, "error", "scope_object_count_mismatch", "Every lineage scope must be represented in task_insight objects.scopes")

    unique_output_names = {item.get("column") for item in profile_lineage if item.get("column")}
    if profile_lineage and task.get("output_column_count") != len(unique_output_names):
        _add(findings, "error", "output_column_count_mismatch", "task.output_column_count must equal unique output columns in profile.end_to_end_lineage")

    complete_count = sum(1 for item in profile_lineage if item.get("trace_complete", True))
    incomplete_count = len(profile_lineage) - complete_count
    if profile_lineage and task.get("trace_complete_count") != complete_count:
        _add(findings, "error", "trace_complete_count_mismatch", "trace_complete_count is inconsistent with profile.end_to_end_lineage")
    if profile_lineage and task.get("trace_incomplete_count") != incomplete_count:
        _add(findings, "error", "trace_incomplete_count_mismatch", "trace_incomplete_count is inconsistent with profile.end_to_end_lineage")

    if task.get("visible_scope_count") != len(visible_scopes):
        _add(findings, "error", "visible_scope_count_mismatch", "task.visible_scope_count does not match non-hidden scope objects")
    if task.get("full_graph_scope_count") != len(scopes):
        _add(findings, "error", "full_graph_scope_count_mismatch", "task.full_graph_scope_count does not match all scope objects")
    if task.get("hidden_scope_count") != len(hidden_scopes):
        _add(findings, "error", "hidden_scope_count_mismatch", "task.hidden_scope_count does not match hidden scope objects")
    if task.get("dag_node_count") != len(visible_scopes) + len(input_tables):
        _add(findings, "error", "dag_node_count_mismatch", "task.dag_node_count must equal visible scopes + input tables")
    if task.get("full_dag_node_count") != len(scopes) + len(input_tables):
        _add(findings, "error", "full_dag_node_count_mismatch", "task.full_dag_node_count must equal all scopes + input tables")


def _check_object_identity(insight: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    for group in OBJECT_GROUPS:
        objects = ((insight.get("objects") or {}).get(group) or {})
        for object_id, item in objects.items():
            if item.get("id") and item.get("id") != object_id:
                _add(findings, "error", "object_id_mismatch", f"{group}.{object_id}.id does not match object key")
    scopes = (insight.get("objects") or {}).get("scopes") or {}
    raw_scope_ids = [scope.get("scope_id") for scope in scopes.values() if scope.get("scope_id")]
    if len(raw_scope_ids) != len(set(raw_scope_ids)):
        _add(findings, "error", "duplicate_scope_id", "Different scope objects share the same raw scope_id")


def _check_links(insight: dict[str, Any], object_ids: set[str], findings: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for index, link in enumerate(insight.get("links") or []):
        source = link.get("from")
        target = link.get("to")
        link_type = link.get("type")
        if not source or not target or not link_type:
            _add(findings, "error", "invalid_link", f"links[{index}] must have from/to/type")
            continue
        key = (source, target, link_type)
        if key in seen:
            _add(findings, "warning", "duplicate_link", f"Duplicate link {key}")
        seen.add(key)
        if source not in object_ids:
            _add(findings, "error", "link_source_missing", f"Link source does not exist: {source}")
        if target not in object_ids:
            _add(findings, "error", "link_target_missing", f"Link target does not exist: {target}")

    for link in insight.get("links") or []:
        if link.get("type") != "feeds":
            continue
        source_type = _object_type(insight, link.get("from"))
        target_type = _object_type(insight, link.get("to"))
        if source_type not in {"table", "scope"} or target_type != "scope":
            _add(findings, "error", "invalid_feeds_link", f"feeds link must connect table/scope to scope: {link}")


def _check_scope_objects(
    lineage: dict[str, Any],
    profile: dict[str, Any],
    insight: dict[str, Any],
    object_ids: set[str],
    findings: list[dict[str, Any]],
) -> None:
    lineage_scope_ids = set((lineage.get("scopes") or {}).keys())
    scopes = (insight.get("objects") or {}).get("scopes") or {}
    tables = (insight.get("objects") or {}).get("tables") or {}
    known_table_ids = set(tables)
    hidden_ids = {
        scope_id for scope_id, scope in scopes.items() if scope.get("hidden_in_business_view")
    }
    graph_diag = insight.get("graph_diagnostics") or {}
    hidden_diag = set(graph_diag.get("hidden_business_scope_ids") or [])
    dangling_diag = set(graph_diag.get("dangling_scope_ids") or [])
    outgoing_feeds = {link.get("from") for link in insight.get("links") or [] if link.get("type") == "feeds"}

    for object_id, scope in scopes.items():
        raw_scope_id = scope.get("scope_id")
        if lineage_scope_ids and raw_scope_id not in lineage_scope_ids:
            _add(findings, "error", "scope_not_in_lineage", f"Scope object is not backed by lineage.scopes: {object_id}/{raw_scope_id}")
        for input_id in scope.get("direct_inputs") or []:
            if input_id not in object_ids:
                _add(findings, "error", "scope_direct_input_missing", f"{object_id} direct_inputs references missing object {input_id}")
        direct_tables = set(scope.get("direct_source_tables") or [])
        physical_tables = set(scope.get("physical_source_tables") or [])
        if not direct_tables.issubset(physical_tables):
            _add(findings, "error", "direct_table_not_physical_source", f"{object_id} direct_source_tables must be subset of physical_source_tables")
        if any(table_id not in known_table_ids for table_id in direct_tables | physical_tables):
            _add(findings, "warning", "scope_table_metadata_missing", f"{object_id} references table ids not present in objects.tables")
        summary = str(scope.get("summary") or scope.get("business_action") or "")
        if "读取" in summary and not direct_tables:
            _add(findings, "error", "scope_reads_without_direct_sources", f"{object_id} says it reads physical tables but direct_source_tables is empty")
        if scope.get("hidden_in_business_view"):
            if not scope.get("hidden_reason"):
                _add(findings, "error", "hidden_scope_missing_reason", f"{object_id} hidden scope must explain hidden_reason")
            if object_id not in hidden_diag:
                _add(findings, "error", "hidden_scope_missing_diagnostic", f"{object_id} hidden scope missing graph diagnostic")
        if scope.get("profiled") is False and object_id not in outgoing_feeds:
            _add(findings, "warning", "dangling_lineage_scope", f"{object_id} has no downstream feeds; inspect parser output or SQL branch")

    if hidden_ids != hidden_diag:
        _add(findings, "error", "hidden_scope_diagnostic_mismatch", "graph_diagnostics.hidden_business_scope_ids must match hidden scope objects")
    if hidden_ids != dangling_diag:
        _add(findings, "warning", "dangling_scope_diagnostic_mismatch", "graph_diagnostics.dangling_scope_ids should match hidden dangling scope objects")

    step_scope_ids = {
        step.get("scope_id") or step.get("name")
        for step in ((profile.get("scope_profile") or {}).get("steps") or [])
    }
    for step_scope_id in step_scope_ids:
        if step_scope_id and lineage_scope_ids and step_scope_id not in lineage_scope_ids:
            _add(findings, "warning", "profile_step_not_in_lineage", f"profile scope_profile step is not present in lineage.scopes: {step_scope_id}")


def _check_rules(insight: dict[str, Any], object_ids: set[str], findings: list[dict[str, Any]]) -> None:
    for rule_id, rule in ((insight.get("objects") or {}).get("rules") or {}).items():
        for scope_id in rule.get("scope_ids") or []:
            if scope_id not in object_ids:
                _add(findings, "error", "rule_scope_missing", f"{rule_id} references missing scope {scope_id}")
        if not rule.get("condition_summary") and not rule.get("condition_expression") and not rule.get("expression_omitted"):
            _add(findings, "warning", "rule_condition_missing", f"{rule_id} has no condition summary/expression")
        for field in rule.get("fields") or []:
            field_id = field.get("field_id")
            if field_id and field_id not in object_ids:
                _add(findings, "error", "rule_field_missing", f"{rule_id} uses missing field {field_id}")
            if not field.get("meaning"):
                _add(findings, "warning", "rule_field_meaning_missing", f"{rule_id} field lacks meaning: {field.get('field')}")


def _check_sections(insight: dict[str, Any], object_ids: set[str], findings: list[dict[str, Any]]) -> None:
    for section_id, section in ((insight.get("objects") or {}).get("sections") or {}).items():
        for key in ("scope_ids", "rule_ids", "column_ids"):
            for ref_id in section.get(key) or []:
                if ref_id not in object_ids:
                    _add(findings, "error", "section_reference_missing", f"{section_id}.{key} references missing object {ref_id}")


def _check_columns(
    profile: dict[str, Any],
    insight: dict[str, Any],
    object_ids: set[str],
    findings: list[dict[str, Any]],
) -> None:
    columns = (insight.get("objects") or {}).get("columns") or {}
    output_columns = {col_id: col for col_id, col in columns.items() if col.get("type") == "output_column"}
    lineage_items = profile.get("end_to_end_lineage") or []
    unique_output_names = {item.get("column") for item in lineage_items if item.get("column")}
    if lineage_items and len(output_columns) != len(unique_output_names):
        _add(findings, "error", "output_column_object_count_mismatch", "Output column objects must match unique output columns in profile.end_to_end_lineage")
    if len(unique_output_names) < len(lineage_items):
        _add(findings, "warning", "duplicate_output_column_assignment", "profile.end_to_end_lineage has duplicate output column assignments, usually from MERGE update/insert clauses")

    source_column_links = {
        (link.get("from"), link.get("to"))
        for link in insight.get("links") or []
        if link.get("type") == "derived_from_column"
    }
    table_links = {
        (link.get("from"), link.get("to"))
        for link in insight.get("links") or []
        if link.get("type") == "derived_from"
    }
    for column_id, column in output_columns.items():
        if column.get("trace_complete") is False and not column.get("trace_incomplete_reasons"):
            _add(findings, "error", "trace_incomplete_reason_missing", f"{column_id} trace_complete=false needs reasons")
        for scope_id in column.get("scope_ids") or []:
            if scope_id not in object_ids:
                _add(findings, "error", "column_scope_missing", f"{column_id} references missing scope {scope_id}")
        for source in column.get("physical_sources") or []:
            table = source.get("table")
            source_col = source.get("column")
            if table and (column_id, f"table:{table}") not in table_links:
                _add(findings, "error", "column_table_link_missing", f"{column_id} missing derived_from table link for {table}")
            if table and source_col:
                physical_id = f"physical_column:{table}.{source_col}"
                if physical_id not in object_ids:
                    _add(findings, "error", "physical_column_missing", f"{column_id} references missing physical column {physical_id}")
                if (column_id, physical_id) not in source_column_links:
                    _add(findings, "error", "column_source_link_missing", f"{column_id} missing derived_from_column link for {physical_id}")


def _check_related_metadata(profile: dict[str, Any], insight: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    related = profile.get("related_metadata") or {}
    input_meta = set((related.get("input_tables") or {}).keys())
    output_meta = set((related.get("output_tables") or {}).keys())
    source_tables = set(profile.get("source_tables") or [])
    target_table = profile.get("target_table")
    if source_tables and not source_tables.issubset(input_meta):
        missing = sorted(source_tables - input_meta)
        _add(findings, "warning", "related_metadata_input_missing", f"Input related_metadata missing {missing[:10]}")
    if target_table and target_table not in output_meta:
        _add(findings, "warning", "related_metadata_output_missing", f"Output related_metadata missing {target_table}")

    table_objects = (insight.get("objects") or {}).get("tables") or {}
    for table_id, table in table_objects.items():
        if table.get("role") == "input" and table.get("name") not in input_meta:
            _add(findings, "warning", "input_table_metadata_missing", f"Input table object lacks related_metadata: {table_id}")
        if table.get("role") == "output" and table.get("name") not in output_meta:
            _add(findings, "warning", "output_table_metadata_missing", f"Output table object lacks related_metadata: {table_id}")


def _check_html_payload(insight: dict[str, Any], html_text: str, findings: list[dict[str, Any]]) -> None:
    if not html_text:
        return
    for marker in (
        'id="task-insight-data"',
        'id="graphMode"',
        'id="graphNotice"',
        'id="scopeSvg"',
        'id="fieldSvg"',
        'id="zoomScopeIn"',
        'id="resetFieldView"',
    ):
        if marker not in html_text:
            _add(findings, "error", "html_control_missing", f"HTML missing required control {marker}")

    match = re.search(
        r'<script\s+id="task-insight-data"\s+type="application/json">(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    if not match:
        _add(findings, "error", "html_payload_missing", "HTML does not embed task-insight-data JSON")
        return
    try:
        payload = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        _add(findings, "error", "html_payload_invalid", f"Embedded task-insight-data is not valid JSON: {exc}")
        return

    for path in (
        ("schema_version",),
        ("task", "task_id"),
        ("task", "target_table"),
        ("task", "visible_scope_count"),
        ("task", "full_graph_scope_count"),
        ("task", "hidden_scope_count"),
    ):
        expected = _get_path(insight, path)
        actual = _get_path(payload, path)
        if expected != actual:
            _add(findings, "error", "html_payload_stale", f"HTML embedded payload differs at {'.'.join(path)}")


def _get_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _finding(severity: str, code: str, message: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message}


def _add(findings: list[dict[str, Any]], severity: str, code: str, message: str) -> None:
    findings.append(_finding(severity, code, message))


def _finding_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        for finding in result.get("findings") or []:
            code = finding.get("code") or "unknown"
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate task_insight outputs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", help="Single task output directory")
    group.add_argument("--root", help="Root directory containing task_insight.json files")
    parser.add_argument("--json-out", help="Write full validation report as JSON")
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Return non-zero when warnings are present",
    )
    args = parser.parse_args(argv)

    report = validate_root(args.root) if args.root else validate_task_dir(args.input)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(_summary(report), ensure_ascii=False, indent=2))
    if report.get("error_count", 0):
        return 1
    if args.fail_on_warning and report.get("warning_count", 0):
        return 1
    return 0


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    if "results" in report:
        return {
            "root": report.get("root"),
            "task_count": report.get("task_count"),
            "ok": report.get("ok"),
            "error_count": report.get("error_count"),
            "warning_count": report.get("warning_count"),
            "finding_counts": report.get("finding_counts"),
        }
    return {
        "task_dir": report.get("task_dir"),
        "ok": report.get("ok"),
        "error_count": report.get("error_count"),
        "warning_count": report.get("warning_count"),
        "finding_counts": _finding_counts([report]),
    }


if __name__ == "__main__":
    raise SystemExit(main())
