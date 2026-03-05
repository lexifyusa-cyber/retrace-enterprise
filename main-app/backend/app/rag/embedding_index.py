"""
EmbeddingIndexService – index and search chunks using ChromaDB.

ChromaDB stores ids, embeddings, and minimal metadata (product_id,
source_path, content_category, chunk_index, indexed_at).

Chunk TEXT is NOT stored in Chroma — it lives in the encrypted compressed
knowledge base blob (KnowledgeBaseStore) and is loaded on-demand at
query time.
"""

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy import select

from app.db.database import async_session_maker
from app.rag.interfaces import EmbeddingClient
from app.rag.models import Chunk, ChunkMetadata, ChunkRecord, ProcessingType

logger = structlog.get_logger()

EMBEDDING_BATCH_SIZE = 35

os.environ["CHROMA_SERVER_NOFILE"] = "65536"

# ---------------------------------------------------------------------------
# ChromaDB singleton
# ---------------------------------------------------------------------------

_chroma_client = None


def _get_chroma_client():
    """Return (or create) a persistent ChromaDB client."""
    global _chroma_client
    if _chroma_client is None:
        import chromadb

        db_path = Path(__file__).parent.parent.parent / "chroma_data"
        db_path.mkdir(exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(db_path))
        logger.info("chroma_client_init", path=str(db_path))
    return _chroma_client


def _get_collection():
    """Get or create the 'chunks' collection with cosine distance."""
    client = _get_chroma_client()
    return client.get_or_create_collection(
        name="chunks",
        metadata={"hnsw:space": "cosine"},
    )


def read_file_lines(path: str, start_line: int, end_line: int) -> str:
    """Read lines [start_line, end_line] (1-based inclusive) from file. Returns "" on error."""
    if not path or start_line < 1 or end_line < 1 or start_line > end_line:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return ""
    if not lines:
        return ""
    # 1-based inclusive → 0-based slice
    lo = max(0, start_line - 1)
    hi = min(len(lines), end_line)
    return "".join(lines[lo:hi])


class EmbeddingIndexService:
    """Embed, store, and search text chunks using ChromaDB + encrypted chunk store."""

    def __init__(self, embedding_client: EmbeddingClient, chunk_store=None):
        self._emb = embedding_client
        self._chunk_store = chunk_store

    # ------------------------------------------------------------------
    # Incremental helpers
    # ------------------------------------------------------------------

    async def get_indexed_files(self, product_id: str) -> dict[str, datetime]:
        """Return {source_path: latest_indexed_at} from Chroma. Used by pipeline for re-processing."""
        collection = _get_collection()
        try:
            result = await asyncio.to_thread(
                collection.get,
                where={"product_id": product_id},
                include=["metadatas"],
            )
        except Exception as exc:
            logger.warning("chroma_get_indexed_files_error", product_id=product_id, error=str(exc))
            return {}
        if not result or not result.get("metadatas"):
            return {}
        by_path: dict[str, float] = {}
        for meta in result["metadatas"]:
            path = meta.get("source_path") or ""
            ts = meta.get("indexed_at")
            if isinstance(ts, (int, float)) and path:
                by_path[path] = max(by_path.get(path, 0), float(ts))
        return {
            path: datetime.utcfromtimestamp(ts) if ts else datetime.utcnow()
            for path, ts in by_path.items()
        }

    async def clear_file_chunks(self, product_id: str, source_paths: list[str]) -> int:
        """Delete chunks for specific files (modified / deleted) from Chroma and chunk store."""
        if not source_paths:
            return 0
        count = 0
        ids_to_delete: list[str] = []
        try:
            collection = _get_collection()
            paths_set = set(source_paths)
            existing = await asyncio.to_thread(
                collection.get,
                where={"product_id": product_id},
                include=["metadatas"],
            )
            if existing and existing["ids"]:
                ids_to_delete = [
                    cid
                    for cid, meta in zip(existing["ids"], existing["metadatas"])
                    if meta.get("source_path") in paths_set
                ]
                if ids_to_delete:
                    await asyncio.to_thread(collection.delete, ids=ids_to_delete)
                    count = len(ids_to_delete)
                    logger.info(
                        "chroma_cleared_files",
                        product_id=product_id,
                        files=len(source_paths),
                        chunks=count,
                    )
        except Exception as exc:
            logger.warning("chroma_clear_files_error", product_id=product_id, error=str(exc))
        if self._chunk_store and ids_to_delete:
            try:
                await self._chunk_store.delete_by_ids(ids_to_delete)
            except Exception as exc:
                logger.warning("chunk_store_clear_files_error", product_id=product_id, error=str(exc))
        logger.info("file_chunks_cleared", product_id=product_id, files=len(source_paths), count=count)
        return count

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def clear_product(self, product_id: str) -> int:
        """Delete all existing chunks for a product from Chroma and chunk store."""
        count = 0
        try:
            collection = _get_collection()
            existing = await asyncio.to_thread(
                collection.get, where={"product_id": product_id}
            )
            if existing and existing["ids"]:
                count = len(existing["ids"])
                await asyncio.to_thread(
                    collection.delete, ids=existing["ids"]
                )
                logger.info(
                    "chroma_cleared",
                    product_id=product_id,
                    count=count,
                )
        except Exception as exc:
            logger.warning("chroma_clear_error", product_id=product_id, error=str(exc))
        if self._chunk_store:
            try:
                await self._chunk_store.delete_product(product_id)
            except Exception as exc:
                logger.warning("chunk_store_clear_error", product_id=product_id, error=str(exc))
        logger.info("chunks_cleared", product_id=product_id, count=count)
        return count

    async def index_chunks(
        self,
        product_id: str,
        chunks: list[Chunk],
        batch_size: int = EMBEDDING_BATCH_SIZE,
        on_batch_progress=None,
    ) -> int:
        """Embed and persist chunks in ChromaDB + encrypted chunk store.
        
        on_batch_progress: optional async callback(batch_num, total_batches, indexed_so_far, error_msg)
        """
        total = 0
        collection = _get_collection()
        total_batches = (len(chunks) + batch_size - 1) // batch_size if chunks else 0

        for i in range(0, len(chunks), batch_size):
            batch_num = i // batch_size + 1
            batch = chunks[i : i + batch_size]
            texts = [c.text for c in batch]

            if on_batch_progress:
                try:
                    await on_batch_progress(batch_num, total_batches, total, None)
                except Exception:
                    pass

            try:
                embeddings = await self._emb.embed_texts(texts)
            except Exception as exc:
                logger.error("embedding_batch_failed", batch_start=i, error=str(exc))
                if on_batch_progress:
                    try:
                        await on_batch_progress(batch_num, total_batches, total, str(exc))
                    except Exception:
                        pass
                continue

            indexed_at = time.time()
            try:
                meta_list = []
                for c in batch:
                    m = {
                        "product_id": product_id,
                        "source_path": c.source_path,
                        "content_category": c.metadata.content_category or "text",
                        "chunk_index": c.metadata.chunk_index if c.metadata.chunk_index is not None else 0,
                        "processing_type": c.processing_type.value,
                        "chunk_type": c.metadata.chunk_type or "content",
                        "indexed_at": indexed_at,
                    }
                    if c.metadata.component:
                        m["component"] = c.metadata.component
                    if c.metadata.description:
                        m["description"] = c.metadata.description[:200]
                    meta_list.append(m)
                await asyncio.to_thread(
                    collection.add,
                    ids=[c.id for c in batch],
                    embeddings=embeddings,
                    metadatas=meta_list,
                )
            except Exception as exc:
                logger.error("chroma_add_failed", batch_start=i, error=str(exc))

            total += len(batch)
            logger.info(
                "indexed_batch",
                batch=batch_num,
                batch_size=len(batch),
                total_so_far=total,
            )
        return total

    async def get_chunks_list(
        self,
        product_id: str,
        processing_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """List chunks from Chroma with pagination. Returns (chunk_dicts, total_count)."""
        collection = _get_collection()
        try:
            where = {"product_id": product_id}
            if processing_type:
                where["processing_type"] = processing_type
            result = await asyncio.to_thread(
                collection.get,
                where=where,
                include=["metadatas"],
            )
        except Exception as exc:
            logger.warning("chroma_get_chunks_error", product_id=product_id, error=str(exc))
            return [], 0
        ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []
        total = len(ids)
        # Sort by indexed_at desc (newest first)
        pairs = list(zip(ids, metadatas))
        pairs.sort(key=lambda p: (p[1].get("indexed_at") or 0), reverse=True)
        page = pairs[offset : offset + limit]
        chunk_dicts = []
        for chunk_id, meta in page:
            path = meta.get("source_path", "")
            sl = meta.get("start_line") or 0
            el = meta.get("end_line") or 0
            text = ""
            if path and sl and el and sl <= el:
                text = read_file_lines(path, int(sl), int(el))
            ts = meta.get("indexed_at")
            created_at = datetime.utcfromtimestamp(ts).isoformat() + "Z" if isinstance(ts, (int, float)) else None
            chunk_dicts.append({
                "chunk_id": chunk_id,
                "source_path": path,
                "processing_type": meta.get("processing_type", "other"),
                "text": text,
                "metadata": {k: v for k, v in meta.items() if k not in ("product_id", "indexed_at")},
                "created_at": created_at,
                "has_embedding": True,
            })
        return chunk_dicts, total

    async def get_training_tree_data(
        self, product_id: str
    ) -> list[tuple[str, str, int, str]]:
        """Return (processing_type, source_path, chunk_count, sub_category) for building training tree."""
        collection = _get_collection()
        try:
            result = await asyncio.to_thread(
                collection.get,
                where={"product_id": product_id},
                include=["metadatas"],
            )
        except Exception as exc:
            logger.warning("chroma_training_tree_error", product_id=product_id, error=str(exc))
            return []
        metadatas = result.get("metadatas") or []
        # Group by (processing_type, source_path), count chunks, pick sub_category from first
        key_to_count: dict[tuple[str, str], int] = {}
        key_to_sub: dict[tuple[str, str], str] = {}
        for meta in metadatas:
            ptype = meta.get("processing_type") or "other"
            path = meta.get("source_path") or ""
            sub = meta.get("sub_category") or "general"
            key = (ptype, path)
            key_to_count[key] = key_to_count.get(key, 0) + 1
            if key not in key_to_sub:
                key_to_sub[key] = sub
        return [
            (ptype, path, key_to_count[(ptype, path)], key_to_sub[(ptype, path)])
            for (ptype, path) in key_to_count
        ]

    async def get_source_paths_by_prefix(
        self, product_id: str, path_prefix: str
    ) -> list[str]:
        """Return distinct source_paths for product where path starts with prefix (e.g. agent-qa://)."""
        collection = _get_collection()
        try:
            result = await asyncio.to_thread(
                collection.get,
                where={"product_id": product_id},
                include=["metadatas"],
            )
        except Exception as exc:
            logger.warning("chroma_source_paths_error", product_id=product_id, error=str(exc))
            return []
        metadatas = result.get("metadatas") or []
        paths = set()
        for meta in metadatas:
            path = meta.get("source_path") or ""
            if path.startswith(path_prefix):
                paths.add(path)
        return list(paths)

    # ------------------------------------------------------------------
    # Read path (fast via ChromaDB)
    # ------------------------------------------------------------------

    async def search(
        self,
        product_id: str,
        query: str,
        top_k: int = 10,
        filters: Optional[dict] = None,
    ) -> list[Chunk]:
        """Semantic search over indexed chunks using ChromaDB ANN.

        *filters* may contain:
          - processing_types: list[str]  — restrict to these content types
          - sub_categories: list[str]    — restrict to these sub-categories
          - component: str               — exact component match
          - concepts: str                — substring to match in concepts field
          - exclude_types: list[str]     — exclude these content types
        """
        # 1) Embed the query
        try:
            query_vecs = await self._emb.embed_texts([query])
        except Exception as exc:
            logger.error("query_embed_failed", error=str(exc))
            return []

        # 2) Build ChromaDB where filter
        conditions: list[dict] = [{"product_id": product_id}]

        if filters:
            if filters.get("processing_types"):
                ptypes = filters["processing_types"]
                if len(ptypes) == 1:
                    conditions.append({"processing_type": ptypes[0]})
                else:
                    conditions.append({"processing_type": {"$in": ptypes}})

            if filters.get("sub_categories"):
                scats = filters["sub_categories"]
                if len(scats) == 1:
                    conditions.append({"sub_category": scats[0]})
                else:
                    conditions.append({"sub_category": {"$in": scats}})

            if filters.get("exclude_types"):
                for et in filters["exclude_types"]:
                    conditions.append({"processing_type": {"$ne": et}})

            if filters.get("component"):
                conditions.append({"component": filters["component"]})

        if len(conditions) == 1:
            where_filter = conditions[0]
        else:
            where_filter = {"$and": conditions}

        # 3) Query ChromaDB (no documents; we load text from disk or SQLite)
        collection = _get_collection()
        try:
            results = await asyncio.to_thread(
                collection.query,
                query_embeddings=query_vecs,
                n_results=top_k,
                where=where_filter,
                include=["metadatas", "distances"],
            )
        except Exception as exc:
            logger.error("chroma_query_failed", error=str(exc))
            return []

        if not results or not results["ids"] or not results["ids"][0]:
            return []

        # 4) Resolve text from encrypted compressed KB store
        chunk_ids = results["ids"][0]
        resolved_texts: dict[str, str] = {}

        try:
            from app.rag.kb_store import KnowledgeBaseStore
            kb_store = KnowledgeBaseStore()
            resolved_texts = await kb_store.get_chunk_texts(product_id, chunk_ids)
        except Exception as exc:
            logger.warning("kb_store_read_failed", error=str(exc))

        # Legacy fallback: try old chunk_store if KB store has no data
        if not resolved_texts and self._chunk_store:
            try:
                resolved_texts = await self._chunk_store.get_many(chunk_ids)
            except Exception as exc:
                logger.warning("chunk_store_fallback_failed", error=str(exc))

        # Last resort: try reading from disk by line range (legacy chunks)
        chunks: list[Chunk] = []
        for idx, chunk_id in enumerate(chunk_ids):
            meta = results["metadatas"][0][idx] if results["metadatas"] else {}
            distance = results["distances"][0][idx] if results["distances"] else 0.0
            path = meta.get("source_path", "")

            text = resolved_texts.get(chunk_id, "")
            if not text:
                sl = meta.get("start_line") or 0
                el = meta.get("end_line") or 0
                if path and sl and el and sl <= el:
                    text = read_file_lines(path, int(sl), int(el))

            similarity = 1.0 - distance
            ptype_str = meta.get("processing_type", "other")
            try:
                ptype = ProcessingType(ptype_str)
            except ValueError:
                ptype = ProcessingType.OTHER
            chunk = Chunk(
                id=chunk_id,
                source_path=path,
                processing_type=ptype,
                text=text or "",
                metadata=ChunkMetadata(
                    score=similarity,
                    content_category=meta.get("content_category") or None,
                    source_file=meta.get("source_path") or None,
                    chunk_index=meta.get("chunk_index"),
                    chunk_type=meta.get("chunk_type") or "content",
                    component=meta.get("component") or None,
                    description=meta.get("description") or None,
                ),
            )
            chunks.append(chunk)

        logger.info(
            "chroma_search_done",
            product_id=product_id,
            top_k=top_k,
            results=len(chunks),
        )
        return chunks


# ---------------------------------------------------------------------------
# Migration helper: populate ChromaDB from existing SQLite data (one-time)
# ---------------------------------------------------------------------------

async def migrate_sqlite_to_chroma() -> int:
    """One-time migration: copy existing embeddings from SQLite → ChromaDB.

    Called on application startup.  Skips if ChromaDB already has data.
    """
    collection = _get_collection()
    existing_count = await asyncio.to_thread(collection.count)

    if existing_count > 0:
        logger.info("chroma_already_populated", count=existing_count)
        return existing_count

    # Load all chunk records that have embeddings
    async with async_session_maker() as session:
        result = await session.execute(
            select(ChunkRecord).where(ChunkRecord.embedding.isnot(None))
        )
        records = list(result.scalars().all())

    if not records:
        logger.info("chroma_migration_nothing_to_migrate")
        return 0

    valid = [r for r in records if r.embedding and len(r.embedding) > 0]
    logger.info("chroma_migration_start", total_records=len(valid))

    batch_size = 100
    migrated = 0
    for i in range(0, len(valid), batch_size):
        batch = valid[i : i + batch_size]
        try:
            meta_list = []
            for r in batch:
                m = {
                    "product_id": r.product_id,
                    "processing_type": r.processing_type,
                    "source_path": r.source_path,
                }
                if r.metadata_json:
                    if r.metadata_json.get("start_line") is not None:
                        m["start_line"] = r.metadata_json["start_line"]
                    if r.metadata_json.get("end_line") is not None:
                        m["end_line"] = r.metadata_json["end_line"]
                m["indexed_at"] = time.time()
                meta_list.append(m)
            await asyncio.to_thread(
                collection.add,
                ids=[r.chunk_id for r in batch],
                embeddings=[r.embedding for r in batch],
                metadatas=meta_list,
            )
            migrated += len(batch)
        except Exception as exc:
            logger.error("chroma_migration_batch_failed", batch=i, error=str(exc))

    logger.info("chroma_migration_complete", migrated=migrated)
    return migrated
