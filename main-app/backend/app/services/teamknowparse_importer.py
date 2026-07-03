"""Import TeamKnowParse JSON output into ReTrace KB tables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.teamknowparse_kb import (
    TeamKnowParseKbChunk,
    TeamKnowParseKbFile,
    TeamKnowParseKbFolder,
    TeamKnowParseKbIngestionRun,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_teamknowparse_run(run_dir: Path) -> dict[str, Any]:
    required = ["folder.json", "chunks.jsonl", "run_log.json", "validation_report.json"]
    missing = [name for name in required if not (run_dir / name).exists()]
    if not (run_dir / "files").is_dir():
        missing.append("files/")
    if missing:
        raise ValueError(f"TeamKnowParse output missing: {', '.join(missing)}")

    validation = _load_json(run_dir / "validation_report.json")
    valid = validation.get("valid")
    if valid is None:
        valid = validation.get("summary", {}).get("valid")
    if valid is not True:
        raise ValueError("TeamKnowParse validation_report is not valid")
    return validation


class TeamKnowParseKbImporter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def import_run(
        self,
        team_id: str,
        run_dir: str | Path,
        replace_existing: bool = True,
        embed: bool = True,
        training_job_id: str | None = None,
    ) -> dict[str, Any]:
        path = Path(run_dir)
        validation = validate_teamknowparse_run(path)
        folder_json = _load_json(path / "folder.json")
        run_log = _load_json(path / "run_log.json")
        run_id = str(run_log.get("run_id") or path.name)

        if replace_existing:
            await self.session.execute(delete(TeamKnowParseKbChunk).where(TeamKnowParseKbChunk.team_id == team_id))
            await self.session.execute(delete(TeamKnowParseKbFile).where(TeamKnowParseKbFile.team_id == team_id))
            await self.session.execute(delete(TeamKnowParseKbFolder).where(TeamKnowParseKbFolder.team_id == team_id))

        folder = TeamKnowParseKbFolder(
            team_id=team_id,
            folder_path=str(folder_json.get("folder_path") or folder_json.get("folder_name") or path.name),
            folder_name=folder_json.get("folder_name") or path.name,
            folder_description=folder_json.get("folder_description"),
            source_json=folder_json,
        )
        self.session.add(folder)
        await self.session.flush()

        files_by_path: dict[str, TeamKnowParseKbFile] = {}
        for file_summary in folder_json.get("files") or []:
            file_path = str(file_summary.get("file_path") or file_summary.get("filename") or "")
            if not file_path:
                continue
            file_payload = None
            for candidate in (path / "files").glob("*.json"):
                try:
                    loaded = _load_json(candidate)
                except Exception:
                    continue
                if loaded.get("file_path") == file_path:
                    file_payload = loaded
                    break
            record = TeamKnowParseKbFile(
                team_id=team_id,
                folder_id=folder.folder_id,
                file_path=file_path,
                filename=str(file_summary.get("filename") or Path(file_path).name),
                page_count=file_summary.get("page_count"),
                file_description=file_summary.get("file_description"),
                source_json=file_payload or file_summary,
            )
            self.session.add(record)
            files_by_path[file_path] = record
        await self.session.flush()

        chunk_count = 0
        with (path / "chunks.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue
                file_path = str(payload.get("file_path") or "")
                file_record = files_by_path.get(file_path)
                self.session.add(TeamKnowParseKbChunk(
                    team_id=team_id,
                    file_id=file_record.file_id if file_record else None,
                    source_run_id=run_id,
                    chunk_index=chunk_count,
                    file_path=file_path,
                    filename=payload.get("filename"),
                    page_number=payload.get("page_number"),
                    page_type=payload.get("page_type"),
                    search_text=text,
                    metadata_json=payload,
                    embedding_status="pending" if embed else "skipped",
                ))
                chunk_count += 1

        ingestion = TeamKnowParseKbIngestionRun(
            team_id=team_id,
            training_job_id=training_job_id,
            teamknowparse_run_id=run_id,
            run_dir=str(path),
            status="completed",
            metrics_json=run_log.get("metrics") or {},
            validation_json=validation,
        )
        self.session.add(ingestion)
        await self.session.commit()

        return {
            "teamknowparse_run_id": run_id,
            "kb_ingestion_run_id": ingestion.ingestion_run_id,
            "file_count": len(files_by_path),
            "chunk_count": chunk_count,
        }
