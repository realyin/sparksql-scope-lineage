"""End-to-end physical lineage summaries for ROOT columns."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class _TraceResult:
    sources: list[tuple[str, str, str]] = field(default_factory=list)
    incomplete_reasons: list[str] = field(default_factory=list)


def build_end_to_end_lineage(result: ScopeLineageResult) -> list[dict[str, Any]]:
    """Return ROOT columns with physical source columns traced through scopes."""
    root = result.scopes.get("ROOT")
    if root is None:
        return []

    items = []
    for column in root.columns:
        trace = _lineage_for_column(result, "ROOT", column.name)
        items.append({
            "column": column.name,
            "transform": column.transform,
            "trace_complete": not trace["trace_incomplete_reasons"],
            "trace_incomplete_reasons": trace["trace_incomplete_reasons"],
            "physical_sources": trace["physical_sources"],
        })
    return items


def _lineage_for_column(
    result: ScopeLineageResult,
    scope_id: str,
    column_name: str,
) -> dict[str, Any]:
    found = _trace_column(result, scope_id, column_name, "DIRECT", set())
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    non_physical = {"UNKNOWN", CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}
    for table, column, transform in found.sources:
        if table in non_physical:
            continue
        unique[(table, column, transform)] = {
            "table": table,
            "column": column,
            "transform": transform,
        }
    return {
        "physical_sources": list(unique.values()),
        "trace_incomplete_reasons": _unique_reasons(found.incomplete_reasons),
    }


def _trace_column(
    result: ScopeLineageResult,
    scope_id: str,
    column_name: str,
    incoming_transform: str,
    visited: set[tuple[str, str]],
) -> _TraceResult:
    key = (scope_id, column_name)
    if key in visited:
        return _TraceResult(incomplete_reasons=["cycle_detected"])
    if scope_id not in result.scopes:
        reasons = _terminal_incomplete_reasons(scope_id, column_name)
        return _TraceResult(
            sources=[(scope_id, column_name, incoming_transform)],
            incomplete_reasons=reasons,
        )

    visited = visited | {key}
    scope = result.scopes[scope_id]
    column = _find_column(scope.columns, column_name)
    if column is None:
        return _TraceResult(incomplete_reasons=["missing_scope_column"])

    dominant = _dominant_transform(incoming_transform, column.transform)
    if not column.sources:
        return _TraceResult()

    traced = _TraceResult(incomplete_reasons=_column_incomplete_reasons(column))
    for source in column.sources:
        source_column = _source_column_for_trace(result, source.scope, source.column, column_name)
        source_trace = _trace_column(
            result, source.scope, source_column, dominant, visited
        )
        traced.sources.extend(source_trace.sources)
        traced.incomplete_reasons.extend(source_trace.incomplete_reasons)
    return traced


def _source_column_for_trace(
    result: ScopeLineageResult,
    source_scope: str,
    source_column: str,
    current_column: str,
) -> str:
    if source_column != "*":
        return source_column
    if source_scope in result.scopes:
        return current_column
    return "*"


def _column_incomplete_reasons(column: ScopeColumn) -> list[str]:
    if column.transform == "EXPAND_ALL" or "*" in column.name:
        return ["star_not_expanded"]
    return []


def _terminal_incomplete_reasons(scope_id: str, column_name: str) -> list[str]:
    reasons: list[str] = []
    if scope_id == "UNKNOWN":
        reasons.append("unknown_source")
    if "*" in column_name:
        reasons.append("star_not_expanded")
    return reasons


def _unique_reasons(reasons: list[str]) -> list[str]:
    seen = set()
    unique = []
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        unique.append(reason)
    return unique


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
