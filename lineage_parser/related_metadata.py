"""Conservative upstream column metadata selection for lineage outputs."""

from __future__ import annotations

from typing import Iterable, Mapping

from .schema_metadata import column_details_for_table, normalize_table_name
from .scope_types import (
    CONSTANT_SCOPE_ID,
    SYSTEM_SCOPE_ID,
    ScopeLineageResult,
    SourceRef,
)


def build_related_metadata(
    result: ScopeLineageResult,
    schema: Mapping[str, Iterable[str]] | None,
) -> dict:
    """Return metadata for upstream fields that may be used by the SQL.

    The filter is intentionally conservative: if a table has a wildcard,
    unresolved, or otherwise uncertain reference, all known metadata for that
    table is kept. Only columns that are clearly absent from every scope are
    removed.
    """
    if not schema:
        return {}

    usage = _collect_usage(result)
    if usage.keep_all_source_tables:
        usage.keep_all_tables.update(normalize_table_name(t) for t in result.source_tables)

    related = {}
    for table in result.source_tables:
        details = column_details_for_table(schema, table)
        if not details:
            continue

        table_key = normalize_table_name(table)
        used_columns = usage.used_columns.get(table_key, set())
        if table_key in usage.keep_all_tables:
            selected = details
        else:
            selected = [item for item in details if item["name"] in used_columns]

        if selected:
            related[table] = {"column_details": selected}
    return related


class _Usage:
    def __init__(self) -> None:
        self.used_columns: dict[str, set[str]] = {}
        self.keep_all_tables: set[str] = set()
        self.keep_all_source_tables = False


def _collect_usage(result: ScopeLineageResult) -> _Usage:
    usage = _Usage()
    for scope_data in result.scopes.values():
        for column in scope_data.columns:
            _add_sources(column.sources, usage)
        for join in scope_data.joins:
            _add_sources(join.condition_columns, usage)
        for scope_filter in scope_data.filters:
            _add_sources(scope_filter.columns, usage)
        for scope_filter in scope_data.having:
            _add_sources(scope_filter.columns, usage)
        _add_sources(scope_data.group_by, usage)
        for item in scope_data.order_by:
            _add_source_ref(SourceRef(item.get("scope", ""), item.get("column", "")), usage)
    return usage


def _add_sources(sources: Iterable[SourceRef], usage: _Usage) -> None:
    for source in sources:
        _add_source_ref(source, usage)


def _add_source_ref(source: SourceRef, usage: _Usage) -> None:
    if source.scope in {"", CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}:
        return
    if source.scope == "UNKNOWN":
        usage.keep_all_source_tables = True
        return
    _add_scope_if_physical(source.scope, usage, source.column)


def _add_scope_if_physical(scope_id: str, usage: _Usage, column: str = "*") -> None:
    if not scope_id or ":" in scope_id or scope_id in {"ROOT", "UNKNOWN"}:
        if scope_id == "UNKNOWN":
            usage.keep_all_source_tables = True
        return

    table_key = normalize_table_name(scope_id)
    if not table_key:
        return
    if not column or "*" in column:
        usage.keep_all_tables.add(table_key)
        return
    usage.used_columns.setdefault(table_key, set()).add(column)
