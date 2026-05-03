# tests/test_scope_warnings.py
from lineage_parser import parse_scope_lineage


# --- filter_in_join_on_clause ---
FILTER_IN_ON_SQL = """
INSERT INTO t.out_tbl
SELECT a.id, a.val FROM s.a a
LEFT JOIN s.b b ON a.id = b.id AND b.status = 'active'
"""

def test_filter_in_join_on_clause_detected():
    r = parse_scope_lineage(FILTER_IN_ON_SQL, "join_filter")
    types = [w.type for w in r.diagnostics.warnings]
    assert "filter_in_join_on_clause" in types


# --- magic_number ---
MAGIC_NUM_SQL = """
INSERT INTO t.out_tbl
SELECT id, amount * 1.13 AS amount_with_tax FROM s.a
"""

def test_magic_number_detected():
    r = parse_scope_lineage(MAGIC_NUM_SQL, "magic_num")
    types = [w.type for w in r.diagnostics.warnings]
    assert "magic_number" in types


# --- duplicate_table_in_union ---
DUP_UNION_SQL = """
INSERT INTO t.out_tbl
SELECT id FROM s.a WHERE type_col = 'x'
UNION ALL
SELECT id FROM s.a WHERE type_col = 'y'
"""

def test_duplicate_table_in_union_detected():
    r = parse_scope_lineage(DUP_UNION_SQL, "dup_union")
    types = [w.type for w in r.diagnostics.warnings]
    assert "duplicate_table_in_union" in types


# --- complex_aggregate_with_case ---
AGG_CASE_SQL = """
INSERT INTO t.out_tbl
SELECT
  SUM(CASE WHEN status_col = 'ok' THEN amount ELSE 0 END) AS ok_amount
FROM s.a
"""

def test_complex_aggregate_with_case_detected():
    r = parse_scope_lineage(AGG_CASE_SQL, "agg_case")
    types = [w.type for w in r.diagnostics.warnings]
    assert "complex_aggregate_with_case" in types


# --- no false positives on clean SQL ---
CLEAN_SQL = """
INSERT INTO t.out_tbl
SELECT a.id, b.name FROM s.a a JOIN s.b b ON a.id = b.id
"""

def test_no_spurious_warnings_on_clean_sql():
    r = parse_scope_lineage(CLEAN_SQL, "clean")
    semantic_types = {
        "filter_in_join_on_clause", "magic_number",
        "duplicate_table_in_union", "complex_aggregate_with_case",
    }
    actual = {w.type for w in r.diagnostics.warnings}
    assert not (actual & semantic_types), f"Spurious: {actual & semantic_types}"
