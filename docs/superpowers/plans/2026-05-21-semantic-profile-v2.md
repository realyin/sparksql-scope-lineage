# Semantic Profile V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a backward-compatible `semantic_profile` section to `profile.json` so LLMs can recover SQL business semantics from task, table, scope, rule, field, and quality structures.

**Architecture:** Keep existing `profile.json` fields unchanged and add `semantic_profile` as a derived view in `lineage_parser/scope_serializer.py`. Reuse existing `scope_profile`, `business_rule_candidates`, `related_metadata`, and `end_to_end_lineage`, then enrich them with structured condition groups, field usage roles, task/process summaries, and quality boundaries. Add a dedicated validator and fixture tests before changing consumers.

**Tech Stack:** Python 3.9+, existing dataclass-to-dict serializer, `pytest`, local corpus runner `tools/run_scope_corpus.py`.

---

## File Map

- Modify `lineage_parser/scope_serializer.py`
  - Build `semantic_profile`.
  - Add rule condition groups.
  - Add field usage classification.
  - Add profile quality summary.
  - Keep existing profile fields compatible.
- Create `tools/validate_profile_semantics.py`
  - Validate `semantic_profile` structure and semantics-readiness.
  - Catch historical failure modes: direct/indirect source confusion, empty condition groups, weak rules, missing evidence/confidence.
- Create `tests/test_semantic_profile.py`
  - Unit tests for task/table/process/rule/field/quality output.
  - Use small SQL fixtures plus one complex WHERE fixture.
- Create `tests/test_validate_profile_semantics.py`
  - Tests for validator success and failure modes.
- Modify `docs/llm-profile-guide.zh-CN.md`
  - Add read order for `semantic_profile`.
- Modify `docs/llm-profile-prompt.zh-CN.md`
  - Instruct LLM to prefer `semantic_profile` when present.
- Optional modify `README.zh-CN.md`
  - Mention `semantic_profile` as the new semantic layer after implementation.

---

## Task 1: Add Semantic Profile Skeleton

**Files:**
- Modify: `lineage_parser/scope_serializer.py`
- Test: `tests/test_semantic_profile.py`

- [ ] **Step 1: Write failing skeleton test**

Add this test file:

```python
from lineage_parser import parse_scope_lineage, to_profile_dict


def test_semantic_profile_contains_task_tables_process_fields_and_quality():
    sql = """
    INSERT OVERWRITE TABLE mart.customer_touch
    WITH base AS (
      SELECT customer_id, app_code, event_time
      FROM ods.touch_events
      WHERE dt = '20260515' AND status = 'connected'
    )
    SELECT customer_id, app_code, event_time AS first_touch_time
    FROM base
    """
    schema = {
        "ods.touch_events": [
            {"name": "customer_id", "type": "string", "comment": "客户ID"},
            {"name": "app_code", "type": "string", "comment": "申请主体"},
            {"name": "event_time", "type": "string", "comment": "触达时间"},
            {"name": "dt", "type": "string", "comment": "日期分区"},
            {"name": "status", "type": "string", "comment": "触达状态"},
        ],
        "mart.customer_touch": [
            {"name": "customer_id", "type": "string", "comment": "客户ID"},
            {"name": "app_code", "type": "string", "comment": "申请主体"},
            {"name": "first_touch_time", "type": "string", "comment": "首次触达时间"},
        ],
    }

    result = parse_scope_lineage(sql, "customer_touch_task", schema=schema)
    profile = to_profile_dict(result)
    semantic = profile["semantic_profile"]

    assert semantic["version"] == "2.0"
    assert semantic["task"]["task_name"] == "customer_touch_task"
    assert semantic["task"]["target_tables"][0]["table"] == "mart.customer_touch"
    assert semantic["tables"]["inputs"][0]["table"] == "ods.touch_events"
    assert semantic["tables"]["outputs"][0]["table"] == "mart.customer_touch"
    assert semantic["process"]["steps"]
    assert semantic["fields"]["output_lineage"]
    assert semantic["quality"]["trace_complete"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_profile_contains_task_tables_process_fields_and_quality -q
```

Expected: `KeyError: 'semantic_profile'`.

- [ ] **Step 3: Add builder entrypoint**

In `lineage_parser/scope_serializer.py`, update `_build_llm_profile_indexes`:

```python
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
```

Add these helper functions below `_build_business_profile`:

```python
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
```

- [ ] **Step 4: Implement minimal helper functions**

Add:

```python
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
```

- [ ] **Step 5: Implement process, rules, fields, quality skeleton**

Add:

```python
def _semantic_process(profile: dict) -> dict:
    steps = [
        _semantic_process_step(step, index)
        for index, step in enumerate((profile.get("scope_profile") or {}).get("steps") or [])
    ]
    return {"step_count": len(steps), "steps": steps}


def _semantic_process_step(step: dict, index: int) -> dict:
    return {
        "step_no": index + 1,
        "statement_id": None,
        "scope_id": step.get("scope_id"),
        "scope_name": step.get("name"),
        "kind": step.get("kind"),
        "role": step.get("role"),
        "semantic_role": _semantic_role_for_step(step),
        "business_object": step.get("business_summary"),
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
            "aggregations": step.get("logic", {}).get("aggregations") or [],
            "window_functions": step.get("logic", {}).get("window_functions") or [],
            "case_when": step.get("logic", {}).get("case_when") or [],
            "distinct": step.get("logic", {}).get("distinct", False),
            "union": {"branch_count": step.get("logic", {}).get("union_branches")} if step.get("logic", {}).get("union_branches") else None,
            "lateral_views": step.get("logic", {}).get("lateral_views") or [],
        },
        "key_fields": [],
        "sql_evidence": [],
    }


def _semantic_rules(profile: dict) -> list[dict]:
    return [
        _semantic_rule(candidate, index)
        for index, candidate in enumerate(profile.get("business_rule_candidates") or [])
    ]


def _semantic_rule(candidate: dict, index: int) -> dict:
    rule_id = f"rule:{_safe_rule_part(candidate.get('scope_id'))}:{candidate.get('source') or 'RULE'}:{index}"
    expression = candidate.get("expression")
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


def _semantic_fields(profile: dict) -> dict:
    return {
        "output_lineage": [_semantic_output_field(item, profile) for item in profile.get("end_to_end_lineage") or []],
        "important_fields": _semantic_important_fields(profile),
    }


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
```

- [ ] **Step 6: Run skeleton test**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_profile_contains_task_tables_process_fields_and_quality -q
```

Expected: PASS.

- [ ] **Step 7: Run existing profile tests**

Run:

```bash
python3 -m pytest tests/test_scope_profile.py tests/test_task_insight.py -q
```

Expected: PASS.

---

## Task 2: Add Structured Rule Condition Groups

**Files:**
- Modify: `lineage_parser/scope_serializer.py`
- Test: `tests/test_semantic_profile.py`

- [ ] **Step 1: Write complex WHERE test**

Append:

```python
def test_semantic_rules_preserve_complex_where_condition_groups():
    sql = """
    INSERT OVERWRITE TABLE collection_files.clct_cf_pre_in_coll_cust
    SELECT a.internal_customer_id, a.acct_nbr
    FROM collection_files.clct_cf_loan_all a
    LEFT JOIN (
      SELECT DISTINCT contra_no FROM collection_files.dim_excess_contra
    ) b ON a.contr_nbr = b.contra_no
    WHERE dt = '20260426'
      AND ((substr(product_cd, 3, 1) != '2' AND product_cd NOT IN ('005800','005605','005502'))
           OR product_cd IN ('002301','002316'))
      AND (
        (overdue_date IS NULL AND paid_out_date IS NULL AND in_clct_dpd >= -7 AND in_clct_dpd <= 0)
        OR (forced_pay_off = 'Y')
        OR stmt_delay_ind = 'Y'
        OR (grace_date >= '2026-04-27' AND tot_due_amt > 0)
        OR nvl(repay_amt, 0) > 0
        OR b.contra_no IS NOT NULL
      )
    """
    schema = {
        "collection_files.clct_cf_loan_all": [
            {"name": "internal_customer_id", "comment": "客户ID"},
            {"name": "acct_nbr", "comment": "账户号"},
            {"name": "contr_nbr", "comment": "合同号"},
            {"name": "dt", "comment": "日期分区"},
            {"name": "product_cd", "comment": "产品码"},
            {"name": "overdue_date", "comment": "逾期日期"},
            {"name": "paid_out_date", "comment": "结清日期"},
            {"name": "in_clct_dpd", "comment": "入催DPD"},
            {"name": "forced_pay_off", "comment": "强制还款标记"},
            {"name": "stmt_delay_ind", "comment": "账单延期标记"},
            {"name": "grace_date", "comment": "宽限期日期"},
            {"name": "tot_due_amt", "comment": "总应还金额"},
            {"name": "repay_amt", "comment": "还款金额"},
        ],
        "collection_files.dim_excess_contra": [{"name": "contra_no", "comment": "超额合同号"}],
        "collection_files.clct_cf_pre_in_coll_cust": [
            {"name": "internal_customer_id", "comment": "客户ID"},
            {"name": "acct_nbr", "comment": "账户号"},
        ],
    }

    result = parse_scope_lineage(sql, "clct_file_in_collect_loan", schema=schema)
    profile = to_profile_dict(result)
    rules = profile["semantic_profile"]["rules"]
    where_rule = next(rule for rule in rules if rule["source"] == "WHERE")
    expressions = [group["expression"] for group in where_rule["condition_groups"]]

    assert any("IN_CLCT_DPD" in expression.upper() and ">=" in expression for expression in expressions)
    assert any("FORCED_PAY_OFF" in expression.upper() for expression in expressions)
    assert any("STMT_DELAY_IND" in expression.upper() for expression in expressions)
    assert any("REPAY_AMT" in expression.upper() for expression in expressions)
    assert where_rule["key_fields"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_rules_preserve_complex_where_condition_groups -q
```

Expected: FAIL because condition grouping is still too coarse.

- [ ] **Step 3: Implement condition group extraction**

In `scope_serializer.py`, add:

```python
def _semantic_condition_groups(candidate: dict, rule_id: str) -> list[dict]:
    expression = candidate.get("expression") or ""
    if not expression:
        return []
    fragments = _split_condition_fragments(expression)
    field_details = candidate.get("field_details") or []
    groups = []
    for index, fragment in enumerate(fragments[:12]):
        fields = _fields_in_expression(fragment, field_details)
        groups.append({
            "group_id": f"{rule_id}:g{index + 1:02d}",
            "name": _condition_group_name(fragment, fields),
            "expression": _truncate_text(fragment, PROFILE_MAX_EXPRESSION_CHARS),
            "fields": [field.get("column") for field in fields if field.get("column")],
            "operators": _operator_hints(fragment),
            "meaning_hint": _meaning_hint_for_condition(fragment, fields),
            "evidence_type": "parsed_sql",
            "sql_fragment": _truncate_text(fragment, PROFILE_MAX_EXPRESSION_CHARS),
            "truncated": len(fragment) > PROFILE_MAX_EXPRESSION_CHARS,
            "original_length": len(fragment) if len(fragment) > PROFILE_MAX_EXPRESSION_CHARS else None,
        })
    return [_drop_none_values(group) for group in groups]
```

Add a pragmatic splitter:

```python
def _split_condition_fragments(expression: str) -> list[str]:
    text = expression.strip()
    if not text:
        return []
    fragments = _split_top_level_or(text)
    if len(fragments) == 1:
        fragments = _split_top_level_and(text)
    return [fragment.strip(" ()") for fragment in fragments if fragment.strip(" ()")]


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
        marker = f" {keyword} "
        if depth == 0 and upper.startswith(marker, i):
            parts.append(expression[start:i])
            i += len(marker)
            start = i
            continue
        i += 1
    parts.append(expression[start:])
    return parts
```

- [ ] **Step 4: Add field/meaning helpers**

Add:

```python
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
    if "product" in text or "product_cd" in field_names:
        return "产品纳入/排除"
    if "dpd" in text or "in_clct_dpd" in field_names:
        return "DPD/预逾期窗口"
    if "overdue" in text:
        return "逾期状态判断"
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
```

- [ ] **Step 5: Run complex WHERE test**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_rules_preserve_complex_where_condition_groups -q
```

Expected: PASS.

---

## Task 3: Add Field Usage Roles and Important Fields

**Files:**
- Modify: `lineage_parser/scope_serializer.py`
- Test: `tests/test_semantic_profile.py`

- [ ] **Step 1: Write field usage test**

Append:

```python
def test_semantic_important_fields_include_filter_join_and_window_usage():
    sql = """
    INSERT OVERWRITE TABLE mart.first_touch
    WITH ranked AS (
      SELECT
        customer_id,
        app_code,
        event_time,
        first_value(event_time) OVER (
          PARTITION BY customer_id, app_code
          ORDER BY event_time
        ) AS first_touch_time
      FROM ods.touch_events
      WHERE status = 'connected'
    )
    SELECT customer_id, app_code, first_touch_time
    FROM ranked
    """
    schema = {
        "ods.touch_events": [
            {"name": "customer_id", "comment": "客户ID"},
            {"name": "app_code", "comment": "申请主体"},
            {"name": "event_time", "comment": "触达时间"},
            {"name": "status", "comment": "触达状态"},
        ],
        "mart.first_touch": [
            {"name": "customer_id", "comment": "客户ID"},
            {"name": "app_code", "comment": "申请主体"},
            {"name": "first_touch_time", "comment": "首次触达时间"},
        ],
    }

    profile = to_profile_dict(parse_scope_lineage(sql, "first_touch_task", schema=schema))
    important = profile["semantic_profile"]["fields"]["important_fields"]
    by_field = {item["field"]: item for item in important}

    assert "filter" in by_field["status"]["used_in"]
    assert "window_partition" in by_field["customer_id"]["used_in"]
    assert "window_partition" in by_field["app_code"]["used_in"]
    assert "window_order" in by_field["event_time"]["used_in"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_important_fields_include_filter_join_and_window_usage -q
```

Expected: FAIL until field usage extraction is implemented.

- [ ] **Step 3: Implement usage collection**

Add:

```python
def _semantic_important_fields(profile: dict) -> list[dict]:
    usage: dict[tuple[str | None, str], dict] = {}
    for candidate in profile.get("business_rule_candidates") or []:
        used_in = "join" if candidate.get("source") == "JOIN_ON" else "filter"
        for detail in candidate.get("field_details") or []:
            _record_field_usage(usage, detail, used_in)

    for step in (profile.get("scope_profile") or {}).get("steps") or []:
        logic = step.get("logic") or {}
        for window in logic.get("window_functions") or []:
            for ref in ((window.get("window") or {}).get("partition_by") or []):
                _record_field_usage(usage, {"table": ref.get("scope"), "column": ref.get("column")}, "window_partition")
            for ref in ((window.get("window") or {}).get("order_by") or []):
                _record_field_usage(usage, {"table": ref.get("scope"), "column": ref.get("column")}, "window_order")
        for case_item in logic.get("case_when") or []:
            if case_item.get("column"):
                _record_field_usage(usage, {"column": case_item.get("column")}, "case_condition")

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
```

- [ ] **Step 4: Add role text helper**

Add:

```python
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
```

- [ ] **Step 5: Run field usage test**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_important_fields_include_filter_join_and_window_usage -q
```

Expected: PASS.

---

## Task 4: Add Profile Semantic Validator

**Files:**
- Create: `tools/validate_profile_semantics.py`
- Test: `tests/test_validate_profile_semantics.py`

- [ ] **Step 1: Write validator tests**

Create `tests/test_validate_profile_semantics.py`:

```python
import importlib.util
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("validate_profile_semantics", TOOLS_DIR / "validate_profile_semantics.py")
validate_profile_semantics = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validate_profile_semantics
SPEC.loader.exec_module(validate_profile_semantics)


def _valid_profile():
    return {
        "semantic_profile": {
            "version": "2.0",
            "task": {"task_name": "t", "target_tables": [{"table": "mart.out"}]},
            "business_summary": {"objective": "生成输出", "evidence": [{"type": "parsed_sql"}]},
            "tables": {"inputs": [{"table": "ods.in"}], "outputs": [{"table": "mart.out"}]},
            "process": {
                "steps": [
                    {
                        "scope_id": "ROOT",
                        "semantic_role": "筛选输出",
                        "direct_source_tables": ["ods.in"],
                        "upstream_physical_tables": ["ods.in"],
                        "logic": {"filters": ["rule:ROOT:WHERE:0"]},
                    }
                ]
            },
            "rules": [
                {
                    "rule_id": "rule:ROOT:WHERE:0",
                    "scope_id": "ROOT",
                    "source": "WHERE",
                    "rule_type": "business_filter",
                    "condition_groups": [
                        {
                            "expression": "status = 'Y'",
                            "fields": ["status"],
                            "operators": ["="],
                            "sql_fragment": "status = 'Y'",
                            "evidence_type": "parsed_sql",
                        }
                    ],
                    "key_fields": [{"field": "status"}],
                }
            ],
            "fields": {"output_lineage": [{"column": "id", "trace_complete": True}], "important_fields": [{"field": "status", "used_in": ["filter"]}]},
            "quality": {"trace_complete": True, "trace_incomplete_columns": []},
        }
    }


def test_validate_profile_semantics_accepts_valid_profile():
    report = validate_profile_semantics.validate_profile(_valid_profile())
    assert report["ok"] is True
    assert report["error_count"] == 0


def test_validate_profile_semantics_rejects_weak_business_filter():
    profile = _valid_profile()
    profile["semantic_profile"]["rules"][0]["condition_groups"] = []
    report = validate_profile_semantics.validate_profile(profile)
    assert report["ok"] is False
    assert any(item["code"] == "business_filter_missing_condition_groups" for item in report["findings"])


def test_validate_profile_semantics_rejects_read_claim_without_direct_source():
    profile = _valid_profile()
    step = profile["semantic_profile"]["process"]["steps"][0]
    step["direct_source_tables"] = []
    step["business_summary"] = "读取 ods.in"
    report = validate_profile_semantics.validate_profile(profile)
    assert report["ok"] is False
    assert any(item["code"] == "read_claim_without_direct_source" for item in report["findings"])
```

- [ ] **Step 2: Run tests to verify import fails**

Run:

```bash
python3 -m pytest tests/test_validate_profile_semantics.py -q
```

Expected: FAIL because `tools/validate_profile_semantics.py` does not exist.

- [ ] **Step 3: Implement validator**

Create `tools/validate_profile_semantics.py`:

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    semantic = profile.get("semantic_profile") or {}
    if not semantic:
        _add(findings, "error", "semantic_profile_missing", "profile.semantic_profile is required")
        return _result(findings)
    _check_required_sections(semantic, findings)
    _check_process(semantic, findings)
    _check_rules(semantic, findings)
    _check_fields(semantic, findings)
    _check_quality(semantic, findings)
    return _result(findings)


def _check_required_sections(semantic: dict, findings: list[dict[str, Any]]) -> None:
    for key in ("task", "business_summary", "tables", "process", "rules", "fields", "quality"):
        if key not in semantic:
            _add(findings, "error", "semantic_section_missing", f"semantic_profile.{key} is required")


def _check_process(semantic: dict, findings: list[dict[str, Any]]) -> None:
    for index, step in enumerate(((semantic.get("process") or {}).get("steps") or [])):
        if not step.get("scope_id"):
            _add(findings, "error", "step_scope_missing", f"process.steps[{index}] missing scope_id")
        if not step.get("semantic_role"):
            _add(findings, "warning", "step_semantic_role_missing", f"{step.get('scope_id')} missing semantic_role")
        summary = str(step.get("business_summary") or step.get("business_object") or "")
        if "读取" in summary and not step.get("direct_source_tables"):
            _add(findings, "error", "read_claim_without_direct_source", f"{step.get('scope_id')} says read but has no direct_source_tables")


def _check_rules(semantic: dict, findings: list[dict[str, Any]]) -> None:
    for index, rule in enumerate(semantic.get("rules") or []):
        rule_type = rule.get("rule_type")
        groups = rule.get("condition_groups") or []
        if rule_type in {"business_filter", "partition_filter", "join_condition"} and not groups:
            _add(findings, "error", f"{rule_type}_missing_condition_groups", f"rules[{index}] has no condition_groups")
        for group_index, group in enumerate(groups):
            if not (group.get("expression") or group.get("sql_fragment")):
                _add(findings, "error", "condition_group_expression_missing", f"rules[{index}].condition_groups[{group_index}] missing expression/sql_fragment")
            if not group.get("fields"):
                _add(findings, "warning", "condition_group_fields_missing", f"rules[{index}].condition_groups[{group_index}] has no fields")
            if not group.get("evidence_type"):
                _add(findings, "warning", "condition_group_evidence_missing", f"rules[{index}].condition_groups[{group_index}] has no evidence_type")


def _check_fields(semantic: dict, findings: list[dict[str, Any]]) -> None:
    fields = semantic.get("fields") or {}
    if not fields.get("output_lineage"):
        _add(findings, "warning", "output_lineage_missing", "semantic_profile.fields.output_lineage is empty")
    for item in fields.get("important_fields") or []:
        if not item.get("used_in"):
            _add(findings, "warning", "important_field_usage_missing", f"{item.get('field')} missing used_in")


def _check_quality(semantic: dict, findings: list[dict[str, Any]]) -> None:
    quality = semantic.get("quality") or {}
    if quality.get("trace_complete") is False and not quality.get("trace_incomplete_columns"):
        _add(findings, "error", "trace_incomplete_columns_missing", "trace_complete=false requires trace_incomplete_columns")


def _result(findings: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [item for item in findings if item["severity"] == "error"]
    warnings = [item for item in findings if item["severity"] == "warning"]
    return {
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "findings": findings,
    }


def _add(findings: list[dict[str, Any]], severity: str, code: str, message: str) -> None:
    findings.append({"severity": severity, "code": code, "message": message})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate profile.json semantic_profile")
    parser.add_argument("--profile", required=True, help="Path to profile.json")
    parser.add_argument("--json-out", help="Write full report as JSON")
    parser.add_argument("--fail-on-warning", action="store_true")
    args = parser.parse_args(argv)
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    report = validate_profile(profile)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("ok", "error_count", "warning_count")}, ensure_ascii=False, indent=2))
    if report["error_count"]:
        return 1
    if args.fail_on_warning and report["warning_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run validator tests**

Run:

```bash
python3 -m pytest tests/test_validate_profile_semantics.py -q
```

Expected: PASS.

---

## Task 5: Connect Semantic Rules to Process Steps

**Files:**
- Modify: `lineage_parser/scope_serializer.py`
- Test: `tests/test_semantic_profile.py`

- [ ] **Step 1: Write process/rule reference test**

Append:

```python
def test_semantic_process_steps_reference_rule_ids():
    sql = """
    INSERT OVERWRITE TABLE mart.out
    SELECT id FROM ods.src WHERE status = 'Y'
    """
    schema = {
        "ods.src": [{"name": "id"}, {"name": "status"}],
        "mart.out": [{"name": "id"}],
    }

    profile = to_profile_dict(parse_scope_lineage(sql, "rule_ref_task", schema=schema))
    semantic = profile["semantic_profile"]
    rule_ids = {rule["rule_id"] for rule in semantic["rules"]}
    root = next(step for step in semantic["process"]["steps"] if step["scope_id"] == "ROOT")

    assert root["logic"]["filters"]
    assert set(root["logic"]["filters"]).issubset(rule_ids)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_process_steps_reference_rule_ids -q
```

Expected: FAIL if filters are not populated with rule ids.

- [ ] **Step 3: Pass rule index into semantic process**

Change:

```python
def _build_semantic_profile(profile: dict, full: dict) -> dict:
    semantic_rules = _semantic_rules(profile)
    rules_by_scope_source = _rules_by_scope_source(semantic_rules)
    return {
        "version": "2.0",
        "task": _semantic_task(profile, full),
        "business_summary": _semantic_business_summary(profile),
        "tables": _semantic_tables(profile),
        "process": _semantic_process(profile, rules_by_scope_source),
        "rules": semantic_rules,
        "fields": _semantic_fields(profile),
        "quality": _semantic_quality(profile, full),
    }
```

Update function signature:

```python
def _semantic_process(profile: dict, rules_by_scope_source: dict[tuple[str | None, str], list[str]]) -> dict:
    steps = [
        _semantic_process_step(step, index, rules_by_scope_source)
        for index, step in enumerate((profile.get("scope_profile") or {}).get("steps") or [])
    ]
    return {"step_count": len(steps), "steps": steps}
```

Add:

```python
def _rules_by_scope_source(rules: list[dict]) -> dict[tuple[str | None, str], list[str]]:
    result: dict[tuple[str | None, str], list[str]] = {}
    for rule in rules:
        result.setdefault((rule.get("scope_id"), rule.get("source")), []).append(rule["rule_id"])
    return result
```

Set process logic:

```python
"logic": {
    "filters": rules_by_scope_source.get((step.get("scope_id"), "WHERE"), []) + rules_by_scope_source.get((step.get("scope_id"), "HAVING"), []),
    "joins": rules_by_scope_source.get((step.get("scope_id"), "JOIN_ON"), []),
    ...
}
```

- [ ] **Step 4: Run process/rule reference test**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py::test_semantic_process_steps_reference_rule_ids -q
```

Expected: PASS.

---

## Task 6: Corpus Validation on Two Benchmark Tasks

**Files:**
- Modify only if validation exposes defects.

- [ ] **Step 1: Rebuild benchmark output**

Run:

```bash
rm -rf /tmp/semantic_profile_benchmark
mkdir -p /tmp/semantic_profile_benchmark/inbound /tmp/semantic_profile_benchmark/collect
cp /Users/yinguoliang/dev/sqllineageparse/task_info/客服任务列表/bj_bas_mxg_ca_customer_inbound_log_d_df.json /tmp/semantic_profile_benchmark/inbound/
cp /Users/yinguoliang/dev/sqllineageparse/task_info/催收任务列表_20260423/clct_file_in_collect_loan.json /tmp/semantic_profile_benchmark/collect/
python3 tools/run_scope_corpus.py --input-dir /tmp/semantic_profile_benchmark/inbound --out /tmp/semantic_profile_benchmark/out --insight --schema /Users/yinguoliang/dev/sqllineageparse/task_info/test_files/schema_info.csv
python3 tools/run_scope_corpus.py --input-dir /tmp/semantic_profile_benchmark/collect --out /tmp/semantic_profile_benchmark/out --insight --schema /Users/yinguoliang/dev/sqllineageparse/task_info/test_files/schema_info.csv
```

Expected: both runs print `error=0`.

- [ ] **Step 2: Validate generated profile semantics**

Run:

```bash
find /tmp/semantic_profile_benchmark/out -name profile.json -print0 | while IFS= read -r -d '' f; do
  python3 tools/validate_profile_semantics.py --profile "$f" || exit 1
done
```

Expected: all profile semantic validations return `error_count=0`.

- [ ] **Step 3: Inspect clct_file_in_collect_loan condition groups**

Run:

```bash
python3 - <<'PY'
import json
from pathlib import Path
for profile_path in sorted(Path('/tmp/semantic_profile_benchmark/out').rglob('profile.json')):
    if 'clct_file_in_collect_loan' not in str(profile_path):
        continue
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    print('\\n###', profile_path)
    for rule in profile['semantic_profile']['rules']:
        if rule.get('source') == 'WHERE':
            print(rule.get('business_name'), rule.get('summary'))
            for group in rule.get('condition_groups', [])[:12]:
                print('-', group.get('name'), '|', group.get('expression'))
PY
```

Expected output includes condition group names/expressions related to DPD, forced pay off, statement delay, grace date, repay amount, product code, or excess contract.

- [ ] **Step 4: Inspect inbound task first-touch semantics**

Run:

```bash
python3 - <<'PY'
import json
from pathlib import Path
profile_path = Path('/tmp/semantic_profile_benchmark/out/inbound/bj_bas_mxg_ca_customer_inbound_log_d_df/profile.json')
profile = json.loads(profile_path.read_text(encoding='utf-8'))
for step in profile['semantic_profile']['process']['steps']:
    if step['scope_id'] in {'cte:manual_first_detail_online', 'cte:robot_first_detail'}:
        print(step['scope_id'], step['semantic_role'])
        print('direct_source_tables=', step['direct_source_tables'])
        print('upstream_physical_tables=', step['upstream_physical_tables'][:4])
        print('windows=', len(step['logic']['window_functions']))
PY
```

Expected:

- `direct_source_tables=[]` for CTEs that only read upstream CTEs.
- `upstream_physical_tables` contains physical tables.
- window count is non-zero for first-touch steps.

---

## Task 7: Update LLM Guide and Prompt

**Files:**
- Modify: `docs/llm-profile-guide.zh-CN.md`
- Modify: `docs/llm-profile-prompt.zh-CN.md`
- Optional Modify: `README.zh-CN.md`

- [ ] **Step 1: Update guide read order**

In `docs/llm-profile-guide.zh-CN.md`, add a section near the read-order section:

```markdown
当 `semantic_profile` 存在时，LLM 应优先读取：

1. `semantic_profile.business_summary`
2. `semantic_profile.tables`
3. `semantic_profile.process.steps`
4. `semantic_profile.rules`
5. `semantic_profile.fields.important_fields`
6. `semantic_profile.fields.output_lineage`
7. `semantic_profile.quality`

旧字段仍可作为兼容补充：`business_profile`、`scope_profile.steps`、
`business_rule_candidates`、`important_columns`、`end_to_end_lineage`。
```

- [ ] **Step 2: Update prompt**

In `docs/llm-profile-prompt.zh-CN.md`, add:

```markdown
如果 profile.json 包含 `semantic_profile`，必须优先使用它生成业务语义。
`semantic_profile.rules[].condition_groups` 是复杂 WHERE/JOIN/CASE 等规则的主要证据，
不能只复述“涉及哪些字段”。输出业务规则时应同时写出：

- scope_id
- 条件组名称
- 关键表达式
- 关键字段及字段含义
- 这是 SQL 明确事实还是基于字段/注释的推断
```

- [ ] **Step 3: Run doc grep sanity check**

Run:

```bash
rg -n "semantic_profile|condition_groups" docs/llm-profile-guide.zh-CN.md docs/llm-profile-prompt.zh-CN.md README.zh-CN.md
```

Expected: all modified docs mention `semantic_profile`; prompt mentions `condition_groups`.

---

## Task 8: Full Verification and Commit

**Files:**
- No source change unless verification exposes issues.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
python3 -m pytest tests/test_semantic_profile.py tests/test_validate_profile_semantics.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run current regression tests**

Run:

```bash
python3 -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Run task insight validator on benchmark output**

Run:

```bash
python3 tools/validate_task_insight.py --root /tmp/semantic_profile_benchmark/out --json-out /tmp/semantic_profile_benchmark/task_insight_validation.json
```

Expected: `error_count=0`.

- [ ] **Step 4: Check profile sizes**

Run:

```bash
find /tmp/semantic_profile_benchmark/out -name profile.json -print0 | xargs -0 ls -lh
```

Expected: profile files remain in a reasonable range for the benchmark. If a file exceeds 150KB, inspect whether repeated SQL fragments or repeated output physical sources caused it.

- [ ] **Step 5: Commit**

Run:

```bash
git status --short
git add lineage_parser/scope_serializer.py tools/validate_profile_semantics.py tests/test_semantic_profile.py tests/test_validate_profile_semantics.py docs/llm-profile-guide.zh-CN.md docs/llm-profile-prompt.zh-CN.md README.zh-CN.md
git commit -m "feat: add semantic profile layer"
```

Expected: commit succeeds.

---

## Self-Review Checklist

- Spec coverage:
  - `semantic_profile.task`: Task 1.
  - `semantic_profile.business_summary`: Task 1.
  - `semantic_profile.tables`: Task 1.
  - `semantic_profile.process`: Tasks 1 and 5.
  - `semantic_profile.rules.condition_groups`: Task 2.
  - `semantic_profile.fields`: Task 3.
  - `semantic_profile.quality`: Task 1 and Task 4 validator.
  - validation: Task 4, Task 6, Task 8.
  - docs/prompt migration: Task 7.
- Historical failures covered:
  - direct/indirect source confusion: Task 4 and Task 6.
  - complex WHERE field-list-only issue: Task 2 and Task 4.
  - missing scope/rule linkage: Task 5.
  - key field usage not based on context: Task 3.
  - benchmark content inspection: Task 6.
- No planned replacement of old profile fields in this phase.
- No task requires changing `task_insight.html`; that can come after semantic profile exists.
