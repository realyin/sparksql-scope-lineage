"""Per-scope column resolver: resolve every column reference to SourceRef(scope_id, column_name).

Works on the scope tree built by scope_builder.py. Populates each ScopeData's
columns, joins, filters, group_by, having, order_by, and depends_on.
"""

from __future__ import annotations

from typing import List, Optional

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.scope import Scope

from .parser import (
    _qualified_table,
    _extract_name_inner,
    _normalize_table_name,
    _cte_output_columns,
    _collect_cte_map,
)
from .scope_types import (
    CONSTANT_SCOPE_ID,
    SYSTEM_SCOPE_ID,
    SourceRef,
    ScopeColumn,
    ScopeData,
    ScopeFilter,
    ScopeJoin,
    ScopeGraphEdge,
    ScopeLineageResult,
    DiagnosticWarning,
)

DIALECT = "spark"

# Attribute name on sqlglot Scope objects holding the scope_id
_SCOPE_ID_ATTR = "_lineage_scope_id"

# Known Hive/Spark aggregate functions that sqlglot parses as exp.Anonymous
_KNOWN_UDAFS = frozenset({
    "COLLECT_SET", "COLLECT_LIST", "CONCAT_WS", "PERCENTILE",
    "PERCENTILE_APPROX", "HISTOGRAM_NUMERIC", "NVL",
})


def _constant_sources(expression: str | None) -> list[SourceRef]:
    """Represent a literal as a traceable leaf instead of an empty lineage edge."""
    literal = expression if expression else "<constant>"
    return [SourceRef(scope=CONSTANT_SCOPE_ID, column=literal)]


def _system_sources(expression: str | None) -> list[SourceRef]:
    """Represent runtime/system expressions as traceable non-table leaves."""
    label = expression if expression else "<system>"
    return [SourceRef(scope=SYSTEM_SCOPE_ID, column=label)]


def _is_dependency_scope(scope_id: str | None) -> bool:
    return bool(scope_id and scope_id not in {"UNKNOWN", CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID})


def resolve_all(
    result: ScopeLineageResult,
    root_scope: Scope,
    all_scopes: list,
    schema: dict | None = None,
) -> None:
    """Resolve columns for all scopes in the result.

    Walks the sqlglot scope tree, resolves projections/joins/filters/etc.,
    populates depends_on, and builds scope_graph edges.

    all_scopes is the full list from traverse_scope(qualified_expr), which includes
    CTE scopes that root_scope.traverse() misses for MERGE...WITH statements.
    """
    # Step 1: Resolve all Select-based scopes (root, cte, subquery, union_branch)
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if scope_id is None or scope_id not in result.scopes:
            continue

        scope_data = result.scopes[scope_id]

        if scope_data.kind in ("root", "cte", "subquery", "union_branch"):
            if isinstance(sg_scope.expression, exp.Values):
                _resolve_values_scope(sg_scope, scope_id, scope_data, result)
            elif isinstance(sg_scope.expression, exp.Select):
                _resolve_select_scope(sg_scope, scope_id, scope_data, result, schema)
            elif isinstance(sg_scope.expression, exp.Lateral):
                _resolve_lateral_scope(sg_scope, scope_id, scope_data, result, schema)

    # Step 2: Resolve synthetic UNION scopes in bottom-up order
    # (nested unions must be resolved before their parent union)
    # Iterate until all union scopes have columns (handles arbitrary nesting depth)
    _resolve_union_scopes_bottom_up(result)

    # Step 3: Handle scopes with Union expression (e.g. ROOT, CTE containing UNION)
    # Their columns are a passthrough from the corresponding union scope
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if scope_id is None or scope_id not in result.scopes:
            continue
        scope_data = result.scopes[scope_id]
        if isinstance(sg_scope.expression, exp.Union) and not scope_data.columns:
            _resolve_scope_union_passthrough(scope_id, scope_data, result)

    # Step 3b: Expand wildcard (*) columns into concrete columns where upstream is known.
    # Iterates until stable so that chains like  subq:a.* → subq:aa.* → union:aa.[cols]
    # are fully unrolled.
    _expand_star_columns(result)
    _materialize_referenced_star_columns(result)

    # Step 4: Resolve MERGE columns (special handling)
    if result.stmt_kind == "MERGE":
        _resolve_merge_columns(root_scope, result, schema, all_scopes)
        _materialize_referenced_star_columns(result)

    _dedupe_scope_columns(result)

    # Step 5: Populate depends_on and build scope_graph edges
    _build_depends_on_and_graph(result)


def _resolve_select_scope(
    sg_scope: Scope, scope_id: str, scope_data: ScopeData,
    result: ScopeLineageResult, schema: dict | None = None,
) -> None:
    """Resolve projections, joins, filters, group_by, having, order_by for a Select scope."""
    sel = sg_scope.expression

    # Resolve projections
    for proj in sel.expressions:
        cols = _resolve_projection(proj, sg_scope, result, schema)
        scope_data.columns.extend(cols)

    # Resolve joins
    for join in sel.args.get("joins") or []:
        j = _resolve_join(join, sg_scope, result, schema)
        if j:
            scope_data.joins.append(j)

    # Resolve WHERE
    where = sel.args.get("where")
    if where:
        scope_data.filters = _resolve_filter(where, sg_scope, result, schema)

    # Resolve GROUP BY
    group = sel.args.get("group")
    if group:
        scope_data.group_by = _resolve_expr_list(
            group.expressions if hasattr(group, "expressions") else [group],
            sg_scope, result, schema,
        )

    # Resolve HAVING
    having = sel.args.get("having")
    if having:
        scope_data.having = _resolve_filter(having, sg_scope, result, schema)

    # Resolve ORDER BY
    order = sel.args.get("order")
    if order:
        for item in (order.expressions if hasattr(order, "expressions") else []):
            direction = "DESC" if isinstance(item, exp.Ordered) and item.desc else "ASC"
            expr = item.this if isinstance(item, exp.Ordered) else item
            if isinstance(expr, exp.Column) and not expr.table:
                output_names = {c.name for c in scope_data.columns}
                if expr.name in output_names:
                    scope_data.order_by.append({
                        "scope": scope_id,
                        "column": expr.name,
                        "direction": direction,
                    })
                    continue
            col_refs = _resolve_column_refs_in_expr(item, sg_scope, result, schema)
            for ref in col_refs:
                scope_data.order_by.append({"scope": ref.scope, "column": ref.column, "direction": direction})


def _resolve_values_scope(
    sg_scope: Scope, scope_id: str, scope_data: ScopeData,
    result: ScopeLineageResult,
) -> None:
    """Resolve Spark VALUES (...) AS alias(col1, col2, ...) into named columns."""
    values = sg_scope.expression
    alias = values.args.get("alias") if isinstance(values, exp.Values) else None
    alias_columns = list(alias.columns or []) if alias is not None else []
    rows = list(values.expressions or []) if isinstance(values, exp.Values) else []
    first_row = rows[0].expressions if rows and hasattr(rows[0], "expressions") else []

    if alias_columns:
        names = [c.name if hasattr(c, "name") else str(c) for c in alias_columns]
    else:
        names = [f"_col_{i}" for i in range(len(first_row))]

    for i, name in enumerate(names):
        expr = first_row[i] if i < len(first_row) else None
        expression = expr.sql(dialect=DIALECT) if expr is not None else ""
        transform = _classify_extended(expr) if expr is not None else "CONSTANT"
        scope_data.columns.append(ScopeColumn(
            name=name,
            transform=transform,
            expression=expression,
            sources=_source_free_leaf_sources(expr, expression) if expr is not None else _constant_sources(expression),
        ))


def _selected_sources(sg_scope: Scope) -> dict:
    """Return only sources that participate in the current SELECT FROM/JOIN list."""
    try:
        selected = sg_scope.selected_sources
    except OptimizeError:
        return sg_scope.sources
    if not selected and sg_scope.sources:
        return sg_scope.sources
    return {alias: source for alias, (_node, source) in selected.items()}


def _resolve_projection(
    proj: exp.Expression, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[ScopeColumn]:
    """Resolve a single SELECT projection into one or more ScopeColumns.

    Star projections (a.* / *) are expanded into individual DIRECT columns
    when the source scope has resolved columns or when schema is available.
    Falls back to a single EXPAND_ALL column when expansion is impossible.
    """
    name, inner = _extract_name_inner(proj)
    transform = _classify_extended(inner)
    expression = inner.sql(dialect=DIALECT)

    multi_alias_cols = _resolve_multi_alias_projection(proj, sg_scope, result, schema)
    if multi_alias_cols:
        return multi_alias_cols

    udtf_cols = _resolve_projection_udtf(inner, name, sg_scope, result, schema)
    if udtf_cols:
        return udtf_cols

    # Handle EXPAND_ALL (SELECT * / a.*)
    if isinstance(inner, exp.Star):
        expanded = _expand_star_into_columns(sg_scope, None, result, schema)
        if expanded:
            return expanded
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN")
        result.diagnostics.warnings.append(DiagnosticWarning(
            type="star_not_expanded",
            scope=scope_id,
            msg="SELECT * could not be expanded: no schema and no resolved source columns",
        ))
        return [ScopeColumn(
            name="*", transform="EXPAND_ALL", expression="*",
            sources=_expand_star_sources(sg_scope, None, schema))]
    if isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
        table_alias = inner.table
        expanded = _expand_star_into_columns(sg_scope, table_alias, result, schema)
        if expanded:
            return expanded
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN")
        result.diagnostics.warnings.append(DiagnosticWarning(
            type="star_not_expanded",
            scope=scope_id,
            msg=f"{table_alias}.* could not be expanded: no schema and no resolved source columns",
        ))
        return [ScopeColumn(
            name=f"{table_alias}.*", transform="EXPAND_ALL",
            expression=expression,
            sources=_expand_star_sources(sg_scope, table_alias, schema))]

    # Handle CONSTANT
    if transform == "CONSTANT":
        return [
            ScopeColumn(
                name=name,
                transform="CONSTANT",
                expression=expression,
                sources=_constant_sources(expression),
            )
        ]

    # Find all Column references and resolve them
    sources = _resolve_column_refs_in_expr(inner, sg_scope, result, schema)
    if not sources:
        sources = _fallback_sources_for_source_free_expr(inner, transform, expression, sg_scope, result)

    col = ScopeColumn(name=name, transform=transform, expression=expression, sources=sources)

    # Populate optional fields by transform type
    if transform == "CONDITIONAL":
        col.case_branches = _extract_case_branches(inner)

    if transform == "WINDOW":
        col.window = _extract_window_info(inner, sg_scope, result, schema)

    if transform == "AGGREGATE":
        col.agg_function = _extract_agg_function(inner)

    return [col]


def _fallback_sources_for_source_free_expr(
    inner: exp.Expression,
    transform: str,
    expression: str,
    sg_scope: Scope,
    result: ScopeLineageResult,
) -> list[SourceRef]:
    """Give source-free non-literal expressions a meaningful terminal lineage.

    Examples:
    - COUNT(*) and ROW_NUMBER() depend on the current input row set.
    - NOW(), CURRENT_DATE(), RAND() are runtime/system values.
    - DATE_ADD('2026-04-27', 1) and CONCAT('a', 'b') are literal-derived values.
    """
    if transform in {"AGGREGATE", "WINDOW"}:
        rowset_sources = _rowset_sources(sg_scope, result)
        if rowset_sources:
            return rowset_sources

    return _source_free_leaf_sources(inner, expression)


def _rowset_sources(sg_scope: Scope, result: ScopeLineageResult) -> list[SourceRef]:
    sources: list[SourceRef] = []
    seen = set()
    for alias, source in _selected_sources(sg_scope).items():
        ref = _source_ref_for_source(alias, source, "*", result)
        key = (ref.scope, ref.column)
        if key not in seen:
            seen.add(key)
            sources.append(ref)
    return sources


def _source_free_leaf_sources(inner: exp.Expression, expression: str) -> list[SourceRef]:
    if _contains_runtime_function(inner):
        return _system_sources(expression)
    return _constant_sources(expression)


def _contains_runtime_function(node: exp.Expression) -> bool:
    runtime_names = {
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "CURRENT_TIME",
        "NOW",
        "RAND",
        "RANDOM",
        "UUID",
        "UNIX_TIMESTAMP",
    }
    for expr in node.walk():
        if isinstance(expr, (exp.CurrentDate, exp.CurrentTimestamp, exp.Rand)):
            return True
        if isinstance(expr, exp.Anonymous):
            name = expr.name.upper() if hasattr(expr, "name") else ""
            if name in runtime_names:
                return True
    sql = node.sql(dialect=DIALECT).upper()
    return any(f"{name}(" in sql or name in {"CURRENT_DATE", "CURRENT_TIMESTAMP"} and name in sql for name in runtime_names)


def _resolve_multi_alias_projection(
    proj: exp.Expression,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> list[ScopeColumn]:
    """Resolve SELECT-list table functions shaped like ``func(x) AS (c1, c2)``."""
    if not isinstance(proj, exp.Aliases):
        return []

    inner = proj.this
    if inner is None:
        return []

    names = [
        alias.name if hasattr(alias, "name") else str(alias)
        for alias in (proj.expressions or [])
    ]
    names = [name for name in names if name]
    if not names:
        return []

    sources = _resolve_column_refs_in_expr(inner, sg_scope, result, schema)
    expression = proj.sql(dialect=DIALECT)
    return [
        ScopeColumn(
            name=name,
            transform="EXPRESSION",
            expression=expression,
            sources=list(sources),
        )
        for name in names
    ]


def _resolve_projection_udtf(
    inner: exp.Expression,
    output_name: str,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> list[ScopeColumn]:
    """Resolve generator functions used as SELECT projections.

    Spark/Hive allow ``SELECT posexplode(arr)`` without an explicit alias. The
    engine exposes default columns (``pos``, ``col``), and downstream SQL often
    references those names directly.
    """
    if isinstance(inner, exp.Posexplode):
        names = ["pos", "col"]
    elif isinstance(inner, exp.Explode):
        names = ["col"] if output_name.startswith("_col_") else [output_name]
    else:
        return []

    sources = _resolve_column_refs_in_expr(inner, sg_scope, result, schema)
    return [
        ScopeColumn(
            name=name,
            transform="EXPRESSION",
            expression=inner.sql(dialect=DIALECT),
            sources=list(sources),
        )
        for name in names
    ]


def _resolve_lateral_scope(
    sg_scope: Scope,
    scope_id: str,
    scope_data: ScopeData,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> None:
    """Resolve LATERAL VIEW generator output columns."""
    lateral = sg_scope.expression
    alias = lateral.args.get("alias")
    alias_columns = list(alias.columns or []) if alias is not None else []
    names = [c.name if hasattr(c, "name") else str(c) for c in alias_columns]

    inner = lateral.this
    if not names:
        names = _infer_lateral_output_names(inner)
    if not names:
        return

    if inner is not None:
        scope_data.lateral_views.append({
            "alias": _lateral_alias_name(alias, scope_id),
            "function": _lateral_function_name(inner),
            "expression": _compact_sql(inner),
            "output_columns": list(names),
        })

    sources = _resolve_column_refs_in_expr(inner, sg_scope, result, schema)
    for name in names:
        scope_data.columns.append(ScopeColumn(
            name=name,
            transform="EXPRESSION",
            expression=inner.sql(dialect=DIALECT) if inner is not None else "",
            sources=list(sources),
        ))


def _lateral_alias_name(alias: exp.Expression | None, scope_id: str) -> str:
    if alias is not None and alias.this is not None:
        return alias.this.name if hasattr(alias.this, "name") else str(alias.this)
    if ":" in scope_id:
        return scope_id.split(":", 1)[1]
    return scope_id


def _compact_sql(expression: exp.Expression) -> str:
    return expression.sql(dialect=DIALECT).replace("`", "")


def _lateral_function_name(inner: exp.Expression) -> str:
    if isinstance(inner, exp.Posexplode):
        return "POSEXPLODE"
    if isinstance(inner, exp.Explode):
        return "EXPLODE"
    if isinstance(inner, exp.Inline):
        return "INLINE"
    key = getattr(inner, "key", "") or inner.__class__.__name__
    return str(key).upper()


def _infer_lateral_output_names(inner: exp.Expression | None) -> list[str]:
    if isinstance(inner, exp.Posexplode):
        return ["pos", "col"]
    if isinstance(inner, exp.Explode):
        return ["col"]
    if isinstance(inner, exp.Inline):
        names = _field_names_from_from_json_schema(inner)
        if names:
            return names
    return []


def _field_names_from_from_json_schema(expr: exp.Expression) -> list[str]:
    for func in expr.find_all(exp.Anonymous):
        if str(func.this).lower() != "from_json":
            continue
        args = list(func.expressions or [])
        if len(args) < 2 or not isinstance(args[1], exp.Literal):
            continue
        names = _extract_struct_field_names(args[1].this or "")
        if names:
            return names
    return []


def _extract_struct_field_names(schema_text: str) -> list[str]:
    marker = "array<struct<"
    lower = schema_text.lower()
    start = lower.rfind(marker)
    if start < 0:
        start = lower.find("struct<")
        if start < 0:
            return []
        start += len("struct<")
    else:
        start += len(marker)

    depth = 0
    fields = []
    token = []
    for ch in schema_text[start:]:
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            fields.append("".join(token).strip())
            token = []
        else:
            token.append(ch)
    if token:
        fields.append("".join(token).strip())

    names = []
    for field in fields:
        name = field.split(":", 1)[0].strip().strip("`")
        if name:
            names.append(name.lower())
    return names


def _resolve_column_refs_in_expr(
    expr: exp.Expression, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[SourceRef]:
    """Find all exp.Column references in an expression and resolve to SourceRefs.

    Deduplicates by (scope, column) tuple.
    """
    seen: set = set()
    sources: list = []

    for col_ref in expr.find_all(exp.Column):
        if _inside_nested_query(col_ref, expr):
            continue
        src = _resolve_column_ref(col_ref, sg_scope, result, schema)
        if src and (src.scope, src.column) not in seen:
            seen.add((src.scope, src.column))
            sources.append(src)

    return sources


def _inside_nested_query(col_ref: exp.Column, root_expr: exp.Expression) -> bool:
    """Return True when a column belongs to a nested query inside root_expr."""
    if col_ref is root_expr:
        return False
    parent = col_ref.parent
    while parent is not None and parent is not root_expr:
        if isinstance(parent, (exp.Select, exp.Union)):
            return True
        parent = parent.parent
    return False


def _resolve_column_ref(
    col_ref: exp.Column, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> SourceRef | None:
    """Resolve a single exp.Column to a SourceRef(scope_id, column_name).

    Decision tree:
      1. Qualified (col_ref.table set): look up in sg_scope.sources
      2. Unqualified: search Scope sources first, then Table sources
      3. Not found: return SourceRef("UNKNOWN", col_name) + warning
    """
    table_alias = col_ref.table
    col_name = col_ref.name

    struct_ref = _resolve_struct_field_ref(col_ref, sg_scope)
    if struct_ref:
        return struct_ref

    if table_alias:
        # Qualified: t1.col
        duplicate_src = _resolve_duplicate_alias_ref(table_alias, col_name, sg_scope, result, schema)
        if duplicate_src is not None:
            return duplicate_src

        src = sg_scope.sources.get(table_alias)
        if isinstance(src, Scope):
            upstream_id = getattr(src, _SCOPE_ID_ATTR, None)
            if upstream_id:
                return SourceRef(scope=upstream_id, column=col_name)
        elif isinstance(src, exp.Table):
            return SourceRef(scope=_qualified_table(src), column=col_name)

        # Alias not found in sources — check selected_sources
        # Guard: duplicate aliases in SQL cause OptimizeError from sqlglot
        try:
            _sel = sg_scope.selected_sources
        except OptimizeError:
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="duplicate_alias",
                scope=getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN"),
                msg=(
                    f"scope.selected_sources raised OptimizeError while resolving alias '{table_alias}' "
                    f"— likely caused by a duplicate subquery alias in the FROM clause. "
                    f"Column resolution skipped."
                ),
            ))
            return SourceRef(scope="UNKNOWN", column=col_name)
        sel_src = _sel.get(table_alias)
        if sel_src:
            _node, source = sel_src
            if isinstance(source, Scope):
                upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
                if upstream_id:
                    return SourceRef(scope=upstream_id, column=col_name)
            elif isinstance(source, exp.Table):
                return SourceRef(scope=_qualified_table(source), column=col_name)

        parent_src = _lookup_alias_in_parent_scopes(table_alias, sg_scope)
        if parent_src:
            source = parent_src
            if isinstance(source, Scope):
                upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
                if upstream_id:
                    return SourceRef(scope=upstream_id, column=col_name)
            elif isinstance(source, exp.Table):
                return SourceRef(scope=_qualified_table(source), column=col_name)

        # Still not found — could be the target table in MERGE (t.col)
        result.diagnostics.warnings.append(DiagnosticWarning(
            type="unresolved_alias",
            scope=getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN"),
            msg=f"Alias '{table_alias}' not found in scope sources",
        ))
        return SourceRef(scope="UNKNOWN", column=col_name)

    else:
        # Unqualified: search through sources
        return _resolve_unqualified(col_name, sg_scope, result, schema)


def _resolve_struct_field_ref(col_ref: exp.Column, sg_scope: Scope) -> SourceRef | None:
    """Resolve ``alias.struct_col.field`` as lineage from ``alias.struct_col``."""
    parts = [p.name if hasattr(p, "name") else str(p) for p in (col_ref.parts or [])]
    if len(parts) < 3:
        return None

    source_alias = parts[0]
    base_column = parts[1]
    source = _lookup_source_in_scope_chain(source_alias, sg_scope)
    if source is None:
        return None

    if isinstance(source, Scope):
        upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
        if upstream_id:
            return SourceRef(scope=upstream_id, column=base_column)
    if isinstance(source, exp.Table):
        return SourceRef(scope=_qualified_table(source), column=base_column)
    return None


def _lookup_source_in_scope_chain(alias: str, sg_scope: Scope) -> Scope | exp.Table | None:
    """Find a source alias in the current scope or an ancestor scope."""
    src = sg_scope.sources.get(alias)
    if isinstance(src, (Scope, exp.Table)):
        return src
    try:
        selected = sg_scope.selected_sources.get(alias)
    except OptimizeError:
        selected = None
    if selected:
        _node, source = selected
        if isinstance(source, (Scope, exp.Table)):
            return source
    return _lookup_alias_in_parent_scopes(alias, sg_scope)


def _lookup_alias_in_parent_scopes(table_alias: str, sg_scope: Scope) -> Scope | exp.Table | None:
    """Find a correlated reference alias in ancestor scopes."""
    parent = sg_scope.parent
    while parent is not None:
        src = parent.sources.get(table_alias)
        if isinstance(src, (Scope, exp.Table)):
            return src
        try:
            sel_src = parent.selected_sources.get(table_alias)
        except OptimizeError:
            sel_src = None
        if sel_src:
            _node, source = sel_src
            if isinstance(source, (Scope, exp.Table)):
                return source
        parent = parent.parent
    return None


def _resolve_unqualified(
    col_name: str, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> SourceRef:
    """Resolve an unqualified column reference.

    Decision tree:
      1. Search Scope sources (CTE, subquery) — check output columns
      2. Search Table sources (physical tables) — use schema if available
      3. Not found → SourceRef("UNKNOWN", col_name) + warning
    """
    # Step 1: Search Scope sources
    exact_scope_candidates = []
    star_scope_candidates = []
    scope_sources = []
    for alias, source in _selected_sources(sg_scope).items():
        if isinstance(source, Scope):
            scope_sources.append((alias, source))
            upstream_id = _source_scope_id(alias, source, result)
            upstream_sd = result.scopes.get(upstream_id) if upstream_id else None
            if upstream_sd and any(c.name == col_name for c in upstream_sd.columns):
                exact_scope_candidates.append((alias, source))
                continue
            inner_expr = source.expression
            if isinstance(inner_expr, exp.Select):
                output_names = inner_expr.named_selects
                # Also match scopes that project SELECT * — they pass through any column
                has_star = _select_has_star_projection(inner_expr)
                if col_name in output_names or has_star:
                    star_scope_candidates.append((alias, source))

    if len(exact_scope_candidates) == 1:
        _alias, source = exact_scope_candidates[0]
        upstream_id = _source_scope_id(_alias, source, result)
        if upstream_id:
            return SourceRef(scope=upstream_id, column=col_name)
    elif len(exact_scope_candidates) > 1:
        _alias, source = exact_scope_candidates[0]
        upstream_id = _source_scope_id(_alias, source, result)
        if upstream_id:
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="ambiguous_unqualified",
                scope=getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN"),
                msg=f"Unqualified column '{col_name}' found in multiple scopes, using first",
            ))
            return SourceRef(scope=upstream_id, column=col_name)

    # Some Spark queries reuse the same lateral-view table alias for multiple
    # generators. sqlglot may keep only one of them in selected_sources, so do
    # a narrow exact-name search across all scope sources before falling back to
    # physical tables or UNKNOWN.
    all_scope_candidates = []
    for alias, source in sg_scope.sources.items():
        if not isinstance(source, Scope):
            continue
        upstream_id = _source_scope_id(alias, source, result)
        upstream_sd = result.scopes.get(upstream_id) if upstream_id else None
        if upstream_sd and any(c.name == col_name for c in upstream_sd.columns):
            all_scope_candidates.append((alias, source))

    if len(all_scope_candidates) == 1:
        alias, source = all_scope_candidates[0]
        upstream_id = _source_scope_id(alias, source, result)
        if upstream_id:
            return SourceRef(scope=upstream_id, column=col_name)

    udtf_candidates = []
    for source in getattr(sg_scope, "udtf_scopes", []) or []:
        upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
        upstream_sd = result.scopes.get(upstream_id) if upstream_id else None
        if upstream_sd and any(c.name == col_name for c in upstream_sd.columns):
            udtf_candidates.append(upstream_id)

    if len(udtf_candidates) == 1:
        return SourceRef(scope=udtf_candidates[0], column=col_name)

    # Step 2: Search Table sources
    first_missing_schema_table: tuple[str, exp.Table] | None = None
    for alias, source in _selected_sources(sg_scope).items():
        if isinstance(source, exp.Table):
            fq = _qualified_table(source)
            if schema:
                norm = _normalize_table_name(fq)
                if norm not in schema and first_missing_schema_table is None:
                    first_missing_schema_table = (alias, source)
                if col_name in schema.get(norm, []):
                    return SourceRef(scope=fq, column=col_name)
            else:
                # No schema — pick first Table source + warning
                result.diagnostics.warnings.append(DiagnosticWarning(
                    type="unresolved_unqualified_no_schema",
                    scope=getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN"),
                    msg=f"Unqualified column '{col_name}' resolved to first Table source without schema",
                ))
                return SourceRef(scope=fq, column=col_name)

    if schema and first_missing_schema_table is not None:
        _alias, source = first_missing_schema_table
        fq = _qualified_table(source)
        result.diagnostics.warnings.append(DiagnosticWarning(
            type="unresolved_unqualified_no_schema",
            scope=getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN"),
            msg=(
                f"Unqualified column '{col_name}' resolved to first Table source "
                f"because schema metadata does not contain that table"
            ),
        ))
        return SourceRef(scope=fq, column=col_name)

    if len(star_scope_candidates) == 1:
        _alias, source = star_scope_candidates[0]
        upstream_id = _source_scope_id(_alias, source, result)
        if upstream_id:
            return SourceRef(scope=upstream_id, column=col_name)
    elif len(star_scope_candidates) > 1:
        _alias, source = star_scope_candidates[0]
        upstream_id = _source_scope_id(_alias, source, result)
        if upstream_id:
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="ambiguous_unqualified",
                scope=getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN"),
                msg=f"Unqualified column '{col_name}' found in multiple star scopes, using first",
            ))
            return SourceRef(scope=upstream_id, column=col_name)

    if len(scope_sources) == 1:
        alias, source = scope_sources[0]
        upstream_id = _source_scope_id(alias, source, result)
        if upstream_id and not getattr(source, "is_udtf", False):
            return SourceRef(scope=upstream_id, column=col_name)

    # Step 3: Not found
    result.diagnostics.warnings.append(DiagnosticWarning(
        type="column_not_found",
        scope=getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN"),
        msg=f"Column '{col_name}' not found in any source",
    ))
    return SourceRef(scope="UNKNOWN", column=col_name)


def _resolve_duplicate_alias_ref(
    table_alias: str,
    col_name: str,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> SourceRef | None:
    """Resolve qualified references when one SELECT reuses the same alias.

    ``sqlglot`` stores sources in a dict. If the same alias appears twice in the
    same FROM/JOIN list, that dict can only keep one binding. For lineage, a
    silent overwrite is worse than an explicit diagnostic, so we inspect the
    SELECT's FROM/JOIN AST in order and disambiguate by known output columns or
    schema metadata.
    """
    candidates = [
        (alias, source)
        for alias, source in _iter_select_sources_in_order(sg_scope)
        if alias == table_alias
    ]
    if len(candidates) <= 1:
        return None

    scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN")
    states = [
        (alias, source, _source_column_state(alias, source, col_name, result, schema))
        for alias, source in candidates
    ]
    exact = [(alias, source) for alias, source, state in states if state == "present"]
    possible = [(alias, source) for alias, source, state in states if state == "unknown"]

    selected: tuple[str, Scope | exp.Table] | None = None
    reason = ""
    if len(exact) == 1:
        selected = exact[0]
        reason = "matched the only source with this output column"
    elif not exact and len(possible) == 1:
        selected = possible[0]
        reason = "only one duplicate source could still contain the column"
    elif len(exact) > 1:
        selected = exact[0]
        reason = "multiple duplicate sources expose the column; using the first one"

    result.diagnostics.warnings.append(DiagnosticWarning(
        type="duplicate_alias",
        scope=scope_id,
        msg=(
            f"Alias '{table_alias}' is used {len(candidates)} times in the same SELECT. "
            f"Column '{col_name}' "
            f"{('was resolved because it ' + reason) if reason else 'could not be disambiguated'}."
        ),
    ))

    if selected is None:
        return SourceRef(scope="UNKNOWN", column=col_name)
    _alias, source = selected
    return _source_ref_for_source(_alias, source, col_name, result)


def _iter_select_sources_in_order(sg_scope: Scope) -> list[tuple[str, Scope | exp.Table]]:
    """Return SELECT FROM/JOIN sources in SQL order, preserving duplicate aliases."""
    expr = sg_scope.expression
    if not isinstance(expr, exp.Select):
        return []

    items: list[tuple[str, Scope | exp.Table]] = []
    from_ = expr.args.get("from_")
    if from_ is not None:
        source = getattr(from_, "this", None)
        item = _source_item_from_ast_node(source, sg_scope)
        if item:
            items.append(item)
    for join in expr.args.get("joins") or []:
        item = _source_item_from_ast_node(join.this, sg_scope)
        if item:
            items.append(item)
    return items


def _source_item_from_ast_node(
    node: exp.Expression | None,
    sg_scope: Scope,
) -> tuple[str, Scope | exp.Table] | None:
    if node is None:
        return None
    alias = node.alias if isinstance(node, (exp.Table, exp.Subquery)) else None
    source: Scope | exp.Table | None = None
    if isinstance(node, exp.Table):
        # A table reference may actually name a CTE; resolve that through the
        # scope source map by table name. Physical tables can be used directly.
        named_source = sg_scope.sources.get(node.name)
        if isinstance(named_source, Scope):
            source = named_source
        else:
            source = node
        alias = alias or node.name
    elif isinstance(node, exp.Subquery):
        if alias:
            mapped = sg_scope.sources.get(alias)
            if isinstance(mapped, Scope):
                source = mapped
            else:
                for sub_scope in getattr(sg_scope, "subquery_scopes", []) or []:
                    if sub_scope.expression is node.this:
                        source = sub_scope
                        break
    if alias and source is not None:
        return alias, source
    return None


def _source_column_state(
    alias: str,
    source: Scope | exp.Table,
    col_name: str,
    result: ScopeLineageResult,
    schema: dict | None,
) -> str:
    """Return present/absent/unknown for whether source can expose col_name."""
    if isinstance(source, Scope):
        upstream_id = _source_scope_id(alias, source, result)
        upstream_sd = result.scopes.get(upstream_id) if upstream_id else None
        if upstream_sd:
            names = {col.name for col in upstream_sd.columns}
            if col_name in names or "*" in names:
                return "present"
            return "absent"
        inner_expr = source.expression
        if isinstance(inner_expr, exp.Select):
            if col_name in inner_expr.named_selects or _select_has_star_projection(inner_expr):
                return "present"
            return "absent"
        return "unknown"

    fq = _qualified_table(source)
    if schema:
        norm = _normalize_table_name(fq)
        return "present" if col_name in schema.get(norm, []) else "absent"
    return "unknown"


def _source_ref_for_source(
    alias: str,
    source: Scope | exp.Table,
    col_name: str,
    result: ScopeLineageResult,
) -> SourceRef:
    if isinstance(source, Scope):
        upstream_id = _source_scope_id(alias, source, result)
        if upstream_id:
            return SourceRef(scope=upstream_id, column=col_name)
        return SourceRef(scope="UNKNOWN", column=col_name)
    return SourceRef(scope=_qualified_table(source), column=col_name)


def _source_scope_id(alias: str, source: Scope, result: ScopeLineageResult) -> str | None:
    """Return a stable result scope id for a sqlglot Scope source."""
    upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
    if upstream_id in result.scopes:
        return upstream_id
    for candidate in (f"cte:{alias}", f"subq:{alias}", f"union:{alias}"):
        if candidate in result.scopes:
            return candidate
    return upstream_id


def _select_has_star_projection(select: exp.Select) -> bool:
    """Return True if a SELECT contains bare or qualified star projections."""
    for projection in select.selects:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Star):
            return True
        if isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
            return True
    return False


def _expand_star_sources(
    sg_scope: Scope, table_alias: str | None, schema: dict | None,
) -> List[SourceRef]:
    """Expand SELECT * or a.* into source refs when possible.

    Returns refs with column="*" — used as fallback when expansion into
    individual columns is not possible.
    """
    sources = []
    if table_alias:
        # Qualified: a.*
        src = sg_scope.sources.get(table_alias)
        if isinstance(src, Scope):
            upstream_id = getattr(src, _SCOPE_ID_ATTR, None)
            if upstream_id:
                sources.append(SourceRef(scope=upstream_id, column="*"))
        elif isinstance(src, exp.Table):
            sources.append(SourceRef(scope=_qualified_table(src), column="*"))
    else:
        # Bare *: all sources
        for alias, source in _selected_sources(sg_scope).items():
            if isinstance(source, Scope):
                upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
                if upstream_id:
                    sources.append(SourceRef(scope=upstream_id, column="*"))
            elif isinstance(source, exp.Table):
                sources.append(SourceRef(scope=_qualified_table(source), column="*"))
    return sources


def _expand_star_into_columns(
    sg_scope: Scope, table_alias: str | None,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[ScopeColumn]:
    """Expand SELECT * or a.* into individual DIRECT ScopeColumns.

    Tries, in order:
    1. Source scope already has resolved columns (CTE/subquery) → expand from those
    2. Schema provides column names for the physical table → expand from schema
    Returns empty list if expansion is impossible (caller should fall back to EXPAND_ALL).
    """
    columns: List[ScopeColumn] = []

    if table_alias:
        # Qualified: a.*
        src = sg_scope.sources.get(table_alias)
        if isinstance(src, Scope):
            upstream_id = getattr(src, _SCOPE_ID_ATTR, None)
            if upstream_id:
                upstream_sd = result.scopes.get(upstream_id)
                if upstream_sd and upstream_sd.columns:
                    for col in upstream_sd.columns:
                        columns.append(ScopeColumn(
                            name=col.name,
                            transform="DIRECT",
                            expression=col.name,
                            sources=[SourceRef(scope=upstream_id, column=col.name)],
                        ))
                    return columns
        elif isinstance(src, exp.Table) and schema:
            fq = _qualified_table(src)
            short = _normalize_table_name(fq)
            col_names = schema.get(short, [])
            if col_names:
                col_names = _with_referenced_columns_missing_from_schema(
                    sg_scope, table_alias, col_names
                )
                for cn in col_names:
                    columns.append(ScopeColumn(
                        name=cn,
                        transform="DIRECT",
                        expression=cn,
                        sources=[SourceRef(scope=fq, column=cn)],
                    ))
                return columns
    else:
        # Bare *: all sources
        for alias, source in _selected_sources(sg_scope).items():
            if isinstance(source, Scope):
                upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
                if upstream_id:
                    upstream_sd = result.scopes.get(upstream_id)
                    if upstream_sd and upstream_sd.columns:
                        for col in upstream_sd.columns:
                            columns.append(ScopeColumn(
                                name=col.name,
                                transform="DIRECT",
                                expression=col.name,
                                sources=[SourceRef(scope=upstream_id, column=col.name)],
                            ))
                    else:
                        # Source scope has no columns yet — cannot expand
                        return []
            elif isinstance(source, exp.Table):
                if schema:
                    fq = _qualified_table(source)
                    short = _normalize_table_name(fq)
                    col_names = schema.get(short, [])
                    if col_names:
                        col_names = _with_referenced_columns_missing_from_schema(
                            sg_scope, alias, col_names
                        )
                        for cn in col_names:
                            columns.append(ScopeColumn(
                                name=cn,
                                transform="DIRECT",
                                expression=cn,
                                sources=[SourceRef(scope=fq, column=cn)],
                            ))
                    else:
                        return []
                else:
                    return []
        if columns:
            return columns

    return []


def _with_referenced_columns_missing_from_schema(
    sg_scope: Scope,
    table_alias: str | None,
    col_names: list[str],
) -> list[str]:
    """Keep explicit filter/join/order refs when schema misses partition-like columns.

    Some metastore exports omit partition columns such as ``dt`` even though
    Spark ``SELECT *`` exposes them. If a star-expanded scope later references
    such a column, treating it as absent creates an internal dangling ref. The
    SQL already names the column, so retaining it as a pass-through is safer
    than dropping it from the expanded star list.
    """
    extra: list[str] = []
    source_count = len(_selected_sources(sg_scope))
    for col_ref in _scope_local_column_refs(sg_scope):
        if col_ref.table:
            if table_alias and col_ref.table != table_alias:
                continue
            if not table_alias:
                continue
        elif table_alias and source_count > 1:
            continue
        name = col_ref.name
        if name and name not in col_names and name not in extra:
            extra.append(name)
    return list(col_names) + extra


def _scope_local_column_refs(sg_scope: Scope) -> list[exp.Column]:
    expr = sg_scope.expression
    if not isinstance(expr, exp.Select):
        return []

    roots = []
    for key in ("where", "having", "qualify"):
        node = expr.args.get(key)
        if node is not None:
            roots.append(node)
    group = expr.args.get("group")
    if group is not None:
        roots.extend(group.expressions if hasattr(group, "expressions") else [group])
    order = expr.args.get("order")
    if order is not None:
        roots.extend(order.expressions if hasattr(order, "expressions") else [order])
    for join in expr.args.get("joins") or []:
        on_expr = join.args.get("on")
        if on_expr is not None:
            roots.append(on_expr)

    refs: list[exp.Column] = []
    for root in roots:
        for col_ref in root.find_all(exp.Column):
            if not _inside_nested_query(col_ref, root):
                refs.append(col_ref)
    return refs


def _resolve_union_scope(
    union_scope_id: str, scope_data: ScopeData, result: ScopeLineageResult,
) -> None:
    """Resolve columns for a synthetic UNION scope from its branch scopes."""
    branch_ids = scope_data.branches or []
    if not branch_ids:
        return

    branch_cols = [result.scopes.get(bid) for bid in branch_ids]
    branch_cols = [sd.columns for sd in branch_cols if sd is not None]
    if not branch_cols:
        return

    n_cols = len(branch_cols[0]) if branch_cols else 0
    for i in range(n_cols):
        # Collect sources and branches from each branch's corresponding column
        sources = []
        branches = []
        # Use the first branch's column name; for positional alignment in UNION,
        # later branches with unnamed columns (_col_N) should adopt the first branch's name
        col_name = branch_cols[0][i].name if i < len(branch_cols[0]) else f"col_{i}"

        for j, (bid, cols) in enumerate(zip(branch_ids, branch_cols)):
            if i < len(cols):
                branch_col = cols[i]
                # Rename _col_N names to match the first branch's name at this position
                if branch_col.name.startswith("_col_") and not col_name.startswith("_col_"):
                    branch_col.name = col_name
                sources.append(SourceRef(scope=bid, column=branch_col.name))
                branches.append({"branch": bid, "from_column": branch_col.name})

        scope_data.columns.append(ScopeColumn(
            name=col_name,
            transform="UNION",
            expression=col_name,
            sources=sources,
            branches=branches,
        ))


def _resolve_union_scopes_bottom_up(result: ScopeLineageResult) -> None:
    """Resolve union scopes in bottom-up order to handle nested unions.

    A union scope can only be resolved after all its branch scopes have columns.
    For nested unions (union inside a union branch), we must resolve the inner
    union first. We iterate until all union scopes have been resolved.
    """
    union_scopes = {sid: sd for sid, sd in result.scopes.items() if sd.kind == "union"}
    if not union_scopes:
        return

    resolved = set()
    max_iterations = len(union_scopes) + 1  # safety limit

    for _ in range(max_iterations):
        progress = False
        for scope_id, scope_data in union_scopes.items():
            if scope_id in resolved:
                continue

            # Check if all branch scopes have columns (meaning they're resolved)
            branch_ids = scope_data.branches or []
            all_branches_ready = True
            for bid in branch_ids:
                branch_sd = result.scopes.get(bid)
                if branch_sd is None or not branch_sd.columns:
                    # A branch with no columns might be a nested union that hasn't been resolved yet
                    if bid in union_scopes and bid not in resolved:
                        all_branches_ready = False
                        break
                    # Or it might be a branch that was already resolved to empty
                    # (shouldn't happen, but handle gracefully)

            if all_branches_ready:
                _resolve_union_scope(scope_id, scope_data, result)
                resolved.add(scope_id)
                progress = True

        if not progress or len(resolved) == len(union_scopes):
            break


def _resolve_scope_union_passthrough(
    scope_id: str, scope_data: ScopeData, result: ScopeLineageResult,
) -> None:
    """When a scope has a Union expression, its columns are passthrough from its union child scope.

    Works for ROOT, CTE, or any scope whose expression is Union.
    The scope's columns are copies of the union scope's columns,
    but each column's sources point to the union scope instead of the branches.
    """
    # Find the union scope that belongs to this scope
    # Convention: union scope ID is "union:<context>" where <context> comes from this scope's ID
    context = None
    if scope_id == "ROOT":
        context = "main"
    elif ":" in scope_id:
        context = scope_id.split(":", 1)[1]
    else:
        context = scope_id

    union_scope_id = f"union:{context}"
    union_scope = result.scopes.get(union_scope_id)
    if union_scope is None:
        return

    for col in union_scope.columns:
        passthrough_col = ScopeColumn(
            name=col.name,
            transform=col.transform,
            expression=col.expression,
            sources=[SourceRef(scope=union_scope_id, column=col.name)],
            branches=col.branches,
        )
        scope_data.columns.append(passthrough_col)


def _expand_star_columns(result: ScopeLineageResult) -> None:
    """Expand wildcard (*) columns into concrete columns when the upstream scope has explicit ones.

    If scope S has  * <- [(upstream, '*')]  and upstream has concrete columns [c1, c2, ...],
    add  cI <- [(upstream, cI)]  to S for each cI not already explicitly defined in S.
    Repeats until stable to unroll chains (e.g. subq:a.* -> subq:aa.* -> union:aa.[cols]).
    """
    changed = True
    while changed:
        changed = False
        for scope_data in result.scopes.values():
            star_cols = _star_passthrough_columns(scope_data)
            if not star_cols:
                continue
            existing = {c.name for c in scope_data.columns}
            for star_col in star_cols:
                for src_ref in star_col.sources:
                    upstream = result.scopes.get(src_ref.scope)
                    if upstream is None:
                        continue
                    for up_col in upstream.columns:
                        if up_col.name == "*" or up_col.name in existing:
                            continue
                        scope_data.columns.append(ScopeColumn(
                            name=up_col.name,
                            transform="DIRECT",
                            sources=[SourceRef(scope=src_ref.scope, column=up_col.name)],
                        ))
                        existing.add(up_col.name)
                        changed = True


def _materialize_referenced_star_columns(result: ScopeLineageResult) -> None:
    """Add pass-through columns for references into a scope that still has SELECT *.

    Without physical table schemas, a scope like ``SELECT * FROM physical`` cannot be
    fully expanded. If a downstream scope later references ``a.call_id``, however, we
    can still materialize just that referenced column as a pass-through from the star
    source. This keeps the graph internally consistent while preserving the broader
    schema limitation.
    """
    changed = True
    while changed:
        changed = False
        known = {
            sid: {c.name for c in sd.columns}
            for sid, sd in result.scopes.items()
        }
        needed: dict[str, set[str]] = {}
        for scope_data in result.scopes.values():
            for col in scope_data.columns:
                for src in col.sources:
                    if src.scope in result.scopes and src.column not in known[src.scope] and src.column != "*":
                        needed.setdefault(src.scope, set()).add(src.column)

        for scope_id, col_names in needed.items():
            scope_data = result.scopes[scope_id]
            star_cols = _star_passthrough_columns(scope_data)
            if not star_cols:
                continue
            existing = known[scope_id]
            for col_name in sorted(col_names):
                if col_name in existing:
                    continue
                sources = []
                for star_col in star_cols:
                    for star_src in star_col.sources:
                        upstream = result.scopes.get(star_src.scope)
                        if upstream is not None and not _scope_can_passthrough_column(upstream, col_name):
                            continue
                        sources.append(SourceRef(scope=star_src.scope, column=col_name))
                if not sources:
                    continue
                scope_data.columns.append(ScopeColumn(
                    name=col_name,
                    transform="DIRECT",
                    expression=col_name,
                    sources=sources,
                ))
                existing.add(col_name)
                changed = True


def _scope_can_passthrough_column(scope_data: ScopeData, col_name: str) -> bool:
    """Return whether an internal scope can plausibly provide a star-materialized column."""
    if any(c.name == col_name for c in scope_data.columns):
        return True
    return bool(_star_passthrough_columns(scope_data))


def _star_passthrough_columns(scope_data: ScopeData) -> list[ScopeColumn]:
    """Return wildcard passthrough columns, covering both ``*`` and ``alias.*``."""
    return [
        c for c in scope_data.columns
        if (
            c.name == "*"
            or c.name.endswith(".*")
            or any(src.column == "*" or src.column.endswith(".*") for src in c.sources)
        )
    ]


def _dedupe_scope_columns(result: ScopeLineageResult) -> None:
    """Merge duplicate columns with the same output name and merge branch."""
    for scope_data in result.scopes.values():
        merged: list[ScopeColumn] = []
        by_key: dict[tuple[str, str | None], ScopeColumn] = {}
        for col in scope_data.columns:
            key = (col.name, col.merge_branch)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = col
                merged.append(col)
                continue

            seen_sources = {(s.scope, s.column) for s in existing.sources}
            for src in col.sources:
                if (src.scope, src.column) not in seen_sources:
                    existing.sources.append(src)
                    seen_sources.add((src.scope, src.column))

            existing.transform = _stronger_transform(existing.transform, col.transform)
            if existing.expression is None and col.expression is not None:
                existing.expression = col.expression
            if existing.branches is None and col.branches is not None:
                existing.branches = col.branches
            elif existing.branches is not None and col.branches:
                existing.branches.extend(col.branches)
        scope_data.columns = merged


def _stronger_transform(left: str, right: str) -> str:
    order = {
        "CONSTANT": 0,
        "DIRECT": 1,
        "EXPAND_ALL": 2,
        "UNION": 3,
        "EXPRESSION": 4,
        "CONDITIONAL": 5,
        "WINDOW": 6,
        "AGGREGATE": 7,
    }
    return left if order.get(left, 0) >= order.get(right, 0) else right


def _resolve_merge_columns(
    root_scope: Scope, result: ScopeLineageResult, schema: dict | None = None,
    all_scopes: list | None = None,
) -> None:
    """Resolve MERGE WHEN clauses into ROOT scope columns."""
    # Find the MERGE AST node from the root scope's expression
    merge_node = None
    if root_scope.expression:
        p = root_scope.expression
        while p and not isinstance(p, exp.Merge):
            p = getattr(p, "parent", None)
        if isinstance(p, exp.Merge):
            merge_node = p

    if merge_node is None:
        return

    # Find the USING scope_id.
    # Strategy: find the scope whose parent.expression IS the USING Subquery node.
    # We can't use root_scope.sources because sqlglot doesn't put the USING alias there.
    using_scope_id = None
    using_subq = merge_node.args.get("using")
    if using_subq and all_scopes:
        for sg in all_scopes:
            if sg.parent and sg.parent.expression is using_subq:
                sid = getattr(sg, _SCOPE_ID_ATTR, None)
                if sid and sid in result.scopes:
                    using_scope_id = sid
                    break
    if not using_scope_id:
        for sid, sd in result.scopes.items():
            if sd.kind == "subquery":
                using_scope_id = sid
                break

    whens = merge_node.args.get("whens")
    if whens is None:
        return

    for when in whens.expressions if hasattr(whens, "expressions") else [whens]:
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            # WHEN MATCHED THEN UPDATE SET
            for eq in then.find_all(exp.EQ):
                dst_col = eq.this
                src_expr = eq.expression
                dst_name = dst_col.name if isinstance(dst_col, exp.Column) else None
                if dst_name is None:
                    continue
                transform = _classify_extended(src_expr)
                expression = src_expr.sql(dialect=DIALECT)
                sources = []
                if transform != "CONSTANT" and using_scope_id:
                    for col_ref in src_expr.find_all(exp.Column):
                        col_table = col_ref.table
                        col_name = col_ref.name
                        if col_table:
                            # Check if it's the USING source alias
                            src = None
                            # Find the USING scope's sg_scope to resolve alias
                            for sg_scope in root_scope.traverse():
                                if getattr(sg_scope, _SCOPE_ID_ATTR, None) == using_scope_id:
                                    src = sg_scope.sources.get(col_table)
                                    break
                            if isinstance(src, exp.Table):
                                fq = _qualified_table(src)
                                if (fq, col_name) not in {(s.scope, s.column) for s in sources}:
                                    sources.append(SourceRef(scope=fq, column=col_name))
                            elif isinstance(src, Scope):
                                upstream_id = getattr(src, _SCOPE_ID_ATTR, None)
                                if upstream_id and (upstream_id, col_name) not in {(s.scope, s.column) for s in sources}:
                                    sources.append(SourceRef(scope=upstream_id, column=col_name))
                            else:
                                # Likely the USING source alias
                                if (using_scope_id, col_name) not in {(s.scope, s.column) for s in sources}:
                                    sources.append(SourceRef(scope=using_scope_id, column=col_name))
                        else:
                            if (using_scope_id, col_name) not in {(s.scope, s.column) for s in sources}:
                                sources.append(SourceRef(scope=using_scope_id, column=col_name))
                if not sources:
                    sources = _source_free_leaf_sources(src_expr, expression)

                result.scopes["ROOT"].columns.append(ScopeColumn(
                    name=dst_name, transform=transform, expression=expression,
                    sources=sources, merge_branch="matched",
                ))

        elif isinstance(then, exp.Insert):
            # WHEN NOT MATCHED THEN INSERT (cols) VALUES (exprs)
            ins_cols = then.this
            values = then.expression
            if isinstance(ins_cols, exp.Tuple) and isinstance(values, exp.Tuple):
                for dst_col_node, val_expr in zip(ins_cols.expressions, values.expressions):
                    dst_name = dst_col_node.name if hasattr(dst_col_node, "name") else str(dst_col_node)
                    transform = _classify_extended(val_expr)
                    expression = val_expr.sql(dialect=DIALECT)
                    sources = []
                    if transform != "CONSTANT" and using_scope_id:
                        for col_ref in val_expr.find_all(exp.Column):
                            col_name = col_ref.name
                            if (using_scope_id, col_name) not in {(s.scope, s.column) for s in sources}:
                                sources.append(SourceRef(scope=using_scope_id, column=col_name))
                    if not sources:
                        sources = _source_free_leaf_sources(val_expr, expression)

                    result.scopes["ROOT"].columns.append(ScopeColumn(
                        name=dst_name, transform=transform, expression=expression,
                        sources=sources, merge_branch="not_matched",
                    ))
        elif _is_merge_delete_then(then):
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="merge_delete_ignored",
                scope="ROOT",
                msg=(
                    "MERGE WHEN MATCHED THEN DELETE is a row-level operation and "
                    "does not produce ROOT output columns."
                ),
            ))


def _is_merge_delete_then(then: exp.Expression | None) -> bool:
    if then is None:
        return False
    if isinstance(then, exp.Var):
        return str(then.this).upper() == "DELETE"
    return then.sql(dialect=DIALECT).strip().upper() == "DELETE"


def _resolve_join(
    join: exp.Join, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> ScopeJoin | None:
    """Resolve a JOIN into a ScopeJoin."""
    # sqlglot mapping:
    #   LEFT JOIN  -> kind='',  side='LEFT'
    #   RIGHT JOIN -> kind='',  side='RIGHT'
    #   FULL JOIN  -> kind='',  side='FULL'
    #   CROSS JOIN -> kind='CROSS', side=''
    #   INNER JOIN -> kind='INNER', side=''
    #   LEFT OUTER JOIN -> kind='OUTER', side='LEFT'
    kind = (join.kind or "").upper()
    side = (join.side or "").upper()

    if kind == "CROSS":
        join_type = "CROSS"
    elif kind == "INNER":
        join_type = "INNER"
    elif kind == "OUTER" and side in ("LEFT", "RIGHT", "FULL"):
        join_type = f"{side}_OUTER"
    elif side in ("LEFT", "RIGHT", "FULL"):
        join_type = f"{side}_OUTER"
    else:
        join_type = kind or "INNER"

    right = join.this
    right_alias = right.alias if isinstance(right, (exp.Table, exp.Subquery)) else None
    right_scope = _resolve_table_to_scope_id(right, sg_scope)

    # Determine left_scope: the first FROM source
    left_scope = None
    from_ = sg_scope.expression.args.get("from_") if isinstance(sg_scope.expression, exp.Select) else None
    if from_:
        from_src = getattr(from_, "this", None)
        if from_src:
            left_scope = _resolve_table_to_scope_id(from_src, sg_scope)

    # Resolve ON clause
    on = join.args.get("on")
    condition_expression = on.sql(dialect=DIALECT) if on else None
    condition_columns = []
    if on:
        condition_columns = _resolve_column_refs_in_expr(on, sg_scope, result, schema)

    return ScopeJoin(
        join_type=str(join_type),
        left_scope=left_scope or "UNKNOWN",
        right_scope=right_scope or "UNKNOWN",
        alias_in_parent=right_alias,
        condition_expression=condition_expression,
        condition_columns=condition_columns,
    )


def _resolve_table_to_scope_id(table_node: exp.Expression, sg_scope: Scope) -> str | None:
    """Resolve a Table or Subquery node to its scope_id."""
    item = _source_item_from_ast_node(table_node, sg_scope)
    if item:
        alias, src = item
        if isinstance(src, Scope):
            return getattr(src, _SCOPE_ID_ATTR, None)
        if isinstance(src, exp.Table):
            return _qualified_table(src)
    if isinstance(table_node, exp.Table):
        return _qualified_table(table_node)
    return None


def _resolve_filter(
    clause: exp.Expression, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[ScopeFilter]:
    """Resolve a WHERE or HAVING clause into ScopeFilter(s)."""
    columns = _resolve_column_refs_in_expr(clause, sg_scope, result, schema)
    return [ScopeFilter(expression=clause.sql(dialect=DIALECT), columns=columns)]


def _resolve_expr_list(
    exprs: list, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[SourceRef]:
    """Resolve a list of expressions (e.g. GROUP BY) to SourceRefs."""
    all_refs = []
    seen = set()
    for expr in exprs:
        for ref in _resolve_column_refs_in_expr(expr, sg_scope, result, schema):
            if (ref.scope, ref.column) not in seen:
                seen.add((ref.scope, ref.column))
                all_refs.append(ref)
    return all_refs


def _classify_extended(node: exp.Expression) -> str:
    """Classify expression type. Extends parser._classify with UNION and EXPAND_ALL."""
    if isinstance(node, exp.Star):
        return "EXPAND_ALL"
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        return "EXPAND_ALL"
    if isinstance(node, exp.Window):
        return "WINDOW"
    if isinstance(node, exp.AggFunc):
        return "AGGREGATE"
    if isinstance(node, (exp.Case, exp.If)):
        return "CONDITIONAL"
    if isinstance(node, exp.Subquery):
        return "EXPRESSION"  # LITERAL_SUBQUERY mapped to EXPRESSION per design decision
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return "CONSTANT"
    if isinstance(node, exp.Column):
        return "DIRECT"
    # Check for Anonymous UDAFs
    if isinstance(node, exp.Anonymous):
        func_name = node.name.upper() if hasattr(node, "name") else ""
        if func_name in _KNOWN_UDAFS:
            return "AGGREGATE"
    return "EXPRESSION"


def _extract_case_branches(node: exp.Expression) -> List[dict]:
    """Extract WHEN/THEN branches from CASE/IF expressions."""
    branches = []
    if isinstance(node, exp.Case):
        # sqlglot Spark dialect: CASE WHEN is Case(ifs=[If(...), ...], default=...)
        ifs = node.args.get("ifs", [])
        for if_clause in ifs:
            if isinstance(if_clause, exp.If):
                branches.append({
                    "when_expr": if_clause.this.sql(dialect=DIALECT) if if_clause.this else "",
                    "then_value": if_clause.args.get("true").sql(dialect=DIALECT) if if_clause.args.get("true") else "",
                })
        # Also handle exp.When nodes (some dialects use these)
        for when in node.find_all(exp.When):
            branches.append({
                "when_expr": when.this.sql(dialect=DIALECT) if when.this else "",
                "then_value": when.expression.sql(dialect=DIALECT) if when.expression else "",
            })
        # Default/ELSE
        default = node.args.get("default")
        if default:
            branches.append({
                "when_expr": "ELSE",
                "then_value": default.sql(dialect=DIALECT),
            })
    elif isinstance(node, exp.If):
        branches.append({
            "when_expr": node.this.sql(dialect=DIALECT) if node.this else "",
            "then_value": node.args.get("true").sql(dialect=DIALECT) if node.args.get("true") else "",
        })
        false_expr = node.args.get("false")
        if false_expr:
            branches.append({
                "when_expr": "ELSE",
                "then_value": false_expr.sql(dialect=DIALECT),
            })
    return branches


def _extract_window_info(
    node: exp.Expression, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> dict:
    """Extract partition_by and order_by from a window function."""
    info = {}
    window = node if isinstance(node, exp.Window) else node.find(exp.Window)
    if window is None:
        return info

    # sqlglot stores partition as "partition_by" key (a list of expressions)
    partition = window.args.get("partition_by")
    if partition:
        partition_refs = []
        for p in partition:
            partition_refs.extend(_resolve_column_refs_in_expr(p, sg_scope, result, schema))
        info["partition_by"] = partition_refs

    order = window.args.get("order")
    if order:
        order_refs = []
        for item in (order.expressions if hasattr(order, "expressions") else []):
            # item.args.get("desc") is True for DESC, None/False for ASC
            # (do NOT use item.desc — that's a method that generates a DESC node)
            is_desc = isinstance(item, exp.Ordered) and item.args.get("desc") is True
            direction = "DESC" if is_desc else "ASC"
            # For Ordered, resolve the inner expression
            inner = item.this if isinstance(item, exp.Ordered) else item
            for ref in _resolve_column_refs_in_expr(inner, sg_scope, result, schema):
                order_refs.append({"scope": ref.scope, "column": ref.column, "direction": direction})
        info["order_by"] = order_refs

    return info


def _extract_agg_function(node: exp.Expression) -> str | None:
    """Extract the aggregate function name."""
    if isinstance(node, exp.AggFunc):
        # For nodes like ArrayUniqueAgg (COLLECT_SET), use the dialect SQL
        # which preserves the original function name
        sql = node.sql(dialect=DIALECT)
        # Extract function name: "COLLECT_SET(...)" -> "COLLECT_SET"
        paren = sql.find("(")
        if paren > 0:
            return sql[:paren].strip().upper()
        # Fallback
        if node.name:
            return node.name.upper()
        return node.sql_name()
    if isinstance(node, exp.Anonymous):
        func_name = node.name.upper() if hasattr(node, "name") else ""
        if func_name in _KNOWN_UDAFS:
            return func_name
        return func_name or None
    return None


def _build_depends_on_and_graph(result: ScopeLineageResult) -> None:
    """Populate depends_on for each scope and build scope_graph edges."""
    all_nodes = set(result.scopes.keys())

    for scope_id, scope_data in result.scopes.items():
        referenced = set()

        for col in scope_data.columns:
            for src in col.sources:
                if _is_dependency_scope(src.scope):
                    referenced.add(src.scope)

        for join in scope_data.joins:
            if _is_dependency_scope(join.left_scope):
                referenced.add(join.left_scope)
            if _is_dependency_scope(join.right_scope):
                referenced.add(join.right_scope)
            for cc in join.condition_columns:
                if _is_dependency_scope(cc.scope):
                    referenced.add(cc.scope)

        for f in scope_data.filters:
            for c in f.columns:
                if _is_dependency_scope(c.scope):
                    referenced.add(c.scope)

        for g in scope_data.group_by:
            if _is_dependency_scope(g.scope):
                referenced.add(g.scope)

        for h in scope_data.having:
            for c in h.columns:
                if _is_dependency_scope(c.scope):
                    referenced.add(c.scope)

        for o in scope_data.order_by:
            if _is_dependency_scope(o.get("scope")):
                referenced.add(o["scope"])

        # Remove self-reference
        referenced.discard(scope_id)
        scope_data.depends_on = sorted(referenced)
        all_nodes.update(referenced)

    # Build edges
    result.scope_graph.nodes = sorted(all_nodes | set(result.source_tables))
    result.scope_graph.edges = []
    for scope_id, scope_data in result.scopes.items():
        for dep in scope_data.depends_on:
            result.scope_graph.edges.append(ScopeGraphEdge(from_=dep, to=scope_id))
