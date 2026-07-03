from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .cleaning import clean_text, normalize_space
from .models import Element


@dataclass
class NativePage:
    text: str
    elements: list[Element] | None = None
    page_type: str | None = None


def split_text_elements(text: str) -> list[Element]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    elements: list[Element] = []
    paragraph: list[str] = []
    list_items: list[dict[str, int | str]] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            elements.append(Element(type="paragraph", text=" ".join(paragraph)))
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            elements.append(Element(type="list", ordered=False, items=list_items))
            list_items = []

    for idx, line in enumerate(lines):
        bullet = re.match(r"^(\s*)([-*•]|\d+[.)])\s+(.+)$", line)
        if idx == 0 and len(line) <= 140 and not bullet:
            flush_paragraph()
            flush_list()
            elements.append(Element(type="title", text=line, level=1))
            continue
        if bullet:
            flush_paragraph()
            indent = len(bullet.group(1))
            list_items.append({"text": bullet.group(3).strip(), "level": min(indent // 2, 3)})
            continue
        if list_items and not _looks_like_heading(line):
            list_items[-1]["text"] = normalize_space(f"{list_items[-1]['text']} {line}")
            continue
        if re.match(r"^(\d+(\.\d+)*\.?\s+|[A-Z][A-Z0-9 /&-]{4,}$)", line) and len(line) <= 160:
            flush_paragraph()
            flush_list()
            elements.append(Element(type="section_header", text=line, level=2))
            continue
        flush_list()
        paragraph.append(line)

    flush_paragraph()
    flush_list()
    return elements


def is_form_line(line: str) -> bool:
    value = line.strip()
    if not value:
        return False
    if re.search(r"_{3,}|\[\s*\]|☐|☑", value):
        return True
    if ":" in value and len(value) <= 160:
        key, raw = value.split(":", 1)
        key = key.strip()
        low_key = key.lower()
        if low_key in {"for example", "remember", "note", "notes", "example"}:
            return False
        if len(key.split()) > 9:
            return False
        return 2 <= len(key) <= 80 and len(raw.strip()) <= 120
    return False


def strong_form_text(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    form_lines = [line for line in lines if is_form_line(line)]
    if len(form_lines) < 3:
        return False
    density = len(form_lines) / max(len(lines), 1)
    blank_field_count = sum(1 for line in form_lines if re.search(r"_{3,}|\[\s*\]|☐|☑", line))
    if len(lines) <= 8:
        return density >= 0.5 or blank_field_count >= 2
    return len(form_lines) >= 4 and (density >= 0.35 or blank_field_count >= 3)


def extract_form_pairs(text: str) -> list[Element]:
    pairs = []
    for line in text.splitlines():
        value = line.strip()
        if not is_form_line(value):
            continue
        if ":" in value and len(value) <= 180:
            key, raw = value.split(":", 1)
            key = key.strip()
            raw = raw.strip().strip("_")
            if 2 <= len(key) <= 80:
                pairs.append({"key": key, "value": raw or None})
        elif re.search(r"_{3,}", value):
            key = re.sub(r"_{3,}.*$", "", value).strip(" :-")
            if key:
                pairs.append({"key": key, "value": None})
    if not pairs:
        return []
    return [Element(type="key_value_pairs", title="Form Fields", pairs=pairs)]


def markdown_table_from_rows(rows: list[list[str]], description: str = "Table") -> Element | None:
    cleaned_rows = [[normalize_space(cell) for cell in row] for row in rows]
    cleaned_rows = [row for row in cleaned_rows if any(row)]
    if len(cleaned_rows) < 1:
        return None
    width = max(len(row) for row in cleaned_rows)
    padded = [row + [""] * (width - len(row)) for row in cleaned_rows]
    if width < 2:
        return None
    header = padded[0]
    sep = ["---"] * width
    md_lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in padded[1:]:
        md_lines.append("| " + " | ".join(row) + " |")
    return Element(type="table", table_description=description, markdown="\n".join(md_lines))


def markdown_table_from_text(text: str) -> Element | None:
    rows = []
    for line in text.splitlines():
        if "|" in line:
            cols = [col.strip() for col in line.strip("|").split("|")]
        elif "\t" in line:
            cols = [col.strip() for col in line.split("\t")]
        else:
            continue
        if len(cols) >= 2:
            rows.append(cols)
    return markdown_table_from_rows(rows, "Detected table")


def rag_text_from_elements(elements: list[Element]) -> str:
    parts: list[str] = []
    for element in elements:
        data = element.model_dump(exclude_none=True)
        if element.type in {"title", "section_header", "paragraph"} and data.get("text"):
            label = {"title": "Title", "section_header": "Section", "paragraph": "Paragraph"}[element.type]
            parts.append(f"{label}: {data['text']}")
        elif element.type == "list":
            items = [str(item["text"]) for item in data.get("items", []) if item.get("text")]
            if items:
                parts.append("List: " + "; ".join(items))
        elif element.type == "table" and data.get("markdown"):
            description = data.get("table_description") or "Table"
            parts.append(f"Table: {description}. {data['markdown']}")
        elif element.type == "image" and (data.get("summary") or data.get("image_description")):
            kind = data.get("kind") or "visual"
            parts.append(f"Image: {kind}. {data.get('summary') or data['image_description']}")
        elif element.type == "key_value_pairs":
            fields = []
            for pair in data.get("pairs", []):
                val = pair.get("value")
                fields.append(f"{pair.get('key')}: {val if val is not None else 'blank'}")
            if fields:
                parts.append("Form fields: " + "; ".join(fields))
    return " ".join(parts).strip()


def extract_native_page(text: str, page_type: str, repeated_noise: set[str] | None = None) -> tuple[list[Element], str]:
    cleaned = clean_text(text, repeated_noise)
    elements: list[Element] = []
    body_text = cleaned
    if page_type == "FORM" and strong_form_text(cleaned):
        elements.extend(extract_form_pairs(cleaned))
        body_text = "\n".join(line for line in cleaned.splitlines() if not is_form_line(line))
    table = markdown_table_from_text(body_text)
    if page_type == "TABLE" and table is not None:
        elements.append(table)
    elements.extend(split_text_elements(body_text))
    rag_text = rag_text_from_elements(elements)
    return elements, rag_text


def _element_text(element: Element) -> str:
    if element.type in {"title", "section_header", "paragraph"} and element.text:
        return element.text
    if element.type == "list" and element.items:
        return "\n".join(item.text for item in element.items)
    if element.type == "table" and element.markdown:
        return element.markdown
    if element.type == "key_value_pairs" and element.pairs:
        return "\n".join(f"{pair.key}: {pair.value or ''}" for pair in element.pairs)
    if element.type == "image" and element.summary:
        return element.summary
    return ""


def _native_page(elements: list[Element]) -> NativePage:
    text = "\n".join(part for part in (_element_text(element) for element in elements) if part).strip()
    has_image = any(element.type == "image" for element in elements)
    has_non_image = any(element.type != "image" for element in elements)
    if has_image and has_non_image:
        page_type = "MIXED"
    elif has_image:
        page_type = "VISUAL"
    elif any(element.type == "key_value_pairs" for element in elements):
        page_type = "FORM"
    elif any(element.type == "table" for element in elements):
        page_type = "TABLE"
    else:
        page_type = "DIGITAL_PURE_TEXT"
    return NativePage(text=text, elements=elements, page_type=page_type)


@dataclass
class _SectionBuilder:
    elements: list[Element] = field(default_factory=list)
    word_count: int = 0

    def add(self, element: Element) -> None:
        self.elements.append(element)
        self.word_count += len(_element_text(element).split())

    def is_empty(self) -> bool:
        return not self.elements


def _finalize_section(section: _SectionBuilder, pages: list[NativePage]) -> None:
    if not section.is_empty():
        pages.append(_native_page(section.elements))
        section.elements = []
        section.word_count = 0


def _style_name(obj: object) -> str:
    style = getattr(obj, "style", None)
    name = getattr(style, "name", "") if style is not None else ""
    return str(name or "")


def _is_list_paragraph(paragraph: object) -> bool:
    style = _style_name(paragraph).lower()
    if "list" in style or "bullet" in style:
        return True
    p = getattr(paragraph, "_p", None)
    return bool(p is not None and p.pPr is not None and p.pPr.numPr is not None)




def _looks_like_heading(text: str) -> bool:
    value = text.strip()
    if not value or len(value) > 140:
        return False
    if value.endswith((".", ",", ";")):
        return False
    words = value.split()
    if len(words) > 12:
        return False
    if value.rstrip().endswith(":") and len(words) <= 8:
        return True
    low = value.lower()
    common_headings = {
        "overview", "functions", "employment standards", "template", "templates",
        "explanation", "policies", "safety policies and procedures", "table of contents",
        "job descriptions", "dress code", "attendance", "program", "date",
    }
    if low in common_headings:
        return True
    letters = [ch for ch in value if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.75:
        return True
    title_words = sum(1 for word in words if word[:1].isupper())
    return len(words) <= 7 and title_words / max(len(words), 1) >= 0.7

def _paragraph_element(paragraph: object, first_content: bool) -> Element | None:
    text = normalize_space(getattr(paragraph, "text", ""))
    if not text:
        return None
    style = _style_name(paragraph).lower()
    if _is_list_paragraph(paragraph):
        return Element(type="list", ordered=False, items=[{"text": text, "level": 0}])
    if style.startswith("title") or (first_content and len(text) <= 160):
        return Element(type="title", text=text, level=1)
    if style.startswith("heading"):
        level_match = re.search(r"(\d+)", style)
        level = int(level_match.group(1)) if level_match else 2
        return Element(type="section_header", text=text, level=min(max(level, 2), 6))
    if _looks_like_heading(text):
        return Element(type="section_header", text=text, level=2)
    return Element(type="paragraph", text=text)


def _paragraph_has_image(paragraph: object) -> bool:
    xml = getattr(getattr(paragraph, "_p", None), "xml", "")
    return "w:drawing" in xml or "w:pict" in xml or "pic:pic" in xml


def _table_element_from_docx(table: object) -> Element | None:
    rows = []
    for row in table.rows:
        rows.append([cell.text.strip() for cell in row.cells])
    return markdown_table_from_rows(rows, "DOCX table")


def _iter_docx_blocks(document: object) -> Iterable[object]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def _pdf_block_element(text: str, first_content: bool) -> Element | None:
    value = normalize_space(text)
    if not value:
        return None
    if first_content and len(value) <= 180:
        return Element(type="title", text=value, level=1)
    if _looks_like_heading(value):
        return Element(type="section_header", text=value, level=2)
    if re.match(r"^([-*•]|\d+[.)])\s+", value):
        item_text = re.sub(r"^([-*•]|\d+[.)])\s+", "", value).strip()
        return Element(type="list", ordered=False, items=[{"text": item_text, "level": 0}])
    return Element(type="paragraph", text=value)


def _pdf_image_area_ratio(page: object) -> float:
    page_area = float(page.rect.width * page.rect.height) or 1.0
    image_area = 0.0
    seen: set[tuple[int, float, float, float, float]] = set()
    for image in page.get_images(full=True):
        xref = int(image[0])
        for rect in page.get_image_rects(xref):
            key = (xref, round(float(rect.x0), 1), round(float(rect.y0), 1), round(float(rect.x1), 1), round(float(rect.y1), 1))
            if key in seen:
                continue
            seen.add(key)
            image_area += max(float(rect.width), 0.0) * max(float(rect.height), 0.0)
    return min(image_area / page_area, 1.0)


def extract_pdf_pages(path: Path) -> list[NativePage]:
    import fitz

    doc = fitz.open(path)
    pages: list[NativePage] = []
    try:
        for page in doc:
            raw_blocks = page.get_text("blocks")
            blocks = []
            for block in raw_blocks:
                if len(block) < 5:
                    continue
                x0, y0, x1, y1, raw_text = block[:5]
                value = " ".join(str(raw_text).split())
                if not value:
                    continue
                if re.fullmatch(r"\d{1,4}", value) and (float(y0) < 40 or float(y0) > page.rect.height - 80):
                    continue
                blocks.append((float(y0), float(x0), float(x1), float(y1), value))
            blocks.sort(key=lambda item: (item[0], item[1]))

            if not blocks:
                image_ratio = _pdf_image_area_ratio(page)
                page_type = "SCANNED" if image_ratio >= 0.50 else "FAILED"
                pages.append(NativePage(text="", elements=[], page_type=page_type))
                continue

            left_margin = min(x0 for _, x0, _, _, _ in blocks)
            line_heights = [max(y1 - y0, 1.0) for y0, _, _, y1, _ in blocks]
            typical_height = sorted(line_heights)[len(line_heights) // 2]
            elements: list[Element] = []
            pending_list: list[dict[str, int | str]] = []
            paragraph_parts: list[str] = []
            previous_bottom: float | None = None

            def flush_paragraph() -> None:
                nonlocal paragraph_parts
                if paragraph_parts:
                    elements.append(Element(type="paragraph", text=normalize_space(" ".join(paragraph_parts))))
                    paragraph_parts = []

            def flush_list() -> None:
                nonlocal pending_list
                if pending_list:
                    elements.append(Element(type="list", ordered=False, items=pending_list))
                    pending_list = []

            for y0, x0, _x1, y1, value in blocks:
                element = _pdf_block_element(value, first_content=not elements and not pending_list and not paragraph_parts)
                if element is None:
                    continue
                vertical_gap = 0.0 if previous_bottom is None else y0 - previous_bottom
                close_to_previous = previous_bottom is not None and vertical_gap <= typical_height * 1.8
                continuation_indent = bool(pending_list) and x0 >= left_margin - 8

                if element.type in {"title", "section_header"}:
                    flush_paragraph()
                    flush_list()
                    elements.append(element)
                    previous_bottom = y1
                    continue
                if element.type == "list" and element.items:
                    flush_paragraph()
                    pending_list.extend(item.model_dump() for item in element.items)
                    previous_bottom = y1
                    continue
                if pending_list and close_to_previous and continuation_indent:
                    pending_list[-1]["text"] = normalize_space(f"{pending_list[-1]['text']} {value}")
                    previous_bottom = y1
                    continue

                flush_list()
                indented_first_line = x0 > left_margin + 20
                large_gap = vertical_gap > typical_height * 1.6
                if paragraph_parts and (indented_first_line or large_gap):
                    flush_paragraph()
                paragraph_parts.append(value)
                previous_bottom = y1

            flush_paragraph()
            flush_list()
            if elements:
                native_page = _native_page(elements)
                if _pdf_image_area_ratio(page) >= 0.08:
                    native_page.page_type = "MIXED"
                pages.append(native_page)
            else:
                pages.append(NativePage(text="", elements=[], page_type="FAILED"))
    finally:
        doc.close()
    return pages or [NativePage(text="", elements=[], page_type="FAILED")]


def extract_docx_pages(path: Path, max_words_per_page: int = 1800) -> list[NativePage]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(path)
    pages: list[NativePage] = []
    section = _SectionBuilder()
    first_content = True
    pending_list: list[dict[str, int | str]] = []

    def flush_list() -> None:
        nonlocal pending_list
        if pending_list:
            section.add(Element(type="list", ordered=False, items=pending_list))
            pending_list = []

    for block in _iter_docx_blocks(doc):
        if isinstance(block, Paragraph):
            element = _paragraph_element(block, first_content)
            has_image = _paragraph_has_image(block)
            if element is None and not has_image:
                continue
            if element is not None:
                first_content = False
                if element.type == "section_header" and section.word_count >= 250:
                    flush_list()
                    _finalize_section(section, pages)
                if element.type == "list" and element.items:
                    pending_list.extend(item.model_dump() for item in element.items)
                    continue
                flush_list()
                section.add(element)
            if has_image:
                flush_list()
                section.add(Element(type="image", kind="embedded_image", summary="Document contains an embedded image."))
        elif isinstance(block, Table):
            flush_list()
            element = _table_element_from_docx(block)
            if element is not None:
                section.add(element)
        if section.word_count >= max_words_per_page:
            flush_list()
            _finalize_section(section, pages)
    flush_list()
    _finalize_section(section, pages)
    return pages or [NativePage(text="", elements=[], page_type="FAILED")]


def extract_pptx_pages(path: Path) -> list[NativePage]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(path)
    pages: list[NativePage] = []

    def shape_has_visual(shape: object) -> bool:
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
            return True
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
            return any(shape_has_visual(child) for child in getattr(shape, "shapes", []))
        xml = getattr(getattr(shape, "element", None), "xml", "")
        return "blip" in xml or "pic:" in xml

    def iter_shapes(shapes: object):
        for shape in shapes:
            yield shape
            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
                yield from iter_shapes(getattr(shape, "shapes", []))

    for index, slide in enumerate(prs.slides, start=1):
        elements: list[Element] = []
        for shape in iter_shapes(slide.shapes):
            if getattr(shape, "has_table", False):
                rows = []
                for row in shape.table.rows:
                    rows.append([cell.text.strip() for cell in row.cells])
                table = markdown_table_from_rows(rows, "PPTX table")
                if table:
                    elements.append(table)
                continue
            if shape_has_visual(shape):
                elements.append(Element(type="image", kind="embedded_image", summary="Slide contains an embedded image."))
                continue
            text = normalize_space(getattr(shape, "text", ""))
            if not text:
                continue
            placeholder = getattr(shape, "placeholder_format", None) if getattr(shape, "is_placeholder", False) else None
            placeholder_type = str(getattr(placeholder, "type", "")).lower() if placeholder else ""
            if "title" in placeholder_type or not any(el.type == "title" for el in elements):
                elements.append(Element(type="title", text=text, level=1))
            elif "\n" in getattr(shape, "text", ""):
                elements.extend(split_text_elements(shape.text))
            else:
                elements.append(Element(type="paragraph", text=text))
        if not elements:
            elements.append(Element(type="paragraph", text=f"Slide {index}"))
        pages.append(_native_page(elements))
    return pages or [NativePage(text="", elements=[], page_type="FAILED")]


def extract_xlsx_pages(path: Path) -> list[NativePage]:
    import openpyxl

    # Chunk large sheets into multiple embeddable pages so no single chunk
    # exceeds the embedding per-input / per-request token limits. The header
    # row is repeated on every batch so each chunk is self-describing.
    rows_per_chunk = int(os.getenv("XLSX_ROWS_PER_CHUNK", "50"))
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    pages: list[NativePage] = []
    for ws in wb.worksheets:
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            values = ["" if cell is None else str(cell) for cell in row]
            if any(value.strip() for value in values):
                rows.append(values)
        if not rows:
            continue
        header = rows[0]
        body = rows[1:]
        if body:
            batches = [[header] + body[i:i + rows_per_chunk] for i in range(0, len(body), rows_per_chunk)]
        else:
            batches = [rows]
        multiple = len(batches) > 1
        for idx, batch in enumerate(batches, start=1):
            label = (f"XLSX sheet: {ws.title} (part {idx} of {len(batches)})"
                     if multiple else f"XLSX sheet: {ws.title}")
            table = markdown_table_from_rows(batch, label)
            if table:
                pages.append(_native_page([table]))
    return pages or [NativePage(text="", elements=[], page_type="FAILED")]


def extract_native_pages(path: Path) -> list[NativePage]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_pages(path)
    if suffix in {".txt", ".md"}:
        return [NativePage(text=path.read_text(errors="ignore"))]
    if suffix == ".docx":
        return extract_docx_pages(path)
    if suffix == ".pptx":
        return extract_pptx_pages(path)
    if suffix in {".xlsx", ".xlsm"}:
        return extract_xlsx_pages(path)
    return [NativePage(text="")]


def extract_text_pages(path: Path) -> list[str]:
    return [page.text for page in extract_native_pages(path)]
