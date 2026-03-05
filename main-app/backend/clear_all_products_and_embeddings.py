#!/usr/bin/env python3
"""
Clear all products data and ChromaDB embeddings.

Run from the backend directory:
  python clear_all_products_and_embeddings.py

Or with venv:
  .venv/bin/python clear_all_products_and_embeddings.py

This will:
  1. Delete ChromaDB data directory (all embeddings and metadata).
  2. Delete all product access records, then all products (cascade deletes
     folder_groups, folder_paths, sops, documentation, conversations, etc.).
"""

import asyncio
import shutil
import sys
from pathlib import Path

# Add backend to path so app imports work
sys.path.insert(0, str(Path(__file__).resolve().parent))


def clear_chroma_data():
    """Remove ChromaDB persistent data (avoids importing chromadb)."""
    backend = Path(__file__).resolve().parent
    chroma_path = backend / "chroma_data"
    if not chroma_path.exists():
        return 0
    size_mb = sum(f.stat().st_size for f in chroma_path.rglob("*") if f.is_file()) / (1024 * 1024)
    shutil.rmtree(chroma_path)
    chroma_path.mkdir(exist_ok=True)  # leave empty dir so app can recreate collection
    return round(size_mb, 2)


async def main():
    from sqlalchemy import delete
    from app.db.database import async_session_maker, init_db
    from app.models.product import Product
    from app.models.product_access import ProductAccess

    print("Initializing DB...")
    await init_db()

    # 1. Clear Chroma by deleting its data directory
    print("Clearing ChromaDB embeddings (deleting chroma_data)...")
    freed_mb = clear_chroma_data()
    print(f"  Cleared ChromaDB data (~{freed_mb} MB).")

    # 2. Delete all product access, then all products
    print("Clearing all products from database...")
    async with async_session_maker() as session:
        # product_access has FK to products without ON DELETE CASCADE; delete first
        await session.execute(delete(ProductAccess))
        await session.commit()
        print("  Deleted all product access records.")

        await session.execute(delete(Product))
        await session.commit()
        print("  Deleted all products (DB cascade removes folder_groups, sops, docs, etc.).")

    print("Done. All products data and embeddings have been cleared.")


if __name__ == "__main__":
    asyncio.run(main())
