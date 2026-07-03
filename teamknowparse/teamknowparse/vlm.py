from __future__ import annotations

import base64
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from .models import Element
from .native_extract import rag_text_from_elements


VLM_EXTRACTION_PROMPT = """You are extracting visible document content.
Rules:
- Return valid JSON only.
- Extract only content visible in the image.
- Do not infer missing words, values, topics, dates, or business meaning.
- If text is unreadable, return null for that field or omit it.
- Blank form values must be null.
- Ignore logos, icons, decorative lines, borders, watermarks, page numbers, repeated headers, repeated footers, and layout furniture unless they contain document-specific content.
- Preserve tables as markdown. Every table element must include table_description, which is the table name/title/caption or a concise description of what the table contains.
- Allowed element types: title, section_header, paragraph, list, table, image, key_value_pairs.
- Every image element must include both kind and summary. kind should be a short visual category such as diagram, chart, photo, screenshot, form_image, architecture, flowchart, table_image, or other_visual. summary must describe only what is visible in that image/diagram/chart.
- If the page type hint is MIXED, focus on non-text visual content such as diagrams, figures, charts, pictures, architecture drawings, and visual explanations. Return those as image elements with summaries grounded only in the image. Do not duplicate plain text that native extraction can already read.
- If the page type hint is SCANNED or VISUAL, extract the visible page content into the appropriate element types.
JSON shape:
{"elements":[...],"rag_text":"short retrieval text using only extracted elements"}
"""

VLM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["elements", "rag_text"],
    "properties": {
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["title", "section_header", "paragraph", "list", "table", "image", "key_value_pairs"]},
                    "text": {"type": ["string", "null"]},
                    "level": {"type": ["integer", "null"]},
                    "ordered": {"type": ["boolean", "null"]},
                    "items": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text"],
                            "properties": {
                                "text": {"type": "string"},
                                "level": {"type": ["integer", "null"]},
                            },
                        },
                    },
                    "title": {"type": ["string", "null"]},
                    "pairs": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["key", "value"],
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": ["string", "null"]},
                            },
                        },
                    },
                    "table_description": {"type": ["string", "null"]},
                    "markdown": {"type": ["string", "null"]},
                    "kind": {"type": ["string", "null"]},
                    "summary": {"type": ["string", "null"]},
                },
            },
        },
        "rag_text": {"type": "string"},
    },
}


MIXED_VISUAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["elements", "rag_text"],
    "properties": {
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "kind", "summary"],
                "properties": {
                    "type": {"type": "string", "const": "image"},
                    "kind": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "rag_text": {"type": "string"},
    },
}



JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


class VlmClient:
    def __init__(
        self,
        endpoint: str,
        timeout_seconds: int = 180,
        model: str = "qwen3.5-2b",
        *,
        temperature: float = 0.0,
        top_p: float = 1.0,
        top_k: int = 1,
        max_tokens: int = 1024,
        complex_max_tokens: int = 2048,
        min_pixels: int | None = None,
        max_pixels: int | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.75,
    ) -> None:
        self.endpoint = _normalize_openai_base_url(endpoint)
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.max_tokens = max_tokens
        self.complex_max_tokens = complex_max_tokens
        self.min_pixels = min_pixels
        self.max_pixels = max_pixels
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)


    def health_check(self) -> dict[str, Any]:
        try:
            response = requests.get(f"{self.endpoint}/models", timeout=5)
            response.raise_for_status()
            data = response.json()
            models = [item.get("id") for item in data.get("data", [])]
            return {
                "ok": self.model in models,
                "endpoint": self.endpoint,
                "model": self.model,
                "available_models": models,
            }
        except Exception as exc:
            return {
                "ok": False,
                "endpoint": self.endpoint,
                "model": self.model,
                "available_models": [],
                "error": str(exc),
            }

    def extract_batch(self, image_paths: list[Path], page_types: list[str]) -> list[tuple[list[Element], str, dict[str, Any]]]:
        pairs = list(zip(image_paths, page_types, strict=True))
        if not pairs:
            return []

        def extract(pair: tuple[Path, str]) -> tuple[list[Element], str, dict[str, Any]]:
            path, page_type = pair
            try:
                return self._extract_one(path, page_type)
            except Exception as exc:
                return [], "", {"error": str(exc), "content": ""}

        with ThreadPoolExecutor(max_workers=len(pairs)) as executor:
            return list(executor.map(extract, pairs))

    def _extract_one(self, image_path: Path, page_type: str) -> tuple[list[Element], str, dict[str, Any]]:
        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self._max_tokens_for_page(page_type),
            "top_k": self.top_k,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{VLM_EXTRACTION_PROMPT}\nPage type hint: {page_type}",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                        },
                    ],
                }
            ],
        }
        if self.min_pixels is not None or self.max_pixels is not None:
            payload["mm_processor_kwargs"] = {
                key: value
                for key, value in {
                    "min_pixels": self.min_pixels,
                    "max_pixels": self.max_pixels,
                }.items()
                if value is not None
            }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    f"{self.endpoint}/chat/completions",
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"].get("content", "")
                elements, rag_text = parse_vlm_json(str(content), page_type=page_type)
                internal = {"raw": data, "content": content, "response_format_used": False}
                if attempt:
                    internal["retry_count"] = attempt
                return elements, rag_text, internal
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries or not _retryable_request_error(exc):
                    raise
                delay = self.retry_backoff_seconds * (2 ** attempt) + random.uniform(0, self.retry_backoff_seconds / 2)
                time.sleep(delay)
        raise last_error or RuntimeError("VLM request failed")

    def _max_tokens_for_page(self, page_type: str) -> int:
        if str(page_type).upper() in {"SCANNED", "VISUAL"}:
            return self.complex_max_tokens
        return self.max_tokens


def _retryable_request_error(exc: requests.RequestException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    message = str(exc).lower()
    return "remote end closed connection" in message or "remotedisconnected" in message or "connection aborted" in message



def _normalize_openai_base_url(endpoint: str) -> str:
    cleaned = endpoint.strip().rstrip("/")
    if cleaned.endswith("/v1"):
        return cleaned
    return f"{cleaned}/v1" if cleaned else ""


def _json_text(raw_text: str) -> str:
    text = raw_text.strip()
    match = JSON_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _normalize_table_item(item: dict[str, Any], fallback_text: str = "") -> dict[str, Any]:
    if item.get("type") != "table":
        return item
    description = str(
        item.get("table_description")
        or item.get("title")
        or item.get("name")
        or item.get("caption")
        or fallback_text
        or "Table"
    ).strip()
    normalized = dict(item)
    normalized["table_description"] = description or "Table"
    normalized.pop("name", None)
    normalized.pop("caption", None)
    return normalized


def _normalize_image_item(item: dict[str, Any], fallback_text: str = "") -> dict[str, Any]:
    if item.get("type") != "image":
        return item
    summary = str(
        item.get("summary")
        or item.get("image_description")
        or item.get("text")
        or item.get("description")
        or item.get("caption")
        or fallback_text
        or ""
    ).strip()
    kind = str(item.get("kind") or item.get("image_type") or item.get("category") or "other_visual").strip()
    normalized = dict(item)
    normalized["kind"] = kind or "other_visual"
    normalized["summary"] = summary
    normalized["image_description"] = str(item.get("image_description") or summary).strip()
    normalized.pop("description", None)
    normalized.pop("caption", None)
    if normalized.get("text") == normalized["summary"]:
        normalized.pop("text", None)
    return normalized


def _repair_element_item(item: dict[str, Any], fallback_text: str = "", page_type: str | None = None) -> dict[str, Any] | None:
    repaired = dict(item)
    if "type" not in repaired and "element_type" in repaired:
        repaired["type"] = repaired.pop("element_type")
    if repaired.get("type") == "heading":
        repaired["type"] = "section_header"
    if repaired.get("type") in {"figure", "diagram", "chart", "photo", "screenshot"}:
        repaired["kind"] = repaired.get("kind") or repaired.get("type")
        repaired["type"] = "image"
    if "content" in repaired:
        if repaired.get("type") == "image":
            repaired["summary"] = repaired.get("summary") or repaired["content"]
        elif repaired.get("type") in {"title", "section_header", "paragraph"}:
            repaired["text"] = repaired.get("text") or repaired["content"]
        repaired.pop("content", None)
    if "description" in repaired and repaired.get("type") == "table":
        repaired["table_description"] = repaired.get("table_description") or repaired["description"]
    if "summary" in repaired and "type" not in repaired:
        repaired["type"] = "image"
    repaired = _normalize_table_item(repaired, fallback_text=fallback_text)
    repaired = _normalize_image_item(repaired, fallback_text=fallback_text)
    if page_type == "MIXED" and repaired.get("type") not in {"image", "table"}:
        return None
    return repaired


VISUAL_SUMMARY_RE = re.compile(
    r"\b(diagram|architecture|figure|chart|graph|flow|flowchart|block|component|sensor|cloud|gateway|service|module|image|visual|iot level)\b",
    re.IGNORECASE,
)


def _field_value(text: str, field: str) -> str | None:
    match = re.search(rf'"{field}"\s*:\s*"((?:\\.|[^"\\])*)"', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except Exception:
        return match.group(1).replace('\\"', '"')


def _salvage_vlm_data(raw_text: str) -> dict[str, Any]:
    text = _json_text(raw_text)
    objects = re.findall(r"\{[^{}]*\}", text, flags=re.DOTALL)
    elements: list[dict[str, Any]] = []
    for obj in objects:
        item: dict[str, Any] = {}
        for field in [
            "type",
            "element_type",
            "kind",
            "image_type",
            "category",
            "summary",
            "image_description",
            "description",
            "caption",
            "content",
            "text",
            "title",
            "markdown",
            "table_description",
        ]:
            value = _field_value(obj, field)
            if value is not None:
                item[field] = value
        if item:
            elements.append(item)
    return {"elements": elements, "rag_text": _field_value(text, "rag_text") or ""}


def _mixed_visual_fallback(raw_elements: list[dict[str, Any]], fallback_text: str) -> list[dict[str, Any]]:
    summaries: list[str] = []
    kinds: list[str] = []
    for item in raw_elements:
        item_type = str(item.get("type") or item.get("element_type") or "").lower()
        text = " ".join(
            str(item.get(field) or "")
            for field in ["summary", "description", "caption", "content", "text", "title"]
        ).strip()
        if item_type in {"image", "figure", "diagram", "chart", "photo", "screenshot"}:
            kinds.append(item_type if item_type != "image" else str(item.get("kind") or "other_visual"))
            if text:
                summaries.append(text)
            continue
        if text and VISUAL_SUMMARY_RE.search(text):
            summaries.append(text)
    if not summaries and fallback_text and VISUAL_SUMMARY_RE.search(fallback_text):
        summaries.append(fallback_text)
    if not summaries:
        return []
    deduped = list(dict.fromkeys(part.strip() for part in summaries if part.strip()))
    summary = " ".join(deduped)
    kind = next((kind for kind in kinds if kind and kind not in {"image", "other_visual"}), "diagram")
    return [{"type": "image", "kind": kind, "summary": summary}]


def parse_vlm_json(raw_text: str, page_type: str | None = None) -> tuple[list[Element], str]:
    try:
        data = json.loads(_json_text(raw_text))
    except json.JSONDecodeError:
        data = _salvage_vlm_data(raw_text)
    fallback_text = str(data.get("rag_text") or "")
    source_elements = [item for item in data.get("elements", []) if isinstance(item, dict)]
    raw_elements = []
    for item in source_elements:
        repaired = _repair_element_item(item, fallback_text=fallback_text, page_type=page_type)
        if repaired is not None:
            raw_elements.append(repaired)
    if page_type == "MIXED" and not raw_elements:
        raw_elements = _mixed_visual_fallback(source_elements, fallback_text)
    elements: list[Element] = []
    for item in raw_elements:
        try:
            elements.append(Element.model_validate(item))
        except Exception:
            continue
    rag_text = rag_text_from_elements(elements)
    if not rag_text and page_type != "MIXED":
        rag_text = str(data.get("rag_text") or "")
    return elements, rag_text
