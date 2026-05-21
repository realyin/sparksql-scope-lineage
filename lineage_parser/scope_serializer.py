"""JSON serializer for scope-based lineage results.

Converts ScopeLineageResult dataclasses to JSON-safe dicts, handling:
  - from_ -> "from" renaming in ScopeGraphEdge
  - Omitting None / empty default values
  - SourceRef / ScopeColumn / ScopeData recursive conversion
"""

from __future__ import annotations

import copy as _copy
import json
from pathlib import Path
from typing import Any

from .scope_types import (
    CONSTANT_SCOPE_ID,
    SYSTEM_SCOPE_ID,
    DiagnosticWarning,
    Diagnostics,
    ScopeColumn,
    ScopeData,
    ScopeFilter,
    ScopeGraph,
    ScopeGraphEdge,
    ScopeJoin,
    ScopeLineageResult,
    SourceRef,
)
from .end_to_end import build_end_to_end_lineage
from .scope_profile import build_scope_profile


_SCHEMA_PATH = Path(__file__).parent / "schemas" / "lineage.schema.json"
_schema_cache: dict | None = None

PROFILE_MAX_EXPRESSION_CHARS = 200
PROFILE_MAX_METADATA_COLUMNS_PER_TABLE = 5
PROFILE_MAX_SEMANTIC_COLUMNS_PER_TABLE = 12
PROFILE_MAX_SEMANTIC_OUTPUT_LINEAGE = 80
PROFILE_MAX_SEMANTIC_SOURCE_COLUMNS = 2
PROFILE_MAX_SEMANTIC_CONDITION_FIELDS = 8
PROFILE_MAX_PHYSICAL_SOURCES_PER_COLUMN = 5
PROFILE_MAX_LOGIC_ITEMS_PER_TYPE = 10
PROFILE_MAX_WARNINGS = 20
PROFILE_MAX_SOURCE_TABLES = 30
PROFILE_MAX_IMPORTANT_COLUMNS = 12
PROFILE_MAX_FILTERS_SUMMARY = 30
PROFILE_MAX_EXPRESSION_CATALOG = 30
PROFILE_MAX_BUSINESS_RULE_CANDIDATES = 12
PROFILE_MAX_BUSINESS_RULE_FIELDS = 12
PROFILE_MAX_CONDITION_GROUPS_PER_RULE = 20
PROFILE_MAX_BUSINESS_SECTIONS = 12
PROFILE_MAX_SECTION_CONDITIONS = 6
PROFILE_TARGET_MAX_BYTES = 80 * 1024


def _load_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _copy.deepcopy(_schema_cache)


def validate_lineage_json(result: "ScopeLineageResult") -> dict:
    """Convert result to dict and validate against JSON Schema. Returns dict on success.

    Raises jsonschema.ValidationError if the output violates the schema.
    Raises ImportError if jsonschema is not installed.
    """
    try:
        import jsonschema
    except ImportError:
        raise ImportError("jsonschema is required: pip install jsonschema")
    d = to_dict(result)
    jsonschema.validate(d, _load_schema())
    return d


def validate_cross_references(data: dict) -> list[str]:
    """Check that all scope IDs in edges and column sources exist in the output.

    Returns a list of error strings (empty list = valid).
    Physical table nodes are in scope_graph.nodes but not in scopes dict — both are valid targets.
    UNKNOWN scope is allowed (used when column resolution fails).
    """
    errors: list[str] = []
    known_scopes: set[str] = set(data.get("scopes", {}).keys())
    all_nodes: set[str] = set(data.get("scope_graph", {}).get("nodes", []))
    valid_ids = known_scopes | all_nodes | {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}

    for edge in data.get("scope_graph", {}).get("edges", []):
        for key in ("from", "to"):
            sid = edge.get(key)
            if sid and sid not in valid_ids:
                errors.append(f"scope_graph edge {key}={sid!r} not in known scopes/nodes")

    for scope_id, scope_data in data.get("scopes", {}).items():
        for col in scope_data.get("columns", []):
            for src in col.get("sources", []):
                sid = src.get("scope")
                if sid and sid not in valid_ids and sid != "UNKNOWN":
                    errors.append(
                        f"scope={scope_id!r} col={col.get('name')!r} "
                        f"source scope={sid!r} not in known scopes/nodes"
                    )

    return errors


def to_dict(obj: Any) -> Any:
    """Recursively convert a dataclass (or nested structure) to a JSON-safe dict.

    - ScopeGraphEdge.from_ -> {"from": ..., "to": ...}
    - None fields are omitted
    - Empty lists/dicts are kept (they convey "no entries")
    """
    if isinstance(obj, ScopeLineageResult):
        return _result_to_dict(obj)
    if isinstance(obj, ScopeData):
        return _scope_data_to_dict(obj)
    if isinstance(obj, ScopeColumn):
        return _scope_column_to_dict(obj)
    if isinstance(obj, ScopeGraphEdge):
        return obj.to_dict()
    if isinstance(obj, ScopeGraph):
        return _scope_graph_to_dict(obj)
    if isinstance(obj, SourceRef):
        return {"scope": obj.scope, "column": obj.column}
    if isinstance(obj, ScopeJoin):
        return _scope_join_to_dict(obj)
    if isinstance(obj, ScopeFilter):
        return _scope_filter_to_dict(obj)
    if isinstance(obj, Diagnostics):
        return _diagnostics_to_dict(obj)
    if isinstance(obj, DiagnosticWarning):
        return {"type": obj.type, "scope": obj.scope, "msg": obj.msg}
    if isinstance(obj, list):
        return [to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def to_json(result: ScopeLineageResult, indent: int = 2) -> str:
    """Serialize a ScopeLineageResult to a JSON string."""
    return json.dumps(to_dict(result), ensure_ascii=False, indent=indent, default=str)


def to_profile_dict(result: ScopeLineageResult) -> dict:
    """Return compact LLM/profile-oriented output without full intermediate scopes."""
    full = to_dict(result)
    profile = {
        "task_name": full["task_id"],
        "target_table": full["target_table"],
        "stmt_kind": full["stmt_kind"],
        "source_tables": full.get("source_tables", []),
        "related_metadata": full.get("related_metadata", {}),
        "scope_profile": full.get("scope_profile", {}),
        "end_to_end_lineage": full.get("end_to_end_lineage", []),
        "diagnostics": full.get("diagnostics", {}),
    }
    profile.update(_build_llm_profile_indexes(profile, full))
    return _compact_profile(profile)


def to_profile_json(result: ScopeLineageResult, indent: int = 2) -> str:
    """Serialize compact LLM/profile-oriented output to JSON."""
    return json.dumps(to_profile_dict(result), ensure_ascii=False, indent=indent, default=str)


def _compact_profile(profile: dict) -> dict:
    profile = _copy.deepcopy(profile)
    _compact_list_field(profile, "source_tables", PROFILE_MAX_SOURCE_TABLES)
    priority_columns = _metadata_priority_columns(profile)
    _compact_related_metadata(profile.get("related_metadata", {}), priority_columns)
    _compact_end_to_end_lineage(profile.get("end_to_end_lineage", []))
    _compact_scope_profile(profile.get("scope_profile", {}))
    _compact_business_rule_candidates(profile.get("business_rule_candidates", []))
    _compact_business_profile(profile.get("business_profile", {}))
    _compact_semantic_profile(profile.get("semantic_profile", {}))
    _compact_list_field(profile, "filters_summary", PROFILE_MAX_FILTERS_SUMMARY)
    _compact_list_field(profile, "expression_catalog", PROFILE_MAX_EXPRESSION_CATALOG)
    _compact_list_field(profile, "business_rule_candidates", PROFILE_MAX_BUSINESS_RULE_CANDIDATES)
    profile["diagnostics"] = _compact_profile_diagnostics(profile.get("diagnostics", {}))
    _enforce_profile_size_budget(profile)
    return profile


def _build_llm_profile_indexes(profile: dict, full: dict | None = None) -> dict:
    end_to_end = profile.get("end_to_end_lineage", [])
    steps = profile.get("scope_profile", {}).get("steps", [])
    operations = _unique(
        operation
        for step in steps
        for operation in step.get("operations", [])
        if operation != "pass_through"
    )
    output_columns = [item.get("column") for item in end_to_end if item.get("column")]
    business_rule_candidates = _build_business_rule_candidates(full or {}, profile)
    business_profile = _build_business_profile(profile, business_rule_candidates)
    enriched_profile = {
        **profile,
        "business_rule_candidates": business_rule_candidates,
        "business_profile": business_profile,
    }
    semantic_profile = _build_semantic_profile(enriched_profile, full or {})
    return {
        "summary": _build_profile_summary(profile, operations, output_columns),
        "grain": _infer_grain(end_to_end, steps),
        "important_columns": _build_important_columns(end_to_end),
        "expression_catalog": _build_expression_catalog(end_to_end, steps),
        "filters_summary": _build_filters_summary(steps),
        "business_rule_candidates": business_rule_candidates,
        "business_profile": business_profile,
        "semantic_profile": semantic_profile,
        "read_order": [
            "summary",
            "semantic_profile",
            "business_profile",
            "grain",
            "scope_profile.steps",
            "business_rule_candidates",
            "important_columns",
            "end_to_end_lineage",
            "related_metadata",
        ],
        "compact_policy": {
            "max_expression_chars": PROFILE_MAX_EXPRESSION_CHARS,
            "max_source_tables": PROFILE_MAX_SOURCE_TABLES,
            "max_metadata_columns_per_table": PROFILE_MAX_METADATA_COLUMNS_PER_TABLE,
            "max_physical_sources_per_column": PROFILE_MAX_PHYSICAL_SOURCES_PER_COLUMN,
            "max_business_rule_candidates": PROFILE_MAX_BUSINESS_RULE_CANDIDATES,
            "max_business_rule_fields": PROFILE_MAX_BUSINESS_RULE_FIELDS,
            "max_business_sections": PROFILE_MAX_BUSINESS_SECTIONS,
            "target_max_bytes": PROFILE_TARGET_MAX_BYTES,
            "full_detail_files": ["lineage.json", "diagnostics.json"],
        },
    }


def _build_profile_summary(profile: dict, operations: list[str], output_columns: list[str]) -> dict:
    source_tables = profile.get("source_tables") or []
    target_table = profile.get("target_table")
    summary = {
        "task_name": profile.get("task_name"),
        "target_table": target_table,
        "stmt_kind": profile.get("stmt_kind"),
        "input_table_count": len(source_tables),
        "output_column_count": len(output_columns),
        "main_operations": operations,
    }
    if source_tables or target_table:
        table_text = f"{len(source_tables)}张输入表" if source_tables else "上游数据"
        op_text = "、".join(operations) if operations else "字段整理"
        summary["main_process"] = f"从{table_text}读取数据，经过{op_text}后写入 {target_table}"
    return summary


def _infer_grain(end_to_end: list[dict], steps: list[dict]) -> dict:
    aggregate_steps = [step for step in steps if "aggregate" in step.get("operations", [])]
    candidate_keys = [
        item.get("column")
        for item in end_to_end
        if _looks_like_key_column(item.get("column", ""))
    ]
    keys = _unique(col for col in candidate_keys if col)[:8]
    evidence = []
    if aggregate_steps:
        evidence.append("aggregate_steps")
    if keys:
        evidence.append("id_like_output_columns")
    if any(item.get("column") == "dt" for item in end_to_end):
        evidence.append("partition_column_dt")
    return {
        "type": "aggregate_level" if aggregate_steps else "record_level",
        "keys": keys,
        "key_type": "candidate_output_keys",
        "confidence": "medium" if keys or aggregate_steps else "low",
        "evidence": evidence,
        "note": "keys are heuristic candidate output identifiers, not a verified primary key",
    }


def _build_important_columns(end_to_end: list[dict]) -> list[dict]:
    important: list[dict] = []
    for item in end_to_end:
        column = item.get("column")
        if not column:
            continue
        transform = item.get("transform")
        reasons = _column_importance_reasons(column, transform, item.get("physical_sources", []))
        if not reasons:
            continue
        important.append({
            "column": column,
            "transform": transform,
            "importance": "high" if transform not in ("DIRECT", "CONSTANT") else "medium",
            "reasons": reasons,
        })
    return important[:PROFILE_MAX_IMPORTANT_COLUMNS]


def _build_expression_catalog(end_to_end: list[dict], steps: list[dict]) -> list[dict]:
    catalog: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in end_to_end:
        expression = item.get("expression")
        transform = item.get("transform")
        if not expression or transform in ("DIRECT", "CONSTANT"):
            continue
        key = (transform or "", expression)
        if key in seen:
            continue
        seen.add(key)
        catalog.append({
            "id": f"expr_{len(catalog) + 1}",
            "type": transform,
            "columns": [item.get("column")],
            "summary": _expression_summary(transform, expression),
            "expression_length": len(expression),
        })

    for step in steps:
        logic = step.get("logic") or {}
        for item in logic.get("case_when", []):
            key = ("CASE_WHEN", item.get("column", ""))
            if key in seen:
                continue
            seen.add(key)
            catalog.append({
                "id": f"expr_{len(catalog) + 1}",
                "type": "CASE_WHEN",
                "columns": [item.get("column")],
                "summary": item.get("summary"),
                "branch_count": item.get("branch_count"),
            })
    return catalog[:PROFILE_MAX_EXPRESSION_CATALOG]


def _build_filters_summary(steps: list[dict]) -> list[dict]:
    filters: list[dict] = []
    seen: set[str] = set()
    for step in steps:
        for expression in (step.get("logic") or {}).get("filters", []):
            if not expression or expression in seen:
                continue
            seen.add(expression)
            filters.append({
                "scope": step.get("name"),
                "expression": expression,
                "type": _filter_type(expression),
            })
    return filters[:PROFILE_MAX_FILTERS_SUMMARY]


def _build_business_rule_candidates(full: dict, profile: dict) -> list[dict]:
    """Extract structured condition evidence for downstream business summaries.

    This intentionally stays factual: it groups WHERE/HAVING/JOIN conditions,
    lists referenced fields with metadata, and leaves business naming to the LLM.
    """
    scopes = full.get("scopes") or {}
    candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for scope_id, scope_data in scopes.items():
        for source, filters in (("WHERE", scope_data.get("filters") or []), ("HAVING", scope_data.get("having") or [])):
            for scope_filter in filters:
                expression = scope_filter.get("expression") or ""
                key = (scope_id, source, expression)
                if not expression or key in seen:
                    continue
                seen.add(key)
                field_refs = _business_field_refs(full, profile, scope_filter.get("columns") or [])
                candidates.append({
                    "scope_id": scope_id,
                    "scope_name": _profile_scope_name(profile, scope_id),
                    "source": source,
                    "rule_kind": _filter_type(expression),
                    "condition_group_type": _condition_group_type(expression),
                    "fields": _field_names(field_refs),
                    "field_details": field_refs,
                    "operator_hints": _operator_hints(expression),
                    "raw_summary": _condition_raw_summary(expression, field_refs),
                    "expression": expression,
                })

        for join in scope_data.get("joins") or []:
            expression = join.get("condition_expression") or ""
            key = (scope_id, "JOIN_ON", expression)
            if not expression or key in seen:
                continue
            seen.add(key)
            field_refs = _business_field_refs(full, profile, join.get("condition_columns") or [])
            candidates.append({
                "scope_id": scope_id,
                "scope_name": _profile_scope_name(profile, scope_id),
                "source": "JOIN_ON",
                "rule_kind": "join_condition",
                "join_type": join.get("join_type"),
                "right": join.get("right_scope"),
                "condition_group_type": _condition_group_type(expression),
                "fields": _field_names(field_refs),
                "field_details": field_refs,
                "operator_hints": _operator_hints(expression),
                "raw_summary": _condition_raw_summary(expression, field_refs),
                "expression": expression,
            })
    return candidates


def _build_business_profile(profile: dict, business_rule_candidates: list[dict]) -> dict:
    related_metadata = profile.get("related_metadata") or {}
    target_table = profile.get("target_table")
    target_label = _table_label(related_metadata, target_table) if target_table else None
    source_labels = [
        _table_label(related_metadata, table) or table
        for table in (profile.get("source_tables") or [])[:5]
    ]
    semantic_hints = _semantic_hints(profile, business_rule_candidates)
    objective_summary = _objective_summary(target_table, target_label, source_labels, semantic_hints)
    return {
        "objective": {
            "summary": objective_summary,
            "target_table": target_table,
            "target_table_label": target_label,
            "primary_decision": "是否保留/纳入目标结果" if business_rule_candidates else None,
            "semantic_hints": semantic_hints[:12],
            "confidence": "medium" if semantic_hints or business_rule_candidates else "low",
            "note": "Program-generated business evidence; use table/column metadata and business_rule_candidates for final wording.",
        },
        "sections": _business_sections(profile, business_rule_candidates),
    }


def _build_semantic_profile(profile: dict, full: dict) -> dict:
    return {
        "version": "2.0",
        "task": _semantic_task(profile, full),
        "business_summary": _semantic_business_summary(profile),
        "tables": _semantic_tables(profile),
        "process": _semantic_process(profile),
        "rules": _semantic_rules(profile),
        "fields": _semantic_fields(profile),
        "quality": _semantic_quality(profile, full),
    }


def _semantic_task(profile: dict, full: dict) -> dict:
    target_table = profile.get("target_table")
    stmt_kind = profile.get("stmt_kind")
    return {
        "task_name": profile.get("task_name"),
        "stmt_kind": stmt_kind,
        "statement_count": 1,
        "source_table_count": len(profile.get("source_tables") or []),
        "target_table_count": 1 if target_table else 0,
        "target_tables": [
            {
                "table": target_table,
                "table_cn": _table_label(profile.get("related_metadata") or {}, target_table),
                "write_modes": [stmt_kind] if stmt_kind else [],
                "statement_ids": [profile.get("task_name")] if profile.get("task_name") else [],
            }
        ] if target_table else [],
    }


def _semantic_business_summary(profile: dict) -> dict:
    objective = (profile.get("business_profile") or {}).get("objective") or {}
    return {
        "objective": objective.get("summary") or (profile.get("summary") or {}).get("main_process"),
        "main_business_object": _table_label(profile.get("related_metadata") or {}, profile.get("target_table")) or profile.get("target_table"),
        "process_summary": [
            step.get("business_summary")
            for step in (profile.get("scope_profile") or {}).get("steps") or []
            if step.get("business_summary")
        ][:8],
        "semantic_confidence": objective.get("confidence") or "low",
        "evidence": [{"type": "parsed_sql", "source": "profile", "path": "$.summary"}],
        "inference_notes": [objective.get("note")] if objective.get("note") else [],
    }


def _semantic_tables(profile: dict) -> dict:
    related = profile.get("related_metadata") or {}
    return {
        "inputs": [
            _semantic_table_item(table, metadata, "input", profile)
            for table, metadata in (related.get("input_tables") or {}).items()
        ],
        "outputs": [
            _semantic_table_item(table, metadata, "output", profile)
            for table, metadata in (related.get("output_tables") or {}).items()
        ],
    }


def _semantic_table_item(table: str, metadata: dict, role: str, profile: dict) -> dict:
    table_metadata = metadata.get("table_metadata") or {}
    return {
        "table": table,
        "table_cn": table_metadata.get("table_name_cn") or table_metadata.get("table_desc"),
        "description": table_metadata.get("table_desc"),
        "role": "主数据来源" if role == "input" else "输出结果表",
        "used_for": _semantic_table_used_for(table, role, profile),
        "used_columns": [
            _semantic_used_column(table, detail, profile)
            for detail in metadata.get("column_details") or []
        ],
        "metadata_complete": metadata.get("metadata_complete"),
    }


def _semantic_table_used_for(table: str, role: str, profile: dict) -> str:
    if role == "output":
        return "承载该 SQL 任务写入的结果数据"
    fields = []
    for candidate in profile.get("business_rule_candidates") or []:
        for detail in candidate.get("field_details") or []:
            if detail.get("table") == table and detail.get("column"):
                fields.append(detail["column"])
    if fields:
        return "提供 " + "、".join(_unique(fields)[:8]) + " 等字段用于规则判断或关联"
    return "作为任务输入数据来源"


def _semantic_used_column(table: str, detail: dict, profile: dict) -> dict:
    column = detail.get("name")
    used_in = _column_used_in(table, column, profile)
    return {
        "name": column,
        "type": detail.get("type"),
        "comment": detail.get("comment"),
        "used_in": used_in,
        "business_role": _business_role_for_field(column or "", used_in[0], detail.get("comment")) if used_in else None,
    }


def _column_used_in(table: str, column: str | None, profile: dict) -> list[str]:
    if not column:
        return []
    used: list[str] = []
    for candidate in profile.get("business_rule_candidates") or []:
        source = candidate.get("source")
        role = "join" if source == "JOIN_ON" else "filter"
        for detail in candidate.get("field_details") or []:
            if detail.get("table") == table and detail.get("column") == column and role not in used:
                used.append(role)
    for item in profile.get("end_to_end_lineage") or []:
        for source in item.get("physical_sources") or []:
            if source.get("table") == table and source.get("column") == column and "output_expression" not in used:
                used.append("output_expression")
    return used


def _semantic_process(profile: dict) -> dict:
    steps = [
        _semantic_process_step(step, index)
        for index, step in enumerate((profile.get("scope_profile") or {}).get("steps") or [])
    ]
    return {"step_count": len(steps), "steps": steps}


def _semantic_process_step(step: dict, index: int) -> dict:
    logic = step.get("logic") or {}
    return {
        "step_no": index + 1,
        "statement_id": None,
        "scope_id": step.get("scope_id"),
        "scope_name": step.get("name"),
        "kind": step.get("kind"),
        "role": step.get("role"),
        "semantic_role": _semantic_role_for_step(step),
        "business_object": step.get("business_summary"),
        "business_summary": step.get("business_summary"),
        "direct_inputs": step.get("direct_inputs") or [],
        "direct_source_tables": step.get("direct_source_tables") or [],
        "upstream_physical_tables": step.get("physical_source_tables") or [],
        "outputs": {
            "column_count": step.get("output_columns"),
            "key_columns": _semantic_step_key_columns(step),
        },
        "logic": {
            "filters": [],
            "joins": [],
            "aggregations": logic.get("aggregations") or [],
            "window_functions": logic.get("window_functions") or [],
            "case_when": logic.get("case_when") or [],
            "distinct": logic.get("distinct", False),
            "union": {"branch_count": logic.get("union_branches")} if logic.get("union_branches") else None,
            "lateral_views": logic.get("lateral_views") or [],
        },
        "key_fields": [],
        "sql_evidence": [],
    }


def _semantic_role_for_step(step: dict) -> str:
    role = step.get("role")
    operations = set(step.get("operations") or [])
    if role == "aggregate" or "aggregate" in operations:
        return "汇总生成指标"
    if role == "dedup" or "window" in operations:
        return "按业务键排序取值或去重"
    if role == "union" or "union" in operations:
        return "合并多路来源或分支"
    if role == "join" or "join" in operations:
        return "关联补充上游信息"
    if role == "filter" or "filter" in operations:
        return "按条件筛选业务记录"
    if "case_when" in operations:
        return "按条件派生分类或标签"
    return "整理并传递上游字段"


def _semantic_step_key_columns(step: dict) -> list[dict]:
    columns: list[dict] = []
    for item in (step.get("logic") or {}).get("window_functions") or []:
        if item.get("column"):
            columns.append({
                "name": item.get("column"),
                "expression": item.get("expression"),
                "meaning": "窗口函数输出字段",
            })
    for item in (step.get("logic") or {}).get("aggregations") or []:
        if item.get("column"):
            columns.append({
                "name": item.get("column"),
                "expression": item.get("expression"),
                "meaning": "聚合输出字段",
            })
    return columns[:20]


def _semantic_rules(profile: dict) -> list[dict]:
    return [
        _semantic_rule(candidate, index)
        for index, candidate in enumerate(profile.get("business_rule_candidates") or [])
    ]


def _semantic_rule(candidate: dict, index: int) -> dict:
    rule_id = f"rule:{_safe_rule_part(candidate.get('scope_id'))}:{candidate.get('source') or 'RULE'}:{index}"
    return {
        "rule_id": rule_id,
        "statement_id": None,
        "scope_id": candidate.get("scope_id"),
        "source": candidate.get("source"),
        "rule_type": candidate.get("rule_kind"),
        "business_name": _semantic_rule_name(candidate),
        "summary": candidate.get("raw_summary"),
        "condition_groups": _semantic_condition_groups(candidate, rule_id),
        "key_fields": _semantic_rule_key_fields(candidate),
        "expression_omitted": candidate.get("expression_omitted", False),
        "truncated": candidate.get("expression_omitted", False),
        "evidence": [{"type": "parsed_sql", "source": "profile", "path": f"$.business_rule_candidates[{index}]"}],
    }


def _semantic_rule_name(candidate: dict) -> str:
    if candidate.get("source") == "JOIN_ON":
        return "关联条件"
    rule_kind = candidate.get("rule_kind")
    if rule_kind == "partition_filter":
        return "分区/日期过滤"
    if rule_kind == "soft_delete_filter":
        return "软删除过滤"
    return "业务筛选规则"


def _safe_rule_part(value: Any) -> str:
    text = str(value or "global")
    return text.replace(":", "_").replace("/", "_").replace(" ", "_")


def _semantic_condition_groups(candidate: dict, rule_id: str) -> list[dict]:
    expression = candidate.get("expression") or ""
    if not expression:
        return []
    fragments = _split_condition_fragments(expression)
    field_details = candidate.get("field_details") or []
    groups = []
    for index, fragment in enumerate(fragments[:PROFILE_MAX_CONDITION_GROUPS_PER_RULE]):
        fields = _fields_in_expression(fragment, field_details)
        compact, truncated = _truncate_text(fragment, PROFILE_MAX_EXPRESSION_CHARS)
        groups.append(_drop_none_values({
            "group_id": f"{rule_id}:g{index + 1:02d}",
            "name": _condition_group_name(fragment, fields),
            "expression": compact,
            "fields": [field.get("column") for field in fields if field.get("column")],
            "operators": _operator_hints(fragment),
            "meaning_hint": _meaning_hint_for_condition(fragment, fields),
            "evidence_type": "parsed_sql",
            "sql_fragment": compact,
            "truncated": truncated,
            "original_length": len(fragment) if truncated else None,
        }))
    return groups


def _semantic_rule_key_fields(candidate: dict) -> list[dict]:
    result = []
    used_in = "join" if candidate.get("source") == "JOIN_ON" else "filter"
    for detail in candidate.get("field_details") or []:
        result.append({
            "field": detail.get("column"),
            "table": detail.get("table"),
            "comment": detail.get("comment"),
            "business_role": _business_role_for_field(detail.get("column") or "", used_in, detail.get("comment")),
        })
    return result


def _semantic_fields(profile: dict) -> dict:
    return {
        "output_lineage": [_semantic_output_field(item, profile) for item in profile.get("end_to_end_lineage") or []],
        "important_fields": _semantic_important_fields(profile),
    }


def _semantic_output_field(item: dict, profile: dict) -> dict:
    return {
        "column": item.get("column"),
        "comment": _column_metadata(profile.get("related_metadata") or {}, profile.get("target_table"), item.get("column") or "").get("comment"),
        "expression": item.get("expression"),
        "transform": item.get("transform"),
        "scope_id": item.get("scope_id"),
        "business_role": _business_role_for_field(item.get("column") or "", "output_expression", None),
        "source_columns": item.get("physical_sources") or [],
        "trace_complete": item.get("trace_complete", True),
        "trace_incomplete_reasons": item.get("trace_incomplete_reasons") or [],
    }


def _semantic_important_fields(profile: dict) -> list[dict]:
    usage: dict[tuple[str | None, str], dict] = {}
    for candidate in profile.get("business_rule_candidates") or []:
        used_in = "join" if candidate.get("source") == "JOIN_ON" else "filter"
        for detail in candidate.get("field_details") or []:
            _record_field_usage(usage, detail, used_in)
    target_table = profile.get("target_table")
    for item in profile.get("important_columns") or []:
        _record_field_usage(usage, {"table": target_table, "column": item.get("column")}, "output_expression")
    return list(usage.values())[:40]


def _record_field_usage(usage: dict, detail: dict, used_in: str) -> None:
    column = detail.get("column")
    if not column:
        return
    table = detail.get("table")
    key = (table, column)
    item = usage.setdefault(key, {
        "field": column,
        "table": table,
        "comment": detail.get("comment"),
        "used_in": [],
        "business_role": _business_role_for_field(column, used_in, detail.get("comment")),
        "importance_reasons": [],
        "evidence": [],
    })
    if used_in not in item["used_in"]:
        item["used_in"].append(used_in)
    reason = f"used_in_{used_in}"
    if reason not in item["importance_reasons"]:
        item["importance_reasons"].append(reason)


def _business_role_for_field(column: str, used_in: str, comment: str | None = None) -> str:
    label = comment or column
    mapping = {
        "filter": f"{label} 用于筛选或准入/排除判断",
        "join": f"{label} 用于关联上游或维表",
        "window_partition": f"{label} 用于窗口分组粒度",
        "window_order": f"{label} 用于窗口排序，通常决定最新/首次/排名",
        "case_condition": f"{label} 用于条件分支判断",
        "output_expression": f"{label} 是输出字段或输出表达式的重要组成",
    }
    return mapping.get(used_in, f"{label} 是 SQL 逻辑中的关键字段")


def _semantic_quality(profile: dict, full: dict) -> dict:
    end_to_end = profile.get("end_to_end_lineage") or []
    incomplete = [item for item in end_to_end if not item.get("trace_complete", True)]
    diagnostics = profile.get("diagnostics") or {}
    return {
        "trace_complete": not incomplete,
        "trace_incomplete_columns": [
            {"column": item.get("column"), "reasons": item.get("trace_incomplete_reasons") or []}
            for item in incomplete
        ],
        "schema_coverage": _semantic_schema_coverage(profile),
        "dangling_scopes": [],
        "warnings": diagnostics.get("warnings_sample") or [],
        "known_limits": _semantic_known_limits(profile),
    }


def _semantic_schema_coverage(profile: dict) -> dict:
    related = profile.get("related_metadata") or {}
    input_tables = related.get("input_tables") or {}
    missing = [
        table
        for table, metadata in input_tables.items()
        if metadata.get("metadata_complete") is False
    ]
    return {
        "input_tables_with_metadata": len(input_tables) - len(missing),
        "input_tables_total": len(input_tables),
        "missing_metadata_tables": missing,
    }


def _semantic_known_limits(profile: dict) -> list[str]:
    limits = []
    diagnostics = profile.get("diagnostics") or {}
    warning_types = diagnostics.get("warning_types") or {}
    if "star_not_expanded" in warning_types:
        limits.append("存在 SELECT * 未完整展开，需要补充 schema 确认字段")
    if any(item.get("trace_complete") is False for item in profile.get("end_to_end_lineage") or []):
        limits.append("部分输出字段未完整追溯到物理表字段")
    if not limits:
        limits.append("部分业务语义基于字段名、表名和注释推断，需结合业务知识确认")
    return limits


def _split_condition_fragments(expression: str) -> list[str]:
    text = expression.strip()
    if not text:
        return []
    text = _strip_outer_parens(text)
    fragments = _split_top_level_or(text)
    if len(fragments) == 1:
        fragments = _split_top_level_and(text)
    flattened: list[str] = []
    for fragment in fragments:
        stripped = _strip_outer_parens(fragment.strip())
        nested_or = _split_top_level_or(stripped)
        if len(nested_or) > 1:
            flattened.extend(_strip_outer_parens(item.strip()) for item in nested_or if item.strip(" ()"))
        else:
            flattened.append(stripped)
    return [fragment for fragment in flattened if fragment]


def _split_top_level_or(expression: str) -> list[str]:
    return _split_top_level_keyword(expression, "OR")


def _split_top_level_and(expression: str) -> list[str]:
    return _split_top_level_keyword(expression, "AND")


def _split_top_level_keyword(expression: str, keyword: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    upper = expression.upper()
    i = 0
    marker = f" {keyword} "
    while i < len(expression):
        ch = expression[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        if depth == 0 and upper.startswith(marker, i):
            parts.append(expression[start:i])
            i += len(marker)
            start = i
            continue
        i += 1
    parts.append(expression[start:])
    return parts


def _strip_outer_parens(expression: str) -> str:
    text = expression.strip()
    while text.startswith("(") and text.endswith(")") and _outer_parens_wrap_all(text):
        text = text[1:-1].strip()
    return text


def _outer_parens_wrap_all(expression: str) -> bool:
    depth = 0
    quote: str | None = None
    for index, ch in enumerate(expression):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and index != len(expression) - 1:
                return False
    return depth == 0


def _fields_in_expression(expression: str, field_details: list[dict]) -> list[dict]:
    upper = expression.upper()
    result = []
    for field in field_details:
        column = field.get("column")
        if column and column.upper() in upper:
            result.append(field)
    return result


def _condition_group_name(expression: str, fields: list[dict]) -> str:
    text = expression.lower()
    field_names = {field.get("column", "").lower() for field in fields}
    if "dt" in field_names and len(field_names) == 1:
        return "分区/日期条件"
    if "dpd" in text or "in_clct_dpd" in field_names:
        return "DPD/预逾期窗口"
    if "overdue" in text:
        return "逾期状态判断"
    if "product" in text or "product_cd" in field_names:
        return "产品纳入/排除"
    if "forced_pay_off" in field_names:
        return "强制还款/结清标记"
    if "stmt_delay_ind" in field_names:
        return "账单延期标记"
    if "grace_date" in field_names:
        return "宽限期判断"
    if "repay_amt" in field_names:
        return "还款金额判断"
    if "contra_no" in field_names:
        return "超额合同命中"
    return "条件组"


def _meaning_hint_for_condition(expression: str, fields: list[dict]) -> str | None:
    name = _condition_group_name(expression, fields)
    mapping = {
        "产品纳入/排除": "按产品码或产品类型控制纳入/排除范围",
        "DPD/预逾期窗口": "基于 DPD 判断是否进入预逾期或入催窗口",
        "逾期状态判断": "根据逾期日期或结清状态判断账户是否应纳入",
        "强制还款/结清标记": "命中强制还款或结清相关标记",
        "账单延期标记": "命中账单延期标记",
        "宽限期判断": "根据宽限期和应还金额判断是否保留",
        "还款金额判断": "根据是否已有还款金额判断是否保留",
        "超额合同命中": "命中超额放款合同维表",
    }
    return mapping.get(name)


def _drop_none_values(value: dict) -> dict:
    return {key: item for key, item in value.items() if item is not None}


def _business_sections(profile: dict, business_rule_candidates: list[dict]) -> list[dict]:
    by_scope: dict[str, list[dict]] = {}
    for item in business_rule_candidates:
        by_scope.setdefault(item.get("scope_id") or "", []).append({
            "source": item.get("source"),
            "rule_kind": item.get("rule_kind"),
            "fields": item.get("fields", []),
            "raw_summary": item.get("raw_summary"),
        })

    sections: list[dict] = []
    target_table = profile.get("target_table")
    for step in (profile.get("scope_profile") or {}).get("steps", []):
        conditions = by_scope.get(step.get("scope_id") or "", [])
        processing = _processing_steps(step.get("operations", []))
        sections.append({
            "scope_id": step.get("scope_id"),
            "name": step.get("name"),
            "role": step.get("role"),
            "purpose": step.get("business_summary"),
            "inputs": step.get("direct_source_tables") or step.get("direct_inputs") or [],
            "upstream_physical_sources": step.get("physical_source_tables") or [],
            "outputs": [target_table] if step.get("scope_id") == "ROOT" and target_table else [],
            "processing": processing,
            "conditions": conditions,
        })
    return sections


def _business_field_refs(full: dict, profile: dict, refs: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str | None, str]] = set()
    for ref in refs:
        resolved_refs = _resolve_ref_to_physical(full, ref, set())
        if not resolved_refs:
            resolved_refs = [{"table": ref.get("scope"), "column": ref.get("column")}]
        for resolved in resolved_refs:
            table = resolved.get("table")
            column = resolved.get("column")
            if not column:
                continue
            key = (table, column)
            if key in seen:
                continue
            seen.add(key)
            detail = _column_metadata(profile.get("related_metadata") or {}, table, column)
            item = {"column": column}
            if table and ":" not in table and table not in {"ROOT", "UNKNOWN"}:
                item["table"] = table
            if detail.get("comment"):
                item["comment"] = detail["comment"]
            if detail.get("type"):
                item["type"] = detail["type"]
            result.append(item)
    return result


def _resolve_ref_to_physical(full: dict, ref: dict, visited: set[tuple[str, str]]) -> list[dict]:
    scope = ref.get("scope") or ""
    column = ref.get("column") or ""
    if not scope or scope in {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID, "UNKNOWN"}:
        return []
    if ":" not in scope and scope != "ROOT":
        return [{"table": scope, "column": column}]
    key = (scope, column)
    if key in visited:
        return []
    visited.add(key)
    scope_data = (full.get("scopes") or {}).get(scope) or {}
    columns = scope_data.get("columns") or []
    matched = [c for c in columns if c.get("name") == column]
    if not matched and column == "*":
        matched = columns
    resolved: list[dict] = []
    for scope_column in matched:
        for source in scope_column.get("sources") or []:
            resolved.extend(_resolve_ref_to_physical(full, source, visited))
    return resolved


def _profile_scope_name(profile: dict, scope_id: str) -> str:
    for step in (profile.get("scope_profile") or {}).get("steps", []):
        if step.get("scope_id") == scope_id:
            return step.get("name") or scope_id
    return scope_id


def _field_names(field_refs: list[dict]) -> list[str]:
    return _unique(item.get("column") for item in field_refs if item.get("column"))


def _condition_group_type(expression: str) -> str:
    text = expression.upper()
    has_or = " OR " in text
    has_and = " AND " in text
    if has_or and has_and:
        return "MIXED_AND_OR"
    if has_or:
        return "OR_GROUP"
    if has_and:
        return "AND_GROUP"
    return "SINGLE_CONDITION"


def _operator_hints(expression: str) -> list[str]:
    text = expression.upper()
    hints = []
    checks = [
        ("IN", " IN "),
        ("NOT_IN", " NOT " if " NOT " in text and " IN " in text else ""),
        ("IS_NULL", " IS NULL"),
        ("IS_NOT_NULL", " IS NULL" if "NOT " in text and " IS NULL" in text else ""),
        ("BETWEEN", " BETWEEN "),
        ("DATEDIFF", "DATEDIFF("),
        ("SUBSTRING", "SUBSTRING("),
        ("COALESCE", "COALESCE("),
        (">=", ">="),
        ("<=", "<="),
        (">", ">"),
        ("<", "<"),
        ("=", "="),
    ]
    for name, marker in checks:
        if marker and marker in text:
            hints.append(name)
    return _unique(hints)


def _condition_raw_summary(expression: str, field_refs: list[dict]) -> str:
    fields = [item.get("comment") or item.get("column") for item in field_refs[:8]]
    field_text = "、".join(f for f in fields if f)
    group_type = _condition_group_type(expression)
    if field_text:
        return f"{group_type} 条件，涉及 {field_text}"
    return f"{group_type} 条件"


def _table_label(related_metadata: dict, table: str | None) -> str | None:
    if not table:
        return None
    for section in ("input_tables", "output_tables"):
        table_item = (related_metadata.get(section) or {}).get(table) or {}
        table_metadata = table_item.get("table_metadata") or {}
        for key in ("table_name_cn", "table_desc", "table_label_layer"):
            value = table_metadata.get(key)
            if value:
                if key == "table_label_layer":
                    return f"{table}（{value}）"
                return str(value)
    return None


def _column_metadata(related_metadata: dict, table: str | None, column: str) -> dict:
    if not table:
        return {}
    for section in ("input_tables", "output_tables"):
        table_item = (related_metadata.get(section) or {}).get(table) or {}
        for detail in table_item.get("column_details") or []:
            if detail.get("name") == column:
                return detail
    return {}


def _semantic_hints(profile: dict, business_rule_candidates: list[dict]) -> list[str]:
    hints: list[str] = []
    related_metadata = profile.get("related_metadata") or {}
    target_table = profile.get("target_table")
    if target_table:
        hints.extend(_identifier_hints(target_table))
        label = _table_label(related_metadata, target_table)
        if label:
            hints.append(label)
    for table in profile.get("source_tables") or []:
        hints.extend(_identifier_hints(table))
    for item in profile.get("end_to_end_lineage") or []:
        hints.extend(_identifier_hints(item.get("column") or ""))
    for section in ("input_tables", "output_tables"):
        for table, metadata in (related_metadata.get(section) or {}).items():
            table_metadata = metadata.get("table_metadata") or {}
            for value in (table_metadata.get("table_name_cn"), table_metadata.get("table_desc")):
                if value:
                    hints.append(str(value))
            hints.extend(_identifier_hints(table))
            for detail in metadata.get("column_details") or []:
                if detail.get("comment"):
                    hints.append(str(detail["comment"]))
                hints.extend(_identifier_hints(detail.get("name") or ""))

    for item in business_rule_candidates:
        for detail in item.get("field_details") or []:
            if detail.get("comment"):
                hints.append(str(detail["comment"]))
            hints.extend(_identifier_hints(detail.get("column") or ""))
    return _unique(hints)


def _identifier_hints(identifier: str) -> list[str]:
    text = identifier.lower()
    mapping = [
        (("clct", "collect", "collection"), "催收"),
        (("in_collect", "in_coll"), "入催"),
        (("loan",), "贷款"),
        (("cust", "customer"), "客户"),
        (("acct", "account"), "账户"),
        (("overdue", "past_due"), "逾期"),
        (("repay", "payment"), "还款"),
        (("dpd", "cpd"), "逾期天数/DPD"),
        (("score",), "评分/分数"),
        (("product",), "产品"),
        (("contract", "contr", "contra"), "合同"),
    ]
    hints = []
    for tokens, label in mapping:
        if any(token in text for token in tokens):
            hints.append(label)
    return hints


def _objective_summary(
    target_table: str | None,
    target_label: str | None,
    source_labels: list[str],
    semantic_hints: list[str],
) -> str:
    target_text = target_label or target_table or "目标表"
    source_text = "、".join(source_labels[:3])
    hint_text = "、".join(semantic_hints[:6])
    parts = [f"生成 {target_text}"]
    if source_text:
        parts.append(f"主要读取 {source_text}")
    if hint_text:
        parts.append(f"语义线索包括 {hint_text}")
    return "；".join(parts)


def _processing_steps(operations: list[str]) -> list[str]:
    mapping = {
        "union": "合并多路数据",
        "distinct": "去重形成唯一记录/名单",
        "lateral_view": "展开数组或复杂类型",
        "join": "关联上游或维表",
        "filter": "按条件筛选记录",
        "aggregate": "聚合生成指标",
        "window": "使用窗口函数排序、去重或取值",
        "case_when": "按条件分支派生字段",
        "rename": "字段重命名",
        "expression": "表达式计算字段",
        "pass_through": "传递上游字段",
    }
    return [mapping.get(operation, operation) for operation in operations]


def _column_importance_reasons(column: str, transform: str | None, sources: list[dict]) -> list[str]:
    reasons: list[str] = []
    if transform and transform not in ("DIRECT", "CONSTANT"):
        reasons.append(f"transform:{transform}")
    if _looks_like_key_column(column):
        reasons.append("id_or_key_column")
    if column == "dt" or column.endswith("_dt") or column.endswith("_date"):
        reasons.append("date_or_partition_column")
    if any(token in column.lower() for token in ("status", "state", "type", "flag", "level")):
        reasons.append("business_classification_column")
    if any(token in column.lower() for token in ("amount", "amt", "cnt", "count", "num", "score", "rate")):
        reasons.append("metric_like_column")
    if any(source.get("transform") not in ("DIRECT", "CONSTANT") for source in sources):
        reasons.append("derived_from_physical_sources")
    return _unique(reasons)


def _looks_like_key_column(column: str) -> bool:
    name = column.lower()
    return name == "id" or name.endswith("_id") or name.endswith("id") or name.endswith("_key")


def _expression_summary(transform: str | None, expression: str) -> str:
    if transform == "CONDITIONAL" or "CASE" in expression.upper():
        return "条件分支派生字段"
    if transform == "AGGREGATE":
        return "聚合计算字段"
    if transform == "WINDOW":
        return "窗口函数计算字段"
    return "表达式计算字段"


def _filter_type(expression: str) -> str:
    text = expression.lower()
    if "dt" in text and ("=" in text or "between" in text):
        return "partition_filter"
    if "is_deleted" in text or "deleted" in text:
        return "soft_delete_filter"
    return "business_filter"


def _compact_list_field(profile: dict, key: str, max_items: int) -> None:
    items = profile.get(key)
    if not isinstance(items, list) or len(items) <= max_items:
        return
    profile[key] = items[:max_items]
    profile[f"{key}_count"] = len(items)
    profile[f"{key}_truncated"] = True


def _unique(values: Any) -> list:
    result = []
    seen = set()
    for value in values:
        if value is None or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _metadata_priority_columns(profile: dict) -> dict[str, list[str]]:
    priorities: dict[str, list[str]] = {}

    def add(table: str | None, column: str | None) -> None:
        if not table or not column or ":" in table or table in {"ROOT", "UNKNOWN"}:
            return
        priorities.setdefault(table, [])
        if column not in priorities[table]:
            priorities[table].append(column)

    for item in profile.get("business_rule_candidates") or []:
        for detail in item.get("field_details") or []:
            add(detail.get("table"), detail.get("column"))
    for item in profile.get("end_to_end_lineage") or []:
        for source in item.get("physical_sources") or []:
            add(source.get("table"), source.get("column"))
    target_table = profile.get("target_table")
    for item in profile.get("important_columns") or []:
        add(target_table, item.get("column"))
    return priorities


def _compact_related_metadata(related_metadata: dict, priority_columns: dict[str, list[str]] | None = None) -> None:
    priority_columns = priority_columns or {}
    for section in ("input_tables", "output_tables"):
        tables = related_metadata.get(section) or {}
        for table, metadata in tables.items():
            table_metadata = metadata.get("table_metadata")
            if isinstance(table_metadata, dict):
                metadata["table_metadata"] = _compact_logic_value(table_metadata)
            columns = metadata.get("column_details") or []
            total = len(columns)
            if total > PROFILE_MAX_METADATA_COLUMNS_PER_TABLE:
                metadata["column_details"] = _prioritized_column_details(
                    columns,
                    priority_columns.get(table, []),
                    PROFILE_MAX_METADATA_COLUMNS_PER_TABLE,
                )
                metadata["column_count"] = total
                metadata["shown_column_count"] = len(metadata["column_details"])
                metadata["columns_truncated"] = True


def _prioritized_column_details(columns: list[dict], priority_names: list[str], max_items: int) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    by_name = {item.get("name"): item for item in columns if item.get("name")}
    for name in priority_names:
        if name in by_name and name not in seen:
            selected.append(by_name[name])
            seen.add(name)
        if len(selected) >= max_items:
            return selected
    for item in columns:
        name = item.get("name")
        if name in seen:
            continue
        selected.append(item)
        if name:
            seen.add(name)
        if len(selected) >= max_items:
            break
    return selected


def _compact_end_to_end_lineage(items: list) -> None:
    for item in items:
        expression = item.get("expression")
        if isinstance(expression, str):
            compact, truncated = _truncate_text(expression, PROFILE_MAX_EXPRESSION_CHARS)
            if truncated:
                item["expression"] = compact
                item["expression_length"] = len(expression)
                item["expression_truncated"] = True

        sources = item.get("physical_sources") or []
        total = len(sources)
        if total > PROFILE_MAX_PHYSICAL_SOURCES_PER_COLUMN:
            item["physical_sources"] = sources[:PROFILE_MAX_PHYSICAL_SOURCES_PER_COLUMN]
            item["physical_source_count"] = total
            item["shown_physical_source_count"] = len(item["physical_sources"])
            item["physical_sources_truncated"] = True


def _compact_scope_profile(scope_profile: dict) -> None:
    for step in scope_profile.get("steps", []):
        logic = step.get("logic") or {}
        for key, value in list(logic.items()):
            if isinstance(value, list):
                total = len(value)
                logic[key] = [_compact_logic_value(item) for item in value[:PROFILE_MAX_LOGIC_ITEMS_PER_TYPE]]
                if total > PROFILE_MAX_LOGIC_ITEMS_PER_TYPE:
                    logic[f"{key}_count"] = total
                    logic[f"{key}_truncated"] = True
            else:
                logic[key] = _compact_logic_value(value)


def _compact_business_rule_candidates(candidates: list) -> None:
    for candidate in candidates:
        _compact_rule_fields(candidate)
        expression = candidate.get("expression")
        if isinstance(expression, str):
            candidate["expression_length"] = len(expression)
            candidate["expression_omitted"] = True
            candidate.pop("expression", None)
        raw_summary = candidate.get("raw_summary")
        if isinstance(raw_summary, str):
            candidate["raw_summary"] = _truncate_text(raw_summary, PROFILE_MAX_EXPRESSION_CHARS)[0]


def _compact_business_profile(business_profile: dict) -> None:
    sections = business_profile.get("sections") or []
    for section in sections:
        conditions = section.get("conditions") or []
        for condition in conditions:
            _compact_rule_fields(condition)
            raw_summary = condition.get("raw_summary")
            if isinstance(raw_summary, str):
                condition["raw_summary"] = _truncate_text(raw_summary, PROFILE_MAX_EXPRESSION_CHARS)[0]
        if len(conditions) > PROFILE_MAX_SECTION_CONDITIONS:
            section["conditions"] = conditions[:PROFILE_MAX_SECTION_CONDITIONS]
            section["condition_count"] = len(conditions)
            section["conditions_truncated"] = True
    if len(sections) > PROFILE_MAX_BUSINESS_SECTIONS:
        business_profile["sections"] = sections[:PROFILE_MAX_BUSINESS_SECTIONS]
        business_profile["section_count"] = len(sections)
        business_profile["sections_truncated"] = True


def _compact_semantic_profile(semantic_profile: dict) -> None:
    tables = semantic_profile.get("tables") or {}
    for section in ("inputs", "outputs"):
        for table in tables.get(section) or []:
            columns = table.get("used_columns") or []
            if len(columns) > PROFILE_MAX_SEMANTIC_COLUMNS_PER_TABLE:
                table["used_columns"] = columns[:PROFILE_MAX_SEMANTIC_COLUMNS_PER_TABLE]
                table["used_column_count"] = len(columns)
                table["used_columns_truncated"] = True

    process = semantic_profile.get("process") or {}
    for step in process.get("steps") or []:
        outputs = step.get("outputs") or {}
        key_columns = outputs.get("key_columns") or []
        if len(key_columns) > PROFILE_MAX_LOGIC_ITEMS_PER_TYPE:
            outputs["key_columns"] = key_columns[:PROFILE_MAX_LOGIC_ITEMS_PER_TYPE]
            outputs["key_column_count"] = len(key_columns)
            outputs["key_columns_truncated"] = True
        logic = step.get("logic") or {}
        for key in ("aggregations", "window_functions", "case_when", "lateral_views"):
            value = logic.get(key)
            if isinstance(value, list):
                total = len(value)
                logic[key] = [_compact_logic_value(item) for item in value[:PROFILE_MAX_LOGIC_ITEMS_PER_TYPE]]
                if total > PROFILE_MAX_LOGIC_ITEMS_PER_TYPE:
                    logic[f"{key}_count"] = total
                    logic[f"{key}_truncated"] = True

    rules = semantic_profile.get("rules") or []
    if len(rules) > PROFILE_MAX_BUSINESS_RULE_CANDIDATES:
        semantic_profile["rules"] = rules[:PROFILE_MAX_BUSINESS_RULE_CANDIDATES]
        semantic_profile["rule_count"] = len(rules)
        semantic_profile["rules_truncated"] = True
        rules = semantic_profile["rules"]
    for rule in rules:
        fields = rule.get("key_fields") or []
        if len(fields) > PROFILE_MAX_BUSINESS_RULE_FIELDS:
            rule["key_fields"] = fields[:PROFILE_MAX_BUSINESS_RULE_FIELDS]
            rule["key_field_count"] = len(fields)
            rule["key_fields_truncated"] = True
        groups = rule.get("condition_groups") or []
        if len(groups) > PROFILE_MAX_CONDITION_GROUPS_PER_RULE:
            rule["condition_groups"] = groups[:PROFILE_MAX_CONDITION_GROUPS_PER_RULE]
            rule["condition_group_count"] = len(groups)
            rule["condition_groups_truncated"] = True
        for group in rule.get("condition_groups") or []:
            fields = group.get("fields") or []
            if len(fields) > PROFILE_MAX_SEMANTIC_CONDITION_FIELDS:
                group["fields"] = fields[:PROFILE_MAX_SEMANTIC_CONDITION_FIELDS]
                group["field_count"] = len(fields)
                group["fields_truncated"] = True
            for key in ("expression", "sql_fragment"):
                value = group.get(key)
                if isinstance(value, str):
                    compact, truncated = _truncate_text(value, PROFILE_MAX_EXPRESSION_CHARS)
                    if truncated:
                        group[key] = compact
                        group[f"{key}_length"] = len(value)
                        group[f"{key}_truncated"] = True

    fields = semantic_profile.get("fields") or {}
    output_lineage = fields.get("output_lineage") or []
    if len(output_lineage) > PROFILE_MAX_SEMANTIC_OUTPUT_LINEAGE:
        fields["output_lineage"] = output_lineage[:PROFILE_MAX_SEMANTIC_OUTPUT_LINEAGE]
        fields["output_lineage_count"] = len(output_lineage)
        fields["output_lineage_truncated"] = True
    for item in fields.get("output_lineage") or []:
        expression = item.get("expression")
        if isinstance(expression, str):
            compact, truncated = _truncate_text(expression, PROFILE_MAX_EXPRESSION_CHARS)
            if truncated:
                item["expression"] = compact
                item["expression_length"] = len(expression)
                item["expression_truncated"] = True
        sources = item.get("source_columns") or []
        if len(sources) > PROFILE_MAX_SEMANTIC_SOURCE_COLUMNS:
            item["source_columns"] = sources[:PROFILE_MAX_SEMANTIC_SOURCE_COLUMNS]
            item["source_column_count"] = len(sources)
            item["source_columns_truncated"] = True


def _tighten_semantic_profile(semantic_profile: dict) -> None:
    process = semantic_profile.get("process") or {}
    for step in process.get("steps") or []:
        logic = step.get("logic") or {}
        for key in ("aggregations", "window_functions", "case_when"):
            value = logic.get(key)
            if not isinstance(value, list):
                continue
            if len(value) > 3:
                logic[key] = value[:3]
                logic[f"{key}_count"] = len(value)
                logic[f"{key}_truncated"] = True
            for item in logic.get(key) or []:
                if isinstance(item, dict) and item.get("expression"):
                    item["expression_omitted"] = "semantic_large_profile"
                    item.pop("expression", None)

    rules = semantic_profile.get("rules") or []
    for rule in rules:
        groups = rule.get("condition_groups") or []
        if len(groups) > 12:
            rule["condition_groups"] = groups[:12]
            rule["condition_group_count"] = len(groups)
            rule["condition_groups_truncated"] = True
        for group in rule.get("condition_groups") or []:
            fields = group.get("fields") or []
            if len(fields) > 5:
                group["fields"] = fields[:5]
                group["field_count"] = len(fields)
                group["fields_truncated"] = True

    fields = semantic_profile.get("fields") or {}
    for item in fields.get("output_lineage") or []:
        if item.get("transform") == "DIRECT":
            item.pop("expression", None)
            item["expression_omitted"] = "direct_mapping"
        if item.get("source_columns"):
            item["source_column_count"] = len(item["source_columns"])
            item["source_columns_omitted"] = True
            item.pop("source_columns", None)


def _compact_rule_fields(rule: dict) -> None:
    fields = rule.get("fields")
    if isinstance(fields, list) and len(fields) > PROFILE_MAX_BUSINESS_RULE_FIELDS:
        rule["fields"] = fields[:PROFILE_MAX_BUSINESS_RULE_FIELDS]
        rule["field_count"] = len(fields)
        rule["fields_truncated"] = True

    details = rule.get("field_details")
    if isinstance(details, list) and len(details) > PROFILE_MAX_BUSINESS_RULE_FIELDS:
        rule["field_details"] = details[:PROFILE_MAX_BUSINESS_RULE_FIELDS]
        rule["field_detail_count"] = len(details)
        rule["field_details_truncated"] = True


def _compact_logic_value(value: Any) -> Any:
    if isinstance(value, str):
        compact, truncated = _truncate_text(value, PROFILE_MAX_EXPRESSION_CHARS)
        if truncated:
            return compact
        return value
    if isinstance(value, list):
        return [_compact_logic_value(item) for item in value]
    if isinstance(value, dict):
        compacted = {}
        for key, item in value.items():
            compacted[key] = _compact_logic_value(item)
            if isinstance(item, str):
                compact, truncated = _truncate_text(item, PROFILE_MAX_EXPRESSION_CHARS)
                if truncated:
                    compacted[key] = compact
                    compacted[f"{key}_length"] = len(item)
                    compacted[f"{key}_truncated"] = True
        return compacted
    return value


def _compact_profile_diagnostics(diagnostics: dict) -> dict:
    compacted = {k: v for k, v in diagnostics.items() if k != "warnings"}
    warnings = diagnostics.get("warnings") or []
    if not warnings:
        return compacted

    compacted["warning_count"] = len(warnings)
    warning_types: dict[str, int] = {}
    for warning in warnings:
        warning_type = warning.get("type", "unknown")
        warning_types[warning_type] = warning_types.get(warning_type, 0) + 1
    compacted["warning_types"] = warning_types
    compacted["warnings_sample"] = warnings[:PROFILE_MAX_WARNINGS]
    if len(warnings) > PROFILE_MAX_WARNINGS:
        compacted["warnings_truncated"] = True
        compacted["shown_warning_count"] = PROFILE_MAX_WARNINGS
    return compacted


def _enforce_profile_size_budget(profile: dict) -> None:
    if _profile_size(profile) <= PROFILE_TARGET_MAX_BYTES:
        return

    _tighten_business_layer(profile)
    _tighten_semantic_profile(profile.get("semantic_profile", {}))
    profile["compact_policy"]["large_profile_compaction"] = True
    if _profile_size(profile) <= PROFILE_TARGET_MAX_BYTES:
        return

    _omit_direct_lineage_expressions(profile)
    if _profile_size(profile) <= PROFILE_TARGET_MAX_BYTES:
        return

    _tighten_scope_profile_logic(profile)
    if _profile_size(profile) <= PROFILE_TARGET_MAX_BYTES:
        return

    _compact_list_field(profile, "filters_summary", 5)
    _compact_list_field(profile, "expression_catalog", 5)
    if _profile_size(profile) <= PROFILE_TARGET_MAX_BYTES:
        return

    if profile.get("filters_summary"):
        profile["filters_summary_count"] = len(profile["filters_summary"])
        profile["filters_summary_omitted"] = True
        profile["filters_summary"] = []
    if profile.get("expression_catalog"):
        profile["expression_catalog_count"] = len(profile["expression_catalog"])
        profile["expression_catalog_omitted"] = True
        profile["expression_catalog"] = []


def _profile_size(profile: dict) -> int:
    return len(json.dumps(profile, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _tighten_business_layer(profile: dict) -> None:
    candidates = profile.get("business_rule_candidates") or []
    if len(candidates) > 6:
        profile["business_rule_candidates"] = candidates[:6]
        profile["business_rule_candidates_count"] = len(candidates)
        profile["business_rule_candidates_truncated"] = True
    for candidate in profile.get("business_rule_candidates") or []:
        if "field_details" in candidate:
            candidate["field_details_omitted"] = True
            candidate.pop("field_details", None)

    business_profile = profile.get("business_profile") or {}
    sections = business_profile.get("sections") or []
    if len(sections) > 6:
        business_profile["sections"] = sections[:6]
        business_profile["section_count"] = len(sections)
        business_profile["sections_truncated"] = True
    for section in business_profile.get("sections") or []:
        for condition in section.get("conditions") or []:
            condition.pop("field_details", None)


def _omit_direct_lineage_expressions(profile: dict) -> None:
    omitted = 0
    for item in profile.get("end_to_end_lineage") or []:
        if item.get("transform") != "DIRECT" or "expression" not in item:
            continue
        item.pop("expression", None)
        item["expression_omitted"] = "direct_mapping"
        omitted += 1
    if omitted:
        profile["compact_policy"]["direct_lineage_expressions_omitted"] = omitted


def _tighten_scope_profile_logic(profile: dict) -> None:
    trimmed = 0
    for step in (profile.get("scope_profile") or {}).get("steps", []):
        logic = step.get("logic") or {}
        for key in ("joins", "filters", "aggregations", "window_functions", "case_when", "key_renames"):
            value = logic.get(key)
            if not isinstance(value, list) or len(value) <= 3:
                continue
            logic[key] = value[:3]
            logic[f"{key}_count"] = len(value)
            logic[f"{key}_truncated"] = True
            trimmed += 1
    if trimmed:
        profile["compact_policy"]["scope_profile_logic_tightened"] = True


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    if max_chars <= 3:
        return text[:max_chars], True
    return text[: max_chars - 3] + "...", True


def write_output(result: ScopeLineageResult, output_dir: str | Path) -> Path:
    """Write lineage.json, profile.json, and diagnostics.json to output_dir.

    Returns the output directory path.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        data = validate_lineage_json(result)
    except ImportError:
        data = to_dict(result)  # jsonschema not installed — skip validation

    xref_errors = validate_cross_references(data)
    if xref_errors:
        raise ValueError(
            f"Cross-reference validation failed ({len(xref_errors)} errors):\n"
            + "\n".join(xref_errors[:5])
        )

    # Write full lineage
    lineage_path = output_dir / "lineage.json"
    with open(lineage_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    # Write compact LLM/profile-oriented output without full intermediate scopes
    profile_path = output_dir / "profile.json"
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(to_profile_dict(result), f, ensure_ascii=False, separators=(",", ":"), default=str)

    # Write diagnostics separately
    diag_path = output_dir / "diagnostics.json"
    with open(diag_path, "w", encoding="utf-8") as f:
        json.dump(data.get("diagnostics", {}), f, ensure_ascii=False, indent=2, default=str)

    return output_dir


# -- internal converters ---------------------------------------------------


def _result_to_dict(r: ScopeLineageResult) -> dict:
    d = {
        "task_id": r.task_id,
        "target_table": r.target_table,
        "stmt_kind": r.stmt_kind,
        "source_tables": r.source_tables,
        "related_metadata": r.related_metadata,
        "scope_graph": to_dict(r.scope_graph),
        "scopes": {k: to_dict(v) for k, v in r.scopes.items()},
        "scope_profile": build_scope_profile(r),
        "end_to_end_lineage": build_end_to_end_lineage(r),
        "diagnostics": to_dict(r.diagnostics),
    }
    return d


def _scope_data_to_dict(sd: ScopeData) -> dict:
    d: dict[str, Any] = {"kind": sd.kind}
    if sd.role is not None:
        d["role"] = sd.role
    if sd.distinct:
        d["distinct"] = sd.distinct
    d["depends_on"] = sd.depends_on if sd.depends_on else []
    if sd.writes_to is not None:
        d["writes_to"] = sd.writes_to
    if sd.alias_in_parent is not None:
        d["alias_in_parent"] = sd.alias_in_parent
    d["columns"] = [to_dict(c) for c in sd.columns] if sd.columns else []
    if sd.joins:
        d["joins"] = [to_dict(j) for j in sd.joins]
    if sd.filters:
        d["filters"] = [to_dict(f) for f in sd.filters]
    if sd.group_by:
        d["group_by"] = [to_dict(g) for g in sd.group_by]
    if sd.having:
        d["having"] = [to_dict(h) for h in sd.having]
    if sd.order_by:
        d["order_by"] = sd.order_by
    if sd.lateral_views:
        d["lateral_views"] = to_dict(sd.lateral_views)
    if sd.set_op is not None:
        d["set_op"] = sd.set_op
    if sd.branches is not None:
        d["branches"] = sd.branches
    if sd.branch_index is not None:
        d["branch_index"] = sd.branch_index
    return d


def _scope_column_to_dict(c: ScopeColumn) -> dict:
    d: dict[str, Any] = {"name": c.name, "transform": c.transform}
    if c.transform_subkind is not None:
        d["transform_subkind"] = c.transform_subkind
    if c.expression is not None:
        d["expression"] = c.expression
    d["sources"] = [to_dict(s) for s in c.sources] if c.sources else []
    if c.case_branches is not None:
        d["case_branches"] = to_dict(c.case_branches)
    if c.window is not None:
        d["window"] = to_dict(c.window)
    if c.agg_function is not None:
        d["agg_function"] = c.agg_function
    if c.branches is not None:
        d["branches"] = to_dict(c.branches)
    if c.merge_branch is not None:
        d["merge_branch"] = c.merge_branch
    return d


def _scope_graph_to_dict(g: ScopeGraph) -> dict:
    return {
        "nodes": g.nodes,
        "edges": [e.to_dict() for e in g.edges] if g.edges else [],
    }


def _scope_filter_to_dict(f: ScopeFilter) -> dict:
    d: dict[str, Any] = {"expression": f.expression}
    if f.columns:
        d["columns"] = [to_dict(c) for c in f.columns]
    return d


def _diagnostics_to_dict(d: Diagnostics) -> dict:
    result: dict[str, Any] = {}
    if d.fallback_used:
        result["fallback_used"] = d.fallback_used
    if d.warnings:
        result["warnings"] = [to_dict(w) for w in d.warnings]
    if d.stats:
        result["stats"] = d.stats
    return result


def _scope_join_to_dict(j: ScopeJoin) -> dict:
    d: dict[str, Any] = {
        "join_type": j.join_type,
        "left_scope": j.left_scope,
        "right_scope": j.right_scope,
    }
    if j.alias_in_parent is not None:
        d["alias_in_parent"] = j.alias_in_parent
    if j.condition_expression is not None:
        d["condition_expression"] = j.condition_expression
    if j.condition_columns:
        d["condition_columns"] = [to_dict(c) for c in j.condition_columns]
    return d
