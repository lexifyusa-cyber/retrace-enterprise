from __future__ import annotations

import os
from dataclasses import dataclass


def _env(names: str | tuple[str, ...]) -> str | None:
    candidates = (names,) if isinstance(names, str) else names
    for name in candidates:
        raw = os.getenv(name)
        if raw is not None and raw != "":
            return raw
    return None


def _int(names: str | tuple[str, ...], default: int) -> int:
    raw = _env(names)
    if raw is None:
        return default
    return int(raw)


def _float(names: str | tuple[str, ...], default: float) -> float:
    raw = _env(names)
    if raw is None:
        return default
    return float(raw)


def _bool(names: str | tuple[str, ...], default: bool) -> bool:
    raw = _env(names)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        raw = os.getenv(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return default


@dataclass(frozen=True)
class Settings:
    digital_workers: int = _int("DIGITAL_WORKERS", 32)
    vlm_workers: int = _int(("TEAMKNOWPARSE_VLM_WORKERS", "VLM_WORKERS"), 10)
    vlm_batch_size: int = _int(("TEAMKNOWPARSE_VLM_BATCH_SIZE", "VLM_BATCH_SIZE"), 1)
    parallel_batching_only: bool = _bool("PARALLEL_BATCHING_ONLY", True)
    digital_queue_max_size: int = _int("DIGITAL_QUEUE_MAX_SIZE", 128)
    vlm_queue_max_size: int = _int("VLM_QUEUE_MAX_SIZE", 32)
    max_inflight_vlm_batches: int = _int(("TEAMKNOWPARSE_MAX_INFLIGHT_VLM_BATCHES", "MAX_INFLIGHT_VLM_BATCHES"), 8)
    vlm_request_timeout_seconds: int = _int(("TEAMKNOWPARSE_VLM_REQUEST_TIMEOUT_SECONDS", "VLM_REQUEST_TIMEOUT_SECONDS"), 45)
    max_retries: int = _int(("TEAMKNOWPARSE_MAX_RETRIES", "MAX_RETRIES"), 1)
    vlm_retry_backoff_seconds: float = _float(("TEAMKNOWPARSE_VLM_RETRY_BACKOFF_SECONDS", "VLM_RETRY_BACKOFF_SECONDS"), 0.75)
    render_zoom: float = _float(("TEAMKNOWPARSE_RENDER_ZOOM", "RENDER_ZOOM"), 1.5)
    mixed_vlm_enabled: bool = _bool(("TEAMKNOWPARSE_MIXED_VLM_ENABLED", "MIXED_VLM_ENABLED"), True)
    mixed_vlm_min_image_area_ratio: float = _float(("TEAMKNOWPARSE_MIXED_VLM_MIN_IMAGE_AREA_RATIO", "MIXED_VLM_MIN_IMAGE_AREA_RATIO"), 0.04)
    mixed_vlm_max_regions_per_page: int = _int(("TEAMKNOWPARSE_MIXED_VLM_MAX_REGIONS_PER_PAGE", "MIXED_VLM_MAX_REGIONS_PER_PAGE"), 2)
    mixed_vlm_header_footer_band_ratio: float = _float(("TEAMKNOWPARSE_MIXED_VLM_HEADER_FOOTER_BAND_RATIO", "MIXED_VLM_HEADER_FOOTER_BAND_RATIO"), 0.10)
    mixed_vlm_min_crop_width_px: int = _int(("TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_WIDTH_PX", "MIXED_VLM_MIN_CROP_WIDTH_PX"), 96)
    mixed_vlm_min_crop_height_px: int = _int(("TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_HEIGHT_PX", "MIXED_VLM_MIN_CROP_HEIGHT_PX"), 96)
    vlm_endpoint: str = _str(("TEAMKNOWPARSE_VLM_ENDPOINT", "VLM_ENDPOINT"))
    vlm_model: str = _str(("TEAMKNOWPARSE_VLM_MODEL", "VLM_MODEL"), "qwen3.5-2b")
    vlm_temperature: float = _float(("TEAMKNOWPARSE_VLM_TEMPERATURE", "VLM_TEMPERATURE"), 0.0)
    vlm_top_p: float = _float(("TEAMKNOWPARSE_VLM_TOP_P", "VLM_TOP_P"), 1.0)
    vlm_top_k: int = _int(("TEAMKNOWPARSE_VLM_TOP_K", "VLM_TOP_K"), 1)
    vlm_max_tokens: int = _int(("TEAMKNOWPARSE_VLM_MAX_TOKENS", "VLM_MAX_TOKENS"), 1024)
    vlm_complex_max_tokens: int = _int(("TEAMKNOWPARSE_VLM_COMPLEX_MAX_TOKENS", "VLM_COMPLEX_MAX_TOKENS"), 2048)
    vlm_min_pixels: int = _int(("TEAMKNOWPARSE_VLM_MIN_PIXELS", "VLM_MIN_PIXELS"), 200704)
    vlm_max_pixels: int = _int(("TEAMKNOWPARSE_VLM_MAX_PIXELS", "VLM_MAX_PIXELS"), 1003520)
    file_desc_min_page_rag_chars: int = _int("FILE_DESC_MIN_PAGE_RAG_CHARS", 300)
    file_desc_max_useful_pages: int = _int("FILE_DESC_MAX_USEFUL_PAGES", 10)

