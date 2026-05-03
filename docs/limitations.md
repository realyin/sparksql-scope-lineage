# Limitations

Scope Lineage is a static parser. It is designed to make lineage explainable,
not to perfectly emulate every runtime behavior of Spark or Hive.

## Known Boundaries

- Runtime-only behavior is not modeled.
- `SELECT *` requires schema metadata for full column coverage.
- Unqualified columns can be ambiguous without schema metadata.
- SQL dialect extensions may require incremental support.
- MERGE DELETE is a row-level operation and is represented as a diagnostic, not
  as output column lineage.
- The audit tool reports confidence boundaries; it does not prove semantic
  equivalence with a query engine.

## Recommended Use

Use Scope Lineage as a static analysis and audit layer. For critical workflows,
combine it with:

- table schema metadata,
- representative SQL regression tests,
- sampled manual review for RED/YELLOW findings,
- engine-specific runtime lineage where available.

