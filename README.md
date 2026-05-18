# sparksql-scope-lineage

[中文](README.zh-CN.md) | English

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
  --md \
  --html
```

Parse SQL with schema metadata so `SELECT *` can be expanded:

```bash
scope-lineage parse \
  --sql-file examples/select_star_with_schema.sql \
  --schema examples/table_cols.csv \
  --out /tmp/scope-lineage-star-demo \
  --md \
  --html
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
  --md \
  --html
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
  --md \
  --html
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
profile.json
diagnostics.json
report.html
lineage.md
views/
  scope_overview.mmd
  field_lineage.mmd
  physical.mmd
  per_column/*.mmd
```

`lineage.json` contains the complete machine-readable lineage result, including
all intermediate `scopes`, `scope_graph`, diagnostics, `scope_profile`, and
end-to-end physical lineage for ROOT columns.

`profile.json` is the compact LLM/task-profile artifact. It omits the full
intermediate `scopes` payload and keeps the pieces that explain the SQL at a
business-logic level:

- `scope_profile`: one processing step per scope, with role, operations,
  physical source tables, joins, filters, aggregations, windows, CASE summaries,
  key renames, DISTINCT flags, UNION branch counts, and lateral-view expansions.
  Parser-only pass-through scopes are omitted, and `profile_step_count` counts
  only the retained profile steps,
- `related_metadata`: `input_tables` and `output_tables` metadata. Entries keep
  schema `type/comment` when available. Input tables fall back to columns
  inferred from scope references when schema is missing, and conservatively keep
  all known columns for wildcard or unresolved references,
- `end_to_end_lineage`: ROOT columns traced back to physical table columns,
  including each target-facing expression and `trace_complete`;
  `trace_incomplete_reasons` is emitted only when tracing stops at patterns
  such as unexpanded stars,
- `diagnostics`: warnings and parser confidence signals.

To keep the artifact LLM-readable, `profile.json` applies conservative
compaction only to this compact output: long expressions are truncated with
length markers, per-table metadata columns and per-column physical sources are
bounded with count/truncation flags, and diagnostics warnings are summarized
with type counts plus a sample. Full detail remains available in `lineage.json`
and `diagnostics.json`.

`report.html` is a self-contained offline visual report with a scope DAG, ROOT
column table, focused field lineage, and diagnostics. It does not load CDN
assets, fonts, scripts, or local sidecar files, so it can be opened directly in
restricted intranet environments.

Mermaid files are intended for visual inspection and debugging.

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
  -> derive compact scope profile and end-to-end physical lineage
  -> render JSON / HTML / Mermaid / Markdown
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

Optional `type` and `comment` columns are preserved in `related_metadata`:

```csv
table_name,column_name,type,comment
ods.users,id,bigint,User ID
ods.users,status,string,Account status
```

or JSON:

```json
{
  "ods.users": ["id", "country", "status"]
}
```

Detailed JSON metadata is also supported:

```json
{
  "ods.users": {
    "column_details": [
      {"name": "id", "type": "bigint", "comment": "User ID"}
    ]
  }
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
