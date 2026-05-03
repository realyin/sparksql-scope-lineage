"""Tests for the task-dict entry point."""

import json
import pathlib
import pytest
from unittest.mock import patch


class TestSkillEntry:
    def test_run_task_returns_output_files(self, tmp_path):
        from lineage_parser.skill_entry import run_task

        task = {
            "task_name": "test_skill_task",
            "sql": "INSERT INTO spark_catalog.dwd.t1 SELECT a.col1, a.col2 FROM ods.src a",
            "source_tables": [{"table_name": "spark_catalog.ods.src", "short_name": "ods.src"}],
            "target_tables": [{"table_name": "spark_catalog.dwd.t1", "short_name": "dwd.t1"}],
        }
        out = run_task(task, output_dir=tmp_path)
        assert (tmp_path / "lineage.json").exists()
        assert (tmp_path / "diagnostics.json").exists()
        assert out["success"] is True
        assert out["task_name"] == "test_skill_task"

    def test_run_task_with_no_sql_returns_error(self, tmp_path):
        from lineage_parser.skill_entry import run_task

        task = {"task_name": "test_empty", "sql": "", "source_tables": [], "target_tables": []}
        out = run_task(task, output_dir=tmp_path)
        assert out["success"] is False
        assert "error" in out

    def test_run_task_with_invalid_sql_returns_error(self, tmp_path):
        from lineage_parser.skill_entry import run_task

        task = {
            "task_name": "test_bad_sql",
            "sql": "THIS IS NOT SQL AT ALL",
            "source_tables": [],
            "target_tables": [],
        }
        out = run_task(task, output_dir=tmp_path)
        assert out["success"] is False
        assert "error" in out

    def test_run_task_multi_insert_returns_result_count(self, tmp_path):
        from lineage_parser.skill_entry import run_task

        task = {
            "task_name": "test_multi",
            "sql": (
                "INSERT INTO spark_catalog.dwd.t1 SELECT a.col1 FROM ods.src a;\n"
                "INSERT INTO spark_catalog.dwd.t2 SELECT a.col2 FROM ods.src a"
            ),
            "source_tables": [],
            "target_tables": [],
        }
        out = run_task(task, output_dir=tmp_path)
        assert out["success"] is True
        assert out["result_count"] == 2
        # Both results should have their own subdirectory with output files
        assert (tmp_path / "test_multi_0" / "lineage.json").exists()
        assert (tmp_path / "test_multi_1" / "lineage.json").exists()

    def test_run_task_partial_success_skips_error_stubs(self, tmp_path):
        """If one INSERT fails (LINEAGE_ERROR stub) but another succeeds,
        run_task should return success=True with result_count=1 and only
        write output for the successful statement."""
        from lineage_parser.skill_entry import run_task
        from lineage_parser.scope_builder import parse_all_scope_lineage
        from lineage_parser.scope_types import ScopeLineageResult, DiagnosticWarning

        two_inserts_sql = (
            "INSERT INTO spark_catalog.dwd.t1 SELECT a.col1 FROM ods.src a;\n"
            "INSERT INTO spark_catalog.dwd.t2 SELECT a.col2 FROM ods.src a"
        )

        # Build the real results, then inject a LINEAGE_ERROR into the first one
        real_results = parse_all_scope_lineage(two_inserts_sql, "test_partial")
        assert len(real_results) == 2

        # Replace first result with an error stub (empty target_table + LINEAGE_ERROR warning)
        error_stub = ScopeLineageResult(
            task_id=real_results[0].task_id,
            target_table="",
            stmt_kind="INSERT",
        )
        error_stub.diagnostics.warnings.append(
            DiagnosticWarning(type="LINEAGE_ERROR", scope="ROOT", msg="simulated parse failure")
        )
        patched_results = [error_stub, real_results[1]]

        task = {
            "task_name": "test_partial",
            "sql": two_inserts_sql,
            "source_tables": [],
            "target_tables": [],
        }

        with patch(
            "lineage_parser.skill_entry.parse_all_scope_lineage",
            return_value=patched_results,
        ):
            out = run_task(task, output_dir=tmp_path)

        assert out["success"] is True
        assert out["result_count"] == 1
        # Only the successful statement's subdirectory should exist
        assert (tmp_path / "test_partial_1" / "lineage.json").exists()
        assert not (tmp_path / "test_partial_0" / "lineage.json").exists()
