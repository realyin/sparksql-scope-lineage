# tests/test_schema_validation.py
import json
import pytest
from lineage_parser import parse_scope_lineage
from lineage_parser.scope_serializer import validate_lineage_json, _load_schema, write_output


SIMPLE_SQL = """
INSERT INTO t.out_tbl
SELECT id, name FROM s.a
"""


def test_valid_result_passes_schema():
    r = parse_scope_lineage(SIMPLE_SQL, "schema_test")
    d = validate_lineage_json(r)   # must not raise
    assert d["task_id"] == "schema_test"


def test_write_output_creates_valid_json(tmp_path):
    """write_output writes a file that passes schema."""
    r = parse_scope_lineage(SIMPLE_SQL, "schema_write_test")
    out = write_output(r, tmp_path)
    with open(out / "lineage.json", encoding="utf-8") as f:
        data = json.load(f)
    assert data["task_id"] == "schema_write_test"
    assert data["target_table"] != ""


def test_write_output_raises_on_schema_violation(tmp_path, monkeypatch):
    """write_output must raise ValidationError when output violates schema."""
    import jsonschema
    from lineage_parser import scope_serializer as ser

    r = parse_scope_lineage(SIMPLE_SQL, "bad_schema_test")
    # Force to_dict to return something schema-invalid (missing required fields)
    monkeypatch.setattr(ser, "to_dict", lambda _: {"broken": True})
    with pytest.raises(jsonschema.ValidationError):
        write_output(r, tmp_path)


def test_schema_rejects_missing_task_id():
    import jsonschema
    schema = _load_schema()
    bad = {"target_table": "t.out_tbl", "scopes": {}, "scope_graph": {"nodes": [], "edges": []}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
