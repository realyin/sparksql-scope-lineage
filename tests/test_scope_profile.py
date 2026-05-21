import json

from lineage_parser import parse_scope_lineage
from lineage_parser import scope_serializer
from lineage_parser.schema_metadata import SchemaMap
from lineage_parser.scope_serializer import to_dict, to_profile_dict, write_output
from lineage_parser.scope_types import DiagnosticWarning


def _step_by_scope(data, scope_id):
    return next(s for s in data["scope_profile"]["steps"] if s["scope_id"] == scope_id)


def test_scope_profile_summarizes_scope_operations_for_llm_use():
    sql = """
    INSERT INTO mart.call_summary
    WITH base AS (
      SELECT
        a.session_id,
        a.user_id AS uid,
        CASE WHEN a.channel = 'online' THEN '在线' ELSE a.channel END AS channel_name,
        ROW_NUMBER() OVER (PARTITION BY a.user_id ORDER BY a.event_time DESC) AS rn
      FROM ods.events a
      WHERE a.risklevel = '高风险'
    ),
    agg AS (
      SELECT
        b.uid,
        COUNT(DISTINCT c.call_id) AS call_count,
        MAX(c.call_time) AS last_call_time
      FROM base b
      LEFT JOIN ods.calls c
        ON b.uid = c.user_id
      GROUP BY b.uid
    )
    SELECT
      uid,
      call_count,
      last_call_time
    FROM agg
    """

    data = to_dict(parse_scope_lineage(sql, "profile_test"))
    json.dumps(data, ensure_ascii=False)

    assert "scope_count" not in data["scope_profile"]
    assert "step_count" not in data["scope_profile"]
    assert data["scope_profile"]["profile_step_count"] == len(data["scope_profile"]["steps"])
    assert [s["scope_id"] for s in data["scope_profile"]["steps"]] == [
        "cte:base",
        "cte:agg",
        "ROOT",
    ]

    base = _step_by_scope(data, "cte:base")
    assert base["name"] == "base"
    assert base["kind"] == "cte"
    assert base["role"] == "dedup"
    assert "filter" in base["operations"]
    assert "case_when" in base["operations"]
    assert "window" in base["operations"]
    assert {"from": "user_id", "to": "uid"} in base["logic"]["key_renames"]
    assert any("risklevel" in f for f in base["logic"]["filters"])
    assert base["logic"]["case_when"] == [
        {"column": "channel_name", "summary": "CASE expression with 2 branches", "branch_count": 2}
    ]
    assert base["direct_inputs"] == ["ods.events"]
    assert base["direct_source_tables"] == ["ods.events"]
    assert base["physical_source_tables"] == ["ods.events"]

    agg = _step_by_scope(data, "cte:agg")
    assert agg["role"] == "aggregate"
    assert "join" in agg["operations"]
    assert "aggregate" in agg["operations"]
    assert "ods.calls" in agg["direct_inputs"]
    assert "cte:base" in agg["direct_inputs"]
    assert agg["direct_source_tables"] == ["ods.calls"]
    assert agg["physical_source_tables"] == ["ods.calls", "ods.events"]
    assert agg["business_summary"].startswith("读取 ods.calls；")
    assert "上游可追溯至 ods.events" in agg["business_summary"]
    assert any(j["right"] == "ods.calls" for j in agg["logic"]["joins"])
    assert any(a["column"] == "call_count" for a in agg["logic"]["aggregations"])

    root = _step_by_scope(data, "ROOT")
    assert root["kind"] == "root"
    assert root["role"] == "pass_through"
    assert root["direct_inputs"] == ["cte:agg"]

    end_to_end = data["end_to_end_lineage"]
    uid_lineage = next(item for item in end_to_end if item["column"] == "uid")
    assert uid_lineage["trace_complete"] is True
    assert "trace_incomplete_reasons" not in uid_lineage
    assert uid_lineage["physical_sources"] == [
        {"table": "ods.events", "column": "user_id", "transform": "DIRECT"}
    ]
    call_count_lineage = next(item for item in end_to_end if item["column"] == "call_count")
    assert call_count_lineage["trace_complete"] is True
    assert "trace_incomplete_reasons" not in call_count_lineage
    assert call_count_lineage["physical_sources"] == [
        {"table": "ods.calls", "column": "call_id", "transform": "AGGREGATE"}
    ]


def test_profile_dict_is_compact_for_llm_preanalysis():
    sql = """
    INSERT INTO mart.t
    WITH labeled AS (
      SELECT
        a.id,
        CASE WHEN a.value IS NULL THEN 'missing' WHEN a.value >= 10 THEN 'high' ELSE 'low' END AS value_range
      FROM ods.scores a
    )
    SELECT id, value_range FROM labeled
    """

    profile = to_profile_dict(parse_scope_lineage(sql, "compact_profile"))

    assert set(profile) == {
        "task_name",
        "target_table",
        "stmt_kind",
        "source_tables",
        "related_metadata",
        "summary",
        "business_profile",
        "semantic_profile",
        "business_rule_candidates",
        "grain",
        "important_columns",
        "expression_catalog",
        "filters_summary",
        "read_order",
        "compact_policy",
        "scope_profile",
        "end_to_end_lineage",
        "diagnostics",
    }
    assert profile["summary"] == {
        "task_name": "compact_profile",
        "target_table": "mart.t",
        "stmt_kind": "INSERT",
        "input_table_count": 1,
        "output_column_count": 2,
        "main_operations": ["case_when"],
        "main_process": "从1张输入表读取数据，经过case_when后写入 mart.t",
    }
    assert profile["grain"] == {
        "type": "record_level",
        "keys": ["id"],
        "key_type": "candidate_output_keys",
        "confidence": "medium",
        "evidence": ["id_like_output_columns"],
        "note": "keys are heuristic candidate output identifiers, not a verified primary key",
    }
    assert profile["important_columns"] == [
        {
            "column": "id",
            "transform": "DIRECT",
            "importance": "medium",
            "reasons": ["id_or_key_column"],
        },
        {
            "column": "value_range",
            "transform": "DIRECT",
            "importance": "medium",
            "reasons": ["derived_from_physical_sources"],
        },
    ]
    assert profile["expression_catalog"] == [
        {
            "id": "expr_1",
            "type": "CASE_WHEN",
            "columns": ["value_range"],
            "summary": "CASE expression with 3 branches",
            "branch_count": 3,
        }
    ]
    assert profile["filters_summary"] == []
    assert profile["business_rule_candidates"] == []
    assert profile["business_profile"]["objective"]["summary"] == (
        "生成 mart.t；主要读取 ods.scores；语义线索包括 评分/分数"
    )
    assert profile["read_order"][:5] == [
        "summary",
        "semantic_profile",
        "business_profile",
        "grain",
        "scope_profile.steps",
    ]
    assert "scopes" not in profile
    assert "scope_graph" not in profile
    assert "root_columns" not in profile
    id_lineage = next(item for item in profile["end_to_end_lineage"] if item["column"] == "id")
    assert id_lineage["expression"] == "`labeled`.`id`"
    value_range_lineage = next(item for item in profile["end_to_end_lineage"] if item["column"] == "value_range")
    assert value_range_lineage["expression"] == "`labeled`.`value_range`"
    case_step = next(s for s in profile["scope_profile"]["steps"] if s["scope_id"] == "cte:labeled")
    assert case_step["business_summary"] == "读取 ods.scores；通过 CASE WHEN 派生字段"
    case_item = case_step["logic"]["case_when"][0]
    assert case_item == {
        "column": "value_range",
        "summary": "CASE expression with 3 branches",
        "branch_count": 3,
    }
    assert "expression" not in case_item
    assert "case_branches" not in case_item


def test_scope_profile_filters_parser_only_pass_through_steps():
    sql = """
    INSERT INTO mart.t
    WITH t_dt AS (
      SELECT dt FROM ods.calendar WHERE dt = '20260515'
    )
    SELECT
      a.id,
      d.dt
    FROM ods.events a
    LEFT JOIN (
      SELECT dt FROM t_dt
    ) d
      ON a.dt = d.dt
    """

    data = to_dict(parse_scope_lineage(sql, "profile_passthrough_filter"))
    step_ids = [step["scope_id"] for step in data["scope_profile"]["steps"]]

    assert "cte:t_dt" in step_ids
    assert "ROOT" in step_ids
    assert not any(step_id.startswith("subq:") for step_id in step_ids)
    assert data["scope_profile"]["profile_step_count"] == len(step_ids)
    assert data["scope_profile"]["profile_step_count"] < len(data["scopes"])


def test_write_output_writes_full_lineage_and_compact_profile(tmp_path):
    result = parse_scope_lineage(
        "INSERT INTO mart.t WITH c AS (SELECT a.id FROM ods.src a) SELECT id FROM c",
        "write_profile",
    )

    write_output(result, tmp_path)

    assert (tmp_path / "lineage.json").exists()
    assert (tmp_path / "profile.json").exists()
    profile_text = (tmp_path / "profile.json").read_text(encoding="utf-8")
    assert "\n" not in profile_text
    profile = json.loads(profile_text)
    lineage = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert "scopes" not in profile
    assert "scopes" in lineage
    assert profile["end_to_end_lineage"][0]["physical_sources"] == [
        {"table": "ods.src", "column": "id", "transform": "DIRECT"}
    ]


def test_end_to_end_lineage_marks_unexpanded_star_incomplete():
    result = parse_scope_lineage(
        "INSERT INTO mart.t SELECT a.* FROM ods.src a",
        "star_incomplete",
    )

    item = to_profile_dict(result)["end_to_end_lineage"][0]

    assert item["column"] == "a.*"
    assert item["trace_complete"] is False
    assert item["trace_incomplete_reasons"] == ["star_not_expanded"]
    assert item["physical_sources"] == [
        {"table": "ods.src", "column": "*", "transform": "EXPAND_ALL"}
    ]


def test_scope_profile_includes_distinct_union_branches_and_lateral_views():
    sql = """
    INSERT INTO mart.t
    WITH dedup AS (
      SELECT DISTINCT a.id FROM ods.src a
    )
    SELECT id, 'dedup' AS src FROM dedup
    UNION ALL
    SELECT t.val, 'explode' AS src
    FROM ods.arrays a
    LATERAL VIEW posexplode(split(a.payload, ',')) t AS pos, val
    """

    profile = to_profile_dict(parse_scope_lineage(sql, "logic_patterns"))

    dedup = _step_by_scope(profile, "cte:dedup")
    assert dedup["logic"]["distinct"] is True

    union = next(s for s in profile["scope_profile"]["steps"] if s["kind"] == "union")
    assert union["logic"]["union_branches"] == 2
    assert not any(s["kind"] == "union_branch" for s in profile["scope_profile"]["steps"])

    lateral = _step_by_scope(profile, "udtf:t")
    assert lateral["logic"]["lateral_views"] == [
        {
            "alias": "t",
            "function": "POSEXPLODE",
            "expression": "POSEXPLODE(SPLIT(a.payload, ','))",
            "output_columns": ["pos", "val"],
        }
    ]


def test_related_metadata_keeps_only_columns_used_by_any_scope_when_safe():
    sql = """
    INSERT INTO mart.t
    SELECT
      a.call_id,
      CASE WHEN a.risklevel = '高风险' THEN a.phone_number ELSE NULL END AS risky_phone
    FROM report_csc_ana.hotline_detail_realtime a
    WHERE a.dt = '20260515'
    """
    schema = {
        "report_csc_ana.hotline_detail_realtime": [
            {"name": "dt", "type": "date", "comment": None},
            {"name": "call_id", "type": "string", "comment": "拨打编号"},
            {"name": "risklevel", "type": "string", "comment": "风险等级"},
            {"name": "phone_number", "type": "string", "comment": "手机号"},
            {"name": "unused_col", "type": "string", "comment": "未使用"},
        ],
        "mart.t": [
            {"name": "call_id", "type": "string", "comment": "拨打编号"},
            {"name": "risky_phone", "type": "string", "comment": "风险手机号"},
            {"name": "load_time", "type": "timestamp", "comment": "加载时间"},
        ]
    }

    profile = to_profile_dict(parse_scope_lineage(sql, "metadata_filter", schema=schema))

    assert profile["related_metadata"] == {
        "input_tables": {
            "report_csc_ana.hotline_detail_realtime": {
                "column_details": [
                    {"name": "dt", "type": "date", "comment": None},
                    {"name": "call_id", "type": "string", "comment": "拨打编号"},
                    {"name": "risklevel", "type": "string", "comment": "风险等级"},
                    {"name": "phone_number", "type": "string", "comment": "手机号"},
                ],
                "metadata_complete": True,
            }
        },
        "output_tables": {
            "mart.t": {
                "column_details": [
                    {"name": "call_id", "type": "string", "comment": "拨打编号"},
                    {"name": "risky_phone", "type": "string", "comment": "风险手机号"},
                ],
                "metadata_complete": True,
            }
        },
    }


def test_related_metadata_output_table_falls_back_to_root_without_schema():
    sql = """
    INSERT INTO mart.t
    SELECT a.id, a.score AS final_score
    FROM ods.src a
    """
    schema = {
        "ods.src": [
            {"name": "id", "type": "string", "comment": "ID"},
            {"name": "score", "type": "decimal(10,2)", "comment": "分数"},
        ]
    }

    profile = to_profile_dict(parse_scope_lineage(sql, "metadata_output_fallback", schema=schema))

    assert profile["related_metadata"]["output_tables"] == {
        "mart.t": {
            "column_details": [
                {"name": "id", "type": None, "comment": None},
                {"name": "final_score", "type": None, "comment": None},
            ],
            "metadata_complete": False,
        }
    }


def test_related_metadata_includes_join_fields_without_keeping_whole_join_table():
    sql = """
    INSERT INTO mart.t
    SELECT a.call_id, b.queue_name
    FROM report_csc_ana.hotline_detail_realtime a
    LEFT JOIN dim.queue b
      ON a.queue_id = b.queue_id
    """
    schema = {
        "report_csc_ana.hotline_detail_realtime": [
            {"name": "call_id", "type": "string", "comment": "拨打编号"},
            {"name": "queue_id", "type": "string", "comment": "队列ID"},
            {"name": "unused_a", "type": "string", "comment": None},
        ],
        "dim.queue": [
            {"name": "queue_id", "type": "string", "comment": "队列ID"},
            {"name": "queue_name", "type": "string", "comment": "队列名称"},
            {"name": "unused_b", "type": "string", "comment": None},
        ],
    }

    profile = to_profile_dict(parse_scope_lineage(sql, "metadata_join", schema=schema))

    assert profile["related_metadata"]["input_tables"]["report_csc_ana.hotline_detail_realtime"]["column_details"] == [
        {"name": "call_id", "type": "string", "comment": "拨打编号"},
        {"name": "queue_id", "type": "string", "comment": "队列ID"},
    ]
    assert profile["related_metadata"]["input_tables"]["dim.queue"]["column_details"] == [
        {"name": "queue_id", "type": "string", "comment": "队列ID"},
        {"name": "queue_name", "type": "string", "comment": "队列名称"},
    ]


def test_business_profile_extracts_rule_candidates_and_semantic_hints():
    sql = """
    INSERT OVERWRITE TABLE mart.in_collect
    SELECT a.internal_customer_id, a.acct_nbr
    FROM ods.loan_all a
    LEFT JOIN (SELECT DISTINCT contra_no FROM dim.excess) b
      ON a.contr_nbr = b.contra_no
    WHERE a.dt = '20260426'
      AND (a.overdue_date IS NOT NULL OR a.in_clct_dpd BETWEEN -7 AND 0 OR b.contra_no IS NOT NULL)
    """
    schema = SchemaMap(
        {
            "ods.loan_all": ["internal_customer_id", "acct_nbr", "dt", "overdue_date", "in_clct_dpd", "contr_nbr", "unused"],
            "dim.excess": ["contra_no"],
            "mart.in_collect": ["internal_customer_id", "acct_nbr"],
        },
        column_details={
            "ods.loan_all": [
                {"name": "internal_customer_id", "type": "string", "comment": "客户ID"},
                {"name": "acct_nbr", "type": "string", "comment": "账户号"},
                {"name": "dt", "type": "string", "comment": "日期分区"},
                {"name": "overdue_date", "type": "string", "comment": "逾期日期"},
                {"name": "in_clct_dpd", "type": "int", "comment": "入催DPD"},
                {"name": "contr_nbr", "type": "string", "comment": "合同号"},
                {"name": "unused", "type": "string", "comment": "未使用"},
            ],
            "dim.excess": [
                {"name": "contra_no", "type": "string", "comment": "超额合同号"},
            ],
            "mart.in_collect": [
                {"name": "internal_customer_id", "type": "string", "comment": "客户ID"},
                {"name": "acct_nbr", "type": "string", "comment": "账户号"},
            ],
        },
        table_details={
            "ods.loan_all": {"table_name_cn": "贷款全量表"},
            "dim.excess": {"table_name_cn": "超额合同维表"},
            "mart.in_collect": {"table_name_cn": "入催名单表"},
        },
    )

    profile = to_profile_dict(parse_scope_lineage(sql, "business_profile", schema=schema))

    assert "入催名单表" in profile["business_profile"]["objective"]["summary"]
    assert "入催" in profile["business_profile"]["objective"]["semantic_hints"]
    assert profile["business_profile"]["objective"]["primary_decision"] == "是否保留/纳入目标结果"
    dedup_step = _step_by_scope(profile, "subq:b")
    assert dedup_step["role"] == "dedup"

    where_rule = next(item for item in profile["business_rule_candidates"] if item["source"] == "WHERE")
    assert where_rule["condition_group_type"] == "MIXED_AND_OR"
    assert where_rule["fields"] == ["dt", "in_clct_dpd", "contra_no", "overdue_date"]
    assert {item["comment"] for item in where_rule["field_details"]} >= {"日期分区", "入催DPD", "超额合同号", "逾期日期"}

    join_rule = next(item for item in profile["business_rule_candidates"] if item["source"] == "JOIN_ON")
    assert join_rule["fields"] == ["contr_nbr", "contra_no"]


def test_metadata_compaction_prioritizes_rule_and_lineage_columns(monkeypatch):
    monkeypatch.setattr(scope_serializer, "PROFILE_MAX_METADATA_COLUMNS_PER_TABLE", 3)
    sql = """
    INSERT INTO mart.t
    SELECT a.id, a.metric
    FROM ods.wide a
    WHERE a.dt = '20260515' AND a.important_flag = 'Y'
    """
    schema = {
        "ods.wide": [
            {"name": "unused_1", "type": "string", "comment": "未使用1"},
            {"name": "unused_2", "type": "string", "comment": "未使用2"},
            {"name": "id", "type": "string", "comment": "主键"},
            {"name": "metric", "type": "decimal", "comment": "指标"},
            {"name": "dt", "type": "string", "comment": "分区"},
            {"name": "important_flag", "type": "string", "comment": "重要标记"},
        ],
        "mart.t": [
            {"name": "id", "type": "string", "comment": "主键"},
            {"name": "metric", "type": "decimal", "comment": "指标"},
        ],
    }

    profile = to_profile_dict(parse_scope_lineage(sql, "metadata_priority", schema=schema))

    names = [
        item["name"]
        for item in profile["related_metadata"]["input_tables"]["ods.wide"]["column_details"]
    ]
    assert names == ["dt", "important_flag", "id"]


def test_related_metadata_keeps_all_columns_for_uncertain_star_reference():
    sql = "INSERT INTO mart.t SELECT a.* FROM report_csc_ana.hotline_detail_realtime a"
    schema = {
        "report_csc_ana.hotline_detail_realtime": [
            {"name": "dt", "type": "date", "comment": None},
            {"name": "call_id", "type": "string", "comment": "拨打编号"},
            {"name": "begin_call_dt", "type": "string", "comment": None},
        ]
    }

    result = parse_scope_lineage(sql, "metadata_star", schema=schema)
    profile = to_profile_dict(result)

    assert profile["end_to_end_lineage"][0]["trace_complete"] is True
    assert profile["related_metadata"] == {
        "input_tables": {
            "report_csc_ana.hotline_detail_realtime": {
                "column_details": [
                    {"name": "dt", "type": "date", "comment": None},
                    {"name": "call_id", "type": "string", "comment": "拨打编号"},
                    {"name": "begin_call_dt", "type": "string", "comment": None},
                ],
                "metadata_complete": True,
            }
        },
        "output_tables": {
            "mart.t": {
                "column_details": [
                    {"name": "dt", "type": None, "comment": None},
                    {"name": "call_id", "type": None, "comment": None},
                    {"name": "begin_call_dt", "type": None, "comment": None},
                ],
                "metadata_complete": False,
            }
        },
    }


def test_related_metadata_includes_input_tables_missing_from_schema():
    sql = """
    INSERT INTO mart.t
    SELECT a.id, b.score
    FROM ods.known a
    LEFT JOIN ods.missing b
      ON a.id = b.id
    WHERE b.dt = '20260515'
    """
    schema = {
        "ods.known": [
            {"name": "id", "type": "string", "comment": "ID"},
            {"name": "unused", "type": "string", "comment": "未使用"},
        ]
    }

    profile = to_profile_dict(parse_scope_lineage(sql, "metadata_missing_schema", schema=schema))

    assert "task_id" not in profile
    assert profile["task_name"] == "metadata_missing_schema"
    assert profile["related_metadata"]["input_tables"] == {
        "ods.known": {
            "column_details": [{"name": "id", "type": "string", "comment": "ID"}],
            "metadata_complete": True,
        },
        "ods.missing": {
            "column_details": [
                {"name": "score", "type": None, "comment": None},
                {"name": "id", "type": None, "comment": None},
                {"name": "dt", "type": None, "comment": None},
            ],
            "metadata_complete": False,
        },
    }


def test_related_metadata_includes_table_level_metadata():
    sql = "INSERT INTO mart.t SELECT a.id FROM ods.known a"
    schema = SchemaMap(
        {"ods.known": ["id"], "mart.t": ["id"]},
        column_details={
            "ods.known": [{"name": "id", "type": "string", "comment": "用户ID"}],
            "mart.t": [{"name": "id", "type": "string", "comment": "用户ID"}],
        },
        table_details={
            "ods.known": {
                "table_name_cn": "用户源表",
                "table_desc": "用户基础信息来源表",
                "table_label_layer": "ODS",
            },
            "mart.t": {
                "table_name_cn": "用户画像表",
                "table_desc": "用户画像输出表",
                "table_label_layer": "ADS",
            },
        },
    )

    profile = to_profile_dict(parse_scope_lineage(sql, "metadata_table_detail", schema=schema))

    assert profile["related_metadata"]["input_tables"]["ods.known"]["table_metadata"] == {
        "table_name_cn": "用户源表",
        "table_desc": "用户基础信息来源表",
        "table_label_layer": "ODS",
    }
    assert profile["related_metadata"]["output_tables"]["mart.t"]["table_metadata"] == {
        "table_name_cn": "用户画像表",
        "table_desc": "用户画像输出表",
        "table_label_layer": "ADS",
    }


def test_profile_compaction_truncates_large_sections(monkeypatch):
    monkeypatch.setattr(scope_serializer, "PROFILE_MAX_EXPRESSION_CHARS", 20)
    monkeypatch.setattr(scope_serializer, "PROFILE_MAX_METADATA_COLUMNS_PER_TABLE", 2)
    monkeypatch.setattr(scope_serializer, "PROFILE_MAX_PHYSICAL_SOURCES_PER_COLUMN", 1)
    monkeypatch.setattr(scope_serializer, "PROFILE_MAX_SOURCE_TABLES", 1)
    monkeypatch.setattr(scope_serializer, "PROFILE_MAX_WARNINGS", 1)

    schema = {
        "ods.a": [
            {"name": "id", "type": "string", "comment": "ID"},
            {"name": "c1", "type": "string", "comment": "一"},
            {"name": "c2", "type": "string", "comment": "二"},
            {"name": "c3", "type": "string", "comment": "三"},
        ],
        "ods.b": [
            {"name": "id", "type": "string", "comment": "ID"},
            {"name": "c1", "type": "string", "comment": "一"},
        ],
    }
    sql = """
    INSERT INTO mart.t
    SELECT
      CONCAT(a.c1, a.c2, a.c3, b.c1, 'this is a deliberately long literal') AS long_expr,
      CASE WHEN a.c1 = 'x' THEN a.c2 ELSE b.c1 END AS mixed_col
    FROM ods.a a
    LEFT JOIN ods.b b
      ON a.id = b.id
    WHERE a.c1 = 'x'
    """

    result = parse_scope_lineage(sql, "profile_compaction", schema=schema)
    result.diagnostics.warnings.append(DiagnosticWarning(type="magic_number", scope="ROOT", msg="first"))
    result.diagnostics.warnings.append(DiagnosticWarning(type="magic_number", scope="ROOT", msg="second"))

    profile = to_profile_dict(result)

    assert profile["source_tables_count"] == 2
    assert profile["source_tables_truncated"] is True
    assert profile["source_tables"] == ["ods.a"]

    long_expr = next(item for item in profile["end_to_end_lineage"] if item["column"] == "long_expr")
    assert len(long_expr["expression"]) <= 20
    assert long_expr["expression_truncated"] is True
    assert long_expr["expression_length"] > len(long_expr["expression"])
    assert long_expr["physical_source_count"] > len(long_expr["physical_sources"])
    assert long_expr["physical_sources_truncated"] is True

    table_meta = profile["related_metadata"]["input_tables"]["ods.a"]
    assert len(table_meta["column_details"]) == 2
    assert table_meta["column_count"] == 4
    assert table_meta["shown_column_count"] == 2
    assert table_meta["columns_truncated"] is True

    diagnostics = profile["diagnostics"]
    assert diagnostics["warning_count"] == 2
    assert diagnostics["warnings_truncated"] is True
    assert diagnostics["warning_types"] == {"magic_number": 2}
    assert len(diagnostics["warnings_sample"]) == 1
    assert "warnings" not in diagnostics
