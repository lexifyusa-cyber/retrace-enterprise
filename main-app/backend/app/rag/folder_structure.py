"""
FolderStructureBuilder – turn a folder tree into embeddable chunk(s).

The folder tree (built by FolderCrawlerService) is annotated with
embed decisions and chunk counts, then serialized into 1–3 text chunks
that fit within the embedding model's context window.
"""

from typing import Optional
from uuid import uuid4

import structlog

from app.rag.models import Chunk, ChunkMetadata, ClassifiedFile, ProcessingType

logger = structlog.get_logger()

MAX_TREE_CHUNK_CHARS = 20400


def build_folder_structure_chunks(
    folder_tree: dict,
    classified_files: list[ClassifiedFile],
    chunk_counts: Optional[dict[str, int]] = None,
) -> list[Chunk]:
    """Produce 1+ folder-structure chunks from the tree + classification results.

    *chunk_counts*: optional {source_path: num_chunks} from the processing step.
    """
    path_info: dict[str, dict] = {}
    for cf in classified_files:
        path_info[cf.path] = {
            "embed": cf.embed,
            "type": cf.processing_type.value,
            "sub_category": cf.sub_category,
        }

    annotated = _annotate_tree(folder_tree, path_info, chunk_counts or {})
    full_text = _render_tree(annotated)

    if not full_text.strip():
        return []

    if len(full_text) <= MAX_TREE_CHUNK_CHARS:
        return [_make_chunk(full_text)]

    return _split_tree_by_top_dirs(annotated)


def _annotate_tree(
    node: dict,
    path_info: dict[str, dict],
    chunk_counts: dict[str, int],
) -> dict:
    """Deep-copy tree and add embed/type/chunks annotation per file."""
    result: dict = {"name": node.get("name", ""), "children": [], "_files": []}
    for f in node.get("_files", []):
        fname = f.get("name", "")
        matched = None
        for p, info in path_info.items():
            if p.endswith("/" + fname) or p.endswith("\\" + fname):
                matched = info
                nc = chunk_counts.get(p, 0)
                break
        entry = dict(f)
        if matched:
            entry["embed"] = matched["embed"]
            entry["type"] = matched["type"]
            entry["chunks"] = nc
        else:
            entry["embed"] = False
            entry["type"] = "unknown"
            entry["chunks"] = 0
        result["_files"].append(entry)
    for child in node.get("children", []):
        result["children"].append(_annotate_tree(child, path_info, chunk_counts))
    return result


def _render_tree(node: dict, indent: int = 0) -> str:
    """Render annotated tree into human-readable text."""
    lines: list[str] = []
    prefix = "  " * indent
    name = node.get("name", "")
    if indent > 0:
        lines.append(f"{prefix}{name}/")
    for f in node.get("_files", []):
        embed_tag = "embed" if f.get("embed") else "skip"
        chunks_tag = f", {f['chunks']} chunks" if f.get("chunks") else ""
        ftype = f.get("type", "")
        lines.append(f"{prefix}  {f['name']}  ({f.get('size', '')}, {ftype}, {embed_tag}{chunks_tag})")
    for child in node.get("children", []):
        lines.append(_render_tree(child, indent + 1))
    return "\n".join(lines)


def _make_chunk(text: str) -> Chunk:
    return Chunk(
        id=str(uuid4()),
        source_path="__folder_structure__",
        processing_type=ProcessingType.FOLDER_STRUCTURE,
        text=f"FOLDER STRUCTURE\n\n{text}",
        metadata=ChunkMetadata(summary_scope="folder_structure"),
    )


def _split_tree_by_top_dirs(annotated: dict) -> list[Chunk]:
    """Split the annotated tree into multiple chunks by top-level directories."""
    chunks: list[Chunk] = []

    root_files_text = _render_tree({"name": annotated["name"], "children": [], "_files": annotated.get("_files", [])})
    if root_files_text.strip():
        buf = root_files_text
    else:
        buf = ""

    for child in annotated.get("children", []):
        child_text = _render_tree(child, indent=1)
        if buf and len(buf) + len(child_text) + 2 > MAX_TREE_CHUNK_CHARS:
            chunks.append(_make_chunk(buf))
            buf = child_text
        else:
            buf = (buf + "\n" + child_text) if buf else child_text

    if buf.strip():
        chunks.append(_make_chunk(buf))

    if not chunks:
        full = _render_tree(annotated)
        if full.strip():
            chunks.append(_make_chunk(full))

    logger.info("folder_structure_chunks_built", count=len(chunks))
    return chunks
