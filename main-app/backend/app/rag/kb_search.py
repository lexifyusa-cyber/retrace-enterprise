"""
KB Search — Enterprise retrieval engine (Cursor-style, zero embeddings).

Five retrieval strategies fused with Reciprocal Rank Fusion (RRF):
  0. Structural Registry — exact match on skill/component names extracted
     from the KB (like checking a table of contents / `glob skills/*/SKILL.md`).
  1. Direct Path Lookup — files whose paths contain query terms get
     guaranteed inclusion (like `grep -l` finding the obvious file).
  2. Multi-Query Keyword Search — LLM generates 3 query reformulations;
     keyword search runs for each against file analysis, folder analysis,
     and full text (like a human trying different grep patterns).
  3. RRF Fusion — merge all rankings into a single ordered list.
  4. LLM Re-Ranking — an LLM pass re-scores the top candidates for
     the actual user question, filtering noise (like a human reading
     grep output and picking the right file).

No embeddings, no vector DB. All data comes from the in-memory KB blob.
"""

import asyncio
import json
import re
import yaml
from pathlib import PurePosixPath
from typing import Optional

import structlog
from openai import AsyncOpenAI

from app.rag.kb_store import KnowledgeBaseStore

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_W_FILE_ANALYSIS = 3.0
_W_FOLDER_MATCH = 2.0
_W_TEXT_MATCH = 1.0

_IMPORTANCE_BOOST = {"critical": 2.0, "high": 1.5, "medium": 1.0, "low": 0.7}
_COMPONENT_BOOST = 2.0
_PATH_BOOST_MAX = 4.0

_NOISY_FILE_PATTERNS = [
    "changelog", "changes", "history", "package-lock", "yarn.lock",
    "pnpm-lock", "poetry.lock", "license", "licence", "authors",
    "contributors", "codeowners",
]
_NOISY_FILE_PENALTY = 0.3

_RRF_K = 60  # standard RRF constant

_MAX_TEXT_PER_FILE = 12_000
_MAX_TOTAL_TEXT = 60_000

_EXPANSION_FALLBACK_KEYWORDS_COUNT = 5

# Cache for structural registries (product_id → registry dict)
_registry_cache: dict[str, tuple[dict, float]] = {}
_REGISTRY_CACHE_TTL = 300

# ---------------------------------------------------------------------------
# Multi-query expansion prompt
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
FILE NAMES in this product (sample): {file_types}

Return ONLY a JSON object with these fields:
{{
  "keywords": ["5-15 terms: original terms + synonyms + related concepts that might appear in code, docs, file descriptions, or folder purposes"],
  "components": ["1-5 component names from the list above most likely to contain the answer, or empty if unclear"],
  "file_patterns": ["3-8 lowercase glob-like substrings for file/folder names, e.g. auth, pass, login, credential"],
  "intent": "broad or specific",
  "alternative_queries": ["2-3 reformulations of the question using different vocabulary and angles, to catch files that use different terminology"]
}}

Rules:
- keywords MUST include the original question terms AND semantic relatives.
  Example: "credentials" → ["credentials", "password", "auth", "authentication",
  "login", "secret", "token", "api key", "oauth", "session"]
- components should only include names from the COMPONENTS list above.
- file_patterns are simple lowercase substrings, not full globs.
  IMPORTANT: include substrings that would match the canonical file for the topic.
  For example, if the user asks about a "skill" named "goplaces", include both
  "goplaces" and "skill" so that files like skills/goplaces/SKILL.md are found.
  Look at the FILE NAMES list for common definition files (e.g. SKILL.md,
  README.md, config.yaml, etc.) and include relevant ones.
- alternative_queries should rephrase the question using synonyms, related
  concepts, or different angles. For example: "skills for reviewing blogs" →
  ["blog monitoring tool", "RSS feed watcher skill", "blog tracking plugin"].
  Think about what DIFFERENT words the actual files might use.
- intent is "broad" for overview/architecture questions, "specific" for targeted lookups.
- Return ONLY valid JSON. No markdown fences, no explanation.
"""

# ---------------------------------------------------------------------------
# LLM Re-ranking prompt
# ---------------------------------------------------------------------------

_RERANK_SYSTEM = """\
You are a search result re-ranker. Given a user question and a list of
candidate files from a knowledge base, select the most relevant files
and rank them by relevance.

For each candidate you receive: file path, description, and a short
content preview. Judge relevance based on:
1. Does this file DIRECTLY answer the question?
2. Is this the CANONICAL/PRIMARY source for the topic (e.g. a SKILL.md
   file for a skill question, a config file for a config question)?
3. Is the content actually about the topic, or does it just mention
   related words in passing?

Return ONLY a JSON array of file paths, ordered from most relevant to
least relevant. Include ONLY files that are actually relevant to the
question. Exclude files that are tangentially related or noise.

Example: ["path/to/most_relevant.md", "path/to/second.md", "path/to/third.md"]

Return ONLY the JSON array. No markdown fences, no explanation.
"""


# ===================================================================
# PUBLIC API
# ===================================================================

async def search_kb(
    query: str,
    product_id: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
    top_k: int = 10,
) -> str:
    """Enterprise hybrid search: multi-query + structural + vector + RRF + re-rank."""
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return "No knowledge base found for this product. Train the product first."

    project_analysis = kb_data.get("project_analysis", {})
    file_analysis: dict = kb_data.get("file_analysis", {})
    folder_analysis: dict = kb_data.get("folder_analysis", {})
    files: dict = kb_data.get("files", {})

    if not file_analysis and not files:
        return "Knowledge base is empty — no files were indexed during training."

    # ── Stage 1: LLM multi-query expansion ────────────────────────
    components = _extract_component_names(project_analysis, file_analysis)
    technologies = project_analysis.get("technologies", [])
    file_types = _extract_file_types(file_analysis, files)

    expansion = await _expand_query(
        query=query,
        components=components,
        technologies=technologies,
        file_types=file_types,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )

    keywords = expansion.get("keywords", [query])
    target_components = expansion.get("components", [])
    file_patterns = expansion.get("file_patterns", [])
    intent = expansion.get("intent", "broad")
    alt_queries = expansion.get("alternative_queries", [])

    logger.info(
        "kb_search_expansion",
        query=query[:80],
        keywords=keywords[:10],
        components=target_components,
        file_patterns=file_patterns,
        intent=intent,
        alt_queries=alt_queries[:3],
    )

    # ── Stage 0: Structural registry search ───────────────────────
    registry = _build_structural_registry(kb_data, product_id)
    structural_ranking = _search_structural_registry(
        registry, query, keywords, file_patterns,
    )

    # ── Stage 1b: Direct path lookup ──────────────────────────────
    direct_hits = _direct_path_matches(file_analysis, files, keywords, file_patterns)

    # ── Stage 2: Multi-query keyword search ───────────────────────
    all_queries = [query] + alt_queries[:2]
    keyword_rankings: list[list[tuple[str, float]]] = []

    for q_variant in all_queries:
        if q_variant == query:
            kw_set = keywords
        else:
            kw_set = _basic_keywords(q_variant) + keywords[:5]

        scores_a, scores_b, scores_c = await asyncio.gather(
            _search_file_analysis(file_analysis, kw_set, target_components, file_patterns),
            _search_folder_analysis(folder_analysis, file_analysis, kw_set, file_patterns),
            _search_file_text(files, kw_set),
        )

        all_paths = set(scores_a) | set(scores_b) | set(scores_c) | direct_hits
        ranked: list[tuple[str, float]] = []
        for path in all_paths:
            score = (
                scores_a.get(path, 0.0) * _W_FILE_ANALYSIS
                + scores_b.get(path, 0.0) * _W_FOLDER_MATCH
                + scores_c.get(path, 0.0) * _W_TEXT_MATCH
            )
            if path in direct_hits and score < 1.0:
                score = max(score, 1.0)

            fa = file_analysis.get(path, {})
            score *= _IMPORTANCE_BOOST.get(fa.get("importance", "medium"), 1.0)

            file_comp = (fa.get("component") or "").lower()
            if file_comp and file_comp in [c.lower() for c in target_components]:
                score *= _COMPONENT_BOOST

            path_lower = path.lower()
            all_terms = list({k.lower() for k in kw_set} | {p.lower() for p in file_patterns})
            path_hits = sum(1 for t in all_terms if t in path_lower)
            if path_hits > 0 and all_terms:
                score *= 1.0 + (path_hits / len(all_terms)) * (_PATH_BOOST_MAX - 1.0)

            fn_lower = PurePosixPath(path).name.lower()
            stem_lower = PurePosixPath(path).stem.lower()
            if any(p in fn_lower or p in stem_lower for p in _NOISY_FILE_PATTERNS):
                score *= _NOISY_FILE_PENALTY

            ranked.append((path, score))

        ranked.sort(key=lambda x: x[1], reverse=True)
        keyword_rankings.append(ranked)

    # ── Stage 3: RRF fusion ─────────────────────────────────────
    all_rankings_for_rrf: list[list[tuple[str, float]]] = []

    if structural_ranking:
        all_rankings_for_rrf.append(structural_ranking)

    all_rankings_for_rrf.extend(keyword_rankings)

    fused = _reciprocal_rank_fusion(*all_rankings_for_rrf)

    if not fused:
        return f"No relevant information found in knowledge base for: {query}"

    # ── Stage 4: LLM re-ranking ──────────────────────────────────
    candidates_for_rerank = fused[:top_k * 3]
    reranked = await _llm_rerank(
        query=query,
        candidates=candidates_for_rerank,
        file_analysis=file_analysis,
        files=files,
        api_key=api_key,
        model=model,
        base_url=base_url,
        top_k=top_k,
    )

    if reranked:
        top_paths = reranked[:top_k]
    else:
        top_paths = [(p, s) for p, s in fused[:top_k]]

    # ── Stage 5: Format response ──────────────────────────────────
    return _format_results(
        query=query,
        top_paths=top_paths,
        project_analysis=project_analysis,
        file_analysis=file_analysis,
        folder_analysis=folder_analysis,
        files=files,
        intent=intent,
    )


# ===================================================================
# Additional public helpers for granular KB tools
# ===================================================================

async def list_kb_entries(
    product_id: str,
    category: str = "all",
) -> str:
    """List entries in the KB: skills, configs, docs, source, or all."""
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return "No knowledge base found. Train the product first."

    registry = _build_structural_registry(kb_data, product_id)
    parts: list[str] = []

    if category in ("all", "skills") and registry.get("skills"):
        parts.append(f"=== SKILLS ({len(registry['skills'])}) ===")
        for s in registry["skills"]:
            parts.append(f"  {s['name']}: {s['description']}")
            parts.append(f"    Path: {s['path']}")

    if category in ("all", "directories") and registry.get("directories"):
        dirs = registry["directories"]
        parts.append(f"\n=== DIRECTORIES ({len(dirs)}) ===")
        for d_path, d_info in sorted(dirs.items()):
            purpose = d_info.get("purpose", "")
            file_count = d_info.get("file_count", 0)
            parts.append(f"  {d_path} ({file_count} files): {purpose}")

    file_analysis = kb_data.get("file_analysis", {})
    if category in ("all", "files"):
        parts.append(f"\n=== FILES ({len(file_analysis)}) ===")
        for path, fa in sorted(file_analysis.items()):
            desc = fa.get("description", "")[:80]
            parts.append(f"  {path}: {desc}")

    if not parts:
        return "No entries found in the knowledge base."

    return "\n".join(parts)


async def read_kb_file(product_id: str, file_path: str) -> str:
    """Read the full text of a specific file from the KB."""
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return "No knowledge base found. Train the product first."

    files = kb_data.get("files", {})
    file_analysis = kb_data.get("file_analysis", {})

    # Try exact match first
    fdata = files.get(file_path)
    if not fdata:
        # Try fuzzy path match (suffix matching)
        for path in files:
            if path.endswith(file_path) or file_path in path:
                fdata = files[path]
                file_path = path
                break

    if not fdata:
        available = list(files.keys())[:20]
        return (
            f"File '{file_path}' not found in KB. "
            f"Available files (first 20): {', '.join(available)}"
        )

    text = fdata.get("text", "")
    fa = file_analysis.get(file_path, {})

    parts = [f"=== {file_path} ==="]
    if fa.get("description"):
        parts.append(f"Description: {fa['description']}")
    if fa.get("component"):
        parts.append(f"Component: {fa['component']}")
    if fa.get("role"):
        parts.append(f"Role: {fa['role']}")
    if fa.get("technologies"):
        parts.append(f"Technologies: {', '.join(fa['technologies'])}")
    parts.append(f"\n--- content ({len(text)} chars) ---")
    parts.append(text[:_MAX_TEXT_PER_FILE * 2])
    if len(text) > _MAX_TEXT_PER_FILE * 2:
        parts.append("\n[... content truncated ...]")

    return "\n".join(parts)


async def browse_kb_structure(product_id: str, path: str = "/") -> str:
    """Browse the KB folder structure at a given path."""
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return "No knowledge base found. Train the product first."

    file_analysis = kb_data.get("file_analysis", {})
    folder_analysis = kb_data.get("folder_analysis", {})
    files_data = kb_data.get("files", {})
    all_paths = sorted(set(file_analysis.keys()) | set(files_data.keys()))

    path_normalized = path.rstrip("/")
    if path_normalized and not path_normalized.startswith("/"):
        path_normalized = "/" + path_normalized

    # Find entries under this path
    children: dict[str, list[str]] = {}  # immediate child dir → files
    direct_files: list[str] = []

    for fp in all_paths:
        if path_normalized and not fp.startswith(path_normalized):
            # Try without leading slash
            if not fp.startswith(path_normalized.lstrip("/")):
                continue

        # Get the relative part after path
        if path_normalized:
            relative = fp[len(path_normalized):].lstrip("/")
        else:
            relative = fp.lstrip("/")

        parts_list = relative.split("/")
        if len(parts_list) == 1:
            direct_files.append(fp)
        elif len(parts_list) > 1:
            child_dir = parts_list[0]
            if child_dir not in children:
                children[child_dir] = []
            children[child_dir].append(fp)

    result_parts = [f"=== KB Structure: {path or '/'} ===\n"]

    if children:
        result_parts.append("Directories:")
        for dir_name, dir_files in sorted(children.items()):
            full_dir = f"{path_normalized}/{dir_name}" if path_normalized else dir_name
            fa_info = folder_analysis.get(full_dir, {})
            purpose = fa_info.get("purpose", "")
            result_parts.append(f"  [{dir_name}/] ({len(dir_files)} files) {purpose}")

    if direct_files:
        result_parts.append("\nFiles:")
        for fp in direct_files:
            name = PurePosixPath(fp).name
            fa = file_analysis.get(fp, {})
            desc = fa.get("description", "")[:60]
            result_parts.append(f"  {name}: {desc}")

    if not children and not direct_files:
        result_parts.append(f"No entries found at path '{path}'.")
        result_parts.append(f"Try browsing from root: '/'")
        # Show top-level dirs
        top_dirs: set[str] = set()
        for fp in all_paths[:100]:
            first = fp.lstrip("/").split("/")[0]
            if first:
                top_dirs.add(first)
        if top_dirs:
            result_parts.append(f"Top-level directories: {', '.join(sorted(top_dirs))}")

    return "\n".join(result_parts)


# ===================================================================
# STRUCTURAL REGISTRY
# ===================================================================

def _build_structural_registry(kb_data: dict, product_id: str) -> dict:
    """Extract a structural registry from the KB blob (cached)."""
    from datetime import datetime
    cached = _registry_cache.get(product_id)
    if cached:
        data, ts = cached
        if (datetime.utcnow().timestamp() - ts) < _REGISTRY_CACHE_TTL:
            return data

    file_analysis = kb_data.get("file_analysis", {})
    folder_analysis = kb_data.get("folder_analysis", {})
    files = kb_data.get("files", {})
    all_paths = set(file_analysis.keys()) | set(files.keys())

    # Extract skills (files matching */SKILL.md or similar patterns)
    skills: list[dict] = []
    for path in all_paths:
        name_lower = PurePosixPath(path).name.lower()
        if name_lower == "skill.md":
            skill_info = _extract_skill_info(path, files.get(path, {}), file_analysis.get(path, {}))
            if skill_info:
                skills.append(skill_info)

    # Extract directory info
    directories: dict[str, dict] = {}
    for dir_path, fa in folder_analysis.items():
        file_count = sum(1 for p in all_paths if p.startswith(dir_path))
        directories[dir_path] = {
            "purpose": fa.get("purpose", ""),
            "technologies": fa.get("technologies", []),
            "importance": fa.get("importance", ""),
            "file_count": file_count,
        }

    registry = {
        "skills": skills,
        "directories": directories,
        "all_paths": sorted(all_paths),
    }

    from datetime import datetime
    _registry_cache[product_id] = (registry, datetime.utcnow().timestamp())
    return registry


def _extract_skill_info(path: str, file_data: dict, fa: dict) -> Optional[dict]:
    """Extract skill name and description from a SKILL.md file."""
    text = file_data.get("text", "")

    # Try YAML frontmatter
    name = ""
    description = ""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            frontmatter = text[3:end].strip()
            try:
                parsed = yaml.safe_load(frontmatter)
                if isinstance(parsed, dict):
                    name = parsed.get("name", "")
                    description = parsed.get("description", "")
            except Exception:
                pass

    # Fallback: derive name from path
    if not name:
        parts = PurePosixPath(path).parts
        if len(parts) >= 2:
            name = parts[-2]

    # Fallback: use file analysis description
    if not description and fa.get("description"):
        description = fa["description"]

    if name:
        return {"name": name, "path": path, "description": description}
    return None


def _search_structural_registry(
    registry: dict,
    query: str,
    keywords: list[str],
    file_patterns: list[str],
) -> list[tuple[str, float]]:
    """Search the structural registry for exact/fuzzy matches."""
    results: list[tuple[str, float]] = []
    query_lower = query.lower()
    kw_lower = [k.lower() for k in keywords]
    pat_lower = [p.lower() for p in file_patterns]
    all_terms = list(set(kw_lower) | set(pat_lower))

    # Search skills
    for skill in registry.get("skills", []):
        skill_name = skill.get("name", "").lower()
        skill_desc = skill.get("description", "").lower()
        path = skill["path"]

        score = 0.0
        # Exact name match
        if skill_name and skill_name in query_lower:
            score += 10.0
        # Name appears in keywords/patterns
        if skill_name and any(skill_name in t or t in skill_name for t in all_terms):
            score += 8.0
        # Keywords appear in skill description
        desc_hits = sum(1 for t in all_terms if t in skill_desc)
        if desc_hits > 0:
            score += desc_hits * 2.0
        # Keywords appear in path
        path_lower = path.lower()
        path_hits = sum(1 for t in all_terms if t in path_lower)
        if path_hits > 0:
            score += path_hits * 1.5

        if score > 0:
            results.append((path, score))

    # Search directories
    for dir_path, dir_info in registry.get("directories", {}).items():
        purpose = dir_info.get("purpose", "").lower()
        dir_lower = dir_path.lower()

        score = 0.0
        purpose_hits = sum(1 for t in all_terms if t in purpose)
        if purpose_hits > 0:
            score += purpose_hits * 1.5
        dir_hits = sum(1 for t in all_terms if t in dir_lower)
        if dir_hits > 0:
            score += dir_hits * 1.0

        if score > 0:
            # Propagate directory score to files within it
            for file_path in registry.get("all_paths", []):
                if file_path.startswith(dir_path):
                    results.append((file_path, score * 0.5))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ===================================================================
# DIRECT PATH LOOKUP
# ===================================================================

def _direct_path_matches(
    file_analysis: dict,
    files: dict,
    keywords: list[str],
    file_patterns: list[str],
) -> set[str]:
    """Files whose paths literally contain multiple query/pattern terms."""
    all_paths = set(file_analysis.keys()) | set(files.keys())
    terms = list({kw.lower() for kw in keywords} | {p.lower() for p in file_patterns})
    if not terms:
        return set()

    matches: set[str] = set()
    for path in all_paths:
        path_lower = path.lower()
        hits = sum(1 for t in terms if t in path_lower)
        if hits >= 2:
            matches.add(path)
    return matches


# ===================================================================
# MULTI-QUERY EXPANSION
# ===================================================================

async def _expand_query(
    query: str,
    components: list[str],
    technologies: list[str],
    file_types: list[str],
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
) -> dict:
    """LLM query expansion with alternative query generation."""
    client_kwargs: dict = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = AsyncOpenAI(**client_kwargs)

    system_prompt = _EXPANSION_SYSTEM.format(
        components=", ".join(components[:30]) if components else "(none identified)",
        technologies=", ".join(technologies[:20]) if technologies else "(none identified)",
        file_types=", ".join(file_types[:60]) if file_types else "(none)",
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"User question: {query}"},
            ],
            temperature=0,
            max_tokens=600,
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
        if not isinstance(data.get("alternative_queries"), list):
            data["alternative_queries"] = []
        data["intent"] = data.get("intent", "broad")

        return data

    except Exception as exc:
        logger.warning("kb_query_expansion_failed", error=str(exc), query=query[:80])
        return {
            "keywords": _basic_keywords(query),
            "components": [],
            "file_patterns": [],
            "alternative_queries": [],
            "intent": "broad",
        }


# ===================================================================
# RECIPROCAL RANK FUSION
# ===================================================================

def _reciprocal_rank_fusion(
    *rankings: list[tuple[str, float]],
    k: int = _RRF_K,
) -> list[tuple[str, float]]:
    """Merge multiple ranked lists using Reciprocal Rank Fusion."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        if not ranking:
            continue
        for rank, (path, _original_score) in enumerate(ranking):
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank + 1)

    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return fused


# ===================================================================
# LLM RE-RANKING
# ===================================================================

async def _llm_rerank(
    query: str,
    candidates: list[tuple[str, float]],
    file_analysis: dict,
    files: dict,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
    top_k: int = 10,
) -> Optional[list[tuple[str, float]]]:
    """LLM re-ranks candidates for the actual user question."""
    if not candidates:
        return None

    # Build candidate descriptions for the LLM
    candidate_parts: list[str] = []
    for i, (path, score) in enumerate(candidates[:20], 1):
        fa = file_analysis.get(path, {})
        fdata = files.get(path, {})
        text = fdata.get("text", "")
        preview = text[:300].replace("\n", " ") if text else "(no content)"
        desc = fa.get("description", "")

        candidate_parts.append(
            f"[{i}] {path}\n"
            f"  Description: {desc}\n"
            f"  Preview: {preview}\n"
        )

    user_msg = (
        f"User question: {query}\n\n"
        f"Candidate files:\n{''.join(candidate_parts)}"
    )

    client_kwargs: dict = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = AsyncOpenAI(**client_kwargs)

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _RERANK_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=500,
        )
        raw = response.choices[0].message.content or "[]"
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        reranked_paths = json.loads(raw)

        if not isinstance(reranked_paths, list):
            return None

        # Build result with descending scores
        score_map = {path: score for path, score in candidates}
        result: list[tuple[str, float]] = []
        for i, path in enumerate(reranked_paths[:top_k]):
            if isinstance(path, str) and path in score_map:
                result.append((path, top_k - i))

        # Append any candidates from RRF that the LLM didn't include
        seen = {p for p, _ in result}
        for path, score in candidates:
            if path not in seen and len(result) < top_k:
                result.append((path, 0.0))
                seen.add(path)

        return result if result else None

    except Exception as exc:
        logger.warning("llm_rerank_failed", error=str(exc))
        return None


# ===================================================================
# KEYWORD SEARCH HELPERS (preserved from original)
# ===================================================================

def _kw_score(text: str, keywords: list[str]) -> float:
    """Score how many keywords appear in text (case-insensitive). Returns 0.0-1.0."""
    if not text or not keywords:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for kw in keywords if kw.lower() in text_lower)
    return hits / len(keywords)


def _pattern_match(name: str, patterns: list[str]) -> bool:
    name_lower = name.lower()
    return any(p.lower() in name_lower for p in patterns)


async def _search_file_analysis(
    file_analysis: dict, keywords: list[str],
    target_components: list[str], file_patterns: list[str],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    target_comp_lower = [c.lower() for c in target_components]
    for path, fa in file_analysis.items():
        filename = PurePosixPath(path).name
        total = (
            _kw_score(fa.get("description", ""), keywords) * 3.0
            + _kw_score(fa.get("role", ""), keywords) * 2.0
            + (1.0 if (fa.get("component") or "").lower() in target_comp_lower else 0.0) * 2.5
            + _kw_score(", ".join(fa.get("technologies", [])), keywords) * 1.0
            + _kw_score(", ".join(fa.get("relationships", [])), keywords) * 1.0
            + (1.0 if _pattern_match(filename, file_patterns) else 0.0) * 2.0
            + (1.0 if _pattern_match(path, file_patterns) else 0.0) * 1.5
        )
        if total > 0:
            scores[path] = total
    return scores


async def _search_folder_analysis(
    folder_analysis: dict, file_analysis: dict,
    keywords: list[str], file_patterns: list[str],
) -> dict[str, float]:
    folder_scores: dict[str, float] = {}
    for fp, fa in folder_analysis.items():
        total = (
            _kw_score(fa.get("purpose", ""), keywords) * 3.0
            + _kw_score(", ".join(fa.get("technologies", [])), keywords) * 1.0
            + (1.0 if _pattern_match(fp, file_patterns) else 0.0) * 2.0
        )
        if total > 0:
            folder_scores[fp] = total
    if not folder_scores:
        return {}
    file_scores: dict[str, float] = {}
    for file_path in file_analysis:
        for fp, fscore in folder_scores.items():
            if file_path.startswith(fp):
                file_scores[file_path] = max(file_scores.get(file_path, 0.0), fscore)
    return file_scores


async def _search_file_text(files: dict, keywords: list[str]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for path, fdata in files.items():
        text = fdata.get("text", "")
        if not text:
            continue
        score = _kw_score(text, keywords)
        if score > 0:
            length_factor = min(1.0, 5000.0 / max(len(text), 1))
            score *= (1.0 + length_factor)
            scores[path] = score
    return scores


# ===================================================================
# UTILITY HELPERS
# ===================================================================

def _basic_keywords(query: str) -> list[str]:
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "about", "between", "under", "above", "and", "but",
        "or", "not", "no", "so", "if", "then", "than", "too", "very", "just",
        "that", "this", "these", "those", "what", "which", "who", "whom",
        "how", "where", "when", "why", "all", "each", "every", "both", "few",
        "more", "most", "some", "any", "it", "its", "my", "your", "his",
        "her", "our", "their", "me", "him", "us", "them", "i", "you", "he",
        "she", "we", "they", "there", "here",
    }
    words = re.findall(r"[a-zA-Z0-9_]+", query.lower())
    return [w for w in words if w not in stop_words and len(w) > 1][:_EXPANSION_FALLBACK_KEYWORDS_COUNT]


def _extract_component_names(project_analysis: dict, file_analysis: dict) -> list[str]:
    components: set[str] = set()
    for comp in project_analysis.get("key_components", []):
        if isinstance(comp, dict) and comp.get("name"):
            components.add(comp["name"])
    for fa in file_analysis.values():
        comp = fa.get("component", "")
        if comp and comp.strip():
            components.add(comp.strip())
    return sorted(components)


def _extract_file_types(file_analysis: dict, files: dict) -> list[str]:
    names: set[str] = set()
    for path in list(file_analysis.keys()) + list(files.keys()):
        name = PurePosixPath(path).name
        if name:
            names.add(name)
    return sorted(names)[:60]


# ===================================================================
# FORMAT RESULTS
# ===================================================================

def _format_results(
    query: str,
    top_paths: list[tuple[str, float]],
    project_analysis: dict,
    file_analysis: dict,
    folder_analysis: dict,
    files: dict,
    intent: str,
) -> str:
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
