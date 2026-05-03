# Contributing

Thanks for considering a contribution.

Scope Lineage is intentionally conservative: lineage output should be
explainable, auditable, and explicit about uncertainty. Parser changes should
come with tests that cover both the parsed structure and the diagnostic behavior
when the SQL is ambiguous.

## Development

```bash
python -m pip install -e "lineage_parser[dev]"
cd lineage_parser
python -m pytest
```

For the public snapshot:

```bash
python scripts/export_public_snapshot.py --out /tmp/scope-lineage-public
cd /tmp/scope-lineage-public
python -m pytest
```

## Pull Request Checklist

- Add or update tests for parser behavior.
- Add diagnostics tests when the behavior is uncertain or lossy.
- Keep examples synthetic and free of private table names, emails, or paths.
- Run the sensitive-string scan described in `OPEN_SOURCE_PLAN.md` before
  publishing a public release.

## SQL Fixtures

Do not add private production SQL to the public repository. If a real failure
requires a regression test, reduce it to a synthetic SQL statement that preserves
the parser shape but removes business names and private identifiers.

