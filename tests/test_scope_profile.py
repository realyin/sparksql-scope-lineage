import json

from lineage_parser import parse_scope_lineage
from lineage_parser.scope_serializer import to_dict, to_profile_dict, write_output


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

    assert data["scope_profile"]["scope_count"] == len(data["scopes"])
    assert data["scope_profile"]["step_count"] == len(data["scope_profile"]["steps"])
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
    assert base["physical_source_tables"] == ["ods.events"]

    agg = _step_by_scope(data, "cte:agg")
    assert agg["role"] == "aggregate"
    assert "join" in agg["operations"]
    assert "aggregate" in agg["operations"]
    assert "ods.calls" in agg["direct_inputs"]
    assert "cte:base" in agg["direct_inputs"]
    assert agg["physical_source_tables"] == ["ods.calls", "ods.events"]
    assert any(j["right"] == "ods.calls" for j in agg["logic"]["joins"])
    assert any(a["column"] == "call_count" for a in agg["logic"]["aggregations"])

    root = _step_by_scope(data, "ROOT")
    assert root["kind"] == "root"
    assert root["role"] == "pass_through"
    assert root["direct_inputs"] == ["cte:agg"]

    end_to_end = data["end_to_end_lineage"]
    uid_lineage = next(item for item in end_to_end if item["column"] == "uid")
    assert uid_lineage["physical_sources"] == [
        {"table": "ods.events", "column": "user_id", "transform": "DIRECT"}
    ]
    call_count_lineage = next(item for item in end_to_end if item["column"] == "call_count")
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
        "task_id",
        "target_table",
        "stmt_kind",
        "source_tables",
        "scope_graph",
        "scope_profile",
        "root_columns",
        "end_to_end_lineage",
        "diagnostics",
    }
    assert "scopes" not in profile
    case_step = next(s for s in profile["scope_profile"]["steps"] if s["scope_id"] == "cte:labeled")
    case_item = case_step["logic"]["case_when"][0]
    assert case_item == {
        "column": "value_range",
        "summary": "CASE expression with 3 branches",
        "branch_count": 3,
    }
    assert "expression" not in case_item
    assert "case_branches" not in case_item


def test_write_output_writes_full_lineage_and_compact_profile(tmp_path):
    result = parse_scope_lineage(
        "INSERT INTO mart.t WITH c AS (SELECT a.id FROM ods.src a) SELECT id FROM c",
        "write_profile",
    )

    write_output(result, tmp_path)

    assert (tmp_path / "lineage.json").exists()
    assert (tmp_path / "profile.json").exists()
    profile = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    lineage = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert "scopes" not in profile
    assert "scopes" in lineage
    assert profile["end_to_end_lineage"][0]["physical_sources"] == [
        {"table": "ods.src", "column": "id", "transform": "DIRECT"}
    ]
