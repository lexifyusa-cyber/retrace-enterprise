"""
FileSummarizerService – LLM-driven enrichment of chunks with semantic metadata.

Three responsibilities:
  1. **Per-file enrichment**: given all chunks from a single file, ask the LLM
     for a one-line description, component name, features, and key concepts.
     Stamp those onto every chunk *and* produce one ``summary`` chunk per file.
  2. **Per-sub-category summaries**: given file summaries grouped by sub_category,
     produce one summary chunk per sub-category.
  3. **Per-group summaries**: given file summaries for an entire folder group,
     produce an architectural overview chunk.
"""

import json
from collections import defaultdict
from typing import Optional
from uuid import uuid4

import structlog
from openai import AsyncOpenAI

from app.rag.models import Chunk, ChunkMetadata, ProcessingType

logger = structlog.get_logger()

# Maximum files per summarization batch
_SUMMARIZE_BATCH = 40

_ENRICHMENT_FIELDS = {"description", "component", "features", "concepts"}

_FILE_ENRICH_SYSTEM = """\
You are a code/document analyst for a knowledge management system.

You will receive file paths and text samples from one or more files.
For EACH file, return a JSON object with exactly these keys:
{
  "description": "One-line summary of what this file does / contains",
  "component": "The component, module, or subsystem this belongs to (e.g. authentication, payments, database, frontend, CI/CD). Use lowercase.",
  "features": "Comma-separated feature areas (e.g. login,token-refresh,password-reset)",
  "concepts": "Comma-separated key technical concepts (e.g. JWT,OAuth,bcrypt,middleware)"
}

If you receive MULTIPLE files, return a JSON object with a "files" key containing
an array of objects, one per file, in the same order:
{"files": [{"description": "...", "component": "...", "features": "...", "concepts": "..."}, ...]}

If you receive a SINGLE file, you may return just the single object directly:
{"description": "...", "component": "...", "features": "...", "concepts": "..."}

Rules:
- Be concise: description ≤ 120 chars, component is a single word or short phrase.
- If you cannot determine a field, use an empty string.
- Return ONLY valid JSON. No markdown, no explanation.
"""

_GROUP_SUMMARY_SYSTEM = """\
You are a technical writer for a knowledge management system.

You will receive a list of file summaries from a single folder group
(a logical grouping of files such as a code module, documentation set,
or ticket collection).

Write a **2–3 paragraph architectural summary** that covers:
- What this group of files does as a whole
- Key components, services, or concepts involved
- How the parts relate to each other
- Any notable patterns, frameworks, or technologies used

Be factual and specific. Reference file names where helpful.
"""

_SUBCATEGORY_SUMMARY_SYSTEM = """\
You are a technical writer for a knowledge management system.

You will receive file summaries that all belong to the same content
sub-category (e.g. all "user-guide" docs, or all "backend" code files).

Write a concise **1–2 paragraph summary** covering:
- What this sub-category of content covers as a whole
- Key topics, features, or areas addressed
- How the files relate to each other

Be factual and specific.
"""


class FileSummarizerService:
    """Enrich chunks with semantic metadata and produce summary chunks."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        kw: dict = {"api_key": api_key}
        if base_url:
            kw["base_url"] = base_url
        self._client = AsyncOpenAI(**kw)
        self._model = model

    # ------------------------------------------------------------------
    # 1. Per-file enrichment
    # ------------------------------------------------------------------

    async def enrich_chunks(self, chunks: list[Chunk]) -> list[Chunk]:
        """Enrich chunks with component/feature/concept metadata.

        Groups chunks by source_path, calls the LLM once per batch of files
        (with a text sample), and stamps the returned metadata onto every chunk.
        Also appends one ``summary`` chunk per file.

        Returns the enriched chunk list (original chunks + new summary chunks).
        """
        if not chunks:
            return chunks

        # Group chunks by file
        by_file: dict[str, list[Chunk]] = defaultdict(list)
        for c in chunks:
            by_file[c.source_path].append(c)

        # Batch files for LLM calls
        file_paths = list(by_file.keys())
        enrichments: dict[str, dict] = {}

        for batch_start in range(0, len(file_paths), _SUMMARIZE_BATCH):
            batch_paths = file_paths[batch_start : batch_start + _SUMMARIZE_BATCH]
            batch_results = await self._enrich_batch(batch_paths, by_file)
            enrichments.update(batch_results)

        # Apply enrichments to chunks and create summary chunks
        summary_chunks: list[Chunk] = []

        for path, file_chunks in by_file.items():
            info = enrichments.get(path, {})
            component = info.get("component", "") or ""
            features = info.get("features", "") or ""
            concepts = info.get("concepts", "") or ""
            description = info.get("description", "") or ""

            # Stamp metadata onto every chunk from this file
            for c in file_chunks:
                c.metadata.component = component or c.metadata.component
                c.metadata.feature = features or c.metadata.feature
                c.metadata.concepts = concepts or c.metadata.concepts
                c.metadata.file_description = description or c.metadata.file_description

            # Create a file-level summary chunk
            if description:
                ptype = file_chunks[0].processing_type if file_chunks else ProcessingType.OTHER
                sub_cat = file_chunks[0].metadata.sub_category or "general" if file_chunks else "general"
                summary_text = (
                    f"FILE: {path}\n"
                    f"TYPE: {ptype.value}\n"
                    f"SUB-CATEGORY: {sub_cat}\n"
                    f"DESCRIPTION: {description}\n"
                    f"COMPONENT: {component}\n"
                    f"FEATURES: {features}\n"
                    f"CONCEPTS: {concepts}\n\n"
                    f"This file is part of the '{component}' component. "
                    f"It {description[0].lower()}{description[1:] if len(description) > 1 else ''}"
                )
                summary_chunks.append(
                    Chunk(
                        id=str(uuid4()),
                        source_path=path,
                        processing_type=ProcessingType.SUMMARY,
                        text=summary_text,
                        metadata=ChunkMetadata(
                            component=component,
                            feature=features,
                            concepts=concepts,
                            sub_category=sub_cat,
                            file_description=description,
                            summary_scope="file",
                        ),
                    )
                )

        logger.info(
            "file_enrichment_done",
            files=len(by_file),
            summary_chunks=len(summary_chunks),
        )
        return chunks + summary_chunks

    async def _enrich_batch(
        self,
        paths: list[str],
        by_file: dict[str, list[Chunk]],
    ) -> dict[str, dict]:
        """Call LLM for a batch of files and return {path: enrichment_dict}."""
        # Build the user prompt with file samples
        entries: list[str] = []
        for i, path in enumerate(paths):
            file_chunks = by_file[path]
            # Take a sample of text (first chunk, up to 500 chars)
            sample = file_chunks[0].text[:500] if file_chunks else ""
            entries.append(json.dumps({
                "index": i,
                "path": path,
                "sample": sample,
            }))

        user_msg = (
            f"Analyze these {len(paths)} files and return enrichment data "
            f"for each file:\n\n"
            + "\n".join(entries)
        )

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _FILE_ENRICH_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(raw)

            if isinstance(parsed, dict):
                # Case 1: wrapper object with a list ({"files": [...], "results": [...]})
                for key in ("files", "results", "items", "data"):
                    if key in parsed and isinstance(parsed[key], list):
                        items = parsed[key]
                        result = {}
                        for i, item in enumerate(items):
                            if i < len(paths) and isinstance(item, dict):
                                result[paths[i]] = item
                        return result

                # Case 2: single-file response — dict has enrichment fields directly
                if _ENRICHMENT_FIELDS.intersection(parsed.keys()):
                    if len(paths) == 1:
                        return {paths[0]: parsed}
                    # Multiple paths but got a single dict — use for first path
                    logger.warning("enrichment_single_dict_for_multi", files=len(paths))
                    return {paths[0]: parsed}

                logger.warning("enrichment_unexpected_dict", keys=list(parsed.keys()))

            elif isinstance(parsed, list):
                result = {}
                for i, item in enumerate(parsed):
                    if i < len(paths) and isinstance(item, dict):
                        result[paths[i]] = item
                return result

        except Exception as exc:
            logger.error("file_enrichment_batch_failed", error=str(exc), files=len(paths))

        # Fallback: return empty enrichments
        return {p: {} for p in paths}

    # ------------------------------------------------------------------
    # 2. Per-sub-category summaries
    # ------------------------------------------------------------------

    async def generate_subcategory_summaries(
        self,
        file_descriptions: dict[str, str],
        file_subcategories: dict[str, str],
    ) -> list[Chunk]:
        """Produce one summary chunk per distinct sub_category.

        *file_descriptions*: {path: description}
        *file_subcategories*: {path: sub_category}
        """
        if not file_descriptions:
            return []

        # Group by sub_category
        by_subcat: dict[str, dict[str, str]] = defaultdict(dict)
        for path, desc in file_descriptions.items():
            subcat = file_subcategories.get(path, "general")
            by_subcat[subcat][path] = desc

        summary_chunks: list[Chunk] = []

        for subcat, files in by_subcat.items():
            if len(files) < 2:
                # Skip sub-categories with only 1 file (file summary is enough)
                continue

            listing = "\n".join(
                f"- {path}: {desc}" for path, desc in list(files.items())[:60]
            )
            if len(files) > 60:
                listing += f"\n... and {len(files) - 60} more files"

            user_msg = (
                f"Sub-category: {subcat}\n"
                f"Files ({len(files)}):\n{listing}"
            )

            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _SUBCATEGORY_SUMMARY_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.2,
                    max_tokens=600,
                )
                summary_text = response.choices[0].message.content or ""
                if not summary_text.strip():
                    continue

                full_text = (
                    f"SUB-CATEGORY: {subcat}\n"
                    f"FILES: {len(files)}\n\n"
                    f"{summary_text}"
                )

                summary_chunks.append(
                    Chunk(
                        id=str(uuid4()),
                        source_path=f"__subcategory__/{subcat}",
                        processing_type=ProcessingType.SUMMARY,
                        text=full_text,
                        metadata=ChunkMetadata(
                            sub_category=subcat,
                            summary_scope="sub_category",
                        ),
                    )
                )
            except Exception as exc:
                logger.error("subcategory_summary_failed", subcat=subcat, error=str(exc))

        logger.info("subcategory_summaries_done", count=len(summary_chunks))
        return summary_chunks

    # ------------------------------------------------------------------
    # 3. Folder-group-level summary
    # ------------------------------------------------------------------

    async def generate_group_summary(
        self,
        group_name: str,
        group_type: str,
        file_summaries: dict[str, str],
    ) -> Optional[Chunk]:
        """Produce a single architectural summary chunk for a folder group.

        *file_summaries*: {file_path: description} from the enrichment step.
        """
        if not file_summaries:
            return None

        listing = "\n".join(
            f"- {path}: {desc}" for path, desc in list(file_summaries.items())[:80]
        )
        if len(file_summaries) > 80:
            listing += f"\n... and {len(file_summaries) - 80} more files"

        user_msg = (
            f"Folder group: {group_name} (type: {group_type})\n"
            f"Files ({len(file_summaries)}):\n{listing}"
        )

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _GROUP_SUMMARY_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_tokens=800,
            )
            summary_text = response.choices[0].message.content or ""
            if not summary_text.strip():
                return None

            full_text = (
                f"GROUP: {group_name}\n"
                f"TYPE: {group_type}\n"
                f"FILES: {len(file_summaries)}\n\n"
                f"{summary_text}"
            )

            return Chunk(
                id=str(uuid4()),
                source_path=f"__group__/{group_name}",
                processing_type=ProcessingType.SUMMARY,
                text=full_text,
                metadata=ChunkMetadata(
                    component=group_name.lower().replace(" ", "-"),
                    summary_scope="group",
                ),
            )

        except Exception as exc:
            logger.error("group_summary_failed", group=group_name, error=str(exc))
            return None
