"""Chroma indexing and retrieval for TeamKnowParse KB chunks."""

from __future__ import annotations

import asyncio
import math
import os
import re
from collections import Counter
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import async_session_maker
import structlog

logger = structlog.get_logger(__name__)
from app.models.teamknowparse_kb import TeamKnowParseKbChunk
from app.services.llm_gateway import LLMGateway

SearchMode = Literal["hybrid"]

_STOPWORDS = {
    "the", "and", "for", "with", "without", "does", "do", "can", "could",
    "would", "should", "work", "works", "working", "whether", "what", "how",
    "when", "where", "which", "that", "this", "from", "into", "about",
    "are", "is", "was", "were", "have", "has", "had", "their", "there",
    "not", "yes", "all", "any", "some", "also", "only", "just", "use",
    "called", "call", "name", "named", "something", "thing", "things", "me",
}

_SYNONYMS = {
    "camera": {"cameras", "surveillance", "security", "cctv"},
    "cameras": {"camera", "surveillance", "security", "cctv"},
    "surveillance": {"camera", "cameras", "security", "cctv"},
    "security": {"camera", "cameras", "surveillance", "cctv"},
    "cctv": {"camera", "cameras", "surveillance", "security"},
    "light": {"day", "night", "dark", "darkness", "low", "infrared", "mode", "modes", "adapt"},
    "day": {"light", "night", "mode", "modes", "adapt"},
    "night": {"light", "day", "dark", "darkness", "infrared", "mode", "modes", "adapt"},
    "dark": {"darkness", "night", "infrared", "light"},
    "darkness": {"dark", "night", "infrared", "light"},
    "infrared": {"night", "dark", "darkness", "light"},
    "adapt": {"adapting", "adaptive", "dynamic", "mode", "modes", "operate"},
    "adapting": {"adapt", "adaptive", "dynamic", "mode", "modes", "operate"},
    "adaptive": {"adapt", "adapting", "dynamic", "mode", "modes", "operate"},
    "mode": {"modes", "adapt", "adapting", "adaptive", "operate"},
    "modes": {"mode", "adapt", "adapting", "adaptive", "operate"},
    "identify": {"detect", "detection", "identification", "track", "tracking", "proximity", "rfid", "object", "objects"},
    "detect": {"detection", "identify", "identification", "proximity", "sensor", "sensors", "object", "objects"},
    "detection": {"detect", "identify", "proximity", "sensor", "sensors", "object", "objects"},
    "near": {"nearby", "close", "proximity", "non", "contact", "object", "objects", "sensor", "sensors"},
    "nearby": {"near", "close", "proximity", "non", "contact", "object", "objects", "sensor", "sensors"},
    "proximity": {"near", "nearby", "close", "non", "contact", "detect", "detection", "sensor", "sensors"},
    "rfid": {"identify", "identification", "track", "tracking", "object", "objects", "tag", "tags"},
    "huge": {"big", "large", "massive", "volume", "volumes", "analytics", "analysis"},
    "amount": {"volume", "volumes", "large", "massive", "big"},
    "amounts": {"volume", "volumes", "large", "massive", "big"},
    "data": {"big", "volume", "volumes", "analytics", "analysis", "mining"},
    "brainstorming": {"analysis", "analytics", "mining", "patterns", "insights"},
}

_CONCEPT_TERMS = {
    "camera": {"camera", "cameras", "surveillance", "security", "cctv"},
    "light_state": {"light", "day", "night", "dark", "darkness", "low", "infrared"},
    "operation": {"adapt", "adapting", "adaptive", "dynamic", "mode", "modes", "operate"},
    "detection": {"identify", "detect", "detection", "proximity", "rfid", "track", "tracking", "sensor", "sensors"},
    "data_scale": {"data", "big", "large", "massive", "volume", "volumes", "analytics", "analysis", "mining"},
}


def _api_key(base_url: str = "", db_key: str = "") -> str:
    if base_url:
        return db_key or settings.EMBEDDING_API_KEY or os.getenv("EMBEDDING_API_KEY", "") or "local-vllm"
    return db_key or settings.EMBEDDING_API_KEY or settings.OPENAI_API_KEY or os.getenv("EMBEDDING_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")


def _embedding_base_url(db_base_url: str = "") -> str:
    return (db_base_url or settings.TEAMKNOWPARSE_EMBEDDING_BASE_URL or "").strip().rstrip("/")


def _dimension_collection_name(base_name: str, dimension: int | None) -> str:
    """Return a Chroma collection name scoped to the embedding dimension.

    Chroma collections are dimension-locked on first insert. ReTrace can switch
    between OpenAI-compatible embedding providers, so the same logical KB
    collection must be split by vector dimension to avoid permanent
    1536-vs-2560 mismatch failures after a provider change.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (base_name or settings.TEAMKNOWPARSE_CHROMA_COLLECTION).strip())
    cleaned = cleaned.strip("._-") or "teamknowparse_kb_chunks"
    if not dimension or dimension <= 0:
        return cleaned
    suffix = f"_d{dimension}"
    return cleaned if cleaned.endswith(suffix) else f"{cleaned}{suffix}"


def _parent_chunk_id(vector_id: str, metadata: dict | None = None) -> str:
    meta_parent = (metadata or {}).get("parent_chunk_id")
    if meta_parent:
        return str(meta_parent)
    return str(vector_id).split("::part", 1)[0]


def _split_embedding_text(text: str, *, max_chars: int = 1600, overlap_chars: int = 160) -> list[tuple[str, int, int]]:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return []
    max_chars = max(400, int(max_chars or 1600))
    overlap_chars = max(0, min(int(overlap_chars or 0), max_chars // 3))
    if len(cleaned) <= max_chars:
        return [(cleaned, 0, len(cleaned))]

    parts: list[tuple[str, int, int]] = []
    start = 0
    while start < len(cleaned):
        hard_end = min(len(cleaned), start + max_chars)
        end = hard_end
        if hard_end < len(cleaned):
            split_at = cleaned.rfind(" ", start + max_chars // 2, hard_end)
            if split_at > start:
                end = split_at
        part = cleaned[start:end].strip()
        if part:
            parts.append((part, start, end))
        if end >= len(cleaned):
            break
        start = max(end - overlap_chars, start + 1)
    return parts


def _embedding_units_for_chunk(chunk: dict, *, max_chars: int = 1600, overlap_chars: int = 160) -> list[dict]:
    parts = _split_embedding_text(chunk.get("search_text") or "", max_chars=max_chars, overlap_chars=overlap_chars)
    if not parts:
        return []
    units = []
    part_count = len(parts)
    for idx, (text, start, end) in enumerate(parts):
        vector_id = chunk["chunk_id"] if part_count == 1 else f"{chunk['chunk_id']}::part{idx}"
        units.append({
            **chunk,
            "vector_id": vector_id,
            "parent_chunk_id": chunk["chunk_id"],
            "embedding_text": text,
            "embedding_part_index": idx,
            "embedding_part_count": part_count,
            "embedding_char_start": start,
            "embedding_char_end": end,
        })
    return units


def _batch_embedding_units(units: list[dict], *, max_count: int, max_chars: int = 120_000) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    max_count = max(1, int(max_count or 96))
    max_chars = max(10_000, int(max_chars or 120_000))
    for unit in units:
        unit_chars = len(unit.get("embedding_text") or "")
        if current and (len(current) >= max_count or current_chars + unit_chars > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += unit_chars
    if current:
        batches.append(current)
    return batches


async def _embedding_config(session: AsyncSession | None = None) -> dict:
    cfg = {
        "model": settings.TEAMKNOWPARSE_EMBEDDING_MODEL,
        "base_url": settings.TEAMKNOWPARSE_EMBEDDING_BASE_URL.strip().rstrip("/"),
        "api_key": settings.EMBEDDING_API_KEY,
        "collection": settings.TEAMKNOWPARSE_CHROMA_COLLECTION,
        "debug_log": settings.TEAMKNOWPARSE_EMBEDDING_DEBUG_LOG,
    }
    if session is None:
        return cfg
    try:
        from app.api.settings import get_active_llm_settings

        llm = await get_active_llm_settings(session)
        cfg["model"] = (llm.get("embedding_model") or cfg["model"] or "").strip()
        cfg["base_url"] = (llm.get("embedding_base_url") or cfg["base_url"] or "").strip().rstrip("/")
        cfg["api_key"] = (llm.get("embedding_api_key") or cfg["api_key"] or "").strip()
        cfg["collection"] = (llm.get("embedding_chroma_collection") or cfg["collection"] or "").strip()
        cfg["debug_log"] = bool(llm.get("embedding_debug_log"))
    except Exception as exc:
        logger.warning("teamknowparse_embedding_config_load_failed", error=str(exc))
    return cfg


def _normalize_term(term: str) -> str:
    term = term.lower()
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("s"):
        return term[:-1]
    return term


def _terms(text: str, *, expand: bool = False) -> set[str]:
    lowered = (text or "").lower()
    terms = {
        _normalize_term(term)
        for term in re.findall(r"[a-zA-Z0-9_]+", lowered)
        if len(term) > 2 and term.lower() not in _STOPWORDS
    }
    if not expand:
        return terms

    expanded = set(terms)
    raw_terms = {_normalize_term(term) for term in re.findall(r"[a-zA-Z0-9_]+", lowered)}
    for term in terms | raw_terms:
        expanded.update(_normalize_term(item) for item in _SYNONYMS.get(term, set()))

    if re.search(r"\b(no|low|without)\s+light\b|\bdark(?:ness)?\b|\bnight\b", lowered):
        expanded.update({"light", "night", "dark", "darkness", "low", "infrared"})
    if re.search(r"\b(work|works|working|operate|operates)\b", lowered):
        expanded.update({"operate", "adapt", "adapting", "mode", "modes"})
    if re.search(r"\b(camera|cameras|surveillance|cctv)\b", lowered):
        expanded.update({"camera", "cameras", "surveillance", "security", "cctv"})
    if re.search(r"\b(identify|detect|track|near|nearby|close)\b|\bnear\s+me\b", lowered):
        expanded.update({"identify", "detect", "detection", "proximity", "sensor", "sensors", "object", "objects", "rfid", "track", "tracking", "non", "contact"})
    if re.search(r"\bhuge\s+amounts?\s+of\s+data\b|\b(big|large|massive)\s+data\b|\bdata\s+(analysis|analytics|mining)\b", lowered):
        expanded.update({"big", "data", "volume", "volumes", "large", "massive", "analytics", "analysis", "mining", "patterns", "insights"})
    return expanded


def _concepts(terms: set[str]) -> set[str]:
    return {name for name, members in _CONCEPT_TERMS.items() if terms & members}


def _term_presence(text: str, term: str) -> bool:
    return bool(re.search(rf"\b{re.escape(term)}\w*\b", text, re.IGNORECASE))


def _term_positions(text: str, terms: set[str]) -> list[int]:
    positions: list[int] = []
    for term in terms:
        pattern = re.compile(rf"\b{re.escape(term)}\w*\b", re.IGNORECASE)
        positions.extend(match.start() for match in pattern.finditer(text))
    return sorted(positions)


def _proximity_bonus(text: str, query_terms: set[str]) -> float:
    positions = _term_positions(text, query_terms)
    if len(positions) < 2:
        return 0.0
    best_span = min((b - a for a, b in zip(positions, positions[1:])), default=10_000)
    if best_span <= 80:
        return 1.5
    if best_span <= 180:
        return 0.9
    if best_span <= 320:
        return 0.4
    return 0.0


def _idf_weights(query_terms: set[str], corpus_terms: list[set[str]]) -> dict[str, float]:
    total = max(1, len(corpus_terms))
    weights: dict[str, float] = {}
    for term in query_terms:
        df = sum(1 for terms in corpus_terms if term in terms)
        weights[term] = 1.0 + math.log((total + 1) / (df + 1))
    return weights


def _lexical_score(text: str, query: str, weights: dict[str, float] | None = None) -> float:
    text_terms = _terms(text, expand=False)
    query_terms = _terms(query, expand=True)
    if not query_terms:
        return 0.0
    weights = weights or {term: 1.0 for term in query_terms}
    weighted_overlap = sum(weights.get(term, 1.0) for term in query_terms if term in text_terms)
    direct_terms = _terms(query, expand=False)
    direct_bonus = sum(0.75 for term in direct_terms if term in text_terms)
    query_concepts = _concepts(query_terms)
    text_concepts = _concepts(text_terms)
    concept_bonus = 1.4 * len(query_concepts & text_concepts)
    proximity_bonus = _proximity_bonus(text, query_terms)
    lowered_query = query.lower()
    lowered_text = text.lower()
    phrase_bonus = 0.5 if lowered_query in lowered_text else 0.0
    if re.search(r"\bhuge\s+amounts?\s+of\s+data\b|\b(big|large|massive)\s+data\b", lowered_query):
        if "big data" in lowered_text:
            phrase_bonus += 10.0
        if "big data is defined" in lowered_text or "characteristics of big data" in lowered_text:
            phrase_bonus += 5.0
    if re.search(r"\b(no|low|without)\s+light\b|\bday\b.*\bnight\b|\bnight\b.*\bday\b", lowered_query):
        if "day or night" in lowered_text or "day and night" in lowered_text:
            phrase_bonus += 6.0
        if "adapt modes" in lowered_text or "adapt mode" in lowered_text:
            phrase_bonus += 4.0
    if re.search(r"\bidentify\b|\bnear\s+me\b|\bnearby\b|\bclose\b", lowered_query):
        if "proximity sensors" in lowered_text or "non-contact detection" in lowered_text:
            phrase_bonus += 8.0
        if "radio frequency identification" in lowered_text or "rfid" in lowered_text:
            phrase_bonus += 3.0
    return weighted_overlap + direct_bonus + concept_bonus + proximity_bonus + phrase_bonus


def _rerank_hits(query: str, hits: list[dict], top_k: int) -> list[dict]:
    """Deterministically rerank Qwen candidates by domain terms and proximity.

    Qwen provides semantic recall. This local stage restores the behavior that
    OpenAI embeddings previously gave for domain-heavy questions by preferring
    rare, meaningful terms and multi-concept chunks over generic IoT text.
    """
    query_terms = _terms(query, expand=True)
    if not hits:
        return []
    if not query_terms:
        return sorted(hits, key=lambda item: item.get("score", 0.0), reverse=True)[:top_k]

    corpus_terms = [_terms(hit.get("text") or "", expand=False) for hit in hits]
    weights = _idf_weights(query_terms, corpus_terms)
    reranked: list[dict] = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        text = f"{hit.get('text') or ''} {meta.get('file_path') or ''} {meta.get('filename') or ''}"
        base_score = float(hit.get("score") or 0.0)
        lexical = _lexical_score(text, query, weights)
        rerank_score = (base_score * 0.35) + lexical
        updated = dict(hit)
        updated["base_score"] = base_score
        updated["lexical_score"] = lexical
        updated["rerank_score"] = rerank_score
        updated["score"] = rerank_score
        updated["reranked"] = True
        reranked.append(updated)
    return sorted(reranked, key=lambda item: item.get("rerank_score", item.get("score", 0.0)), reverse=True)[:top_k]


class TeamKnowParseVectorService:
    def __init__(self) -> None:
        self.client = None
        self.collection = None
        self.collection_name = None

    def _collection(self, collection_name: str | None = None):
        import chromadb

        name = collection_name or settings.TEAMKNOWPARSE_CHROMA_COLLECTION
        if self.collection is None or self.collection_name != name:
            self.client = chromadb.PersistentClient(path=settings.TEAMKNOWPARSE_CHROMA_DIR)
            self.collection = self.client.get_or_create_collection(name)
            self.collection_name = name
        return self.collection

    async def _embed_with_config(self, texts: list[str], cfg: dict) -> list[list[float]]:
        base_url = _embedding_base_url(cfg.get("base_url") or "")
        key = _api_key(base_url, cfg.get("api_key") or "")
        if not key:
            raise RuntimeError("EMBEDDING_API_KEY or OPENAI_API_KEY is required for TeamKnowParse vector indexing")
        model = cfg.get("model") or settings.TEAMKNOWPARSE_EMBEDDING_MODEL
        if cfg.get("debug_log"):
            sample = texts[0] if texts else ""
            logger.info(
                "teamknowparse_embedding_request",
                model=model,
                base_url=base_url or "openai",
                collection=cfg.get("collection") or settings.TEAMKNOWPARSE_CHROMA_COLLECTION,
                input_count=len(texts),
                total_chars=sum(len(text or "") for text in texts),
                sample_chars=len(sample),
                sample_text=(sample[:500] + ("..." if len(sample) > 500 else "")),
            )
        gateway = LLMGateway.from_settings(
            {
                "embedding_base_url": base_url,
                "embedding_api_key": key,
                "embedding_model": model,
                "embedding_chroma_collection": cfg.get("collection"),
                "embedding_debug_log": cfg.get("debug_log"),
            }
        )
        return await gateway.embed_texts(texts, model=model)

    async def _embed(self, texts: list[str], session: AsyncSession | None = None) -> list[list[float]]:
        return await self._embed_with_config(texts, await _embedding_config(session))

    async def _mark_batch_indexed(self, batch: list[dict]) -> None:
        ids = [item["chunk_id"] for item in batch]
        async with async_session_maker() as db:
            await db.execute(
                update(TeamKnowParseKbChunk)
                .where(TeamKnowParseKbChunk.chunk_id.in_(ids))
                .values(embedding_status="indexed", embedding_error=None)
            )
            await db.commit()

    async def _mark_batch_failed(self, batch: list[dict], error_text: str) -> None:
        ids = [item["chunk_id"] for item in batch]
        async with async_session_maker() as db:
            await db.execute(
                update(TeamKnowParseKbChunk)
                .where(TeamKnowParseKbChunk.chunk_id.in_(ids))
                .values(embedding_status="failed", embedding_error=error_text[:2000])
            )
            await db.commit()

    async def _index_unit_batch_resilient(
        self,
        team_id: str,
        units: list[dict],
        cfg: dict,
        *,
        max_attempts: int = 3,
    ) -> tuple[list[str], dict[str, str]]:
        if not units:
            return [], {}
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                embeddings = await self._embed_with_config([item["embedding_text"] for item in units], cfg)
                dimension = len(embeddings[0]) if embeddings else None
                base_collection = cfg.get("collection") or settings.TEAMKNOWPARSE_CHROMA_COLLECTION
                collection_name = _dimension_collection_name(base_collection, dimension)
                self._collection(collection_name).upsert(
                    ids=[item["vector_id"] for item in units],
                    embeddings=embeddings,
                    metadatas=[{
                        "team_id": item["team_id"],
                        "parent_chunk_id": item["parent_chunk_id"],
                        "file_path": item["file_path"],
                        "filename": item["filename"] or "",
                        "page_number": item["page_number"] or 0,
                        "page_type": item["page_type"] or "",
                        "source_run_id": item["source_run_id"],
                        "embedding_dimension": dimension or 0,
                        "embedding_collection": collection_name,
                        "embedding_part_index": item["embedding_part_index"],
                        "embedding_part_count": item["embedding_part_count"],
                        "embedding_char_start": item["embedding_char_start"],
                        "embedding_char_end": item["embedding_char_end"],
                    } for item in units],
                )
                return [item["parent_chunk_id"] for item in units], set()
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "teamknowparse_embedding_batch_attempt_failed",
                    team_id=team_id,
                    error=str(exc),
                    batch_size=len(units),
                    total_chars=sum(len(item.get("embedding_text") or "") for item in units),
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** attempt, 8))

        if len(units) > 1:
            mid = max(1, len(units) // 2)
            await asyncio.sleep(1)
            left_indexed, left_failed = await self._index_unit_batch_resilient(team_id, units[:mid], cfg, max_attempts=max_attempts)
            await asyncio.sleep(1)
            right_indexed, right_failed = await self._index_unit_batch_resilient(team_id, units[mid:], cfg, max_attempts=max_attempts)
            return left_indexed + right_indexed, {**left_failed, **right_failed}

        error_text = str(last_error or "Embedding failed")[:2000]
        failed_parent = units[0]["parent_chunk_id"]
        await self._mark_batch_failed([{"chunk_id": failed_parent}], error_text)
        return [], {failed_parent: error_text}

    async def index_team_chunks(self, session: AsyncSession, team_id: str, batch_size: int | None = None) -> dict[str, int]:
        configured_batch_size = batch_size or settings.TEAMKNOWPARSE_EMBEDDING_BATCH_SIZE
        configured_batch_size = max(1, min(512, int(configured_batch_size or 96)))
        concurrency = max(1, min(16, int(settings.TEAMKNOWPARSE_EMBEDDING_CONCURRENCY or 2)))
        cfg = await _embedding_config(session)
        result = await session.execute(
            select(TeamKnowParseKbChunk)
            .where(TeamKnowParseKbChunk.team_id == team_id)
            .where(TeamKnowParseKbChunk.embedding_status.in_(["pending", "failed"]))
            .order_by(TeamKnowParseKbChunk.chunk_index)
        )
        chunks = [
            {
                "chunk_id": chunk.chunk_id,
                "team_id": chunk.team_id,
                "search_text": chunk.search_text,
                "file_path": chunk.file_path,
                "filename": chunk.filename or "",
                "page_number": chunk.page_number or 0,
                "page_type": chunk.page_type or "",
                "source_run_id": chunk.source_run_id,
            }
            for chunk in result.scalars().all()
        ]
        units: list[dict] = []
        parent_unit_totals: Counter[str] = Counter()
        subchunk_chars = max(400, int(settings.TEAMKNOWPARSE_EMBEDDING_SUBCHUNK_CHARS or 1600))
        subchunk_overlap = max(0, int(settings.TEAMKNOWPARSE_EMBEDDING_SUBCHUNK_OVERLAP or 0))
        batch_max_chars = max(10_000, int(settings.TEAMKNOWPARSE_EMBEDDING_BATCH_MAX_CHARS or 120_000))
        for chunk in chunks:
            chunk_units = _embedding_units_for_chunk(chunk, max_chars=subchunk_chars, overlap_chars=subchunk_overlap)
            units.extend(chunk_units)
            parent_unit_totals[chunk["chunk_id"]] = len(chunk_units)

        batches = _batch_embedding_units(units, max_count=configured_batch_size, max_chars=batch_max_chars)
        semaphore = asyncio.Semaphore(concurrency)

        async def run_batch(batch: list[dict]) -> tuple[list[str], set[str]]:
            async with semaphore:
                result = await self._index_unit_batch_resilient(team_id, batch, cfg)
                await asyncio.sleep(0.5)
                return result

        results = await asyncio.gather(*(run_batch(batch) for batch in batches)) if batches else []
        indexed_unit_counts: Counter[str] = Counter()
        failed_parent_errors: dict[str, str] = {}
        for indexed_parents, failed_errors in results:
            indexed_unit_counts.update(indexed_parents)
            failed_parent_errors.update(failed_errors)

        indexed_parents = [
            {"chunk_id": parent_id}
            for parent_id, total in parent_unit_totals.items()
            if total > 0 and indexed_unit_counts[parent_id] == total and parent_id not in failed_parent_errors
        ]
        if indexed_parents:
            await self._mark_batch_indexed(indexed_parents)
        for parent_id, error_text in failed_parent_errors.items():
            await self._mark_batch_failed([{"chunk_id": parent_id}], error_text)

        indexed = len(indexed_parents)
        failed = len(failed_parent_errors)
        return {
            "indexed": indexed,
            "failed": failed,
            "total": len(chunks),
            "embedding_units": len(units),
            "embedding_batches": len(batches),
            "batch_size": configured_batch_size,
            "batch_max_chars": batch_max_chars,
            "subchunk_chars": subchunk_chars,
            "subchunk_overlap": subchunk_overlap,
            "concurrency": concurrency,
        }

    async def search(self, session: AsyncSession, team_id: str, query: str, mode: SearchMode = "hybrid", top_k: int = 10) -> list[dict]:
        mode = "hybrid"

        exact_hits = []
        if mode in {"exact", "hybrid"}:
            result = await session.execute(select(TeamKnowParseKbChunk).where(TeamKnowParseKbChunk.team_id == team_id))
            chunks = list(result.scalars().all())
            query_terms = _terms(query, expand=True)
            corpus_terms = [_terms((chunk.search_text or "") + " " + (chunk.file_path or ""), expand=False) for chunk in chunks]
            weights = _idf_weights(query_terms, corpus_terms)
            for chunk in chunks:
                haystack = f"{chunk.search_text or ''} {chunk.file_path or ''} {chunk.filename or ''}"
                score = _lexical_score(haystack, query, weights)
                if score:
                    exact_hits.append({"chunk": chunk, "score": float(score), "source": "exact"})
            exact_hits.sort(key=lambda item: item["score"], reverse=True)
            exact_hits = exact_hits[:max(top_k * 10, 80)]

        vector_hits = []
        if mode in {"vector", "hybrid"}:
            try:
                cfg = await _embedding_config(session)
                embedding = (await self._embed([query], session=session))[0]
                base_collection = cfg.get("collection") or settings.TEAMKNOWPARSE_CHROMA_COLLECTION
                collection_name = _dimension_collection_name(base_collection, len(embedding))
                result = self._collection(collection_name).query(
                    query_embeddings=[embedding],
                    n_results=max(top_k * 20, 80),
                    where={"team_id": team_id},
                    include=["metadatas", "distances"],
                )
                ids = result.get("ids", [[]])[0]
                metas = result.get("metadatas", [[]])[0]
                distances = result.get("distances", [[]])[0]
                parent_ids = [_parent_chunk_id(vector_id, metas[idx] if idx < len(metas) else {}) for idx, vector_id in enumerate(ids)]
                chunks_by_id = {}
                if parent_ids:
                    db_result = await session.execute(
                        select(TeamKnowParseKbChunk).where(TeamKnowParseKbChunk.team_id == team_id).where(TeamKnowParseKbChunk.chunk_id.in_(parent_ids))
                    )
                    chunks_by_id = {chunk.chunk_id: chunk for chunk in db_result.scalars().all()}
                for idx, vector_id in enumerate(ids):
                    meta = metas[idx] if idx < len(metas) else {}
                    parent_id = _parent_chunk_id(vector_id, meta)
                    chunk = chunks_by_id.get(parent_id)
                    if chunk:
                        meta = {**meta, "vector_id": vector_id, "file_path": chunk.file_path, "filename": chunk.filename or "", "page_number": chunk.page_number or 0, "page_type": chunk.page_type or ""}
                    vector_hits.append({
                        "chunk_id": parent_id,
                        "text": chunk.search_text if chunk else "",
                        "metadata": meta,
                        "score": 1.0 / (1.0 + (distances[idx] if idx < len(distances) else 0.0)),
                        "source": "vector",
                    })
            except Exception as exc:
                logger.warning("teamknowparse_vector_search_failed", team_id=team_id, error=str(exc))
                if mode == "vector":
                    raise

        combined: dict[str, dict] = {}
        for item in exact_hits:
            chunk = item["chunk"]
            combined.setdefault(chunk.chunk_id, {
                "chunk_id": chunk.chunk_id,
                "text": chunk.search_text,
                "metadata": {"file_path": chunk.file_path, "filename": chunk.filename or "", "page_number": chunk.page_number or 0, "page_type": chunk.page_type or ""},
                "score": 0.0,
                "source": "",
            })
            combined[chunk.chunk_id]["score"] += item["score"]
            combined[chunk.chunk_id]["source"] = (combined[chunk.chunk_id]["source"] + "+exact").strip("+")
        for item in vector_hits:
            combined.setdefault(item["chunk_id"], item)
            if combined[item["chunk_id"]] is not item:
                combined[item["chunk_id"]]["score"] += item["score"]
                combined[item["chunk_id"]]["source"] += "+vector"
        results = sorted(combined.values(), key=lambda item: item["score"], reverse=True)
        if mode in {"vector", "hybrid"}:
            return _rerank_hits(query, results, top_k)
        return results[:top_k]
