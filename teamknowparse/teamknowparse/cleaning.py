from __future__ import annotations

import re
from collections import Counter

SYMBOL_RE = re.compile(r"^[\W_]+$")
MOSTLY_SYMBOLS_RE = re.compile(r"[\w]", re.UNICODE)
PAGE_NUM_RE = re.compile(r"^(page\s*)?\d+(\s+of\s+\d+)?$", re.IGNORECASE)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_mostly_numbers_or_symbols(text: str) -> bool:
    cleaned = normalize_space(text)
    if not cleaned:
        return True
    alnum = sum(ch.isalnum() for ch in cleaned)
    return alnum / max(len(cleaned), 1) < 0.35


def is_noise_line(line: str) -> bool:
    value = normalize_space(line)
    if not value:
        return True
    if len(value) <= 2 and not value.isalnum():
        return True
    if SYMBOL_RE.match(value):
        return True
    if PAGE_NUM_RE.match(value):
        return True
    low = value.lower()
    if low in {"confidential", "company confidential", "internal use only"}:
        return True
    return False


def repeated_header_footer_lines(pages: list[list[str]], min_repeats: int = 2) -> set[str]:
    candidates: list[str] = []
    for lines in pages:
        cleaned = [normalize_space(line) for line in lines if normalize_space(line)]
        if not cleaned:
            continue
        per_page_candidates = set(cleaned[:3] + cleaned[-3:])
        candidates.extend(per_page_candidates)
    counts = Counter(candidates)
    return {line for line, count in counts.items() if count >= min_repeats and len(line) <= 140}


def clean_lines(lines: list[str], repeated_noise: set[str] | None = None) -> list[str]:
    repeated_noise = repeated_noise or set()
    output: list[str] = []
    for line in lines:
        value = normalize_space(line)
        if is_noise_line(value):
            continue
        if value in repeated_noise:
            continue
        output.append(value)
    return output


def clean_text(text: str, repeated_noise: set[str] | None = None) -> str:
    return "\n".join(clean_lines(text.splitlines(), repeated_noise))

