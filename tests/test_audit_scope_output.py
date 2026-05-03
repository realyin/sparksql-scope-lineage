import importlib.util
import json
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "audit_scope_output", TOOLS_DIR / "audit_scope_output.py"
)
audit_scope_output = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = audit_scope_output
SPEC.loader.exec_module(audit_scope_output)


def _write_stmt(out_dir: Path, name: str, lineage: dict, diagnostics: dict | None = None) -> None:
    stmt_dir = out_dir / name
    (stmt_dir / "views" / "per_column").mkdir(parents=True)
    (stmt_dir / "lineage.json").write_text(
        json.dumps(lineage, ensure_ascii=False),
        encoding="utf-8",
    )
    (stmt_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics or {"warnings": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (stmt_dir / "views" / "scope_overview.mmd").write_text("graph TD\n", encoding="utf-8")
    (stmt_dir / "views" / "field_lineage.mmd").write_text(
        "graph TD\n  a --> b\n",
        encoding="utf-8",
    )
    (stmt_dir / "views" / "per_column" / "id.mmd").write_text(
        "graph TD\n  a --> b\n",
        encoding="utf-8",
    )


def test_audit_output_classifies_red_and_yellow(tmp_path):
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "bad.json").write_text("{}", encoding="utf-8")
    (task_dir / "yellow.json").write_text("{}", encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_stmt(
        out_dir,
        "bad",
        {
            "target_table": "dwd.bad",
            "source_tables": ["ods.src"],
            "scope_graph": {"nodes": ["ROOT", "subq:a", "ods.src"], "edges": []},
            "scopes": {
                "ROOT": {
                    "columns": [
                        {
                            "name": "id",
                            "expression": "a.id",
                            "sources": [{"scope": "subq:a", "column": "id"}],
                        }
                    ]
                },
                "subq:a": {"columns": [{"name": "other", "sources": []}]},
            },
        },
    )
    _write_stmt(
        out_dir,
        "yellow",
        {
            "target_table": "dwd.yellow",
            "source_tables": ["ods.src"],
            "scope_graph": {"nodes": ["ROOT", "ods.src"], "edges": []},
            "scopes": {
                "ROOT": {
                    "columns": [
                        {
                            "name": "*",
                            "transform": "EXPAND_ALL",
                            "expression": "*",
                            "sources": [{"scope": "ods.src", "column": "*"}],
                        }
                    ]
                }
            },
        },
        {"warnings": [{"type": "star_not_expanded", "scope": "ROOT", "msg": "x"}]},
    )

    result = audit_scope_output.audit_output(task_dir, out_dir)

    assert result.source_task_count == 2
    assert result.statement_count == 2
    assert result.severity_counts["RED"] == 1
    assert result.severity_counts["YELLOW"] == 1
    assert result.issue_counts["dangling_column_ref"] == 1
    assert result.warning_counts["star_not_expanded"] == 1


def test_render_markdown_and_json_summary(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_stmt(
        out_dir,
        "ok",
        {
            "target_table": "dwd.ok",
            "source_tables": ["ods.src"],
            "scope_graph": {"nodes": ["ROOT", "ods.src"], "edges": []},
            "scopes": {
                "ROOT": {
                    "columns": [
                        {
                            "name": "id",
                            "expression": "id",
                            "sources": [{"scope": "ods.src", "column": "id"}],
                        }
                    ]
                }
            },
        },
    )

    result = audit_scope_output.audit_output(None, out_dir)
    markdown = audit_scope_output.render_markdown(result, "测试报告")
    payload = audit_scope_output.to_jsonable(result)

    assert "# 测试报告" in markdown
    assert "RED | 0" in markdown
    assert payload["statement_count"] == 1
    assert payload["severity_counts"]["GREEN"] == 1


def test_schema_expanded_scope_missing_column_is_yellow_metadata_gap(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_stmt(
        out_dir,
        "schema_gap",
        {
            "target_table": "dwd.t",
            "source_tables": ["ods.src"],
            "scope_graph": {"nodes": ["ROOT", "subq:s", "ods.src"], "edges": []},
            "scopes": {
                "ROOT": {
                    "columns": [
                        {
                            "name": "missing_col",
                            "expression": "s.missing_col",
                            "sources": [{"scope": "subq:s", "column": "missing_col"}],
                        }
                    ]
                },
                "subq:s": {
                    "columns": [
                        {
                            "name": "id",
                            "transform": "DIRECT",
                            "expression": "id",
                            "sources": [{"scope": "ods.src", "column": "id"}],
                        },
                        {
                            "name": "name",
                            "transform": "DIRECT",
                            "expression": "name",
                            "sources": [{"scope": "ods.src", "column": "name"}],
                        },
                        {
                            "name": "dt",
                            "transform": "DIRECT",
                            "expression": "dt",
                            "sources": [{"scope": "ods.src", "column": "dt"}],
                        },
                    ]
                },
            },
        },
    )

    result = audit_scope_output.audit_output(None, out_dir)

    assert result.severity_counts["RED"] == 0
    assert result.severity_counts["YELLOW"] == 1
    assert result.issue_counts["schema_incomplete_column_ref"] == 1
    assert result.issue_counts["dangling_column_ref"] == 0


def test_main_writes_report_json_and_fails_on_red(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_stmt(
        out_dir,
        "bad",
        {
            "target_table": "dwd.bad",
            "source_tables": ["ods.src"],
            "scope_graph": {"nodes": ["ROOT", "subq:a", "ods.src"], "edges": []},
            "scopes": {
                "ROOT": {
                    "columns": [
                        {
                            "name": "id",
                            "expression": "a.id",
                            "sources": [{"scope": "subq:a", "column": "id"}],
                        }
                    ]
                },
                "subq:a": {"columns": []},
            },
        },
    )
    report = tmp_path / "audit.md"
    json_path = tmp_path / "audit.json"

    code = audit_scope_output.main([
        "--out-dir",
        str(out_dir),
        "--report",
        str(report),
        "--json",
        str(json_path),
        "--fail-on-red",
    ])

    assert code == 1
    assert "RED" in report.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["severity_counts"]["RED"] == 1
