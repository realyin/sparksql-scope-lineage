# sparksql-scope-lineage

[中文](README.zh-CN.md) | English

Scope-aware SQL business semantics and task profiling for Spark SQL and
Hive-style warehouse SQL.

`sparksql-scope-lineage` is not only about answering "where did this column
come from?" Its broader goal is to turn complex SQL into business information
that agents and humans can understand: what business object the task produces,
which upstream tables it reads, which scopes/stages make up the processing
chain, what each stage filters, joins, deduplicates, aggregates, windows, or
derives with CASE logic, which fields and metrics are central, and which parts
of the result are bounded by schema or static-analysis limits.

Column-level lineage remains the foundation. The project parses SQL statically,
keeps CTEs, subqueries, UNION branches, and other intermediate query blocks
visible, expands `SELECT *` with optional schema metadata, and audits generated
output for structural confidence. On top of that, it emits `profile.json`, a
compact LLM/agent-facing artifact for generating task profiles, business-rule
explanations, and handoff-ready Markdown documentation.

It is useful when you need to answer:

- What is this SQL task trying to produce from a business point of view?
- Which scopes or processing stages make up the SQL, and what does each stage do?
- What are the key conditions in each stage, and how do fields participate in
  those rules?
- What are the semantic names and descriptions of the key input tables, output
  tables, fields, and metrics?
- Which physical columns feed each target column?
- Which CTE, subquery, or UNION branch transformed the column?
- Did `SELECT *` expand completely, or is schema metadata missing?
- Are there UNKNOWN sources or broken internal references in the lineage graph?
- When generating agent- or human-readable task documentation, which conclusions
  are backed by complete lineage and which require checking full artifacts?

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

Flattening all column references too early makes these queries hard to debug and
even harder to explain as business logic. This project keeps every query block
as an explicit **scope**, traces lineage through those scopes, and then derives
business-facing stages, rules, important fields, and risk boundaries from the
same evidence.

This gives two layers of output:

- machine-readable lineage for precise field tracing and structural auditing;
- LLM- and human-readable profiles that explain task intent, processing stages,
  rule logic, field semantics, and confidence boundaries.

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
  --html \
  --insight
```

Parse SQL with schema metadata so `SELECT *` can be expanded:

```bash
scope-lineage parse \
  --sql-file examples/select_star_with_schema.sql \
  --schema examples/table_cols.csv \
  --out /tmp/scope-lineage-star-demo \
  --md \
  --html \
  --insight
```

If an output directory already contains `lineage.json` and `profile.json`, you
can render the task-insight workbench without re-parsing SQL:

```bash
scope-lineage insight \
  --input /tmp/scope-lineage-demo/simple_insert
```

Run the tests:

```bash
python -m pytest -q
```

Validate generated task-insight artifacts:

```bash
python tools/validate_task_insight.py --input /tmp/scope-lineage-demo/simple_insert
python tools/validate_task_insight.py --root /tmp/scope-lineage-demo
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
  --table-metadata examples/table_info.csv \
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
task_insight.json
task_insight.html
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
  business summaries, physical source tables, joins, filters, aggregations,
  windows, CASE summaries, key renames, DISTINCT flags, UNION branch counts, and
  lateral-view expansions. Parser-only pass-through scopes are omitted, and
  `profile_step_count` counts only the retained profile steps,
- `summary`, `grain`, `important_columns`, `filters_summary`, and
  `expression_catalog`: lightweight LLM reading aids that summarize the task,
  infer the likely row grain, highlight key/derived/metric-like output columns,
  gather important filters, and de-duplicate notable expression patterns,
- `business_profile` and `business_rule_candidates`: business-facing evidence
  derived from scopes. `business_profile` gives objective clues and per-scope
  sections, while `business_rule_candidates` groups WHERE/HAVING/JOIN
  conditions with referenced fields, column comments, operator hints, and raw
  summaries so an LLM can describe rules such as eligibility, exclusion, or
  classification logic without reading one giant SQL predicate,
- `related_metadata`: `input_tables` and `output_tables` metadata. Entries keep
  schema `type/comment` when available. Input tables fall back to columns
  inferred from scope references when schema is missing, and conservatively keep
  all known columns for wildcard or unresolved references. When table-level
  metadata is provided, each table also includes `table_metadata` with semantic
  details such as Chinese table name, table description, and warehouse layer,
- `end_to_end_lineage`: ROOT columns traced back to physical table columns,
  including each target-facing expression and `trace_complete`;
  `trace_incomplete_reasons` is emitted only when tracing stops at patterns
  such as unexpanded stars,
- `diagnostics`: warnings and parser confidence signals.

To keep the artifact LLM-readable, `profile.json` applies conservative
compaction only to this compact output: long expressions are truncated with
length markers, per-table metadata columns and per-column physical sources are
bounded with count/truncation flags, and diagnostics warnings are summarized
with type counts plus a sample. When table column metadata must be truncated,
columns referenced by business rules, joins, and important lineage fields are
kept first. Full detail remains available in `lineage.json` and
`diagnostics.json`.

`report.html` is a self-contained offline visual report with a scope DAG, ROOT
column table, focused field lineage, and diagnostics. It does not load CDN
assets, fonts, scripts, or local sidecar files, so it can be opened directly in
restricted intranet environments.

`task_insight.json` and `task_insight.html` are the task-insight workbench
artifacts. `task_insight.json` normalizes `lineage.json`, `profile.json`, and
`diagnostics.json` into a stable object/link model for tasks, scopes, columns,
tables, rules, business sections, diagnostics, and evidence. `task_insight.html`
uses that model to provide linked business stages, Scope DAG, rules, field
lineage, metadata, and evidence chains in one offline page.
Use `tools/validate_task_insight.py` to cross-check `lineage.json`,
`profile.json`, `task_insight.json`, and `task_insight.html` for content
accuracy, object links, business/full graph counts, and embedded HTML payload
consistency.

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
  -> derive task-insight objects and links
  -> render JSON / HTML / Mermaid / Markdown / task-insight workbench
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

Optional `type`/`column_type` and `comment`/`column_comment` columns are
preserved in `related_metadata`:

```csv
table_name,column_name,type,comment
ods.users,id,bigint,User ID
ods.users,status,string,Account status
```

Table-level semantics can be provided separately with `--table-metadata`:

```csv
table_name,table_name_cn,table_desc,table_label_layer
ods.users,User table,User base information table,ODS
mart.user_snapshot,User snapshot,User snapshot output table,ADS
```

The generated `profile.json` includes this under each matching table:

```json
{
  "table_metadata": {
    "table_name_cn": "User table",
    "table_desc": "User base information table",
    "table_label_layer": "ODS"
  }
}
```

This warehouse-style shape is also accepted:

```csv
table_name,column_name,column_type,column_comment
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
