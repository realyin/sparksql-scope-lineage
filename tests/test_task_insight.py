import json

from lineage_parser import (
    build_task_insight,
    parse_scope_lineage,
    render_task_insight_html,
    to_dict,
    to_profile_dict,
    write_task_insight_report,
)


def _result():
    sql = """
    INSERT OVERWRITE TABLE mart.customer_touch
    WITH base AS (
      SELECT customer_id, app_code, event_time
      FROM ods.touch_events
      WHERE dt = '20260515' AND status = 'connected'
    ),
    ranked AS (
      SELECT
        customer_id,
        app_code,
        first_value(event_time) over(
          partition by customer_id, app_code
          order by event_time
        ) as first_touch_time
      FROM base
    )
    SELECT customer_id, app_code, first_touch_time
    FROM ranked
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
    return parse_scope_lineage(sql, "customer_touch_task", schema=schema)


def test_build_task_insight_indexes_scopes_rules_columns_and_links():
    result = _result()
    lineage = to_dict(result)
    profile = to_profile_dict(result)

    insight = build_task_insight(lineage=lineage, profile=profile)

    assert insight["schema_version"] == "1.0"
    assert insight["task"]["task_name"] == "customer_touch_task"
    assert insight["task"]["target_table"] == "mart.customer_touch"
    assert insight["capabilities"]["has_rule_index"] is True
    assert "scope:base" in insight["objects"]["scopes"]
    assert "scope:ranked" in insight["objects"]["scopes"]
    assert "column:first_touch_time" in insight["objects"]["columns"]
    assert "table:ods.touch_events" in insight["objects"]["tables"]

    base_scope = insight["objects"]["scopes"]["scope:base"]
    assert base_scope["logic"]["filters"]
    assert base_scope["evidence"]

    rules = insight["objects"]["rules"]
    assert any("status" in json.dumps(rule, ensure_ascii=False) for rule in rules.values())
    assert any(link["type"] == "implemented_by" for link in insight["links"])
    assert any(link["type"] == "uses_field" for link in insight["links"])
    assert any(link["type"] == "references" for link in insight["links"])


def test_render_task_insight_html_contains_payload_and_workbench_sections():
    result = _result()
    insight = build_task_insight(lineage=to_dict(result), profile=to_profile_dict(result))

    html = render_task_insight_html(insight)

    assert "<!doctype html>" in html
    assert 'id="task-insight-data"' in html
    assert "SQL Task Insight" in html
    assert "业务阶段" in html
    assert "Scope DAG" in html
    assert "字段血缘" in html
    assert "customer_touch_task" in html


def test_write_task_insight_report_writes_json_and_html(tmp_path):
    result = _result()

    out = write_task_insight_report(result, tmp_path)

    assert out == tmp_path
    insight_path = tmp_path / "task_insight.json"
    html_path = tmp_path / "task_insight.html"
    assert insight_path.exists()
    assert html_path.exists()

    insight = json.loads(insight_path.read_text(encoding="utf-8"))
    assert insight["objects"]["columns"]["column:first_touch_time"]["trace_complete"] is True
    assert "task-insight-data" in html_path.read_text(encoding="utf-8")


def test_build_task_insight_accepts_full_diagnostics_warnings():
    result = _result()
    diagnostics = {
        "warnings": [
            {
                "type": "filter_in_join_on_clause",
                "scope": "cte:ranked",
                "msg": "JOIN ON clause contains a row filter",
            }
        ],
        "stats": {"scope_count": 3},
    }

    insight = build_task_insight(
        lineage=to_dict(result),
        profile=to_profile_dict(result),
        diagnostics=diagnostics,
    )

    assert insight["task"]["warning_count"] == 1
    assert insight["objects"]["diagnostics"]
    diagnostic = next(iter(insight["objects"]["diagnostics"].values()))
    assert diagnostic["code"] == "filter_in_join_on_clause"
    assert diagnostic["scope_ids"] == ["scope:ranked"]


def test_task_insight_keeps_union_branch_nodes_for_scope_graph():
    sql = """
    INSERT OVERWRITE TABLE mart.touch_union
    SELECT id, event_time FROM ods.online_touch
    UNION ALL
    SELECT id, event_time FROM ods.hotline_touch
    """
    schema = {
        "ods.online_touch": [{"name": "id"}, {"name": "event_time"}],
        "ods.hotline_touch": [{"name": "id"}, {"name": "event_time"}],
        "mart.touch_union": [{"name": "id"}, {"name": "event_time"}],
    }
    result = parse_scope_lineage(sql, "touch_union_task", schema=schema)

    insight = build_task_insight(lineage=to_dict(result), profile=to_profile_dict(result))

    assert "scope:main:b01" in insight["objects"]["scopes"]
    assert "scope:main:b02" in insight["objects"]["scopes"]
    assert any(
        link == {"from": "table:ods.online_touch", "to": "scope:main:b01", "type": "feeds"}
        for link in insight["links"]
    )
    assert any(
        link == {"from": "scope:main:b01", "to": "scope:main", "type": "feeds"}
        for link in insight["links"]
    )
