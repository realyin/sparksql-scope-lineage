"""Tests for scope_serializer cross-reference validation."""

import pytest
from lineage_parser import parse_scope_lineage
from lineage_parser.scope_serializer import to_dict, validate_cross_references


class TestCrossReferenceValidation:
    def test_valid_result_passes(self):
        sql = """
        WITH cte AS (SELECT a.col1, a.col2 FROM ods.src a)
        INSERT INTO spark_catalog.dwd.t1
        SELECT c.col1, c.col2 FROM cte c
        """
        result = parse_scope_lineage(sql, "test_xref")
        data = to_dict(result)
        errors = validate_cross_references(data)
        assert errors == []

    def test_dangling_edge_detected(self):
        sql = "INSERT INTO spark_catalog.dwd.t1 SELECT a.col1 FROM ods.src a"
        result = parse_scope_lineage(sql, "test_dangling")
        data = to_dict(result)
        # Inject a bad edge manually
        data["scope_graph"]["edges"].append({"from": "nonexistent_scope", "to": "ROOT"})
        errors = validate_cross_references(data)
        assert any("nonexistent_scope" in e for e in errors)

    def test_dangling_source_ref_detected(self):
        sql = "INSERT INTO spark_catalog.dwd.t1 SELECT a.col1 FROM ods.src a"
        result = parse_scope_lineage(sql, "test_dangling_src")
        data = to_dict(result)
        # Inject a bad source ref in ROOT column
        data["scopes"]["ROOT"]["columns"][0]["sources"].append(
            {"scope": "ghost_scope", "column": "col1"}
        )
        errors = validate_cross_references(data)
        assert any("ghost_scope" in e for e in errors)

    def test_write_output_raises_on_cross_ref_failure(self, tmp_path):
        from lineage_parser.scope_serializer import write_output
        from lineage_parser.scope_types import ScopeGraphEdge
        sql = "INSERT INTO spark_catalog.dwd.t1 SELECT a.col1 FROM ods.src a"
        result = parse_scope_lineage(sql, "test_write_xref")
        # Inject a bad edge into the result object before writing
        result.scope_graph.edges.append(ScopeGraphEdge(from_="phantom", to="ROOT"))
        with pytest.raises(ValueError, match="phantom"):
            write_output(result, tmp_path)
