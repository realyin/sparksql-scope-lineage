# Scope Model

Scope Lineage models SQL as a graph of named scopes instead of flattening every
column reference immediately.

## Scope Types

| Scope | Example | Meaning |
| --- | --- | --- |
| `ROOT` | outer SELECT | the statement output written to the target table |
| `cte:name` | `WITH name AS (...)` | CTE output columns |
| `subq:alias` | `FROM (SELECT ...) alias` | derived table output columns |
| `union:main` | `SELECT ... UNION ALL ...` | synthetic UNION alignment scope |
| `union:main:b01` | first UNION branch | branch-local output columns |
| physical table | `ods.users` | source table node |

## Invariant

Each `ScopeColumn.sources` list points only to direct upstream scopes or physical
tables. It does not jump across layers. Full physical lineage is computed later
by tracing through the graph.

This keeps intermediate transformations inspectable and makes audit failures
local: a bad edge can be tied to one scope instead of a flattened global graph.

