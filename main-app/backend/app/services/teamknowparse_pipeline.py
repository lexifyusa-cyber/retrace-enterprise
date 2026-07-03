"""TeamKnowParse automation runner for ReTrace training."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.core.config import settings
from app.db.database import async_session_maker
from app.models.folder_group import FolderGroup
from app.models.training import TrainingJob
from app.services.teamknowparse_artifacts import encrypt_run_artifacts
from app.services.teamknowparse_importer import TeamKnowParseKbImporter, validate_teamknowparse_run
from app.services.teamknowparse_vector import TeamKnowParseVectorService


def _log_line(message: str) -> str:
    return f"[{datetime.utcnow().strftime('%H:%M:%S')}] {message}"


def _safe_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _stage_path(src: Path, dest_root: Path) -> int:
    copied = 0
    if src.is_file():
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as archive:
                for member in archive.infolist():
                    target = (dest_root / member.filename).resolve()
                    if not str(target).startswith(str(dest_root.resolve())):
                        raise ValueError(f"Unsafe zip path: {member.filename}")
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        copied += 1
            return copied
        _safe_copy_file(src, dest_root / src.name)
        return 1
    if src.is_dir():
        for item in src.rglob("*"):
            if item.is_file():
                _safe_copy_file(item, dest_root / item.relative_to(src))
                copied += 1
        return copied
    raise FileNotFoundError(str(src))


class TeamKnowParsePipelineRunner:
    def __init__(self) -> None:
        self.project_dir = Path(settings.TEAMKNOWPARSE_PROJECT_DIR)
        self.uploads_dir = Path(settings.TEAMKNOWPARSE_UPLOADS_DIR)
        self.runs_dir = Path(settings.TEAMKNOWPARSE_RUNS_DIR)

    async def create_job(self, team_id: str, group_ids: list[str], mode: str, input_dir: Path) -> str:
        async with async_session_maker() as session:
            job = TrainingJob(
                group_id=group_ids[0],
                status="queued",
                progress_data={
                    "team_id": team_id,
                    "mode": "teamknowparse",
                    "teamknowparse_mode": mode,
                    "input_dir": str(input_dir),
                    "group_ids": group_ids,
                    "logs": [_log_line("TeamKnowParse job queued")],
                },
                statistics={},
            )
            session.add(job)
            await session.commit()
            return job.job_id

    async def run_background(self, *, team_id: str, group_ids: list[str], source_paths: list[str], mode: str, team_name: str | None = None) -> str:
        job_id = str(uuid4())
        input_dir = self.uploads_dir / "retrace" / team_id / job_id
        job_id = await self.create_job(team_id, group_ids, mode, input_dir)
        asyncio.create_task(self.run_job(job_id=job_id, team_id=team_id, group_ids=group_ids, source_paths=source_paths, mode=mode, input_dir=input_dir))
        return job_id

    async def _update(self, job_id: str, status: str, message: str | None = None, **stats) -> None:
        async with async_session_maker() as session:
            job = await session.get(TrainingJob, job_id)
            if job:
                job.status = status
                data = dict(job.progress_data or {})
                logs = list(data.get("logs") or [])
                if message:
                    logs.append(_log_line(message))
                data.update({"phase": status, "logs": logs})
                job.progress_data = data
                merged = dict(job.statistics or {})
                merged.update(stats)
                job.statistics = merged
                if status in {"running_teamknowparse", "staging_files"} and not job.started_at:
                    job.started_at = datetime.utcnow()
                if status in {"completed", "failed", "cancelled"}:
                    job.completed_at = datetime.utcnow()
            result = await session.execute(select(FolderGroup).where(FolderGroup.group_id.in_((job.progress_data or {}).get("group_ids", [])))) if job else None
            if result:
                for group in result.scalars().all():
                    group.training_status = "training" if status not in {"completed", "failed", "cancelled"} else status
                    meta = dict(group.metadata_json or {})
                    meta.update((job.progress_data or {}) if job else {})
                    meta.update((job.statistics or {}) if job else {})
                    group.metadata_json = meta
                    if status == "completed":
                        group.last_trained = datetime.utcnow()
            await session.commit()

    def _command(self, input_dir: Path, mode: str) -> list[str]:
        if settings.TEAMKNOWPARSE_COMMAND.strip():
            return [part.replace("<upload_dir>", str(input_dir)).replace("<runs_dir>", str(self.runs_dir)).replace("<mode>", mode) for part in settings.TEAMKNOWPARSE_COMMAND.split()]
        project_python = self.project_dir / ".venv" / "bin" / "python"
        python_executable = str(project_python) if project_python.exists() else sys.executable
        return [python_executable, "-m", "teamknowparse.cli", str(input_dir), "--out", str(self.runs_dir), "--mode", mode]

    def _subprocess_env(self) -> dict[str, str]:
        env = {**os.environ, "PYTHONPATH": str(self.project_dir)}
        vlm_endpoint = settings.TEAMKNOWPARSE_VLM_ENDPOINT.strip().rstrip("/")
        vlm_model = settings.TEAMKNOWPARSE_VLM_MODEL.strip()
        if vlm_endpoint:
            env["TEAMKNOWPARSE_VLM_ENDPOINT"] = vlm_endpoint
            env["VLM_ENDPOINT"] = vlm_endpoint
        if vlm_model:
            env["TEAMKNOWPARSE_VLM_MODEL"] = vlm_model
            env["VLM_MODEL"] = vlm_model
        vlm_request_settings = {
            "VLM_WORKERS": str(settings.TEAMKNOWPARSE_VLM_WORKERS),
            "VLM_BATCH_SIZE": str(settings.TEAMKNOWPARSE_VLM_BATCH_SIZE),
            "MAX_INFLIGHT_VLM_BATCHES": str(settings.TEAMKNOWPARSE_MAX_INFLIGHT_VLM_BATCHES),
            "VLM_REQUEST_TIMEOUT_SECONDS": str(settings.TEAMKNOWPARSE_VLM_REQUEST_TIMEOUT_SECONDS),
            "MIXED_VLM_ENABLED": str(settings.TEAMKNOWPARSE_MIXED_VLM_ENABLED).lower(),
            "MIXED_VLM_MIN_IMAGE_AREA_RATIO": str(settings.TEAMKNOWPARSE_MIXED_VLM_MIN_IMAGE_AREA_RATIO),
            "MIXED_VLM_MAX_REGIONS_PER_PAGE": str(settings.TEAMKNOWPARSE_MIXED_VLM_MAX_REGIONS_PER_PAGE),
            "MIXED_VLM_HEADER_FOOTER_BAND_RATIO": str(settings.TEAMKNOWPARSE_MIXED_VLM_HEADER_FOOTER_BAND_RATIO),
            "MIXED_VLM_MIN_CROP_WIDTH_PX": str(settings.TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_WIDTH_PX),
            "MIXED_VLM_MIN_CROP_HEIGHT_PX": str(settings.TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_HEIGHT_PX),
            "VLM_TEMPERATURE": str(settings.TEAMKNOWPARSE_VLM_TEMPERATURE),
            "VLM_TOP_P": str(settings.TEAMKNOWPARSE_VLM_TOP_P),
            "VLM_TOP_K": str(settings.TEAMKNOWPARSE_VLM_TOP_K),
            "VLM_MAX_TOKENS": str(settings.TEAMKNOWPARSE_VLM_MAX_TOKENS),
            "VLM_COMPLEX_MAX_TOKENS": str(settings.TEAMKNOWPARSE_VLM_COMPLEX_MAX_TOKENS),
            "VLM_MIN_PIXELS": str(settings.TEAMKNOWPARSE_VLM_MIN_PIXELS),
            "VLM_MAX_PIXELS": str(settings.TEAMKNOWPARSE_VLM_MAX_PIXELS),
            "RENDER_ZOOM": str(settings.TEAMKNOWPARSE_RENDER_ZOOM),
            "MAX_RETRIES": str(settings.TEAMKNOWPARSE_MAX_RETRIES),
            "VLM_RETRY_BACKOFF_SECONDS": str(settings.TEAMKNOWPARSE_VLM_RETRY_BACKOFF_SECONDS),
        }
        env.update(vlm_request_settings)
        env.update({f"TEAMKNOWPARSE_{key}": value for key, value in vlm_request_settings.items()})
        return env

    async def run_job(self, *, job_id: str, team_id: str, group_ids: list[str], source_paths: list[str], mode: str, input_dir: Path) -> None:
        try:
            await self._update(job_id, "staging_files", "Staging files for TeamKnowParse")
            input_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for raw in source_paths:
                copied += _stage_path(Path(raw), input_dir / Path(raw).name if Path(raw).is_dir() else input_dir)
            if copied == 0:
                raise RuntimeError("No files staged for TeamKnowParse")

            await self._update(job_id, "running_teamknowparse", f"Running TeamKnowParse {mode} mode", staged_files=copied)
            before = {p.name for p in self.runs_dir.glob("*") if p.is_dir()} if self.runs_dir.exists() else set()
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *self._command(input_dir, mode),
                cwd=str(self.project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"TeamKnowParse failed ({proc.returncode}): {(stderr or stdout).decode(errors='replace')[-4000:]}")

            candidates = [p for p in self.runs_dir.glob("*") if p.is_dir() and p.name not in before]
            if candidates:
                run_dir = max(candidates, key=lambda p: p.stat().st_mtime)
            else:
                output_lines = [line.strip() for line in stdout.decode(errors="replace").splitlines() if line.strip()]
                if not output_lines:
                    raise RuntimeError("TeamKnowParse finished without reporting an output run directory")
                reported = Path(output_lines[-1])
                run_dir = reported if reported.is_absolute() else (self.project_dir / reported).resolve()
            validate_teamknowparse_run(run_dir)

            await self._update(job_id, "teamknowparse_completed", "TeamKnowParse output validated", teamknowparse_run_id=run_dir.name)
            await self._update(job_id, "ingesting_kb", "Importing TeamKnowParse JSON into KB tables")
            async with async_session_maker() as session:
                import_stats = await TeamKnowParseKbImporter(session).import_run(team_id=team_id, run_dir=run_dir, replace_existing=True, embed=settings.TEAMKNOWPARSE_VECTOR_ENABLED, training_job_id=job_id)

            embedded = 0
            embedding_failed = 0
            if settings.TEAMKNOWPARSE_VECTOR_ENABLED:
                await self._update(job_id, "embedding_chunks", "Embedding chunks into Chroma")
                async with async_session_maker() as session:
                    vector_stats = await TeamKnowParseVectorService().index_team_chunks(session, team_id)
                    embedded = vector_stats.get("indexed", 0)
                    embedding_failed = vector_stats.get("failed", 0)
            artifact_stats = encrypt_run_artifacts(run_dir)
            if artifact_stats.get("enabled"):
                await self._update(job_id, "securing_artifacts", "Encrypted TeamKnowParse output artifacts", artifact_encrypted_count=artifact_stats.get("encrypted", 0))

            completion_message = "TeamKnowParse KB training completed"
            if embedding_failed:
                completion_message += f" with {embedding_failed} embedding failures; exact search remains available"
            await self._update(
                job_id,
                "completed",
                completion_message,
                **import_stats,
                embedded_count=embedded,
                embedding_failed_count=embedding_failed,
                artifact_encryption=artifact_stats,
                artifact_encrypted_count=artifact_stats.get("encrypted", 0),
            )
        except Exception as exc:
            async with async_session_maker() as session:
                job = await session.get(TrainingJob, job_id)
                if job:
                    job.error_log = str(exc)
                await session.commit()
            await self._update(job_id, "failed", f"TeamKnowParse training failed: {exc}")
