"""
OCR service — handles text extraction from image-based documents.

Used in two contexts:
  1. OCR *probe* (Phase 2): sample 2 random pages from a PDF, run OCR,
     check if the extracted text looks like real content (>= 100 words).
  2. Full OCR extraction (Phase 4): render every page to image and OCR
     the entire document for text_in_image files.

Dependencies (graceful degradation if missing):
  - pytesseract  (pip install pytesseract)
  - pdf2image    (pip install pdf2image)
  - System: tesseract-ocr (brew install tesseract / apt install tesseract-ocr)
"""

import random
import re
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_HAS_TESSERACT: Optional[bool] = None
_HAS_PDF2IMAGE: Optional[bool] = None


def _check_tesseract() -> bool:
    global _HAS_TESSERACT
    if _HAS_TESSERACT is not None:
        return _HAS_TESSERACT
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        _HAS_TESSERACT = True
    except Exception:
        _HAS_TESSERACT = False
        logger.info("tesseract_not_available", hint="Install tesseract-ocr for OCR support")
    return _HAS_TESSERACT


def _check_pdf2image() -> bool:
    global _HAS_PDF2IMAGE
    if _HAS_PDF2IMAGE is not None:
        return _HAS_PDF2IMAGE
    try:
        import pdf2image  # noqa: F401
        _HAS_PDF2IMAGE = True
    except ImportError:
        _HAS_PDF2IMAGE = False
        logger.info("pdf2image_not_available", hint="pip install pdf2image for OCR support")
    return _HAS_PDF2IMAGE


def _count_real_words(text: str) -> int:
    """Count words that look like natural language (>= 2 chars, alphabetic)."""
    words = re.findall(r"\b[a-zA-Z]{2,}\b", text)
    return len(words)


def ocr_probe_pdf(path: str, page_count: int) -> bool:
    """Sample 2 random pages from a PDF, OCR them, return True if >= 100 real words.

    Returns False if OCR dependencies are not installed (graceful).
    """
    if not _check_tesseract() or not _check_pdf2image():
        return False

    import pytesseract
    from pdf2image import convert_from_path

    sample_pages = random.sample(range(1, page_count + 1), min(2, page_count))
    total_words = 0

    for page_num in sample_pages:
        try:
            images = convert_from_path(
                path,
                first_page=page_num,
                last_page=page_num,
                dpi=200,
            )
            if not images:
                continue
            text = pytesseract.image_to_string(images[0])
            total_words += _count_real_words(text)
        except Exception as exc:
            logger.debug("ocr_probe_page_failed", path=path, page=page_num, error=str(exc))
            continue

    logger.debug("ocr_probe_result", path=path, pages_sampled=len(sample_pages), words=total_words)
    return total_words >= 100


def ocr_full_pdf(path: str, on_page: Optional[callable] = None) -> str:
    """OCR every page of a PDF and return the concatenated text.

    *on_page* is called with (page_num, total_pages) for progress.
    Returns empty string if OCR is not available.
    """
    if not _check_tesseract() or not _check_pdf2image():
        logger.warning("ocr_skipped_no_deps", path=path)
        return ""

    import pytesseract
    from pdf2image import convert_from_path

    all_text: list[str] = []

    try:
        images = convert_from_path(path, dpi=200)
    except Exception as exc:
        logger.error("ocr_pdf_render_failed", path=path, error=str(exc))
        return ""

    total = len(images)
    for idx, img in enumerate(images):
        page_num = idx + 1
        try:
            text = pytesseract.image_to_string(img)
            if text.strip():
                all_text.append(f"--- Page {page_num} ---\n{text.strip()}")
        except Exception as exc:
            logger.warning("ocr_page_failed", path=path, page=page_num, error=str(exc))
            all_text.append(f"--- Page {page_num} ---\n[OCR failed: {str(exc)[:60]}]")

        if on_page:
            try:
                on_page(page_num, total)
            except Exception:
                pass

    return "\n\n".join(all_text)


def ocr_image_file(path: str) -> str:
    """OCR a single image file (.png, .jpg, etc.) and return extracted text."""
    if not _check_tesseract():
        return ""

    import pytesseract
    from PIL import Image

    try:
        img = Image.open(path)
        text = pytesseract.image_to_string(img)
        return text.strip()
    except Exception as exc:
        logger.warning("ocr_image_failed", path=path, error=str(exc))
        return ""
