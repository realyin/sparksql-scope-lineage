"""Tests for scope_resolver: column resolution, depends_on, scope_graph edges."""

import pytest

from lineage_parser.scope_builder import parse_scope_lineage
from lineage_parser.scope_types import CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID, ScopeColumn, SourceRef
from lineage_parser.scope_views import trace_to_physical


class TestSimpleDirect:
    """Simple INSERT SELECT with DIRECT column references."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.name FROM ods.src a",
            "test_direct",
        )

    def test_root_columns(self):
        root = self.result.scopes["ROOT"]
        assert len(root.columns) == 2
        assert root.columns[0].name == "id"
        assert root.columns[1].name == "name"

    def test_direct_transform(self):
        for col in self.result.scopes["ROOT"].columns:
            assert col.transform == "DIRECT"

    def test_sources_reference_physical_table(self):
        root = self.result.scopes["ROOT"]
        assert root.columns[0].sources == [SourceRef(scope="ods.src", column="id")]
        assert root.columns[1].sources == [SourceRef(scope="ods.src", column="name")]

    def test_root_depends_on(self):
        assert self.result.scopes["ROOT"].depends_on == ["ods.src"]

    def test_graph_edges(self):
        edges = {(e.from_, e.to) for e in self.result.scope_graph.edges}
        assert ("ods.src", "ROOT") in edges


class TestCTEReference:
    """ROOT references CTE, CTE references physical table."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t WITH cte AS (SELECT t1.id FROM ods.t1 t1) SELECT a.id FROM cte a",
            "test_cte_ref",
        )

    def test_root_sources_reference_cte(self):
        root = self.result.scopes["ROOT"]
        assert root.columns[0].sources == [SourceRef(scope="cte:cte", column="id")]

    def test_cte_sources_reference_physical(self):
        cte = self.result.scopes["cte:cte"]
        assert cte.columns[0].sources == [SourceRef(scope="ods.t1", column="id")]

    def test_dependency_chain(self):
        assert self.result.scopes["ROOT"].depends_on == ["cte:cte"]
        assert self.result.scopes["cte:cte"].depends_on == ["ods.t1"]

    def test_graph_edges_chain(self):
        edges = {(e.from_, e.to) for e in self.result.scope_graph.edges}
        assert ("ods.t1", "cte:cte") in edges
        assert ("cte:cte", "ROOT") in edges


class TestUnionResolution:
    """UNION ALL: branches resolve independently, union scope aligns by position."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT OVERWRITE TABLE dwd.t SELECT a.id FROM ods.t1 a "
            "UNION ALL SELECT b.id FROM ods.t2 b",
            "test_union",
        )

    def test_branch_columns(self):
        b01 = self.result.scopes["union:main:b01"]
        b02 = self.result.scopes["union:main:b02"]
        assert b01.columns[0].sources == [SourceRef(scope="ods.t1", column="id")]
        assert b02.columns[0].sources == [SourceRef(scope="ods.t2", column="id")]

    def test_union_scope_columns(self):
        union = self.result.scopes["union:main"]
        assert len(union.columns) == 1
        col = union.columns[0]
        assert col.transform == "UNION"
        assert col.sources == [
            SourceRef(scope="union:main:b01", column="id"),
            SourceRef(scope="union:main:b02", column="id"),
        ]

    def test_union_branches_field(self):
        union = self.result.scopes["union:main"]
        col = union.columns[0]
        assert col.branches == [
            {"branch": "union:main:b01", "from_column": "id"},
            {"branch": "union:main:b02", "from_column": "id"},
        ]

    def test_root_columns_from_union(self):
        root = self.result.scopes["ROOT"]
        assert len(root.columns) == 1
        assert root.columns[0].sources == [SourceRef(scope="union:main", column="id")]

    def test_dependency_chain(self):
        assert self.result.scopes["union:main:b01"].depends_on == ["ods.t1"]
        assert self.result.scopes["union:main:b02"].depends_on == ["ods.t2"]
        assert set(self.result.scopes["union:main"].depends_on) == {
            "union:main:b01", "union:main:b02"
        }
        assert self.result.scopes["ROOT"].depends_on == ["union:main"]

    def test_graph_edges_full_chain(self):
        edges = {(e.from_, e.to) for e in self.result.scope_graph.edges}
        assert ("ods.t1", "union:main:b01") in edges
        assert ("ods.t2", "union:main:b02") in edges
        assert ("union:main:b01", "union:main") in edges
        assert ("union:main:b02", "union:main") in edges
        assert ("union:main", "ROOT") in edges


class TestValuesAndStarExpansion:
    """VALUES aliases and SELECT * should resolve when columns are locally known."""

    def test_values_alias_columns_expand_through_cte_star(self):
        result = parse_scope_lineage(
            "WITH dim AS (SELECT * FROM VALUES ('k1', 'v1') AS tab(k, v)) "
            "INSERT INTO dwd.t SELECT d.k, d.v FROM dim d",
            "test_values",
        )

        assert [c.name for c in result.scopes["udtf:tab"].columns] == ["k", "v"]
        assert [c.name for c in result.scopes["cte:dim"].columns] == ["k", "v"]
        assert result.scopes["ROOT"].columns[0].sources == [
            SourceRef(scope="cte:dim", column="k")
        ]

    def test_values_expression_column_has_literal_source(self):
        result = parse_scope_lineage(
            "WITH dim AS (SELECT * FROM VALUES (MD5(CONCAT_WS(',', 'a', 'b'))) AS tab(k)) "
            "INSERT INTO dwd.t SELECT d.k FROM dim d",
            "test_values_expr_source",
        )

        value_col = result.scopes["udtf:tab"].columns[0]
        assert value_col.transform == "EXPRESSION"
        assert value_col.sources == [SourceRef(scope=CONSTANT_SCOPE_ID, column=value_col.expression)]

    def test_physical_bare_star_expands_with_mock_schema(self):
        result = parse_scope_lineage(
            "INSERT OVERWRITE TABLE dwd.t SELECT * FROM spark_catalog.ods.src",
            "test_physical_star_schema",
            schema={"ods.src": ["id", "name", "dt"]},
        )

        root = result.scopes["ROOT"]
        assert [c.name for c in root.columns] == ["id", "name", "dt"]
        assert root.columns[0].sources == [SourceRef(scope="spark_catalog.ods.src", column="id")]
        assert not [
            w for w in result.diagnostics.warnings
            if w.type == "star_not_expanded"
        ]

    def test_physical_qualified_star_expands_with_mock_schema(self):
        result = parse_scope_lineage(
            "INSERT OVERWRITE TABLE dwd.t SELECT s.* FROM ods.src s",
            "test_physical_qualified_star_schema",
            schema={"ods.src": ["id", "name"]},
        )

        root = result.scopes["ROOT"]
        assert [c.name for c in root.columns] == ["id", "name"]
        assert root.columns[1].sources == [SourceRef(scope="ods.src", column="name")]

    def test_star_expansion_keeps_referenced_partition_column_missing_from_schema(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT t.id, t.dt "
            "FROM (SELECT * FROM ods.src WHERE dt = '20260519') t",
            "test_star_partition_ref",
            schema={"ods.src": ["id", "name"]},
        )

        subq = result.scopes["subq:t"]
        assert [c.name for c in subq.columns] == ["id", "name", "dt"]
        assert subq.columns[-1].sources == [SourceRef(scope="ods.src", column="dt")]
        root_dt = next(c for c in result.scopes["ROOT"].columns if c.name == "dt")
        assert root_dt.sources == [SourceRef(scope="subq:t", column="dt")]

    def test_partial_schema_missing_table_uses_no_schema_unqualified_fallback(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT id FROM ods.missing a JOIN ods.other b ON a.k = b.k",
            "test_partial_schema_unqualified_fallback",
            schema={"ods.unrelated": ["id"]},
        )

        root = result.scopes["ROOT"]
        assert root.columns[0].sources == [SourceRef(scope="ods.missing", column="id")]
        assert [
            w for w in result.diagnostics.warnings
            if w.type == "unresolved_unqualified_no_schema"
        ]

    def test_union_star_branches_use_only_selected_source(self):
        result = parse_scope_lineage(
            "WITH a AS (SELECT x.id FROM ods.a x), "
            "b AS (SELECT y.id FROM ods.b y), "
            "c AS (SELECT z.id FROM ods.c z) "
            "INSERT INTO dwd.t SELECT * FROM a "
            "UNION ALL SELECT * FROM b "
            "UNION ALL SELECT * FROM c",
            "test_union_star",
        )

        assert result.scopes["union:main:b01"].depends_on == ["cte:a"]
        assert result.scopes["union:main:b02"].depends_on == ["cte:b"]
        assert result.scopes["union:main:b03"].depends_on == ["cte:c"]
        assert len(result.scopes["union:main"].columns) == 1


class TestSubqueryReference:
    """Subquery in FROM: ROOT references subq scope."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT sq.id FROM (SELECT a.id FROM ods.t1 a) sq",
            "test_subq",
        )

    def test_root_sources(self):
        root = self.result.scopes["ROOT"]
        assert root.columns[0].sources == [SourceRef(scope="subq:sq", column="id")]

    def test_subq_sources(self):
        subq = self.result.scopes["subq:sq"]
        assert subq.columns[0].sources == [SourceRef(scope="ods.t1", column="id")]


class TestJoinResolution:
    """JOIN: verify left_scope, right_scope, condition_columns."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.name, b.amount "
            "FROM ods.t1 a JOIN ods.t2 b ON a.id = b.id",
            "test_join",
        )

    def test_join_exists(self):
        root = self.result.scopes["ROOT"]
        assert len(root.joins) == 1

    def test_join_type(self):
        join = self.result.scopes["ROOT"].joins[0]
        assert join.join_type == "INNER"

    def test_join_scopes(self):
        join = self.result.scopes["ROOT"].joins[0]
        assert join.left_scope == "ods.t1"
        assert join.right_scope == "ods.t2"

    def test_join_condition_columns(self):
        join = self.result.scopes["ROOT"].joins[0]
        cond_scopes = {(c.scope, c.column) for c in join.condition_columns}
        assert ("ods.t1", "id") in cond_scopes
        assert ("ods.t2", "id") in cond_scopes

    def test_depends_on_both_tables(self):
        assert set(self.result.scopes["ROOT"].depends_on) == {"ods.t1", "ods.t2"}


class TestDuplicateAliasResolution:
    """Duplicate aliases should not silently overwrite earlier sources."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "WITH dim_clct_misc AS ("
            "  SELECT misc_type, misc_val, misc_val_desc "
            "  FROM hw_jhy_iceberg.dim.dim_clct_misc_dc"
            ") "
            "INSERT OVERWRITE TABLE dwd.dwd_clct_stop_call_det_df "
            "SELECT b.unique_id AS unique_id, "
            "       b.misc_val_desc AS contact_rel_desc, "
            "       b.app_code AS app_cd "
            "FROM ods.ods_clct_prod_aux1_stop_dial_record_df a "
            "JOIN ods.ods_appserver_user_info_df b "
            "  ON a.customercode = b.open_id "
            " AND b.unique_id IS NOT NULL "
            " AND b.dt = '20260512' "
            "LEFT JOIN dim_clct_misc b "
            "  ON a.relationship = b.misc_val "
            " AND b.misc_type = 'relations'",
            "test_duplicate_alias_resolution",
        )

    def test_qualified_columns_disambiguate_duplicate_alias_by_column_presence(self):
        root = self.result.scopes["ROOT"]
        by_name = {col.name: col for col in root.columns}

        assert by_name["unique_id"].sources == [
            SourceRef(scope="ods.ods_appserver_user_info_df", column="unique_id")
        ]
        assert by_name["app_cd"].sources == [
            SourceRef(scope="ods.ods_appserver_user_info_df", column="app_code")
        ]
        assert by_name["contact_rel_desc"].sources == [
            SourceRef(scope="cte:dim_clct_misc", column="misc_val_desc")
        ]

    def test_join_conditions_use_the_matching_duplicate_alias_source(self):
        joins = self.result.scopes["ROOT"].joins

        assert joins[0].right_scope == "ods.ods_appserver_user_info_df"
        assert SourceRef(scope="ods.ods_appserver_user_info_df", column="open_id") in joins[0].condition_columns
        assert SourceRef(scope="ods.ods_appserver_user_info_df", column="unique_id") in joins[0].condition_columns
        assert SourceRef(scope="ods.ods_appserver_user_info_df", column="dt") in joins[0].condition_columns

        assert joins[1].right_scope == "cte:dim_clct_misc"
        assert SourceRef(scope="cte:dim_clct_misc", column="misc_val") in joins[1].condition_columns
        assert SourceRef(scope="cte:dim_clct_misc", column="misc_type") in joins[1].condition_columns

    def test_duplicate_alias_warning_is_emitted(self):
        assert any(w.type == "duplicate_alias" for w in self.result.diagnostics.warnings)


class TestWhereFilter:
    """WHERE clause: columns resolved."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id FROM ods.src a WHERE a.status = 1",
            "test_where",
        )

    def test_filter_exists(self):
        root = self.result.scopes["ROOT"]
        assert len(root.filters) == 1

    def test_filter_columns(self):
        f = self.result.scopes["ROOT"].filters[0]
        assert any(c.column == "status" for c in f.columns)


class TestGroupByHaving:
    """GROUP BY / HAVING: column references resolved."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, COUNT(*) AS cnt FROM ods.src a "
            "GROUP BY a.id HAVING COUNT(*) > 1",
            "test_group",
        )

    def test_group_by(self):
        root = self.result.scopes["ROOT"]
        assert len(root.group_by) > 0
        assert any(c.column == "id" for c in root.group_by)

    def test_having_filter(self):
        root = self.result.scopes["ROOT"]
        assert len(root.having) >= 1

    def test_aggregate_column(self):
        root = self.result.scopes["ROOT"]
        cnt_col = [c for c in root.columns if c.name == "cnt"][0]
        assert cnt_col.transform == "AGGREGATE"


class TestExpression:
    """EXPRESSION transform: function call with multiple sources."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT COALESCE(a.x, b.y) AS val "
            "FROM ods.t1 a JOIN ods.t2 b ON a.id = b.id",
            "test_expr",
        )

    def test_transform_is_expression(self):
        root = self.result.scopes["ROOT"]
        val_col = [c for c in root.columns if c.name == "val"][0]
        assert val_col.transform == "EXPRESSION"

    def test_multiple_sources(self):
        root = self.result.scopes["ROOT"]
        val_col = [c for c in root.columns if c.name == "val"][0]
        src_scopes = {s.scope for s in val_col.sources}
        assert "ods.t1" in src_scopes
        assert "ods.t2" in src_scopes


class TestSourcesDedup:
    """Same column referenced multiple times in one expression: deduplicated."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT IF(a.x > 0, a.x, 0) AS val FROM ods.src a",
            "test_dedup",
        )

    def test_deduplicated_sources(self):
        root = self.result.scopes["ROOT"]
        val_col = [c for c in root.columns if c.name == "val"][0]
        # a.x appears twice in IF(a.x > 0, a.x, 0) but should dedup to 1
        x_sources = [s for s in val_col.sources if s.column == "x"]
        assert len(x_sources) == 1


class TestConditionalCaseWhen:
    """CASE WHEN: case_branches populated."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT CASE WHEN a.x > 0 THEN 'pos' ELSE 'neg' END AS label "
            "FROM ods.src a",
            "test_case",
        )

    def test_transform(self):
        root = self.result.scopes["ROOT"]
        label = [c for c in root.columns if c.name == "label"][0]
        assert label.transform == "CONDITIONAL"

    def test_case_branches(self):
        root = self.result.scopes["ROOT"]
        label = [c for c in root.columns if c.name == "label"][0]
        assert label.case_branches is not None
        assert len(label.case_branches) >= 1
        # First branch has a when_expr
        assert label.case_branches[0]["when_expr"] != ""


class TestConstantColumn:
    """CONSTANT transform: literal values are represented as traceable leaves."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT 1 AS one, 'hello' AS greeting, a.id FROM ods.src a",
            "test_const",
        )

    def test_constant_transform(self):
        root = self.result.scopes["ROOT"]
        one_col = [c for c in root.columns if c.name == "one"][0]
        assert one_col.transform == "CONSTANT"
        assert one_col.sources == [SourceRef(scope=CONSTANT_SCOPE_ID, column="1")]

    def test_mixed_with_direct(self):
        root = self.result.scopes["ROOT"]
        id_col = [c for c in root.columns if c.name == "id"][0]
        assert id_col.transform == "DIRECT"
        assert len(id_col.sources) == 1


class TestUnionConstantBranchLineage:
    """UNION constants should not look like empty upstream lineage."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO mart.t "
            "SELECT 'total' AS metric, '-' AS create_name, a.id FROM ods.a a "
            "UNION ALL "
            "SELECT 'used' AS metric, b.create_name, b.id FROM ods.b b",
            "test_union_constants",
        )

    def test_constant_union_branch_has_literal_source(self):
        b01 = self.result.scopes["union:main:b01"]
        metric = [c for c in b01.columns if c.name == "metric"][0]
        create_name = [c for c in b01.columns if c.name == "create_name"][0]

        assert metric.sources == [SourceRef(scope=CONSTANT_SCOPE_ID, column="'total'")]
        assert create_name.sources == [SourceRef(scope=CONSTANT_SCOPE_ID, column="'-'")]

    def test_union_traces_to_constants_and_physical_sources(self):
        metric_sources = trace_to_physical(self.result, "ROOT", "metric")
        create_name_sources = trace_to_physical(self.result, "ROOT", "create_name")

        assert (CONSTANT_SCOPE_ID, "'total'", "UNION") in metric_sources
        assert (CONSTANT_SCOPE_ID, "'used'", "UNION") in metric_sources
        assert (CONSTANT_SCOPE_ID, "'-'", "UNION") in create_name_sources
        assert ("ods.b", "create_name", "UNION") in create_name_sources


class TestSourceFreeDerivedLineage:
    """Source-free non-constant transforms should still have explainable leaves."""

    def test_count_star_uses_input_rowset(self):
        result = parse_scope_lineage(
            "INSERT INTO mart.t SELECT COUNT(*) AS cnt FROM ods.src a",
            "test_count_star",
        )

        cnt = result.scopes["ROOT"].columns[0]
        assert cnt.transform == "AGGREGATE"
        assert cnt.sources == [SourceRef(scope="ods.src", column="*")]

    def test_runtime_expression_uses_system_leaf(self):
        result = parse_scope_lineage(
            "INSERT INTO mart.t SELECT DATE_FORMAT(NOW(), 'yyyy-MM-dd') AS dt",
            "test_runtime_expr",
        )

        dt = result.scopes["ROOT"].columns[0]
        assert dt.transform == "EXPRESSION"
        assert dt.sources == [SourceRef(scope=SYSTEM_SCOPE_ID, column=dt.expression)]

    def test_literal_expression_uses_constant_leaf(self):
        result = parse_scope_lineage(
            "INSERT INTO mart.t SELECT DATE_ADD('2026-04-27', 1) AS dt",
            "test_literal_expr",
        )

        dt = result.scopes["ROOT"].columns[0]
        assert dt.transform == "EXPRESSION"
        assert dt.sources == [SourceRef(scope=CONSTANT_SCOPE_ID, column=dt.expression)]


class TestWindowFunction:
    """WINDOW transform: window info extracted."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT ROW_NUMBER() OVER (PARTITION BY a.dept ORDER BY a.salary DESC) AS rn "
            "FROM ods.src a",
            "test_window",
        )

    def test_transform(self):
        root = self.result.scopes["ROOT"]
        rn_col = [c for c in root.columns if c.name == "rn"][0]
        assert rn_col.transform == "WINDOW"

    def test_window_info(self):
        root = self.result.scopes["ROOT"]
        rn_col = [c for c in root.columns if c.name == "rn"][0]
        assert rn_col.window is not None


class TestMergeMatched:
    """MERGE: MATCHED branch produces column with merge_branch='matched'."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "MERGE INTO dwd.t USING (SELECT a.id, a.name FROM ods.src a) s "
            "ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.name = s.name",
            "test_merge_matched",
        )

    def test_root_has_columns(self):
        root = self.result.scopes["ROOT"]
        assert len(root.columns) >= 1

    def test_merge_branch_matched(self):
        root = self.result.scopes["ROOT"]
        name_cols = [c for c in root.columns if c.name == "name"]
        assert len(name_cols) >= 1
        assert name_cols[0].merge_branch == "matched"

    def test_source_from_using_scope(self):
        root = self.result.scopes["ROOT"]
        name_col = [c for c in root.columns if c.name == "name"][0]
        assert name_col.sources[0].scope == "subq:s"


class TestMergeBothBranches:
    """MERGE with both MATCHED and NOT MATCHED branches."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "MERGE INTO dwd.t USING (SELECT a.id, a.name FROM ods.src a) s "
            "ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET t.name = s.name "
            "WHEN NOT MATCHED THEN INSERT (id, name) VALUES (s.id, s.name)",
            "test_merge_both",
        )

    def test_both_branches_present(self):
        root = self.result.scopes["ROOT"]
        matched = [c for c in root.columns if c.merge_branch == "matched"]
        not_matched = [c for c in root.columns if c.merge_branch == "not_matched"]
        assert len(matched) >= 1
        assert len(not_matched) >= 1

    def test_not_matched_sources(self):
        root = self.result.scopes["ROOT"]
        nm_cols = [c for c in root.columns if c.merge_branch == "not_matched"]
        # All NOT MATCHED columns should source from subq:s
        for col in nm_cols:
            if col.sources:
                assert col.sources[0].scope == "subq:s"

    def test_merge_constant_value_has_literal_source(self):
        result = parse_scope_lineage(
            "MERGE INTO dwd.t USING (SELECT a.id FROM ods.src a) s "
            "ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET t.flag = '1' "
            "WHEN NOT MATCHED THEN INSERT (id, flag) VALUES (s.id, '0')",
            "test_merge_constants",
        )

        flags = [c for c in result.scopes["ROOT"].columns if c.name == "flag"]
        assert {c.merge_branch for c in flags} == {"matched", "not_matched"}
        assert [SourceRef(scope=CONSTANT_SCOPE_ID, column="'1'")] in [c.sources for c in flags]
        assert [SourceRef(scope=CONSTANT_SCOPE_ID, column="'0'")] in [c.sources for c in flags]


class TestMergeDeleteBranch:
    """MERGE DELETE is recorded as an explicit diagnostic, not silent column lineage."""

    def test_delete_branch_warns_without_root_delete_column(self):
        result = parse_scope_lineage(
            "MERGE INTO dwd.t t USING (SELECT a.id, a.flag FROM ods.src a) s "
            "ON t.id = s.id "
            "WHEN MATCHED AND s.flag = 'D' THEN DELETE "
            "WHEN MATCHED THEN UPDATE SET t.flag = s.flag",
            "test_merge_delete",
        )

        root = result.scopes["ROOT"]
        assert [c.name for c in root.columns] == ["flag"]
        assert root.columns[0].merge_branch == "matched"
        assert [
            w for w in result.diagnostics.warnings
            if w.type == "merge_delete_ignored"
        ]


class TestUnqualifiedFallbacks:
    """High-risk unqualified fallback paths are intentional and diagnosed."""

    def test_no_schema_single_physical_table_fallback_warns(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT id FROM ods.a a JOIN ods.b b ON a.k = b.k",
            "test_no_schema_physical_fallback",
        )

        root = result.scopes["ROOT"]
        assert root.columns[0].sources == [SourceRef(scope="ods.a", column="id")]
        assert [
            w for w in result.diagnostics.warnings
            if w.type == "unresolved_unqualified_no_schema"
        ]


class TestCTEChain:
    """CTE b references CTE a: verify column resolution through chain."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH a AS (SELECT t1.id FROM ods.src t1), "
            "b AS (SELECT a.id FROM a) "
            "SELECT b.id FROM b",
            "test_cte_chain",
        )

    def test_b_sources_from_a(self):
        cte_b = self.result.scopes["cte:b"]
        assert cte_b.columns[0].sources == [SourceRef(scope="cte:a", column="id")]

    def test_dependency_chain(self):
        assert self.result.scopes["cte:a"].depends_on == ["ods.src"]
        assert self.result.scopes["cte:b"].depends_on == ["cte:a"]
        assert self.result.scopes["ROOT"].depends_on == ["cte:b"]


class TestNestedUnionInCTE:
    """UNION inside a CTE: cte:cte scope has columns from union:cte."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS (SELECT a.id FROM ods.t1 a UNION ALL SELECT b.id FROM ods.t2 b) "
            "SELECT c.id FROM cte c",
            "test_nested_union",
        )

    def test_cte_columns(self):
        cte = self.result.scopes["cte:cte"]
        assert len(cte.columns) == 1
        assert cte.columns[0].sources == [SourceRef(scope="union:cte", column="id")]

    def test_union_scope_columns(self):
        union = self.result.scopes["union:cte"]
        assert union.columns[0].transform == "UNION"

    def test_full_dependency_chain(self):
        assert self.result.scopes["ROOT"].depends_on == ["cte:cte"]
        assert "union:cte" in self.result.scopes["cte:cte"].depends_on


class TestSameTableInUnion:
    """Same physical table in multiple UNION branches: single node, multiple refs."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id FROM ods.t1 a WHERE dt=1 "
            "UNION ALL SELECT b.id FROM ods.t1 b WHERE dt=2",
            "test_dedup",
        )

    def test_single_physical_table(self):
        assert self.result.source_tables == ["ods.t1"]

    def test_both_branches_reference_same_table(self):
        b01 = self.result.scopes["union:main:b01"]
        b02 = self.result.scopes["union:main:b02"]
        assert b01.columns[0].sources[0].scope == "ods.t1"
        assert b02.columns[0].sources[0].scope == "ods.t1"


class TestLateralView:
    """LATERAL VIEW: UDTF output columns available in parent scope."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, e.pos, e.val "
            "FROM ods.t1 a LATERAL VIEW posexplode(a.arr) e AS pos, val",
            "test_lateral",
        )

    def test_root_references_udtf(self):
        root = self.result.scopes["ROOT"]
        pos_col = [c for c in root.columns if c.name == "pos"]
        val_col = [c for c in root.columns if c.name == "val"]
        assert len(pos_col) == 1
        assert len(val_col) == 1
        assert pos_col[0].sources[0].scope == "udtf:e"
        assert val_col[0].sources[0].scope == "udtf:e"

    def test_root_references_physical(self):
        root = self.result.scopes["ROOT"]
        id_col = [c for c in root.columns if c.name == "id"]
        assert len(id_col) == 1
        assert id_col[0].sources[0].scope == "ods.t1"


class TestAggregateFunction:
    """AGGREGATE transform: known UDAF recognized."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT COLLECT_SET(a.status) AS statuses FROM ods.src a GROUP BY a.id",
            "test_agg",
        )

    def test_aggregate_transform(self):
        root = self.result.scopes["ROOT"]
        col = [c for c in root.columns if c.name == "statuses"][0]
        assert col.transform == "AGGREGATE"

    def test_agg_function_name(self):
        root = self.result.scopes["ROOT"]
        col = [c for c in root.columns if c.name == "statuses"][0]
        assert col.agg_function == "COLLECT_SET"


class TestExpandAllStar:
    """SELECT * without schema: EXPAND_ALL transform + diagnostic warning."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT * FROM ods.src a",
            "test_star",
        )

    def test_expand_all_transform(self):
        root = self.result.scopes["ROOT"]
        assert any(c.transform == "EXPAND_ALL" for c in root.columns)

    def test_expand_all_sources(self):
        root = self.result.scopes["ROOT"]
        star_cols = [c for c in root.columns if c.transform == "EXPAND_ALL"]
        assert len(star_cols) >= 1
        # Should reference ods.src
        for sc in star_cols:
            assert any(s.scope == "ods.src" for s in sc.sources)

    def test_star_not_expanded_warning(self):
        """When * cannot be expanded, a diagnostic warning is emitted."""
        warnings = self.result.diagnostics.warnings
        assert any(w.type == "star_not_expanded" for w in warnings)


class TestStarExpansionFromCTE:
    """Star expansion: SELECT a.* from a CTE with resolved columns expands into individual DIRECT columns."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS (SELECT x.id, x.name, x.amount FROM ods.src x) "
            "SELECT a.* FROM cte a",
            "test_star_cte",
        )

    def test_no_expand_all(self):
        """No EXPAND_ALL columns — all expanded to DIRECT."""
        root = self.result.scopes["ROOT"]
        expand_all = [c for c in root.columns if c.transform == "EXPAND_ALL"]
        assert expand_all == []

    def test_expanded_columns(self):
        """ROOT has 3 individual DIRECT columns from the CTE."""
        root = self.result.scopes["ROOT"]
        names = [c.name for c in root.columns]
        assert "id" in names
        assert "name" in names
        assert "amount" in names

    def test_sources_reference_cte(self):
        """Each expanded column's source is the CTE scope."""
        root = self.result.scopes["ROOT"]
        for c in root.columns:
            if c.name in ("id", "name", "amount"):
                assert c.transform == "DIRECT"
                assert len(c.sources) == 1
                assert c.sources[0].scope == "cte:cte"
                assert c.sources[0].column == c.name


class TestStarExpansionMixedWithExpressions:
    """SELECT a.*, expr — star expands plus additional expression columns."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS (SELECT x.id, x.name FROM ods.src x) "
            "SELECT a.*, a.id + 1 AS id2 FROM cte a",
            "test_star_mixed",
        )

    def test_expanded_plus_extra(self):
        root = self.result.scopes["ROOT"]
        names = [c.name for c in root.columns]
        assert "id" in names
        assert "name" in names
        assert "id2" in names

    def test_id2_is_expression(self):
        root = self.result.scopes["ROOT"]
        id2 = [c for c in root.columns if c.name == "id2"][0]
        assert id2.transform == "EXPRESSION"


class TestStarExpansionInCTEBody:
    """CTE body uses a.* referencing another CTE — expansion propagates."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte1 AS (SELECT x.id, x.name FROM ods.src x), "
            "cte2 AS (SELECT a.* FROM cte1 a) "
            "SELECT b.id FROM cte2 b",
            "test_star_cte_chain",
        )

    def test_cte2_expanded(self):
        """cte2 should have expanded a.* into individual columns from cte1."""
        cte2 = self.result.scopes["cte:cte2"]
        expand_all = [c for c in cte2.columns if c.transform == "EXPAND_ALL"]
        assert expand_all == []
        names = [c.name for c in cte2.columns]
        assert "id" in names
        assert "name" in names


class TestSelectListMultiAliasFunction:
    """Spark table function projection: func(x) AS (c1, c2) exposes named columns."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH a AS ("
            "  SELECT user_id, default.ma_flow_attribution(-1, 1, seq) "
            "  AS (tgt_event_name, tgt_event_time) FROM ods.src"
            ") "
            "SELECT a.user_id, a.tgt_event_name, a.tgt_event_time FROM a",
            "test_multi_alias_function",
        )

    def test_function_outputs_are_named_columns(self):
        cte = self.result.scopes["cte:a"]
        names = {c.name for c in cte.columns}
        assert "tgt_event_name" in names
        assert "tgt_event_time" in names

    def test_downstream_references_do_not_dangle(self):
        root = self.result.scopes["ROOT"]
        tgt_event_name = [c for c in root.columns if c.name == "tgt_event_name"][0]
        assert tgt_event_name.sources[0].scope == "cte:a"
        assert tgt_event_name.sources[0].column == "tgt_event_name"


class TestUnionBranchSelectedSourcesFallback:
    """Union branches with empty selected_sources still resolve CTE inputs."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH src AS (SELECT a.user_id, a.reg_date FROM ods.src a) "
            "SELECT user_id, reg_date FROM ("
            "  SELECT CAST(user_id AS BIGINT) AS user_id, reg_date FROM src "
            "  UNION ALL "
            "  SELECT CAST(user_id AS BIGINT) AS user_id, reg_date FROM src"
            ") u",
            "test_union_branch_selected_sources_fallback",
        )

    def test_union_branch_has_cte_sources(self):
        branch = self.result.scopes["union:main:b01"]
        user_id = [c for c in branch.columns if c.name == "user_id"][0]
        assert user_id.sources[0].scope != "UNKNOWN"
        assert user_id.sources[0].column == "user_id"


class TestStructFieldAccess:
    """Spark struct/map access alias.struct_col.field traces to the base struct column."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH j AS (SELECT a.max_group_id FROM ods.src a) "
            "SELECT j.max_group_id.is_open_contrast AS is_open_contrast FROM j",
            "test_struct_field_access",
        )

    def test_struct_field_sources_base_column(self):
        root = self.result.scopes["ROOT"]
        col = [c for c in root.columns if c.name == "is_open_contrast"][0]
        assert col.sources[0].scope == "cte:j"
        assert col.sources[0].column == "max_group_id"
        assert not any(s.scope == "UNKNOWN" for s in col.sources)


class TestLateralViewStarMaterialization:
    """Columns materialized through SELECT * should not be attributed to UDTF aliases."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH expanded AS ("
            "  SELECT *, e.col AS property_value "
            "  FROM ods.src "
            "  LATERAL VIEW EXPLODE(SPLIT(value, ',')) e AS col"
            ") "
            "SELECT point_id, property_value FROM expanded",
            "test_lateral_star_materialization",
        )

    def test_original_column_does_not_source_from_udtf(self):
        expanded = self.result.scopes["cte:expanded"]
        point_id = [c for c in expanded.columns if c.name == "point_id"][0]
        assert any(s.scope == "ods.src" and s.column == "point_id" for s in point_id.sources)
        assert not any(s.scope.startswith("udtf:") for s in point_id.sources)

    def test_generated_column_sources_from_udtf(self):
        expanded = self.result.scopes["cte:expanded"]
        prop = [c for c in expanded.columns if c.name == "property_value"][0]
        assert any(s.scope.startswith("udtf:") and s.column == "col" for s in prop.sources)


class TestRepeatedLateralAlias:
    """Repeated lateral-view table aliases should still expose both output columns."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT u_unit_code AS unit_code, u_creative_code AS creative_code "
            "FROM ods.src "
            "LATERAL VIEW EXPLODE(SPLIT(unit_code, ',')) t AS u_unit_code "
            "LATERAL VIEW EXPLODE(SPLIT(creative_code, ',')) t AS u_creative_code",
            "test_repeated_lateral_alias",
        )

    def test_first_lateral_output_resolves(self):
        root = self.result.scopes["ROOT"]
        unit_code = [c for c in root.columns if c.name == "unit_code"][0]
        assert any(s.column == "u_unit_code" and s.scope.startswith("udtf:") for s in unit_code.sources)
        assert not any(s.scope == "UNKNOWN" for s in unit_code.sources)

    def test_second_lateral_output_resolves(self):
        root = self.result.scopes["ROOT"]
        creative_code = [c for c in root.columns if c.name == "creative_code"][0]
        assert any(s.column == "u_creative_code" and s.scope.startswith("udtf:") for s in creative_code.sources)


class TestWindowUnqualifiedResolution:
    """Window expressions should bind unqualified columns to the right input."""

    def test_window_value_prefers_join_table_over_star_scope(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH base AS (SELECT * FROM ods.base) "
            "SELECT a.*, FIRST_VALUE(loan_bill_no) OVER ("
            "  PARTITION BY a.unique_id ORDER BY b.final_complete_dt DESC"
            ") AS loan_bill_no "
            "FROM base a LEFT JOIN ods.bill b ON a.unique_id = b.unique_id",
            "test_window_join_table_over_star",
        )

        root = result.scopes["ROOT"]
        bill_no = [c for c in root.columns if c.name == "loan_bill_no"][0]
        assert any(s.scope == "ods.bill" and s.column == "loan_bill_no" for s in bill_no.sources)
        assert not any(s.scope == "cte:base" and s.column == "loan_bill_no" for s in bill_no.sources)

    def test_window_partition_order_do_not_bind_to_udtf(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT session_id, create_time, "
            "FIRST_VALUE(item.matchvalue) OVER (PARTITION BY session_id ORDER BY create_time) AS first_matchvalue "
            "FROM ods.chat t "
            "LATERAL VIEW inline(from_json(t.payload, "
            "'struct<matchDataList: array<struct<matchSource: string, matchValue: string>>>'"
            ").matchDataList) item",
            "test_window_partition_not_udtf",
        )

        root = result.scopes["ROOT"]
        first = [c for c in root.columns if c.name == "first_matchvalue"][0]
        source_pairs = {(s.scope, s.column) for s in first.sources}
        assert ("udtf:item", "matchvalue") in source_pairs
        assert ("ods.chat", "session_id") in source_pairs
        assert ("ods.chat", "create_time") in source_pairs
        assert ("udtf:item", "session_id") not in source_pairs
        assert ("udtf:item", "create_time") not in source_pairs


class TestStarExpansionKeepsMaterializationPath:
    """Internal star placeholders stay available so later references can materialize."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH base AS (SELECT * FROM ods.src), "
            "a AS (SELECT base.*, base.id FROM base) "
            "SELECT a.* FROM a",
            "test_star_materialization_path",
        )

    def test_root_has_named_column_from_star_chain(self):
        root = self.result.scopes["ROOT"]
        names = [c.name for c in root.columns]
        assert "id" in names


class TestGraphIntegrity:
    """Verify scope_graph edges are consistent with depends_on."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t WITH cte AS (SELECT a.id FROM ods.t1 a) SELECT b.id FROM cte b",
            "test_integrity",
        )

    def test_edges_match_depends_on(self):
        for sid, sd in self.result.scopes.items():
            for dep in sd.depends_on:
                assert (dep, sid) in {(e.from_, e.to) for e in self.result.scope_graph.edges}

    def test_depends_on_subset_of_nodes(self):
        all_nodes = set(self.result.scope_graph.nodes)
        for sid, sd in self.result.scopes.items():
            for dep in sd.depends_on:
                assert dep in all_nodes, f"{dep} not in scope_graph.nodes"

    def test_scopes_in_nodes(self):
        all_nodes = set(self.result.scope_graph.nodes)
        for sid in self.result.scopes:
            assert sid in all_nodes


# ── Regression tests for Bugs 4-6 ──────────────────────────────────────────


class TestLeftJoinType:
    """Bug 4 regression: LEFT JOIN must be LEFT_OUTER, not LEFT_INNER."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, b.name "
            "FROM ods.t1 a LEFT JOIN ods.t2 b ON a.id = b.id",
            "test_left_join",
        )

    def test_join_type_is_left_outer(self):
        root = self.result.scopes["ROOT"]
        assert len(root.joins) == 1
        assert root.joins[0].join_type == "LEFT_OUTER"


class TestRightJoinType:
    """Bug 4 regression: RIGHT JOIN must be RIGHT_OUTER."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, b.name "
            "FROM ods.t1 a RIGHT JOIN ods.t2 b ON a.id = b.id",
            "test_right_join",
        )

    def test_join_type_is_right_outer(self):
        root = self.result.scopes["ROOT"]
        assert len(root.joins) == 1
        assert root.joins[0].join_type == "RIGHT_OUTER"


class TestCrossJoinType:
    """Bug 4: CROSS JOIN stays as CROSS."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, b.name "
            "FROM ods.t1 a CROSS JOIN ods.t2 b",
            "test_cross_join",
        )

    def test_join_type_is_cross(self):
        root = self.result.scopes["ROOT"]
        assert len(root.joins) == 1
        assert root.joins[0].join_type == "CROSS"


class TestWindowPartitionByAndDirection:
    """Bug 5 regression: window partition_by populated, order_by direction correct."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.dept, a.salary, "
            "ROW_NUMBER() OVER (PARTITION BY a.dept ORDER BY a.salary DESC) AS rn "
            "FROM ods.t1 a",
            "test_window",
        )

    def test_partition_by_populated(self):
        root = self.result.scopes["ROOT"]
        rn_col = [c for c in root.columns if c.name == "rn"]
        assert len(rn_col) == 1
        assert rn_col[0].window is not None
        assert "partition_by" in rn_col[0].window
        assert len(rn_col[0].window["partition_by"]) > 0

    def test_order_by_direction_desc(self):
        root = self.result.scopes["ROOT"]
        rn_col = [c for c in root.columns if c.name == "rn"][0]
        order_by = rn_col.window.get("order_by", [])
        assert len(order_by) > 0
        assert order_by[0]["direction"] == "DESC"


class TestWindowOrderByAsc:
    """Bug 5: ORDER BY without DESC should be ASC, not DESC."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, "
            "ROW_NUMBER() OVER (ORDER BY a.salary) AS rn "
            "FROM ods.t1 a",
            "test_window_asc",
        )

    def test_order_by_direction_asc(self):
        root = self.result.scopes["ROOT"]
        rn_col = [c for c in root.columns if c.name == "rn"][0]
        order_by = rn_col.window.get("order_by", [])
        assert len(order_by) > 0
        assert order_by[0]["direction"] == "ASC"


class TestUnionUnnamedColumnNames:
    """Bug 6 regression: unnamed columns in later UNION branches adopt first branch's names."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT a.id, a.name FROM ods.t1 a "
            "UNION ALL "
            "SELECT b.id, TO_DATE(b.create_time) FROM ods.t2 b",
            "test_unnamed_union",
        )

    def test_no_col_n_in_branches(self):
        """Later branch columns should not have _col_N names."""
        b02 = self.result.scopes.get("union:main:b02")
        assert b02 is not None
        unnamed = [c.name for c in b02.columns if c.name.startswith("_col_")]
        assert unnamed == [], f"Found _col_ names in b02: {unnamed}"

    def test_branch_adopted_first_branch_name(self):
        """TO_DATE column in b02 should adopt 'name' from b01."""
        b02 = self.result.scopes["union:main:b02"]
        assert len(b02.columns) >= 2
        assert b02.columns[1].name == "name"

    def test_union_scope_column_name(self):
        """Union scope's second column should be 'name', not '_col_N'."""
        union = self.result.scopes["union:main"]
        assert len(union.columns) >= 2
        assert union.columns[1].name == "name"


class TestPosexplodeDefaultColumns:
    """Spark posexplode projection exposes default pos/col columns."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT split(col, ':')[1] AS value FROM ("
            "  SELECT id, posexplode(split(payload, ',')) FROM ods.t1"
            ") a WHERE col LIKE '%score%'",
            "test_posexplode_defaults",
        )

    def test_posexplode_outputs_col(self):
        subq = self.result.scopes["subq:a"]
        assert "col" in {c.name for c in subq.columns}

    def test_downstream_col_resolves_to_udtf_input(self):
        root = self.result.scopes["ROOT"]
        value = [c for c in root.columns if c.name == "value"][0]
        assert ("subq:a", "col") in {(s.scope, s.column) for s in value.sources}

        subq = self.result.scopes["subq:a"]
        col = [c for c in subq.columns if c.name == "col"][0]
        assert ("ods.t1", "payload") in {(s.scope, s.column) for s in col.sources}


class TestGeneratorColumns:
    def test_explode_explicit_alias_is_preserved(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT a.json_row FROM ("
            "  SELECT explode(from_json(payload, 'array<struct<name:string,value:string>>')) AS json_row "
            "  FROM ods.t1"
            ") a",
            "test_explode_alias",
        )

        subq = result.scopes["subq:a"]
        assert "json_row" in {c.name for c in subq.columns}
        root_col = result.scopes["ROOT"].columns[0]
        assert root_col.sources == [SourceRef(scope="subq:a", column="json_row")]

    def test_lateral_posexplode_explicit_columns(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT t.node_id_cd, t.node_id_name "
            "FROM ods.t1 a LATERAL VIEW posexplode(split(a.node_id, '-')) t AS node_id_cd, node_id_name",
            "test_lateral_posexplode",
        )

        udtf = result.scopes["udtf:t"]
        assert {c.name for c in udtf.columns} == {"node_id_cd", "node_id_name"}
        for col in udtf.columns:
            assert col.sources == [SourceRef(scope="ods.t1", column="node_id")]

    def test_lateral_inline_infers_struct_fields(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT item.matchsource, item.matchvalue "
            "FROM ods.t1 t "
            "LATERAL VIEW inline(from_json(t.payload, "
            "'struct<matchDataList: array<struct<matchSource: string, matchValue: string>>>'"
            ").matchDataList) item",
            "test_lateral_inline",
        )

        udtf = result.scopes["udtf:item"]
        assert {c.name for c in udtf.columns} == {"matchsource", "matchvalue"}
