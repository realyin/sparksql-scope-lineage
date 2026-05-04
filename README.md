# sparksql-scope-lineage

Scope-aware column-level lineage for Spark SQL and Hive-style warehouse SQL.

`sparksql-scope-lineage` parses SQL statically, keeps intermediate query scopes
visible, expands `SELECT *` with optional schema metadata, and audits generated
lineage output for structural confidence.

It is useful when you need to answer:

- Which physical columns feed each target column?
- Which CTE, subquery, or UNION branch transformed the column?
- Did `SELECT *` expand completely, or is schema metadata missing?
- Are there UNKNOWN sources or broken internal references in the lineage graph?

## Why Another Lineage Parser?

Complex Spark SQL is rarely a single flat `SELECT col FROM table`.

Real warehouse SQL often contains:

- long `WITH` chains,
- nested derived tables,
- positional `UNION ALL`,
- window functions and CASE expressions,
- `LATERAL VIEW` / generator functions,
- `MERGE INTO`,
- and `SELECT *` from physical tables.

Flattening all column references too early makes these queries hard to debug.
This project keeps every query block as an explicit **scope** and then traces
lineage through those scopes.

## Quick Start

Install in editable mode:

```bash
python -m pip install -e ".[dev]"
```

Parse one SQL file:

```bash
scope-lineage parse \
  --sql-file examples/simple_insert.sql \
  --out /tmp/scope-lineage-demo \
  --md
```

Parse SQL with schema metadata so `SELECT *` can be expanded:

```bash
scope-lineage parse \
  --sql-file examples/select_star_with_schema.sql \
  --schema examples/table_cols.csv \
  --out /tmp/scope-lineage-star-demo \
  --md
```

Run the tests:

```bash
python -m pytest -q
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
root = results[0].scopes["ROOT"]

for column in root.columns:
    print(column.name, column.sources)
```

## Command Line Tools

### Parse One SQL File

```bash
scope-lineage parse \
  --sql-file examples/simple_insert.sql \
  --out /tmp/scope-lineage-demo \
  --md
```

### Parse A Task Directory

Task files are JSON objects with at least `task_name` and `sql`:

```json
{
  "task_name": "simple_insert",
  "sql": "INSERT OVERWRITE TABLE mart.t SELECT id FROM ods.users"
}
```

Run:

```bash
python tools/run_scope_corpus.py \
  --input-dir examples/tasks \
  --out /tmp/scope-output \
  --schema examples/table_cols.csv \
  --md
```

### Audit Generated Output

```bash
python tools/audit_scope_output.py \
  --out-dir /tmp/scope-output/tasks \
  --report /tmp/scope-audit.md \
  --json /tmp/scope-audit.json \
  --fail-on-red
```

The audit report classifies generated output as:

| Level | Meaning |
| --- | --- |
| RED | structural lineage issue, such as UNKNOWN sources or internal dangling refs |
| YELLOW | usable output with known accuracy or completeness boundaries |
| GREEN | no structural issues or significant warnings |

### Compare Two Output Directories

Use this before and after parser changes to check whether generated artifacts
changed:

```bash
python tools/compare_scope_outputs.py \
  --left /tmp/scope-output-before/tasks \
  --right /tmp/scope-output-after/tasks \
  --report /tmp/scope-compare.md \
  --json /tmp/scope-compare.json \
  --fail-on-diff
```

### Summarize Multiple Audit Reports

```bash
python tools/summarize_audit_reports.py \
  --audit /tmp/dwd-audit.json \
  --audit /tmp/support-audit.json \
  --report /tmp/scope-audit-summary.md \
  --json /tmp/scope-audit-summary.json
```

## Output Files

Each parsed statement can produce:

```text
lineage.json
diagnostics.json
lineage.md
views/
  scope_overview.mmd
  field_lineage.mmd
  physical.mmd
  per_column/*.mmd
```

`lineage.json` contains the machine-readable scope graph. Mermaid files are
intended for visual inspection and debugging.

## How It Works

The core idea is simple: **a SQL query is a graph of scopes**.

Examples:

| SQL construct | Scope id example |
| --- | --- |
| outer SELECT writing the target | `ROOT` |
| `WITH users AS (...)` | `cte:users` |
| `FROM (SELECT ...) s` | `subq:s` |
| `SELECT ... UNION ALL SELECT ...` | `union:main` |
| first UNION branch | `union:main:b01` |
| physical table | `ods.users` |

Each scope owns its output columns. A column source points only to the immediate
upstream scope or physical table. Full physical lineage is computed by tracing
through the graph later.

This gives two advantages:

- intermediate transformations remain visible,
- audit failures can be localized to a specific CTE, subquery, or UNION branch.

High-level pipeline:

```text
SQL
  -> parse with sqlglot
  -> build scope tree
  -> assign stable scope ids
  -> resolve columns inside each scope
  -> align UNION branches by position
  -> expand SELECT * when schema is available
  -> build scope graph and diagnostics
  -> render JSON / Mermaid / Markdown
  -> audit output consistency
```

More detail:

- [Scope model](docs/scope-model.md)
- [How it works](docs/how-it-works.md)
- [Schema metadata](docs/schema-metadata.md)
- [Audit methodology](docs/audit-methodology.md)
- [Limitations](docs/limitations.md)

## Schema Metadata

`SELECT *` cannot be fully expanded without table columns. Provide schema
metadata as CSV:

```csv
table_name,column_name
ods.users,id
ods.users,country
ods.users,status
```

or JSON:

```json
{
  "ods.users": ["id", "country", "status"]
}
```

The loader normalizes three-part table names by dropping the catalog segment, so
both `catalog.db.table` and `db.table` can match.

## Supported Patterns

Currently covered by tests and examples:

- `INSERT` / `INSERT OVERWRITE`
- `MERGE INTO` update and insert branches
- CTE chains
- nested subqueries
- `UNION ALL`
- qualified and unqualified column references
- `SELECT *` / `alias.*`
- CASE expressions
- aggregate functions
- window functions
- selected Spark/Hive generator functions
- Mermaid and JSON output validation

## Limitations

This is a static parser, not a Spark runtime.

- Runtime-only behavior is not modeled.
- Complete `SELECT *` expansion requires schema metadata.
- Unqualified columns can be ambiguous without schema.
- Some dialect-specific SQL may need incremental support.
- MERGE DELETE is treated as a row-level operation and reported as a diagnostic,
  not as output column lineage.

See [docs/limitations.md](docs/limitations.md).

## Project Status

This project is early but usable. The current focus is:

- improving Spark SQL coverage,
- keeping diagnostics explicit,
- expanding synthetic regression tests,
- and making audit output useful for real-world review.

Contributions are welcome, especially minimized SQL cases that reproduce parser
or audit issues without private business data.

## License

Apache-2.0
