"""
KnowledgeBaseStore — compressed + encrypted knowledge base per team.

The KB blob now stores THREE layers of knowledge:
  1. Structural knowledge — LLM's project analysis (summary, tech stack,
     components, folder purposes, file descriptions, relationships)
  2. Content knowledge — full extracted text per file
  3. Chunk map — maps chunk IDs to file offsets for fast retrieval

Security model:
  1. All data is serialised as JSON.
  2. JSON is compressed with gzip (typically 60-70% reduction).
  3. Compressed bytes are encrypted with AES-256-GCM.
  4. The encrypted blob is stored as a single row in the knowledge_bases table.

Without the encryption key the data is unreadable even if the DB is stolen.
"""

import gzip
import json
import os
from datetime import datetime
from functools import lru_cache
from typing import Optional
from uuid import uuid4

import structlog
from sqlalchemy import select, delete

from app.db.database import async_session_maker
from app.rag.encryption import encrypt_text, decrypt_text
from app.rag.models import KnowledgeBase, TreeNode, ProjectAnalysis

logger = structlog.get_logger()

_kb_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 300


class KnowledgeBaseStore:
    """Build, persist, and query the compressed+encrypted knowledge base."""

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def build_and_store(
        self,
        team_id: str,
        trees: list[TreeNode],
        project_analysis: ProjectAnalysis,
        folder_analysis: dict[str, dict],
        file_analysis: dict[str, dict],
        file_texts: dict[str, dict],
        chunk_map: dict[str, dict],
    ) -> dict:
        """Build the KB JSON, compress, encrypt, and store.

        Args:
            team_id: Team this KB belongs to.
            trees: The full tree structures (pruned to kept items).
            project_analysis: LLM's project-level understanding.
            folder_analysis: {folder_path: {purpose, technologies, importance}}
            file_analysis: {file_path: {description, component, role, relationships, …}}
            file_texts: {file_path: {content_type, extraction_method, text, size_bytes, modified_at}}
            chunk_map: {chunk_id: {type, file, offset, length} or {type, folder} or {type}}
        """
        # Build the folder_structure from trees
        folder_structure = [self._tree_to_dict(t) for t in trees]

        kb_json = {
            "team_id": team_id,
            "version": 2,
            "created_at": datetime.utcnow().isoformat() + "Z",

            # Layer 1: LLM structural knowledge
            "project_analysis": project_analysis.model_dump(),
            "folder_analysis": folder_analysis,
            "file_analysis": file_analysis,

            # Layer 2: folder structure
            "folder_structure": folder_structure,

            # Layer 3: extracted text content
            "files": file_texts,

            # Layer 4: chunk index
            "chunk_map": chunk_map,
        }

        json_bytes = json.dumps(kb_json, ensure_ascii=False).encode("utf-8")
        total_text_bytes = sum(
            len(f.get("text", "").encode("utf-8"))
            for f in file_texts.values()
        )

        compressed = gzip.compress(json_bytes, compresslevel=6)

        encrypted = encrypt_text(compressed.decode("latin-1"))

        async with async_session_maker() as session:
            existing = await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.team_id == team_id)
            )
            old = existing.scalar_one_or_none()
            new_version = (old.version + 1) if old else 1

            if old:
                await session.execute(
                    delete(KnowledgeBase).where(KnowledgeBase.team_id == team_id)
                )

            kb = KnowledgeBase(
                kb_id=str(uuid4()),
                team_id=team_id,
                version=new_version,
                compressed_data=encrypted if isinstance(encrypted, bytes) else encrypted.encode("latin-1"),
                file_count=len(file_texts),
                total_text_bytes=total_text_bytes,
                compressed_bytes=len(compressed),
                created_at=datetime.utcnow(),
            )
            session.add(kb)
            await session.commit()

        _kb_cache.pop(team_id, None)

        compression_ratio = round(len(compressed) / max(len(json_bytes), 1) * 100, 1)
        stats = {
            "file_count": len(file_texts),
            "chunk_count": len(chunk_map),
            "total_text_bytes": total_text_bytes,
            "json_bytes": len(json_bytes),
            "compressed_bytes": len(compressed),
            "encrypted_bytes": len(kb.compressed_data),
            "compression_ratio": compression_ratio,
            "version": new_version,
            "analysis_files": len(file_analysis),
            "analysis_folders": len(folder_analysis),
        }
        logger.info("kb_stored", team_id=team_id, **stats)
        return stats

    def _tree_to_dict(self, node: TreeNode) -> dict:
        """Convert a TreeNode to a serialisable dict for the KB."""
        d: dict = {
            "name": node.name,
            "path": node.path,
            "is_file": node.is_file,
        }
        if node.is_file:
            d["ext"] = node.ext
            d["size_bytes"] = node.size_bytes
            d["content_type"] = node.content_type
            d["decision"] = node.decision
        else:
            d["decision"] = node.decision
            d["children"] = [self._tree_to_dict(c) for c in node.children]
        return d

    # ------------------------------------------------------------------
    # MCP Docs — lightweight KB from crawled/uploaded text
    # ------------------------------------------------------------------

    async def store_mcp_docs(
        self,
        team_id: str,
        pages: list[tuple[str, str]],
        api_name: str = "",
    ) -> dict:
        """Store crawled/uploaded API documentation as a searchable KB.

        This is a lightweight alternative to the full 4-phase training pipeline.
        No LLM analysis — just chunk the text and store for keyword retrieval.

        Args:
            team_id: The ID to store under (e.g., "__mcp__webull").
            pages: List of (page_title_or_url, page_text) tuples.
            api_name: Display name for the API.

        Returns:
            Stats dict with file_count, chunk_count, etc.
        """
        MAX_CHUNK_CHARS = 20_400

        file_texts: dict[str, dict] = {}
        file_analysis: dict[str, dict] = {}
        chunk_map: dict[str, dict] = {}

        for i, (title, text) in enumerate(pages):
            if not text.strip():
                continue

            file_key = f"doc/{title}" if not title.startswith("doc/") else title

            file_texts[file_key] = {
                "content_type": "text",
                "extraction_method": "direct",
                "text": text,
                "size_bytes": len(text.encode("utf-8")),
                "modified_at": datetime.utcnow().isoformat(),
            }

            file_analysis[file_key] = {
                "description": title,
                "component": "api_docs",
                "role": "documentation",
                "technologies": [],
                "relationships": [],
                "importance": "high",
            }

            offset = 0
            while offset < len(text):
                end = min(offset + MAX_CHUNK_CHARS, len(text))
                cid = str(uuid4())
                chunk_map[cid] = {
                    "type": "content",
                    "file": file_key,
                    "offset": offset,
                    "length": end - offset,
                }
                offset = end

        kb_json = {
            "team_id": team_id,
            "version": 2,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "project_analysis": {
                "summary": f"API documentation for {api_name}",
                "tech_stack": [],
                "key_components": [{"name": "api_docs", "description": f"{api_name} API documentation"}],
                "key_files": [{"path": k, "description": v.get("description", "")} for k, v in file_analysis.items()],
            },
            "folder_analysis": {
                "doc": {"purpose": "API documentation pages", "technologies": [], "importance": "high"}
            },
            "file_analysis": file_analysis,
            "folder_structure": [{
                "name": "doc",
                "path": "doc",
                "is_file": False,
                "decision": "keep",
                "children": [
                    {"name": title, "path": f"doc/{title}", "is_file": True, "ext": ".md",
                     "size_bytes": len(text.encode("utf-8")), "content_type": "text", "decision": "keep"}
                    for title, text in pages if text.strip()
                ],
            }],
            "files": file_texts,
            "chunk_map": chunk_map,
        }

        json_bytes = json.dumps(kb_json, ensure_ascii=False).encode("utf-8")
        total_text_bytes = sum(
            len(f.get("text", "").encode("utf-8"))
            for f in file_texts.values()
        )
        compressed = gzip.compress(json_bytes, compresslevel=6)
        encrypted = encrypt_text(compressed.decode("latin-1"))

        async with async_session_maker() as session:
            existing = await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.team_id == team_id)
            )
            old = existing.scalar_one_or_none()
            new_version = (old.version + 1) if old else 1

            if old:
                await session.execute(
                    delete(KnowledgeBase).where(KnowledgeBase.team_id == team_id)
                )

            kb = KnowledgeBase(
                kb_id=str(uuid4()),
                team_id=team_id,
                version=new_version,
                compressed_data=encrypted if isinstance(encrypted, bytes) else encrypted.encode("latin-1"),
                file_count=len(file_texts),
                total_text_bytes=total_text_bytes,
                compressed_bytes=len(compressed),
                created_at=datetime.utcnow(),
            )
            session.add(kb)
            await session.commit()

        _kb_cache.pop(team_id, None)

        stats = {
            "file_count": len(file_texts),
            "chunk_count": len(chunk_map),
            "total_text_bytes": total_text_bytes,
            "compressed_bytes": len(compressed),
            "version": new_version,
        }
        logger.info("mcp_kb_stored", team_id=team_id, **stats)
        return stats

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def load(self, team_id: str) -> Optional[dict]:
        """Load, decrypt, and decompress the KB for a team."""
        cached = _kb_cache.get(team_id)
        if cached:
            data, ts = cached
            if (datetime.utcnow().timestamp() - ts) < _CACHE_TTL_SECONDS:
                return data

        async with async_session_maker() as session:
            result = await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.team_id == team_id)
            )
            kb = result.scalar_one_or_none()

        if not kb:
            return None

        try:
            raw_encrypted = kb.compressed_data
            if isinstance(raw_encrypted, memoryview):
                raw_encrypted = bytes(raw_encrypted)
            decrypted_latin = decrypt_text(raw_encrypted)
            compressed = decrypted_latin.encode("latin-1")
            json_bytes = gzip.decompress(compressed)
            data = json.loads(json_bytes.decode("utf-8"))
        except Exception as exc:
            logger.error("kb_decrypt_failed", team_id=team_id, error=str(exc))
            return None

        _kb_cache[team_id] = (data, datetime.utcnow().timestamp())
        return data

    async def get_chunk_texts(self, team_id: str, chunk_ids: list[str]) -> dict[str, str]:
        """Return {chunk_id: text} for the requested chunks from the KB blob.

        Handles all chunk types:
          - content chunks → extract file text at offset/length
          - file_analysis chunks → return file description + metadata
          - folder_analysis chunks → return folder purpose + metadata
          - project_analysis chunks → return project summary
        """
        if not chunk_ids:
            return {}

        kb_data = await self.load(team_id)
        if not kb_data:
            return {}

        chunk_map = kb_data.get("chunk_map", {})
        files = kb_data.get("files", {})
        file_analysis = kb_data.get("file_analysis", {})
        folder_analysis = kb_data.get("folder_analysis", {})
        project_analysis = kb_data.get("project_analysis", {})
        result: dict[str, str] = {}

        for cid in chunk_ids:
            mapping = chunk_map.get(cid)
            if not mapping:
                continue

            chunk_type = mapping.get("type", "content")

            if chunk_type == "content":
                file_path = mapping.get("file", "")
                offset = mapping.get("offset", 0)
                length = mapping.get("length", 0)
                file_data = files.get(file_path, {})
                full_text = file_data.get("text", "")
                result[cid] = full_text[offset:offset + length]

            elif chunk_type == "file_analysis":
                file_path = mapping.get("file", "")
                info = file_analysis.get(file_path, {})
                if info:
                    result[cid] = (
                        f"FILE ANALYSIS: {file_path}\n"
                        f"Description: {info.get('description', '')}\n"
                        f"Component: {info.get('component', '')}\n"
                        f"Role: {info.get('role', '')}\n"
                        f"Technologies: {', '.join(info.get('technologies', []))}\n"
                        f"Importance: {info.get('importance', '')}\n"
                        f"Relationships: {', '.join(info.get('relationships', []))}"
                    )

            elif chunk_type == "folder_analysis":
                folder_path = mapping.get("folder", "")
                info = folder_analysis.get(folder_path, {})
                if info:
                    result[cid] = (
                        f"FOLDER ANALYSIS: {folder_path}\n"
                        f"Purpose: {info.get('purpose', '')}\n"
                        f"Technologies: {', '.join(info.get('technologies', []))}\n"
                        f"Importance: {info.get('importance', '')}"
                    )

            elif chunk_type == "project_analysis":
                if project_analysis:
                    components_text = ""
                    for comp in project_analysis.get("key_components", []):
                        if isinstance(comp, dict):
                            components_text += (
                                f"\n  - {comp.get('name', '')}: "
                                f"{comp.get('description', '')}"
                            )
                    result[cid] = (
                        f"PROJECT ANALYSIS\n"
                        f"Summary: {project_analysis.get('summary', '')}\n"
                        f"Technologies: {', '.join(project_analysis.get('technologies', []))}\n"
                        f"Architecture: {project_analysis.get('architecture_style', '')}\n"
                        f"Key Components:{components_text}"
                    )

        return result

    async def get_kb_info(self, team_id: str) -> Optional[dict]:
        """Return KB metadata without loading the full blob."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.team_id == team_id)
            )
            kb = result.scalar_one_or_none()

        if not kb:
            return None

        return {
            "kb_id": kb.kb_id,
            "team_id": kb.team_id,
            "version": kb.version,
            "file_count": kb.file_count,
            "total_text_bytes": kb.total_text_bytes,
            "compressed_bytes": kb.compressed_bytes,
            "created_at": kb.created_at.isoformat() if kb.created_at else None,
        }

    async def delete_team(self, team_id: str) -> bool:
        """Delete the KB for a team."""
        async with async_session_maker() as session:
            await session.execute(
                delete(KnowledgeBase).where(KnowledgeBase.team_id == team_id)
            )
            await session.commit()
        _kb_cache.pop(team_id, None)
        logger.info("kb_deleted", team_id=team_id)
        return True
