"""Data model for scope-based column lineage.

Each CTE, subquery, UNION branch, and top-level SELECT is a "scope" node in a DAG.
Column sources reference scope_id + column_name (immediate upstream scope)
instead of physical table names.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SourceRef:
    """A reference from a column to an upstream scope + column."""
    scope: str      # scope_id of the upstream scope
    column: str     # column name in the upstream scope


@dataclass
class ScopeColumn:
    """A column within a scope."""
    name: str
    transform: str   # DIRECT|EXPRESSION|AGGREGATE|WINDOW|CONDITIONAL|CONSTANT|UNION|EXPAND_ALL
    transform_subkind: Optional[str] = None
    expression: Optional[str] = None
    sources: List[SourceRef] = field(default_factory=list)
    # Optional fields by transform type:
    case_branches: Optional[List[dict]] = None      # CONDITIONAL
    window: Optional[dict] = None                     # WINDOW
    agg_function: Optional[str] = None                # AGGREGATE
    branches: Optional[List[dict]] = None             # UNION
    merge_branch: Optional[str] = None                # MERGE: "matched"|"not_matched"


@dataclass
class ScopeJoin:
    """A JOIN relationship within a scope."""
    join_type: str
    left_scope: str
    right_scope: str
    alias_in_parent: Optional[str] = None
    condition_expression: Optional[str] = None
    condition_columns: List[SourceRef] = field(default_factory=list)


@dataclass
class ScopeFilter:
    """A WHERE or HAVING filter within a scope."""
    expression: str
    columns: List[SourceRef] = field(default_factory=list)


@dataclass
class ScopeData:
    """All data for a single scope (CTE, subquery, UNION branch, or ROOT)."""
    kind: str          # cte|subquery|union|union_branch|root
    role: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    writes_to: Optional[str] = None
    alias_in_parent: Optional[str] = None
    columns: List[ScopeColumn] = field(default_factory=list)
    joins: List[ScopeJoin] = field(default_factory=list)
    filters: List[ScopeFilter] = field(default_factory=list)
    group_by: List[SourceRef] = field(default_factory=list)
    having: List[ScopeFilter] = field(default_factory=list)
    order_by: List[dict] = field(default_factory=list)
    # Union-specific:
    set_op: Optional[str] = None
    branches: Optional[List[str]] = None
    branch_index: Optional[int] = None


@dataclass
class ScopeGraphEdge:
    """An edge in the scope dependency graph (from upstream to downstream)."""
    from_: str  # "from" is Python keyword; serialized as "from" in JSON
    to: str

    def to_dict(self) -> dict:
        return {"from": self.from_, "to": self.to}


@dataclass
class ScopeGraph:
    """Scope dependency graph: nodes + directed edges."""
    nodes: List[str] = field(default_factory=list)
    edges: List[ScopeGraphEdge] = field(default_factory=list)


@dataclass
class DiagnosticWarning:
    """A diagnostic warning about a SQL pattern or potential issue."""
    type: str
    scope: str
    msg: str


@dataclass
class Diagnostics:
    """Parsing diagnostics: fallback status, warnings, and statistics."""
    fallback_used: bool = False
    warnings: List[DiagnosticWarning] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


@dataclass
class ScopeLineageResult:
    """The complete scope-based lineage result."""
    task_id: str
    target_table: str
    stmt_kind: str   # INSERT_OVERWRITE|INSERT|CTAS|MERGE|UPDATE|DELETE
    source_tables: List[str] = field(default_factory=list)
    scope_graph: ScopeGraph = field(default_factory=ScopeGraph)
    scopes: Dict[str, ScopeData] = field(default_factory=dict)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
