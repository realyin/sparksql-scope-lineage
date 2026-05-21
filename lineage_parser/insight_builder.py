"""Build a stable task-insight model from lineage/profile artifacts."""

from __future__ import annotations

import copy
import re
from typing import Any


def build_task_insight(
    *,
    lineage: dict[str, Any],
    profile: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
    business_doc: str | None = None,
    business_doc_index: dict[str, Any] | None = None,
    business_knowledge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize lineage/profile data into the page-facing insight model.

    The returned structure is intentionally more stable than ``profile.json``.
    Renderers should depend on this model instead of reading the current
    profile layout directly.
    """

    diagnostics_data = _normalize_diagnostics(diagnostics or profile.get("diagnostics") or lineage.get("diagnostics") or {})
    insight: dict[str, Any] = {
        "schema_version": "1.0",
        "task": _build_task(lineage, profile, diagnostics_data, business_knowledge),
        "objects": {
            "scopes": {},
            "columns": {},
            "tables": {},
            "rules": {},
            "sections": {},
            "diagnostics": {},
            "knowledge": {},
        },
        "links": [],
        "sources": _build_sources(profile, diagnostics_data, business_doc, business_doc_index, business_knowledge),
        "capabilities": {},
        "warnings": [],
    }

    _add_tables(insight, profile)
    scope_id_map = _add_scopes(insight, lineage, profile)
    _add_missing_graph_tables(insight, lineage)
    _add_columns(insight, profile)
    _add_rules(insight, profile, scope_id_map)
    _add_sections(insight, profile, scope_id_map, business_doc_index)
    _add_diagnostics(insight, diagnostics_data, scope_id_map)
    _add_knowledge(insight, business_knowledge)
    _add_graph_links(insight, lineage, scope_id_map)
    _dedupe_links(insight)
    _mark_hidden_implementation_scopes(insight)
    _finalize_task_counts(insight)
    _build_capabilities(insight, business_doc, business_doc_index, business_knowledge)
    return insight


def _build_task(
    lineage: dict[str, Any],
    profile: dict[str, Any],
    diagnostics: dict[str, Any],
    business_knowledge: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = profile.get("summary") or {}
    end_to_end = profile.get("end_to_end_lineage") or []
    complete_count = sum(1 for item in end_to_end if item.get("trace_complete", True))
    incomplete_count = len(end_to_end) - complete_count
    target_table = profile.get("target_table") or lineage.get("target_table")
    lineage_scope_count = len(lineage.get("scopes") or {}) or (diagnostics.get("stats") or {}).get("scope_count")
    return {
        "task_id": profile.get("task_name") or lineage.get("task_id"),
        "task_name": (profile.get("task_name") or lineage.get("task_id") or "").split("#")[0],
        "target_table": target_table,
        "target_table_label": _target_label(profile, target_table, business_knowledge),
        "stmt_kind": profile.get("stmt_kind") or lineage.get("stmt_kind"),
        "summary": summary.get("main_process") or (profile.get("business_profile") or {}).get("objective", {}).get("summary"),
        "input_table_count": summary.get("input_table_count") or len(profile.get("source_tables") or []),
        "output_column_count": summary.get("output_column_count") or len(end_to_end),
        "scope_count": lineage_scope_count,
        "lineage_scope_count": lineage_scope_count,
        "trace_complete_count": complete_count,
        "trace_incomplete_count": incomplete_count,
        "warning_count": diagnostics.get("warning_count", 0),
        "risk_level": _risk_level(diagnostics, incomplete_count),
    }


def _normalize_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(diagnostics or {})
    warnings = normalized.get("warnings")
    if warnings and not normalized.get("warnings_sample"):
        normalized["warnings_sample"] = warnings[:20]
    if "warning_count" not in normalized:
        if warnings is not None:
            normalized["warning_count"] = len(warnings)
        elif normalized.get("warnings_sample") is not None:
            normalized["warning_count"] = len(normalized.get("warnings_sample") or [])
        else:
            normalized["warning_count"] = 0
    if "warning_types" not in normalized:
        warning_types: dict[str, int] = {}
        for warning in normalized.get("warnings_sample") or []:
            code = warning.get("type")
            if code:
                warning_types[code] = warning_types.get(code, 0) + 1
        if warning_types:
            normalized["warning_types"] = warning_types
    return normalized


def _target_label(profile: dict[str, Any], target_table: str | None, business_knowledge: dict[str, Any] | None) -> str | None:
    if target_table and business_knowledge:
        task_knowledge = (business_knowledge.get("tables") or {}).get(target_table) or {}
        if task_knowledge.get("business_name"):
            return task_knowledge["business_name"]
    related = profile.get("related_metadata") or {}
    output = (related.get("output_tables") or {}).get(target_table or "") or {}
    table_metadata = output.get("table_metadata") or {}
    return (
        table_metadata.get("table_name_cn")
        or table_metadata.get("table_desc")
        or (profile.get("business_profile") or {}).get("objective", {}).get("target_table_label")
        or target_table
    )


def _risk_level(diagnostics: dict[str, Any], incomplete_count: int) -> str:
    if incomplete_count:
        return "RED"
    if diagnostics.get("warning_count") or (diagnostics.get("warning_types") or {}):
        return "YELLOW"
    return "GREEN"


def _build_sources(
    profile: dict[str, Any],
    diagnostics: dict[str, Any],
    business_doc: str | None,
    business_doc_index: dict[str, Any] | None,
    business_knowledge: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "lineage": {"path": "lineage.json"},
        "profile": {"path": "profile.json", "compact_policy": profile.get("compact_policy") or {}},
        "diagnostics": {"path": "diagnostics.json", "available": bool(diagnostics)},
        "business_doc": {"path": "business_profile.md", "available": business_doc is not None},
        "business_doc_index": {"path": "business_profile.index.json", "available": business_doc_index is not None},
        "business_knowledge": {"path": "business_knowledge.json", "available": business_knowledge is not None},
    }


def _add_tables(insight: dict[str, Any], profile: dict[str, Any]) -> None:
    related = profile.get("related_metadata") or {}
    for role, section in (("input", "input_tables"), ("output", "output_tables")):
        for table_name, metadata in (related.get(section) or {}).items():
            table_id = _table_id(table_name)
            table_metadata = metadata.get("table_metadata") or {}
            insight["objects"]["tables"][table_id] = {
                "id": table_id,
                "type": "table",
                "name": table_name,
                "label": table_metadata.get("table_name_cn") or table_metadata.get("table_desc") or table_name,
                "description": table_metadata.get("table_desc"),
                "layer": table_metadata.get("table_label_layer"),
                "role": role,
                "used_columns": [item.get("name") for item in metadata.get("column_details") or [] if item.get("name")],
                "column_details": copy.deepcopy(metadata.get("column_details") or []),
                "metadata_complete": metadata.get("metadata_complete"),
                "evidence": [{"source": "profile", "path": f"$.related_metadata.{section}['{table_name}']"}],
            }


def _add_missing_graph_tables(insight: dict[str, Any], lineage: dict[str, Any]) -> None:
    for edge in (lineage.get("scope_graph") or {}).get("edges") or []:
        for endpoint in (edge.get("from"), edge.get("to")):
            if not _looks_like_table(endpoint):
                continue
            table_id = _table_id(str(endpoint))
            insight["objects"]["tables"].setdefault(
                table_id,
                {
                    "id": table_id,
                    "type": "table",
                    "name": str(endpoint),
                    "label": str(endpoint),
                    "description": None,
                    "layer": None,
                    "role": "input",
                    "used_columns": [],
                    "column_details": [],
                    "metadata_complete": False,
                    "evidence": [{"source": "lineage", "path": "$.scope_graph.edges"}],
                },
            )


def _add_scopes(insight: dict[str, Any], lineage: dict[str, Any], profile: dict[str, Any]) -> dict[str, str]:
    scope_id_map: dict[str, str] = {}
    steps = (profile.get("scope_profile") or {}).get("steps") or []
    for index, step in enumerate(steps):
        raw_scope_id = step.get("scope_id") or step.get("name") or f"scope_{index}"
        object_id = _scope_object_id(step.get("name") or raw_scope_id)
        existing = insight["objects"]["scopes"].get(object_id)
        if existing and existing.get("scope_id") != raw_scope_id:
            object_id = _scope_object_id(str(raw_scope_id))
        scope_id_map[str(raw_scope_id)] = object_id
        if step.get("name") and step.get("name") not in scope_id_map:
            scope_id_map[str(step["name"])] = object_id
        insight["objects"]["scopes"][object_id] = {
            "id": object_id,
            "type": "scope",
            "scope_id": raw_scope_id,
            "name": step.get("name") or raw_scope_id,
            "kind": step.get("kind"),
            "role": step.get("role"),
            "operations": step.get("operations") or [],
            "business_action": _business_action(step),
            "summary": step.get("business_summary"),
            "direct_inputs": [_input_object_id(item, scope_id_map) for item in step.get("direct_inputs") or []],
            "direct_source_tables": [_table_id(item) for item in step.get("direct_source_tables") or []],
            "physical_source_tables": [_table_id(item) for item in step.get("physical_source_tables") or []],
            "output_column_count": step.get("output_columns"),
            "logic": copy.deepcopy(step.get("logic") or {}),
            "evidence": [
                {"source": "profile", "path": f"$.scope_profile.steps[{index}]"},
                {"source": "lineage", "path": f"$.scopes['{raw_scope_id}']"},
            ],
        }

    for scope_key in (lineage.get("scopes") or {}).keys():
        object_id = _scope_object_id(_display_scope_name(scope_key))
        existing = insight["objects"]["scopes"].get(object_id)
        if existing and existing.get("scope_id") != scope_key:
            object_id = _scope_object_id(scope_key)
        scope_id_map.setdefault(scope_key, object_id)

    for scope_key, scope in (lineage.get("scopes") or {}).items():
        object_id = scope_id_map.get(scope_key)
        if not object_id or object_id in insight["objects"]["scopes"]:
            continue
        physical_tables = _lineage_physical_tables(scope)
        insight["objects"]["scopes"][object_id] = {
            "id": object_id,
            "type": "scope",
            "scope_id": scope_key,
            "name": _display_scope_name(scope_key),
            "kind": scope.get("kind"),
            "role": scope.get("role"),
            "operations": _lineage_operations(scope),
            "business_action": _lineage_business_action(scope, physical_tables),
            "summary": _lineage_business_action(scope, physical_tables),
            "direct_inputs": [_input_object_id(item, scope_id_map) for item in scope.get("depends_on") or []],
            "direct_source_tables": [_table_id(item) for item in physical_tables],
            "physical_source_tables": [_table_id(item) for item in physical_tables],
            "output_column_count": len(scope.get("columns") or []),
            "logic": _lineage_logic(scope),
            "profiled": False,
            "evidence": [{"source": "lineage", "path": f"$.scopes['{scope_key}']"}],
        }
    return scope_id_map


def _lineage_operations(scope: dict[str, Any]) -> list[str]:
    operations: list[str] = []
    kind = scope.get("kind")
    role = scope.get("role")
    if kind == "union_branch":
        operations.append("union_branch")
    if role and role not in operations:
        operations.append(str(role))
    if scope.get("filters"):
        operations.append("filter")
    transforms = {str(column.get("transform") or "").upper() for column in scope.get("columns") or []}
    if "CONDITIONAL" in transforms:
        operations.append("case_when")
    if "AGGREGATE" in transforms:
        operations.append("aggregate")
    if "WINDOW" in transforms:
        operations.append("window")
    return operations


def _lineage_physical_tables(scope: dict[str, Any]) -> list[str]:
    tables: list[str] = []
    for item in scope.get("depends_on") or []:
        if _looks_like_table(item) and item not in tables:
            tables.append(item)
    for column in scope.get("columns") or []:
        for source in column.get("sources") or []:
            source_scope = source.get("scope")
            if _looks_like_table(source_scope) and source_scope not in tables:
                tables.append(source_scope)
    return tables


def _lineage_business_action(scope: dict[str, Any], physical_tables: list[str]) -> str:
    parts: list[str] = []
    if physical_tables:
        parts.append("读取 " + ", ".join(physical_tables[:3]) + (" 等物理表" if len(physical_tables) > 3 else ""))
    if scope.get("kind") == "union_branch":
        branch_index = scope.get("branch_index")
        if branch_index is not None:
            parts.append(f"作为 UNION 第 {int(branch_index) + 1} 个分支")
        else:
            parts.append("作为 UNION 分支")
    if scope.get("filters"):
        parts.append("按分支条件筛选记录")
    return "；".join(parts) or "加工中间结果"


def _lineage_logic(scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "joins": [],
        "filters": copy.deepcopy(scope.get("filters") or []),
        "aggregations": [],
        "window_functions": [],
        "case_when": _lineage_case_when(scope),
        "key_renames": [],
        "distinct": False,
        "union_branches": 0,
        "lateral_views": [],
    }


def _lineage_case_when(scope: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for column in scope.get("columns") or []:
        if column.get("transform") == "CONDITIONAL":
            items.append(
                {
                    "column": column.get("name"),
                    "summary": "CASE expression",
                    "branch_count": len(column.get("case_branches") or []),
                }
            )
    return items


def _business_action(step: dict[str, Any]) -> str:
    role = step.get("role")
    summary = step.get("business_summary") or ""
    role_text = {
        "filter": "按条件筛选/保留/排除记录",
        "join": "关联补充信息或命中名单",
        "dedup": "构造去重结果或按窗口取最新/首次记录",
        "aggregate": "按粒度汇总指标",
        "window": "排序取首、取末、取最新或跨行比较",
        "case_when": "按条件派生分类、状态或标签",
        "union": "合并多个来源、渠道或策略分支",
        "lateral_view": "展开数组、明细项或复杂类型",
    }.get(role, "加工中间结果")
    return f"{role_text}；{summary}" if summary else role_text


def _add_columns(insight: dict[str, Any], profile: dict[str, Any]) -> None:
    for index, item in enumerate(profile.get("end_to_end_lineage") or []):
        column_name = item.get("column")
        if not column_name:
            continue
        column_id = _column_id(column_name)
        insight["objects"]["columns"][column_id] = {
            "id": column_id,
            "type": "output_column",
            "name": column_name,
            "label": _column_label(profile, column_name),
            "transform": item.get("transform"),
            "trace_complete": item.get("trace_complete", True),
            "trace_incomplete_reasons": item.get("trace_incomplete_reasons") or [],
            "expression": item.get("expression"),
            "semantic_role": _semantic_role(column_name),
            "source_columns": [_physical_column_id(src.get("table"), src.get("column")) for src in item.get("physical_sources") or []],
            "physical_sources": copy.deepcopy(item.get("physical_sources") or []),
            "evidence": [{"source": "profile", "path": f"$.end_to_end_lineage[{index}]"}],
        }
        for src in item.get("physical_sources") or []:
            table = src.get("table")
            col = src.get("column")
            if table:
                _add_link(insight, column_id, _table_id(table), "derived_from")
            if table and col:
                physical_id = _physical_column_id(table, col)
                insight["objects"]["columns"].setdefault(physical_id, {
                    "id": physical_id,
                    "type": "physical_column",
                    "name": col,
                    "table": table,
                    "label": _metadata_comment(profile, table, col) or col,
                    "transform": src.get("transform"),
                    "evidence": [{"source": "profile", "path": f"$.end_to_end_lineage[{index}].physical_sources"}],
                })
                _add_link(insight, column_id, physical_id, "derived_from_column")


def _add_rules(insight: dict[str, Any], profile: dict[str, Any], scope_id_map: dict[str, str]) -> None:
    for index, candidate in enumerate(profile.get("business_rule_candidates") or []):
        scope_ref = candidate.get("scope_id") or candidate.get("scope_name")
        scope_object_id = _lookup_scope(scope_ref, scope_id_map)
        rule_id = _rule_id(scope_object_id or "global", candidate.get("source") or "rule", index)
        fields = []
        for detail in candidate.get("field_details") or []:
            field_name = detail.get("column")
            if not field_name:
                continue
            field_id = _column_id(field_name)
            fields.append({
                "field_id": field_id,
                "field": field_name,
                "table": detail.get("table"),
                "comment": detail.get("comment"),
                "type": detail.get("type"),
                "role": _field_role(field_name, candidate),
                "meaning": _field_meaning(field_name, detail),
            })
            insight["objects"]["columns"].setdefault(field_id, {
                "id": field_id,
                "type": "rule_field",
                "name": field_name,
                "label": detail.get("comment") or field_name,
                "semantic_role": _semantic_role(field_name),
                "evidence": [{"source": "profile", "path": f"$.business_rule_candidates[{index}].field_details"}],
            })
            _add_link(insight, rule_id, field_id, "uses_field")
        insight["objects"]["rules"][rule_id] = {
            "id": rule_id,
            "type": "rule",
            "title": _rule_title(candidate),
            "rule_kind": candidate.get("rule_kind"),
            "source": candidate.get("source"),
            "scope_ids": [scope_object_id] if scope_object_id else [],
            "condition_summary": candidate.get("raw_summary"),
            "condition_expression": None if candidate.get("expression_omitted") else candidate.get("expression"),
            "fields": fields,
            "operator_hints": candidate.get("operator_hints") or [],
            "result": _rule_result(candidate),
            "confidence": "derived",
            "expression_omitted": candidate.get("expression_omitted", False),
            "fields_truncated": candidate.get("fields_truncated", False),
            "evidence": [{"source": "profile", "path": f"$.business_rule_candidates[{index}]"}],
        }
        if scope_object_id:
            _add_link(insight, rule_id, scope_object_id, "implemented_by")


def _add_sections(
    insight: dict[str, Any],
    profile: dict[str, Any],
    scope_id_map: dict[str, str],
    business_doc_index: dict[str, Any] | None,
) -> None:
    if business_doc_index:
        for index, section in enumerate(business_doc_index.get("sections") or []):
            section_id = section.get("id") or f"section:doc:{index}"
            normalized = copy.deepcopy(section)
            normalized["id"] = section_id
            normalized.setdefault("source", "business_doc_index")
            insight["objects"]["sections"][section_id] = normalized
            for scope_id in section.get("scope_ids") or []:
                _add_link(insight, section_id, _lookup_scope(scope_id, scope_id_map) or scope_id, "references")
            for rule_id in section.get("rule_ids") or []:
                _add_link(insight, section_id, rule_id, "references")
            for column_id in section.get("column_ids") or []:
                _add_link(insight, section_id, _column_id(str(column_id).removeprefix("column:")), "references")
        return

    sections = (profile.get("business_profile") or {}).get("sections") or []
    for index, section in enumerate(sections):
        scope_object_id = _lookup_scope(section.get("scope_id") or section.get("name"), scope_id_map)
        section_id = f"section:{_safe_id(section.get('name') or section.get('scope_id') or index)}"
        related_rules = [
            rule_id
            for rule_id, rule in insight["objects"]["rules"].items()
            if scope_object_id and scope_object_id in (rule.get("scope_ids") or [])
        ]
        insight["objects"]["sections"][section_id] = {
            "id": section_id,
            "title": section.get("name") or section.get("scope_id") or f"section {index + 1}",
            "level": "L3",
            "body": section.get("purpose"),
            "scope_ids": [scope_object_id] if scope_object_id else [],
            "rule_ids": related_rules,
            "column_ids": [],
            "source": "derived_from_profile",
            "processing": section.get("processing") or [],
            "conditions": section.get("conditions") or [],
            "evidence": [{"source": "profile", "path": f"$.business_profile.sections[{index}]"}],
        }
        if scope_object_id:
            _add_link(insight, section_id, scope_object_id, "references")
        for rule_id in related_rules:
            _add_link(insight, section_id, rule_id, "references")

    if not sections:
        for scope_id, scope in insight["objects"]["scopes"].items():
            section_id = f"section:{_safe_id(scope['name'])}"
            insight["objects"]["sections"][section_id] = {
                "id": section_id,
                "title": scope["name"],
                "level": "L3",
                "body": scope.get("summary") or scope.get("business_action"),
                "scope_ids": [scope_id],
                "rule_ids": [],
                "column_ids": [],
                "source": "derived_from_scope_profile",
            }
            _add_link(insight, section_id, scope_id, "references")


def _add_diagnostics(insight: dict[str, Any], diagnostics: dict[str, Any], scope_id_map: dict[str, str]) -> None:
    for index, warning in enumerate(diagnostics.get("warnings_sample") or []):
        scope_id = _lookup_scope(warning.get("scope"), scope_id_map)
        diag_id = f"diagnostic:{_safe_id(warning.get('type') or 'warning')}:{index}"
        insight["objects"]["diagnostics"][diag_id] = {
            "id": diag_id,
            "type": "diagnostic",
            "severity": "warning",
            "code": warning.get("type"),
            "message": warning.get("msg"),
            "scope_ids": [scope_id] if scope_id else [],
            "meaning": _diagnostic_meaning(warning.get("type")),
            "evidence": [{"source": "diagnostics", "path": f"$.warnings_sample[{index}]"}],
        }
        if scope_id:
            _add_link(insight, diag_id, scope_id, "affects")


def _add_knowledge(insight: dict[str, Any], business_knowledge: dict[str, Any] | None) -> None:
    if not business_knowledge:
        return
    for kind in ("tasks", "tables", "columns", "rules", "domains"):
        for key, value in (business_knowledge.get(kind) or {}).items():
            knowledge_id = f"knowledge:{kind}:{_safe_id(key)}"
            item = copy.deepcopy(value) if isinstance(value, dict) else {"definition": str(value)}
            item.update({"id": knowledge_id, "type": "knowledge", "target": key, "source": "business_knowledge"})
            insight["objects"]["knowledge"][knowledge_id] = item


def _add_graph_links(insight: dict[str, Any], lineage: dict[str, Any], scope_id_map: dict[str, str]) -> None:
    for edge in (lineage.get("scope_graph") or {}).get("edges") or []:
        from_id = _input_object_id(edge.get("from"), scope_id_map)
        to_id = _input_object_id(edge.get("to"), scope_id_map)
        if from_id and to_id:
            _add_link(insight, from_id, to_id, "feeds")

    for column_id, column in insight["objects"]["columns"].items():
        if column.get("type") != "output_column":
            continue
        for scope_id in _scopes_for_column(lineage, column.get("name"), scope_id_map):
            column.setdefault("scope_ids", []).append(scope_id)
            _add_link(insight, scope_id, column_id, "produces")


def _mark_hidden_implementation_scopes(insight: dict[str, Any]) -> None:
    """Mark lineage-only implementation scopes hidden in the default business graph."""

    scopes = insight["objects"]["scopes"]
    outgoing_feeds = {link["from"] for link in insight["links"] if link["type"] == "feeds"}
    hidden_ids = {
        scope_id
        for scope_id, scope in scopes.items()
        if scope.get("profiled") is False
        and scope_id not in outgoing_feeds
    }
    for scope_id in hidden_ids:
        scopes[scope_id]["hidden_in_business_view"] = True
        scopes[scope_id]["hidden_reason"] = "lineage-only scope has no downstream feeds; inspect in full mode because this may indicate parser or SQL lineage issues"
    insight.setdefault("graph_diagnostics", {})["hidden_business_scope_ids"] = sorted(hidden_ids)
    insight["graph_diagnostics"]["dangling_scope_ids"] = sorted(hidden_ids)


def _finalize_task_counts(insight: dict[str, Any]) -> None:
    visible_scope_count = sum(
        1 for scope in insight["objects"]["scopes"].values() if not scope.get("hidden_in_business_view")
    )
    full_scope_count = len(insight["objects"]["scopes"])
    output_column_count = sum(
        1 for column in insight["objects"]["columns"].values() if column.get("type") == "output_column"
    )
    input_table_count = sum(
        1 for table in insight["objects"]["tables"].values() if table.get("role") == "input"
    )
    insight["task"]["output_column_count"] = output_column_count
    insight["task"]["visible_scope_count"] = visible_scope_count
    insight["task"]["full_graph_scope_count"] = full_scope_count
    insight["task"]["hidden_scope_count"] = full_scope_count - visible_scope_count
    insight["task"]["dag_node_count"] = visible_scope_count + input_table_count
    insight["task"]["full_dag_node_count"] = full_scope_count + input_table_count


def _scopes_for_column(lineage: dict[str, Any], column_name: str | None, scope_id_map: dict[str, str]) -> list[str]:
    if not column_name:
        return []
    scopes: list[str] = []
    for scope_key, scope in (lineage.get("scopes") or {}).items():
        for column in scope.get("columns") or []:
            if column.get("name") == column_name:
                scope_id = _lookup_scope(scope_key, scope_id_map)
                if scope_id and scope_id not in scopes:
                    scopes.append(scope_id)
    return scopes


def _build_capabilities(
    insight: dict[str, Any],
    business_doc: str | None,
    business_doc_index: dict[str, Any] | None,
    business_knowledge: dict[str, Any] | None,
) -> None:
    insight["capabilities"] = {
        "has_business_doc": business_doc is not None,
        "has_business_doc_index": business_doc_index is not None,
        "has_business_knowledge": business_knowledge is not None,
        "has_complete_lineage": insight["task"].get("trace_incomplete_count", 0) == 0,
        "has_scope_graph": bool([link for link in insight["links"] if link["type"] == "feeds"]),
        "has_field_lineage": bool(insight["objects"]["columns"]),
        "has_rule_index": bool(insight["objects"]["rules"]),
        "has_diagnostics": bool(insight["objects"]["diagnostics"]),
    }


def _column_label(profile: dict[str, Any], column_name: str) -> str:
    target_table = profile.get("target_table")
    comment = _metadata_comment(profile, target_table, column_name) if target_table else None
    return comment or column_name


def _metadata_comment(profile: dict[str, Any], table_name: str | None, column_name: str | None) -> str | None:
    if not table_name or not column_name:
        return None
    related = profile.get("related_metadata") or {}
    for section in ("input_tables", "output_tables"):
        metadata = (related.get(section) or {}).get(table_name) or {}
        for detail in metadata.get("column_details") or []:
            if detail.get("name") == column_name:
                return detail.get("comment")
    return None


def _rule_title(candidate: dict[str, Any]) -> str:
    source = candidate.get("source") or "RULE"
    kind = candidate.get("rule_kind") or "condition"
    title_map = {
        "partition_filter": "分区/日期过滤",
        "business_filter": "业务筛选规则",
        "join_condition": "关联/命中规则",
    }
    return f"{title_map.get(kind, kind)}（{source}）"


def _rule_result(candidate: dict[str, Any]) -> str:
    if candidate.get("source") == "JOIN_ON":
        return "用于关联上游 scope、物理表或名单，并为下游补充字段或命中结果。"
    return "满足条件的记录会被保留或进入后续加工；不满足条件的记录被过滤或不参与该分支。"


def _field_meaning(field_name: str, detail: dict[str, Any]) -> str:
    comment = detail.get("comment")
    if comment:
        return f"{field_name} 表示{comment}，在规则中用于{_semantic_role_text(field_name)}。"
    return f"{field_name} 在规则中用于{_semantic_role_text(field_name)}。"


def _field_role(field_name: str, candidate: dict[str, Any]) -> str:
    source = candidate.get("source")
    if source == "JOIN_ON":
        return "join_key"
    return _semantic_role(field_name)


def _semantic_role(field_name: str | None) -> str:
    name = (field_name or "").lower()
    if any(token in name for token in ("dt", "date", "time", "begin", "start", "end")):
        return "time_or_partition"
    if any(token in name for token in ("id", "key", "code", "no", "nbr")):
        return "identifier"
    if any(token in name for token in ("amt", "amount", "cnt", "count", "num", "score", "rate", "days")):
        return "metric"
    if any(token in name for token in ("status", "state", "type", "flag", "ind", "channel", "result")):
        return "classification"
    return "business_field"


def _semantic_role_text(field_name: str) -> str:
    role = _semantic_role(field_name)
    return {
        "time_or_partition": "时间窗口、分区或排序判断",
        "identifier": "关联、去重或标识记录",
        "metric": "金额、数量、分数或间隔指标判断",
        "classification": "状态、类型、渠道或标签判断",
        "business_field": "业务条件判断",
    }[role]


def _diagnostic_meaning(code: str | None) -> str:
    return {
        "filter_in_join_on_clause": "JOIN ON 中包含业务过滤条件，不一定是错误，但需要理解 JOIN 同时承担关联和筛选作用。",
        "duplicate_table_in_union": "同一物理表出现在多个 UNION 分支，通常表示按渠道或策略复用来源，需要确认是否符合预期。",
        "star_not_expanded": "SELECT * 未完整展开，通常需要补充 schema。",
        "unresolved_unqualified_no_schema": "无 schema 时未限定字段无法精确绑定来源表。",
    }.get(code or "", "解析器提示，需要结合 scope 和 SQL 判断影响。")


def _input_object_id(value: Any, scope_id_map: dict[str, str]) -> str | None:
    if not value:
        return None
    text = str(value)
    scope = _lookup_scope(text, scope_id_map)
    if scope:
        return scope
    if "." in text:
        return _table_id(text)
    return _scope_object_id(_display_scope_name(text))


def _lookup_scope(value: Any, scope_id_map: dict[str, str]) -> str | None:
    if not value:
        return None
    text = str(value)
    if text in scope_id_map:
        return scope_id_map[text]
    if text.startswith("scope:"):
        return text
    display = _display_scope_name(text)
    if display in scope_id_map:
        return scope_id_map[display]
    return None


def _display_scope_name(scope_id: str) -> str:
    if scope_id == "ROOT":
        return "ROOT"
    if ":" in scope_id:
        return scope_id.split(":", 1)[1]
    return scope_id


def _scope_object_id(name: str) -> str:
    return f"scope:{_safe_id(name)}"


def _column_id(name: str) -> str:
    return f"column:{_safe_id(name)}"


def _table_id(name: str) -> str:
    return f"table:{name}"


def _physical_column_id(table: str | None, column: str | None) -> str:
    return f"physical_column:{table}.{column}"


def _rule_id(scope_id: str, source: str, index: int) -> str:
    return f"rule:{_safe_id(scope_id)}:{_safe_id(source)}:{index}"


def _safe_id(value: Any) -> str:
    text = str(value or "unknown").strip()
    text = text.removeprefix("scope:")
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^0-9A-Za-z_\-:.]+", "_", text)
    return text.strip("_") or "unknown"


def _looks_like_table(value: Any) -> bool:
    if not value:
        return False
    text = str(value)
    if text == "CONSTANT":
        return False
    if text.startswith(("cte:", "subq:", "union:", "ROOT", "scope:")):
        return False
    return "." in text


def _add_link(insight: dict[str, Any], from_id: str, to_id: str, link_type: str) -> None:
    if not from_id or not to_id:
        return
    insight["links"].append({"from": from_id, "to": to_id, "type": link_type})


def _dedupe_links(insight: dict[str, Any]) -> None:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, str]] = []
    for link in insight["links"]:
        key = (link["from"], link["to"], link["type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(link)
    insight["links"] = deduped
