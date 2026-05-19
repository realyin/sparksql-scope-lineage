from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "summarize_yellow_actions", TOOLS_DIR / "summarize_yellow_actions.py"
)
summarize_yellow_actions = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = summarize_yellow_actions
SPEC.loader.exec_module(summarize_yellow_actions)


def test_yellow_action_summary_classifies_schema_gap_star_and_no_edges(tmp_path):
    out_root = tmp_path / "out"
    group_dir = out_root / "group"
    stmt_dir = group_dir / "stmt1"
    stmt_dir.mkdir(parents=True)
    (stmt_dir / "profile.json").write_text(
        json.dumps(
            {
                "end_to_end_lineage": [
                    {
                        "column": "a_star",
                        "trace_complete": False,
                        "trace_incomplete_reasons": ["star_not_expanded"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "out_dir": str(group_dir),
                "severity_counts": {"YELLOW": 1},
                "issue_counts": {
                    "schema_incomplete_column_ref": 1,
                    "star_passthrough_placeholder": 1,
                    "field_lineage.mmd_no_edges": 1,
                },
                "warning_counts": {"star_not_expanded": 1},
                "records": [
                    {
                        "stmt": "stmt1",
                        "severity": "YELLOW",
                        "target": "mart.t",
                        "root_cols": 1,
                        "issues": {
                            "schema_incomplete_column_ref": 1,
                            "star_passthrough_placeholder": 1,
                            "field_lineage.mmd_no_edges": 1,
                        },
                        "warnings": {"star_not_expanded": 1},
                        "details": [
                            "schema incomplete: ROOT.c -> subq:a.dt expr=`a`.`dt`",
                            "star passthrough placeholders: 1",
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = summarize_yellow_actions.build_action_summary(out_root, [audit_path])

    assert summary["summary"]["schema_gap_count"] == 1
    assert summary["summary"]["star_final_affected_tasks"] == 1
    assert summary["summary"]["field_lineage_no_edges_count"] == 1
    assert summary["schema_gap_columns"] == [
        {
            "source_scope": "subq:a",
            "column": "dt",
            "tasks": ["stmt1"],
            "targets": ["mart.t"],
            "examples": [
                {
                    "stmt": "stmt1",
                    "consumer": "ROOT.c",
                    "expression": "`a`.`dt`",
                }
            ],
        }
    ]
    assert summary["star_final_affected"][0]["affected_columns"] == ["a_star"]
