from __future__ import annotations

import re

from .cleaning import is_mostly_numbers_or_symbols
from .native_extract import strong_form_text


TABLE_HINT_RE = re.compile(r"(\|.+\||\t| {2,}\S+ {2,}\S+)")
VISUAL_HINT_RE = re.compile(r"\b(diagram|figure|chart|image|screenshot|architecture|flow)\b", re.IGNORECASE)


def classify_page(text: str, image_area_ratio: float = 0.0) -> str:
    stripped = text.strip()
    if not stripped:
        return "SCANNED" if image_area_ratio > 0.5 else "VISUAL"
    if len(stripped) < 120 and image_area_ratio > 0.55:
        return "VISUAL"
    if image_area_ratio > 0.70:
        return "MIXED"
    if strong_form_text(stripped):
        return "FORM"
    if TABLE_HINT_RE.search(stripped):
        return "TABLE"
    if VISUAL_HINT_RE.search(stripped) and image_area_ratio > 0.08:
        return "MIXED"
    if is_mostly_numbers_or_symbols(stripped):
        return "FAILED"
    return "DIGITAL_PURE_TEXT"
