"""Task-dict entry point for field-level lineage parsing.

Input (task dict):
    task_name: str
    sql: str
    source_tables: list of {table_name, short_name}
    target_tables: list of {table_name, short_name}

Output (return dict):
    success: bool
    task_name: str
    result_count: int
    output_dir: str
    error: str  (only present on failure)
"""

from __future__ import annotations

import pathlib

from .scope_builder import parse_all_scope_lineage
from .scope_serializer import write_output


def run_task(task: dict, output_dir: str | pathlib.Path = "/tmp/lineage_output") -> dict:
    """Parse a single task's SQL and write lineage output files."""
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    task_name = task.get("task_name") or "unnamed_task"
    sql = task.get("sql") or ""

    if not sql.strip():
        return {"success": False, "task_name": task_name, "error": "Empty SQL", "result_count": 0}

    try:
        results = parse_all_scope_lineage(sql, task_name)
        success_count = 0
        for result in results:
            has_parse_error = any(
                w.type == "LINEAGE_ERROR" for w in result.diagnostics.warnings
            )
            if has_parse_error:
                continue
            sub_dir = output_dir / result.task_id.replace("#", "_") if len(results) > 1 else output_dir
            write_output(result, sub_dir)
            success_count += 1

        if success_count == 0:
            return {
                "success": False,
                "task_name": task_name,
                "error": "All INSERT statements failed to parse",
                "result_count": 0,
            }
        return {
            "success": True,
            "task_name": task_name,
            "result_count": success_count,
            "output_dir": str(output_dir),
        }
    except Exception as e:
        return {
            "success": False,
            "task_name": task_name,
            "error": f"{type(e).__name__}: {e}",
            "result_count": 0,
        }
