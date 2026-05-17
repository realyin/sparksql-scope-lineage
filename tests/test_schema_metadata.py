import json

from lineage_parser.schema_metadata import (
    DictSchemaProvider,
    column_details_for_table,
    load_schema,
    materialize_schema,
    normalize_table_name,
)


def test_load_schema_json_mapping_shape(tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({
            "spark_catalog.ods.src": [
                {"name": "id"},
                {"column_name": "name"},
            ]
        }),
        encoding="utf-8",
    )

    schema = load_schema(schema_path)

    assert schema == {"ods.src": ["id", "name"]}
    assert column_details_for_table(schema, "ods.src") == [
        {"name": "id", "type": None, "comment": None},
        {"name": "name", "type": None, "comment": None},
    ]


def test_load_schema_csv_rows(tmp_path):
    schema_path = tmp_path / "schema.csv"
    schema_path.write_text(
        "table_name,column_name\n"
        "spark_catalog.ods.src,id\n"
        "ods.src,name\n",
        encoding="utf-8",
    )

    schema = load_schema(schema_path)

    assert schema == {"ods.src": ["id", "name"]}


def test_load_schema_preserves_column_details_from_csv(tmp_path):
    schema_path = tmp_path / "schema.csv"
    schema_path.write_text(
        "table_name,column_name,type,comment\n"
        "spark_catalog.ods.src,id,bigint,用户ID\n"
        "ods.src,dt,date,\n",
        encoding="utf-8",
    )

    schema = load_schema(schema_path)

    assert schema == {"ods.src": ["id", "dt"]}
    assert column_details_for_table(schema, "spark_catalog.ods.src") == [
        {"name": "id", "type": "bigint", "comment": "用户ID"},
        {"name": "dt", "type": "date", "comment": None},
    ]


def test_load_schema_preserves_column_details_from_json(tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({
            "report_csc_ana.hotline_detail_realtime": {
                "column_details": [
                    {"name": "dt", "type": "date", "comment": None},
                    {"name": "call_id", "type": "string", "comment": "拨打编号"},
                ]
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    schema = load_schema(schema_path)

    assert schema == {"report_csc_ana.hotline_detail_realtime": ["dt", "call_id"]}
    assert column_details_for_table(schema, "report_csc_ana.hotline_detail_realtime") == [
        {"name": "dt", "type": "date", "comment": None},
        {"name": "call_id", "type": "string", "comment": "拨打编号"},
    ]


def test_materialize_schema_from_mock_provider():
    provider = DictSchemaProvider({"spark_catalog.ods.src": ["id", "name"]})

    schema = materialize_schema(provider, ["ods.src", "ods.missing"])

    assert normalize_table_name("spark_catalog.ods.src") == "ods.src"
    assert schema == {"ods.src": ["id", "name"]}
