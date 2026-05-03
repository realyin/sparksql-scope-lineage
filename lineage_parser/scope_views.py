"""Visualization and query layer for scope-based lineage results.

Generates Mermaid diagrams, Markdown reports, and query functions
from a ScopeLineageResult. All functions are pure — they return strings
or data structures, no I/O. Use write_views() for file output.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .scope_types import (
    ScopeColumn,
    ScopeData,
    ScopeLineageResult,
    SourceRef,
)

# -- Visual encoding constants (design doc Section 4.3) ----------------------

EDGE_STYLE: Dict[str, Tuple[str, str]] = {
    # transform -> (mermaid_arrow, default_label)
    "DIRECT": ("-->", ""),
    "EXPRESSION": ("-.->", "f(x)"),
    "AGGREGATE": ("==>", "Σ"),
    "WINDOW": ("==>", "win"),
    "CONDITIONAL": ("-.->", "?"),
    "CONSTANT": ("-.->", "="),
    "UNION": ("-->", "∪"),
    "EXPAND_ALL": ("==>", "*"),
}

NODE_COLORS: Dict[str, Tuple[str, str]] = {
    # kind -> (fill, stroke)
    "physical_table": ("#e1f5ff", "#0277bd"),
    "root": ("#c8e6c9", "#2e7d32"),
    "cte": ("#fff9c4", "#f57f17"),
    "union": ("#f3e5f5", "#6a1b9a"),
    "union_branch": ("#fce4ec", "#c2185b"),
    "subquery": ("#e8f5e9", "#388e3c"),
}

_TRANSFORM_PRIORITY: Dict[str, int] = {
    "CONSTANT": 0, "DIRECT": 1, "EXPAND_ALL": 2, "UNION": 3,
    "EXPRESSION": 4, "CONDITIONAL": 5, "WINDOW": 6, "AGGREGATE": 7,
}

# -- Utility functions -------------------------------------------------------


def safe_id(text: str) -> str:
    """Convert text to a Mermaid-safe node ID.

    Replaces all non-alphanumeric characters (except underscore) with underscore.
    """
    return re.sub(r"[^a-zA-Z0-9_]", "_", text)


def column_node_id(scope_id: str, col_name: str, suffix: str = "") -> str:
    """Generate a unique Mermaid node ID for a column within a scope."""
    base = f"{safe_id(scope_id)}__{safe_id(col_name)}"
    return f"{base}__{suffix}" if suffix else base


def _mermaid_escape(text: str, max_len: int = 0) -> str:
    """Escape text for safe use inside Mermaid label strings.
    max_len=0 means no truncation."""
    text = text.replace('"', "'")
    text = text.replace("[", "(").replace("]", ")")
    text = text.replace("{", "(").replace("}", ")")
    # Parentheses are special in Mermaid (node shape syntax) — escape them
    text = text.replace("(", "（").replace(")", "）")
    text = text.replace("|", "∥")
    text = text.replace("\n", " ")
    if max_len and len(text) > max_len:
        text = text[:max_len - 3] + "..."
    return text


def _edge_arrow(transform: str, label: str = "") -> str:
    """Build a Mermaid edge arrow string from transform type."""
    style = EDGE_STYLE.get(transform, ("-->", ""))
    arrow_base, default_label = style
    display_label = label or default_label
    if display_label:
        return f"{arrow_base}|{_mermaid_escape(display_label)}|"
    return arrow_base


def _scope_kind_for_node(scope_id: str, result: ScopeLineageResult) -> str:
    """Determine the visual kind for a scope node."""
    if scope_id not in result.scopes:
        return "physical_table"
    return result.scopes[scope_id].kind


def _more_significant_transform(a: str, b: str) -> str:
    """Return the transform with higher priority (more complex)."""
    pa = _TRANSFORM_PRIORITY.get(a, 0)
    pb = _TRANSFORM_PRIORITY.get(b, 0)
    return a if pa >= pb else b


def _short_table_name(fq: str) -> str:
    """Show last 2 parts of a fully qualified table name."""
    parts = fq.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else fq


# -- Query functions ---------------------------------------------------------


def _trace_field_bfs(
    result: ScopeLineageResult,
    target_scope: str,
    target_col: str,
) -> Tuple[Set[Tuple[str, str]], List[Tuple[str, str, str, str, str]]]:
    """BFS reverse trace from (target_scope, target_col) to physical sources.

    Returns:
        visited: set of (scope_id, col_name) reached
        edges: list of (from_scope, from_col, to_scope, to_col, transform)
    """
    queue: deque = deque([(target_scope, target_col)])
    visited: Set[Tuple[str, str]] = set()
    edges: List[Tuple[str, str, str, str, str]] = []

    while queue:
        s, c = queue.popleft()
        if (s, c) in visited:
            continue
        visited.add((s, c))

        if s not in result.scopes:
            continue  # Physical table — terminal

        scope_data = result.scopes[s]
        for col in scope_data.columns:
            if col.name == c:
                for src in col.sources:
                    edges.append((src.scope, src.column, s, c, col.transform))
                    queue.append((src.scope, src.column))
                break

    return visited, edges


def trace_to_physical(
    result: ScopeLineageResult,
    scope_id: str,
    col_name: str,
) -> List[Tuple[str, str, str]]:
    """Recursively trace a column to its physical sources.

    Returns list of (physical_scope, physical_col, dominant_transform).
    Penetrates through CTEs, subqueries, and UNION scopes.
    """
    return _trace_to_physical_impl(result, scope_id, col_name, "DIRECT", frozenset())


def _trace_to_physical_impl(
    result: ScopeLineageResult,
    scope_id: str,
    col_name: str,
    incoming_transform: str,
    visited: frozenset,
) -> List[Tuple[str, str, str]]:
    key = (scope_id, col_name)
    if key in visited:
        return []  # cycle detected

    if scope_id not in result.scopes:
        return [(scope_id, col_name, incoming_transform)]

    visited = visited | {key}
    scope_data = result.scopes[scope_id]
    matched_col = None
    wildcard_col = None
    for col in scope_data.columns:
        if col.name == col_name:
            matched_col = col
            break
        if col.name == "*":
            wildcard_col = col

    effective_col = matched_col or wildcard_col
    if effective_col is None:
        return []

    dominant = _more_significant_transform(effective_col.transform, incoming_transform)
    result_list = []
    for src in effective_col.sources:
        src_col = src.column if matched_col else col_name
        result_list.extend(
            _trace_to_physical_impl(result, src.scope, src_col, dominant, visited)
        )
    return result_list


def upstream(
    result: ScopeLineageResult,
    scope_id: str,
    col_name: str,
) -> dict:
    """Return all physical sources for a given column.

    Returns dict with: target, physical_sources, edges, path_length.
    """
    visited, edges = _trace_field_bfs(result, scope_id, col_name)
    physical = [(s, c) for s, c in visited if s not in result.scopes]
    return {
        "target": (scope_id, col_name),
        "physical_sources": physical,
        "edges": edges,
        "path_length": len(visited),
    }


def downstream(
    result: ScopeLineageResult,
    physical_table: str,
    physical_col: str,
) -> list:
    """Return all ROOT columns affected by a given physical column."""
    affected = []
    root = result.scopes.get("ROOT")
    if root is None:
        return affected

    seen = set()
    for col in root.columns:
        physicals = trace_to_physical(result, "ROOT", col.name)
        for phys_scope, phys_col, _ in physicals:
            if phys_scope == physical_table and phys_col == physical_col:
                if col.name not in seen:
                    seen.add(col.name)
                    affected.append(col.name)
                break

    return affected


# -- Mermaid diagram generators ----------------------------------------------


def scope_overview_mmd(result: ScopeLineageResult) -> str:
    """Generate a Mermaid diagram showing the scope-level DAG."""
    lines = ["graph TD"]
    style_lines = []
    emitted_nodes = set()

    # Emit nodes
    for node in result.scope_graph.nodes:
        nid = safe_id(node)
        if nid in emitted_nodes:
            continue
        emitted_nodes.add(nid)

        kind = _scope_kind_for_node(node, result)
        if kind == "physical_table":
            label = _short_table_name(node)
            lines.append(f'    {nid}[("{_mermaid_escape(label)}")]')
        elif node == "ROOT":
            target = result.target_table or "?"
            label = f"ROOT → {_short_table_name(target)}"
            lines.append(f'    {nid}["{_mermaid_escape(label)}"]')
            style_lines.append(f"style {nid} fill:#c8e6c9,stroke:#2e7d32,stroke-width:3px")
        else:
            sd = result.scopes.get(node)
            kind_label = (sd.kind or "scope").upper()
            alias = sd.alias_in_parent if sd else None
            if alias and re.search(r'_\d+$', node):
                label = f"{kind_label}: {alias} ({node})"
            else:
                label = f"{kind_label}: {alias or node}"
            lines.append(f'    {nid}["{_mermaid_escape(label)}"]')

            fill, stroke = NODE_COLORS.get(kind, ("#f5f5f5", "#999"))
            style_lines.append(f"style {nid} fill:{fill},stroke:{stroke}")

    # Emit edges
    for edge in result.scope_graph.edges:
        from_id = safe_id(edge.from_)
        to_id = safe_id(edge.to)
        lines.append(f"    {from_id} --> {to_id}")

    lines.append("")
    lines.extend(style_lines)
    return "\n".join(lines) + "\n"


def physical_lineage_mmd(result: ScopeLineageResult) -> str:
    """Generate a Mermaid diagram: physical table columns → ROOT columns."""
    lines = ["graph TD"]
    style_lines = []
    root = result.scopes.get("ROOT")
    if root is None:
        return "graph TD\n"

    # Collect physical table columns
    physical_cols: Dict[str, Set[str]] = {}
    root_traces: List[Tuple[str, str, str, List[Tuple[str, str, str]]]] = []
    # (col_name, suffix, transform, [(phys_scope, phys_col, phys_transform)])

    for col in root.columns:
        suffix = ""
        if col.merge_branch:
            suffix = col.merge_branch
        physicals = trace_to_physical(result, "ROOT", col.name)
        # Use ROOT column's transform if no physical trace (constant)
        dominant = col.transform
        if physicals:
            dominant = physicals[0][2]  # dominant from trace
        root_traces.append((col.name, suffix, dominant, physicals))

        for ps, pc, _ in physicals:
            physical_cols.setdefault(ps, set()).add(pc)

    # Emit physical table subgraphs
    for table in sorted(physical_cols.keys()):
        tid = safe_id(table)
        label = _short_table_name(table)
        lines.append(f'    subgraph {tid}["{label}"]')
        for col_name in sorted(physical_cols[table]):
            cnid = column_node_id(table, col_name)
            lines.append(f'        {cnid}[("{col_name}")]')
        lines.append("    end")
        style_lines.append(f"style {tid} fill:#e1f5ff,stroke:#0277bd")

    # Emit ROOT subgraph
    lines.append('    subgraph ROOT["ROOT"]')
    for col_name, suffix, _, _ in root_traces:
        cnid = column_node_id("ROOT", col_name, suffix)
        display = col_name
        if suffix:
            display = f"{col_name} ({suffix})"
        lines.append(f'        {cnid}["{display}"]')
    lines.append("    end")

    # Emit edges
    for col_name, suffix, dominant, physicals in root_traces:
        to_id = column_node_id("ROOT", col_name, suffix)
        for ps, pc, pt in physicals:
            from_id = column_node_id(ps, pc)
            arrow = _edge_arrow(dominant)
            lines.append(f"    {from_id} {arrow} {to_id}")

    # Style ROOT target columns
    for col_name, suffix, _, _ in root_traces:
        cnid = column_node_id("ROOT", col_name, suffix)
        style_lines.append(f"style {cnid} fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px")

    lines.append("")
    lines.extend(style_lines)
    return "\n".join(lines) + "\n"


def single_field_trace_mmd(
    result: ScopeLineageResult,
    scope_id: str,
    col_name: str,
) -> str:
    """Generate a Mermaid diagram tracing one column to its physical sources."""
    visited, edges = _trace_field_bfs(result, scope_id, col_name)
    if not visited:
        return "graph TD\n"

    lines = ["graph TD"]
    style_lines = []

    # Group visited nodes by scope
    scope_cols: Dict[str, Set[str]] = {}
    for s, c in visited:
        scope_cols.setdefault(s, set()).add(c)

    # Emit subgraphs
    for s in _topological_scope_order(scope_cols.keys(), result):
        cols = scope_cols[s]
        sid = safe_id(s)
        kind = _scope_kind_for_node(s, result)

        if kind == "physical_table":
            label = _short_table_name(s)
            lines.append(f'    subgraph {sid}["{label}"]')
            for c in sorted(cols):
                cnid = column_node_id(s, c)
                lines.append(f'        {cnid}[("{c}")]')
            lines.append("    end")
            style_lines.append(f"style {sid} fill:#e1f5ff,stroke:#0277bd")
        else:
            sd = result.scopes.get(s)
            kind_label = (sd.kind if sd else "scope").upper()
            alias = sd.alias_in_parent if sd else None
            if s == "ROOT":
                display = "ROOT"
            elif alias and re.search(r'_\d+$', s):
                display = f"{kind_label}: {alias} ({s})"
            else:
                display = f"{kind_label}: {alias or s}"
            lines.append(f'    subgraph {sid}["{display}"]')
            for c in sorted(cols):
                cnid = column_node_id(s, c)
                lines.append(f'        {cnid}["{c}"]')
            lines.append("    end")

            fill, stroke = NODE_COLORS.get(kind, ("#f5f5f5", "#999"))
            style_lines.append(f"style {sid} fill:{fill},stroke:{stroke}")
            if s == "ROOT":
                style_lines.append(f"style {sid} fill:#c8e6c9,stroke:#2e7d32,stroke-width:3px")

    # Emit edges
    for from_s, from_c, to_s, to_c, transform in edges:
        from_nid = column_node_id(from_s, from_c)
        to_nid = column_node_id(to_s, to_c)
        arrow = _edge_arrow(transform)
        lines.append(f"    {from_nid} {arrow} {to_nid}")

    lines.append("")
    lines.extend(style_lines)
    return "\n".join(lines) + "\n"


def field_lineage_mmd(result: ScopeLineageResult) -> str:
    """Generate a complete field-level lineage diagram with subgraphs per scope."""
    lines = ["graph TD"]
    style_lines = []
    extra_scope_cols: Dict[str, Set[str]] = {}

    # Collect physical table columns from all SourceRefs
    physical_cols: Dict[str, Set[str]] = {}
    for scope_id, scope_data in result.scopes.items():
        for col in scope_data.columns:
            for src in col.sources:
                if src.scope not in result.scopes:
                    physical_cols.setdefault(src.scope, set()).add(src.column)
                else:
                    src_scope = result.scopes[src.scope]
                    if not any(c.name == src.column for c in src_scope.columns):
                        extra_scope_cols.setdefault(src.scope, set()).add(src.column)

    # Build ordered scope list: physical first, then topological
    all_scopes = list(result.scopes.keys())
    ordered = _topological_scope_order(all_scopes, result)

    # Emit physical table subgraphs first
    for table in sorted(physical_cols.keys()):
        tid = safe_id(table)
        label = _short_table_name(table)
        lines.append(f'    subgraph {tid}["{label}"]')
        for col_name in sorted(physical_cols[table]):
            cnid = column_node_id(table, col_name)
            lines.append(f'        {cnid}[("{col_name}")]')
        lines.append("    end")
        style_lines.append(f"style {tid} fill:#e1f5ff,stroke:#0277bd")

    # Emit scope subgraphs
    for scope_id in ordered:
        sd = result.scopes[scope_id]
        sid = safe_id(scope_id)
        kind = sd.kind

        if scope_id == "ROOT":
            target = result.target_table or ""
            label = f"ROOT → {_short_table_name(target)}" if target else "ROOT"
        elif kind == "cte":
            label = f"CTE: {sd.alias_in_parent or scope_id}"
        elif kind == "union":
            label = f"UNION: {sd.set_op or 'UNION'}"
        elif kind == "union_branch":
            label = f"Branch {sd.branch_index}"
        else:
            label = f"{kind.upper()}: {sd.alias_in_parent or scope_id}"

        lines.append(f'    subgraph {sid}["{label}"]')

        for col in sd.columns:
            suffix = col.merge_branch or ""
            cnid = column_node_id(scope_id, col.name, suffix)
            display = col.name
            if suffix:
                display = f"{col.name} ({suffix})"
            if kind == "union_branch":
                lines.append(f'        {cnid}["{display}"]')
            else:
                lines.append(f'        {cnid}["{display}"]')

        for extra_col in sorted(extra_scope_cols.get(scope_id, set())):
            cnid = column_node_id(scope_id, extra_col)
            lines.append(f'        {cnid}["{extra_col}"]')

        lines.append("    end")

        fill, stroke = NODE_COLORS.get(kind, ("#f5f5f5", "#999"))
        style_lines.append(f"style {sid} fill:{fill},stroke:{stroke}")
        if scope_id == "ROOT":
            style_lines.append(f"style {sid} fill:#c8e6c9,stroke:#2e7d32,stroke-width:3px")

    # Emit edges from all scope columns
    for scope_id, scope_data in result.scopes.items():
        for col in scope_data.columns:
            suffix = col.merge_branch or ""
            to_nid = column_node_id(scope_id, col.name, suffix)
            for src in col.sources:
                from_nid = column_node_id(src.scope, src.column)
                arrow = _edge_arrow(col.transform)
                lines.append(f"    {from_nid} {arrow} {to_nid}")

    # Style ROOT column nodes with bold border
    root = result.scopes.get("ROOT")
    if root:
        for col in root.columns:
            suffix = col.merge_branch or ""
            cnid = column_node_id("ROOT", col.name, suffix)
            style_lines.append(f"style {cnid} fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px")

    lines.append("")
    lines.extend(style_lines)
    return "\n".join(lines) + "\n"


# -- Markdown report ---------------------------------------------------------


def lineage_md(result: ScopeLineageResult) -> str:
    """Generate a structured Markdown lineage report."""
    sections = []
    root = result.scopes.get("ROOT")
    root_cols = root.columns if root else []

    # 1. Header
    sections.append(f"# Lineage: {result.task_id}")

    # 2. Summary
    sections.append("## Summary")
    sections.append(f"- **Target**: `{result.target_table}`")
    sections.append(f"- **Statement**: {result.stmt_kind}")
    sections.append(f"- **Source tables**: {len(result.source_tables)}")
    for t in result.source_tables:
        sections.append(f"  - `{t}`")
    sections.append(f"- **Scopes**: {len(result.scopes)}")
    sections.append(f"- **Target columns**: {len(root_cols)}")
    if result.diagnostics.fallback_used:
        sections.append("- **Fallback**: qualify failed, used unqualified AST")

    # 3. Scope overview (embedded Mermaid)
    sections.append("")
    sections.append("## Scope Overview")
    sections.append("```mermaid")
    sections.append(scope_overview_mmd(result).rstrip())
    sections.append("```")

    # 4. Field lineage table
    sections.append("")
    sections.append("## Field Lineage")
    if root_cols:
        sections.append("| Target Field | Transform | Upstream Sources | Expression |")
        sections.append("|---|---|---|---|")
        for col in root_cols:
            src_strs = [f"`{s.scope}.{s.column}`" for s in col.sources]
            src_display = ", ".join(src_strs) if src_strs else "—"
            expr = f"`{_md_escape(col.expression or '')}`" if col.expression and col.transform != "DIRECT" else ""
            branch = f" [{col.merge_branch}]" if col.merge_branch else ""
            sections.append(
                f"| {col.name}{branch} | {col.transform} | {src_display} | {expr} |"
            )

    # 5. Key computation logic
    non_direct = [c for c in root_cols if c.transform not in ("DIRECT", "CONSTANT")]
    if non_direct:
        sections.append("")
        sections.append("## Key Computation Logic")
        for col in non_direct:
            branch = f" ({col.merge_branch})" if col.merge_branch else ""
            sections.append(f"### `{col.name}`{branch} — {col.transform}")
            if col.expression:
                sections.append(f"- Expression: `{_md_escape(col.expression)}`")
            if col.agg_function:
                sections.append(f"- Aggregate: `{col.agg_function}`")
            if col.case_branches:
                sections.append(f"- Case branches: {len(col.case_branches)}")
            if col.window:
                sections.append(f"- Window function")
            if col.sources:
                src_strs = [f"`{s.scope}.{s.column}`" for s in col.sources]
                sections.append(f"- Sources: {', '.join(src_strs)}")

    # 6. Join & filter logic
    join_sections = []
    filter_sections = []
    for sid, sd in result.scopes.items():
        if sd.joins:
            join_sections.append(f"### {sid}")
            for j in sd.joins:
                join_sections.append(
                    f"- **{j.join_type}** `{j.left_scope}` ↔ `{j.right_scope}`"
                    + (f" (alias: `{j.alias_in_parent}`)" if j.alias_in_parent else "")
                )
                if j.condition_expression:
                    join_sections.append(f"  ON: `{_md_escape(j.condition_expression)}`")
        if sd.filters:
            filter_sections.append(f"### {sid}")
            for f in sd.filters:
                cols = [f"`{c.scope}.{c.column}`" for c in f.columns]
                filter_sections.append(f"- `{_md_escape(f.expression)}` ← {', '.join(cols)}")
        if sd.having:
            filter_sections.append(f"### {sid} (HAVING)")
            for h in sd.having:
                cols = [f"`{c.scope}.{c.column}`" for c in h.columns]
                filter_sections.append(f"- `{_md_escape(h.expression)}` ← {', '.join(cols)}")

    if join_sections:
        sections.append("")
        sections.append("## Join Logic")
        sections.extend(join_sections)
    if filter_sections:
        sections.append("")
        sections.append("## Filter Logic")
        sections.extend(filter_sections)

    # 7. Detected concerns
    if result.diagnostics.warnings:
        sections.append("")
        sections.append("## Detected Concerns")
        for w in result.diagnostics.warnings:
            sections.append(f"- **[{w.type}]** (scope: `{w.scope}`) {w.msg}")

    # 8. Physical lineage per column
    if root_cols:
        sections.append("")
        sections.append("## Physical Source Trace")
        for col in root_cols:
            physicals = trace_to_physical(result, "ROOT", col.name)
            branch = f" ({col.merge_branch})" if col.merge_branch else ""
            if physicals:
                src_strs = [f"`{s}.{c}` ({t})" for s, c, t in physicals]
                sections.append(f"- **{col.name}{branch}**: {', '.join(src_strs)}")
            else:
                sections.append(f"- **{col.name}{branch}**: (constant/no source)")

    return "\n".join(sections) + "\n"


# -- File output -------------------------------------------------------------


def write_views(
    result: ScopeLineageResult,
    output_dir: str | Path,
    generate_per_column: bool | None = None,
) -> Path:
    """Write all view files to output_dir.

    Creates:
        views/scope_overview.mmd
        views/field_lineage.mmd
        views/physical.mmd
        lineage.md
        views/per_column/<col_name>.mmd  (if enabled)
    """
    output_dir = Path(output_dir)
    views_dir = output_dir / "views"
    views_dir.mkdir(parents=True, exist_ok=True)

    # Mermaid diagrams
    _write_text(views_dir / "scope_overview.mmd", scope_overview_mmd(result))
    _write_text(views_dir / "field_lineage.mmd", field_lineage_mmd(result))
    _write_text(views_dir / "physical.mmd", physical_lineage_mmd(result))

    # Markdown report
    _write_text(output_dir / "lineage.md", lineage_md(result))

    # Per-column traces
    root = result.scopes.get("ROOT")
    if root is not None:
        n_cols = len(root.columns)
        should_generate = generate_per_column if generate_per_column is not None else True
        if should_generate:
            per_col_dir = views_dir / "per_column"
            per_col_dir.mkdir(parents=True, exist_ok=True)
            seen = set()
            for col in root.columns:
                suffix = col.merge_branch or ""
                fname = col.name if not suffix else f"{col.name}_{suffix}"
                if fname in seen:
                    continue
                seen.add(fname)
                mmd = single_field_trace_mmd(result, "ROOT", col.name)
                _write_text(per_col_dir / f"{safe_id(fname)}.mmd", mmd)

    return output_dir


# -- Internal helpers --------------------------------------------------------


def _topological_scope_order(
    scope_ids: Iterable[str], result: ScopeLineageResult,
) -> List[str]:
    """Sort scope_ids topologically using depends_on. Physical tables first."""
    scope_ids_set = set(scope_ids)
    # Build in-degree map
    in_degree: Dict[str, int] = {s: 0 for s in scope_ids_set}
    for s in scope_ids_set:
        sd = result.scopes.get(s)
        if sd:
            for dep in sd.depends_on:
                if dep in scope_ids_set:
                    in_degree[s] = in_degree.get(s, 0) + 1

    # Kahn's algorithm
    queue = sorted([s for s, d in in_degree.items() if d == 0])
    result_order = []
    while queue:
        node = queue.pop(0)
        result_order.append(node)
        for s in scope_ids_set:
            sd = result.scopes.get(s)
            if sd and node in sd.depends_on:
                in_degree[s] -= 1
                if in_degree[s] == 0:
                    queue.append(s)
        queue.sort()

    # Add any remaining (cycles)
    for s in scope_ids_set:
        if s not in result_order:
            result_order.append(s)

    return result_order


def _write_text(path: Path, content: str) -> None:
    """Write text content to a file with UTF-8 encoding."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _md_escape(text: str) -> str:
    """Escape text for safe use in Markdown."""
    return text.replace("|", "∥").replace("\n", " ")
