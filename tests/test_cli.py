import json

from lineage_parser.cli import main
from lineage_parser.schema_metadata import column_details_for_table, load_schema


def test_cli_parse_writes_outputs(tmp_path):
    sql_path = tmp_path / "demo.sql"
    sql_path.write_text(
        "INSERT OVERWRITE TABLE mart.user_snapshot "
        "SELECT s.* FROM ods.users s",
        encoding="utf-8",
    )
    schema_path = tmp_path / "table_cols.csv"
    schema_path.write_text(
        "table_name,column_name,type,comment\n"
        "ods.users,id,bigint,用户ID\n"
        "ods.users,country,string,国家\n",
        encoding="utf-8",
    )
    table_path = tmp_path / "tables.csv"
    table_path.write_text(
        "table_name,table_name_cn,table_desc,table_label_layer\n"
        "ods.users,用户表,用户基础信息表,ODS\n"
        "mart.user_snapshot,用户快照表,用户快照输出表,ADS\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    code = main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--schema",
        str(schema_path),
        "--table-metadata",
        str(table_path),
        "--out",
        str(out_dir),
        "--md",
        "--html",
    ])

    assert code == 0
    lineage_path = out_dir / "demo" / "lineage.json"
    assert lineage_path.exists()
    data = json.loads(lineage_path.read_text(encoding="utf-8"))
    assert [c["name"] for c in data["scopes"]["ROOT"]["columns"]] == ["id", "country"]
    assert data["related_metadata"] == {
        "input_tables": {
            "ods.users": {
                "column_details": [
                    {"name": "id", "type": "bigint", "comment": "用户ID"},
                    {"name": "country", "type": "string", "comment": "国家"},
                ],
                "metadata_complete": True,
                "table_metadata": {
                    "table_name_cn": "用户表",
                    "table_desc": "用户基础信息表",
                    "table_label_layer": "ODS",
                },
            }
        },
        "output_tables": {
            "mart.user_snapshot": {
                "column_details": [
                    {"name": "id", "type": None, "comment": None},
                    {"name": "country", "type": None, "comment": None},
                ],
                "metadata_complete": False,
                "table_metadata": {
                    "table_name_cn": "用户快照表",
                    "table_desc": "用户快照输出表",
                    "table_label_layer": "ADS",
                },
            }
        },
    }
    profile = json.loads((out_dir / "demo" / "profile.json").read_text(encoding="utf-8"))
    assert profile["related_metadata"] == data["related_metadata"]
    assert (out_dir / "demo" / "views" / "scope_overview.mmd").exists()
    assert (out_dir / "demo" / "report.html").exists()


def test_cli_parse_writes_task_insight_when_requested(tmp_path):
    sql_path = tmp_path / "demo.sql"
    sql_path.write_text(
        "INSERT OVERWRITE TABLE mart.user_snapshot "
        "SELECT id FROM ods.users WHERE dt = '20260515'",
        encoding="utf-8",
    )
    schema_path = tmp_path / "table_cols.csv"
    schema_path.write_text(
        "table_name,column_name,type,comment\n"
        "ods.users,id,bigint,用户ID\n"
        "ods.users,dt,string,日期分区\n"
        "mart.user_snapshot,id,bigint,用户ID\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    code = main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--schema",
        str(schema_path),
        "--out",
        str(out_dir),
        "--insight",
    ])

    assert code == 0
    task_dir = out_dir / "demo"
    insight = json.loads((task_dir / "task_insight.json").read_text(encoding="utf-8"))
    assert insight["task"]["target_table"] == "mart.user_snapshot"
    assert "scope:ROOT" in insight["objects"]["scopes"]
    assert "column:id" in insight["objects"]["columns"]
    assert (task_dir / "task_insight.html").exists()


def test_cli_insight_renders_from_existing_output_dir(tmp_path):
    sql_path = tmp_path / "demo.sql"
    sql_path.write_text(
        "INSERT OVERWRITE TABLE mart.user_snapshot SELECT id FROM ods.users",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    assert main(["parse", "--sql-file", str(sql_path), "--out", str(out_dir)]) == 0

    task_dir = out_dir / "demo"
    assert main(["insight", "--input", str(task_dir)]) == 0

    insight = json.loads((task_dir / "task_insight.json").read_text(encoding="utf-8"))
    assert insight["task"]["target_table"] == "mart.user_snapshot"
    assert (task_dir / "task_insight.html").exists()


def test_schema_csv_accepts_column_type_and_column_comment(tmp_path):
    schema_path = tmp_path / "schema_info.csv"
    schema_path.write_text(
        "table_name,column_name,column_type,column_comment\n"
        "ods.users,id,bigint,用户ID\n"
        "ods.users,country,string,国家\n",
        encoding="utf-8",
    )

    schema = load_schema(schema_path)

    assert schema["ods.users"] == ["id", "country"]
    assert column_details_for_table(schema, "ods.users") == [
        {"name": "id", "type": "bigint", "comment": "用户ID"},
        {"name": "country", "type": "string", "comment": "国家"},
    ]
