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
        "table_name,column_name\n"
        "ods.users,id\n"
        "ods.users,country\n",
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
    ])

    assert code == 0
    lineage_path = out_dir / "demo" / "lineage.json"
    assert lineage_path.exists()
    data = json.loads(lineage_path.read_text(encoding="utf-8"))
    assert [c["name"] for c in data["scopes"]["ROOT"]["columns"]] == ["id", "country"]
    assert (out_dir / "demo" / "views" / "scope_overview.mmd").exists()
