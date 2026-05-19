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
include `related_metadata` with separate `input_tables` and `output_tables`.
Input table metadata contains columns that may be used by any scope. The filter
is conservative: columns that are clearly absent from every scope are removed,
while wildcard or unresolved references keep all known columns for that table.
If an input table is missing from schema metadata, the output still includes the
columns inferred from SQL references with `type/comment` set to null and
`metadata_complete=false`.

Output table metadata also uses schema details when the target table is present
in schema metadata. If target schema is missing, it falls back to ROOT output
columns with `type/comment=null` and `metadata_complete=false`.

## Table Metadata

Column schema metadata can be paired with table-level semantic metadata:

```csv
table_name,table_name_cn,table_desc,table_label_layer
ods.users,用户表,用户基础信息表,ODS
mart.user_snapshot,用户快照表,用户快照输出表,ADS
```

Use it from the CLI with:

```bash
scope-lineage parse \
  --sql-file task.sql \
  --schema columns_metadata.csv \
  --table-metadata tables_metadata.csv \
  --out /tmp/scope-output
```

When a table matches, `related_metadata.input_tables` or
`related_metadata.output_tables` includes:

```json
{
  "table_metadata": {
    "table_name_cn": "用户表",
    "table_desc": "用户基础信息表",
    "table_label_layer": "ODS"
  }
}
```

This metadata is especially useful for LLM task profiles because it lets the
model explain a source table semantically instead of only listing its technical
name.
