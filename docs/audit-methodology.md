# Audit Methodology

The audit tool checks generated lineage output without re-parsing SQL.

## Severity Levels

| Level | Meaning |
| --- | --- |
| RED | structural issue that can directly break field-level lineage |
| YELLOW | output is usable, but has an accuracy or completeness boundary |
| GREEN | no structural issues or significant warnings |

## RED Examples

- `UNKNOWN` sources in column lineage
- internal source scope exists but referenced column is missing
- source scope is absent from the graph
- ROOT has no columns for a statement with a target table
- core view files are missing

## YELLOW Examples

- `SELECT *` could not be expanded because metadata is missing
- schema-expanded scope is missing a downstream referenced column
- unqualified column was resolved without schema
- complex aggregate or CASE expressions require business review

The distinction matters: YELLOW is often fixed by adding schema metadata or by
reviewing SQL style, while RED usually indicates parser logic or output
consistency needs attention.

