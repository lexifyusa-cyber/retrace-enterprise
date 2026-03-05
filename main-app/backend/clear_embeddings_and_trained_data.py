#!/usr/bin/env python3
"""
Clear existing embeddings and trained chunk data so you can re-train from scratch.

Run from the backend directory:
  python clear_embeddings_and_trained_data.py

Or with venv:
  .venv/bin/python clear_embeddings_and_trained_data.py

This will:
  1. Delete ChromaDB data directory (all embeddings and vector metadata).
  2. Delete all rows from the encrypted_chunks table (stored chunk text).

Products and folder config are left unchanged. After running, re-train to
repopulate with the new chunking (e.g. token-based code stitching).
"""

import asyncio
import shutil
import sys
from pathlib import Path

# Add backend to path so app imports work
sys.path.insert(0, str(Path(__file__).resolve().parent))


def clear_chroma_data() -> float:
    """Remove ChromaDB persistent data (avoids importing chromadb)."""
    backend = Path(__file__).resolve().parent
    chroma_path = backend / "chroma_data"
    if not chroma_path.exists():
        return 0.0
    size_mb = sum(f.stat().st_size for f in chroma_path.rglob("*") if f.is_file()) / (1024 * 1024)
    shutil.rmtree(chroma_path)
    chroma_path.mkdir(exist_ok=True)
    return round(size_mb, 2)


async def clear_encrypted_chunks() -> int:
    """Delete all rows from encrypted_chunks table."""
    from sqlalchemy import delete
    from app.db.database import async_session_maker, init_db
    from app.rag.chunk_store import EncryptedChunk

    await init_db()
    async with async_session_maker() as session:
        result = await session.execute(delete(EncryptedChunk))
        await session.commit()
        return result.rowcount or 0


async def main():
    print("Clearing existing embeddings and trained chunk data...")
    chroma_mb = clear_chroma_data()
    print(f"  ChromaDB: cleared ~{chroma_mb} MB.")
    count = await clear_encrypted_chunks()
    print(f"  Encrypted chunks: deleted {count} rows.")
    print("Done. Products unchanged. Re-train to repopulate with new chunking.")


if __name__ == "__main__":
    asyncio.run(main())
