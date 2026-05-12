import json

from lineage_parser.html_report import render_html, write_html_report, write_html_report_from_dir
from lineage_parser.scope_builder import parse_scope_lineage
from lineage_parser.scope_serializer import to_dict


def test_render_html_is_self_contained_and_contains_core_ui():
    result = parse_scope_lineage(
        "INSERT OVERWRITE TABLE mart.t "
        "WITH c AS (SELECT id, amount FROM ods.src) "
        "SELECT id, amount FROM c",
        "html_demo",
    )
    data = to_dict(result)

    html = render_html(data)

    assert "Scope DAG" in html
    assert "ROOT Columns" in html
    assert "Focused Column Lineage" in html
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src=" not in html
    assert "<link href=" not in html


def test_write_html_report_outputs_report_file(tmp_path):
    result = parse_scope_lineage(
        "INSERT OVERWRITE TABLE mart.t SELECT id FROM ods.src",
        "html_file",
    )

    path = write_html_report(result, tmp_path)

    assert path == tmp_path / "report.html"
    text = path.read_text(encoding="utf-8")
    assert "html_file" in text
    assert "mart.t" in text


def test_write_html_report_from_existing_output_dir(tmp_path):
    result = parse_scope_lineage(
        "INSERT OVERWRITE TABLE mart.t SELECT id FROM ods.src",
        "html_existing",
    )
    out_dir = tmp_path / "stmt"
    write_html_report(result, out_dir)
    lineage = {
        "task_id": result.task_id,
        "target_table": result.target_table,
        "source_tables": result.source_tables,
        "scope_graph": to_dict(result.scope_graph),
        "scopes": {},
        "diagnostics": {"warnings": [{"type": "demo", "scope": "ROOT", "msg": "x"}]},
    }
    (out_dir / "lineage.json").write_text(json.dumps(lineage), encoding="utf-8")
    (out_dir / "diagnostics.json").write_text(
        json.dumps({"warnings": [{"type": "demo", "scope": "ROOT", "msg": "x"}]}),
        encoding="utf-8",
    )

    path = write_html_report_from_dir(out_dir)

    assert path.exists()
    assert "Diagnostics" in path.read_text(encoding="utf-8")
