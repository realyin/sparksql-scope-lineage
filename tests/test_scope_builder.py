"""Tests for scope_builder: scope tree construction, ID assignment, and ScopeData stubs."""

import pytest

from lineage_parser.scope_builder import parse_scope_lineage


class TestSimpleInsert:
    """Simple INSERT SELECT with no CTE/UNION."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.name FROM ods.src a",
            "test_simple",
        )

    def test_scope_ids(self):
        assert list(self.result.scopes.keys()) == ["ROOT"]

    def test_kind(self):
        assert self.result.scopes["ROOT"].kind == "root"

    def test_stmt_kind(self):
        assert self.result.stmt_kind == "INSERT"

    def test_target_table(self):
        assert self.result.target_table == "dwd.t"

    def test_source_tables(self):
        assert self.result.source_tables == ["ods.src"]

    def test_graph_nodes_include_physical(self):
        assert "ods.src" in self.result.scope_graph.nodes
        assert "ROOT" in self.result.scope_graph.nodes

    def test_physical_not_in_scopes(self):
        """Physical tables are in graph nodes but NOT in scopes dict."""
        assert "ods.src" not in self.result.scopes


class TestCTE:
    """INSERT with a CTE."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t WITH cte AS (SELECT t1.id FROM ods.t1 t1) SELECT a.id FROM cte a",
            "test_cte",
        )

    def test_scope_ids(self):
        assert set(self.result.scopes.keys()) == {"ROOT", "cte:cte"}

    def test_cte_kind(self):
        assert self.result.scopes["cte:cte"].kind == "cte"

    def test_cte_alias(self):
        assert self.result.scopes["cte:cte"].alias_in_parent == "cte"

    def test_root_writes_to(self):
        assert self.result.scopes["ROOT"].writes_to == "dwd.t"

    def test_source_tables(self):
        assert self.result.source_tables == ["ods.t1"]


class TestUnionAll:
    """INSERT OVERWRITE with UNION ALL."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT OVERWRITE TABLE dwd.t SELECT a.id FROM ods.t1 a UNION ALL SELECT b.id FROM ods.t2 b",
            "test_union",
        )

    def test_scope_ids(self):
        assert set(self.result.scopes.keys()) == {
            "ROOT", "union:main", "union:main:b01", "union:main:b02"
        }

    def test_union_scope_kind(self):
        assert self.result.scopes["union:main"].kind == "union"

    def test_union_set_op(self):
        assert self.result.scopes["union:main"].set_op == "UNION_ALL"

    def test_union_branches(self):
        assert self.result.scopes["union:main"].branches == [
            "union:main:b01", "union:main:b02"
        ]

    def test_branch_kind(self):
        assert self.result.scopes["union:main:b01"].kind == "union_branch"
        assert self.result.scopes["union:main:b02"].kind == "union_branch"

    def test_branch_index(self):
        assert self.result.scopes["union:main:b01"].branch_index == 0
        assert self.result.scopes["union:main:b02"].branch_index == 1

    def test_stmt_kind(self):
        assert self.result.stmt_kind == "INSERT_OVERWRITE"

    def test_source_tables(self):
        assert set(self.result.source_tables) == {"ods.t1", "ods.t2"}


class TestSubquery:
    """INSERT with a subquery in FROM."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT sq.id FROM (SELECT a.id FROM ods.t1 a) sq",
            "test_subq",
        )

    def test_scope_ids(self):
        assert set(self.result.scopes.keys()) == {"ROOT", "subq:sq"}

    def test_subquery_kind(self):
        assert self.result.scopes["subq:sq"].kind == "subquery"

    def test_subquery_alias(self):
        assert self.result.scopes["subq:sq"].alias_in_parent == "sq"

    def test_source_tables(self):
        assert self.result.source_tables == ["ods.t1"]


class TestMerge:
    """MERGE INTO with USING subquery."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "MERGE INTO dwd.t USING (SELECT a.id FROM ods.src a) s "
            "ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.name = s.name",
            "test_merge",
        )

    def test_stmt_kind(self):
        assert self.result.stmt_kind == "MERGE"

    def test_target_table(self):
        assert self.result.target_table == "dwd.t"

    def test_has_using_scope(self):
        assert "subq:s" in self.result.scopes

    def test_using_scope_kind(self):
        assert self.result.scopes["subq:s"].kind == "subquery"

    def test_source_tables(self):
        assert self.result.source_tables == ["ods.src"]


class TestCteReferencesCte:
    """Two CTEs where b references a."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH a AS (SELECT t1.id FROM ods.src t1), "
            "b AS (SELECT a.id FROM a) "
            "SELECT b.id FROM b",
            "test_cte_chain",
        )

    def test_scope_ids(self):
        assert set(self.result.scopes.keys()) == {"ROOT", "cte:a", "cte:b"}

    def test_cte_kinds(self):
        assert self.result.scopes["cte:a"].kind == "cte"
        assert self.result.scopes["cte:b"].kind == "cte"

    def test_cte_aliases(self):
        assert self.result.scopes["cte:a"].alias_in_parent == "a"
        assert self.result.scopes["cte:b"].alias_in_parent == "b"

    def test_source_tables(self):
        assert self.result.source_tables == ["ods.src"]

    def test_depends_on_populated(self):
        """depends_on is populated by the resolver after column resolution."""
        assert "cte:a" in self.result.scopes["cte:b"].depends_on


class TestNestedUnion:
    """UNION inside a CTE."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS (SELECT a.id FROM ods.t1 a UNION ALL SELECT b.id FROM ods.t2 b) "
            "SELECT c.id FROM cte c",
            "test_nested_union",
        )

    def test_scope_ids(self):
        expected = {"ROOT", "cte:cte", "union:cte", "union:cte:b01", "union:cte:b02"}
        assert set(self.result.scopes.keys()) == expected

    def test_union_context_derived_from_cte(self):
        """Union scope ID uses CTE name as context, not 'main'."""
        assert "union:cte" in self.result.scopes

    def test_union_branches_in_cte(self):
        assert self.result.scopes["union:cte"].branches == [
            "union:cte:b01", "union:cte:b02"
        ]

    def test_source_tables(self):
        assert set(self.result.source_tables) == {"ods.t1", "ods.t2"}


class TestMultiInsert:
    """parse_all_scope_lineage returns one result per INSERT."""

    def test_two_inserts_return_two_results(self):
        from lineage_parser import parse_all_scope_lineage
        sql = """
        INSERT OVERWRITE spark_catalog.dwd.t1 PARTITION(dt='2026-01-01')
        SELECT a.col1, a.col2 FROM ods.src a;
        INSERT INTO spark_catalog.dwd.t2 PARTITION(dt='2026-01-01')
        SELECT a.col3 FROM ods.src a
        """
        results = parse_all_scope_lineage(sql, "test_multi")
        assert len(results) == 2

    def test_targets_are_distinct(self):
        from lineage_parser import parse_all_scope_lineage
        sql = """
        INSERT OVERWRITE spark_catalog.dwd.t1 PARTITION(dt='2026-01-01')
        SELECT a.col1 FROM ods.src a;
        INSERT INTO spark_catalog.dwd.t2 PARTITION(dt='2026-01-01')
        SELECT a.col2 FROM ods.src a
        """
        results = parse_all_scope_lineage(sql, "test_multi")
        targets = [r.target_table for r in results]
        assert "t1" in targets[0]
        assert "t2" in targets[1]

    def test_single_insert_returns_one_result(self):
        from lineage_parser import parse_all_scope_lineage
        sql = "INSERT INTO spark_catalog.dwd.t1 SELECT a.col1 FROM ods.src a"
        results = parse_all_scope_lineage(sql, "test_single")
        assert len(results) == 1

    def test_each_result_has_columns(self):
        from lineage_parser import parse_all_scope_lineage
        sql = """
        INSERT OVERWRITE spark_catalog.dwd.t1 PARTITION(dt='2026-01-01')
        SELECT a.col1, a.col2 FROM ods.src a;
        INSERT INTO spark_catalog.dwd.t2 PARTITION(dt='2026-01-01')
        SELECT a.col3 FROM ods.src a
        """
        results = parse_all_scope_lineage(sql, "test_multi")
        assert len(results[0].scopes["ROOT"].columns) == 2
        assert len(results[1].scopes["ROOT"].columns) == 1

    def test_error_in_one_insert_does_not_abort_others(self):
        from unittest.mock import patch
        from lineage_parser import parse_all_scope_lineage
        import lineage_parser.scope_builder as sb

        sql = """
        INSERT INTO spark_catalog.dwd.t1 SELECT a.col1 FROM ods.src a;
        INSERT INTO spark_catalog.dwd.t2 SELECT a.col2 FROM ods.src a
        """
        original = sb._build_insert_scope
        call_count = [0]

        def fail_first(tree, task_name, schema=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated parse failure")
            return original(tree, task_name, schema)

        with patch.object(sb, "_build_insert_scope", fail_first):
            results = parse_all_scope_lineage(sql, "test_multi_err")

        assert len(results) == 2
        # First result should have a LINEAGE_ERROR warning
        assert any(w.type == "LINEAGE_ERROR" for w in results[0].diagnostics.warnings)
        # Second result should be normal
        assert results[1].scopes.get("ROOT") is not None


class TestSameTableInUnion:
    """Same physical table in multiple UNION branches produces one physical node."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id FROM ods.t1 a WHERE dt=1 "
            "UNION ALL SELECT b.id FROM ods.t1 b WHERE dt=2",
            "test_dedup",
        )

    def test_single_physical_table(self):
        assert self.result.source_tables == ["ods.t1"]

    def test_single_physical_node(self):
        physical_in_nodes = [n for n in self.result.scope_graph.nodes if n == "ods.t1"]
        assert len(physical_in_nodes) == 1


class TestLateralView:
    """LATERAL VIEW posexplode creates a UDTF scope."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, e.pos, e.val "
            "FROM ods.t1 a LATERAL VIEW posexplode(a.arr) e AS pos, val",
            "test_lateral",
        )

    def test_has_udtf_scope(self):
        assert "udtf:e" in self.result.scopes

    def test_udtf_kind(self):
        assert self.result.scopes["udtf:e"].kind == "subquery"

    def test_source_tables(self):
        assert self.result.source_tables == ["ods.t1"]


class TestUnionChainFlattening:
    """Bug 1 regression: A UNION ALL B UNION ALL C must produce 1 union scope + 3 branches,
    not nested union scopes."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT a.id FROM ods.t1 a "
            "UNION ALL SELECT b.id FROM ods.t2 b "
            "UNION ALL SELECT c.id FROM ods.t3 c",
            "test_union_chain",
        )

    def test_flat_union_scope(self):
        """Exactly one union:main scope, not nested."""
        union_scopes = [sid for sid in self.result.scopes if sid.startswith("union:")]
        assert "union:main" in union_scopes
        # No nested union scopes like union:main:b01:b01
        nested = [sid for sid in union_scopes if sid.count(":") > 2]
        assert nested == [], f"Found nested union scopes: {nested}"

    def test_three_branches(self):
        assert self.result.scopes["union:main"].branches == [
            "union:main:b01", "union:main:b02", "union:main:b03"
        ]

    def test_no_orphan_tmp_ids(self):
        tmp = [sid for sid in self.result.scopes if "_union_tmp_" in sid]
        assert tmp == []


class TestUnionChainFlatteningInCTE:
    """Bug 1 regression: 4-branch UNION inside CTE flattened correctly.
    Also covers Bug 2 (CTE-with-UNION body columns) and Bug 3 (union→cte edge)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS ("
            "  SELECT a.id FROM ods.t1 a "
            "  UNION ALL SELECT b.id FROM ods.t2 b "
            "  UNION ALL SELECT c.id FROM ods.t3 c "
            "  UNION ALL SELECT d.id FROM ods.t4 d"
            ") SELECT e.id FROM cte e",
            "test_union_chain_cte",
        )

    def test_flat_union_in_cte(self):
        assert "union:cte" in self.result.scopes
        assert self.result.scopes["union:cte"].branches == [
            "union:cte:b01", "union:cte:b02", "union:cte:b03", "union:cte:b04"
        ]

    def test_cte_has_columns(self):
        """Bug 2 regression: CTE-with-UNION body has columns."""
        assert len(self.result.scopes["cte:cte"].columns) > 0

    def test_union_to_cte_edge(self):
        """Bug 3 regression: union→cte edge exists in scope_graph."""
        edges = [(e.from_, e.to) for e in self.result.scope_graph.edges]
        assert ("union:cte", "cte:cte") in edges
