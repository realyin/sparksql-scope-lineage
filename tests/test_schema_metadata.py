import json

from lineage_parser.schema_metadata import (
    DictSchemaProvider,
    attach_table_metadata,
    column_details_for_table,
    load_schema,
    load_table_metadata,
    materialize_schema,
    normalize_table_name,
    table_details_for_table,
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


def test_load_table_metadata_csv_and_attach_to_schema(tmp_path):
    schema_path = tmp_path / "columns.csv"
    schema_path.write_text(
        "table_name,column_name,column_type,column_comment\n"
        "hw_jhy_iceberg.ods.users,id,bigint,用户ID\n",
        encoding="utf-8",
    )
    table_path = tmp_path / "tables.csv"
    table_path.write_text(
        "table_name,table_name_cn,table_desc,table_label_layer\n"
        "hw_jhy_iceberg.ods.users,用户表,用户基础信息表,ODS\n",
        encoding="utf-8",
    )

    schema = attach_table_metadata(load_schema(schema_path), load_table_metadata(table_path))

    assert schema == {"ods.users": ["id"]}
    assert column_details_for_table(schema, "ods.users") == [
        {"name": "id", "type": "bigint", "comment": "用户ID"}
    ]
    assert table_details_for_table(schema, "ods.users") == {
        "table_name_cn": "用户表",
        "table_desc": "用户基础信息表",
        "table_label_layer": "ODS",
    }
