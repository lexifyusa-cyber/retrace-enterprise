from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import FileJson, FolderJson, PageAudit, PageJson


def _safe_stem(path: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path)[:160]


def _valid_item(name: str, loader) -> dict[str, Any]:
    try:
        loader()
        return {"name": name, "valid": True, "errors": []}
    except ValidationError as exc:
        return {
            "name": name,
            "valid": False,
            "errors": [
                {
                    "path": ".".join(str(part) for part in error["loc"]),
                    "message": error["msg"],
                }
                for error in exc.errors()
            ],
        }
    except Exception as exc:
        return {"name": name, "valid": False, "errors": [{"path": "", "message": str(exc)}]}


def element_quality_issues(page: PageJson) -> list[str]:
    issues: list[str] = []
    if page.page_type != "FAILED" and not page.rag_text.strip():
        issues.append("rag_text is empty")
    for index, element in enumerate(page.elements):
        prefix = f"page {page.page_number} element {index} ({element.type})"
        if element.type in {"title", "section_header", "paragraph"} and not (element.text or "").strip():
            issues.append(f"{prefix} has no text")
        if element.type == "list" and not element.items:
            issues.append(f"{prefix} has no list items")
        if element.type == "table":
            if not (element.table_description or "").strip():
                issues.append(f"{prefix} has no table description")
            if not (element.markdown or "").strip():
                issues.append(f"{prefix} has no table markdown")
        if element.type == "key_value_pairs" and not element.pairs:
            issues.append(f"{prefix} has no key/value pairs")
        if element.type == "image":
            if not (element.kind or "").strip():
                issues.append(f"{prefix} has no image kind")
            if not (element.summary or "").strip():
                issues.append(f"{prefix} has no image summary")
    return issues


def build_validation_report(run_dir: Path) -> dict[str, Any]:
    folder_path = run_dir / "folder.json"
    files_dir = run_dir / "files"
    run_log_path = run_dir / "run_log.json"

    schema_results: list[dict[str, Any]] = []
    consistency_errors: list[str] = []
    quality_warnings: list[dict[str, Any]] = []

    folder_json: FolderJson | None = None
    schema_results.append(
        _valid_item(
            "folder.json",
            lambda: FolderJson.model_validate_json(folder_path.read_text(encoding="utf-8")),
        )
    )
    if folder_path.exists():
        try:
            folder_json = FolderJson.model_validate_json(folder_path.read_text(encoding="utf-8"))
            if folder_json.total_file_count != len(folder_json.files):
                consistency_errors.append(
                    f"folder.total_file_count is {folder_json.total_file_count}, but files length is {len(folder_json.files)}"
                )
        except Exception:
            folder_json = None

    expected_file_paths = set()
    if folder_json:
        for file_record in folder_json.files:
            expected_file_paths.add(file_record.file_path)
            expected_json = files_dir / f"{_safe_stem(file_record.file_path)}.json"
            if not expected_json.exists():
                consistency_errors.append(f"missing file JSON for {file_record.file_path}")

    file_jsons: list[FileJson] = []
    for file_path in sorted(files_dir.glob("*.json")):
        schema_results.append(
            _valid_item(
                f"files/{file_path.name}",
                lambda path=file_path: FileJson.model_validate_json(path.read_text(encoding="utf-8")),
            )
        )
        try:
            file_json = FileJson.model_validate_json(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        file_jsons.append(file_json)
        if expected_file_paths and file_json.file_path not in expected_file_paths:
            consistency_errors.append(f"file JSON {file_path.name} is not referenced by folder.json")
        if len(file_json.pages) > file_json.page_count:
            consistency_errors.append(f"{file_json.filename} has more extracted pages than declared page_count")
        for page in file_json.pages:
            issues = element_quality_issues(page)
            if issues:
                quality_warnings.append({
                    "file": file_json.filename,
                    "page_number": page.page_number,
                    "issues": issues,
                })

    if run_log_path.exists():
        try:
            run_log = json.loads(run_log_path.read_text(encoding="utf-8"))
            for file_log in run_log.get("files", []):
                for audit in file_log.get("audits", []):
                    PageAudit.model_validate(audit)
        except Exception as exc:
            consistency_errors.append(f"run_log audit validation failed: {exc}")

    valid = all(item["valid"] for item in schema_results) and not consistency_errors
    return {
        "valid": valid,
        "schema": schema_results,
        "consistency_errors": consistency_errors,
        "quality_warnings": quality_warnings,
        "summary": {
            "schema_valid": all(item["valid"] for item in schema_results),
            "consistency_error_count": len(consistency_errors),
            "quality_warning_count": len(quality_warnings),
            "file_json_count": len(file_jsons),
        },
    }


def write_validation_report(run_dir: Path) -> dict[str, Any]:
    report = build_validation_report(run_dir)
    (run_dir / "validation_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return report
