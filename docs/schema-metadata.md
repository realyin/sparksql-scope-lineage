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

Optional `type` and `comment` columns are preserved in `related_metadata`:

```csv
table_name,column_name,type,comment
ods.users,id,bigint,User ID
ods.users,status,string,Account status
```

## JSON Format

```json
{
  "ods.users": ["id", "country", "status"]
}
```

Column details can be provided either as direct column objects:

```json
{
  "ods.users": [
    {"name": "id", "type": "bigint", "comment": "User ID"},
    {"name": "status", "type": "string", "comment": "Account status"}
  ]
}
```

or under `column_details`:

```json
{
  "ods.users": {
    "column_details": [
      {"name": "id", "type": "bigint", "comment": "User ID"}
    ]
  }
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

## Related Metadata Output

When schema metadata includes column details, `lineage.json` and `profile.json`
include `related_metadata`. It contains upstream column metadata that may be
used by any scope. The filter is conservative: columns that are clearly absent
from every scope are removed, while wildcard or unresolved references keep all
known columns for that table.
