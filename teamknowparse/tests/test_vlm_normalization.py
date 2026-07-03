from teamknowparse.vlm import parse_vlm_json


def test_vlm_table_description_is_normalized_from_title():
    elements, rag_text = parse_vlm_json(
        '{"elements":[{"type":"table","title":"Cost Summary","markdown":"| A | B |\\n| --- | --- |\\n| 1 | 2 |"}]}'
    )

    assert elements[0].type == "table"
    assert elements[0].table_description == "Cost Summary"
    assert "| A | B |" in elements[0].markdown


def test_vlm_image_kind_and_summary_are_normalized():
    elements, rag_text = parse_vlm_json(
        '{"elements":[{"type":"image","image_type":"diagram","caption":"A visible architecture diagram."}]}'
    )

    assert elements[0].type == "image"
    assert elements[0].kind == "diagram"
    assert elements[0].summary == "A visible architecture diagram."
    assert elements[0].image_description == "A visible architecture diagram."
    assert "A visible architecture diagram" in rag_text


def test_vlm_image_description_is_used_as_summary():
    elements, rag_text = parse_vlm_json(
        '{"elements":[{"type":"image","kind":"diagram","image_description":"A sensor gateway flow diagram."}]}'
    )

    assert elements[0].summary == "A sensor gateway flow diagram."
    assert elements[0].image_description == "A sensor gateway flow diagram."
    assert "sensor gateway" in rag_text


def test_vlm_repairs_common_bad_keys():
    elements, rag_text = parse_vlm_json(
        '{"elements":[{"element_type":"heading","content":"Overview"},{"element_type":"paragraph","content":"Visible only."}]}'
    )

    assert [element.type for element in elements] == ["section_header", "paragraph"]
    assert elements[0].text == "Overview"
    assert "Visible only" in rag_text


def test_mixed_vlm_keeps_only_visual_elements():
    elements, rag_text = parse_vlm_json(
        '{"elements":[{"type":"paragraph","text":"Duplicate readable text"},{"type":"diagram","content":"A visible process diagram."}]}',
        page_type="MIXED",
    )

    assert len(elements) == 1
    assert elements[0].type == "image"
    assert elements[0].kind == "diagram"
    assert "process diagram" in elements[0].summary
    assert "Duplicate" not in rag_text


def test_mixed_vlm_converts_diagram_text_output_to_image():
    elements, rag_text = parse_vlm_json(
        '{"elements":[{"type":"title","summary":"IoT Level-1 System Architecture Diagram","description":"Diagram showing app, services, storage, and IoT device components."}]}',
        page_type="MIXED",
    )

    assert len(elements) == 1
    assert elements[0].type == "image"
    assert elements[0].kind == "diagram"
    assert "Architecture Diagram" in elements[0].summary


def test_malformed_mixed_vlm_output_is_salvaged_as_image():
    raw = '```json\n{\n  "elements": [\n    {\n      "element_type": "title",\n      "summary": "IoT architecture diagram",\n      "description": "A block diagram with sensors, gateway, cloud service and application modules."\n    }\n  ],\n  "rag_text": "Architecture diagram showing sensors connected to a gateway and cloud service."\n'
    elements, rag_text = parse_vlm_json(raw, page_type="MIXED")

    assert len(elements) == 1
    assert elements[0].type == "image"
    assert "gateway" in elements[0].summary


def test_mixed_vlm_keeps_visual_table_and_rejects_text():
    elements, rag_text = parse_vlm_json(
        '{"elements":[{"type":"paragraph","text":"Duplicate native text"},{"type":"table","title":"M2M vs IoT comparison","markdown":"| M2M | IoT |\n| --- | --- |\n| Proprietary | IP based |"}]}',
        page_type="MIXED",
    )

    assert [element.type for element in elements] == ["table"]
    assert elements[0].table_description == "M2M vs IoT comparison"
    assert "Proprietary" in elements[0].markdown
    assert "Duplicate native text" not in rag_text


def test_vlm_client_normalizes_base_url_and_payload(monkeypatch, tmp_path):
    from teamknowparse.vlm import VlmClient

    image = tmp_path / "page.png"
    image.write_bytes(b"fake-png")
    calls = []

    class Response:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def fake_get(url, timeout):
        calls.append(("get", url, timeout))
        return Response({"data": [{"id": "qwen3.5-2b"}]})

    def fake_post(url, json, timeout):
        calls.append(("post", url, json, timeout))
        return Response({"choices": [{"message": {"content": "{\"elements\": [], \"rag_text\": \"\"}"}}]})

    monkeypatch.setattr("teamknowparse.vlm.requests.get", fake_get)
    monkeypatch.setattr("teamknowparse.vlm.requests.post", fake_post)

    client = VlmClient(
        "http://10.0.0.51:18017/v1/",
        model="qwen3.5-2b",
        temperature=0,
        top_p=1,
        top_k=1,
        max_tokens=1024,
        complex_max_tokens=2048,
        min_pixels=200704,
        max_pixels=1003520,
    )

    assert client.health_check()["ok"] is True
    client.extract_batch([image], ["VISUAL"])

    assert calls[0] == ("get", "http://10.0.0.51:18017/v1/models", 5)
    assert calls[1][0] == "post"
    assert calls[1][1] == "http://10.0.0.51:18017/v1/chat/completions"
    payload = calls[1][2]
    assert payload["model"] == "qwen3.5-2b"
    assert payload["temperature"] == 0
    assert payload["top_p"] == 1
    assert payload["top_k"] == 1
    assert payload["max_tokens"] == 2048
    assert payload["mm_processor_kwargs"] == {
        "min_pixels": 200704,
        "max_pixels": 1003520,
    }


def test_vlm_client_uses_standard_token_limit_for_mixed_pages(tmp_path, monkeypatch):
    from teamknowparse.vlm import VlmClient

    image = tmp_path / "page.png"
    image.write_bytes(b"fake-png")
    payloads = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{\"elements\": [], \"rag_text\": \"\"}"}}]}

    def fake_post(url, json, timeout):
        payloads.append(json)
        return Response()

    monkeypatch.setattr("teamknowparse.vlm.requests.post", fake_post)

    client = VlmClient("http://10.0.0.51:18017", max_tokens=1024, complex_max_tokens=2048)
    client.extract_batch([image], ["MIXED"])

    assert payloads[0]["max_tokens"] == 1024





def test_vlm_client_extract_batch_runs_requests_in_parallel(tmp_path, monkeypatch):
    import threading
    import time

    from teamknowparse.vlm import VlmClient

    images = []
    for idx in range(4):
        image = tmp_path / f"page-{idx}.png"
        image.write_bytes(b"fake-png")
        images.append(image)

    client = VlmClient("http://10.0.0.51:18017")
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_extract(path, page_type):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return [], "", {"path": str(path)}

    monkeypatch.setattr(client, "_extract_one", fake_extract)

    results = client.extract_batch(images, ["VISUAL"] * len(images))

    assert len(results) == len(images)
    assert max_active > 1

def test_vlm_client_retries_remote_disconnect(monkeypatch, tmp_path):
    import requests

    from teamknowparse.vlm import VlmClient

    image = tmp_path / "page.png"
    image.write_bytes(b"fake-png")
    calls = 0
    sleeps = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{\"elements\": [], \"rag_text\": \"\"}"}}]}

    def fake_post(url, json, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.ConnectionError("Remote end closed connection without response")
        return Response()

    monkeypatch.setattr("teamknowparse.vlm.requests.post", fake_post)
    monkeypatch.setattr("teamknowparse.vlm.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("teamknowparse.vlm.random.uniform", lambda low, high: 0)

    client = VlmClient("http://10.0.0.51:18017", max_retries=2, retry_backoff_seconds=0.75)
    _elements, _rag_text, internal = client._extract_one(image, "MIXED")

    assert calls == 2
    assert sleeps == [0.75]
    assert internal["retry_count"] == 1


def test_chunks_preserve_image_description_metadata(tmp_path):
    import json

    from teamknowparse.models import Element, FileJson, FolderFile, FolderJson, PageJson
    from teamknowparse.pipeline import TeamKnowParsePipeline, _safe_stem

    run_dir = tmp_path / "run"
    (run_dir / "files").mkdir(parents=True)
    file_json = FileJson(
        filename="diagram.pdf",
        file_path="diagram.pdf",
        page_count=1,
        pages=[
            PageJson(
                page_number=1,
                page_type="MIXED",
                elements=[Element(type="image", kind="diagram", summary="A labelled IoT gateway diagram.", image_description="A labelled IoT gateway diagram.")],
                rag_text="Image: diagram. A labelled IoT gateway diagram.",
            )
        ],
    )
    (run_dir / "files" / f"{_safe_stem(file_json.file_path)}.json").write_text(file_json.model_dump_json(), encoding="utf-8")

    folder_json = FolderJson(
        folder_name="docs",
        folder_path="docs",
        folder_description="docs",
        total_file_count=1,
        files=[FolderFile(filename="diagram.pdf", file_path="diagram.pdf", page_count=1, tags=[], file_description="diagram")],
    )

    TeamKnowParsePipeline()._write_chunks(run_dir, folder_json)

    payload = json.loads((run_dir / "chunks.jsonl").read_text(encoding="utf-8"))
    assert payload["image_descriptions"] == [{
        "kind": "diagram",
        "summary": "A labelled IoT gateway diagram.",
        "image_description": "A labelled IoT gateway diagram.",
    }]
    assert payload["elements"][0]["image_description"] == "A labelled IoT gateway diagram."
