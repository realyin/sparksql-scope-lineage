"""Validate an LLM-generated profile document against profile.json.

The validator is intentionally lightweight. It checks structural coverage and
fact-boundary rules that are easy to regress in prompts:

- L1-L5 sections exist.
- task_name and target_table are mentioned.
- grain keys are described as candidates, not verified primary keys.
- trace_complete=false fields are disclosed.
- schema/star metadata boundaries are disclosed when profile signals them.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


REQUIRED_SECTIONS = ["L1", "L2", "L3", "L4", "L5"]
SCHEMA_BOUNDARY_WARNING_TYPES = {
    "star_not_expanded",
    "unresolved_unqualified_no_schema",
    "column_not_found",
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def validate_profile_doc(profile: dict[str, Any], doc_text: str) -> dict[str, Any]:
    findings: list[dict[str, str]] = []

    _check_sections(doc_text, findings)
    _check_basic_facts(profile, doc_text, findings)
    _check_grain_language(profile, doc_text, findings)
    _check_trace_completeness(profile, doc_text, findings)
    _check_schema_boundaries(profile, doc_text, findings)
    _check_semantic_metadata_usage(profile, doc_text, findings)
    _check_truncation_boundaries(profile, doc_text, findings)

    return {
        "ok": not any(item["severity"] == "error" for item in findings),
        "error_count": sum(1 for item in findings if item["severity"] == "error"),
        "warning_count": sum(1 for item in findings if item["severity"] == "warning"),
        "findings": findings,
    }


def _check_sections(doc_text: str, findings: list[dict[str, str]]) -> None:
    for section in REQUIRED_SECTIONS:
        if not re.search(rf"(^|\n)#+\s*{section}[：:\s]", doc_text):
            findings.append({
                "severity": "error",
                "code": "missing_section",
                "message": f"Missing required section {section}",
            })


def _check_basic_facts(profile: dict[str, Any], doc_text: str, findings: list[dict[str, str]]) -> None:
    for key in ("task_name", "target_table"):
        value = str(profile.get(key) or "")
        if value and value not in doc_text:
            findings.append({
                "severity": "error",
                "code": f"missing_{key}",
                "message": f"Document does not mention {key}: {value}",
            })


def _check_grain_language(profile: dict[str, Any], doc_text: str, findings: list[dict[str, str]]) -> None:
    grain = profile.get("grain") or {}
    keys = grain.get("keys") or []
    if not keys:
        return
    if "候选" not in doc_text or "标识" not in doc_text:
        findings.append({
            "severity": "error",
            "code": "grain_candidate_not_disclosed",
            "message": "grain.keys must be described as candidate output identifiers",
        })
    if re.search(r"(主键是|主键为|primary key is|primary key:)", doc_text, flags=re.IGNORECASE):
        findings.append({
            "severity": "error",
            "code": "grain_called_primary_key",
            "message": "Document appears to call grain.keys a primary key",
        })


def _check_trace_completeness(profile: dict[str, Any], doc_text: str, findings: list[dict[str, str]]) -> None:
    lineage = profile.get("end_to_end_lineage") or []
    incomplete = [item for item in lineage if not item.get("trace_complete", True)]
    if incomplete:
        if "trace_complete=false" not in doc_text and "追溯不完整" not in doc_text:
            findings.append({
                "severity": "error",
                "code": "trace_incomplete_not_disclosed",
                "message": "Document must disclose trace_complete=false fields",
            })
        for item in incomplete[:20]:
            column = str(item.get("column") or "")
            if column and column not in doc_text:
                findings.append({
                    "severity": "error",
                    "code": "missing_incomplete_column",
                    "message": f"trace_complete=false column not mentioned: {column}",
                })
    else:
        if "完整追溯" not in doc_text and "trace_complete=true" not in doc_text:
            findings.append({
                "severity": "warning",
                "code": "trace_complete_summary_missing",
                "message": "All columns are complete, but document does not explicitly say so",
            })


def _check_schema_boundaries(profile: dict[str, Any], doc_text: str, findings: list[dict[str, str]]) -> None:
    warning_types = set((profile.get("diagnostics") or {}).get("warning_types") or {})
    metadata_incomplete = _has_incomplete_metadata(profile)
    trace_reasons = {
        reason
        for item in profile.get("end_to_end_lineage") or []
        for reason in (item.get("trace_incomplete_reasons") or [])
    }
    needs_schema_note = bool(
        warning_types & SCHEMA_BOUNDARY_WARNING_TYPES
        or metadata_incomplete
        or "star_not_expanded" in trace_reasons
    )
    if needs_schema_note and not any(token in doc_text for token in ("schema", "Schema", "元数据", "SELECT *", "星号")):
        findings.append({
            "severity": "error",
            "code": "schema_boundary_not_disclosed",
            "message": "Profile signals schema/star boundary but document does not disclose it",
        })

    if (
        "magic_number" in warning_types
        and re.search(r"magic_number.*(错误|失败|断链)", doc_text)
        and not re.search(r"magic_number.{0,40}(不是|不等同于|不属于).{0,12}(错误|失败|断链)", doc_text)
    ):
        findings.append({
            "severity": "warning",
            "code": "magic_number_overstated",
            "message": "magic_number should be treated as a hard-coded literal hint, not a lineage error",
        })


def _check_truncation_boundaries(profile: dict[str, Any], doc_text: str, findings: list[dict[str, str]]) -> None:
    if not _profile_has_truncation(profile):
        return
    if "lineage.json" not in doc_text and "截断" not in doc_text:
        findings.append({
            "severity": "warning",
            "code": "truncation_not_disclosed",
            "message": "Profile has truncated sections; document should mention lineage.json for full detail",
        })


def _check_semantic_metadata_usage(profile: dict[str, Any], doc_text: str, findings: list[dict[str, str]]) -> None:
    table_names = _semantic_table_names(profile)
    if table_names and not any(name in doc_text for name in table_names[:10]):
        findings.append({
            "severity": "warning",
            "code": "table_semantics_not_used",
            "message": "Profile has table_metadata, but document does not appear to use table Chinese names/descriptions",
        })

    comments = _important_column_comments(profile)
    if comments and not any(comment in doc_text for comment in comments[:20]):
        findings.append({
            "severity": "warning",
            "code": "column_semantics_not_used",
            "message": "Profile has column comments for important columns, but document does not appear to use them",
        })


def _semantic_table_names(profile: dict[str, Any]) -> list[str]:
    names: list[str] = []
    related = profile.get("related_metadata") or {}
    for section in ("input_tables", "output_tables"):
        for metadata in (related.get(section) or {}).values():
            table_metadata = metadata.get("table_metadata") or {}
            for key in ("table_name_cn", "table_desc"):
                value = table_metadata.get(key)
                if isinstance(value, str) and value and value not in names:
                    names.append(value)
    return names


def _important_column_comments(profile: dict[str, Any]) -> list[str]:
    important = {
        item.get("column")
        for item in (profile.get("important_columns") or [])
        if item.get("column")
    }
    if not important:
        return []
    comments: list[str] = []
    related = profile.get("related_metadata") or {}
    for section in ("input_tables", "output_tables"):
        for metadata in (related.get(section) or {}).values():
            for column in metadata.get("column_details") or []:
                comment = column.get("comment")
                if column.get("name") in important and isinstance(comment, str) and comment:
                    if comment not in comments:
                        comments.append(comment)
    return comments


def _has_incomplete_metadata(profile: dict[str, Any]) -> bool:
    related = profile.get("related_metadata") or {}
    for section in ("input_tables", "output_tables"):
        for metadata in (related.get(section) or {}).values():
            if metadata.get("metadata_complete") is False:
                return True
    return False


def _profile_has_truncation(value: Any) -> bool:
    if isinstance(value, dict):
        if any(key.endswith("_truncated") and item is True for key, item in value.items()):
            return True
        return any(_profile_has_truncation(item) for item in value.values())
    if isinstance(value, list):
        return any(_profile_has_truncation(item) for item in value)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an LLM-generated profile document")
    parser.add_argument("--profile", required=True, help="profile.json path")
    parser.add_argument("--doc", required=True, help="Generated Markdown document path")
    parser.add_argument("--json", dest="json_path", help="Write validation JSON")
    args = parser.parse_args(argv)

    profile = _load_json(Path(args.profile))
    doc_text = Path(args.doc).read_text(encoding="utf-8")
    result = validate_profile_doc(profile, doc_text)

    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "Profile doc validation: "
        f"ok={result['ok']}, errors={result['error_count']}, warnings={result['warning_count']}"
    )
    for finding in result["findings"][:20]:
        print(f"- {finding['severity']} {finding['code']}: {finding['message']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
