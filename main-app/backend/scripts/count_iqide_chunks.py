#!/usr/bin/env python3
"""
Count how many chunks the RAG pipeline would create for the iqide folder.
Uses the same crawler + fallback classifier + file processor as the real pipeline.
Now includes: folder tree, embed yes/no filter, folder-structure chunks,
and the new 30k chunk cap / symbol-only code chunking.
"""
import asyncio
import os
import sys

backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend not in sys.path:
    sys.path.insert(0, backend)

from app.rag.folder_crawler import FolderCrawlerService
from app.rag.file_classifier import _fallback_type, _fallback_embed
from app.rag.file_processor import FileProcessorService
from app.rag.folder_structure import build_folder_structure_chunks
from app.rag.models import ClassifiedFile, FileRecord, ProcessingType


IQIDE_ROOT = os.path.expanduser("~/iqide")
IQIDE_ROOT = os.path.abspath(IQIDE_ROOT)


def main():
    if not os.path.isdir(IQIDE_ROOT):
        print(f"ERROR: iqide folder not found at {IQIDE_ROOT}")
        sys.exit(1)

    crawler = FolderCrawlerService()
    records, folder_tree = crawler.crawl([IQIDE_ROOT])
    print(f"Crawled {len(records)} files under {IQIDE_ROOT}\n")

    classified: list[ClassifiedFile] = []
    for r in records:
        ptype = _fallback_type(r)
        embed = _fallback_embed(r)
        classified.append(
            ClassifiedFile(
                path=r.path,
                name=r.name,
                ext=r.ext,
                size_bytes=r.size_bytes,
                modified_at=r.modified_at,
                processing_type=ptype,
                sub_category="general",
                embed=embed,
            )
        )

    embed_yes = [c for c in classified if c.embed]
    embed_no = [c for c in classified if not c.embed]
    print(f"Embed yes: {len(embed_yes)}, Embed no: {len(embed_no)}")
    if embed_no:
        print("  Skipped files:")
        for c in embed_no:
            print(f"    {os.path.relpath(c.path, IQIDE_ROOT)} ({c.name}, {c.size_bytes}B)")

    by_type: dict[ProcessingType, int] = {}
    for c in embed_yes:
        by_type[c.processing_type] = by_type.get(c.processing_type, 0) + 1
    print("\nEmbed=yes files by processing type:")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {t.value}: {n}")

    async def run_processor():
        processor = FileProcessorService(vision_client=None)
        total = 0
        per_file: list[tuple[str, int]] = []
        chunk_counts: dict[str, int] = {}
        for f in embed_yes:
            chunks = await processor.process(f)
            n = len(chunks)
            total += n
            rel = os.path.relpath(f.path, IQIDE_ROOT)
            per_file.append((rel, n))
            chunk_counts[f.path] = n

        fs_chunks = build_folder_structure_chunks(folder_tree, classified, chunk_counts)
        total += len(fs_chunks)

        return total, per_file, len(fs_chunks)

    total_chunks, per_file, fs_count = asyncio.run(run_processor())

    print(f"\nContent chunks: {total_chunks - fs_count}")
    print(f"Folder structure chunks: {fs_count}")
    print(f"Total chunks: {total_chunks}")
    print("\nChunks per file (sorted by count descending):")
    for path, n in sorted(per_file, key=lambda x: -x[1]):
        print(f"  {n:4d}  {path}")


if __name__ == "__main__":
    main()
