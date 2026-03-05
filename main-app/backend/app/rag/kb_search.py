"""
KB Search — LLM-expanded semantic search over the encrypted knowledge base.

Replaces the old embedding/ChromaDB retrieval with a three-stage approach:
  1. LLM Query Expansion — turn user question into keywords, components,
     file patterns grounded in the KB's actual vocabulary.
  2. Parallel Search — scan file analysis, folder analysis, and full text
     concurrently (all in-memory from the cached KB blob).
  3. Merge + Rank — score, deduplicate, and return the top-K file texts
     with their structural metadata.
"""

import asyncio
import json
import re
from pathlib import PurePosixPath
from typing import Optional

import structlog
from openai import AsyncOpenAI

from app.rag.kb_store import KnowledgeBaseStore

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Query expansion prompt
# ---------------------------------------------------------------------------

_EXPANSION_SYSTEM = """\
You are a search query expander for a software knowledge base.

The knowledge base contains structured information about a software product:
- Project summary and architecture
- Per-folder purpose descriptions
- Per-file descriptions with component, role, technologies, relationships
- Full extracted text of source files

Your job: given a user question, produce a JSON search plan that maximizes
recall by expanding the question into terms that would match the KB content.

COMPONENTS in this product: {components}
TECHNOLOGIES in this product: {technologies}

Return ONLY a JSON object with these fields:
{{
  "keywords": ["5-15 terms: original terms + synonyms + related concepts that might appear in code, docs, file descriptions, or folder purposes"],
  "components": ["1-5 component names from the list above most likely to contain the answer, or empty if unclear"],
  "file_patterns": ["3-8 lowercase glob-like substrings for file/folder names, e.g. auth, pass, login, credential"],
  "intent": "broad or specific"
}}

Rules:
- keywords MUST include the original question terms AND semantic relatives.
  Example: "credentials" → ["credentials", "password", "auth", "authentication",
  "login", "secret", "token", "api key", "oauth", "session"]
- components should only include names from the COMPONENTS list above.
- file_patterns are simple lowercase substrings, not full globs.
- intent is "broad" for overview/architecture questions, "specific" for targeted lookups.
- Return ONLY valid JSON. No markdown fences, no explanation.
"""

_EXPANSION_FALLBACK_KEYWORDS_COUNT = 5


# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

_W_FILE_ANALYSIS = 3.0
_W_FOLDER_MATCH = 2.0
_W_TEXT_MATCH = 1.0

_IMPORTANCE_BOOST = {
    "critical": 2.0,
    "high": 1.5,
    "medium": 1.0,
    "low": 0.7,
}

_COMPONENT_BOOST = 2.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def search_kb(
    query: str,
    product_id: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
    top_k: int = 10,
) -> str:
    """Search the knowledge base and return formatted results.

    This is the main entry point called by the agent tool.
    """
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return f"No knowledge base found for this product. Train the product first."

    project_analysis = kb_data.get("project_analysis", {})
    file_analysis: dict = kb_data.get("file_analysis", {})
    folder_analysis: dict = kb_data.get("folder_analysis", {})
    files: dict = kb_data.get("files", {})

    if not file_analysis and not files:
        return "Knowledge base is empty — no files were indexed during training."

    # -- Stage 1: LLM query expansion --
    components = _extract_component_names(project_analysis, file_analysis)
    technologies = project_analysis.get("technologies", [])

    expansion = await _expand_query(
        query=query,
        components=components,
        technologies=technologies,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )

    keywords = expansion.get("keywords", [query])
    target_components = expansion.get("components", [])
    file_patterns = expansion.get("file_patterns", [])
    intent = expansion.get("intent", "broad")

    logger.info(
        "kb_search_expansion",
        query=query[:80],
        keywords=keywords[:10],
        components=target_components,
        file_patterns=file_patterns,
        intent=intent,
    )

    # -- Stage 2: Parallel search --
    scores_a, scores_b, scores_c = await asyncio.gather(
        _search_file_analysis(file_analysis, keywords, target_components, file_patterns),
        _search_folder_analysis(folder_analysis, file_analysis, keywords, file_patterns),
        _search_file_text(files, keywords),
    )

    # -- Stage 3: Merge + Rank --
    all_paths = set(scores_a) | set(scores_b) | set(scores_c)
    if not all_paths:
        return f"No relevant information found in knowledge base for: {query}"

    ranked: list[tuple[str, float]] = []
    for path in all_paths:
        score = (
            scores_a.get(path, 0.0) * _W_FILE_ANALYSIS
            + scores_b.get(path, 0.0) * _W_FOLDER_MATCH
            + scores_c.get(path, 0.0) * _W_TEXT_MATCH
        )

        fa = file_analysis.get(path, {})
        importance = fa.get("importance", "medium")
        score *= _IMPORTANCE_BOOST.get(importance, 1.0)

        file_component = (fa.get("component") or "").lower()
        if file_component and file_component in [c.lower() for c in target_components]:
            score *= _COMPONENT_BOOST

        ranked.append((path, score))

    ranked.sort(key=lambda x: x[1], reverse=True)
    top_paths = ranked[:top_k]

    # -- Stage 4: Assemble response --
    return _format_results(
        query=query,
        top_paths=top_paths,
        project_analysis=project_analysis,
        file_analysis=file_analysis,
        folder_analysis=folder_analysis,
        files=files,
        intent=intent,
    )


# ---------------------------------------------------------------------------
# Stage 1: LLM query expansion
# ---------------------------------------------------------------------------

async def _expand_query(
    query: str,
    components: list[str],
    technologies: list[str],
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
) -> dict:
    """Call the LLM to expand the user query into keywords, components, and patterns."""
    client_kwargs: dict = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = AsyncOpenAI(**client_kwargs)

    system_prompt = _EXPANSION_SYSTEM.format(
        components=", ".join(components[:30]) if components else "(none identified)",
        technologies=", ".join(technologies[:20]) if technologies else "(none identified)",
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"User question: {query}"},
            ],
            temperature=0,
            max_tokens=500,
        )
        raw = response.choices[0].message.content or "{}"
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)

        if not isinstance(data.get("keywords"), list) or not data["keywords"]:
            data["keywords"] = _basic_keywords(query)
        if not isinstance(data.get("components"), list):
            data["components"] = []
        if not isinstance(data.get("file_patterns"), list):
            data["file_patterns"] = []
        data["intent"] = data.get("intent", "broad")

        return data

    except Exception as exc:
        logger.warning("kb_query_expansion_failed", error=str(exc), query=query[:80])
        return {
            "keywords": _basic_keywords(query),
            "components": [],
            "file_patterns": [],
            "intent": "broad",
        }


def _basic_keywords(query: str) -> list[str]:
    """Fallback keyword extraction when LLM expansion fails."""
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                  "being", "have", "has", "had", "do", "does", "did", "will",
                  "would", "could", "should", "may", "might", "shall", "can",
                  "to", "of", "in", "for", "on", "with", "at", "by", "from",
                  "as", "into", "through", "during", "before", "after",
                  "about", "between", "under", "above", "and", "but", "or",
                  "not", "no", "so", "if", "then", "than", "too", "very",
                  "just", "that", "this", "these", "those", "what", "which",
                  "who", "whom", "how", "where", "when", "why", "all", "each",
                  "every", "both", "few", "more", "most", "some", "any", "it",
                  "its", "my", "your", "his", "her", "our", "their", "me",
                  "him", "us", "them", "i", "you", "he", "she", "we", "they"}
    words = re.findall(r"[a-zA-Z0-9_]+", query.lower())
    return [w for w in words if w not in stop_words and len(w) > 1][:_EXPANSION_FALLBACK_KEYWORDS_COUNT]


def _extract_component_names(project_analysis: dict, file_analysis: dict) -> list[str]:
    """Collect distinct component names from the KB data."""
    components: set[str] = set()
    for comp in project_analysis.get("key_components", []):
        if isinstance(comp, dict) and comp.get("name"):
            components.add(comp["name"])
    for fa in file_analysis.values():
        comp = fa.get("component", "")
        if comp and comp.strip():
            components.add(comp.strip())
    return sorted(components)


# ---------------------------------------------------------------------------
# Stage 2: Parallel search functions
# ---------------------------------------------------------------------------

def _kw_score(text: str, keywords: list[str]) -> float:
    """Score how many keywords appear in text (case-insensitive). Returns 0.0-1.0."""
    if not text or not keywords:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for kw in keywords if kw.lower() in text_lower)
    return hits / len(keywords)


def _pattern_match(name: str, patterns: list[str]) -> bool:
    """Check if any pattern substring appears in the filename (case-insensitive)."""
    name_lower = name.lower()
    return any(p.lower() in name_lower for p in patterns)


async def _search_file_analysis(
    file_analysis: dict,
    keywords: list[str],
    target_components: list[str],
    file_patterns: list[str],
) -> dict[str, float]:
    """Search file analysis metadata for keyword matches. Returns {path: score}."""
    scores: dict[str, float] = {}
    target_comp_lower = [c.lower() for c in target_components]

    for path, fa in file_analysis.items():
        filename = PurePosixPath(path).name

        desc_score = _kw_score(fa.get("description", ""), keywords)
        role_score = _kw_score(fa.get("role", ""), keywords)
        tech_text = ", ".join(fa.get("technologies", []))
        tech_score = _kw_score(tech_text, keywords)
        rel_text = ", ".join(fa.get("relationships", []))
        rel_score = _kw_score(rel_text, keywords)

        comp = (fa.get("component") or "").lower()
        comp_match = 1.0 if comp and comp in target_comp_lower else 0.0

        name_match = 1.0 if _pattern_match(filename, file_patterns) else 0.0
        path_match = 1.0 if _pattern_match(path, file_patterns) else 0.0

        total = (
            desc_score * 3.0
            + role_score * 2.0
            + comp_match * 2.5
            + tech_score * 1.0
            + rel_score * 1.0
            + name_match * 2.0
            + path_match * 1.5
        )
        if total > 0:
            scores[path] = total

    return scores


async def _search_folder_analysis(
    folder_analysis: dict,
    file_analysis: dict,
    keywords: list[str],
    file_patterns: list[str],
) -> dict[str, float]:
    """Search folder purposes and propagate scores to files within matched folders."""
    folder_scores: dict[str, float] = {}
    for folder_path, fa in folder_analysis.items():
        purpose_score = _kw_score(fa.get("purpose", ""), keywords)
        tech_text = ", ".join(fa.get("technologies", []))
        tech_score = _kw_score(tech_text, keywords)
        name_match = 1.0 if _pattern_match(folder_path, file_patterns) else 0.0

        total = purpose_score * 3.0 + tech_score * 1.0 + name_match * 2.0
        if total > 0:
            folder_scores[folder_path] = total

    if not folder_scores:
        return {}

    file_scores: dict[str, float] = {}
    for file_path in file_analysis:
        for folder_path, fscore in folder_scores.items():
            if file_path.startswith(folder_path):
                file_scores[file_path] = max(file_scores.get(file_path, 0.0), fscore)

    return file_scores


async def _search_file_text(
    files: dict,
    keywords: list[str],
) -> dict[str, float]:
    """Search actual file text content for keyword matches."""
    scores: dict[str, float] = {}
    for path, fdata in files.items():
        text = fdata.get("text", "")
        if not text:
            continue
        score = _kw_score(text, keywords)
        if score > 0:
            scores[path] = score
    return scores


# ---------------------------------------------------------------------------
# Stage 4: Format results
# ---------------------------------------------------------------------------

_MAX_TEXT_PER_FILE = 12_000
_MAX_TOTAL_TEXT = 60_000


def _format_results(
    query: str,
    top_paths: list[tuple[str, float]],
    project_analysis: dict,
    file_analysis: dict,
    folder_analysis: dict,
    files: dict,
    intent: str,
) -> str:
    """Build the formatted response string for the agent."""
    parts: list[str] = []

    if intent == "broad" and project_analysis:
        components_text = ""
        for comp in project_analysis.get("key_components", []):
            if isinstance(comp, dict):
                components_text += f"\n  - {comp.get('name', '')}: {comp.get('description', '')}"
        parts.append(
            f"PROJECT OVERVIEW\n"
            f"Summary: {project_analysis.get('summary', '')}\n"
            f"Technologies: {', '.join(project_analysis.get('technologies', []))}\n"
            f"Architecture: {project_analysis.get('architecture_style', '')}\n"
            f"Key Components:{components_text}\n"
        )

    total_text = 0
    for i, (path, score) in enumerate(top_paths, 1):
        if total_text >= _MAX_TOTAL_TEXT:
            parts.append(f"\n[{len(top_paths) - i + 1} more results truncated for context length]")
            break

        fa = file_analysis.get(path, {})
        fdata = files.get(path, {})
        text = fdata.get("text", "")

        header = f"[{i}] {path} (score: {score:.2f})"
        if fa.get("component"):
            header += f" | component: {fa['component']}"
        if fa.get("importance"):
            header += f" | importance: {fa['importance']}"

        meta_lines = []
        if fa.get("description"):
            meta_lines.append(f"  Description: {fa['description']}")
        if fa.get("role"):
            meta_lines.append(f"  Role: {fa['role']}")
        if fa.get("technologies"):
            meta_lines.append(f"  Technologies: {', '.join(fa['technologies'])}")
        if fa.get("relationships"):
            meta_lines.append(f"  Relationships: {', '.join(fa['relationships'])}")

        content = text[:_MAX_TEXT_PER_FILE]
        if len(text) > _MAX_TEXT_PER_FILE:
            content += "\n[... content truncated ...]"

        entry = header + "\n" + "\n".join(meta_lines)
        if content.strip():
            entry += "\n--- content ---\n" + content

        parts.append(entry)
        total_text += len(content)

    if not parts:
        return f"No relevant information found in knowledge base for: {query}"

    return f"Found {len(top_paths)} relevant result(s) for '{query}':\n\n" + "\n\n".join(parts)
