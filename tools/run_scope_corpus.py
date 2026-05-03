"""Run v2 scope-based parser over task_info corpus directories.

Usage:
    python tools/run_scope_corpus.py [--input-dir examples/tasks] [--out /tmp/scope_out] [--md]

Outputs per-task directory with lineage.json + diagnostics.json (and optionally views/).
"""

import argparse
import json
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
TASK_INFO = ROOT.parent / "task_info"
sys.path.insert(0, str(ROOT))

from lineage_parser import load_schema, parse_all_scope_lineage, write_output, write_views  # noqa: E402


def run_directory(
    input_dir: pathlib.Path,
    output_dir: pathlib.Path,
    write_md: bool,
    schema: dict | None = None,
) -> tuple:
    stats = {"ok": 0, "error": 0, "empty": 0}
    errors = []

    for fx_path in sorted(input_dir.glob("*.json")):
        task_data = json.loads(fx_path.read_text(encoding="utf-8"))
        task_name = task_data.get("task_name") or fx_path.stem
        sql = task_data.get("sql") or ""

        if not sql.strip():
            stats["empty"] += 1
            continue

        try:
            results = parse_all_scope_lineage(sql, task_name, schema=schema)
            for result in results:
                task_out = output_dir / result.task_id.replace("#", "_")
                task_out.mkdir(parents=True, exist_ok=True)
                write_output(result, task_out)
                if write_md:
                    write_views(result, task_out)
            stats["ok"] += 1
        except Exception as e:
            stats["error"] += 1
            errors.append({
                "task": task_name,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            })

    return stats, errors


def main():
    parser = argparse.ArgumentParser(description="Run v2 scope lineage parser over corpus")
    parser.add_argument("--dir", help="Single corpus dir name under ../task_info")
    parser.add_argument(
        "--input-dir",
        help="Explicit task JSON directory. Overrides --dir and built-in corpus directories.",
    )
    parser.add_argument("--out", default="/tmp/scope_v2_output", help="Output root directory")
    parser.add_argument("--md", action="store_true", help="Also generate Markdown + Mermaid views")
    parser.add_argument(
        "--schema",
        help=(
            "Optional schema metadata file for SELECT * expansion. "
            "Supports CSV(table_name,column_name) or JSON mock metadata."
        ),
    )
    args = parser.parse_args()

    out_root = pathlib.Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    schema = load_schema(args.schema) if args.schema else None
    if schema is not None:
        print(f"Loaded schema metadata: {len(schema)} tables")

    if args.input_dir:
        input_dir = pathlib.Path(args.input_dir)
        output_dir = out_root / input_dir.name
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Processing {input_dir}...")
        stats, errors = run_directory(input_dir, output_dir, args.md, schema=schema)
        print(f"  ok={stats['ok']}, error={stats['error']}, empty={stats['empty']}")
        err_log = out_root / "errors.json"
        err_log.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Error log: {err_log}")
        return

    if args.dir:
        dirs = [args.dir]
    else:
        dirs = sorted(p.name for p in TASK_INFO.iterdir() if p.is_dir()) if TASK_INFO.exists() else []
        if not dirs:
            print(f"No corpus directories found under {TASK_INFO}. Use --input-dir to pass one explicitly.")
            return
    total_stats = {"ok": 0, "error": 0, "empty": 0}
    all_errors = []

    for d in dirs:
        input_dir = TASK_INFO / d
        if not input_dir.exists():
            print(f"SKIP (not found): {input_dir}")
            continue
        output_dir = out_root / d
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Processing {d}...")
        stats, errors = run_directory(input_dir, output_dir, args.md, schema=schema)
        for k in total_stats:
            total_stats[k] += stats.get(k, 0)
        all_errors.extend(errors)
        print(f"  ok={stats['ok']}, error={stats['error']}, empty={stats['empty']}")

    print(f"\nTotal: ok={total_stats['ok']}, error={total_stats['error']}, empty={total_stats['empty']}")
    total_parseable = total_stats["ok"] + total_stats["error"]
    if total_parseable > 0:
        success_rate = total_stats["ok"] / total_parseable * 100
        print(f"Success rate (excluding empty): {success_rate:.1f}%")

    err_log = out_root / "errors.json"
    err_log.write_text(json.dumps(all_errors, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Error log: {err_log}")


if __name__ == "__main__":
    main()
