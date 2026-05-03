# Release Guide

This project is currently prepared as a sanitized public snapshot.

## Create The Public Repository

```bash
python scripts/export_public_snapshot.py --out /tmp/scope-lineage-public
cd /tmp/scope-lineage-public
git init
git add .
git commit -m "Initial public release"
git branch -M main
```

Then create an empty GitHub repository and push:

```bash
git remote add origin https://github.com/<owner>/scope-lineage.git
git push -u origin main
```

## Tag v0.1.0

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

## Preflight Checks

```bash
python -m pytest -q
scope-lineage parse \
  --sql-file examples/select_star_with_schema.sql \
  --schema examples/table_cols.csv \
  --out /tmp/scope-lineage-release-check \
  --md
rg -n "<private_catalog>|<private_schema>|<private_domain>|<private_path>|<private_email>" .
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir /tmp/scope-lineage-dist
```

The sensitive-string scan should produce no matches.
