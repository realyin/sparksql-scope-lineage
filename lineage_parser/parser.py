"""Column-level lineage parser (hybrid: sqlglot.lineage + post-processor).

v0.3 scope:
  - INSERT...SELECT and INSERT...WITH...SELECT (CTE) and INSERT...UNION ALL
  - MERGE INTO (UPDATE SET column lineage)
  - Strategy A (strict): never guess source table for unqualified columns;
    falls into AMBIGUOUS_COLUMN unresolved.
  - `*` / `a.*` expanded when schema is provided; otherwise STAR_UNRESOLVED.

We delegate column traversal (CTE / UNION / Subquery) to
``sqlglot.lineage.lineage`` and post-process its leaves into our flat edge
representation.
"""

from typing import Dict, List, Optional, Tuple

import sqlglot
from sqlglot import ErrorLevel
from sqlglot import exp, lineage as sg_lineage

from .types import Column, ColumnRef, JoinKey, LineageResult, Unresolved
from .sqlglot_config import suppress_invalid_json_path_warnings
from .schema_metadata import load_schema, normalize_table_name as _schema_normalize_table_name


DIALECT = "spark"
PARSE_OPTS = {"error_level": ErrorLevel.IGNORE}
suppress_invalid_json_path_warnings()


def parse_all_lineage(
    sql: str, task_name: str, schema: Optional[Dict[str, List[str]]] = None
) -> List[LineageResult]:
    """Parse all top-level INSERT/MERGE statements; return one LineageResult per target.

    Multi-INSERT (Hive `FROM ... INSERT ... INSERT ...` or stmt-separated) yields
    multiple results sharing the same task_name.
    """
    trees = sqlglot.parse(sql, dialect=DIALECT, **PARSE_OPTS)
    # Filter to only Insert/Merge-bearing trees
    insert_trees = [
        t for t in trees
        if t is not None and (
            isinstance(t, (exp.Insert, exp.Merge))
            or t.find(exp.Insert) is not None
            or t.find(exp.Merge) is not None
        )
    ]
    results: List[LineageResult] = []
    for i, tree in enumerate(insert_trees):
        sub = f"{task_name}#{i}" if len(insert_trees) > 1 else task_name
        try:
            results.append(_parse_tree(tree, sub, schema=schema))
        except Exception as e:
            r = LineageResult(task_name=sub, target_table="")
            r.unresolved.append(
                Unresolved(
                    kind="LINEAGE_ERROR",
                    expression=type(tree).__name__,
                    reason=f"{type(e).__name__}: {e}",
                )
            )
            results.append(r)
    if not results:
        raise ValueError("No INSERT/MERGE statement found")
    return results


def parse_lineage(
    sql: str, task_name: str, schema: Optional[Dict[str, List[str]]] = None
) -> LineageResult:
    tree = sqlglot.parse_one(sql, dialect=DIALECT, **PARSE_OPTS)
    return _parse_tree(tree, task_name, schema=schema)


def _parse_tree(
    tree: exp.Expression, task_name: str, schema: Optional[Dict[str, List[str]]] = None
) -> LineageResult:
    # MERGE INTO: sqlglot parses as exp.Merge.
    if isinstance(tree, exp.Merge) or (
        tree.find(exp.Merge) is not None and tree.find(exp.Insert) is None
    ):
        merge = tree if isinstance(tree, exp.Merge) else tree.find(exp.Merge)
        return _parse_merge(merge, task_name, schema=schema)

    insert = tree if isinstance(tree, exp.Insert) else tree.find(exp.Insert)
    if insert is None:
        raise ValueError("No INSERT statement found")

    target_table = _qualified_table(_unwrap_target(insert.this))

    if insert.expression is None:
        result = LineageResult(task_name=task_name, target_table=target_table)
        result.unresolved.append(
            Unresolved(
                kind="UNSUPPORTED_SOURCE",
                expression=type(insert).__name__,
                reason="INSERT has no source expression",
            )
        )
        return result

    # Graft CTEs from Insert onto the inner Select so lineage can see them.
    src_expr = _build_source_expression(insert)
    # Expand `cte.*` projections (no schema needed when CTE has explicit cols)
    src_expr = _expand_cte_stars(src_expr)
    result = LineageResult(task_name=task_name, target_table=target_table)

    projections = _top_projections(src_expr)
    if not projections:
        result.unresolved.append(
            Unresolved(
                kind="UNSUPPORTED_SOURCE",
                expression=type(src_expr).__name__,
                reason="cannot extract top-level projections",
            )
        )
        return result

    src_sql = src_expr.sql(dialect=DIALECT)

    # Build alias map (top-level only) so we can resolve qualified .*
    # like `a.*` back to the real table name.
    alias_map: Dict[str, str] = {}
    if isinstance(src_expr, exp.Select):
        alias_map = _build_alias_map(src_expr, recursive=True)
    elif isinstance(src_expr, exp.Union):
        sel = _leftmost_select(src_expr)
        if sel is not None:
            alias_map = _build_alias_map(sel, recursive=True)
    result.alias_map = alias_map

    # Build position map for target column name resolution
    target_col_list = schema.get(_normalize_table_name(target_table)) if schema else None

    # For UNION ALL: collect branches so each can be traced independently.
    union_branches: Optional[List[exp.Select]] = None
    if isinstance(src_expr, exp.Union):
        union_branches = _collect_union_branches(src_expr)

    # For plain SELECT: build subquery column map for inline-subquery resolution.
    subquery_col_map: Dict[str, Dict[str, exp.Expression]] = {}
    if isinstance(src_expr, exp.Select):
        subquery_col_map = _build_subquery_col_map(src_expr)

    # Process projections with positional target column tracking.
    # `a.*` / `*` expand to multiple output columns, consuming multiple
    # positions in the target table. We track the current target position
    # across all projections.
    target_pos = 0
    for proj in projections:
        name, inner = _extract_name_inner(proj)
        is_bare_star = isinstance(proj, exp.Star) or isinstance(inner, exp.Star)
        is_qualified_star = isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)

        # Target column override: only apply when the projection has NO alias
        # (anonymous expression like `a + b`).  When the SELECT already supplies
        # a name, trust it — the schema order rarely matches the SELECT order in
        # Spark INSERT OVERWRITE, so positional remapping causes false renames.
        tc = None
        if target_col_list and target_pos < len(target_col_list) and not name:
            tc = target_col_list[target_pos]

        if is_bare_star or is_qualified_star:
            n_expanded = _process_star_projection(
                proj, inner, alias_map, result, schema=schema,
                target_col_list=target_col_list, target_pos=target_pos,
            )
            target_pos += n_expanded
        elif union_branches is not None:
            # UNION ALL: trace each branch independently to get all upstream sources
            # and capture per-branch expression differences.
            _process_union_projection(
                target_pos, name, union_branches, result,
                schema=schema, target_col=tc,
            )
            target_pos += 1
        else:
            _process_projection(
                proj, src_sql, alias_map, result, schema=schema, target_col=tc,
                subquery_col_map=subquery_col_map,
            )
            target_pos += 1

    # JOIN keys: only at top-level Select.
    if isinstance(src_expr, exp.Select):
        for join in src_expr.args.get("joins") or []:
            _process_join(join, alias_map, result)

    return result


def _parse_merge(
    merge: exp.Merge, task_name: str, schema: Optional[Dict[str, List[str]]] = None
) -> LineageResult:
    """Parse MERGE INTO column lineage from UPDATE SET and INSERT clauses."""
    target = _unwrap_target(merge.this) if merge.this is not None else None
    target_table = _qualified_table(target) if isinstance(target, exp.Table) else ""
    result = LineageResult(task_name=task_name, target_table=target_table)

    # Build the USING source SQL for lineage tracing
    using = merge.args.get("using")
    if using is None:
        result.unresolved.append(
            Unresolved(kind="MERGE_UNSUPPORTED", expression="MERGE INTO",
                       reason="no USING clause"))
        return result

    # Unwrap Subquery and graft WITH if present
    src_expr = _unwrap_subquery(using).copy()
    w = merge.args.get("with_")
    if w is not None and isinstance(src_expr, (exp.Select, exp.Union)):
        if src_expr.args.get("with_") is None:
            src_expr.set("with_", w.copy())
    src_sql = src_expr.sql(dialect=DIALECT)
    alias_map: Dict[str, str] = {}
    if isinstance(src_expr, exp.Select):
        alias_map = _build_alias_map(src_expr, recursive=True)
    elif isinstance(src_expr, exp.Union):
        sel = _leftmost_select(src_expr)
        if sel is not None:
            alias_map = _build_alias_map(sel, recursive=True)
    # Add the USING subquery's own alias (e.g. "source" in MERGE USING (...) source)
    if isinstance(using, exp.Subquery) and using.alias:
        first_tbl = using.find(exp.Table)
        if first_tbl is not None:
            if first_tbl.alias:
                alias_map[using.alias] = first_tbl.alias
            else:
                alias_map[using.alias] = first_tbl.name
    result.alias_map = alias_map

    # Process WHEN clauses
    whens = merge.args.get("whens")
    if whens is None:
        return result

    for when in whens:
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            # UPDATE SET target.col = source.expr
            for eq in then.find_all(exp.EQ):
                dst_col_node = eq.this
                src_expr_node = eq.expression
                dst_name = dst_col_node.name if isinstance(dst_col_node, exp.Column) else None
                if dst_name is None:
                    continue
                col_type = _classify(src_expr_node)
                expr_sql = src_expr_node.sql(dialect=DIALECT)
                upstream, ambig_cols = _trace_via_lineage(
                    dst_name, src_sql, result
                )
                # Strip leading cte_expressions that duplicate the column's own expression.
                # cte_expressions are in outer→inner order from _walk.
                for u in upstream:
                    while u.cte_expressions and u.cte_expressions[0] == expr_sql:
                        u.cte_expressions.pop(0)
                        if u.scope_alias_maps:
                            u.scope_alias_maps.pop(0)
                # For MERGE, also check if source is a simple source.col reference
                if not upstream and isinstance(src_expr_node, exp.Column):
                    src_tbl = src_expr_node.table
                    src_col = src_expr_node.name
                    fq = alias_map.get(src_tbl, src_tbl) if src_tbl else None
                    if fq:
                        upstream = [ColumnRef(table=fq, column=src_col)]
                result.columns.append(
                    Column(name=dst_name, type=col_type, expression=expr_sql,
                           upstream=upstream)
                )
                for col in ambig_cols:
                    result.unresolved.append(
                        Unresolved(kind="AMBIGUOUS_COLUMN", expression=col,
                                   reason="cannot determine source table without schema",
                                   column=col))

        elif isinstance(then, exp.Insert):
            # WHEN NOT MATCHED THEN INSERT (cols) VALUES (exprs) or INSERT *
            ins_this = then.this
            if isinstance(ins_this, exp.Star):
                result.unresolved.append(
                    Unresolved(kind="STAR_UNRESOLVED", expression="*",
                               reason="MERGE INSERT * requires schema metadata"))
            # Note: explicit INSERT (col1, col2) VALUES (v1, v2) is rare in
            # Iceberg MERGE; handle if needed.

    return result


# -- AST helpers ----------------------------------------------------------


def _unwrap_target(node):
    if isinstance(node, exp.Schema):
        return node.this
    return node


def _qualified_table(t: exp.Table) -> str:
    parts = []
    cat = t.args.get("catalog")
    db = t.args.get("db")
    if cat is not None:
        parts.append(cat.name)
    if db is not None:
        parts.append(db.name)
    parts.append(t.name)
    return ".".join(parts)


def _build_source_expression(insert: exp.Insert) -> exp.Expression:
    """Return Insert's source expression with any top-level WITH grafted on.

    sqlglot 30 attaches WITH on the Insert node (key 'with_'); the inner
    Select doesn't carry it, so we copy it down before serialization.

    Also unwraps Subquery wrappers: INSERT INTO t (SELECT ...) is parsed as
    Insert(expression=Subquery(this=Select(...))).

    Handles the pattern ``INSERT INTO t SELECT * FROM (SELECT ... UNION ALL
    SELECT ...) alias`` by unwrapping the outer passthrough Select so the
    Union is at the top level — this lets _parse_tree use the UNION ALL path.
    """
    src = insert.expression.copy()
    w = insert.args.get("with_")

    # Unwrap Subquery wrapper
    if isinstance(src, exp.Subquery):
        inner = src.this
        if isinstance(inner, (exp.Select, exp.Union)):
            src = inner.copy()
            sw = src.args.get("with_")
            if sw is None and w is not None:
                src.set("with_", w.copy())
            return src

    # Unwrap outer passthrough SELECT that wraps a Union subquery:
    #   SELECT t.col1, t.col2 FROM (SELECT ... UNION ALL SELECT ...) t
    # → SELECT ... UNION ALL SELECT ...
    # Only unwrap when all projections are simple column references (passthrough).
    if isinstance(src, exp.Select):
        from_ = src.args.get("from_")
        if from_ is not None:
            from_this = getattr(from_, "this", None)
            if isinstance(from_this, exp.Subquery) and isinstance(from_this.this, exp.Union):
                all_passthrough = all(
                    isinstance(p, (exp.Column, exp.Star)) or
                    (isinstance(p, exp.Alias) and isinstance(p.this, (exp.Column, exp.Star)))
                    for p in src.expressions
                )
                if all_passthrough:
                    union = from_this.this.copy()
                    if w is not None and union.args.get("with_") is None:
                        union.set("with_", w.copy())
                    return union

    if w is not None and isinstance(src, (exp.Select, exp.Union)):
        src.set("with_", w.copy())
    return src


def _collect_cte_map(src_expr: exp.Expression) -> Dict[str, exp.Expression]:
    """Return alias -> CTE body (Select or Union)."""
    out: Dict[str, exp.Expression] = {}
    w = src_expr.args.get("with_") if hasattr(src_expr, "args") else None
    if w is None:
        return out
    for cte in w.expressions:
        out[cte.alias] = cte.this
    return out


def _unwrap_subquery(node: exp.Expression) -> exp.Expression:
    """Unwrap Subquery wrappers that sqlglot sometimes inserts."""
    if isinstance(node, exp.Subquery) and isinstance(node.this, (exp.Select, exp.Union)):
        return node.this
    return node


def _collect_union_branches(union_expr: exp.Union) -> List[exp.Select]:
    """Return SELECT branches in left-to-right order, each with the WITH clause attached."""
    with_clause = union_expr.args.get("with_")
    rights: List[exp.Select] = []
    node: exp.Expression = union_expr
    while isinstance(node, exp.Union):
        right = _unwrap_subquery(node.right)
        if isinstance(right, exp.Select):
            rights.append(right)
        node = _unwrap_subquery(node.left)
    # node is now the leftmost operand (a Select or another Union that was fully unwound)
    lefts: List[exp.Select] = [node] if isinstance(node, exp.Select) else []
    branches_raw = lefts + list(reversed(rights))

    result: List[exp.Select] = []
    for sel in branches_raw:
        if with_clause and sel.args.get("with_") is None:
            sel = sel.copy()
            sel.set("with_", with_clause.copy())
        result.append(sel)
    return result


def _build_subquery_col_map(
    select: exp.Select,
) -> Dict[str, Dict[str, exp.Expression]]:
    """Map table_alias -> {col_name -> inner_expression} for inline subqueries.

    Used to resolve e.g. ``t2.name`` when t2 is a derived-table subquery rather
    than a real base table, so we can surface the subquery's actual expression
    (e.g. a window function) as the column's expression and type.
    """
    col_map: Dict[str, Dict[str, exp.Expression]] = {}
    sources: List[exp.Expression] = []

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None:
        src = getattr(from_, "this", None)
        if src is not None:
            sources.append(src)
        for s in getattr(from_, "expressions", None) or []:
            sources.append(s)

    for join in select.args.get("joins") or []:
        sources.append(join.this)

    for src in sources:
        if not isinstance(src, exp.Subquery):
            continue
        alias = src.alias
        if not alias:
            continue
        unwrapped = _unwrap_subquery(src)
        # Skip UNION subqueries — their branch expressions are captured by
        # _trace_via_lineage's tree walk.  Resolving here would overwrite the
        # outer SELECT's column reference (e.g. c.queue_cd) with only the
        # leftmost branch's expression, losing the outer layer.
        if isinstance(unwrapped, exp.Union):
            continue
        inner_sel = _leftmost_select(unwrapped)
        if inner_sel is None:
            continue
        alias_cols: Dict[str, exp.Expression] = {}
        for proj in inner_sel.expressions:
            pname, pinner = _extract_name_inner(proj)
            if pname and pname != "*":
                alias_cols[pname] = pinner
        if alias_cols:
            col_map[alias] = alias_cols

    return col_map


def _leftmost_select(node: exp.Expression) -> Optional[exp.Select]:
    node = _unwrap_subquery(node)
    while isinstance(node, exp.Union):
        node = _unwrap_subquery(node.left)
    return node if isinstance(node, exp.Select) else None


def _cte_output_columns(
    body: exp.Expression, cte_map: Dict[str, exp.Expression]
) -> Optional[List[str]]:
    """Statically derive a CTE's output column names. None = un-derivable
    (e.g. base-table * is involved somewhere in the chain)."""
    sel = _leftmost_select(body)
    if sel is None:
        return None

    names: List[str] = []
    for proj in sel.expressions:
        if isinstance(proj, exp.Alias):
            names.append(proj.alias)
            continue
        if isinstance(proj, exp.Star):
            return None  # bare *
        if isinstance(proj, exp.Column):
            inner = proj.this
            if isinstance(inner, exp.Star):
                # qualified .*: only expandable if the source is a CTE
                tbl = proj.table
                if tbl in cte_map:
                    sub = _cte_output_columns(cte_map[tbl], cte_map)
                    if sub is None:
                        return None
                    names.extend(sub)
                    continue
                return None  # base-table .*
            names.append(proj.name)
            continue
        # anonymous expression with no alias — no resolvable name
        return None

    return names


def _expand_cte_stars(src_expr: exp.Expression) -> exp.Expression:
    """Replace `cte_alias.*` projections in the top-level SELECT with the
    explicit column list derived from the CTE definition. Base-table `*` and
    bare `*` are left untouched (they remain STAR_UNRESOLVED downstream)."""
    cte_map = _collect_cte_map(src_expr)
    if not cte_map:
        return src_expr

    sel = _leftmost_select(src_expr)
    if sel is None:
        return src_expr

    new_projs: List[exp.Expression] = []
    for proj in sel.expressions:
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        # qualified .* whose qualifier is a CTE
        if isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
            tbl_alias = inner.table
            if tbl_alias in cte_map:
                cols = _cte_output_columns(cte_map[tbl_alias], cte_map)
                if cols is not None:
                    for c in cols:
                        new_projs.append(
                            exp.Column(
                                this=exp.to_identifier(c),
                                table=exp.to_identifier(tbl_alias),
                            )
                        )
                    continue
        new_projs.append(proj)

    sel.set("expressions", new_projs)
    return src_expr


def _normalize_table_name(name: str) -> str:
    """Normalize table names for schema lookup."""
    return _schema_normalize_table_name(name)


def _top_projections(src_expr: exp.Expression) -> List[exp.Expression]:
    """Top-level output columns of the INSERT source."""
    if isinstance(src_expr, exp.Select):
        return list(src_expr.expressions)
    if isinstance(src_expr, exp.Union):
        sel = _leftmost_select(src_expr)
        if sel is not None:
            return list(sel.expressions)
    return []


def _build_alias_map(select: exp.Select, recursive: bool = False) -> Dict[str, str]:
    """alias (or table name) -> fully qualified table name. Skips CTE names.

    When recursive=True, walks into subqueries and CTE bodies to collect
    aliases from all nested SELECT layers (not just the top-level).
    """
    cte_names: set = set()
    w = select.args.get("with") or select.args.get("with_")
    if w is not None:
        for cte in w.expressions:
            cte_names.add(cte.alias)

    aliases: Dict[str, str] = {}

    def add(tbl: exp.Table):
        if not isinstance(tbl, exp.Table):
            return
        if tbl.name in cte_names:
            # CTE table reference: map alias -> CTE name (not skip)
            if tbl.alias and tbl.alias not in aliases:
                aliases[tbl.alias] = tbl.name
            return
        fq = _qualified_table(tbl)
        if tbl.alias and tbl.alias not in aliases:
            aliases[tbl.alias] = fq
        # Only add tbl.name if no alias already claimed it
        if tbl.name not in aliases:
            aliases[tbl.name] = fq

    if not recursive:
        # Original top-level-only behavior
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is not None:
            node = getattr(from_, "this", None)
            if isinstance(node, exp.Table):
                add(node)
            for src in getattr(from_, "expressions", None) or []:
                add(src)
        for join in select.args.get("joins") or []:
            node = join.this
            if isinstance(node, exp.Table):
                add(node)
        return aliases

    # Recursive: walk outer→inner so that outer-scope aliases take priority
    # when the same alias name appears in multiple scopes (e.g. t1 at outer
    # level = tmp_fas, t1 inside CTE = ods_fas_loan_ln_recv_plan_df).

    def _add_tables_and_subqueries(node, aliases):
        """Collect tables and subquery aliases from a node, outer-first."""
        # First: add direct FROM/JOIN tables at this level
        if isinstance(node, exp.Select):
            from_ = node.args.get("from_") or node.args.get("from")
            if from_ is not None:
                src = getattr(from_, "this", None)
                if isinstance(src, exp.Table):
                    add(src)
                elif isinstance(src, exp.Subquery):
                    _map_subquery(src, aliases)
                for s in getattr(from_, "expressions", None) or []:
                    if isinstance(s, exp.Table):
                        add(s)
                    elif isinstance(s, exp.Subquery):
                        _map_subquery(s, aliases)
            for join in node.args.get("joins") or []:
                jn = join.this
                if isinstance(jn, exp.Table):
                    add(jn)
                elif isinstance(jn, exp.Subquery):
                    _map_subquery(jn, aliases)
            # Then recurse into subqueries in FROM/JOIN (inner scope)
            if from_ is not None:
                src = getattr(from_, "this", None)
                if isinstance(src, exp.Subquery):
                    _add_tables_and_subqueries(src.this, aliases)
                for s in getattr(from_, "expressions", None) or []:
                    if isinstance(s, exp.Subquery):
                        _add_tables_and_subqueries(s.this, aliases)
            for join in node.args.get("joins") or []:
                jn = join.this
                if isinstance(jn, exp.Subquery):
                    _add_tables_and_subqueries(jn.this, aliases)
        elif isinstance(node, exp.Union):
            left = _unwrap_subquery(node.left)
            right = _unwrap_subquery(node.right)
            if isinstance(left, exp.Select):
                _add_tables_and_subqueries(left, aliases)
            if isinstance(right, exp.Select):
                _add_tables_and_subqueries(right, aliases)

    def _map_subquery(sq, aliases):
        """Map a Subquery alias to the first Table inside it, if not already mapped."""
        sq_alias = sq.alias
        if not sq_alias or sq_alias in aliases:
            return
        first_tbl = sq.find(exp.Table)
        if first_tbl is None:
            return
        # If the first Table is a CTE reference, map to CTE name (not its alias)
        if first_tbl.name in cte_names:
            aliases[sq_alias] = first_tbl.name
        else:
            aliases[sq_alias] = _qualified_table(first_tbl)

    # Walk the top-level SELECT (outer scope first)
    _add_tables_and_subqueries(select, aliases)
    # Walk CTE bodies (each CTE is its own scope)
    if w is not None:
        for cte in w.expressions:
            _add_tables_and_subqueries(cte.this, aliases)
    return aliases


# -- projection processing -----------------------------------------------


def _process_star_projection(
    proj: exp.Expression,
    inner: exp.Expression,
    alias_map: Dict[str, str],
    result: LineageResult,
    schema: Optional[Dict[str, List[str]]] = None,
    target_col_list: Optional[List[str]] = None,
    target_pos: int = 0,
) -> int:
    """Handle bare `*` or qualified `a.*`. Returns number of columns produced."""
    is_bare = isinstance(proj, exp.Star) or isinstance(inner, exp.Star)
    is_qualified = isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)

    if not schema:
        if is_bare:
            result.unresolved.append(
                Unresolved(kind="STAR_UNRESOLVED", expression="*",
                           reason="bare * requires schema metadata"))
        elif is_qualified:
            tbl_alias = inner.table
            resolved = alias_map.get(tbl_alias, tbl_alias) if tbl_alias else None
            result.unresolved.append(
                Unresolved(kind="STAR_UNRESOLVED",
                           expression=inner.sql(dialect=DIALECT),
                           reason="qualified .* requires schema metadata",
                           table=resolved))
        return 1  # occupies 1 projection position in the no-schema case

    # Expand with schema
    if is_qualified:
        tbl_alias = inner.table
        resolved = alias_map.get(tbl_alias, tbl_alias) if tbl_alias else None
        if resolved:
            cols = schema.get(_normalize_table_name(resolved))
            if cols:
                for c in cols:
                    result.columns.append(
                        Column(name=c, type="DIRECT",
                               expression=f"{tbl_alias}.{c}",
                               upstream=[ColumnRef(table=resolved, column=c)]))
                return len(cols)
        # Fall through to STAR_UNRESOLVED if schema doesn't have this table
        result.unresolved.append(
            Unresolved(kind="STAR_UNRESOLVED",
                       expression=inner.sql(dialect=DIALECT),
                       reason="table not found in schema",
                       table=resolved))
        return 1

    # Bare * — expand using first source table's schema
    for tbl_alias, tbl_fq in alias_map.items():
        cols = schema.get(_normalize_table_name(tbl_fq))
        if cols:
            for c in cols:
                result.columns.append(
                    Column(name=c, type="DIRECT",
                           expression=f"{tbl_alias}.{c}",
                           upstream=[ColumnRef(table=tbl_fq, column=c)]))
            return len(cols)

    result.unresolved.append(
        Unresolved(kind="STAR_UNRESOLVED", expression="*",
                   reason="bare * requires schema metadata"))
    return 1


_TYPE_RANK: Dict[str, int] = {
    "AGGREGATE": 6, "WINDOW": 5, "CONDITIONAL": 4,
    "LITERAL_SUBQUERY": 3, "EXPRESSION": 2, "DIRECT": 1, "CONSTANT": 0,
}


def _process_union_projection(
    proj_pos: int,
    col_name: str,
    branches: List[exp.Select],
    result: LineageResult,
    schema: Optional[Dict[str, List[str]]] = None,
    target_col: Optional[str] = None,
):
    """Trace a UNION ALL column by tracing each branch's SQL independently.

    This avoids the problem where sqlglot returns positional Select-leaves for
    unqualified columns from branches other than the first, which the old single-
    call approach misclassified as LEAF_SELECT noise.
    """
    branch_exprs: List[str] = []
    branch_types: List[str] = []
    all_upstream: List[ColumnRef] = []
    ambig_all: List[str] = []
    seen_unresolved: set = set()

    for branch_sel in branches:
        if proj_pos >= len(branch_sel.expressions):
            continue
        branch_proj = branch_sel.expressions[proj_pos]
        b_name, b_inner = _extract_name_inner(branch_proj)
        b_type = _classify(b_inner)
        b_expr = b_inner.sql(dialect=DIALECT)
        branch_exprs.append(b_expr)
        branch_types.append(b_type)

        if b_type == "CONSTANT":
            continue  # literals contribute no upstream

        branch_sql = branch_sel.sql(dialect=DIALECT)
        trace_col = b_name if (b_name and b_name != "*") else col_name

        sub_result = LineageResult(task_name=result.task_name, target_table=result.target_table)
        upstream, ambig = _trace_via_lineage(trace_col, branch_sql, sub_result)

        # Attach this branch's expression to each upstream ref so the CSV
        # can emit one expression per row rather than a merged string.
        # Do NOT add b_expr to cte_expressions — it's the branch-level
        # expression, not a CTE layer.  Deeper CTE layers within the branch
        # are already captured by _trace_via_lineage in u.cte_expressions.
        # Strip all leading cte_expressions that duplicate the branch expression.
        # cte_expressions are in outer→inner order from _walk.
        for u in upstream:
            u.expression = b_expr
            while u.cte_expressions and u.cte_expressions[0] == b_expr:
                u.cte_expressions.pop(0)
                if u.scope_alias_maps:
                    u.scope_alias_maps.pop(0)
        all_upstream.extend(upstream)

        for a in ambig:
            if a not in ambig_all:
                ambig_all.append(a)
        for u in sub_result.unresolved:
            key = (u.kind, u.expression, u.column)
            if key not in seen_unresolved:
                seen_unresolved.add(key)
                result.unresolved.append(u)

    # Deduplicate upstream by (table, column, expression) — keep separate rows
    # when the same source column appears in multiple branches with different
    # expressions (e.g. `col` directly vs `CASE WHEN ... THEN col ...`).
    seen_up: set = set()
    deduped: List[ColumnRef] = []
    for u in all_upstream:
        key = (u.table, u.column, u.expression)
        if key not in seen_up:
            seen_up.add(key)
            deduped.append(u)

    # Column.expression = first non-constant branch expression (JSON reference).
    # The per-row expression for CSV comes from ColumnRef.expression above.
    first_expr = next((e for e, t in zip(branch_exprs, branch_types) if t != "CONSTANT"), col_name)
    col_type = max(branch_types, key=lambda t: _TYPE_RANK.get(t, 0)) if branch_types else "DIRECT"

    out_name = target_col if target_col else col_name
    result.columns.append(
        Column(name=out_name, type=col_type, expression=first_expr, upstream=deduped)
    )

    for col in ambig_all:
        result.unresolved.append(
            Unresolved(
                kind="AMBIGUOUS_COLUMN",
                expression=col,
                reason="cannot determine source table without schema",
                column=col,
            )
        )


def _process_projection(
    proj: exp.Expression,
    src_sql: str,
    alias_map: Dict[str, str],
    result: LineageResult,
    schema: Optional[Dict[str, List[str]]] = None,
    target_col: Optional[str] = None,
    subquery_col_map: Optional[Dict[str, Dict[str, exp.Expression]]] = None,
):
    name, inner = _extract_name_inner(proj)

    # Resolve column references into inline subquery expressions (one level).
    # E.g. ``t2.name`` where t2 is an inline subquery containing
    # ``FIRST_VALUE(name) OVER (...)`` → surface that window expression directly.
    # Only resolve when the subquery column is non-trivial (not a bare Column),
    # so simple pass-throughs like ``t5.question → question`` remain unchanged.
    resolved_inner = inner
    if subquery_col_map and isinstance(inner, exp.Column) and inner.table:
        alias_cols = subquery_col_map.get(inner.table)
        if alias_cols is not None:
            col_def = alias_cols.get(inner.name)
            if col_def is not None and not isinstance(col_def, exp.Column):
                resolved_inner = col_def

    col_type = _classify(resolved_inner)
    expr_sql = resolved_inner.sql(dialect=DIALECT)

    # Constants (literals, booleans, nulls) have no upstream — skip lineage trace
    # to avoid spurious LEAF_SELECT unresolved entries.
    if col_type == "CONSTANT":
        upstream: List[ColumnRef] = []
        ambig_cols: List[str] = []
    else:
        upstream, ambig_cols = _trace_via_lineage(name, src_sql, result)

    # For a window function surfaced via subquery resolution, restrict upstream
    # to columns used in the value expression (exclude PARTITION BY / ORDER BY cols).
    if isinstance(resolved_inner, exp.Window) and resolved_inner is not inner:
        window_val_cols = {c.name for c in resolved_inner.this.find_all(exp.Column)}
        if window_val_cols:
            upstream = [u for u in upstream if u.column in window_val_cols]

    # Strip leading cte_expressions that duplicate the column's own expression.
    # When the outer SELECT's expression is a simple column pass-through
    # (e.g. `a.plan_id`) and the UNION branch also has `a.plan_id`, we get
    # consecutive identical entries. Strip all leading matches.
    # cte_expressions are in outer→inner order from _walk.
    for u in upstream:
        while u.cte_expressions and u.cte_expressions[0] == expr_sql:
            u.cte_expressions.pop(0)
            if u.scope_alias_maps:
                u.scope_alias_maps.pop(0)

    col_name = target_col if target_col else name

    result.columns.append(
        Column(name=col_name, type=col_type, expression=expr_sql, upstream=upstream)
    )

    for col in ambig_cols:
        result.unresolved.append(
            Unresolved(
                kind="AMBIGUOUS_COLUMN",
                expression=col,
                reason="cannot determine source table without schema",
                column=col,
            )
        )


def _extract_name_inner(proj: exp.Expression) -> Tuple[str, exp.Expression]:
    if isinstance(proj, exp.Alias):
        return proj.alias, proj.this
    if isinstance(proj, exp.Column):
        return proj.name, proj
    if isinstance(proj, exp.Star):
        return "*", proj
    # Anonymous expression — fall back to its SQL text as a name.
    return proj.sql(dialect=DIALECT), proj


def _classify(node: exp.Expression) -> str:
    if isinstance(node, exp.Window):
        return "WINDOW"
    if isinstance(node, exp.AggFunc):
        return "AGGREGATE"
    if isinstance(node, (exp.Case, exp.If)):
        return "CONDITIONAL"
    if isinstance(node, exp.Subquery):
        return "LITERAL_SUBQUERY"
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return "CONSTANT"
    if isinstance(node, exp.Column):
        return "DIRECT"
    return "EXPRESSION"


# -- lineage traversal ---------------------------------------------------


def _trace_via_lineage(
    column_name: str, src_sql: str, result: LineageResult
) -> Tuple[List[ColumnRef], List[str]]:
    """Run sqlglot.lineage and post-process the full tree into our edge model.

    Walks the lineage tree (not just leaves) so we can:
      - Capture **all** CTE/subquery expressions from intermediate Select nodes
        along the root-to-leaf path, stored as ``ColumnRef.cte_expressions``
        (inner→outer order).  Supports N nested layers, not just 2.
      - Resolve ``Table:*`` leaves whose parent Select node carries the real
        column name (e.g. ``curt_term_stat`` under ``SELECT * FROM t1``).

    Returns (upstream column refs, ambiguous column names).
    """
    try:
        node = sg_lineage.lineage(column_name, src_sql, dialect=DIALECT)
    except Exception as e:
        result.unresolved.append(
            Unresolved(
                kind="LINEAGE_ERROR",
                expression=column_name,
                reason=f"{type(e).__name__}: {e}",
                column=column_name,
            )
        )
        return [], []

    # Walk the full tree, collecting per-leaf context.
    # Each entry: (leaf_name, source, col_part, cte_exprs)
    #   cte_exprs = list of (expression, scope_alias_map) tuples along the path,
    #               in outer→inner order.
    _CteEntry = Tuple[str, Dict[str, str]]
    _LeafInfo = Tuple[str, exp.Expression, str, List[_CteEntry]]
    table_leaves: List[_LeafInfo] = []
    placeholder_leaves: List[_LeafInfo] = []
    other_leaves: List[_LeafInfo] = []

    def _scope_aliases(sel: exp.Select) -> Dict[str, str]:
        """Build a local alias map for a single SELECT's FROM/JOIN only (non-recursive)."""
        local: Dict[str, str] = {}
        from_ = sel.args.get("from_") or sel.args.get("from")
        if from_ is not None:
            node = getattr(from_, "this", None)
            if isinstance(node, exp.Table):
                fq = _qualified_table(node)
                if node.alias:
                    local[node.alias] = fq
                if node.name not in local:
                    local[node.name] = fq
            elif isinstance(node, exp.Subquery) and node.alias:
                # Subquery alias: map to the first Table inside it
                first_tbl = node.find(exp.Table)
                if first_tbl is not None:
                    if first_tbl.name in cte_names_set:
                        local[node.alias] = first_tbl.name
                    else:
                        local[node.alias] = _qualified_table(first_tbl)
        for join in sel.args.get("joins") or []:
            jn = join.this
            if isinstance(jn, exp.Table):
                fq = _qualified_table(jn)
                if jn.alias:
                    local[jn.alias] = fq
                if jn.name not in local:
                    local[jn.name] = fq
            elif isinstance(jn, exp.Subquery) and jn.alias:
                first_tbl = jn.find(exp.Table)
                if first_tbl is not None:
                    if first_tbl.name in cte_names_set:
                        local[jn.alias] = first_tbl.name
                    else:
                        local[jn.alias] = _qualified_table(first_tbl)
        return local

    # Pre-scan CTE names for scope resolution
    cte_names_set: set = set()
    try:
        parsed_src = sqlglot.parse_one(src_sql, dialect=DIALECT, **PARSE_OPTS)
        if isinstance(parsed_src, (exp.Select, exp.Union)):
            pw = parsed_src.args.get("with") or parsed_src.args.get("with_")
            if pw is not None:
                for cte in pw.expressions:
                    cte_names_set.add(cte.alias)
    except Exception:
        pass

    def _walk(node, cte_exprs: Optional[List[_CteEntry]] = None, select_col_name: Optional[str] = None):
        """Walk the lineage tree, accumulating CTE expressions and column context.

        cte_exprs: accumulated list of (expression, scope_alias_map) tuples.
          Every intermediate Select node (including root) appends its entry
          so leaves get the full path.  The caller strips the outermost entry
          if it duplicates Column.expression.

        select_col_name: when a parent Select node has a qualified name like
          ``t1.curt_term_stat``, this is ``curt_term_stat``.  If the child is
          a ``Table:*`` leaf, we use this as the real column name instead of ``*``.
        """
        if cte_exprs is None:
            cte_exprs = []
        src = node.source
        leaf_name = node.name
        col_part = leaf_name.split(".")[-1] if leaf_name else None

        # If this node is a Select (intermediate), its expression is the
        # CTE/subquery expression for everything below it.
        if isinstance(src, exp.Select):
            inner_expr = node.expression.sql(dialect=DIALECT)
            # Strip trailing alias/comment: "SUM(...) AS ovd_int /* comment */"
            alias_node = node.expression.args.get("alias")
            if alias_node:
                inner_expr = node.expression.this.sql(dialect=DIALECT)
            # Build scope-local alias map for this SELECT
            scope_am = _scope_aliases(src)
            # A bare '*' expression is a passthrough (SELECT *) — not a real
            # CTE expression. Skip it to avoid noise.
            if inner_expr.strip() != '*':
                child_exprs = cte_exprs + [(inner_expr, scope_am)]  # append: outer→inner
            else:
                child_exprs = cte_exprs
            # Propagate the column name from this Select node to children.
            child_col = col_part if col_part != "*" else None
            for d in node.downstream:
                _walk(d, cte_exprs=child_exprs, select_col_name=child_col)
            return

        # Terminal node
        if not node.downstream:
            # For Table:* leaves with a propagated column name, resolve the star.
            if isinstance(src, exp.Table) and col_part == "*" and select_col_name:
                col_part = select_col_name
                leaf_name = f"{leaf_name.rsplit('.', 1)[0]}.{col_part}" if '.' in leaf_name else col_part
            entry = (leaf_name, src, col_part, list(cte_exprs))
            if isinstance(src, exp.Table):
                table_leaves.append(entry)
            elif isinstance(src, (exp.Literal, exp.Boolean, exp.Null)):
                return  # CONSTANT — no upstream edge
            elif isinstance(src, exp.Placeholder):
                placeholder_leaves.append(entry)
            else:
                other_leaves.append(entry)
            return

        # Non-Select, non-terminal (unlikely but safe)
        for d in node.downstream:
            _walk(d, cte_exprs=cte_exprs, select_col_name=select_col_name)

    _walk(node)

    # If no table leaves were found, fall back to other leaves as before
    suppressed_other = [] if table_leaves else other_leaves

    upstream: List[ColumnRef] = []
    ambig: List[str] = []
    seen: set = set()
    star_seen: set = set()

    for leaf_name, src, col_part, cte_exprs in table_leaves:
        tbl_fq = _qualified_table(src)
        if col_part == "*":
            if tbl_fq not in star_seen:
                star_seen.add(tbl_fq)
                result.unresolved.append(
                    Unresolved(
                        kind="STAR_UNRESOLVED",
                        expression=f"{src.alias_or_name}.*",
                        reason="propagated through .* in CTE/subquery",
                        table=tbl_fq,
                    )
                )
            continue
        key = (tbl_fq, col_part)
        if key in seen:
            continue
        seen.add(key)
        cte_strs = [t[0] for t in cte_exprs]
        cte_scope_maps = [t[1] for t in cte_exprs]
        upstream.append(ColumnRef(table=tbl_fq, column=col_part,
                                   cte_expressions=cte_strs,
                                   scope_alias_maps=cte_scope_maps))

    for leaf_name, src, col_part, cte_exprs in placeholder_leaves:
        if col_part and col_part not in ambig:
            ambig.append(col_part)
        key = (None, col_part)
        if key not in seen:
            seen.add(key)
            upstream.append(ColumnRef(table=None, column=col_part))

    for leaf_name, src, col_part, cte_exprs in suppressed_other:
        kind = type(src).__name__
        dedup_key = (f"LEAF_{kind.upper()}", col_part)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        result.unresolved.append(
            Unresolved(
                kind=f"LEAF_{kind.upper()}",
                expression=leaf_name or "",
                reason=f"lineage leaf is {kind}; likely cross-join false positive",
                column=col_part,
            )
        )

    return upstream, ambig


# -- JOIN ----------------------------------------------------------------


def _process_join(join: exp.Join, alias_map: Dict[str, str], result: LineageResult):
    on = join.args.get("on")
    if on is None:
        return
    for eq in on.find_all(exp.EQ):
        left = _column_ref(eq.this, alias_map)
        right = _column_ref(eq.expression, alias_map)
        if left is None or right is None:
            continue
        result.join_keys.append(
            JoinKey(left=left, right=right, expression=eq.sql(dialect=DIALECT))
        )


def _column_ref(node: exp.Expression, alias_map: Dict[str, str]) -> Optional[ColumnRef]:
    cols = list(node.find_all(exp.Column))
    if not cols:
        return None
    col = cols[0]
    tbl_alias = col.table
    fq = alias_map.get(tbl_alias, tbl_alias) if tbl_alias else None
    return ColumnRef(table=fq, column=col.name)
