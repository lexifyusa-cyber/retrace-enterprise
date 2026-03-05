#!/usr/bin/env python3
"""
Clear only ChromaDB embeddings. Products and DB are left unchanged.

Run from the backend directory:
  python clear_chroma_only.py

Or with venv:
  .venv/bin/python clear_chroma_only.py

After running, re-train any product to repopulate embeddings.
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
        return 0.0
    size_mb = sum(f.stat().st_size for f in chroma_path.rglob("*") if f.is_file()) / (1024 * 1024)
    shutil.rmtree(chroma_path)
    chroma_path.mkdir(exist_ok=True)  # leave empty dir so app can recreate collection
    return round(size_mb, 2)


def main():
    print("Clearing ChromaDB embeddings only (chroma_data)...")
    freed_mb = clear_chroma_data()
    print(f"  Cleared ~{freed_mb} MB. All chunks removed.")
    print("  Products are unchanged. Re-train to repopulate embeddings.")


if __name__ == "__main__":
    main()
