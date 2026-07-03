from __future__ import annotations

import asyncio
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import streamlit as st

from teamknowparse.config import Settings
from teamknowparse.pipeline import TeamKnowParsePipeline


st.set_page_config(page_title="TeamKnowParse", layout="wide")
st.title("TeamKnowParse")


def _read_json(path: Path) -> dict[str, Any] | list[Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_options(output_root: Path) -> list[Path]:
    if not output_root.exists():
        return []
    return sorted((path for path in output_root.iterdir() if path.is_dir()), reverse=True)


def _metrics_from_run(run_dir: Path) -> dict[str, int | float]:
    run_log_path = run_dir / "run_log.json"
    if not run_log_path.exists():
        return {}
    run_log = _read_json(run_log_path)
    files = run_log.get("files", []) if isinstance(run_log, dict) else []
    audits = [audit for file_log in files for audit in file_log.get("audits", [])]
    stored = run_log.get("metrics", {}) if isinstance(run_log, dict) else {}
    total_pages = stored.get("total_pages", sum(file_log.get("page_count", 0) for file_log in files))
    passed_pages = stored.get("passed_pages", sum(1 for audit in audits if audit.get("status") == "passed"))
    failed_pages = stored.get("failed_pages", sum(1 for audit in audits if audit.get("status") == "failed"))
    skipped_pages = stored.get("skipped_pages", sum(1 for audit in audits if audit.get("status") == "skipped"))
    processed_pages = stored.get("processed_pages", passed_pages + failed_pages)
    missed_pages = stored.get("missed_pages", max(total_pages - passed_pages - failed_pages - skipped_pages, 0))
    wall_time_seconds = stored.get("wall_time_seconds", round(sum(file_log.get("elapsed_ms", 0) for file_log in files) / 1000, 3))
    pages_per_second = stored.get("pages_per_second", round(total_pages / wall_time_seconds, 3) if wall_time_seconds else 0.0)
    confidence = stored.get("overall_confidence", run_log.get("confidence") or 0)
    return {
        "files": len(files),
        "total pages": total_pages,
        "processed pages": processed_pages,
        "missed pages": missed_pages,
        "wall time sec": wall_time_seconds,
        "pages/sec": pages_per_second,
        "confidence %": int((confidence or 0) * 100),
        "digital pages": sum(file_log.get("digital_pages", 0) for file_log in files),
        "vlm pages": sum(file_log.get("vlm_pages", 0) for file_log in files),
        "skipped pages": skipped_pages,
        "failed pages": failed_pages,
    }


def render_run_outputs(run_dir: Path) -> None:
    st.subheader("Run output")
    st.caption(str(run_dir))

    metrics = _metrics_from_run(run_dir)
    if metrics:
        columns = st.columns(len(metrics))
        for column, (label, value) in zip(columns, metrics.items(), strict=True):
            column.metric(label.title(), value)

    folder_tab, files_tab, metrics_tab, validation_tab = st.tabs(["Folder JSON", "File JSONs", "Metrics", "Validation"])

    with folder_tab:
        folder_path = run_dir / "folder.json"
        if folder_path.exists():
            st.json(_read_json(folder_path), expanded=2)
            st.download_button(
                "Download folder.json",
                folder_path.read_bytes(),
                file_name="folder.json",
                mime="application/json",
            )
        else:
            st.warning("folder.json was not found for this run.")

    with files_tab:
        file_paths = sorted((run_dir / "files").glob("*.json"))
        if file_paths:
            selected_file = st.selectbox("File JSON", file_paths, format_func=lambda item: item.name)
            st.json(_read_json(selected_file), expanded=1)
            st.download_button(
                "Download selected file JSON",
                selected_file.read_bytes(),
                file_name=selected_file.name,
                mime="application/json",
            )
        else:
            st.info("No file JSONs were created.")

    with metrics_tab:
        run_log_path = run_dir / "run_log.json"
        chunks_path = run_dir / "chunks.jsonl"
        if run_log_path.exists():
            run_log = _read_json(run_log_path)
            run_metrics = run_log.get("metrics", {})
            if run_metrics:
                st.json(run_metrics, expanded=1)
            file_rows = []
            for file_log in run_log.get("files", []):
                audits = file_log.get("audits", [])
                pages = file_log.get("page_count", 0)
                elapsed_sec = (file_log.get("elapsed_ms", 0) or 0) / 1000
                passed = sum(1 for audit in audits if audit.get("status") == "passed")
                failed = sum(1 for audit in audits if audit.get("status") == "failed")
                skipped = sum(1 for audit in audits if audit.get("status") == "skipped")
                file_rows.append({
                    "filename": file_log.get("filename"),
                    "pages": pages,
                    "processed": passed + failed,
                    "missed": max(pages - passed - failed - skipped, 0),
                    "digital": file_log.get("digital_pages", 0),
                    "vlm": file_log.get("vlm_pages", 0),
                    "skipped": skipped,
                    "confidence": file_log.get("confidence"),
                    "passed": passed,
                    "failed": failed,
                    "elapsed_sec": round(elapsed_sec, 3),
                    "pages_per_sec": round(pages / elapsed_sec, 3) if elapsed_sec else 0.0,
                })
            st.dataframe(file_rows, use_container_width=True, hide_index=True)
            with st.expander("Raw run_log.json"):
                st.json(run_log, expanded=1)
            st.download_button(
                "Download run_log.json",
                run_log_path.read_bytes(),
                file_name="run_log.json",
                mime="application/json",
            )
        else:
            st.warning("run_log.json was not found for this run.")

        if chunks_path.exists():
            chunk_count = sum(1 for _ in chunks_path.open(encoding="utf-8"))
            st.metric("Chunks", chunk_count)
            st.download_button(
                "Download chunks.jsonl",
                chunks_path.read_bytes(),
                file_name="chunks.jsonl",
                mime="application/x-ndjson",
            )


    with validation_tab:
        validation_path = run_dir / "validation_report.json"
        if validation_path.exists():
            report = _read_json(validation_path)
            summary = report.get("summary", {}) if isinstance(report, dict) else {}
            cols = st.columns(4)
            cols[0].metric("Schema Valid", "Yes" if summary.get("schema_valid") else "No")
            cols[1].metric("Consistency Errors", summary.get("consistency_error_count", 0))
            cols[2].metric("Quality Warnings", summary.get("quality_warning_count", 0))
            cols[3].metric("File JSONs", summary.get("file_json_count", 0))
            if report.get("consistency_errors"):
                st.error("Consistency errors found")
                st.json(report["consistency_errors"], expanded=2)
            if report.get("quality_warnings"):
                st.warning("Quality warnings found")
                st.json(report["quality_warnings"], expanded=2)
            with st.expander("Full validation_report.json", expanded=not report.get("valid", False)):
                st.json(report, expanded=2)
            st.download_button(
                "Download validation_report.json",
                validation_path.read_bytes(),
                file_name="validation_report.json",
                mime="application/json",
            )
        else:
            st.info("No validation report was created for this run.")

mode = st.radio("Extraction mode", ["txt", "mixed"], horizontal=True)
tags = st.text_input("Tags", placeholder="policy, procedure, safety")
output_root = Path(st.text_input("Output directory", value=str(Path.cwd() / "runs")))

with st.expander("Worker configuration", expanded=False):
    settings = Settings()
    st.write({
        "DIGITAL_WORKERS": settings.digital_workers,
        "VLM_WORKERS": settings.vlm_workers,
        "VLM_BATCH_SIZE": settings.vlm_batch_size,
        "MAX_INFLIGHT_VLM_BATCHES": settings.max_inflight_vlm_batches,
        "VLM_ENDPOINT": settings.vlm_endpoint,
        "VLM_MODEL": settings.vlm_model,
    })

uploaded = st.file_uploader("Upload files or a zip folder", accept_multiple_files=True)

if st.button("Train", type="primary", disabled=not uploaded):
    with tempfile.TemporaryDirectory() as tmp:
        input_dir = Path(tmp) / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        for item in uploaded:
            target = input_dir / item.name
            target.write_bytes(item.getbuffer())
            if target.suffix.lower() == ".zip":
                with zipfile.ZipFile(target) as archive:
                    archive.extractall(input_dir / target.stem)
                target.unlink()
        pipeline = TeamKnowParsePipeline(Settings())
        with st.status("Processing pages...", expanded=True) as status:
            try:
                run_dir = asyncio.run(
                    pipeline.run(
                        input_dir,
                        output_root,
                        mode=mode,
                        tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
                    )
                )
                status.update(label="Complete", state="complete")
                st.session_state["selected_run_dir"] = str(run_dir)
                st.success(f"Run complete: {run_dir}")
            except Exception as exc:
                status.update(label="Failed", state="error")
                st.exception(exc)

runs = _run_options(output_root)
if runs:
    remembered = Path(st.session_state.get("selected_run_dir", runs[0]))
    default_index = runs.index(remembered) if remembered in runs else 0
    selected_run = st.selectbox("View run", runs, index=default_index, format_func=lambda item: item.name)
    render_run_outputs(selected_run)
