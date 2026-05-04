# How It Works

This document explains the main design choices behind Scope Lineage.

## 1. Scope First, Physical Lineage Later

The parser does not immediately flatten every target column to physical tables.
Instead, it first builds a scope graph.

For example:

```sql
INSERT OVERWRITE TABLE mart.orders
WITH cleaned AS (
  SELECT order_id, user_id FROM ods.orders WHERE deleted = false
)
SELECT order_id, user_id FROM cleaned
```

The graph contains:

```text
ods.orders -> cte:cleaned -> ROOT
```

`ROOT.order_id` points to `cte:cleaned.order_id`, and
`cte:cleaned.order_id` points to `ods.orders.order_id`.

Only when a user asks for physical lineage does the view layer trace through the
graph.

## 2. Stable Scope IDs

Each query block receives a stable id:

| SQL construct | Scope id |
| --- | --- |
| target-writing SELECT | `ROOT` |
| CTE | `cte:<name>` |
| subquery | `subq:<alias>` |
| UNION container | `union:<context>` |
| UNION branch | `union:<context>:bNN` |
| UDTF / lateral output | `udtf:<alias>` |

When the same alias appears in nested scopes, ids are deduplicated with suffixes
such as `subq:a_2`.

## 3. Column Resolution

For each SELECT-like scope, the resolver walks projections and creates
`ScopeColumn` objects.

Each output column includes:

- output name,
- transform kind,
- original expression,
- immediate source references,
- optional metadata for CASE, window, aggregate, UNION, or MERGE branches.

Common transform kinds:

| Transform | Meaning |
| --- | --- |
| `DIRECT` | simple column passthrough |
| `EXPRESSION` | computed expression |
| `CONDITIONAL` | CASE / IF |
| `AGGREGATE` | aggregate function |
| `WINDOW` | window function |
| `CONSTANT` | literal or no-input expression |
| `UNION` | positional UNION alignment |
| `EXPAND_ALL` | unresolved wildcard |

## 4. Qualified And Unqualified References

Qualified references such as `s.user_id` are resolved through the current
scope's sources.

Unqualified references such as `user_id` are resolved conservatively:

1. exact match in upstream scope outputs,
2. exact match in physical table metadata if schema is available,
3. selected fallback paths when the SQL shape makes the source plausible,
4. `UNKNOWN` plus diagnostics when no reliable source exists.

Diagnostics are intentional. The parser should expose uncertainty instead of
silently inventing lineage.

## 5. UNION Alignment

UNION branches are aligned by position, not by name.

```sql
SELECT id, name FROM a
UNION ALL
SELECT user_id, full_name FROM b
```

The synthetic `union:*` scope uses the first branch names (`id`, `name`) and
keeps branch metadata so users can inspect where each branch value came from.

## 6. SELECT Star Handling

`SELECT *` has three possible outcomes:

1. If the upstream scope already has named columns, expand from that scope.
2. If a physical table schema is provided, expand from metadata.
3. Otherwise keep an `EXPAND_ALL` placeholder and emit a warning.

The parser can also materialize columns that are later referenced downstream,
but complete wildcard coverage requires schema metadata.

## 7. MERGE Handling

MERGE UPDATE and INSERT clauses are represented as ROOT columns with branch
metadata:

- `matched` for `WHEN MATCHED THEN UPDATE`
- `not_matched` for `WHEN NOT MATCHED THEN INSERT`

MERGE DELETE is row-level behavior. It is reported as a diagnostic and does not
produce target column lineage.

## 8. Views And Audit

Generated views are deliberately separate from the parser:

- `scope_overview.mmd`: scope-level DAG
- `field_lineage.mmd`: field-level graph
- `physical.mmd`: physical table columns to ROOT columns
- `per_column/*.mmd`: single target column traces

The audit tool reads generated output and checks consistency without reparsing
SQL. This keeps the verification step independent from the parser entry point.

## 9. Why Diagnostics Matter

Static SQL lineage has unavoidable uncertainty:

- schema may be missing,
- columns may be unqualified,
- dialect constructs may be partially supported,
- engine-specific runtime behavior may not appear in SQL text.

Scope Lineage treats those as first-class diagnostics. A partial but explicit
result is usually more useful than a confident-looking but silent guess.

