from sqlalchemy.dialects import postgresql, sqlite

from app.api.agent import _should_prefetch_kb_evidence
from app.db.database import (
    database_backend,
    is_postgresql_database_url,
    is_sqlite_database_url,
    normalize_database_url,
    sqlite_db_path_from_url,
)
from app.db.expressions import json_bool_true
from app.models.team import Team
from app.services.llm_gateway import LLMGateway


def test_database_url_normalization_supports_common_postgres_forms():
    assert normalize_database_url("postgres://u:p@host/db") == "postgresql+asyncpg://u:p@host/db"
    assert normalize_database_url("postgresql://u:p@host/db") == "postgresql+asyncpg://u:p@host/db"
    assert normalize_database_url("postgresql+asyncpg://u:p@host/db") == "postgresql+asyncpg://u:p@host/db"


def test_database_backend_detection():
    assert database_backend("sqlite+aiosqlite:///./retrace.db") == "sqlite"
    assert is_sqlite_database_url("sqlite+aiosqlite:///./retrace.db") is True
    assert is_postgresql_database_url("postgres://u:p@host/db") is True


def test_sqlite_path_rejects_non_sqlite_url():
    try:
        sqlite_db_path_from_url("postgresql+asyncpg://u:p@host/db")
    except ValueError as exc:
        assert "not a SQLite URL" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_json_bool_true_compiles_for_sqlite_and_postgresql():
    expr = json_bool_true(Team.metadata_json, "is_prebuilt")
    sqlite_sql = str(expr.compile(dialect=sqlite.dialect()))
    postgres_sql = str(expr.compile(dialect=postgresql.dialect()))

    assert "JSON_EXTRACT" in sqlite_sql or "JSON_QUOTE" in sqlite_sql
    assert "json_extract" not in postgres_sql.lower()
    assert "->" in postgres_sql or "[" in postgres_sql


def test_kb_prefetch_skips_web_actions_unless_kb_explicit():
    assert _should_prefetch_kb_evidence("") is False
    assert _should_prefetch_kb_evidence("open youtube and search for demos") is False
    assert _should_prefetch_kb_evidence("search the web for pricing") is False
    assert _should_prefetch_kb_evidence("search the knowledge base for camera mode") is True
    assert _should_prefetch_kb_evidence("what does the uploaded manual say about cameras?") is True


def test_llm_gateway_does_not_use_custom_chat_endpoint_for_embeddings_when_embedding_url_unset():
    gateway = LLMGateway.from_settings({
        "provider": "custom",
        "api_url": " http://gateway.lumenatech.ai/v1/ ",
        "api_key": "local-chat-key",
        "model_name": "Chitti-Fast",
        "embedding_model": "qwen3-embedding-4b",
    })

    assert gateway.config.chat_base_url == "http://gateway.lumenatech.ai/v1"
    assert gateway.config.chat_api_key == "local-chat-key"
    assert gateway.config.chat_model == "Chitti-Fast"
    assert gateway.config.embedding_base_url != gateway.config.chat_base_url
    assert gateway.config.embedding_api_key != gateway.config.chat_api_key
    assert gateway.config.embedding_model == "qwen3-embedding-4b"


def test_llm_gateway_keeps_explicit_embedding_endpoint_separate_from_chat_endpoint():
    gateway = LLMGateway.from_settings({
        "provider": "custom",
        "api_url": "http://chat.local/v1",
        "api_key": "chat-key",
        "model_name": "local-chat-model",
        "embedding_base_url": "http://embed.local/v1",
        "embedding_api_key": "embed-key",
        "embedding_model": "qwen3-embedding-4b",
    })

    assert gateway.config.chat_base_url == "http://chat.local/v1"
    assert gateway.config.chat_api_key == "chat-key"
    assert gateway.config.embedding_base_url == "http://embed.local/v1"
    assert gateway.config.embedding_api_key == "embed-key"
    assert gateway.config.embedding_model == "qwen3-embedding-4b"


def test_upload_relative_paths_preserve_folders_and_files():
    from app.api.teams import _safe_upload_relative_path

    assert str(_safe_upload_relative_path("docs/guide/readme.md", "readme.md")) == "docs/guide/readme.md"
    assert str(_safe_upload_relative_path("", "single.pdf")) == "single.pdf"


def test_upload_relative_paths_reject_traversal():
    from app.api.teams import _safe_upload_relative_path

    for value in ["../secret.txt", "docs/../../secret.txt", "docs/../secret.txt"]:
        try:
            _safe_upload_relative_path(value, "secret.txt")
        except Exception as exc:
            assert "Unsafe upload path" in str(exc)
        else:
            raise AssertionError(f"expected unsafe path rejection for {value}")


def test_dimension_collection_name_scopes_chroma_by_embedding_size():
    from app.services.teamknowparse_vector import _dimension_collection_name

    assert _dimension_collection_name("teamknowparse_kb_chunks", 1536) == "teamknowparse_kb_chunks_d1536"
    assert _dimension_collection_name("teamknowparse_kb_chunks", 2560) == "teamknowparse_kb_chunks_d2560"
    assert _dimension_collection_name("teamknowparse_kb_chunks_d2560", 2560) == "teamknowparse_kb_chunks_d2560"
    assert _dimension_collection_name("bad name!", 2560) == "bad_name_d2560"


def test_dimension_collection_name_keeps_base_when_dimension_unknown():
    from app.services.teamknowparse_vector import _dimension_collection_name

    assert _dimension_collection_name("teamknowparse_kb_chunks", None) == "teamknowparse_kb_chunks"
    assert _dimension_collection_name("teamknowparse_kb_chunks", 0) == "teamknowparse_kb_chunks"


def test_embedding_throughput_defaults_are_configured():
    from app.core.config import settings

    assert settings.TEAMKNOWPARSE_EMBEDDING_BATCH_SIZE == 96
    assert settings.TEAMKNOWPARSE_EMBEDDING_CONCURRENCY == 4
    assert settings.TEAMKNOWPARSE_EMBEDDING_SUBCHUNK_CHARS == 1600
    assert settings.TEAMKNOWPARSE_EMBEDDING_SUBCHUNK_OVERLAP == 160
    assert settings.TEAMKNOWPARSE_EMBEDDING_BATCH_MAX_CHARS == 120000


def test_teamknowparse_runner_maps_vlm_settings_to_subprocess_env(monkeypatch):
    from app.core.config import settings
    from app.services.teamknowparse_pipeline import TeamKnowParsePipelineRunner

    monkeypatch.setattr(settings, "TEAMKNOWPARSE_VLM_ENDPOINT", " http://10.0.0.51:18017/v1/ ")
    monkeypatch.setattr(settings, "TEAMKNOWPARSE_VLM_MODEL", " qwen3.5-2b ")

    env = TeamKnowParsePipelineRunner()._subprocess_env()

    assert env["VLM_ENDPOINT"] == "http://10.0.0.51:18017/v1"
    assert env["VLM_MODEL"] == "qwen3.5-2b"
    assert env["TEAMKNOWPARSE_VLM_ENDPOINT"] == "http://10.0.0.51:18017/v1"
    assert env["TEAMKNOWPARSE_VLM_MODEL"] == "qwen3.5-2b"
    assert env["VLM_WORKERS"] == "10"
    assert env["VLM_BATCH_SIZE"] == "1"
    assert env["MAX_INFLIGHT_VLM_BATCHES"] == "8"
    assert env["VLM_REQUEST_TIMEOUT_SECONDS"] == "45"
    assert env["MIXED_VLM_ENABLED"] == "true"
    assert env["TEAMKNOWPARSE_VLM_WORKERS"] == "10"
    assert env["TEAMKNOWPARSE_VLM_BATCH_SIZE"] == "1"
    assert env["TEAMKNOWPARSE_MAX_INFLIGHT_VLM_BATCHES"] == "8"
    assert env["TEAMKNOWPARSE_VLM_REQUEST_TIMEOUT_SECONDS"] == "45"
    assert env["TEAMKNOWPARSE_MIXED_VLM_ENABLED"] == "true"
    assert env["MIXED_VLM_MIN_IMAGE_AREA_RATIO"] == "0.04"
    assert env["MIXED_VLM_MAX_REGIONS_PER_PAGE"] == "2"
    assert env["MIXED_VLM_HEADER_FOOTER_BAND_RATIO"] == "0.1"
    assert env["TEAMKNOWPARSE_MIXED_VLM_MIN_IMAGE_AREA_RATIO"] == "0.04"
    assert env["TEAMKNOWPARSE_MIXED_VLM_MAX_REGIONS_PER_PAGE"] == "2"
    assert env["VLM_MIN_PIXELS"] == "200704"
    assert env["VLM_MAX_PIXELS"] == "1003520"
    assert env["TEAMKNOWPARSE_VLM_MIN_PIXELS"] == "200704"
    assert env["TEAMKNOWPARSE_VLM_MAX_PIXELS"] == "1003520"
    assert env["RENDER_ZOOM"] == "1.5"
    assert env["TEAMKNOWPARSE_RENDER_ZOOM"] == "1.5"
    assert env["TEAMKNOWPARSE_MIXED_VLM_HEADER_FOOTER_BAND_RATIO"] == "0.1"
    assert env["MIXED_VLM_MIN_CROP_WIDTH_PX"] == "96"
    assert env["MIXED_VLM_MIN_CROP_HEIGHT_PX"] == "96"
    assert env["TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_WIDTH_PX"] == "96"
    assert env["TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_HEIGHT_PX"] == "96"
    assert env["VLM_TEMPERATURE"] == "0.0"
    assert env["VLM_TOP_P"] == "1.0"
    assert env["VLM_TOP_K"] == "1"
    assert env["VLM_MAX_TOKENS"] == "1024"
    assert env["VLM_COMPLEX_MAX_TOKENS"] == "2048"
    assert env["MAX_RETRIES"] == "1"
    assert env["TEAMKNOWPARSE_MAX_RETRIES"] == "1"
    assert env["VLM_RETRY_BACKOFF_SECONDS"] == "0.75"
    assert env["TEAMKNOWPARSE_VLM_RETRY_BACKOFF_SECONDS"] == "0.75"


def test_embedding_text_is_split_into_parented_subchunks():
    from app.services.teamknowparse_vector import _embedding_units_for_chunk

    chunk = {
        "chunk_id": "chunk-1",
        "team_id": "team-1",
        "search_text": " ".join(["word"] * 900),
        "file_path": "docs/manual.pdf",
        "filename": "manual.pdf",
        "page_number": 1,
        "page_type": "text",
        "source_run_id": "run-1",
    }

    units = _embedding_units_for_chunk(chunk, max_chars=1000, overlap_chars=100)

    assert len(units) > 1
    assert units[0]["vector_id"] == "chunk-1::part0"
    assert units[1]["vector_id"] == "chunk-1::part1"
    assert {unit["parent_chunk_id"] for unit in units} == {"chunk-1"}
    assert all(len(unit["embedding_text"]) <= 1000 for unit in units)
    assert all(unit["embedding_part_count"] == len(units) for unit in units)


def test_single_embedding_unit_keeps_chunk_id_for_backward_compatibility():
    from app.services.teamknowparse_vector import _embedding_units_for_chunk

    chunk = {
        "chunk_id": "chunk-2",
        "team_id": "team-1",
        "search_text": "short text",
        "file_path": "docs/manual.pdf",
        "filename": "manual.pdf",
        "page_number": 1,
        "page_type": "text",
        "source_run_id": "run-1",
    }

    units = _embedding_units_for_chunk(chunk, max_chars=1000, overlap_chars=100)

    assert len(units) == 1
    assert units[0]["vector_id"] == "chunk-2"
    assert units[0]["parent_chunk_id"] == "chunk-2"


def test_vector_part_ids_map_back_to_parent_chunk_id():
    from app.services.teamknowparse_vector import _parent_chunk_id

    assert _parent_chunk_id("chunk-1::part3", {}) == "chunk-1"
    assert _parent_chunk_id("vector-id", {"parent_chunk_id": "chunk-9"}) == "chunk-9"


def test_embedding_unit_batches_respect_count_and_char_budget():
    from app.services.teamknowparse_vector import _batch_embedding_units

    units = [{"embedding_text": "x" * 6000} for _ in range(5)]
    batches = _batch_embedding_units(units, max_count=3, max_chars=10_000)

    assert [len(batch) for batch in batches] == [1, 1, 1, 1, 1]
    assert all(sum(len(unit["embedding_text"]) for unit in batch) <= 10_000 for batch in batches)


def test_embedding_batch_failure_returns_exact_parent_error(monkeypatch):
    import asyncio

    from app.services.teamknowparse_vector import TeamKnowParseVectorService

    service = TeamKnowParseVectorService()
    marked = []

    async def fail_embed(texts, cfg):
        raise RuntimeError(f"embedding endpoint rejected {texts[0]}")

    async def mark_failed(batch, error_text):
        marked.append(([item["chunk_id"] for item in batch], error_text))

    monkeypatch.setattr(service, "_embed_with_config", fail_embed)
    monkeypatch.setattr(service, "_mark_batch_failed", mark_failed)

    units = [{
        "parent_chunk_id": "chunk-1",
        "chunk_id": "chunk-1",
        "vector_id": "chunk-1",
        "embedding_text": "bad payload",
        "team_id": "team-1",
        "file_path": "doc.pdf",
        "filename": "doc.pdf",
        "page_number": 1,
        "page_type": "TABLE",
        "source_run_id": "run-1",
        "embedding_part_index": 0,
        "embedding_part_count": 1,
        "embedding_char_start": 0,
        "embedding_char_end": 11,
    }]

    indexed, failed_errors = asyncio.run(service._index_unit_batch_resilient("team-1", units, {}, max_attempts=1))

    assert indexed == []
    assert failed_errors == {"chunk-1": "embedding endpoint rejected bad payload"}
    assert marked == [(["chunk-1"], "embedding endpoint rejected bad payload")]
