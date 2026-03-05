#!/usr/bin/env python3
"""
Verify whether chunk text is actually stored in SQLite and ChromaDB.
Run from main-app/backend: python scripts/verify_chunk_text_storage.py
"""
import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.db.database import async_session_maker
from app.rag.models import ChunkRecord


async def check_sqlite():
    """Check if chunk_records table has non-empty text."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(
                ChunkRecord.chunk_id,
                ChunkRecord.source_path,
                ChunkRecord.processing_type,
                ChunkRecord.text,
            ).limit(5)
        )
        rows = result.all()
    return rows


def check_chroma():
    """Check if Chroma returns documents (chunk text)."""
    try:
        from app.rag.embedding_index import _get_collection
    except Exception as e:
        return (None, None), str(e)
    collection = _get_collection()
    n = collection.count
    if n == 0:
        return ([], None), "Collection is empty (no chunks indexed yet)."
    # Get first 3 ids with documents
    sample = collection.get(limit=3, include=["documents", "metadatas"])
    ids = sample["ids"]
    docs = sample.get("documents")  # Chroma returns list of doc strings when include=["documents"]
    if docs is None and ids:
        sample2 = collection.get(ids=ids[:3], include=["documents"])
        docs = sample2.get("documents")
    return (ids, docs), None


def main():
    print("=== Chunk text storage verification ===\n")

    # 1) SQLite
    print("1. SQLite (chunk_records table)")
    print("-" * 50)
    try:
        rows = asyncio.run(check_sqlite())
        if not rows:
            print("   No rows in chunk_records. Train a product first.")
        else:
            for r in rows:
                text_len = len(r.text) if r.text else 0
                preview = (r.text[:80] + "…") if r.text and len(r.text) > 80 else (r.text or "(empty)")
                print(f"   chunk_id: {r.chunk_id[:12]}...")
                print(f"   source_path: {r.source_path[:60]}...")
                print(f"   text length: {text_len} bytes")
                print(f"   text preview: {preview!r}")
                print()
    except Exception as e:
        print(f"   Error: {e}")

    # 2) Chroma
    print("2. ChromaDB (documents in collection)")
    print("-" * 50)
    try:
        (ids, docs), err = check_chroma()
        if err:
            print(f"   {err}")
        elif not ids:
            print("   No ids in collection.")
        else:
            if docs is None:
                print("   include=['documents'] returned no 'documents' key — Chroma may not be storing document text.")
            elif isinstance(docs, list) and len(docs) > 0:
                for i, doc in enumerate(docs[:3]):
                    doc_len = len(doc) if doc else 0
                    preview = (doc[:80] + "…") if doc and len(doc) > 80 else (doc or "(empty)")
                    id_preview = (ids[i][:12] + "…") if ids and i < len(ids) else "?"
                    print(f"   id: {id_preview}")
                    print(f"   document length: {doc_len} chars")
                    print(f"   document preview: {preview!r}")
                    print()
            else:
                print("   documents list empty or not as expected.")
    except Exception as e:
        print(f"   Error: {e}")

    print("=== Done ===")


if __name__ == "__main__":
    main()
