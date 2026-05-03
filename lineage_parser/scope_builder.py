"""Scope tree builder: parse SQL, qualify, build_scope, assign scope_ids, create ScopeData stubs.

Entry point: parse_scope_lineage(sql, task_name, schema=None) -> ScopeLineageResult
"""

from __future__ import annotations

import sqlglot
from sqlglot import ErrorLevel
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify as sg_qualify
from sqlglot.optimizer.scope import traverse_scope, Scope, ScopeType

from .parser import (
    _qualified_table,
    _unwrap_target,
    _unwrap_subquery,
    _leftmost_select,
    _collect_cte_map,
    _collect_union_branches,
    _normalize_table_name,
)
from .scope_types import (
    ScopeData,
    ScopeGraph,
    ScopeGraphEdge,
    ScopeLineageResult,
    Diagnostics,
    DiagnosticWarning,
)
from .scope_resolver import resolve_all
from .scope_warnings import detect_warnings
from .scope_role_inferrer import infer_roles
from .sqlglot_config import suppress_invalid_json_path_warnings

DIALECT = "spark"
PARSE_OPTS = {"error_level": ErrorLevel.IGNORE}
suppress_invalid_json_path_warnings()

# Attr name for attaching scope_id to sqlglot Scope objects
_SCOPE_ID_ATTR = "_lineage_scope_id"


def _collect_insert_trees(sql: str) -> list:
    """Parse SQL and return all top-level INSERT/MERGE expression trees."""
    trees = sqlglot.parse(sql, dialect=DIALECT, **PARSE_OPTS)
    return [
        t for t in trees
        if t is not None and (
            isinstance(t, (exp.Insert, exp.Merge))
            or t.find(exp.Insert) is not None
            or t.find(exp.Merge) is not None
        )
    ]


def parse_scope_lineage(
    sql: str, task_name: str, schema: dict | None = None
) -> ScopeLineageResult:
    """Parse SQL into a scope-based lineage result with full column resolution."""
    insert_trees = _collect_insert_trees(sql)
    if not insert_trees:
        raise ValueError("No INSERT/MERGE statement found")

    # For now, handle the first INSERT/MERGE only (multi-statement later)
    tree = insert_trees[0]

    if isinstance(tree, exp.Merge) or (
        tree.find(exp.Merge) is not None and tree.find(exp.Insert) is None
    ):
        return _build_merge_scope(tree, task_name, schema)
    else:
        return _build_insert_scope(tree, task_name, schema)


def parse_all_scope_lineage(
    sql: str, task_name: str, schema: dict | None = None
) -> list[ScopeLineageResult]:
    """Parse all INSERT/MERGE statements; return one ScopeLineageResult per target."""
    insert_trees = _collect_insert_trees(sql)
    if not insert_trees:
        raise ValueError("No INSERT/MERGE statement found")

    results: list[ScopeLineageResult] = []
    for i, tree in enumerate(insert_trees):
        sub = f"{task_name}#{i}" if len(insert_trees) > 1 else task_name
        try:
            if isinstance(tree, exp.Merge) or (
                tree.find(exp.Merge) is not None and tree.find(exp.Insert) is None
            ):
                results.append(_build_merge_scope(tree, sub, schema))
            else:
                results.append(_build_insert_scope(tree, sub, schema))
        except Exception as e:
            result = ScopeLineageResult(task_id=sub, target_table="", stmt_kind="INSERT")
            result.diagnostics.warnings.append(
                DiagnosticWarning(
                    type="LINEAGE_ERROR",
                    scope="ROOT",
                    msg=f"{type(e).__name__}: {e}",
                )
            )
            results.append(result)
    return results


def _qualify_ast(ast: exp.Expression) -> exp.Expression:
    """Run sqlglot qualify with graceful degradation."""
    try:
        return sg_qualify(
            ast,
            dialect=DIALECT,
            validate_qualify_columns=False,
            infer_schema=True,
            expand_stars=False,
        )
    except Exception:
        return ast


def _build_insert_scope(
    tree: exp.Expression, task_name: str, schema: dict | None = None
) -> ScopeLineageResult:
    """Build scope tree for INSERT statements."""
    insert = tree if isinstance(tree, exp.Insert) else tree.find(exp.Insert)
    target = _unwrap_target(insert.this) if insert.this is not None else None
    target_table = _qualified_table(target) if isinstance(target, exp.Table) else ""

    is_overwrite = bool(insert.args.get("overwrite"))
    stmt_kind = "INSERT_OVERWRITE" if is_overwrite else "INSERT"

    result = ScopeLineageResult(
        task_id=task_name,
        target_table=target_table,
        stmt_kind=stmt_kind,
        diagnostics=Diagnostics(),
    )

    if insert.expression is None:
        return result

    src_expr = _build_source_expression(insert)
    qualified = _qualify_ast(src_expr)

    if qualified is src_expr:
        # Check if qualify would have failed
        try:
            sg_qualify(src_expr, dialect=DIALECT,
                       validate_qualify_columns=False, infer_schema=True,
                       expand_stars=False)
        except Exception:
            result.diagnostics.fallback_used = True

    _build_result_from_scope(qualified, result, target_table, schema)
    result.diagnostics.stats = _compute_stats(result)
    detect_warnings(result)
    infer_roles(result)
    return result


def _build_merge_scope(
    tree: exp.Expression, task_name: str, schema: dict | None = None
) -> ScopeLineageResult:
    """Build scope tree for MERGE statements.

    build_scope on the full MERGE AST produces:
      ROOT (Subquery expression) -> child SUBQUERY scope (the USING Select)
    """
    merge = tree if isinstance(tree, exp.Merge) else tree.find(exp.Merge)
    target = _unwrap_target(merge.this) if merge.this is not None else None
    target_table = _qualified_table(target) if isinstance(target, exp.Table) else ""

    result = ScopeLineageResult(
        task_id=task_name,
        target_table=target_table,
        stmt_kind="MERGE",
        diagnostics=Diagnostics(),
    )

    using = merge.args.get("using")
    if using is None:
        return result

    qualified = _qualify_ast(merge)
    _build_result_from_scope(qualified, result, target_table, schema)
    result.diagnostics.stats = _compute_stats(result)
    detect_warnings(result)
    infer_roles(result)
    return result


def _build_result_from_scope(
    qualified_expr, result: ScopeLineageResult, target_table: str,
    schema: dict | None = None,
) -> None:
    """Common logic: assign IDs, create stubs, collect physical tables, resolve columns.

    Uses traverse_scope(qualified_expr) to build the scope list so that CTE scopes
    inside MERGE...WITH are not missed (build_scope().traverse() silently skips them
    for MERGE statements). The root scope is extracted from the traversal result.
    """
    all_scopes = list(traverse_scope(qualified_expr))
    root_scope = next((s for s in reversed(all_scopes) if s.is_root), None)

    # Step 1: Assign scope_ids to every scope (children before parents — traverse_scope order)
    for sg_scope in all_scopes:
        scope_id = _compute_scope_id(sg_scope)
        setattr(sg_scope, _SCOPE_ID_ATTR, scope_id)

    # Step 1b: Deduplicate IDs — same alias at different nesting levels must not collide.
    # Process in all_scopes order (bottom-up): first occurrence keeps natural ID,
    # subsequent occurrences get _2, _3, etc.
    _seen_ids: dict[str, int] = {}
    for sg_scope in all_scopes:
        sid = getattr(sg_scope, _SCOPE_ID_ATTR)
        count = _seen_ids.get(sid, 0) + 1
        _seen_ids[sid] = count
        if count > 1:
            setattr(sg_scope, _SCOPE_ID_ATTR, f"{sid}_{count}")

    # Step 2: Create synthetic UNION scopes for any scope with Union expression + union_scopes
    if root_scope:
        _create_union_scopes_recursive(root_scope, result)
    for sg_scope in all_scopes:
        if (
            not sg_scope.is_union
            and isinstance(sg_scope.expression, exp.Union)
            and sg_scope.union_scopes
        ):
            union_scope_id = _union_scope_id_for_container(
                getattr(sg_scope, _SCOPE_ID_ATTR, None)
            )
            if union_scope_id not in result.scopes:
                _create_union_scope(sg_scope, result)

    # Step 3: Create ScopeData stubs for each scope
    # Skip all is_union scopes: they are handled entirely by _create_union_scope.
    # - Leaf branches got their real "union:xxx:bNN" IDs assigned in Step 2.
    # - Intermediate Union scopes still have "_union_tmp_*" placeholder IDs.
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if scope_id is None:
            continue

        # Skip all is_union scopes — handled by _create_union_scope
        if sg_scope.is_union:
            continue

        kind = _scope_kind(sg_scope)
        alias_in_parent = _find_alias_in_parent(sg_scope)

        if scope_id in result.scopes:
            # Already created by _create_union_scopes_recursive (e.g. union scope or branch)
            if alias_in_parent:
                result.scopes[scope_id].alias_in_parent = alias_in_parent
            continue

        result.scopes[scope_id] = ScopeData(
            kind=kind,
            alias_in_parent=alias_in_parent,
        )

    # Ensure ROOT exists
    if "ROOT" not in result.scopes:
        result.scopes["ROOT"] = ScopeData(kind="root")
    result.scopes["ROOT"].writes_to = target_table

    # Step 4: Collect physical table nodes
    physical_tables = set()
    for sg_scope in all_scopes:
        for _alias, src in sg_scope.sources.items():
            if isinstance(src, exp.Table):
                fq = _qualified_table(src)
                physical_tables.add(fq)

    result.source_tables = sorted(physical_tables)
    all_nodes = set(result.scopes.keys()) | physical_tables
    result.scope_graph.nodes = sorted(all_nodes)

    # Step 5: Resolve columns for all scopes
    resolve_all(result, root_scope, all_scopes, schema)


def _build_source_expression(insert: exp.Insert) -> exp.Expression:
    """Extract source expression from INSERT, with WITH grafted and wrappers unwrapped."""
    src = insert.expression.copy()
    w = insert.args.get("with_")

    if isinstance(src, exp.Subquery):
        inner = src.this
        if isinstance(inner, (exp.Select, exp.Union)):
            src = inner.copy()
            sw = src.args.get("with_")
            if sw is None and w is not None:
                src.set("with_", w.copy())
            return src

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


def _compute_scope_id(sg_scope: Scope) -> str:
    """Compute a scope_id for a single sqlglot Scope."""
    if sg_scope.is_root:
        return "ROOT"

    if sg_scope.is_cte:
        # CTE name from expression.parent.alias
        cte_node = sg_scope.expression.parent
        if hasattr(cte_node, "alias") and cte_node.alias:
            return f"cte:{cte_node.alias}"
        # Fallback: find in parent sources
        if sg_scope.parent:
            for name, src in sg_scope.parent.sources.items():
                if src is sg_scope:
                    return f"cte:{name}"
        return "cte:unknown"

    if sg_scope.is_union:
        # Placeholder: _create_union_scope will assign the real ID after flattening.
        # We use a temporary ID based on object identity to avoid collisions.
        return f"_union_tmp_{id(sg_scope)}"

    if sg_scope.is_derived_table or sg_scope.is_subquery:
        # Find alias in parent sources first
        alias = _find_alias_in_parent(sg_scope)
        if alias:
            return f"subq:{alias}"
        # Fallback: check if parent's expression has a Subquery with alias
        # (for MERGE USING subquery where sources dict doesn't list it)
        if sg_scope.parent and isinstance(sg_scope.parent.expression, exp.Subquery):
            sq_alias = sg_scope.parent.expression.alias
            if sq_alias:
                return f"subq:{sq_alias}"
        return "subq:derived_0"

    if sg_scope.is_udtf:
        alias = _find_alias_in_parent(sg_scope)
        if alias:
            return f"udtf:{alias}"
        return "udtf:unknown_0"

    return "scope:unknown"


def _create_union_scopes_recursive(sg_scope: Scope, result: ScopeLineageResult) -> None:
    """Walk the scope tree and create synthetic UNION scopes wherever a scope has
    Union expression + union_scopes children.

    Handles both top-level UNION and UNION inside CTEs.
    UNION chains (A UNION ALL B UNION ALL C) are flattened into a single union scope
    with N branches, not nested union scopes.
    """
    # Check if this scope has UNION children
    if sg_scope.union_scopes and isinstance(sg_scope.expression, exp.Union):
        _create_union_scope(sg_scope, result)

    # Recurse into child scopes
    for child in sg_scope.cte_scopes:
        _create_union_scopes_recursive(child, result)
    for child in sg_scope.derived_table_scopes:
        _create_union_scopes_recursive(child, result)
    for child in sg_scope.subquery_scopes:
        _create_union_scopes_recursive(child, result)
    # Note: do NOT recurse into union_scopes here — _create_union_scope already
    # flattened the chain and assigned IDs to all leaf branches. Recursing into
    # union_scopes would re-enter scopes that have already been handled.
    for child in sg_scope.udtf_scopes:
        _create_union_scopes_recursive(child, result)


def _flatten_union_branches(sg_scope: Scope) -> list[Scope]:
    """Flatten a left-deep Union tree into a flat list of leaf SELECT scopes.

    sqlglot parses `A UNION ALL B UNION ALL C` as Union(Union(A, B), C),
    producing a nested scope tree. We want a single flat union scope with 3 branches.
    A branch that is itself a Union scope is recursively flattened.
    """
    leaves = []
    for branch in sg_scope.union_scopes:
        if isinstance(branch.expression, exp.Union) and branch.union_scopes:
            # Nested union — flatten it
            leaves.extend(_flatten_union_branches(branch))
        else:
            leaves.append(branch)
    return leaves


def _create_union_scope(container_scope: Scope, result: ScopeLineageResult) -> None:
    """Create a synthetic UNION scope for a scope that has Union expression + union_scopes.

    Flattens UNION chains so A UNION ALL B UNION ALL C produces one union scope
    with 3 branches, not nested union scopes.
    """
    # Flatten the left-deep union tree into leaf branches
    flat_branches = _flatten_union_branches(container_scope)
    if not flat_branches:
        return

    container_id = getattr(container_scope, _SCOPE_ID_ATTR, None)
    union_scope_id = _union_scope_id_for_container(container_id)
    context = union_scope_id.split(":", 1)[1]

    # Assign branch IDs to the flattened leaf branches
    branch_ids = []
    for i, branch in enumerate(flat_branches):
        branch_id = f"union:{context}:b{i + 1:02d}"
        setattr(branch, _SCOPE_ID_ATTR, branch_id)
        branch_ids.append(branch_id)

    # Also fix the _lineage_scope_id on intermediate Union scopes that we
    # flattened away — they should NOT appear as separate scopes in the result.
    # Point them to the union scope so they're treated as aliases.
    for branch in container_scope.union_scopes:
        if isinstance(branch.expression, exp.Union) and branch.union_scopes:
            setattr(branch, _SCOPE_ID_ATTR, union_scope_id)

    # Determine set_op type
    union_expr = container_scope.expression
    set_op = "UNION_ALL"
    if hasattr(union_expr, "args"):
        kind = union_expr.args.get("kind", "")
        if kind and "ALL" not in str(kind).upper():
            set_op = "UNION"

    result.scopes[union_scope_id] = ScopeData(
        kind="union",
        set_op=set_op,
        branches=branch_ids,
    )

    # Create branch stubs
    for i, branch_id in enumerate(branch_ids):
        if branch_id not in result.scopes:
            result.scopes[branch_id] = ScopeData(
                kind="union_branch",
                branch_index=i,
            )

    # If container is ROOT, also create the ROOT stub
    if container_id == "ROOT":
        if "ROOT" not in result.scopes:
            result.scopes["ROOT"] = ScopeData(kind="root")


def _union_scope_id_for_container(container_id: str | None) -> str:
    """Return the synthetic union scope ID for a container scope ID."""
    if not container_id or container_id == "ROOT":
        context = "main"
    elif ":" in container_id:
        context = container_id.split(":", 1)[1]
    else:
        context = container_id
    return f"union:{context}"


def _scope_kind(sg_scope: Scope) -> str:
    """Map sqlglot ScopeType to our kind string."""
    if sg_scope.is_root:
        return "root"
    if sg_scope.is_cte:
        return "cte"
    if sg_scope.is_union:
        return "union_branch"
    if sg_scope.is_derived_table:
        return "subquery"
    if sg_scope.is_subquery:
        return "subquery"
    if sg_scope.is_udtf:
        return "subquery"
    return "unknown"


def _find_alias_in_parent(sg_scope: Scope) -> str | None:
    """Find the alias this scope uses in its parent scope's sources."""
    if sg_scope.parent is None:
        return None
    for name, src in sg_scope.parent.sources.items():
        if src is sg_scope:
            return name
    return None


def _compute_stats(result: "ScopeLineageResult") -> dict:
    """Compute diagnostics.stats from the fully-built result."""
    cte_count = sum(1 for s in result.scopes.values() if s.kind == "cte")
    subquery_count = sum(1 for s in result.scopes.values() if s.kind == "subquery")
    union_count = sum(1 for s in result.scopes.values() if s.kind == "union")
    union_branch_count = sum(1 for s in result.scopes.values() if s.kind == "union_branch")

    physical_ids: set = set()
    for scope in result.scopes.values():
        for col in scope.columns:
            for src in col.sources:
                if src.scope and src.scope not in result.scopes and src.scope not in ("UNKNOWN", ""):
                    physical_ids.add(src.scope)
        for j in scope.joins:
            for sid in (j.left_scope, j.right_scope):
                if sid and sid not in result.scopes and sid not in ("UNKNOWN", ""):
                    physical_ids.add(sid)

    agg_count = window_count = case_count = join_count = 0
    for scope in result.scopes.values():
        join_count += len(scope.joins)
        for col in scope.columns:
            if col.transform == "AGGREGATE":
                agg_count += 1
            elif col.transform == "WINDOW":
                window_count += 1
            elif col.transform == "CONDITIONAL":
                case_count += 1

    def _depth(scope_id: str, memo: dict, visiting: set) -> int:
        if scope_id in memo:
            return memo[scope_id]
        if scope_id in visiting:   # cycle detected — return 0 to break recursion
            return 0
        visiting.add(scope_id)
        scope = result.scopes.get(scope_id)
        if scope is None:
            visiting.discard(scope_id)
            memo[scope_id] = 0
            return 0
        d = 1 + max((_depth(dep, memo, visiting) for dep in scope.depends_on), default=0)
        visiting.discard(scope_id)
        memo[scope_id] = d
        return d

    memo: dict = {}
    max_depth = max((_depth(sid, memo, set()) for sid in result.scopes), default=0)
    scope_count = len(result.scopes) + len(physical_ids)

    return {
        "scope_count": scope_count,
        "physical_table_count": len(physical_ids),
        "cte_count": cte_count,
        "subquery_count": subquery_count,
        "union_count": union_count,
        "union_branch_count": union_branch_count,
        "max_depth": max_depth,
        "case_when_count": case_count,
        "window_function_count": window_count,
        "join_count": join_count,
        "aggregate_function_count": agg_count,
    }
