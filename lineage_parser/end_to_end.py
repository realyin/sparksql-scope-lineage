"""End-to-end physical lineage summaries for ROOT columns."""

from __future__ import annotations

from typing import Any

from .scope_types import CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID, ScopeColumn, ScopeLineageResult


_TRANSFORM_PRIORITY: dict[str, int] = {
    "CONSTANT": 0,
    "DIRECT": 1,
    "EXPAND_ALL": 2,
    "UNION": 3,
    "EXPRESSION": 4,
    "CONDITIONAL": 5,
    "WINDOW": 6,
    "AGGREGATE": 7,
}


def build_end_to_end_lineage(result: ScopeLineageResult) -> list[dict[str, Any]]:
    """Return ROOT columns with physical source columns traced through scopes."""
    root = result.scopes.get("ROOT")
    if root is None:
        return []

    return [
        {
            "column": column.name,
            "transform": column.transform,
            "physical_sources": _physical_sources_for_column(result, "ROOT", column.name),
        }
        for column in root.columns
    ]


def _physical_sources_for_column(
    result: ScopeLineageResult,
    scope_id: str,
    column_name: str,
) -> list[dict[str, str]]:
    found = _trace_column(result, scope_id, column_name, "DIRECT", set())
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    non_physical = {"UNKNOWN", CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}
    for table, column, transform in found:
        if table in non_physical:
            continue
        unique[(table, column, transform)] = {
            "table": table,
            "column": column,
            "transform": transform,
        }
    return list(unique.values())


def _trace_column(
    result: ScopeLineageResult,
    scope_id: str,
    column_name: str,
    incoming_transform: str,
    visited: set[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    key = (scope_id, column_name)
    if key in visited:
        return []
    if scope_id not in result.scopes:
        return [(scope_id, column_name, incoming_transform)]

    visited = visited | {key}
    scope = result.scopes[scope_id]
    column = _find_column(scope.columns, column_name)
    if column is None:
        return []

    dominant = _dominant_transform(incoming_transform, column.transform)
    if not column.sources:
        return []

    traced: list[tuple[str, str, str]] = []
    for source in column.sources:
        source_column = column_name if source.column == "*" else source.column
        traced.extend(
            _trace_column(result, source.scope, source_column, dominant, visited)
        )
    return traced


def _find_column(columns: list[ScopeColumn], column_name: str) -> ScopeColumn | None:
    wildcard = None
    for column in columns:
        if column.name == column_name:
            return column
        if column.name == "*":
            wildcard = column
    return wildcard


def _dominant_transform(left: str, right: str) -> str:
    if _TRANSFORM_PRIORITY.get(left, 0) >= _TRANSFORM_PRIORITY.get(right, 0):
        return left
    return right
