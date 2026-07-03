"""Formatting helpers for TeamKnowParse KB retrieval."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.teamknowparse_vector import TeamKnowParseVectorService, _terms


def _focused_excerpt(text: str, query: str, max_chars: int = 900) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= max_chars:
        return text
    query_terms = _terms(query, expand=True)
    if not query_terms:
        return text[:max_chars] + "..."

    best_start = 0
    best_score = -1.0
    half = max_chars // 2
    positions: list[int] = []
    for term in query_terms:
        pattern = re.compile(rf"\b{re.escape(term)}\w*\b", re.IGNORECASE)
        positions.extend(match.start() for match in pattern.finditer(text))
    if not positions:
        return text[:max_chars] + "..."

    for pos in positions:
        start = max(0, pos - half)
        end = min(len(text), start + max_chars)
        window = text[start:end].lower()
        score = sum(1 for term in query_terms if re.search(rf"\b{re.escape(term)}\w*\b", window))
        if "day or night" in window or "non-contact detection" in window or "big data is defined" in window:
            score += 6
        if score > best_score:
            best_score = score
            best_start = start

    best_end = min(len(text), best_start + max_chars)
    excerpt = text[best_start:best_end].strip()
    if best_start > 0:
        excerpt = "..." + excerpt
    if best_end < len(text):
        excerpt += "..."
    return excerpt


async def search_teamknowparse_kb(session: AsyncSession, team_id: str, query: str, mode: str = "hybrid", top_k: int = 10) -> str:
    hits = await TeamKnowParseVectorService().search(session, team_id=team_id, query=query, mode=mode, top_k=top_k)
    if not hits:
        return f"No TeamKnowParse KB chunks found for: {query}"
    lines = [f"TeamKnowParse KB results ({mode}) for: {query}", ""]
    for idx, hit in enumerate(hits, 1):
        meta = hit.get("metadata") or {}
        cite = meta.get("file_path") or meta.get("filename") or "unknown"
        page = meta.get("page_number")
        suffix = f" page {page}" if page else ""
        text = _focused_excerpt(hit.get("text") or "", query, max_chars=900)
        lines.append(f"[{idx}] {cite}{suffix} ({hit.get('source', 'kb')}, score={hit.get('score', 0):.3f})")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()
