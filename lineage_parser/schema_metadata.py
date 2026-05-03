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


SchemaMap = dict[str, list[str]]


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
    normalized: SchemaMap = {}
    for table, columns in schema.items():
        key = normalize_table_name(table)
        if not key:
            continue
        normalized[key] = _dedupe_columns(columns)
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

    Supported JSON shapes:
      - ``{"db.table": ["c1", "c2"]}``
      - ``{"db.table": [{"name": "c1"}, {"column_name": "c2"}]}``
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
    schema: SchemaMap = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            table = row.get("table_name") or row.get("table") or ""
            column = row.get("column_name") or row.get("column") or row.get("name") or ""
            _append_schema_column(schema, table, column)
    return schema


def load_schema_json(path: str | Path) -> SchemaMap:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _schema_from_json_value(data)


def materialize_schema(provider: SchemaProvider, tables: Iterable[str]) -> SchemaMap:
    """Fetch a parser-ready schema map from a provider for selected tables."""

    schema: SchemaMap = {}
    for table in tables:
        columns = provider.get_columns(table)
        if columns:
            schema[normalize_table_name(table)] = _dedupe_columns(columns)
    return schema


def _schema_from_json_value(data) -> SchemaMap:
    schema: SchemaMap = {}

    if isinstance(data, dict) and isinstance(data.get("tables"), list):
        for item in data["tables"]:
            if not isinstance(item, dict):
                continue
            table = item.get("table_name") or item.get("table") or item.get("name") or ""
            columns = item.get("columns") or []
            for column in _iter_column_names(columns):
                _append_schema_column(schema, table, column)
        return schema

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            table = item.get("table_name") or item.get("table") or ""
            column = item.get("column_name") or item.get("column") or item.get("name") or ""
            _append_schema_column(schema, table, column)
        return schema

    if isinstance(data, dict):
        for table, columns in data.items():
            if table == "tables":
                continue
            for column in _iter_column_names(columns):
                _append_schema_column(schema, table, column)
        return schema

    raise ValueError("Unsupported JSON schema metadata shape")


def _iter_column_names(columns) -> Iterable[str]:
    if isinstance(columns, dict):
        columns = columns.get("columns") or columns.get("fields") or []
    if not isinstance(columns, list):
        return []

    names = []
    for column in columns:
        if isinstance(column, str):
            names.append(column)
        elif isinstance(column, dict):
            names.append(column.get("column_name") or column.get("name") or column.get("column") or "")
    return names


def _append_schema_column(schema: SchemaMap, table: str, column: str) -> None:
    table_key = normalize_table_name(table)
    column = (column or "").strip().strip("`")
    if not table_key or not column:
        return
    cols = schema.setdefault(table_key, [])
    if column not in cols:
        cols.append(column)


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
