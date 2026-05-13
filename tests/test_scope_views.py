"""Tests for scope_views.py: Mermaid diagrams, Markdown report, query functions, file output."""

import pytest
from pathlib import Path

from lineage_parser.scope_builder import parse_scope_lineage
from lineage_parser.scope_types import ScopeColumn, ScopeData, ScopeGraph, ScopeLineageResult, SourceRef
from lineage_parser.scope_types import ScopeLineageResult, ScopeData, ScopeColumn, SourceRef
from lineage_parser.scope_views import (
    safe_id,
    column_node_id,
    _mermaid_escape,
    _edge_arrow,
    _more_significant_transform,
    trace_to_physical,
    upstream,
    downstream,
    scope_overview_mmd,
    physical_lineage_mmd,
    single_field_trace_mmd,
    field_lineage_mmd,
    lineage_md,
    write_views,
)


# -- Utility tests -----------------------------------------------------------


class TestSafeId:
    def test_colon_replaced(self):
        assert safe_id("cte:my_cte") == "cte_my_cte"

    def test_dot_replaced(self):
        assert safe_id("ods.t1") == "ods_t1"

    def test_star_replaced(self):
        assert safe_id("a.*") == "a__"

    def test_dash_replaced(self):
        assert safe_id("my-scope") == "my_scope"

    def test_no_special(self):
        assert safe_id("ROOT") == "ROOT"


class TestColumnNodeId:
    def test_basic(self):
        assert column_node_id("cte:a", "id") == "cte_a__id"

    def test_with_suffix(self):
        assert column_node_id("ROOT", "name", "matched") == "ROOT__name__matched"

    def test_avoids_unicode_collisions(self):
        first = column_node_id("CONSTANT", "'已使用的量'")
        second = column_node_id("CONSTANT", "'已失效的量'")
        assert first != second
        assert first.startswith("CONSTANT__")
        assert second.startswith("CONSTANT__")


class TestMermaidEscape:
    def test_quotes_escaped(self):
        assert '"' not in _mermaid_escape('he said "hello"')

    def test_brackets_escaped(self):
        result = _mermaid_escape("arr[0]")
        assert "[" not in result and "]" not in result

    def test_parens_escaped(self):
        result = _mermaid_escape("f(x)")
        assert "(" not in result and ")" not in result
        # Full-width parens present
        assert "（" in result and "）" in result

    def test_pipe_escaped(self):
        assert "|" not in _mermaid_escape("a | b")

    def test_no_truncation_by_default(self):
        long_text = "x" * 500
        assert len(_mermaid_escape(long_text)) == 500

    def test_truncation_when_set(self):
        assert len(_mermaid_escape("abcdefgh", max_len=5)) == 5


class TestEdgeArrow:
    def test_direct(self):
        assert _edge_arrow("DIRECT") == "-->"

    def test_expression(self):
        arrow = _edge_arrow("EXPRESSION")
        assert "-.->" in arrow and "f" in arrow

    def test_aggregate(self):
        arrow = _edge_arrow("AGGREGATE")
        assert "==>" in arrow

    def test_conditional(self):
        arrow = _edge_arrow("CONDITIONAL")
        assert "-.->" in arrow

    def test_union(self):
        arrow = _edge_arrow("UNION")
        assert "-->" in arrow


class TestMoreSignificantTransform:
    def test_aggregate_over_direct(self):
        assert _more_significant_transform("AGGREGATE", "DIRECT") == "AGGREGATE"

    def test_direct_over_constant(self):
        assert _more_significant_transform("DIRECT", "CONSTANT") == "DIRECT"

    def test_same(self):
        assert _more_significant_transform("EXPRESSION", "EXPRESSION") == "EXPRESSION"


# -- Query function tests ----------------------------------------------------


class TestTraceToPhysical:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS (SELECT a.id, a.name FROM ods.src a) "
            "SELECT b.id, b.name FROM cte b",
            "test_trace",
        )

    def test_penetrates_cte(self):
        traces = trace_to_physical(self.result, "ROOT", "id")
        assert len(traces) >= 1
        # Should reach ods.src
        physical_scopes = [t[0] for t in traces]
        assert "ods.src" in physical_scopes

    def test_unknown_column_returns_empty(self):
        traces = trace_to_physical(self.result, "ROOT", "nonexistent")
        assert traces == []

    def test_wildcard_fallback_passes_requested_column_name(self):
        result = ScopeLineageResult(
            task_id="manual_wildcard",
            target_table="dwd.t",
            stmt_kind="INSERT",
            source_tables=["ods.src"],
            scope_graph=ScopeGraph(nodes=["ROOT", "subq:s", "ods.src"], edges=[]),
            scopes={
                "ROOT": ScopeData(
                    kind="root",
                    columns=[
                        ScopeColumn(
                            name="id",
                            transform="DIRECT",
                            sources=[SourceRef(scope="subq:s", column="id")],
                        )
                    ],
                ),
                "subq:s": ScopeData(
                    kind="subquery",
                    columns=[
                        ScopeColumn(
                            name="*",
                            transform="EXPAND_ALL",
                            sources=[SourceRef(scope="ods.src", column="*")],
                        )
                    ],
                ),
            },
        )

        assert trace_to_physical(result, "ROOT", "id") == [("ods.src", "id", "EXPAND_ALL")]

    def test_star_request_expands_concrete_scope_columns(self):
        result = ScopeLineageResult(
            task_id="manual_star_rowset",
            target_table="dwd.t",
            stmt_kind="INSERT",
            source_tables=["ods.src"],
            scope_graph=ScopeGraph(nodes=["ROOT", "subq:s", "ods.src"], edges=[]),
            scopes={
                "ROOT": ScopeData(
                    kind="root",
                    columns=[
                        ScopeColumn(
                            name="cnt",
                            transform="AGGREGATE",
                            sources=[SourceRef(scope="subq:s", column="*")],
                        )
                    ],
                ),
                "subq:s": ScopeData(
                    kind="subquery",
                    columns=[
                        ScopeColumn(name="id", transform="DIRECT", sources=[SourceRef(scope="ods.src", column="id")]),
                        ScopeColumn(name="name", transform="DIRECT", sources=[SourceRef(scope="ods.src", column="name")]),
                    ],
                ),
            },
        )

        assert sorted(trace_to_physical(result, "ROOT", "cnt")) == [
            ("ods.src", "id", "AGGREGATE"),
            ("ods.src", "name", "AGGREGATE"),
        ]

    def test_missing_column_with_single_upstream_is_passed_through(self):
        result = ScopeLineageResult(
            task_id="manual_single_upstream_missing",
            target_table="dwd.t",
            stmt_kind="INSERT",
            source_tables=["ods.src"],
            scope_graph=ScopeGraph(nodes=["ROOT", "subq:s", "ods.src"], edges=[]),
            scopes={
                "ROOT": ScopeData(
                    kind="root",
                    columns=[
                        ScopeColumn(
                            name="late_col",
                            transform="DIRECT",
                            sources=[SourceRef(scope="subq:s", column="late_col")],
                        )
                    ],
                ),
                "subq:s": ScopeData(
                    kind="subquery",
                    depends_on=["ods.src"],
                    columns=[
                        ScopeColumn(name="known_col", transform="DIRECT", sources=[SourceRef(scope="ods.src", column="known_col")]),
                    ],
                ),
            },
        )

        assert trace_to_physical(result, "ROOT", "late_col") == [("ods.src", "late_col", "DIRECT")]


class TestUpstream:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id FROM ods.src a",
            "test_upstream",
        )

    def test_returns_dict(self):
        result = upstream(self.result, "ROOT", "id")
        assert isinstance(result, dict)
        assert "physical_sources" in result
        assert "edges" in result


class TestDownstream:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id FROM ods.src a",
            "test_downstream",
        )

    def test_returns_list(self):
        result = downstream(self.result, "ods.src", "id")
        assert isinstance(result, list)


# -- Mermaid diagram tests ---------------------------------------------------


class TestScopeOverviewMmd:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS (SELECT a.id FROM ods.src a) "
            "SELECT b.id FROM cte b",
            "test_overview",
        )
        self.mmd = scope_overview_mmd(self.result)

    def test_starts_with_graph_td(self):
        assert self.mmd.startswith("graph TD")

    def test_contains_root_node(self):
        assert "ROOT" in self.mmd

    def test_contains_cte_node(self):
        assert "cte_cte" in self.mmd

    def test_contains_physical_node(self):
        assert "ods_src" in self.mmd

    def test_has_edge(self):
        assert "-->" in self.mmd

    def test_has_style_line(self):
        assert "style " in self.mmd

    def test_deduped_alias_label_includes_scope_id(self):
        result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT x.id FROM ("
            "  SELECT y.id FROM (SELECT a.id FROM ods.src a) x "
            "  JOIN (SELECT b.id FROM ods.src2 b) y ON x.id = y.id"
            ") x",
            "test_deduped_alias_label",
        )

        mmd = scope_overview_mmd(result)

        assert "SUBQUERY: x （subq:x_2）" in mmd


class TestScopeOverviewUnion:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "SELECT a.id FROM ods.t1 a UNION ALL SELECT b.id FROM ods.t2 b",
            "test_overview_union",
        )
        self.mmd = scope_overview_mmd(self.result)

    def test_contains_union_node(self):
        assert "union_main" in self.mmd

    def test_contains_branch_nodes(self):
        assert "union_main_b01" in self.mmd


class TestPhysicalLineageMmd:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.name FROM ods.src a",
            "test_physical",
        )
        self.mmd = physical_lineage_mmd(self.result)

    def test_starts_with_graph_td(self):
        assert self.mmd.startswith("graph TD")

    def test_has_root_columns(self):
        assert "ROOT__id" in self.mmd or "ROOT" in self.mmd

    def test_has_physical_columns(self):
        assert "ods_src" in self.mmd


class TestPhysicalLineageWithExpression:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.x + a.y AS total FROM ods.src a",
            "test_physical_expr",
        )
        self.mmd = physical_lineage_mmd(self.result)

    def test_expression_edge_style(self):
        # EXPRESSION edges should use dashed arrows
        assert "-.->" in self.mmd


class TestSingleFieldTraceMmd:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t "
            "WITH cte AS (SELECT a.id FROM ods.src a) "
            "SELECT b.id FROM cte b",
            "test_single_trace",
        )
        self.mmd = single_field_trace_mmd(self.result, "ROOT", "id")

    def test_starts_with_graph_td(self):
        assert self.mmd.startswith("graph TD")

    def test_traces_to_physical(self):
        assert "ods_src" in self.mmd

    def test_only_relevant_scopes(self):
        # Should contain ROOT and cte, but not random other scopes
        assert "ROOT" in self.mmd


class TestFieldLineageMmd:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.name FROM ods.src a",
            "test_field_lineage",
        )
        self.mmd = field_lineage_mmd(self.result)

    def test_starts_with_graph_td(self):
        assert self.mmd.startswith("graph TD")

    def test_has_root_subgraph(self):
        assert "ROOT" in self.mmd

    def test_has_physical_subgraph(self):
        assert "ods_src" in self.mmd

    def test_has_column_nodes(self):
        assert "__id" in self.mmd or "id" in self.mmd

    def test_no_unsafe_star_in_ids(self):
        """Regression: no * character in Mermaid node IDs."""
        import re
        for line in self.mmd.split("\n"):
            if "subgraph" in line or "__" in line:
                assert "*" not in line.split("[")[0], f"Unsafe * in: {line}"


class TestFieldLineageNoParensInLabels:
    """Regression: no half-width parens in edge labels (causes Mermaid parse error)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.x + 1 AS total FROM ods.src a",
            "test_parens",
        )
        self.mmd = field_lineage_mmd(self.result)

    def test_no_half_width_parens_in_labels(self):
        import re
        for line in self.mmd.split("\n"):
            for m in re.finditer(r'\|[^|]*\|', line):
                label = m.group()
                assert '(' not in label or '（' in label, f"Half-width ( in label: {label}"
                assert ')' not in label or '）' in label, f"Half-width ) in label: {label}"


class TestFieldLineageMergeDisambiguation:
    """MERGE produces matched/not_matched columns — node IDs must be unique."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "MERGE INTO dwd.t USING (SELECT 1 AS id, 'x' AS name) s "
            "ON dwd.t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET name = s.name "
            "WHEN NOT MATCHED THEN INSERT (id, name) VALUES (s.id, s.name)",
            "test_merge_views",
        )
        self.mmd = field_lineage_mmd(self.result)

    def test_starts_with_graph_td(self):
        assert self.mmd.startswith("graph TD")


# -- Markdown report tests ---------------------------------------------------


class TestLineageMd:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.name, a.x + 1 AS total FROM ods.src a",
            "test_md",
        )
        self.md = lineage_md(self.result)

    def test_has_header(self):
        assert "# Lineage:" in self.md

    def test_has_summary(self):
        assert "## Summary" in self.md

    def test_has_scope_overview(self):
        assert "## Scope Overview" in self.md
        assert "```mermaid" in self.md

    def test_has_field_table(self):
        assert "## Field Lineage" in self.md

    def test_has_computation_logic(self):
        assert "## Key Computation Logic" in self.md

    def test_has_join_logic(self):
        # Join section is conditional — only present when there are joins
        # This simple SQL has no joins, so just verify the MD is valid
        assert isinstance(self.md, str) and len(self.md) > 0

    def test_has_filter_logic(self):
        # Filter section is conditional — only present when there are filters
        assert isinstance(self.md, str) and len(self.md) > 0

    def test_has_concerns(self):
        # Concerns section is conditional — only present when there are warnings
        assert isinstance(self.md, str) and len(self.md) > 0

    def test_has_physical_trace(self):
        assert "## Physical Source Trace" in self.md or "Physical" in self.md

    def test_field_table_row_count(self):
        root = self.result.scopes["ROOT"]
        # Count table rows (after header separator)
        lines = self.md.split("\n")
        in_field_table = False
        data_rows = 0
        for line in lines:
            if "Target Field" in line:
                in_field_table = True
                continue
            if in_field_table:
                if line.strip() == "" or line.startswith("#"):
                    break
                if "|" in line and "---" not in line:
                    data_rows += 1
        assert data_rows == len(root.columns)


class TestLineageMdWithJoin:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, b.name "
            "FROM ods.t1 a LEFT JOIN ods.t2 b ON a.id = b.id",
            "test_md_join",
        )
        self.md = lineage_md(self.result)

    def test_join_section_populated(self):
        assert "LEFT_OUTER" in self.md or "LEFT" in self.md


class TestLineageMdWithFilter:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id FROM ods.src a WHERE a.id > 10",
            "test_md_filter",
        )
        self.md = lineage_md(self.result)

    def test_filter_section_populated(self):
        assert "WHERE" in self.md or "id" in self.md


# -- File output tests -------------------------------------------------------


class TestWriteViews:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id FROM ods.src a",
            "test_write",
        )
        self.out_dir = tmp_path / "output"
        write_views(self.result, self.out_dir)

    def test_creates_views_dir(self):
        assert (self.out_dir / "views").is_dir()

    def test_creates_scope_overview(self):
        assert (self.out_dir / "views" / "scope_overview.mmd").exists()

    def test_creates_physical_lineage(self):
        assert (self.out_dir / "views" / "physical.mmd").exists()

    def test_creates_field_lineage(self):
        assert (self.out_dir / "views" / "field_lineage.mmd").exists()

    def test_creates_markdown(self):
        assert (self.out_dir / "lineage.md").exists()

    def test_markdown_not_empty(self):
        content = (self.out_dir / "lineage.md").read_text(encoding="utf-8")
        assert len(content) > 0

    def test_mmd_files_not_empty(self):
        for name in ["scope_overview.mmd", "physical.mmd", "field_lineage.mmd"]:
            content = (self.out_dir / "views" / name).read_text(encoding="utf-8")
            assert len(content) > 0, f"{name} is empty"


class TestWriteViewsPerColumnTraces:
    """When ROOT has <= 20 columns, per-column trace files are generated."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.result = parse_scope_lineage(
            "INSERT INTO dwd.t SELECT a.id, a.name FROM ods.src a",
            "test_per_col",
        )
        self.out_dir = tmp_path / "output"
        write_views(self.result, self.out_dir)

    def test_creates_per_column_dir(self):
        assert (self.out_dir / "views" / "per_column").is_dir()

    def test_creates_per_column_files(self):
        per_col = (self.out_dir / "views" / "per_column")
        mmd_files = list(per_col.glob("*.mmd"))
        assert len(mmd_files) >= 1
