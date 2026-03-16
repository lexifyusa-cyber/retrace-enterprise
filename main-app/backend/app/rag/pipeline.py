"""
TrainingPipeline — revamped 4-phase training flow.

  Phase 1: Build tree + extension-based exclusion (no LLM)
  Phase 2: LLM project analysis + keep/exclude filtering
  Phase 3: Text extraction (direct + OCR for mixed files)
  Phase 4: Build compressed + encrypted knowledge base

No embedding happens during training.  Embedding is deferred to
retrieval time (or a separate indexing step if added later).

Every phase reports ultra-detailed progress so the frontend status bar
shows exactly what is happening, which file is being processed, and
how much work remains.
"""

import asyncio
import time
from datetime import datetime
from typing import Optional, Callable, Awaitable
from uuid import uuid4

import structlog
from sqlalchemy import select, update

from app.rag.folder_crawler import FolderCrawlerService
from app.rag.file_filter import FileFilterService
from app.rag.text_extractor import TextExtractorService
from app.rag.kb_store import KnowledgeBaseStore
from app.rag.models import TreeNode, ProjectAnalysis

logger = structlog.get_logger()

ProgressCallback = Optional[Callable[[dict], Awaitable[None]]]

_active_training_tasks: dict[str, "asyncio.Task"] = {}


class TrainingCancelled(Exception):
    pass


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f}{unit}" if unit != "B" else f"{nbytes}{unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f}TB"


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


class TrainingPipeline:
    """End-to-end: folders → LLM-analysed, encrypted knowledge base."""

    def __init__(self, filter_service: FileFilterService):
        self.crawler = FolderCrawlerService()
        self.filter = filter_service
        self.extractor = TextExtractorService()
        self.kb_store = KnowledgeBaseStore()

    async def run(
        self,
        product_id: str,
        folder_paths: list[str],
        product_name: str,
        product_description: str,
        on_progress: ProgressCallback = None,
        force_full: bool = False,
        debug_logging: bool = False,
        max_parallel_files: int = 1,
    ) -> dict:
        """Execute the full 4-phase pipeline. Returns a stats dict."""
        _logs: list[str] = []
        timings: dict[str, float] = {}

        def _log(msg: str):
            ts = datetime.utcnow().strftime("%H:%M:%S")
            _logs.append(f"[{ts}] {msg}")

        async def _report(data: dict):
            if on_progress:
                try:
                    await on_progress({**data, "logs": list(_logs)})
                except Exception:
                    pass

        def _check_cancelled():
            task = asyncio.current_task()
            if task and task.cancelled():
                raise TrainingCancelled("Training was stopped by user")

        # ══════════════════════════════════════════════════════════════════
        # PHASE 1: Build tree + extension-based exclusion
        # ══════════════════════════════════════════════════════════════════
        _log("═══ PHASE 1: Building file tree and excluding binary extensions ═══")
        _log(f"Scanning {len(folder_paths)} root folder(s)…")
        await _report({"phase": "phase1", "message": "Phase 1: Scanning folders…"})
        t0 = time.time()

        trees = self.crawler.crawl(folder_paths)

        timings["phase1"] = time.time() - t0
        c = self.crawler
        _log(f"Phase 1 scan found {c.total_files} files in {c.total_folders} folders")
        _log(f"  Total size: {_human_size(c.total_size_bytes)}")
        _log(f"  Excluded by extension: {c.excluded_count} files (binary/compiled/audio/video)")
        _log(f"  Remaining after Phase 1: {c.kept_count} files")
        _log(f"  Phase 1 completed in {_fmt(timings['phase1'])}")

        await _report({
            "phase": "phase1",
            "message": (
                f"Phase 1 done: {c.total_files} files found, "
                f"{c.excluded_count} excluded, {c.kept_count} remaining"
            ),
            "total_files": c.total_files,
            "total_folders": c.total_folders,
            "phase1_excluded": c.excluded_count,
            "phase1_kept": c.kept_count,
            "total_size": _human_size(c.total_size_bytes),
            "elapsed_phase1": _fmt(timings["phase1"]),
        })

        if c.kept_count == 0:
            _log("❌ No processable files found after Phase 1")
            await _report({"phase": "failed", "message": "No processable files found"})
            raise RuntimeError("No processable files found after extension exclusion")

        _check_cancelled()

        # ══════════════════════════════════════════════════════════════════
        # PHASE 2: LLM project analysis + keep/exclude filtering
        # ══════════════════════════════════════════════════════════════════
        _log("")
        _log("═══ PHASE 2: LLM project analysis and file filtering ═══")
        _log(f"Sending tree to LLM for analysis ({c.kept_count} active files)…")
        await _report({
            "phase": "phase2",
            "message": "Phase 2: Sending tree to LLM for project analysis…",
        })
        t0 = time.time()

        async def _filter_progress(msg: str):
            _log(msg)
            await _report({"phase": "phase2", "message": msg})

        def _on_debug_prompt(
            step_name: str,
            system_content: str,
            user_content: str,
            batch_idx: Optional[int],
            total_batches: Optional[int],
        ):
            label = step_name
            if batch_idx is not None and total_batches is not None and total_batches > 1:
                label = f"{step_name} (batch {batch_idx + 1}/{total_batches})"
            _log("")
            _log(f"═══ [DEBUG] Full prompt sent to model: {label} ═══")
            _log("--- SYSTEM ---")
            _log(system_content)
            _log("--- USER ---")
            _log(user_content)

        project_analysis = await self.filter.analyse_and_filter(
            trees=trees,
            product_name=product_name,
            product_description=product_description,
            on_progress=_filter_progress,
            on_debug_prompt=_on_debug_prompt if debug_logging else None,
        )

        # Folder decisions are already applied inside analyse_and_filter.
        # Build abs→rel map for debug output.
        abs_to_rel = getattr(self.filter, "_abs_to_rel", {})

        if debug_logging:
            _log("")
            _log("═══ [DEBUG] Phase 2a — Folder decisions (LLM) ═══")
            kept_folders = 0
            excluded_folders = 0
            for tree in trees:
                for folder in tree.all_folders():
                    rel = abs_to_rel.get(folder.path, folder.path)
                    if folder.decision == "keep":
                        _log(f"  KEEP    {rel}")
                        kept_folders += 1
                    elif folder.decision == "excluded":
                        _log(f"  EXCLUDE {rel}  {('— ' + folder.phase2_reason) if folder.phase2_reason else ''}")
                        excluded_folders += 1
            _log(f"  → {kept_folders} folders kept, {excluded_folders} excluded")

        timings["phase2"] = time.time() - t0

        # Gather kept files for statistics (only files in kept folders)
        kept_files: list[TreeNode] = []
        all_files: list[TreeNode] = []
        for tree in trees:
            kept_files.extend(tree.kept_files())
            all_files.extend(tree.all_files())

        if debug_logging:
            excluded_folder_paths = {
                folder.path
                for tree in trees for folder in tree.all_folders()
                if folder.decision == "excluded"
            }

            def _in_excluded_folder(fpath: str) -> bool:
                for ep in excluded_folder_paths:
                    if fpath.startswith(ep + "/"):
                        return True
                return False

            _log("")
            _log("═══ [DEBUG] Phase 2b — File decisions (LLM, kept folders only) ═══")
            kept_count = 0
            excluded_count = 0
            for f in all_files:
                if f.phase1_excluded or _in_excluded_folder(f.path):
                    continue
                rel = abs_to_rel.get(f.path, f.path)
                if f.decision == "keep":
                    _log(f"  KEEP    {rel}")
                    kept_count += 1
                else:
                    reason = f"  — {f.phase2_reason}" if f.phase2_reason else ""
                    _log(f"  EXCLUDE {rel}{reason}")
                    excluded_count += 1
            _log(f"  → {kept_count} files kept, {excluded_count} excluded by LLM")

        phase2_excluded = sum(
            1 for f in all_files
            if f.decision == "excluded" and not f.phase1_excluded
        )
        kept_text = sum(1 for f in kept_files if f.content_type == "text")
        kept_image = sum(1 for f in kept_files if f.content_type == "image")
        kept_mixed = sum(1 for f in kept_files if f.content_type == "text_or_image")

        _log(f"Phase 2 LLM analysis complete in {_fmt(timings['phase2'])}")
        _log(f"  Project: {project_analysis.summary[:120]}")
        _log(f"  Technologies: {', '.join(project_analysis.technologies[:10])}")
        _log(f"  Architecture: {project_analysis.architecture_style}")
        _log(f"  Components: {len(project_analysis.key_components)}")
        _log(f"  Files kept: {len(kept_files)} "
             f"(text: {kept_text}, image: {kept_image}, mixed: {kept_mixed})")
        _log(f"  Files excluded by LLM: {phase2_excluded}")

        await _report({
            "phase": "phase2",
            "message": (
                f"Phase 2 done: {len(kept_files)} files kept, "
                f"{phase2_excluded} excluded by LLM"
            ),
            "kept_files": len(kept_files),
            "kept_text": kept_text,
            "kept_image": kept_image,
            "kept_mixed": kept_mixed,
            "phase2_excluded": phase2_excluded,
            "project_summary": project_analysis.summary[:200],
            "elapsed_phase2": _fmt(timings["phase2"]),
        })

        if not kept_files:
            _log("❌ No files kept after Phase 2 LLM analysis")
            await _report({"phase": "failed", "message": "LLM excluded all files"})
            raise RuntimeError("No files survived LLM filtering")

        _check_cancelled()

        # ══════════════════════════════════════════════════════════════════
        # PHASE 3: Text extraction
        # ══════════════════════════════════════════════════════════════════
        _log("")
        _log("═══ PHASE 3: Extracting text from kept files ═══")
        text_files = [f for f in kept_files if f.content_type in ("text", "text_or_image")]
        image_files = [f for f in kept_files if f.content_type == "image"]
        _log(f"Files to extract: {len(text_files)} text/mixed, {len(image_files)} image (skipped)")
        await _report({
            "phase": "phase3",
            "message": f"Phase 3: Extracting text from {len(text_files)} files…",
            "files_to_extract": len(text_files),
            "files_extracted": 0,
            "files_total": len(text_files),
            "image_files_skipped": len(image_files),
        })
        t0 = time.time()
        total_files = len(text_files)
        workers = max(1, min(32, max_parallel_files))
        sem = asyncio.Semaphore(workers)
        phase3_done = 0
        phase3_lock = asyncio.Lock()

        def _ocr_progress(msg):
            _log(f"    {msg}")

        async def extract_one(fnode: TreeNode) -> None:
            nonlocal phase3_done
            short = "/".join(fnode.path.split("/")[-2:])
            size_str = _human_size(fnode.size_bytes)
            async with sem:
                _check_cancelled()
                t_file = time.time()
                await asyncio.to_thread(
                    self.extractor.extract, fnode, _ocr_progress
                )
                elapsed_file = time.time() - t_file
            async with phase3_lock:
                phase3_done += 1
                remaining = total_files - phase3_done
                _log(
                    f"  [{phase3_done}/{total_files}] {short} — "
                    f"{fnode.extraction_chars:,} chars in {_fmt(elapsed_file)}"
                )
                if fnode.extraction_method == "failed":
                    _log(f"    ⚠️ Extraction failed for {short}")
                elif fnode.extraction_method == "ocr":
                    _log(
                        f"    ✓ OCR extracted {fnode.extraction_chars:,} chars "
                        f"in {_fmt(elapsed_file)}"
                    )
                await _report({
                    "phase": "phase3",
                    "message": (
                        f"Phase 3: [{phase3_done}/{total_files}] "
                        f"{short} — {remaining} left"
                    ),
                    "current_file": fnode.path,
                    "files_extracted": phase3_done,
                    "files_total": total_files,
                })

        if total_files > 0:
            await _report({
                "phase": "phase3",
                "message": f"Phase 3: Extracting {total_files} files (up to {workers} in parallel)…",
                "files_extracted": 0,
                "files_total": total_files,
            })
            await asyncio.gather(*[extract_one(fnode) for fnode in text_files])

        timings["phase3"] = time.time() - t0
        stats_e = self.extractor.stats
        _log(f"Phase 3 extraction complete in {_fmt(timings['phase3'])}")
        _log(f"  Direct reads: {stats_e['text_direct']}")
        _log(f"  Mixed → direct: {stats_e['text_or_image_direct']}")
        _log(f"  Mixed → OCR: {stats_e['text_or_image_ocr']}")
        _log(f"  Mixed → failed: {stats_e['text_or_image_failed']}")
        _log(f"  Images skipped: {stats_e['image_skipped']}")
        _log(f"  Errors: {stats_e['errors']}")
        _log(f"  Total extracted: {stats_e['total_chars']:,} characters")

        await _report({
            "phase": "phase3",
            "message": (
                f"Phase 3 done: {stats_e['total_chars']:,} chars extracted "
                f"from {stats_e['text_direct'] + stats_e['text_or_image_direct'] + stats_e['text_or_image_ocr']} files"
            ),
            "files_extracted": len(text_files),
            "files_total": len(text_files),
            "extraction_stats": stats_e,
            "elapsed_phase3": _fmt(timings["phase3"]),
        })

        _check_cancelled()

        # ══════════════════════════════════════════════════════════════════
        # PHASE 4: Build encrypted knowledge base
        # ══════════════════════════════════════════════════════════════════
        _log("")
        _log("═══ PHASE 4: Building encrypted knowledge base ═══")
        t0 = time.time()

        # ── 4a: Collect analysis data for KB ──
        _log("Phase 4a: Collecting LLM analysis data…")
        await _report({"phase": "phase4", "message": "Phase 4a: Collecting analysis data…"})

        folder_analysis: dict[str, dict] = {}
        for tree in trees:
            for folder in tree.all_folders():
                if folder.decision == "keep" and folder.description:
                    folder_analysis[folder.path] = {
                        "purpose": folder.description,
                        "technologies": folder.technologies,
                        "importance": folder.importance,
                    }

        file_analysis: dict[str, dict] = {}
        for f in kept_files:
            if f.description:
                file_analysis[f.path] = {
                    "content_type": f.content_type,
                    "description": f.description,
                    "component": f.component,
                    "role": f.role,
                    "relationships": f.relationships,
                    "importance": f.importance,
                    "technologies": f.technologies,
                }

        _log(f"  Folder analysis entries: {len(folder_analysis)}")
        _log(f"  File analysis entries: {len(file_analysis)}")

        # ── 4b: Collect extracted text for KB ──
        _log("Phase 4b: Collecting extracted text…")
        await _report({"phase": "phase4", "message": "Phase 4b: Collecting extracted text…"})

        file_texts: dict[str, dict] = {}
        for f in text_files:
            if f.extracted_text.strip():
                file_texts[f.path] = {
                    "content_type": f.content_type,
                    "extraction_method": f.extraction_method,
                    "size_bytes": f.size_bytes,
                    "modified_at": f.modified_at,
                    "text": f.extracted_text,
                }

        total_text_chars = sum(len(ft["text"]) for ft in file_texts.values())
        _log(f"  Files with text: {len(file_texts)}")
        _log(f"  Total text: {total_text_chars:,} characters ({_human_size(total_text_chars)})")

        # ── 4c: Build chunk map (for future retrieval, no embedding) ──
        _log("Phase 4c: Building chunk map…")
        await _report({"phase": "phase4", "message": "Phase 4c: Building chunk map…"})

        chunk_map: dict[str, dict] = {}
        chunk_count = 0

        MAX_CHUNK_CHARS = 20_400
        for file_path, fdata in file_texts.items():
            text = fdata["text"]
            offset = 0
            chunk_idx = 0
            while offset < len(text):
                end = min(offset + MAX_CHUNK_CHARS, len(text))
                chunk_text = text[offset:end]
                if not chunk_text.strip():
                    offset = end
                    continue
                cid = str(uuid4())
                chunk_map[cid] = {
                    "type": "content",
                    "file": file_path,
                    "offset": offset,
                    "length": end - offset,
                }
                offset = end
                chunk_idx += 1
                chunk_count += 1

        # File analysis entries in chunk map
        for file_path in file_analysis:
            cid = str(uuid4())
            chunk_map[cid] = {"type": "file_analysis", "file": file_path}
            chunk_count += 1

        # Folder analysis entries in chunk map
        for folder_path in folder_analysis:
            cid = str(uuid4())
            chunk_map[cid] = {"type": "folder_analysis", "folder": folder_path}
            chunk_count += 1

        # Project analysis entry in chunk map
        if project_analysis.summary:
            cid = str(uuid4())
            chunk_map[cid] = {"type": "project_analysis"}
            chunk_count += 1

        _log(f"  Chunk map entries: {chunk_count}")

        # ── 4d: Compress, encrypt, and store KB ──
        _log("Phase 4d: Compressing and encrypting knowledge base…")
        await _report({"phase": "phase4", "message": "Phase 4d: Compressing and encrypting KB…"})

        kb_stats = await self.kb_store.build_and_store(
            product_id=product_id,
            trees=trees,
            project_analysis=project_analysis,
            folder_analysis=folder_analysis,
            file_analysis=file_analysis,
            file_texts=file_texts,
            chunk_map=chunk_map,
        )

        timings["phase4"] = time.time() - t0
        _log(f"Phase 4 complete in {_fmt(timings['phase4'])}")
        _log(f"  KB stored: v{kb_stats['version']}")
        _log(f"  JSON size: {_human_size(kb_stats['json_bytes'])}")
        _log(f"  Compressed: {_human_size(kb_stats['compressed_bytes'])} "
             f"({kb_stats['compression_ratio']}% of original)")
        _log(f"  Encrypted: {_human_size(kb_stats['encrypted_bytes'])}")

        await _report({
            "phase": "phase4",
            "message": (
                f"Phase 4 done: KB v{kb_stats['version']} stored — "
                f"{_human_size(kb_stats['compressed_bytes'])} compressed "
                f"({kb_stats['compression_ratio']}% ratio)"
            ),
            "kb_version": kb_stats["version"],
            "kb_json_bytes": kb_stats["json_bytes"],
            "kb_compressed_bytes": kb_stats["compressed_bytes"],
            "kb_encrypted_bytes": kb_stats["encrypted_bytes"],
            "kb_compression_ratio": kb_stats["compression_ratio"],
            "elapsed_phase4": _fmt(timings["phase4"]),
        })

        # ══════════════════════════════════════════════════════════════════
        # DONE — compile final stats
        # ══════════════════════════════════════════════════════════════════
        total_time = sum(timings.values())
        _log("")
        _log("═══ TRAINING COMPLETE ═══")
        _log(f"Total time: {_fmt(total_time)}")
        _log(f"  Phase 1 (tree + exclude): {_fmt(timings['phase1'])}")
        _log(f"  Phase 2 (LLM analysis):   {_fmt(timings['phase2'])}")
        _log(f"  Phase 3 (text extract):   {_fmt(timings['phase3'])}")
        _log(f"  Phase 4 (encrypt KB):     {_fmt(timings['phase4'])}")

        stats = {
            "total_files": c.total_files,
            "total_folders": c.total_folders,
            "total_size": _human_size(c.total_size_bytes),
            "phase1_excluded": c.excluded_count,
            "phase1_kept": c.kept_count,
            "phase2_excluded": phase2_excluded,
            "files_kept": len(kept_files),
            "kept_text": kept_text,
            "kept_image": kept_image,
            "kept_mixed": kept_mixed,
            "files_extracted": len(file_texts),
            "total_text_chars": total_text_chars,
            "chunk_map_entries": chunk_count,
            "kb_version": kb_stats["version"],
            "kb_json_bytes": kb_stats["json_bytes"],
            "kb_compressed_bytes": kb_stats["compressed_bytes"],
            "kb_compression_ratio": kb_stats["compression_ratio"],
            "extraction_stats": self.extractor.stats,
            "timings": {k: round(v, 1) for k, v in timings.items()},
            "total_time": round(total_time, 1),
            "project_summary": project_analysis.summary[:300],
            "technologies": project_analysis.technologies,
            "logs": list(_logs),
        }

        _log("🎉 Training complete!")
        await _report({"phase": "completed", "message": "Training complete!", **stats})

        logger.info("pipeline_complete", product_id=product_id, stats={
            k: v for k, v in stats.items() if k != "logs"
        })
        return stats


# ---------------------------------------------------------------------------
# Background-task helper (called from the /train endpoint)
# ---------------------------------------------------------------------------

async def run_product_training(
    product_id: str,
    folder_paths: list[str],
    group_ids: list[str],
    product_name: str,
    product_description: str = "",
    force_full: bool = False,
) -> None:
    """Run the full pipeline in a background asyncio.Task."""
    from app.db.database import async_session_maker
    from app.models.folder_group import FolderGroup
    from app.api.settings import get_active_llm_settings

    _active_training_tasks[product_id] = asyncio.current_task()  # type: ignore
    _last_progress: dict = {}

    async def _write_progress(progress_data: dict):
        nonlocal _last_progress
        _last_progress = progress_data
        try:
            async with async_session_maker() as session:
                await session.execute(
                    update(FolderGroup)
                    .where(FolderGroup.group_id.in_(group_ids))
                    .values(metadata_json=progress_data)
                )
                await session.commit()
        except Exception:
            pass

    try:
        await _write_progress({
            "phase": "initializing",
            "message": "Setting up training pipeline…",
        })

        # Load LLM settings
        async with async_session_maker() as session:
            llm = await get_active_llm_settings(session)

        api_key = llm.get("api_key")
        if not api_key:
            raise RuntimeError("No LLM API key configured. Set one in Settings.")

        filter_service = FileFilterService(
            api_key=api_key,
            model=llm["model_name"],
            base_url=llm.get("api_url") or None,
        )

        pipeline = TrainingPipeline(filter_service=filter_service)
        debug_logging = bool(llm.get("debug_logging", False))
        max_parallel_files = max(1, min(32, int(llm.get("max_parallel_files", 1) or 1)))

        stats = await pipeline.run(
            product_id=product_id,
            folder_paths=folder_paths,
            product_name=product_name,
            product_description=product_description or "",
            on_progress=_write_progress,
            force_full=force_full,
            debug_logging=debug_logging,
            max_parallel_files=max_parallel_files,
        )

        # Mark groups as completed
        final_stats = {**stats, "phase": "completed", "message": "Training complete!"}
        async with async_session_maker() as session:
            await session.execute(
                update(FolderGroup)
                .where(FolderGroup.group_id.in_(group_ids))
                .values(
                    training_status="completed",
                    last_trained=datetime.utcnow(),
                    metadata_json=final_stats,
                )
            )
            await session.commit()

            # Auto-generate product description if enabled
            try:
                from app.models.product import Product
                result = await session.execute(
                    select(Product).where(Product.product_id == product_id)
                )
                product = result.scalar_one_or_none()
                auto_gen = getattr(product, "auto_generate_description", True) if product else True
                if product and auto_gen:
                    from app.services.product_description_generator import generate_product_description
                    await generate_product_description(product_id, session)
                    logger.info("product_description_auto_generated", product_id=product_id)
            except Exception as exc:
                logger.warning("product_description_generation_skipped",
                               product_id=product_id, error=str(exc))

        logger.info("product_training_success", product_id=product_id)

    except (asyncio.CancelledError, TrainingCancelled):
        logger.info("product_training_cancelled", product_id=product_id)
        try:
            last_logs = _last_progress.get("logs", []) if _last_progress else []
            cancel_meta = {
                "phase": "cancelled",
                "message": "Training stopped by user",
                "logs": last_logs + [
                    f"[{datetime.utcnow().strftime('%H:%M:%S')}] 🛑 Training stopped by user"
                ],
            }
            async with async_session_maker() as session:
                await session.execute(
                    update(FolderGroup)
                    .where(FolderGroup.group_id.in_(group_ids))
                    .values(training_status="pending", metadata_json=cancel_meta)
                )
                await session.commit()
        except Exception:
            pass

    except Exception as exc:
        logger.error("product_training_failed", product_id=product_id, error=str(exc))
        try:
            last_logs = _last_progress.get("logs", []) if _last_progress else []
            fail_meta = {
                "phase": "failed",
                "message": str(exc),
                "logs": last_logs + [
                    f"[{datetime.utcnow().strftime('%H:%M:%S')}] ❌ Training failed: {str(exc)}"
                ],
            }
            async with async_session_maker() as session:
                await session.execute(
                    update(FolderGroup)
                    .where(FolderGroup.group_id.in_(group_ids))
                    .values(training_status="failed", metadata_json=fail_meta)
                )
                await session.commit()
        except Exception:
            pass

    finally:
        _active_training_tasks.pop(product_id, None)
