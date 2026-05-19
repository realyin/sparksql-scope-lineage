from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "validate_llm_profile_doc", TOOLS_DIR / "validate_llm_profile_doc.py"
)
validate_llm_profile_doc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = validate_llm_profile_doc
SPEC.loader.exec_module(validate_llm_profile_doc)


def _profile():
    return {
        "task_name": "demo_task",
        "target_table": "mart.demo",
        "grain": {
            "keys": ["id"],
            "key_type": "candidate_output_keys",
        },
        "related_metadata": {
            "input_tables": {
                "ods.src": {
                    "column_details": [{"name": "id", "type": "string", "comment": None}],
                    "metadata_complete": False,
                }
            },
            "output_tables": {},
        },
        "end_to_end_lineage": [
            {
                "column": "id",
                "trace_complete": True,
                "physical_sources": [{"table": "ods.src", "column": "id"}],
            },
            {
                "column": "a.*",
                "trace_complete": False,
                "trace_incomplete_reasons": ["star_not_expanded"],
            },
        ],
        "diagnostics": {
            "warning_types": {
                "star_not_expanded": 1,
                "magic_number": 1,
            }
        },
    }


def test_validate_profile_doc_accepts_compliant_document():
    doc = """
# SQL 任务画像：demo_task
## L1：任务概览
任务 demo_task 写入 mart.demo。
## L2：输入输出
输入元数据 metadata_complete=false，存在 schema/SELECT * 覆盖边界。
## L3：加工步骤
基于 profile 中的步骤描述。
## L4：核心字段/指标
候选输出标识字段包括 id，不是已验证主键。
## L5：血缘可信度和风险边界
id 可完整追溯；a.* trace_complete=false，原因是 star_not_expanded，追溯不完整。
magic_number 只是硬编码提示。
"""

    result = validate_llm_profile_doc.validate_profile_doc(_profile(), doc)

    assert result["ok"] is True
    assert result["error_count"] == 0
    assert result["warning_count"] == 0


def test_validate_profile_doc_rejects_primary_key_claim_and_missing_incomplete_field():
    doc = """
# SQL 任务画像：demo_task
## L1：任务概览
任务 demo_task 写入 mart.demo。
## L2：输入输出
输入输出。
## L3：加工步骤
步骤。
## L4：核心字段/指标
主键是 id。
## L5：血缘可信度和风险边界
全部完整。
"""

    result = validate_llm_profile_doc.validate_profile_doc(_profile(), doc)
    codes = {finding["code"] for finding in result["findings"]}

    assert result["ok"] is False
    assert "grain_candidate_not_disclosed" in codes
    assert "grain_called_primary_key" in codes
    assert "missing_incomplete_column" in codes
    assert "schema_boundary_not_disclosed" in codes


def test_validate_profile_doc_warns_when_semantic_metadata_is_not_used():
    profile = _profile()
    profile["important_columns"] = [{"column": "id", "reasons": ["id_or_key_column"]}]
    profile["related_metadata"]["input_tables"]["ods.src"]["column_details"][0]["comment"] = "用户ID"
    profile["related_metadata"]["input_tables"]["ods.src"]["table_metadata"] = {
        "table_name_cn": "用户源表",
        "table_desc": "用户基础信息来源表",
    }
    doc = """
# SQL 任务画像：demo_task
## L1：任务概览
任务 demo_task 写入 mart.demo。
## L2：输入输出
输入表 ods.src。
## L3：加工步骤
步骤。
## L4：核心字段/指标
候选输出标识字段包括 id，不是已验证主键。
## L5：血缘可信度和风险边界
id 可完整追溯；a.* trace_complete=false，原因是 star_not_expanded，追溯不完整。
存在 schema/SELECT * 覆盖边界。
"""

    result = validate_llm_profile_doc.validate_profile_doc(profile, doc)
    codes = {finding["code"] for finding in result["findings"]}

    assert result["ok"] is True
    assert "table_semantics_not_used" in codes
    assert "column_semantics_not_used" in codes
