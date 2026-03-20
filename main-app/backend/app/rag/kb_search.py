"""
KB Search — Fast retrieval engine (zero embeddings, zero internal LLM calls).

Retrieval strategies fused with Reciprocal Rank Fusion (RRF):
  0. Structural Registry — match on component/key-file names from the KB.
  1. Direct Path Lookup — files whose paths contain query terms.
  2. Multi-keyword search — deterministic keyword expansion + agent-provided
     keywords; search runs against file analysis, folder analysis, and full
     text (like a human trying different grep patterns).
  3. RRF Fusion — merge all rankings into a single ordered list.

No embeddings, no vector DB, no internal LLM calls.
All data comes from the in-memory KB blob.  Speed target: <500ms per search.
"""

import asyncio
import re
from pathlib import PurePosixPath
from typing import Optional

import structlog

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

_BROAD_QUERY_HINTS = {"what", "how", "overview", "architecture", "explain", "describe", "summary", "structure"}


# ===================================================================
# PUBLIC API
# ===================================================================

async def search_kb(
    query: str,
    product_id: str,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
    top_k: int = 10,
    agent_keywords: Optional[list[str]] = None,
) -> str:
    """Fast hybrid search — deterministic expansion + RRF, no internal LLM calls.

    *agent_keywords*: optional keywords provided by the calling agent at tool-call
    time (the agent LLM already understands the query, so its keywords are high
    quality and come at zero extra latency).
    """
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

    # ── Deterministic expansion (no LLM call) ─────────────────────
    components = _extract_component_names(project_analysis, file_analysis)
    expansion = _deterministic_expand(query, components, agent_keywords)

    keywords = expansion["keywords"]
    target_components = expansion["components"]
    file_patterns = expansion["file_patterns"]
    intent = expansion["intent"]

    logger.info(
        "kb_search_fast",
        query=query[:80],
        keywords=keywords[:10],
        components=target_components,
        file_patterns=file_patterns,
        intent=intent,
        agent_kw_count=len(agent_keywords) if agent_keywords else 0,
    )

    # ── Stage 0: Structural registry search ───────────────────────
    registry = _build_structural_registry(kb_data, product_id)
    structural_ranking = _search_structural_registry(
        registry, query, keywords, file_patterns,
    )

    # ── Stage 1: Direct path lookup ───────────────────────────────
    direct_hits = _direct_path_matches(file_analysis, files, keywords, file_patterns)

    # ── Stage 2: Keyword search (parallel across analysis layers) ─
    scores_a, scores_b, scores_c = await asyncio.gather(
        _search_file_analysis(file_analysis, keywords, target_components, file_patterns),
        _search_folder_analysis(folder_analysis, file_analysis, keywords, file_patterns),
        _search_file_text(files, keywords),
    )

    all_paths = set(scores_a) | set(scores_b) | set(scores_c) | direct_hits
    keyword_ranking: list[tuple[str, float]] = []
    all_terms = list({k.lower() for k in keywords} | {p.lower() for p in file_patterns})

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
        path_hits = sum(1 for t in all_terms if t in path_lower)
        if path_hits > 0 and all_terms:
            score *= 1.0 + (path_hits / len(all_terms)) * (_PATH_BOOST_MAX - 1.0)
        fn_lower = PurePosixPath(path).name.lower()
        stem_lower = PurePosixPath(path).stem.lower()
        if any(p in fn_lower or p in stem_lower for p in _NOISY_FILE_PATTERNS):
            score *= _NOISY_FILE_PENALTY
        keyword_ranking.append((path, score))

    keyword_ranking.sort(key=lambda x: x[1], reverse=True)

    # ── Stage 3: RRF fusion ───────────────────────────────────────
    rankings: list[list[tuple[str, float]]] = []
    if structural_ranking:
        rankings.append(structural_ranking)
    if keyword_ranking:
        rankings.append(keyword_ranking)

    fused = _reciprocal_rank_fusion(*rankings)

    if not fused:
        return f"No relevant information found in knowledge base for: {query}"

    top_paths = fused[:top_k]

    # ── Format response ───────────────────────────────────────────
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

async def get_product_summary(product_id: str) -> Optional[str]:
    """Return a short product summary from the KB for embedding in the system prompt.

    Returns None if no KB exists.  The summary is cheap to produce (just reads
    the cached blob's project_analysis) and lets the agent answer overview
    questions without any tool call at all.
    """
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return None

    pa = kb_data.get("project_analysis", {})
    if not pa:
        return None

    parts: list[str] = []
    if pa.get("summary"):
        parts.append(pa["summary"])
    if pa.get("technologies"):
        parts.append(f"Technologies: {', '.join(pa['technologies'][:15])}")
    if pa.get("architecture_style"):
        parts.append(f"Architecture: {pa['architecture_style']}")
    comps = pa.get("key_components", [])
    if comps:
        comp_lines = [f"  - {c['name']}: {c.get('description', '')}" for c in comps[:10] if isinstance(c, dict)]
        if comp_lines:
            parts.append("Key components:\n" + "\n".join(comp_lines))

    folder_analysis = kb_data.get("folder_analysis", {})
    if folder_analysis:
        top_dirs = sorted(folder_analysis.items(), key=lambda x: x[1].get("importance", ""), reverse=True)[:8]
        dir_lines = [f"  - {d}: {info.get('purpose', '')[:80]}" for d, info in top_dirs]
        if dir_lines:
            parts.append("Top-level structure:\n" + "\n".join(dir_lines))

    return "\n".join(parts) if parts else None


async def list_kb_entries(
    product_id: str,
    category: str = "all",
) -> str:
    """List entries in the KB: key_files, directories, files, or all."""
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return "No knowledge base found. Train the product first."

    registry = _build_structural_registry(kb_data, product_id)
    parts: list[str] = []

    if category in ("all", "key_files") and registry.get("key_files"):
        kf = registry["key_files"]
        parts.append(f"=== KEY FILES ({len(kf)}) ===")
        for f in kf:
            parts.append(f"  {f['path']}: {f['description']}")

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
    """Browse the KB folder structure at a given path.

    Handles paths stored with or without leading slashes and supports
    substring/suffix matching so ``browse("/docs/experiments")`` works even
    when the KB stores paths as ``/full/repo/docs/experiments/...``.
    """
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(product_id)
    if not kb_data:
        return "No knowledge base found. Train the product first."

    file_analysis = kb_data.get("file_analysis", {})
    folder_analysis = kb_data.get("folder_analysis", {})
    files_data = kb_data.get("files", {})
    all_paths = sorted(set(file_analysis.keys()) | set(files_data.keys()))

    children, direct_files, matched_prefix = _browse_match(all_paths, path)

    result_parts = [f"=== KB Structure: {path or '/'} ===\n"]

    if children:
        result_parts.append("Directories:")
        for dir_name, dir_files in sorted(children.items()):
            full_dir = f"{matched_prefix}/{dir_name}" if matched_prefix else dir_name
            fa_info = folder_analysis.get(full_dir, {}) or folder_analysis.get(full_dir.lstrip("/"), {})
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
        result_parts.append("Try browsing from root: '/'")
        top_dirs: set[str] = set()
        for fp in all_paths[:200]:
            first = fp.lstrip("/").split("/")[0]
            if first:
                top_dirs.add(first)
        if top_dirs:
            result_parts.append(f"Top-level directories: {', '.join(sorted(top_dirs))}")

    return "\n".join(result_parts)


def _browse_match(
    all_paths: list[str], path: str,
) -> tuple[dict[str, list[str]], list[str], str]:
    """Match *path* against KB paths using prefix, then suffix/substring fallback.

    Returns ``(children_dict, direct_files_list, matched_prefix_str)``.
    """
    needle = path.strip("/")

    if not needle:
        return _browse_split(all_paths, "")

    with_slash = "/" + needle
    without_slash = needle

    # 1. Try exact prefix match (with and without leading /)
    for prefix in (with_slash, without_slash):
        children, direct = _browse_split(
            [fp for fp in all_paths if fp == prefix or fp.startswith(prefix + "/") or fp.startswith(prefix.lstrip("/") + "/")],
            prefix,
        )
        if children or direct:
            return children, direct, prefix

    # 2. Suffix / substring fallback — find paths that contain the needle
    suffix_hit = "/" + needle + "/"
    matched = [fp for fp in all_paths if suffix_hit in ("/" + fp.lstrip("/") + "/")]
    if not matched:
        matched = [fp for fp in all_paths if needle in fp]
    if matched:
        common = _common_prefix_up_to(matched, needle)
        return _browse_split(matched, common)

    return {}, [], ""


def _browse_split(
    paths: list[str], prefix: str,
) -> tuple[dict[str, list[str]], list[str]]:
    """Split paths into immediate child dirs and direct files relative to prefix."""
    children: dict[str, list[str]] = {}
    direct_files: list[str] = []
    prefix_stripped = prefix.rstrip("/")
    plen = len(prefix_stripped)

    for fp in paths:
        if prefix_stripped:
            rel = fp[plen:] if fp.startswith(prefix_stripped) else fp
            rel = rel.lstrip("/")
        else:
            rel = fp.lstrip("/")
        if not rel:
            continue
        seg = rel.split("/")
        if len(seg) == 1:
            direct_files.append(fp)
        elif len(seg) > 1:
            children.setdefault(seg[0], []).append(fp)
    return children, direct_files


def _common_prefix_up_to(paths: list[str], needle: str) -> str:
    """Find the longest common prefix of *paths* that ends right before *needle*."""
    if not paths:
        return ""
    first = paths[0]
    idx = first.find(needle)
    if idx >= 0:
        return first[:idx + len(needle)]
    return ""


# ===================================================================
# STRUCTURAL REGISTRY
# ===================================================================

_KEY_FILE_NAMES = {
    "readme.md", "readme", "readme.txt", "readme.rst",
    "skill.md", "config.yaml", "config.yml", "config.json",
    "package.json", "pyproject.toml", "cargo.toml",
    "index.ts", "index.js", "index.py", "main.py", "main.ts", "main.go",
    "agents.md", "changelog.md",
}


def _build_structural_registry(kb_data: dict, product_id: str) -> dict:
    """Extract a structural registry from the KB blob (cached).

    Identifies *key files* generically (READMEs, configs, entry points, and
    any file marked critical/high importance) instead of only looking for
    SKILL.md.
    """
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

    key_files: list[dict] = []
    for path in sorted(all_paths):
        name_lower = PurePosixPath(path).name.lower()
        fa = file_analysis.get(path, {})
        importance = fa.get("importance", "").lower()
        is_key = name_lower in _KEY_FILE_NAMES or importance in ("critical", "high")
        if is_key:
            desc = fa.get("description", "")[:120]
            key_files.append({"path": path, "description": desc})

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
        "key_files": key_files,
        "directories": directories,
        "all_paths": sorted(all_paths),
    }

    _registry_cache[product_id] = (registry, datetime.utcnow().timestamp())
    return registry


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

    for kf in registry.get("key_files", []):
        kf_desc = kf.get("description", "").lower()
        path = kf["path"]
        path_lower = path.lower()

        score = 0.0
        desc_hits = sum(1 for t in all_terms if t in kf_desc)
        if desc_hits > 0:
            score += desc_hits * 2.0
        path_hits = sum(1 for t in all_terms if t in path_lower)
        if path_hits > 0:
            score += path_hits * 2.5
        if score > 0:
            results.append((path, score))

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
# DETERMINISTIC QUERY EXPANSION (replaces LLM expansion — zero latency)
# ===================================================================

def _deterministic_expand(
    query: str,
    components: list[str],
    agent_keywords: Optional[list[str]] = None,
) -> dict:
    """Expand query into keywords, components, file_patterns, and intent
    without any LLM call.  Merges agent-provided keywords when available.
    """
    base_kw = _basic_keywords(query)

    if agent_keywords:
        merged = list(dict.fromkeys(agent_keywords + base_kw))
    else:
        merged = base_kw

    query_lower = query.lower()
    target_components = [
        c for c in components
        if c.lower() in query_lower or any(k in c.lower() for k in base_kw)
    ]

    file_patterns = list(base_kw)

    words_lower = set(query_lower.split())
    intent = "broad" if words_lower & _BROAD_QUERY_HINTS else "specific"

    return {
        "keywords": merged[:20],
        "components": target_components[:8],
        "file_patterns": file_patterns[:10],
        "intent": intent,
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
# KEYWORD SEARCH HELPERS
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
