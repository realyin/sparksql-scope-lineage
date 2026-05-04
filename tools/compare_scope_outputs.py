"""Compare two generated scope-lineage output directories.

The tool compares generated artifacts, not source SQL. It is useful for
checking whether parser changes preserve output on a private or public corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CORE_FILES = (
    "lineage.json",
    "diagnostics.json",
    "views/scope_overview.mmd",
    "views/field_lineage.mmd",
    "views/physical.mmd",
)


@dataclass
class FileDiff:
    statement: str
    path: str
    kind: str


@dataclass
class CompareResult:
    left: Path
    right: Path
    left_statements: int
    right_statements: int
    compared_files: int
    missing_left: list[str] = field(default_factory=list)
    missing_right: list[str] = field(default_factory=list)
    file_diffs: list[FileDiff] = field(default_factory=list)
    severity_diffs: list[str] = field(default_factory=list)

    @property
    def has_diff(self) -> bool:
        return bool(
            self.missing_left
            or self.missing_right
            or self.file_diffs
            or self.severity_diffs
        )


def _statement_dirs(root: Path) -> dict[str, Path]:
    return {
        path.name: path
        for path in root.iterdir()
        if path.is_dir() and (path / "lineage.json").exists()
    }


def _canonical_bytes(path: Path) -> bytes:
    if path.suffix == ".json":
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    return path.read_bytes()


def _digest(path: Path) -> str:
    return hashlib.sha256(_canonical_bytes(path)).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _severity_from_files(stmt_dir: Path) -> tuple[str | None, Counter[str]]:
    diagnostics = stmt_dir / "diagnostics.json"
    warnings: Counter[str] = Counter()
    if diagnostics.exists():
        data = _load_json(diagnostics)
        warnings.update(w.get("type", "") for w in data.get("warnings") or [])

    field_lineage = stmt_dir / "views" / "field_lineage.mmd"
    if field_lineage.exists() and "UNKNOWN" in field_lineage.read_text(
        encoding="utf-8", errors="ignore"
    ):
        return "RED", warnings
    if warnings:
        return "YELLOW", warnings
    return "GREEN", warnings


def compare_outputs(left: Path, right: Path, files: tuple[str, ...] = CORE_FILES) -> CompareResult:
    if not left.exists():
        raise FileNotFoundError(f"left output directory not found: {left}")
    if not right.exists():
        raise FileNotFoundError(f"right output directory not found: {right}")

    left_dirs = _statement_dirs(left)
    right_dirs = _statement_dirs(right)
    result = CompareResult(
        left=left,
        right=right,
        left_statements=len(left_dirs),
        right_statements=len(right_dirs),
        compared_files=0,
    )

    result.missing_left = sorted(set(right_dirs) - set(left_dirs))
    result.missing_right = sorted(set(left_dirs) - set(right_dirs))

    for statement in sorted(set(left_dirs) & set(right_dirs)):
        left_stmt = left_dirs[statement]
        right_stmt = right_dirs[statement]

        for rel_path in files:
            left_file = left_stmt / rel_path
            right_file = right_stmt / rel_path
            if not left_file.exists() and not right_file.exists():
                continue
            if not left_file.exists():
                result.file_diffs.append(FileDiff(statement, rel_path, "missing_left_file"))
                continue
            if not right_file.exists():
                result.file_diffs.append(FileDiff(statement, rel_path, "missing_right_file"))
                continue
            result.compared_files += 1
            if _digest(left_file) != _digest(right_file):
                result.file_diffs.append(FileDiff(statement, rel_path, "content_diff"))

        left_severity, left_warnings = _severity_from_files(left_stmt)
        right_severity, right_warnings = _severity_from_files(right_stmt)
        if left_severity != right_severity or left_warnings != right_warnings:
            result.severity_diffs.append(statement)

    return result


def render_markdown(result: CompareResult) -> str:
    lines = [
        "# Scope Output Comparison",
        "",
        f"- Left: `{result.left}`",
        f"- Right: `{result.right}`",
        f"- Left statements: {result.left_statements}",
        f"- Right statements: {result.right_statements}",
        f"- Compared files: {result.compared_files}",
        f"- Result: {'DIFF' if result.has_diff else 'MATCH'}",
        "",
        "## Summary",
        "",
        "| Check | Count |",
        "|---|---:|",
        f"| Missing from left | {len(result.missing_left)} |",
        f"| Missing from right | {len(result.missing_right)} |",
        f"| File diffs | {len(result.file_diffs)} |",
        f"| Severity/warning diffs | {len(result.severity_diffs)} |",
    ]

    if result.missing_left:
        lines.extend(["", "## Missing From Left", ""])
        lines.extend(f"- `{name}`" for name in result.missing_left[:100])

    if result.missing_right:
        lines.extend(["", "## Missing From Right", ""])
        lines.extend(f"- `{name}`" for name in result.missing_right[:100])

    if result.file_diffs:
        lines.extend(["", "## File Differences", "", "| Statement | File | Kind |", "|---|---|---|"])
        for diff in result.file_diffs[:200]:
            lines.append(f"| `{diff.statement}` | `{diff.path}` | `{diff.kind}` |")

    if result.severity_diffs:
        lines.extend(["", "## Severity Or Warning Differences", ""])
        lines.extend(f"- `{name}`" for name in result.severity_diffs[:200])

    return "\n".join(lines) + "\n"


def to_jsonable(result: CompareResult) -> dict[str, Any]:
    return {
        "left": str(result.left),
        "right": str(result.right),
        "left_statements": result.left_statements,
        "right_statements": result.right_statements,
        "compared_files": result.compared_files,
        "has_diff": result.has_diff,
        "missing_left": result.missing_left,
        "missing_right": result.missing_right,
        "file_diffs": [
            {"statement": diff.statement, "path": diff.path, "kind": diff.kind}
            for diff in result.file_diffs
        ],
        "severity_diffs": result.severity_diffs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two scope-lineage output directories")
    parser.add_argument("--left", required=True, help="First generated output directory")
    parser.add_argument("--right", required=True, help="Second generated output directory")
    parser.add_argument("--report", help="Write Markdown comparison report")
    parser.add_argument("--json", dest="json_path", help="Write JSON comparison report")
    parser.add_argument("--fail-on-diff", action="store_true", help="Exit 1 when differences exist")
    args = parser.parse_args(argv)

    result = compare_outputs(Path(args.left), Path(args.right))

    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_markdown(result), encoding="utf-8")

    if args.json_path:
        json_path = Path(args.json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "Comparison complete: "
        f"left_statements={result.left_statements}, "
        f"right_statements={result.right_statements}, "
        f"compared_files={result.compared_files}, "
        f"diffs={len(result.file_diffs) + len(result.severity_diffs) + len(result.missing_left) + len(result.missing_right)}"
    )
    if args.report:
        print(f"Markdown report: {args.report}")
    if args.json_path:
        print(f"JSON report: {args.json_path}")

    return 1 if args.fail_on_diff and result.has_diff else 0


if __name__ == "__main__":
    raise SystemExit(main())

