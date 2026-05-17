import json

from lineage_parser.cli import main


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
    out_dir = tmp_path / "out"

    code = main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--schema",
        str(schema_path),
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
            }
        },
        "output_tables": {
            "mart.user_snapshot": {
                "column_details": [
                    {"name": "id", "type": None, "comment": None},
                    {"name": "country", "type": None, "comment": None},
                ],
                "metadata_complete": False,
            }
        },
    }
    profile = json.loads((out_dir / "demo" / "profile.json").read_text(encoding="utf-8"))
    assert profile["related_metadata"] == data["related_metadata"]
    assert (out_dir / "demo" / "views" / "scope_overview.mmd").exists()
    assert (out_dir / "demo" / "report.html").exists()
