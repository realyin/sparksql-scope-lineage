"""Command line interface for Scope Lineage."""

from __future__ import annotations

import argparse
from pathlib import Path

from .schema_metadata import load_schema
from .scope_builder import parse_all_scope_lineage
from .scope_serializer import write_output
from .scope_views import write_views


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope-lineage")
    subcommands = parser.add_subparsers(dest="command", required=True)

    parse_cmd = subcommands.add_parser("parse", help="Parse one SQL file")
    parse_cmd.add_argument("--sql-file", required=True, help="Path to a SQL file")
    parse_cmd.add_argument("--task-name", help="Task name. Defaults to SQL file stem.")
    parse_cmd.add_argument("--out", required=True, help="Output directory")
    parse_cmd.add_argument("--schema", help="Optional CSV/JSON schema metadata")
    parse_cmd.add_argument("--md", action="store_true", help="Also write Markdown and Mermaid views")

    args = parser.parse_args(argv)

    if args.command == "parse":
        return _parse_file(args)
    parser.error(f"unknown command: {args.command}")
    return 2


def _parse_file(args: argparse.Namespace) -> int:
    sql_path = Path(args.sql_file)
    sql = sql_path.read_text(encoding="utf-8")
    task_name = args.task_name or sql_path.stem
    out_root = Path(args.out)
    schema = load_schema(args.schema) if args.schema else None

    results = parse_all_scope_lineage(sql, task_name=task_name, schema=schema)
    for result in results:
        out_dir = out_root / result.task_id.replace("#", "_")
        out_dir.mkdir(parents=True, exist_ok=True)
        write_output(result, out_dir)
        if args.md:
            write_views(result, out_dir)

    print(f"Parsed {len(results)} statement(s) into {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
