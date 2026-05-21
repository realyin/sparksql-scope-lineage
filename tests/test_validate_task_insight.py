import copy
import importlib.util
import json
import sys
from pathlib import Path

from lineage_parser import (
    build_task_insight,
    parse_scope_lineage,
    render_task_insight_html,
    to_dict,
    to_profile_dict,
)


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("validate_task_insight", TOOLS_DIR / "validate_task_insight.py")
validate_task_insight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validate_task_insight
SPEC.loader.exec_module(validate_task_insight)


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


def test_validate_task_insight_accepts_generated_output(tmp_path):
    result = _result()
    lineage = to_dict(result)
    profile = to_profile_dict(result)
    insight = build_task_insight(lineage=lineage, profile=profile)
    _write_artifacts(tmp_path, lineage, profile, insight)

    report = validate_task_insight.validate_task_dir(tmp_path)

    assert report["ok"] is True
    assert report["error_count"] == 0


def test_validate_task_insight_catches_count_mismatch(tmp_path):
    result = _result()
    lineage = to_dict(result)
    profile = to_profile_dict(result)
    insight = build_task_insight(lineage=lineage, profile=profile)
    _write_artifacts(tmp_path, lineage, profile, insight)
    insight_path = tmp_path / "task_insight.json"
    insight = json.loads(insight_path.read_text(encoding="utf-8"))
    insight["task"]["visible_scope_count"] = 999
    insight_path.write_text(json.dumps(insight, ensure_ascii=False), encoding="utf-8")

    report = validate_task_insight.validate_task_dir(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "visible_scope_count_mismatch" for item in report["findings"])
    assert any(item["code"] == "html_payload_stale" for item in report["findings"])


def test_validate_task_insight_catches_missing_link_endpoint(tmp_path):
    result = _result()
    lineage = to_dict(result)
    profile = to_profile_dict(result)
    insight = build_task_insight(lineage=lineage, profile=profile)
    _write_artifacts(tmp_path, lineage, profile, insight)
    insight_path = tmp_path / "task_insight.json"
    insight = json.loads(insight_path.read_text(encoding="utf-8"))
    insight["links"].append({"from": "scope:missing", "to": "scope:base", "type": "feeds"})
    (tmp_path / "task_insight.html").write_text(render_task_insight_html(insight), encoding="utf-8")
    insight_path.write_text(json.dumps(insight, ensure_ascii=False), encoding="utf-8")

    report = validate_task_insight.validate_task_dir(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "link_source_missing" for item in report["findings"])


def test_validate_task_insight_catches_scope_read_claim_without_direct_source(tmp_path):
    result = _result()
    lineage = to_dict(result)
    profile = to_profile_dict(result)
    insight = build_task_insight(lineage=lineage, profile=profile)
    broken = copy.deepcopy(insight)
    broken["objects"]["scopes"]["scope:base"]["summary"] = "读取 ods.touch_events"
    broken["objects"]["scopes"]["scope:base"]["direct_source_tables"] = []

    _write_artifacts(tmp_path, lineage, profile, broken)

    report = validate_task_insight.validate_task_dir(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "scope_reads_without_direct_sources" for item in report["findings"])


def _write_artifacts(tmp_path, lineage, profile, insight):
    (tmp_path / "lineage.json").write_text(json.dumps(lineage, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "task_insight.json").write_text(json.dumps(insight, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "task_insight.html").write_text(render_task_insight_html(insight), encoding="utf-8")
