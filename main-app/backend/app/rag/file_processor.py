"""
FileProcessorService – converts classified files into text chunks.

Each processing-type has its own handler that understands the file format
and produces semantically meaningful Chunk objects.
"""

import csv
import io
import json
import re
from pathlib import Path
from typing import Optional
from uuid import uuid4

import structlog

from typing import Protocol, runtime_checkable

from app.rag.models import ClassifiedFile, Chunk, ChunkMetadata, ProcessingType


@runtime_checkable
class VisionLLMClient(Protocol):
    """Minimal protocol for a vision-capable LLM client."""
    async def describe_image(self, image_bytes: bytes, prompt: str) -> str: ...

logger = structlog.get_logger()

# Embedding models (e.g. text-embedding-3-small) have ~8191 tokens per input.
# 20400 chars stays under 8191 tokens even at ~2.5 chars/token (dense text).
MAX_CHUNK_CHARS = 20400
OVERLAP_CHARS = 0

# Code path: stitch consecutive symbols until this token budget per chunk.
CODE_CHUNK_MAX_TOKENS = 7000
# When a single symbol exceeds CODE_CHUNK_MAX_TOKENS, we split it by this char size (~7k tokens).
CODE_CHUNK_MAX_CHARS_FOR_SPLIT = 21_000


def _count_tokens(text: str) -> int:
    """Token count for embedding model (cl100k_base). Fallback: chars/3."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 3)


# ── language detection ─────────────────────────────────────────────────────

LANG_MAP: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".go": "go", ".rb": "ruby",
    ".cs": "csharp", ".cpp": "cpp", ".c": "c",
    ".h": "c", ".hpp": "cpp",
    ".rs": "rust", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".php": "php",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".r": "r", ".sql": "sql", ".lua": "lua",
    ".ex": "elixir", ".exs": "elixir",
    ".clj": "clojure", ".hs": "haskell",
    ".dart": "dart",
}


class FileProcessorService:
    """Dispatch file processing to type-specific handlers."""

    def __init__(self, vision_client: Optional[VisionLLMClient] = None):
        self._vision = vision_client

    async def process(self, file: ClassifiedFile) -> list[Chunk]:
        try:
            match file.processing_type:
                case ProcessingType.CODE:
                    chunks = await self._process_code(file)
                case ProcessingType.DOC:
                    chunks = await self._process_doc(file)
                case ProcessingType.TICKET_EXPORT:
                    chunks = await self._process_ticket_export(file)
                case ProcessingType.DIAGRAM_IMAGE:
                    chunks = await self._process_diagram_image(file)
                case ProcessingType.DOC_WITH_DIAGRAMS:
                    chunks = await self._process_doc_with_diagrams(file)
                case _:
                    chunks = await self._process_other(file)

            # Stamp sub_category from classification onto every chunk
            sub_cat = getattr(file, "sub_category", "general") or "general"
            for c in chunks:
                c.metadata.sub_category = sub_cat

            return chunks
        except Exception as exc:
            logger.error("file_process_error", path=file.path, error=str(exc))
            return []

    # ── 3.1  Code ──────────────────────────────────────────────────────────

    async def _process_code(self, file: ClassifiedFile) -> list[Chunk]:
        text = _read_text(file.path)
        if not text.strip():
            return []

        language = LANG_MAP.get(file.ext, "unknown")
        blocks = _split_code_into_symbols(text, language)
        file_header = f"FILE: {file.path}\n"

        def chunk_text_for_symbol(sym_name: str, block_text: str) -> str:
            return file_header + f"SYMBOL: {sym_name}\n\nCode:\n{block_text}"

        chunks: list[Chunk] = []
        batch: list[tuple[str, str, int, int]] = []  # (symbol_name, block_text, start_line, end_line)
        batch_tokens = 0

        def flush_batch() -> None:
            nonlocal batch, batch_tokens
            if not batch:
                return
            if len(batch) == 1:
                sym_name, block_text, start_line, end_line = batch[0]
                formatted = chunk_text_for_symbol(sym_name, block_text)
                chunks.append(
                    Chunk(
                        source_path=file.path,
                        processing_type=ProcessingType.CODE,
                        text=formatted,
                        metadata=ChunkMetadata(
                            language=language,
                            symbol_name=sym_name,
                            start_line=start_line or None,
                            end_line=end_line or None,
                        ),
                    )
                )
            else:
                parts = [
                    f"SYMBOL: {sym_name}\n\nCode:\n{block_text}"
                    for sym_name, block_text, _, _ in batch
                ]
                formatted = file_header + "\n\n".join(parts)
                first_start = batch[0][2]
                last_end = batch[-1][3]
                symbol_names = ", ".join(b[0] for b in batch)
                chunks.append(
                    Chunk(
                        source_path=file.path,
                        processing_type=ProcessingType.CODE,
                        text=formatted,
                        metadata=ChunkMetadata(
                            language=language,
                            symbol_name=symbol_names,
                            start_line=first_start or None,
                            end_line=last_end or None,
                        ),
                    )
                )
            batch = []
            batch_tokens = 0

        for symbol_name, block_text, block_start_line, block_end_line in blocks:
            candidate_text = chunk_text_for_symbol(symbol_name, block_text)
            sym_tokens = _count_tokens(candidate_text)
            if sym_tokens > CODE_CHUNK_MAX_TOKENS:
                flush_batch()
                for piece in _sliding_window(
                    block_text, CODE_CHUNK_MAX_CHARS_FOR_SPLIT, OVERLAP_CHARS
                ):
                    ps, pe = _line_range_for_piece(block_text, piece)
                    start_line = (block_start_line + ps - 1) if ps else 0
                    end_line = (block_start_line + pe - 1) if pe else 0
                    formatted = chunk_text_for_symbol(symbol_name, piece)
                    chunks.append(
                        Chunk(
                            source_path=file.path,
                            processing_type=ProcessingType.CODE,
                            text=formatted,
                            metadata=ChunkMetadata(
                                language=language,
                                symbol_name=symbol_name,
                                start_line=start_line or None,
                                end_line=end_line or None,
                            ),
                        )
                    )
            else:
                if batch_tokens + sym_tokens > CODE_CHUNK_MAX_TOKENS and batch:
                    flush_batch()
                batch.append((symbol_name, block_text, block_start_line, block_end_line))
                batch_tokens += sym_tokens

        flush_batch()
        return chunks

    # ── 3.2  Doc ───────────────────────────────────────────────────────────

    async def _process_doc(self, file: ClassifiedFile) -> list[Chunk]:
        ext = file.ext.lower()
        chunks: list[Chunk] = []

        if ext == ".pdf":
            pages = _extract_pdf_text(file.path)
            for page_num, page_text in pages:
                for piece in _sliding_window(page_text, MAX_CHUNK_CHARS, OVERLAP_CHARS):
                    chunks.append(
                        Chunk(
                            source_path=file.path,
                            processing_type=ProcessingType.DOC,
                            text=f"FILE: {file.path}\nPAGE: {page_num}\n\n{piece}",
                            metadata=ChunkMetadata(page_number=page_num),
                        )
                    )
        elif ext == ".docx":
            text = _extract_docx_text(file.path)
            for piece in _sliding_window(text, MAX_CHUNK_CHARS, OVERLAP_CHARS):
                chunks.append(
                    Chunk(
                        source_path=file.path,
                        processing_type=ProcessingType.DOC,
                        text=f"FILE: {file.path}\n\n{piece}",
                        metadata=ChunkMetadata(),
                    )
                )
        elif ext in (".html", ".htm"):
            text = _extract_html_text(file.path)
            for piece in _sliding_window(text, MAX_CHUNK_CHARS, OVERLAP_CHARS):
                chunks.append(
                    Chunk(
                        source_path=file.path,
                        processing_type=ProcessingType.DOC,
                        text=f"FILE: {file.path}\n\n{piece}",
                        metadata=ChunkMetadata(),
                    )
                )
        elif ext in (".md", ".mdx"):
            full_text = _read_text(file.path)
            sections = _split_markdown_by_headings(file.path)
            for heading, body in sections:
                for piece in _sliding_window(body, MAX_CHUNK_CHARS, OVERLAP_CHARS):
                    sl, el = _line_range_for_piece(full_text, piece) if full_text else (0, 0)
                    chunks.append(
                        Chunk(
                            source_path=file.path,
                            processing_type=ProcessingType.DOC,
                            text=f"FILE: {file.path}\nSECTION: {heading}\n\n{piece}",
                            metadata=ChunkMetadata(
                                start_line=sl or None,
                                end_line=el or None,
                            ),
                        )
                    )
        else:
            # .txt, .rst, .adoc, .tex, etc. – plain-text sliding window
            text = _read_text(file.path)
            for piece in _sliding_window(text, MAX_CHUNK_CHARS, OVERLAP_CHARS):
                sl, el = _line_range_for_piece(text, piece)
                chunks.append(
                    Chunk(
                        source_path=file.path,
                        processing_type=ProcessingType.DOC,
                        text=f"FILE: {file.path}\n\n{piece}",
                        metadata=ChunkMetadata(
                            start_line=sl or None,
                            end_line=el or None,
                        ),
                    )
                )
        return chunks

    # ── 3.3  Ticket export ─────────────────────────────────────────────────

    async def _process_ticket_export(self, file: ClassifiedFile) -> list[Chunk]:
        ext = file.ext.lower()
        rows: list[dict] = []

        if ext == ".csv":
            rows = _read_csv(file.path)
        elif ext == ".json":
            rows = _read_json_rows(file.path)
        elif ext == ".xlsx":
            rows = _read_xlsx(file.path)

        chunks: list[Chunk] = []
        for row in rows:
            ticket_id = (
                row.get("ticket_id")
                or row.get("id")
                or row.get("key")
                or row.get("number")
                or str(uuid4())[:8]
            )
            component = row.get("component") or row.get("service") or ""
            summary = row.get("summary") or row.get("title") or row.get("subject") or ""
            description = row.get("description") or row.get("body") or ""
            resolution = row.get("resolution") or row.get("root_cause") or ""
            created = row.get("created") or row.get("created_at") or ""
            updated = row.get("updated") or row.get("updated_at") or ""

            text = (
                f"TICKET: {ticket_id}\n"
                f"Component: {component}\n"
                f"Summary: {summary}\n\n"
                f"{description}\n\n"
                f"Resolution: {resolution}"
            ).strip()

            # Trim if huge
            if len(text) > MAX_CHUNK_CHARS:
                text = text[:MAX_CHUNK_CHARS]

            chunks.append(
                Chunk(
                    source_path=file.path,
                    processing_type=ProcessingType.TICKET_EXPORT,
                    text=text,
                    metadata=ChunkMetadata(
                        ticket_id=str(ticket_id),
                        component=component or None,
                        created_at=str(created) if created else None,
                        updated_at=str(updated) if updated else None,
                    ),
                )
            )
        return chunks

    # ── 3.4  Diagram / image ──────────────────────────────────────────────

    async def _process_diagram_image(self, file: ClassifiedFile) -> list[Chunk]:
        description = ""
        if self._vision:
            try:
                description = await self._vision.describe_image(file.path)
            except Exception as exc:
                logger.warning("vision_failed", path=file.path, error=str(exc))

        if not description:
            description = f"[Diagram image: {file.name}] — vision processing not available."

        text = f"FILE: {file.path}\nTYPE: diagram\n\n{description}"
        return [
            Chunk(
                source_path=file.path,
                processing_type=ProcessingType.DIAGRAM_IMAGE,
                text=text,
                metadata=ChunkMetadata(),
            )
        ]

    # ── 3.5  Doc with diagrams ────────────────────────────────────────────

    async def _process_doc_with_diagrams(self, file: ClassifiedFile) -> list[Chunk]:
        # Process the textual part as a regular doc
        chunks = await self._process_doc(file)

        # If vision client available, attempt image extraction for PDFs
        if self._vision and file.ext.lower() == ".pdf":
            try:
                images = _extract_pdf_images(file.path)
                for img_idx, img_path in enumerate(images):
                    desc = await self._vision.describe_image(img_path)
                    chunks.append(
                        Chunk(
                            source_path=file.path,
                            processing_type=ProcessingType.DOC_WITH_DIAGRAMS,
                            text=f"FILE: {file.path}\nEMBEDDED DIAGRAM #{img_idx + 1}\n\n{desc}",
                            metadata=ChunkMetadata(page_number=img_idx + 1),
                        )
                    )
            except Exception as exc:
                logger.warning("embedded_image_extract_failed", path=file.path, error=str(exc))

        return chunks

    # ── 3.6  Other / fallback ─────────────────────────────────────────────

    async def _process_other(self, file: ClassifiedFile) -> list[Chunk]:
        """Best-effort: try to read as text and chunk it."""
        text = _read_text(file.path)
        if not text.strip():
            return []
        chunks: list[Chunk] = []
        for piece in _sliding_window(text, MAX_CHUNK_CHARS, OVERLAP_CHARS):
            sl, el = _line_range_for_piece(text, piece)
            chunks.append(
                Chunk(
                    source_path=file.path,
                    processing_type=ProcessingType.OTHER,
                    text=f"FILE: {file.path}\n\n{piece}",
                    metadata=ChunkMetadata(
                        start_line=sl or None,
                        end_line=el or None,
                    ),
                )
            )
        return chunks


# ===========================================================================
# Helpers – text extraction
# ===========================================================================

def _read_text(path: str, max_bytes: int = 1024 * 1024 * 1024) -> str:
    """Read a file as UTF-8 text, ignoring errors. Default up to 1 GB per file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_bytes)
    except Exception:
        return ""


def _extract_pdf_text(path: str) -> list[tuple[int, str]]:
    """Return (page_number, text) tuples from a PDF."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber_not_installed")
        return []
    pages: list[tuple[int, str]] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append((i + 1, text))
    except Exception as exc:
        logger.warning("pdf_extract_error", path=path, error=str(exc))
    return pages


def _extract_pdf_images(path: str) -> list[str]:
    """Extract embedded images from a PDF and save to temp files.

    Returns a list of temporary file paths.
    """
    try:
        import pdfplumber
        import tempfile
    except ImportError:
        return []
    saved: list[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for img in page.images or []:
                    # pdfplumber exposes bounding boxes; raw image extraction
                    # requires additional tooling.  For now, skip.
                    pass
    except Exception:
        pass
    return saved


def _extract_docx_text(path: str) -> str:
    try:
        from docx import Document as DocxDocument
    except ImportError:
        logger.warning("python_docx_not_installed")
        return ""
    try:
        doc = DocxDocument(path)
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as exc:
        logger.warning("docx_extract_error", path=path, error=str(exc))
        return ""


def _extract_html_text(path: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("bs4_not_installed")
        return _read_text(path)
    try:
        raw = _read_text(path)
        soup = BeautifulSoup(raw, "html.parser")
        return soup.get_text(separator="\n")
    except Exception as exc:
        logger.warning("html_extract_error", path=path, error=str(exc))
        return ""


def _split_markdown_by_headings(path: str) -> list[tuple[str, str]]:
    """Split a markdown file into (heading, body) pairs."""
    text = _read_text(path)
    if not text.strip():
        return []
    sections: list[tuple[str, str]] = []
    current_heading = "(top)"
    current_lines: list[str] = []

    for line in text.splitlines():
        if re.match(r"^#{1,4}\s+", line):
            if current_lines:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = line.lstrip("#").strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, "\n".join(current_lines).strip()))
    return sections


# ===========================================================================
# Helpers – ticket row readers
# ===========================================================================

def _read_csv(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(dict(row))
    except Exception as exc:
        logger.warning("csv_read_error", path=path, error=str(exc))
    return rows


def _read_json_rows(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            # Try common wrapper keys
            for key in ("tickets", "issues", "incidents", "items", "data", "results"):
                if key in data and isinstance(data[key], list):
                    return [r for r in data[key] if isinstance(r, dict)]
            return [data]
    except Exception as exc:
        logger.warning("json_read_error", path=path, error=str(exc))
    return []


def _read_xlsx(path: str) -> list[dict]:
    try:
        import openpyxl
    except ImportError:
        logger.warning("openpyxl_not_installed")
        return []
    rows: list[dict] = []
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        if ws is None:
            return []
        headers: list[str] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                headers = [str(h or f"col_{j}").strip().lower() for j, h in enumerate(row)]
                continue
            rows.append({headers[j]: (str(v) if v is not None else "") for j, v in enumerate(row) if j < len(headers)})
        wb.close()
    except Exception as exc:
        logger.warning("xlsx_read_error", path=path, error=str(exc))
    return rows


# ===========================================================================
# Helpers – code splitting
# ===========================================================================

_TOP_LEVEL_PATTERN: dict[str, re.Pattern] = {
    "python":     re.compile(r"^(?:class |def |async def )", re.MULTILINE),
    "javascript": re.compile(r"^(?:function |class |export (?:default )?(?:function |class |const |let |var )|const |let |var )", re.MULTILINE),
    "typescript": re.compile(r"^(?:function |class |export (?:default )?(?:function |class |const |let |var |interface |type |enum )|const |let |var |interface |type |enum )", re.MULTILINE),
    "go":         re.compile(r"^func ", re.MULTILINE),
    "java":       re.compile(r"^(?:public |private |protected |class |interface |enum |@)", re.MULTILINE),
    "csharp":     re.compile(r"^(?:public |private |protected |internal |class |interface |enum |namespace )", re.MULTILINE),
    "ruby":       re.compile(r"^(?:class |module |def )", re.MULTILINE),
    "rust":       re.compile(r"^(?:pub |fn |struct |enum |impl |trait |mod )", re.MULTILINE),
}


def _split_code_into_symbols(
    text: str, language: str
) -> list[tuple[str, str, int, int]]:
    """Split source code into (symbol_name, block_text, start_line, end_line) at top-level definitions. Line numbers 1-based."""
    pattern = _TOP_LEVEL_PATTERN.get(language)
    if pattern is None:
        result = []
        for name, body in _split_by_blank_lines(text):
            sl, el = _line_range_for_piece(text, body)
            result.append((name, body, sl, el))
        return result

    lines = text.split("\n")
    split_indices: list[int] = []
    for idx, line in enumerate(lines):
        if language in ("python", "ruby"):
            if not line.startswith(" ") and not line.startswith("\t") and pattern.match(line):
                split_indices.append(idx)
        else:
            if pattern.match(line):
                split_indices.append(idx)

    if not split_indices:
        result = []
        for name, body in _split_by_blank_lines(text):
            sl, el = _line_range_for_piece(text, body)
            result.append((name, body, sl, el))
        return result

    result: list[tuple[str, str, int, int]] = []
    if split_indices[0] > 0:
        preamble = "\n".join(lines[: split_indices[0]]).strip()
        if preamble:
            result.append(("<module>", preamble, 1, split_indices[0]))

    for i, start in enumerate(split_indices):
        end = split_indices[i + 1] if i + 1 < len(split_indices) else len(lines)
        block_text = "\n".join(lines[start:end]).strip()
        symbol = _extract_symbol_name(lines[start], language)
        result.append((symbol, block_text, start + 1, end))
    return result


def _extract_symbol_name(line: str, language: str) -> str:
    stripped = line.strip()
    # Python
    if language == "python":
        m = re.match(r"(?:async\s+)?def\s+(\w+)", stripped)
        if m:
            return m.group(1)
        m = re.match(r"class\s+(\w+)", stripped)
        if m:
            return m.group(1)
    # JS / TS
    elif language in ("javascript", "typescript"):
        m = re.match(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)", stripped)
        if m:
            return m.group(1)
        m = re.match(r"(?:export\s+)?class\s+(\w+)", stripped)
        if m:
            return m.group(1)
        m = re.match(r"(?:export\s+)?(?:const|let|var)\s+(\w+)", stripped)
        if m:
            return m.group(1)
    # Go
    elif language == "go":
        m = re.match(r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)", stripped)
        if m:
            return m.group(1)
    # Generic fallback
    m = re.match(
        r"(?:pub(?:lic)?|private|protected|internal|static|final|abstract|virtual|override)?\s*"
        r"(?:class|interface|enum|struct|func|def|function|fn|trait|impl|mod|module|type)\s+(\w+)",
        stripped,
    )
    if m:
        return m.group(1)
    return "<block>"


def _split_by_blank_lines(text: str) -> list[tuple[str, str]]:
    """Fallback: group non-empty lines separated by blank lines."""
    blocks: list[tuple[str, str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            if current:
                blocks.append(("<block>", "\n".join(current)))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(("<block>", "\n".join(current)))
    # Merge tiny blocks so we don't end up with hundreds of 2-line chunks
    merged: list[tuple[str, str]] = []
    buf = ""
    for name, body in blocks:
        if len(buf) + len(body) < MAX_CHUNK_CHARS // 2:
            buf += ("\n\n" + body) if buf else body
        else:
            if buf:
                merged.append(("<block>", buf))
            buf = body
    if buf:
        merged.append(("<block>", buf))
    return merged


# ===========================================================================
# Helpers – line range for reading from disk (Cursor-style)
# ===========================================================================


def _line_range_for_piece(full_text: str, piece: str) -> tuple[int, int]:
    """Return (start_line_1based, end_line_1based) for piece inside full_text, or (0, 0) if not found."""
    piece_clean = piece.strip() or piece
    if not piece_clean:
        return (0, 0)
    pos = full_text.find(piece_clean)
    if pos == -1:
        pos = full_text.find(piece)
    if pos == -1:
        return (0, 0)
    start_line = full_text[:pos].count("\n") + 1
    end_pos = pos + len(piece_clean)
    end_line = full_text[:end_pos].count("\n") + 1
    return (start_line, end_line)


# ===========================================================================
# Helpers – sliding window
# ===========================================================================

def _sliding_window(
    text: str, max_size: int = MAX_CHUNK_CHARS, overlap: int = OVERLAP_CHARS
) -> list[str]:
    """Split *text* into overlapping chunks, breaking at paragraph or line boundaries."""
    if not text or not text.strip():
        return []
    if len(text) <= max_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_size, len(text))
        if end < len(text):
            # Try to break at a good boundary
            bp = text.rfind("\n\n", start + max_size // 2, end)
            if bp == -1:
                bp = text.rfind("\n", start + max_size // 2, end)
            if bp == -1:
                bp = text.rfind(". ", start + max_size // 2, end)
            if bp != -1:
                end = bp + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = end - overlap
    return chunks
