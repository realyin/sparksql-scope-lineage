"""Schema metadata helpers for expanding physical-table ``SELECT *``.

The parser consumes a lightweight ``{table_name: [column_names...]}`` mapping.
This module keeps loading and normalization in one place so local mock metadata
and the target environment's metadata provider can feed the same contract.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Mapping, Protocol


class SchemaMap(dict):
    """Parser-ready table -> column names map with optional column details."""

    def __init__(
        self,
        *args,
        column_details: Mapping[str, list[dict]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.column_details = {
            normalize_table_name(table): [_normalize_column_detail(item) for item in details]
            for table, details in (column_details or {}).items()
        }


class SchemaProvider(Protocol):
    """Minimal interface for runtime metadata providers."""

    def get_columns(self, table_name: str) -> list[str] | None:
        """Return column names for ``table_name`` or ``None`` when unknown."""


class DictSchemaProvider:
    """Schema provider backed by an in-memory mapping.

    Useful for tests, mock metadata, and adapters that have already fetched all
    needed table schemas from an external catalog.
    """

    def __init__(self, schema: Mapping[str, Iterable[str]] | None = None):
        self.schema = normalize_schema_map(schema or {})

    def get_columns(self, table_name: str) -> list[str] | None:
        return lookup_columns(self.schema, table_name)


def normalize_table_name(name: str) -> str:
    """Normalize table names for metadata lookup.

    Current SQL output usually uses two-part names (``db.table``). When a
    three-part name looks like ``catalog.db.table``, we strip the catalog for
    lookup. We also lower-case because sqlglot normalizes many unquoted
    identifiers to lower-case.
    """

    name = (name or "").strip().strip("`")
    parts = [part.strip("`") for part in name.split(".") if part]
    if len(parts) >= 3:
        parts = parts[-2:]
    return ".".join(parts).lower()


def normalize_schema_map(schema: Mapping[str, Iterable[str]]) -> SchemaMap:
    normalized: SchemaMap = SchemaMap()
    for table, columns in schema.items():
        key = normalize_table_name(table)
        if not key:
            continue
        details = _column_details_from_columns(columns)
        normalized[key] = _dedupe_columns(detail["name"] for detail in details)
        _merge_column_details(normalized, key, details)
    return normalized


def lookup_columns(schema: Mapping[str, Iterable[str]], table_name: str) -> list[str] | None:
    """Lookup columns with both exact and normalized table names."""

    if table_name in schema:
        return list(schema[table_name])
    normalized = normalize_table_name(table_name)
    cols = schema.get(normalized)
    return list(cols) if cols is not None else None


def load_schema(path: str | Path) -> SchemaMap:
    """Load schema metadata from CSV or JSON.

    Supported CSV shape:
      - rows with ``table_name`` and ``column_name``
      - optional ``type`` and ``comment`` columns

    Supported JSON shapes:
      - ``{"db.table": ["c1", "c2"]}``
      - ``{"db.table": [{"name": "c1", "type": "string", "comment": "..."}]}``
      - ``{"db.table": {"column_details": [{"name": "c1"}]}}``
      - ``[{"table_name": "db.table", "column_name": "c1"}]``
      - ``{"tables": [{"table_name": "db.table", "columns": ["c1"]}]}``
    """

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_schema_csv(path)
    if suffix == ".json":
        return load_schema_json(path)
    raise ValueError(f"Unsupported schema metadata file type: {path}")


def load_schema_csv(path: str | Path) -> SchemaMap:
    schema: SchemaMap = SchemaMap()
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            table = row.get("table_name") or row.get("table") or ""
            column = row.get("column_name") or row.get("column") or row.get("name") or ""
            detail = {
                "name": column,
                "type": row.get("type") or row.get("data_type"),
                "comment": row.get("comment") or row.get("column_comment"),
            }
            _append_schema_column(schema, table, detail)
    return schema


def load_schema_json(path: str | Path) -> SchemaMap:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _schema_from_json_value(data)


def materialize_schema(provider: SchemaProvider, tables: Iterable[str]) -> SchemaMap:
    """Fetch a parser-ready schema map from a provider for selected tables."""

    schema: SchemaMap = SchemaMap()
    for table in tables:
        columns = provider.get_columns(table)
        if columns:
            key = normalize_table_name(table)
            details = _column_details_from_columns(columns)
            schema[key] = _dedupe_columns(detail["name"] for detail in details)
            _merge_column_details(schema, key, details)
    return schema


def _schema_from_json_value(data) -> SchemaMap:
    schema: SchemaMap = SchemaMap()

    if isinstance(data, dict) and isinstance(data.get("tables"), list):
        for item in data["tables"]:
            if not isinstance(item, dict):
                continue
            table = item.get("table_name") or item.get("table") or item.get("name") or ""
            columns = item.get("column_details") or item.get("columns") or []
            for column in _iter_column_details(columns):
                _append_schema_column(schema, table, column)
        return schema

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            table = item.get("table_name") or item.get("table") or ""
            column = {
                "name": item.get("column_name") or item.get("column") or item.get("name") or "",
                "type": item.get("type") or item.get("data_type"),
                "comment": item.get("comment") or item.get("column_comment"),
            }
            _append_schema_column(schema, table, column)
        return schema

    if isinstance(data, dict):
        for table, columns in data.items():
            if table == "tables":
                continue
            if isinstance(columns, dict):
                columns = columns.get("column_details") or columns.get("columns") or columns.get("fields") or []
            for column in _iter_column_details(columns):
                _append_schema_column(schema, table, column)
        return schema

    raise ValueError("Unsupported JSON schema metadata shape")


def _iter_column_names(columns) -> Iterable[str]:
    return [detail["name"] for detail in _iter_column_details(columns)]


def _iter_column_details(columns) -> Iterable[dict]:
    if isinstance(columns, dict):
        columns = columns.get("column_details") or columns.get("columns") or columns.get("fields") or []
    if not isinstance(columns, list):
        return []

    details = []
    for column in columns:
        if isinstance(column, str):
            details.append(_normalize_column_detail({"name": column}))
        elif isinstance(column, dict):
            details.append(_normalize_column_detail(column))
    return details


def _append_schema_column(schema: SchemaMap, table: str, column: str | dict) -> None:
    table_key = normalize_table_name(table)
    detail = _normalize_column_detail(column)
    column_name = detail["name"]
    if not table_key or not column_name:
        return
    cols = schema.setdefault(table_key, [])
    if column_name not in cols:
        cols.append(column_name)
    _merge_column_details(schema, table_key, [detail])


def _column_details_from_columns(columns) -> list[dict]:
    return list(_iter_column_details(columns))


def _normalize_column_detail(column: str | Mapping | None) -> dict:
    if isinstance(column, str):
        raw = {"name": column}
    elif isinstance(column, Mapping):
        raw = dict(column)
    else:
        raw = {}

    name = raw.get("column_name") or raw.get("name") or raw.get("column") or ""
    col_type = raw.get("type") or raw.get("data_type")
    comment = raw.get("comment") or raw.get("column_comment")
    return {
        "name": (name or "").strip().strip("`"),
        "type": _blank_to_none(col_type),
        "comment": _blank_to_none(comment),
    }


def _merge_column_details(schema: SchemaMap, table_key: str, details: Iterable[dict]) -> None:
    existing = {item["name"]: item for item in schema.column_details.get(table_key, [])}
    ordered_names = [item["name"] for item in schema.column_details.get(table_key, [])]
    for detail in details:
        name = detail.get("name")
        if not name:
            continue
        if name not in existing:
            ordered_names.append(name)
            existing[name] = {"name": name, "type": None, "comment": None}
        existing[name] = {
            "name": name,
            "type": detail.get("type"),
            "comment": detail.get("comment"),
        }
    schema.column_details[table_key] = [existing[name] for name in ordered_names]


def column_details_for_table(schema: Mapping[str, Iterable[str]], table_name: str) -> list[dict]:
    """Return column metadata details for a table, defaulting type/comment to null."""
    key = normalize_table_name(table_name)
    details_by_table = getattr(schema, "column_details", {})
    details = details_by_table.get(key)
    if details is not None:
        return [dict(item) for item in details]

    cols = lookup_columns(schema, table_name) or []
    return [{"name": col, "type": None, "comment": None} for col in cols]


def _blank_to_none(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _dedupe_columns(columns: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for column in columns:
        name = (column or "").strip().strip("`")
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result
