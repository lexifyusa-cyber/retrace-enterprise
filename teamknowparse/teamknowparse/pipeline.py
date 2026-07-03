from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .classifier import classify_page
from .cleaning import repeated_header_footer_lines
from .config import Settings
from .description import create_file_description, create_folder_description
from .models import Element, FileJson, FolderFile, FolderJson, PageAudit, PageJson
from .qa import write_validation_report
from .native_extract import extract_native_page, extract_native_pages, rag_text_from_elements
from .vlm import VlmClient

SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".docx", ".pptx", ".xlsx", ".xlsm", ".png", ".jpg", ".jpeg"}


@dataclass
class PageTask:
    file_path: Path
    relative_path: str
    filename: str
    page_number: int
    text: str
    page_type: str
    native_elements: list[Element] | None = None


def _safe_stem(path: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path)[:160]



def _prune_none_for_final(value: object) -> object:
    if isinstance(value, list):
        return [_prune_none_for_final(item) for item in value]
    if isinstance(value, dict):
        keep_form_value_null = "key" in value and "value" in value
        output = {}
        for key, item in value.items():
            if item is None and not (keep_form_value_null and key == "value"):
                continue
            output[key] = _prune_none_for_final(item)
        return output
    return value

def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(data, "model_dump"):
        payload = _prune_none_for_final(data.model_dump(exclude_none=False))
    else:
        payload = data
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def discover_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(
        path for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _image_file_page(path: Path):
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        from .native_extract import NativePage
        return [NativePage(text="", elements=None, page_type="VISUAL")]
    return extract_native_pages(path)


def _is_generic_embedded_image(element: Element) -> bool:
    if element.type != "image":
        return False
    kind = (element.kind or "").strip().lower()
    summary = (element.summary or "").strip().lower()
    return kind == "embedded_image" and summary in {
        "slide contains an embedded image.",
        "document contains an embedded image.",
    }


def _element_has_specific_visual_detail(element: Element) -> bool:
    if element.type == "table":
        return bool((element.table_description or element.markdown or "").strip())
    if element.type != "image":
        return False
    if _is_generic_embedded_image(element):
        return False
    summary = (element.summary or "").strip()
    return len(summary.split()) >= 4


def _source_pdf_for_render(task: PageTask, out_dir: Path) -> Path | None:
    suffix = task.file_path.suffix.lower()
    if suffix == ".pdf":
        return task.file_path
    if suffix not in {".docx", ".pptx"}:
        return None
    libreoffice = shutil.which("libreoffice")
    if not libreoffice:
        return None
    office_dir = out_dir / "office_pdf"
    office_dir.mkdir(parents=True, exist_ok=True)
    expected_pdf = office_dir / f"{task.file_path.stem}.pdf"
    if not expected_pdf.exists():
        subprocess.run(
            [
                libreoffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(office_dir),
                str(task.file_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    if expected_pdf.exists():
        return expected_pdf
    converted = list(office_dir.glob(f"{task.file_path.stem}*.pdf"))
    return converted[0] if converted else None


def _render_page_image(task: PageTask, out_dir: Path, zoom: float) -> Path | None:
    target = out_dir / f"{_safe_stem(task.relative_path)}_page_{task.page_number:03d}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    if task.file_path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return task.file_path
    source_pdf = _source_pdf_for_render(task, out_dir)
    if source_pdf is None:
        return None
    import fitz

    doc = fitz.open(source_pdf)
    try:
        page = doc[task.page_number - 1]
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(target)
        return target
    finally:
        doc.close()


def _image_hash(page: object, xref: int, rect: object) -> str:
    try:
        image = page.parent.extract_image(xref)
        raw = image.get("image") if isinstance(image, dict) else None
        if raw:
            return hashlib.sha256(raw).hexdigest()
    except Exception:
        pass
    coordinates = ":".join(str(round(float(value), 1)) for value in (rect.x0, rect.y0, rect.x1, rect.y1))
    return f"xref:{xref}:{coordinates}"


def _crop_similarity_hash(pix: object) -> str | None:
    try:
        from PIL import Image

        png = pix.tobytes("png")
        image = Image.open(io.BytesIO(png)).convert("L").resize((8, 8))
        pixels = list(image.getdata())
        average = sum(pixels) / len(pixels)
        bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
        return f"ahash:{int(bits, 2):016x}"
    except Exception:
        return None


def _image_rects_for_page(
    page: object,
    min_area_ratio: float,
    max_regions: int,
    header_footer_band_ratio: float,
    zoom: float,
    min_crop_width_px: int,
    min_crop_height_px: int,
    seen_hashes: set[str],
) -> list[object]:
    page_area = float(page.rect.width * page.rect.height) or 1.0
    page_height = float(page.rect.height) or 1.0
    margin_band = max(0.0, min(header_footer_band_ratio, 0.45)) * page_height
    rects = []
    seen_rects: set[tuple[int, float, float, float, float]] = set()
    for image in page.get_images(full=True):
        xref = int(image[0])
        for rect in page.get_image_rects(xref):
            key = (xref, round(float(rect.x0), 1), round(float(rect.y0), 1), round(float(rect.x1), 1), round(float(rect.y1), 1))
            if key in seen_rects:
                continue
            seen_rects.add(key)
            width = max(float(rect.width), 0.0)
            height = max(float(rect.height), 0.0)
            if width * zoom < min_crop_width_px or height * zoom < min_crop_height_px:
                continue
            area_ratio = (width * height) / page_area
            if area_ratio < min_area_ratio:
                continue
            in_header_or_footer = float(rect.y1) <= margin_band or float(rect.y0) >= page_height - margin_band
            if in_header_or_footer:
                continue
            image_hash = _image_hash(page, xref, rect)
            if image_hash in seen_hashes:
                continue
            seen_hashes.add(image_hash)
            rects.append((area_ratio, rect))
    rects.sort(key=lambda item: item[0], reverse=True)
    return [rect for _, rect in rects[:max_regions]]


def _render_mixed_visual_images(
    task: PageTask,
    out_dir: Path,
    zoom: float,
    min_area_ratio: float,
    max_regions: int,
    header_footer_band_ratio: float,
    min_crop_width_px: int,
    min_crop_height_px: int,
    seen_hashes: set[str],
) -> list[Path]:
    if task.file_path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return [task.file_path]
    source_pdf = _source_pdf_for_render(task, out_dir)
    if source_pdf is None:
        return []
    import fitz

    doc = fitz.open(source_pdf)
    try:
        page = doc[task.page_number - 1]
        rects = _image_rects_for_page(
            page,
            min_area_ratio,
            max_regions,
            header_footer_band_ratio,
            zoom,
            min_crop_width_px,
            min_crop_height_px,
            seen_hashes,
        )
        output: list[Path] = []
        matrix = fitz.Matrix(zoom, zoom)
        for index, rect in enumerate(rects, start=1):
            target = out_dir / f"{_safe_stem(task.relative_path)}_page_{task.page_number:03d}_image_{index:02d}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            pix = page.get_pixmap(matrix=matrix, alpha=False, clip=rect)
            crop_hash = _crop_similarity_hash(pix)
            if crop_hash is not None:
                if crop_hash in seen_hashes:
                    continue
                seen_hashes.add(crop_hash)
            pix.save(target)
            output.append(target)
            if len(output) >= max_regions:
                break
        return output
    finally:
        doc.close()


class TeamKnowParsePipeline:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.vlm_client = VlmClient(
            self.settings.vlm_endpoint,
            self.settings.vlm_request_timeout_seconds,
            self.settings.vlm_model,
            temperature=self.settings.vlm_temperature,
            top_p=self.settings.vlm_top_p,
            top_k=self.settings.vlm_top_k,
            max_tokens=self.settings.vlm_max_tokens,
            complex_max_tokens=self.settings.vlm_complex_max_tokens,
            min_pixels=self.settings.vlm_min_pixels,
            max_pixels=self.settings.vlm_max_pixels,
            max_retries=self.settings.max_retries,
            retry_backoff_seconds=self.settings.vlm_retry_backoff_seconds,
        )

    async def run(self, input_path: Path, output_root: Path, mode: str = "mixed", tags: list[str] | None = None) -> Path:
        run_started = time.perf_counter()
        run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        run_dir = output_root / run_id
        (run_dir / "files").mkdir(parents=True, exist_ok=True)
        tags = tags or []
        files = discover_files(input_path)
        folder_files: list[FolderFile] = []
        processed_file_jsons: list[FileJson] = []
        run_log: dict[str, object] = {
            "run_id": run_id,
            "input_path": str(input_path),
            "mode": mode,
            "settings": self.settings.__dict__,
            "files": [],
        }
        if mode == "mixed":
            run_log["vlm_health"] = self.vlm_client.health_check()

        for file_path in files:
            file_json, file_log = await self.process_file(file_path, input_path, run_dir, mode, tags)
            processed_file_jsons.append(file_json)
            file_out = run_dir / "files" / f"{_safe_stem(file_json.file_path)}.json"
            _write_json(file_out, file_json)
            file_description = create_file_description(
                file_json,
                min_chars=self.settings.file_desc_min_page_rag_chars,
                max_pages=self.settings.file_desc_max_useful_pages,
            )
            folder_files.append(
                FolderFile(
                    filename=file_json.filename,
                    file_path=file_json.file_path,
                    page_count=file_json.page_count,
                    tags=file_json.tags,
                    file_description=file_description,
                )
            )
            run_log["files"].append(file_log)

        folder_name = input_path.name if input_path.is_dir() else input_path.parent.name
        folder_path = input_path.name if input_path.is_dir() else input_path.parent.name
        folder_description = create_folder_description(
            folder_name,
            folder_files,
            processed_file_jsons,
            min_chars=self.settings.file_desc_min_page_rag_chars,
            max_pages_per_file=self.settings.file_desc_max_useful_pages,
        )
        folder_json = FolderJson(
            folder_name=folder_name,
            folder_path=folder_path,
            folder_description=folder_description,
            total_file_count=len(folder_files),
            files=folder_files,
        )
        _write_json(run_dir / "folder.json", folder_json)
        self._write_chunks(run_dir, folder_json)
        run_log["confidence"] = self._run_confidence(run_log)
        run_log["metrics"] = self._run_metrics(run_log, run_started)
        _write_json(run_dir / "run_log.json", run_log)
        validation_report = write_validation_report(run_dir)
        run_log["validation"] = validation_report["summary"]
        _write_json(run_dir / "run_log.json", run_log)
        return run_dir

    async def process_file(
        self,
        file_path: Path,
        input_root: Path,
        run_dir: Path,
        mode: str,
        tags: list[str],
    ) -> tuple[FileJson, dict[str, object]]:
        start = time.perf_counter()
        relative_path = str(file_path.relative_to(input_root)) if input_root.is_dir() else file_path.name
        native_pages = _image_file_page(file_path)
        texts = [page.text for page in native_pages]
        line_pages = [text.splitlines() for text in texts]
        repeated_noise = repeated_header_footer_lines(line_pages)
        tasks = [
            PageTask(
                file_path=file_path,
                relative_path=relative_path,
                filename=file_path.name,
                page_number=index + 1,
                text=text,
                page_type=native_pages[index].page_type or classify_page(text, image_area_ratio=1.0 if file_path.suffix.lower() in {".png", ".jpg", ".jpeg"} else 0.0),
                native_elements=native_pages[index].elements,
            )
            for index, text in enumerate(texts)
        ]

        routed_tasks = [(task, self._route(task, mode)) for task in tasks]
        digital_tasks = [task for task, route in routed_tasks if route in {"digital", "both"}]
        vlm_tasks = [task for task, route in routed_tasks if route in {"vlm", "both"}]
        skipped_tasks = [task for task, route in routed_tasks if route == "skip"]

        pages: dict[int, PageJson] = {}
        audits: list[PageAudit] = [self._skipped_result(task) for task in skipped_tasks]
        digital_result, vlm_result = await asyncio.gather(
            self._run_digital_tasks(digital_tasks, repeated_noise, run_dir),
            self._run_vlm_tasks(vlm_tasks, run_dir),
        )
        for page_json, audit in digital_result + vlm_result:
            existing = pages.get(page_json.page_number)
            pages[page_json.page_number] = page_json if existing is None else self._merge_page_json(existing, page_json)
            audits.append(audit)

        ordered_pages = [pages[number] for number in sorted(pages)]
        file_json = FileJson(
            filename=file_path.name,
            file_path=relative_path,
            page_count=len(texts),
            tags=tags,
            pages=ordered_pages,
        )
        confidence_by_page = self._page_confidences(ordered_pages, audits)
        for audit in audits:
            if audit.page_number in confidence_by_page:
                audit.internal["confidence"] = confidence_by_page[audit.page_number]
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        file_confidences = [score for score in confidence_by_page.values() if score is not None]
        return file_json, {
            "filename": file_path.name,
            "file_path": relative_path,
            "page_count": len(texts),
            "elapsed_ms": elapsed_ms,
            "digital_pages": len(digital_tasks),
            "vlm_pages": len(vlm_tasks),
            "skipped_pages": len(skipped_tasks),
            "confidence": round(sum(file_confidences) / len(file_confidences), 3) if file_confidences else None,
            "audits": [audit.model_dump(exclude_none=True) for audit in sorted(audits, key=lambda item: item.page_number)],
        }


    def _merge_page_json(self, first: PageJson, second: PageJson) -> PageJson:
        page_type = "MIXED" if "MIXED" in {first.page_type, second.page_type} else second.page_type
        first_elements = first.elements
        second_has_visual_detail = any(
            element.type in {"image", "table"} and _element_has_specific_visual_detail(element)
            for element in second.elements
        )
        second_is_empty_vlm_result = second.page_type == "FAILED" or not second.elements
        if second_has_visual_detail or second_is_empty_vlm_result:
            first_elements = [element for element in first_elements if not _is_generic_embedded_image(element)]
        elements = first_elements + second.elements
        rag_text = rag_text_from_elements(elements)
        return PageJson(
            page_number=first.page_number,
            page_type=page_type,
            elements=elements,
            rag_text=rag_text,
        )

    def _page_confidences(self, pages: list[PageJson], audits: list[PageAudit]) -> dict[int, float | None]:
        pages_by_number = {page.page_number: page for page in pages}
        output: dict[int, float | None] = {}
        for audit in audits:
            if audit.status == "skipped":
                output[audit.page_number] = None
                continue
            if audit.status == "failed":
                output[audit.page_number] = 0.0
                continue
            page = pages_by_number.get(audit.page_number)
            if page is None:
                output[audit.page_number] = 0.0
                continue
            score = 0.72 if audit.route == "qwen_vlm" else 0.80
            if page.rag_text.strip():
                score += 0.10
            if len(page.rag_text.strip()) >= 120:
                score += 0.05
            if page.elements:
                score += 0.07
            if any(element.type == "title" for element in page.elements):
                score += 0.03
            if audit.warnings:
                score -= min(0.20, 0.05 * len(audit.warnings))
            output[audit.page_number] = round(max(0.0, min(score, 0.98)), 3)
        return output

    def _run_confidence(self, run_log: dict[str, object]) -> float | None:
        scores = []
        for file_log in run_log.get("files", []):
            if isinstance(file_log, dict) and isinstance(file_log.get("confidence"), float):
                scores.append(file_log["confidence"])
        return round(sum(scores) / len(scores), 3) if scores else None

    def _run_metrics(self, run_log: dict[str, object], started: float) -> dict[str, object]:
        files = [file_log for file_log in run_log.get("files", []) if isinstance(file_log, dict)]
        total_pages = sum(int(file_log.get("page_count", 0) or 0) for file_log in files)
        page_statuses: dict[tuple[str, int], set[str]] = {}
        for file_log in files:
            filename = str(file_log.get("filename", ""))
            for audit in file_log.get("audits", []):
                page_number = int(audit.get("page_number", 0) or 0)
                page_statuses.setdefault((filename, page_number), set()).add(str(audit.get("status", "")))
        passed_pages = sum(1 for statuses in page_statuses.values() if "passed" in statuses)
        failed_pages = sum(1 for statuses in page_statuses.values() if "passed" not in statuses and "failed" in statuses)
        skipped_pages = sum(1 for statuses in page_statuses.values() if statuses == {"skipped"})
        missed_pages = max(total_pages - len(page_statuses), 0)
        wall_time_seconds = max(time.perf_counter() - started, 0.001)
        processed_pages = passed_pages + failed_pages
        return {
            "file_count": len(files),
            "total_pages": total_pages,
            "processed_pages": processed_pages,
            "passed_pages": passed_pages,
            "failed_pages": failed_pages,
            "skipped_pages": skipped_pages,
            "missed_pages": missed_pages,
            "wall_time_seconds": round(wall_time_seconds, 3),
            "pages_per_second": round(total_pages / wall_time_seconds, 3) if total_pages else 0.0,
            "processed_pages_per_second": round(processed_pages / wall_time_seconds, 3) if total_pages else 0.0,
            "overall_confidence": run_log.get("confidence"),
        }

    def _route(self, task: PageTask, mode: str) -> str:
        if mode == "txt" and task.page_type in {"SCANNED", "VISUAL"}:
            return "skip"
        if task.page_type in {"SCANNED", "VISUAL"}:
            return "vlm"
        if mode == "mixed" and task.page_type == "MIXED" and self.settings.mixed_vlm_enabled:
            return "both"
        return "digital"

    async def _run_digital_tasks(
        self,
        tasks: list[PageTask],
        repeated_noise: set[str],
        run_dir: Path,
    ) -> list[tuple[PageJson, PageAudit]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[PageTask | None] = asyncio.Queue(maxsize=self.settings.digital_queue_max_size)
        results: list[tuple[PageJson, PageAudit]] = []

        def process(task: PageTask) -> tuple[PageJson, PageAudit]:
            started = time.perf_counter()
            if task.native_elements is not None:
                elements = task.native_elements
                rag_text = rag_text_from_elements(elements)
            else:
                elements, rag_text = extract_native_page(task.text, task.page_type, repeated_noise)
            if not rag_text:
                task_type = "FAILED"
                status = "failed"
            else:
                task_type = task.page_type
                status = "passed"
            page = PageJson(page_number=task.page_number, page_type=task_type, elements=elements, rag_text=rag_text)
            audit = PageAudit(
                filename=task.filename,
                page_number=task.page_number,
                route="digital_extractor",
                page_type=task_type,
                status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return page, audit

        async def worker(executor: ThreadPoolExecutor) -> None:
            while True:
                task = await queue.get()
                if task is None:
                    queue.task_done()
                    break
                result = await loop.run_in_executor(executor, process, task)
                results.append(result)
                queue.task_done()

        with ThreadPoolExecutor(max_workers=self.settings.digital_workers) as executor:
            workers = [asyncio.create_task(worker(executor)) for _ in range(self.settings.digital_workers)]
            for task in tasks:
                await queue.put(task)
            for _ in workers:
                await queue.put(None)
            await queue.join()
            await asyncio.gather(*workers)
        return results

    async def _run_vlm_tasks(self, tasks: list[PageTask], run_dir: Path) -> list[tuple[PageJson, PageAudit]]:
        if not tasks:
            return []
        queue: asyncio.Queue[PageTask | None] = asyncio.Queue(maxsize=self.settings.vlm_queue_max_size)
        results: list[tuple[PageJson, PageAudit]] = []
        inflight_requests = asyncio.Semaphore(self.settings.max_inflight_vlm_batches)
        mixed_seen_hashes: set[str] = set()

        async def worker() -> None:
            while True:
                first = await queue.get()
                if first is None:
                    queue.task_done()
                    break
                batch = [first]
                while len(batch) < self.settings.vlm_batch_size:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is None:
                        await queue.put(None)
                        queue.task_done()
                        break
                    batch.append(item)
                    queue.task_done()
                batch_results = await self._process_vlm_batch(batch, run_dir, inflight_requests, mixed_seen_hashes)
                results.extend(batch_results)
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self.settings.vlm_workers)]
        for task in tasks:
            await queue.put(task)
        for _ in workers:
            await queue.put(None)
        await queue.join()
        await asyncio.gather(*workers)
        return results

    async def _process_vlm_batch(
        self,
        tasks: list[PageTask],
        run_dir: Path,
        inflight_requests: asyncio.Semaphore,
        mixed_seen_hashes: set[str],
    ) -> list[tuple[PageJson, PageAudit]]:
        started = time.perf_counter()
        rendered_dir = run_dir / "rendered_pages"
        valid_pairs: list[tuple[PageTask, Path]] = []
        no_region_tasks: list[PageTask] = []

        for task in tasks:
            if task.page_type == "MIXED":
                paths = _render_mixed_visual_images(
                    task,
                    rendered_dir,
                    self.settings.render_zoom,
                    self.settings.mixed_vlm_min_image_area_ratio,
                    self.settings.mixed_vlm_max_regions_per_page,
                    self.settings.mixed_vlm_header_footer_band_ratio,
                    self.settings.mixed_vlm_min_crop_width_px,
                    self.settings.mixed_vlm_min_crop_height_px,
                    mixed_seen_hashes,
                )
                if not paths:
                    no_region_tasks.append(task)
                    continue
                valid_pairs.extend((task, path) for path in paths)
                continue

            path = _render_page_image(task, rendered_dir, self.settings.render_zoom)
            if path is None:
                no_region_tasks.append(task)
                continue
            valid_pairs.append((task, path))

        if not valid_pairs:
            return [self._failed_vlm_result(task, started, "No renderable image or visual region for VLM route") for task in tasks]

        async def extract_one(task: PageTask, path: Path) -> tuple[list[Element], str, dict[str, object]]:
            async with inflight_requests:
                try:
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, self.vlm_client._extract_one, path, task.page_type)
                except Exception as exc:
                    return [], "", {"error": str(exc), "content": ""}

        extracted = await asyncio.gather(*(extract_one(task, path) for task, path in valid_pairs))

        grouped: dict[int, dict[str, object]] = {}
        for (task, path), (elements, _rag_text, internal) in zip(valid_pairs, extracted, strict=False):
            page_group = grouped.setdefault(task.page_number, {"task": task, "elements": [], "internals": [], "paths": []})
            if task.page_type == "MIXED":
                elements = [element for element in elements if element.type in {"image", "table"}]
            page_group["elements"].extend(elements)
            page_group["internals"].append(internal)
            page_group["paths"].append(str(path))

        output: list[tuple[PageJson, PageAudit]] = []
        for group in grouped.values():
            task = group["task"]
            assert isinstance(task, PageTask)
            elements = group["elements"]
            assert isinstance(elements, list)
            rag_text = rag_text_from_elements(elements)
            status = "passed" if rag_text or elements else "failed"
            page_type = task.page_type if status == "passed" else "FAILED"
            page = PageJson(page_number=task.page_number, page_type=page_type, elements=elements, rag_text=rag_text)
            audit = PageAudit(
                filename=task.filename,
                page_number=task.page_number,
                route="qwen_vlm_regions" if task.page_type == "MIXED" else "qwen_vlm",
                page_type=page_type,
                status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
                internal={
                    "region_count": len(group["paths"]),
                    "image_paths": group["paths"],
                    "responses": group["internals"],
                },
            )
            output.append((page, audit))

        output.extend(
            self._skipped_vlm_region_result(task, started, "No significant visual regions after filtering tiny/header/footer images")
            for task in no_region_tasks
            if task.page_type == "MIXED"
        )
        output.extend(
            self._failed_vlm_result(task, started, "No renderable image for VLM route")
            for task in no_region_tasks
            if task.page_type != "MIXED"
        )
        return output

    def _failed_vlm_result(self, task: PageTask, started: float, warning: str) -> tuple[PageJson, PageAudit]:
        page = PageJson(page_number=task.page_number, page_type="FAILED", elements=[], rag_text="")
        audit = PageAudit(
            filename=task.filename,
            page_number=task.page_number,
            route="qwen_vlm",
            page_type="FAILED",
            status="failed",
            latency_ms=int((time.perf_counter() - started) * 1000),
            warnings=[warning],
        )
        return page, audit

    def _skipped_vlm_region_result(self, task: PageTask, started: float, warning: str) -> tuple[PageJson, PageAudit]:
        page = PageJson(page_number=task.page_number, page_type=task.page_type, elements=[], rag_text="")
        audit = PageAudit(
            filename=task.filename,
            page_number=task.page_number,
            route="qwen_vlm_regions",
            page_type=task.page_type,
            status="skipped",
            latency_ms=int((time.perf_counter() - started) * 1000),
            warnings=[warning],
        )
        return page, audit

    def _skipped_result(self, task: PageTask) -> PageAudit:
        return PageAudit(
            filename=task.filename,
            page_number=task.page_number,
            route="ignored_txt_mode",
            page_type=task.page_type,
            status="skipped",
            latency_ms=0,
            warnings=["Ignored in txt mode because the page has no extractable digital text."],
        )

    def _write_page_outputs(self, run_dir: Path, relative_path: str, page: PageJson, audit: PageAudit) -> None:
        stem = _safe_stem(relative_path)
        _write_json(run_dir / "pages" / f"{stem}_page_{page.page_number:03d}.final.json", page)
        _write_json(run_dir / "pages" / f"{stem}_page_{page.page_number:03d}.audit.json", audit)

    def _write_chunks(self, run_dir: Path, folder_json: FolderJson) -> None:
        chunks_path = run_dir / "chunks.jsonl"
        with chunks_path.open("w", encoding="utf-8") as handle:
            for file_record in folder_json.files:
                file_path = run_dir / "files" / f"{_safe_stem(file_record.file_path)}.json"
                if not file_path.exists():
                    continue
                file_json = FileJson.model_validate_json(file_path.read_text(encoding="utf-8"))
                for page in file_json.pages:
                    elements = [element.model_dump(exclude_none=True) for element in page.elements]
                    image_descriptions = [
                        {
                            "kind": element.get("kind") or "visual",
                            "summary": element.get("summary") or element.get("image_description") or "",
                            "image_description": element.get("image_description") or element.get("summary") or "",
                        }
                        for element in elements
                        if element.get("type") == "image" and (element.get("summary") or element.get("image_description"))
                    ]
                    handle.write(json.dumps({
                        "file_path": file_json.file_path,
                        "filename": file_json.filename,
                        "page_number": page.page_number,
                        "page_type": page.page_type,
                        "text": page.rag_text,
                        "elements": elements,
                        "image_descriptions": image_descriptions,
                    }, ensure_ascii=False) + "\n")

