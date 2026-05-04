from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "compare_scope_outputs", TOOLS_DIR / "compare_scope_outputs.py"
)
compare_scope_outputs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = compare_scope_outputs
SPEC.loader.exec_module(compare_scope_outputs)


def _write_stmt(root: Path, stmt: str, *, target: str = "mart.t", column: str = "id") -> None:
    stmt_dir = root / stmt
    (stmt_dir / "views" / "per_column").mkdir(parents=True)
    lineage = {
        "target_table": target,
        "source_tables": ["ods.src"],
        "scope_graph": {"nodes": ["ROOT", "ods.src"], "edges": []},
        "scopes": {
            "ROOT": {
                "columns": [
                    {
                        "name": column,
                        "expression": column,
                        "sources": [{"scope": "ods.src", "column": column}],
                    }
                ]
            }
        },
    }
    diagnostics = {"warnings": []}
    (stmt_dir / "lineage.json").write_text(json.dumps(lineage), encoding="utf-8")
    (stmt_dir / "diagnostics.json").write_text(json.dumps(diagnostics), encoding="utf-8")
    (stmt_dir / "views" / "scope_overview.mmd").write_text("graph TD\n", encoding="utf-8")
    (stmt_dir / "views" / "field_lineage.mmd").write_text("graph TD\n  a --> b\n", encoding="utf-8")
    (stmt_dir / "views" / "physical.mmd").write_text("graph TD\n  src --> dst\n", encoding="utf-8")


def test_compare_outputs_matches_identical_directories(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_stmt(left, "stmt1")
    _write_stmt(right, "stmt1")

    result = compare_scope_outputs.compare_outputs(left, right)

    assert not result.has_diff
    assert result.left_statements == 1
    assert result.right_statements == 1
    assert result.compared_files == 5
    assert "MATCH" in compare_scope_outputs.render_markdown(result)


def test_compare_outputs_detects_file_and_statement_diffs(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_stmt(left, "stmt1", column="id")
    _write_stmt(right, "stmt1", column="user_id")
    _write_stmt(left, "left_only")

    result = compare_scope_outputs.compare_outputs(left, right)
    payload = compare_scope_outputs.to_jsonable(result)

    assert result.has_diff
    assert result.missing_right == ["left_only"]
    assert any(diff.path == "lineage.json" for diff in result.file_diffs)
    assert payload["has_diff"] is True


def test_compare_main_writes_reports_and_fails_on_diff(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_stmt(left, "stmt1", column="id")
    _write_stmt(right, "stmt1", column="user_id")
    report = tmp_path / "compare.md"
    json_path = tmp_path / "compare.json"

    code = compare_scope_outputs.main(
        [
            "--left",
            str(left),
            "--right",
            str(right),
            "--report",
            str(report),
            "--json",
            str(json_path),
            "--fail-on-diff",
        ]
    )

    assert code == 1
    assert "DIFF" in report.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["has_diff"] is True

