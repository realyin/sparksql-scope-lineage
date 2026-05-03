import pytest
from lineage_parser import parse_scope_lineage


DUPLICATE_ALIAS_SQL = """
INSERT INTO target.tbl
SELECT a.id, f.name, g.code
FROM src.a AS a
LEFT JOIN (SELECT id, name FROM src.b) f ON a.id = f.id
LEFT JOIN (SELECT id, code FROM src.c) f ON a.id = f.id
"""


def test_duplicate_subquery_alias_does_not_crash():
    """Parser must not raise on duplicate subquery alias."""
    result = parse_scope_lineage(DUPLICATE_ALIAS_SQL, "dup_alias_test")
    assert result is not None
    warning_types = [w.type for w in result.diagnostics.warnings]
    assert "duplicate_alias" in warning_types


def test_duplicate_alias_columns_are_unknown_or_resolved():
    result = parse_scope_lineage(DUPLICATE_ALIAS_SQL, "dup_alias_test2")
    root_cols = {c.name: c for c in result.scopes["ROOT"].columns}
    # 'id' comes from src.a (unambiguous) — must resolve
    assert "id" in root_cols
    # 'name' or 'code' come through the ambiguous alias 'f' — must not crash,
    # and at least one of them should be present (possibly with UNKNOWN source)
    assert "name" in root_cols or "code" in root_cols


SIMPLE_CTE_SQL = """
INSERT INTO target.result_tbl
WITH cte1 AS (SELECT id, name FROM src.a),
     cte2 AS (SELECT id, SUM(val) AS total FROM src.b GROUP BY id)
SELECT c1.id, c1.name, c2.total
FROM cte1 c1
JOIN cte2 c2 ON c1.id = c2.id
"""


@pytest.fixture(scope="module")
def simple_cte_result():
    return parse_scope_lineage(SIMPLE_CTE_SQL, "stats_test")


def test_stats_scope_count(simple_cte_result):
    assert simple_cte_result.diagnostics.stats["scope_count"] >= 3


def test_stats_cte_count(simple_cte_result):
    assert simple_cte_result.diagnostics.stats["cte_count"] == 2


def test_stats_aggregate_count(simple_cte_result):
    assert simple_cte_result.diagnostics.stats["aggregate_function_count"] >= 1


def test_stats_join_count(simple_cte_result):
    assert simple_cte_result.diagnostics.stats["join_count"] >= 1


DEDUP_SQL = """
INSERT INTO t.out_tbl
WITH rn_cte AS (
  SELECT id, name, ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) AS rn
  FROM s.a
)
SELECT id, name FROM rn_cte WHERE rn = 1
"""

AGG_SQL = """
INSERT INTO t.out_tbl
SELECT dept_id, SUM(salary) AS total_salary FROM s.emp GROUP BY dept_id
"""

JOIN_SQL = """
INSERT INTO t.out_tbl
SELECT a.id, b.name FROM s.a a JOIN s.b b ON a.id = b.id
"""

FILTER_SQL = """
INSERT INTO t.out_tbl
SELECT id, name FROM s.a WHERE status_col = 'active'
"""


def test_role_aggregate_scope():
    result = parse_scope_lineage(AGG_SQL, "agg_role")
    root = result.scopes.get("ROOT")
    assert root is not None
    assert root.role == "aggregate"


def test_role_join_scope():
    result = parse_scope_lineage(JOIN_SQL, "join_role")
    root = result.scopes.get("ROOT")
    assert root is not None
    assert root.role == "join"


def test_role_filter_scope():
    result = parse_scope_lineage(FILTER_SQL, "filter_role")
    root = result.scopes.get("ROOT")
    assert root is not None
    assert root.role == "filter"


def test_role_dedup_cte():
    result = parse_scope_lineage(DEDUP_SQL, "dedup_role")
    cte = result.scopes.get("cte:rn_cte")
    assert cte is not None
    assert cte.role == "dedup"


def test_all_scopes_have_role():
    """Every scope must have a non-None role after inference."""
    for sql, name in [(AGG_SQL, "agg"), (JOIN_SQL, "join"), (FILTER_SQL, "fil"), (DEDUP_SQL, "ded")]:
        result = parse_scope_lineage(sql, name)
        for scope_id, scope_data in result.scopes.items():
            assert scope_data.role is not None, f"scope {scope_id} has no role"


UNION_SQL = """
INSERT INTO t.out_tbl
SELECT id FROM s.a WHERE flag = 1
UNION ALL
SELECT id FROM s.b WHERE flag = 1
"""


def test_role_union_scopes():
    result = parse_scope_lineage(UNION_SQL, "union_role")
    for scope_id, scope_data in result.scopes.items():
        if scope_data.kind == "union":
            assert scope_data.role == "union", f"Expected union role for {scope_id}"
        elif scope_data.kind == "union_branch":
            assert scope_data.role == "union_branch", f"Expected union_branch role for {scope_id}"
