# SparkSql Scope Lineage

Sparksql Scope Lineage is a scope-aware column lineage parser and audit toolkit for
Spark/Hive SQL. It focuses on complex warehouse SQL where column lineage is hard
to explain: nested CTEs, subqueries, UNION branches, MERGE statements, window
functions, lateral views, and `SELECT *` expansion with external schema
metadata.


## Why This Exists

Many SQL lineage tools can trace simple statements, but become hard to trust
when SQL contains multiple scopes:

- `WITH` chains and nested subqueries
- `UNION ALL` with positional column alignment
- `SELECT *` from physical tables
- unqualified columns with ambiguous sources
- Spark/Hive constructs such as `LATERAL VIEW`, `posexplode`, and `MERGE`

Scope Lineage keeps intermediate scopes explicit. Instead of flattening all
relationships immediately, it models:

- `ROOT`: the target-writing SELECT
- `cte:*`: CTE outputs
- `subq:*`: derived table outputs
- `union:*` and `union:*:bNN`: UNION scopes and branches
- physical tables

That model makes the output easier to debug, visualize, and audit.

## Current Capabilities

- Parse Spark/Hive-style `INSERT`, `INSERT OVERWRITE`, and `MERGE`
- Build column-level lineage through CTEs, subqueries, UNION, joins, filters,
  aggregates, windows, CASE expressions, and selected UDTFs
- Expand `SELECT *` and `alias.*` when table schema metadata is provided
- Generate JSON output, Markdown reports, and Mermaid diagrams
- Audit generated output with RED/YELLOW/GREEN severity classification
- Report diagnostics instead of silently hiding uncertain bindings

## Install For Local Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Quick Start

```bash
scope-lineage parse \
  --sql-file examples/simple_insert.sql \
  --out /tmp/scope-lineage-demo \
  --md
```

With schema metadata for `SELECT *`:

```bash
scope-lineage parse \
  --sql-file examples/select_star_with_schema.sql \
  --schema examples/table_cols.csv \
  --out /tmp/scope-lineage-star-demo \
  --md
```

## Python API

```python
from lineage_parser import parse_all_scope_lineage

sql = """
INSERT OVERWRITE TABLE mart.user_summary
WITH active_users AS (
  SELECT id, country FROM ods.users WHERE status = 'active'
)
SELECT id, country FROM active_users
"""

results = parse_all_scope_lineage(sql, task_name="user_summary")
root_columns = results[0].scopes["ROOT"].columns
```

## Schema Metadata

`SELECT *` cannot be fully expanded without table columns. Provide schema
metadata as CSV or JSON:

```csv
table_name,column_name
ods.users,id
ods.users,country
ods.users,status
```

```bash
python tools/run_scope_corpus.py \
  --input-dir examples/tasks \
  --out /tmp/scope_output \
  --md \
  --schema examples/table_cols.csv
```

The loader normalizes table names by stripping a leading catalog segment from
three-part names and lower-casing identifiers, so both `catalog.db.table` and
`db.table` can match.

## Audit Output

After generating lineage output, run the audit tool:

```bash
python tools/audit_scope_output.py \
  --out-dir /tmp/scope_output \
  --report /tmp/scope_audit.md \
  --json /tmp/scope_audit.json \
  --fail-on-red
```

The audit intentionally separates:

- **RED**: structural issues such as UNKNOWN sources or internal dangling refs
- **YELLOW**: usable output with accuracy boundaries, such as missing schema
- **GREEN**: no structural issues or significant warnings

## Output Shape

Each parsed statement can produce:

```text
lineage.json
diagnostics.json
views/
  lineage.md
  scope_overview.mmd
  field_lineage.mmd
  physical.mmd
  per_column/*.mmd
```

## Known Limits

- Static parsing does not fully equal runtime Spark semantics.
- `SELECT *` requires external schema metadata for complete coverage.
- Unqualified columns can be ambiguous without schema or table aliases.
- MERGE DELETE is treated as a row-level operation and reported as a diagnostic,
  not as an output column.
- Some enterprise SQL dialect extensions may need incremental parser support.

More detail: [docs/limitations.md](docs/limitations.md).

## Recommended Open Source Positioning

This project should be presented as:

> A scope-aware column lineage parser and audit toolkit for Spark/Hive SQL.

It should not be positioned as a full metadata platform or a guaranteed
production replacement for engine-backed lineage.
