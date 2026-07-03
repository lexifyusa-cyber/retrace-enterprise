from __future__ import annotations

import re

from .cleaning import is_mostly_numbers_or_symbols, normalize_space
from .models import Element, FileJson, FolderFile, PageJson


DESCRIPTION_WORD_LIMIT_LONG = 110
DESCRIPTION_WORD_LIMIT_SHORT = 75
UNIT_HEADING_RE = re.compile(r"\bUNIT\s+(?:[IVXLC]+|\d+)\b", re.IGNORECASE)
UNIT_TOPIC_RE = re.compile(
    r"\bUNIT\s+(?:[IVXLC]+|\d+)\s*[-:–—]?\s*(.*?)"
    r"(?=\bUNIT\s+(?:[IVXLC]+|\d+)\b|\bCOURSE OUTCOMES\b|\bTOTAL\b|$)",
    re.IGNORECASE,
)
LABEL_PREFIX_RE = re.compile(r"^(title|section|paragraph|list|image|table):\s*", re.IGNORECASE)
BULLET_RE = re.compile(r"[\uf0b7\u2022\u25cf\u25aa\u25e6]+")
COVER_WORDS = {
    "department",
    "engineering",
    "college",
    "university",
    "school",
    "author",
    "date",
    "location",
    "presentation",
}
LOW_VALUE_HEADINGS = {
    "notes",
    "course objectives",
    "course outcomes",
    "text books",
    "reference books",
    "references books",
    "total",
}


def useful_pages_for_description(file_json: FileJson, min_chars: int = 300, max_pages: int = 10) -> list[PageJson]:
    candidates: list[tuple[PageJson, float]] = []
    seen: set[str] = set()
    for page in file_json.pages:
        text = _clean_description_text(page.rag_text)
        if not text or text in seen:
            continue
        seen.add(text)
        has_structural_element = any(
            element.type in {"title", "section_header", "table", "image", "key_value_pairs"}
            for element in page.elements
        )
        if len(text) < min_chars and not has_structural_element:
            continue
        if is_mostly_numbers_or_symbols(text):
            continue
        candidates.append((page, _page_description_score(page, text)))
    return _select_representative_pages(candidates, file_json.page_count, max_pages)


def create_file_description(file_json: FileJson, min_chars: int = 300, max_pages: int = 10) -> str:
    pages = useful_pages_for_description(file_json, min_chars=min_chars, max_pages=max_pages)
    if not pages:
        return f"{file_json.filename} contains {file_json.page_count} processed page{'s' if file_json.page_count != 1 else ''}."

    topics = _extract_topics(pages)
    doc_kind = _document_kind(file_json, pages)
    if topics:
        topic_text = _join_topics(topics[:10])
        concise = f"{file_json.filename} is {doc_kind} covering {topic_text}."
    else:
        snippets = [_page_description_snippet(page) for page in pages]
        concise = f"{file_json.filename} is {doc_kind} covering {normalize_space(' '.join(snippets))}."

    word_limit = DESCRIPTION_WORD_LIMIT_LONG if file_json.page_count >= 20 else DESCRIPTION_WORD_LIMIT_SHORT
    words = concise.split()
    concise = " ".join(words[:word_limit]).strip()
    if not concise.endswith("."):
        concise += "."
    return concise


def create_folder_description(
    folder_name: str,
    folder_files: list[FolderFile],
    file_jsons: list[FileJson],
    min_chars: int = 300,
    max_pages_per_file: int = 10,
    max_topics: int = 12,
) -> str:
    if not folder_files:
        return f"{folder_name} contains no processed files."

    file_lookup = {file_json.file_path: file_json for file_json in file_jsons}
    evidence = _rank_folder_topics(folder_files, file_lookup, min_chars, max_pages_per_file)
    topics = [topic for topic, _score in evidence[:max_topics]]
    kind_summary = _folder_kind_summary(file_lookup.values())
    feature_summary = _folder_feature_summary(file_lookup.values())
    file_count = len(folder_files)
    file_word = "file" if file_count == 1 else "files"

    if topics:
        description = f"{folder_name} contains {file_count} processed {file_word} focused on {_join_topics(topics)}"
    else:
        filenames = _join_topics([file.filename for file in folder_files[:4]])
        description = f"{folder_name} contains {file_count} processed {file_word}, including {filenames}"

    details = [part for part in [kind_summary, feature_summary] if part]
    if details:
        description += ". It includes " + " and ".join(details)
    description = _clean_description_text(description)
    words = description.split()
    description = " ".join(words[:95]).strip()
    if not description.endswith("."):
        description += "."
    return description


def _rank_folder_topics(
    folder_files: list[FolderFile],
    file_lookup: dict[str, FileJson],
    min_chars: int,
    max_pages_per_file: int,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    labels: dict[str, str] = {}
    order: dict[str, int] = {}

    def add(raw_topic: str, score: float) -> None:
        topic = _normalize_topic(raw_topic)
        if not topic:
            return
        key = _topic_key(topic)
        if _is_low_value_folder_topic(key, folder_files):
            return
        if key not in labels:
            labels[key] = topic
            order[key] = len(order)
        scores[key] = scores.get(key, 0.0) + score

    for file_record in folder_files:
        for tag in file_record.tags:
            add(tag, 3.0)
        for topic in _topics_from_file_description(file_record.file_description):
            add(topic, 2.25)

        file_json = file_lookup.get(file_record.file_path)
        if not file_json:
            continue
        pages = useful_pages_for_description(file_json, min_chars=min_chars, max_pages=max_pages_per_file)
        for topic in _extract_topics(pages):
            add(topic, 4.0)
        for page in pages:
            for element in page.elements:
                if element.type == "title" and element.text:
                    add(element.text, 2.0)
                elif element.type == "section_header" and element.text:
                    add(element.text, 2.75)
                elif element.type == "table" and element.table_description:
                    add(element.table_description, 2.5)
                elif element.type == "image" and element.summary:
                    add(element.summary, 1.25)

    ranked = sorted(scores, key=lambda key: (-scores[key], order[key], labels[key].lower()))
    return [(labels[key], scores[key]) for key in ranked]


def _topics_from_file_description(description: str) -> list[str]:
    cleaned = _clean_description_text(description)
    if " covering " in cleaned:
        cleaned = cleaned.split(" covering ", 1)[1]
    cleaned = re.sub(r"^[^:]+:\s*", "", cleaned)
    cleaned = cleaned.rstrip(".")
    parts = re.split(r",\s+|\s+and\s+|;", cleaned)
    return _dedupe_topics(_normalize_topic(part) for part in parts)


def _folder_kind_summary(file_jsons: object) -> str:
    counts: dict[str, int] = {}
    for file_json in file_jsons:
        pages = useful_pages_for_description(file_json, max_pages=5)
        kind = _document_kind(file_json, pages) if pages else "a document"
        label = kind.removeprefix("a ").removeprefix("an ")
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    labels = [label for label, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]]
    if len(labels) == 1:
        return labels[0] + "s"
    return _join_topics([label + "s" for label in labels])


def _folder_feature_summary(file_jsons: object) -> str:
    has_tables = False
    has_visuals = False
    has_forms = False
    for file_json in file_jsons:
        for page in file_json.pages:
            for element in page.elements:
                has_tables = has_tables or element.type == "table"
                has_visuals = has_visuals or element.type == "image"
                has_forms = has_forms or element.type == "key_value_pairs"
    features: list[str] = []
    if has_tables:
        features.append("structured tables")
    if has_visuals:
        features.append("visual summaries")
    if has_forms:
        features.append("form fields")
    return _join_topics(features) if features else ""


def _topic_key(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", topic.lower()).strip()


def _is_low_value_folder_topic(key: str, folder_files: list[FolderFile]) -> bool:
    if not key or key in LOW_VALUE_HEADINGS or len(key) <= 2:
        return True
    filenames = {_topic_key(file.filename.rsplit(".", 1)[0]) for file in folder_files}
    if key in filenames:
        return True
    if key.endswith(" processed page") or key.endswith(" processed pages") or " contains " in key:
        return True
    if key.startswith("department of") or " department of " in f" {key} ":
        return True
    cover_hits = sum(1 for word in COVER_WORDS if word in key)
    if cover_hits >= 2 and not any(signal in key for signal in {"policy", "protocol", "safety", "iot", "cloud", "security"}):
        return True
    return False


def _clean_description_text(text: str) -> str:
    text = BULLET_RE.sub(" ", text)
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([.;:])\1+", r"\1", text)
    cleaned = normalize_space(text)
    return LABEL_PREFIX_RE.sub("", cleaned)


def _page_description_score(page: PageJson, text: str) -> float:
    score = min(len(text) / 250, 4.0)
    types = {element.type for element in page.elements}
    if "paragraph" in types:
        score += 2.0
    if "list" in types:
        score += 1.5
    if "table" in types:
        score += 1.0
    if "image" in types:
        score += 0.75
    if UNIT_HEADING_RE.search(text):
        score += 4.0
    score += min(sum(1 for element in page.elements if element.type == "section_header") * 0.5, 2.0)
    if _is_weak_cover_page(page, text):
        score -= 5.0
    return score


def _is_weak_cover_page(page: PageJson, text: str) -> bool:
    if page.page_number > 3:
        return False
    lowered = text.lower()
    word_count = len(text.split())
    paragraph_chars = sum(len(element.text or "") for element in page.elements if element.type == "paragraph")
    cover_hits = sum(1 for word in COVER_WORDS if word in lowered)
    has_real_topic = bool(UNIT_HEADING_RE.search(text)) or paragraph_chars >= 180
    structural_only = all(element.type in {"title", "section_header"} for element in page.elements)
    return not has_real_topic and (structural_only or word_count < 45) and cover_hits >= 1


def _select_representative_pages(
    candidates: list[tuple[PageJson, float]],
    page_count: int,
    max_pages: int,
) -> list[PageJson]:
    if not candidates or max_pages <= 0:
        return []
    if len(candidates) == 1:
        return [candidates[0][0]]

    strong = [(page, score) for page, score in candidates if score > -1.0]
    pool = strong or candidates
    if len(pool) <= max_pages:
        return [page for page, _score in sorted(pool, key=lambda item: item[0].page_number)]

    targets = _target_page_numbers(page_count, max_pages)
    selected: list[tuple[PageJson, float]] = []
    remaining = pool[:]
    for target in targets:
        best = max(
            remaining,
            key=lambda item: item[1] - abs(item[0].page_number - target) / max(page_count, 1),
        )
        selected.append(best)
        remaining.remove(best)
        if not remaining:
            break
    return [page for page, _score in sorted(selected, key=lambda item: item[0].page_number)]


def _target_page_numbers(page_count: int, max_pages: int) -> list[int]:
    if max_pages == 1:
        return [max(1, page_count // 2)]
    step = max(page_count - 1, 1) / (max_pages - 1)
    return [max(1, round(1 + step * index)) for index in range(max_pages)]


def _document_kind(file_json: FileJson, pages: list[PageJson]) -> str:
    combined = " ".join(_clean_description_text(page.rag_text).lower() for page in pages)
    filename = file_json.filename.lower()
    if "course objectives" in combined or "unit i" in combined or "notes" in filename:
        return "a course notes document"
    if any(element.type == "key_value_pairs" for page in pages for element in page.elements):
        return "a form document"
    if any(element.type == "table" for page in pages for element in page.elements):
        return "a structured document"
    if any(element.type == "image" for page in pages for element in page.elements):
        return "a visual document"
    return "a document"


def _extract_topics(pages: list[PageJson]) -> list[str]:
    unit_topics: list[str] = []
    structural_topics: list[str] = []
    list_topics: list[str] = []
    for page in pages:
        text = _clean_description_text(page.rag_text)
        for match in UNIT_TOPIC_RE.finditer(text):
            unit_topics.append(match.group(1))
        for element in page.elements:
            if element.type == "section_header" and element.text:
                structural_topics.append(element.text)
            elif element.type == "title" and element.text and not unit_topics:
                structural_topics.append(element.text)
            elif element.type == "table" and element.table_description:
                structural_topics.append(element.table_description)
            elif element.type == "image":
                structural_topics.append(f"{element.kind or 'visual'} visuals")
            elif element.type == "list" and element.items:
                list_topics.extend(item.text for item in element.items[:3])

    primary = _dedupe_topics(_normalize_topic(topic) for topic in unit_topics + structural_topics)
    list_fallback = _dedupe_topics(_normalize_topic(topic) for topic in list_topics)
    if len(primary) < 3:
        return _dedupe_topics(primary + list_fallback)
    if primary:
        return primary
    return list_fallback


def _normalize_topic(topic: str) -> str:
    topic = _clean_description_text(topic)
    topic = UNIT_HEADING_RE.sub("", topic)
    topic = re.sub(r"\b\d+\s+.*$", "", topic)
    topic = re.sub(r"\s+-\s+.*$", "", topic)
    topic = re.sub(r"\b(total|periods|paragraph)\b.*$", "", topic, flags=re.IGNORECASE)
    topic = topic.strip(" :-.,;")
    topic = re.sub(r"^(and|or)\s+", "", topic, flags=re.IGNORECASE)
    words = topic.split()
    if len(words) > 12:
        topic = " ".join(words[:12])
    letters = [char for char in topic if char.isalpha()]
    if letters and sum(char.isupper() for char in letters) / len(letters) > 0.7:
        topic = topic.title()
    replacements = {
        "Iot": "IoT",
        "M2M": "M2M",
        "Sdn": "SDN",
        "Nfv": "NFV",
        "Api": "API",
        "Apis": "APIs",
        "Wifi": "Wi-Fi",
    }
    for old, new in replacements.items():
        topic = re.sub(rf"\b{old}\b", new, topic)
    return topic


def _dedupe_topics(topics: object) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw_topic in topics:
        topic = str(raw_topic).strip()
        if not topic:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", topic.lower()).strip()
        if not key or key in seen or key in LOW_VALUE_HEADINGS:
            continue
        if key.endswith(" can") or key.endswith(" include") or key.endswith(" includes"):
            continue
        if len(key) <= 2:
            continue
        seen.add(key)
        output.append(topic)
    return output


def _join_topics(topics: list[str]) -> str:
    if not topics:
        return "the extracted document content"
    if len(topics) == 1:
        return topics[0]
    return ", ".join(topics[:-1]) + f", and {topics[-1]}"


def _element_summary(element: Element) -> str:
    if element.type == "title" and element.text:
        return _clean_description_text(element.text)
    if element.type == "section_header" and element.text:
        return _clean_description_text(element.text)
    if element.type == "paragraph" and element.text:
        return _clean_description_text(element.text)
    if element.type == "list" and element.items:
        return "; ".join(_clean_description_text(item.text) for item in element.items[:5])
    if element.type == "table":
        return _clean_description_text(element.table_description or "table")
    if element.type == "image":
        return _clean_description_text(element.summary or element.kind or "embedded visual content")
    if element.type == "key_value_pairs" and element.pairs:
        return "form fields " + "; ".join(_clean_description_text(pair.key) for pair in element.pairs[:8])
    return ""


def _page_description_snippet(page: PageJson) -> str:
    parts = [_element_summary(element) for element in page.elements]
    text = _clean_description_text(". ".join(part for part in parts if part))
    return text[:500] if text else _clean_description_text(page.rag_text)[:500]
