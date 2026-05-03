# Schema Metadata

`SELECT *` and `alias.*` require table schema metadata for complete field-level
lineage. Without metadata, the parser can preserve a wildcard placeholder and
materialize columns referenced downstream, but it cannot know every output
field.

## CSV Format

```csv
table_name,column_name
ods.users,id
ods.users,country
ods.users,status
```

## JSON Format

```json
{
  "ods.users": ["id", "country", "status"]
}
```

or:

```json
{
  "tables": [
    {
      "table_name": "ods.users",
      "columns": ["id", "country", "status"]
    }
  ]
}
```

## Normalization

The loader currently:

- strips the leading catalog segment from three-part table names
- lower-cases table names
- preserves column order from the metadata file

Column order matters for wildcard expansion.
