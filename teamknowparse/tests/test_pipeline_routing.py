from pathlib import Path

from teamknowparse.models import Element, PageJson
from teamknowparse.pipeline import PageTask, TeamKnowParsePipeline, _image_rects_for_page


def _task(page_type: str) -> PageTask:
    return PageTask(
        file_path=Path("sample.pdf"),
        relative_path="sample.pdf",
        filename="sample.pdf",
        page_number=1,
        text="sample text",
        page_type=page_type,
    )


def test_txt_mode_skips_visual_only_pages():
    pipeline = TeamKnowParsePipeline()

    assert pipeline._route(_task("SCANNED"), "txt") == "skip"
    assert pipeline._route(_task("VISUAL"), "txt") == "skip"


def test_txt_mode_extracts_text_from_mixed_pages_without_vlm():
    pipeline = TeamKnowParsePipeline()

    assert pipeline._route(_task("MIXED"), "txt") == "digital"
    assert pipeline._route(_task("DIGITAL_PURE_TEXT"), "txt") == "digital"


def test_mixed_mode_routes_visual_pages_to_vlm_and_mixed_pages_to_region_vlm_by_default():
    pipeline = TeamKnowParsePipeline()

    assert pipeline._route(_task("SCANNED"), "mixed") == "vlm"
    assert pipeline._route(_task("VISUAL"), "mixed") == "vlm"
    assert pipeline._route(_task("MIXED"), "mixed") == "both"
    assert pipeline._route(_task("DIGITAL_PURE_TEXT"), "mixed") == "digital"


def test_mixed_mode_can_disable_vlm_visual_enrichment():
    from teamknowparse.config import Settings

    settings = Settings()
    object.__setattr__(settings, "mixed_vlm_enabled", False)
    pipeline = TeamKnowParsePipeline(settings)

    assert pipeline._route(_task("MIXED"), "mixed") == "digital"


def test_mixed_page_merge_drops_generic_native_embedded_image_when_vlm_fails():
    pipeline = TeamKnowParsePipeline()
    native_page = PageJson(
        page_number=8,
        page_type="MIXED",
        elements=[
            Element(type="title", text="Slide Title", level=1),
            Element(type="image", kind="embedded_image", summary="Slide contains an embedded image."),
        ],
        rag_text="Title: Slide Title Image: embedded_image. Slide contains an embedded image.",
    )
    failed_vlm_page = PageJson(page_number=8, page_type="FAILED", elements=[], rag_text="")

    merged = pipeline._merge_page_json(native_page, failed_vlm_page)

    assert [element.type for element in merged.elements] == ["title"]
    assert "embedded image" not in merged.rag_text


def test_mixed_page_merge_keeps_text_and_image_elements_in_one_page_object():
    pipeline = TeamKnowParsePipeline()
    native_page = PageJson(
        page_number=1,
        page_type="MIXED",
        elements=[Element(type="paragraph", text="Direct text from the PDF.")],
        rag_text="Direct text from the PDF.",
    )
    vlm_page = PageJson(
        page_number=1,
        page_type="MIXED",
        elements=[Element(type="image", kind="diagram", summary="A process diagram visible on the page.")],
        rag_text="A process diagram visible on the page.",
    )

    merged = pipeline._merge_page_json(native_page, vlm_page)

    assert merged.page_type == "MIXED"
    assert [element.type for element in merged.elements] == ["paragraph", "image"]
    assert "Direct text from the PDF" in merged.rag_text
    assert "process diagram" in merged.rag_text


def test_mixed_page_merge_replaces_generic_native_embedded_image_with_vlm_detail():
    pipeline = TeamKnowParsePipeline()
    native_page = PageJson(
        page_number=8,
        page_type="MIXED",
        elements=[
            Element(type="title", text="Slide Title", level=1),
            Element(type="image", kind="embedded_image", summary="Slide contains an embedded image."),
        ],
        rag_text="Title: Slide Title Image: embedded_image. Slide contains an embedded image.",
    )
    vlm_page = PageJson(
        page_number=8,
        page_type="MIXED",
        elements=[Element(type="image", kind="photo", summary="Exterior view of a white building with a pergola and trees.")],
        rag_text="Exterior view of a white building with a pergola and trees.",
    )

    merged = pipeline._merge_page_json(native_page, vlm_page)

    assert [element.kind for element in merged.elements if element.type == "image"] == ["photo"]
    assert "Slide contains an embedded image" not in merged.rag_text
    assert "white building" in merged.rag_text


def test_mixed_page_merge_keeps_text_image_and_table_elements():
    pipeline = TeamKnowParsePipeline()
    native_page = PageJson(
        page_number=1,
        page_type="MIXED",
        elements=[Element(type="paragraph", text="Direct text from the PDF.")],
        rag_text="Direct text from the PDF.",
    )
    vlm_page = PageJson(
        page_number=1,
        page_type="MIXED",
        elements=[
            Element(type="image", kind="diagram", summary="A process diagram visible on the page."),
            Element(type="table", table_description="Visual comparison table", markdown="| A | B |\n| --- | --- |"),
        ],
        rag_text="A process diagram visible on the page. | A | B |",
    )

    merged = pipeline._merge_page_json(native_page, vlm_page)

    assert [element.type for element in merged.elements] == ["paragraph", "image", "table"]



def test_vlm_request_concurrency_is_capped_globally(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time

    from teamknowparse.config import Settings

    settings = Settings()
    object.__setattr__(settings, "max_inflight_vlm_batches", 2)
    object.__setattr__(settings, "vlm_batch_size", 4)
    pipeline = TeamKnowParsePipeline(settings)

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_render(task, out_dir, zoom):
        path = tmp_path / f"page-{task.page_number}.png"
        path.write_bytes(b"fake-png")
        return path

    def fake_extract(path, page_type):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return [], "", {"path": str(path)}

    monkeypatch.setattr("teamknowparse.pipeline._render_page_image", fake_render)
    monkeypatch.setattr(pipeline.vlm_client, "_extract_one", fake_extract)

    tasks = [
        PageTask(
            file_path=Path(f"page-{idx}.pdf"),
            relative_path=f"page-{idx}.pdf",
            filename=f"page-{idx}.pdf",
            page_number=idx,
            text="",
            page_type="VISUAL",
        )
        for idx in range(1, 7)
    ]

    asyncio.run(pipeline._run_vlm_tasks(tasks, tmp_path))

    assert max_active == 2


def test_vlm_singleton_batches_still_run_parallel_workers(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time

    from teamknowparse.config import Settings

    settings = Settings()
    object.__setattr__(settings, "max_inflight_vlm_batches", 8)
    object.__setattr__(settings, "vlm_batch_size", 1)
    object.__setattr__(settings, "vlm_workers", 8)
    pipeline = TeamKnowParsePipeline(settings)

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_render(task, out_dir, zoom):
        path = tmp_path / f"page-{task.page_number}.png"
        path.write_bytes(b"fake-png")
        return path

    def fake_extract(path, page_type):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return [], "", {"path": str(path)}

    monkeypatch.setattr("teamknowparse.pipeline._render_page_image", fake_render)
    monkeypatch.setattr(pipeline.vlm_client, "_extract_one", fake_extract)

    tasks = [
        PageTask(
            file_path=Path(f"page-{idx}.pdf"),
            relative_path=f"page-{idx}.pdf",
            filename=f"page-{idx}.pdf",
            page_number=idx,
            text="",
            page_type="VISUAL",
        )
        for idx in range(1, 9)
    ]

    asyncio.run(pipeline._run_vlm_tasks(tasks, tmp_path))

    assert max_active > 1


class _Rect:
    def __init__(self, x0, y0, x1, y1):
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.width = x1 - x0
        self.height = y1 - y0


class _FakeParent:
    def __init__(self, images):
        self._images = images

    def extract_image(self, xref):
        return {"image": self._images[xref]}


class _FakePage:
    def __init__(self):
        self.rect = _Rect(0, 0, 1000, 1000)
        self.parent = _FakeParent({1: b"hero", 2: b"hero", 3: b"tiny", 4: b"header", 5: b"chart"})
        self._rects = {
            1: [_Rect(100, 200, 300, 400)],
            2: [_Rect(320, 200, 520, 400)],
            3: [_Rect(100, 450, 180, 1050)],
            4: [_Rect(100, 0, 600, 100)],
            5: [_Rect(100, 520, 400, 820)],
        }

    def get_images(self, full=True):
        return [(xref,) for xref in self._rects]

    def get_image_rects(self, xref):
        return self._rects[xref]


def test_mixed_vlm_region_filter_drops_tiny_header_footer_and_duplicate_images():
    page = _FakePage()
    seen_hashes: set[str] = set()

    rects = _image_rects_for_page(
        page,
        min_area_ratio=0.04,
        max_regions=2,
        header_footer_band_ratio=0.10,
        zoom=1.0,
        min_crop_width_px=96,
        min_crop_height_px=96,
        seen_hashes=seen_hashes,
    )

    assert rects == [page._rects[5][0], page._rects[1][0]]
    assert len(seen_hashes) == 2
