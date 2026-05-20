"""
ReTrace Search MCP Server — web search tools.

Exposes the three pre-OC web tools that OpenCode has no equivalent for:
  - web_search:   Serper API (primary) → DuckDuckGo fallback. Returns organic
                  results plus Knowledge Graph + "People Also Ask" + Related
                  Searches.
  - web_research: Search → LLM picks top 3 → scrape full pages → synthesize.
                  Bundles multi-source research into one tool call.
  - web_advanced: Full Serper operator surface — site: / filetype: / inurl: /
                  intitle: / before: / after: / exact: / exclude / or_terms /
                  region / language / time filters / pagination.

All three lift directly from `app/tools/web_search.py` — pure Python, no
LangChain dependency.

Required env (injected by opencode_manager.generate_config):
  RETRACE_SERPER_API_KEY  — Serper key. Empty → DDG fallback for search and
                            advanced, "search-only" mode for research (no
                            LLM ranking — falls back to first 3 results).
  RETRACE_LLM_API_KEY     — required for web_research's top-3 LLM ranker.
  RETRACE_LLM_BASE_URL    — LLM provider base URL (e.g. https://api.x.ai/v1).
  RETRACE_LLM_MODEL       — model name for the top-3 ranker.

Without LLM creds, `web_research` falls back to naively scraping the first 3
search results without LLM-ranking them.
"""

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("retrace-search")

_SERPER_API_KEY = os.environ.get("RETRACE_SERPER_API_KEY", "")
_LLM_API_KEY = os.environ.get("RETRACE_LLM_API_KEY", "")
_LLM_BASE_URL = os.environ.get("RETRACE_LLM_BASE_URL", "")
_LLM_MODEL = os.environ.get("RETRACE_LLM_MODEL", "")

# Configure the shared search module's Serper key at import time.
from app.tools.web_search import (
    configure_web_search,
    web_search as _web_search_fn,
    web_advanced as _web_advanced_fn,
    make_web_research_fn,
    _search_and_extract_with_provider,
    _scrape_urls,
    _format_research_content,
)

configure_web_search(serper_api_key=_SERPER_API_KEY)


def _get_llm_client():
    """Build an AsyncOpenAI client from injected env vars, or None if creds
    are missing. Caller (web_research) handles the None case by skipping the
    top-3 ranking step."""
    if not _LLM_API_KEY:
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None
    kwargs = {"api_key": _LLM_API_KEY}
    if _LLM_BASE_URL:
        kwargs["base_url"] = _LLM_BASE_URL
    return AsyncOpenAI(**kwargs)


@mcp.tool()
def web_search(query: str, num_results: int = 10, gl: str = "us", hl: str = "en") -> str:
    """Search the web. Returns titles, URLs, snippets, plus Knowledge Graph
    info, "People Also Ask" questions, and Related Searches when available.

    Uses Serper API if configured, otherwise falls back to DuckDuckGo.

    Args:
        query: Search query.
        num_results: Number of results (1-100, default 10).
        gl: Region code (ISO 3166-1 alpha-2, default "us").
        hl: Language code (ISO 639-1, default "en").
    """
    return _web_search_fn(query, num_results=num_results, gl=gl, hl=hl)


@mcp.tool()
def web_research(query: str) -> str:
    """Multi-source research: search → pick top 3 → scrape full pages → format.

    Performs a search, uses an LLM to pick the 3 most relevant results, then
    scrapes the full text of each and returns a formatted multi-source
    summary. One tool call delivers what would otherwise take 5+ calls
    (search + fetch + fetch + fetch + synthesize).

    If no LLM is configured for the MCP, falls back to scraping the first
    three search results without ranking.

    Args:
        query: The research question.
    """
    if not query or not query.strip():
        return "Error: empty query"

    client = _get_llm_client()
    if client and _LLM_MODEL:
        try:
            fn = make_web_research_fn(client, _LLM_MODEL)
            return fn(query)
        except Exception as exc:
            # Fall through to no-LLM mode rather than failing the call.
            pass

    # Fallback: no LLM available — use the first 3 results directly.
    try:
        results = _search_and_extract_with_provider(query)
        if not results:
            return f"No search results found for: {query}"
        top = results[:3]
        scraped = _scrape_urls(top)
        if not scraped:
            return "Failed to scrape any URLs."
        return _format_research_content(query, scraped, top)
    except Exception as exc:
        return f"Web research error: {exc}"


@mcp.tool()
def web_advanced(
    query: str,
    num_results: int = 10,
    gl: str = "us",
    hl: str = "en",
    location: Optional[str] = None,
    tbs: Optional[str] = None,
    page: int = 1,
    autocorrect: bool = True,
    site: Optional[str] = None,
    filetype: Optional[str] = None,
    inurl: Optional[str] = None,
    intitle: Optional[str] = None,
    related: Optional[str] = None,
    cache: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    exact: Optional[str] = None,
    exclude: Optional[str] = None,
    or_terms: Optional[str] = None,
) -> str:
    """Advanced web search with full Serper operator surface.

    Supports search operators, region/language targeting, time filters, and
    pagination. Falls back to basic search (with operators embedded in the
    query) if Serper is not configured.

    Args:
        query: Search query.
        num_results: Number of results (1-100, default 10).
        gl: Region code (default "us").
        hl: Language code (default "en").
        location: Location string (e.g. "Brooklyn, New York, United States").
        tbs: Time filter: "qdr:h" (hour), "qdr:d" (day), "qdr:w" (week),
            "qdr:m" (month), "qdr:y" (year).
        page: Page number (default 1).
        autocorrect: Enable autocorrect (default True).
        site: Limit to a domain (e.g. "python.org").
        filetype: File type filter (e.g. "pdf").
        inurl: Word must appear in URL.
        intitle: Word must appear in title.
        related: Find similar sites (e.g. "github.com").
        cache: View cached version of a URL.
        before: Date filter (YYYY-MM-DD).
        after: Date filter (YYYY-MM-DD).
        exact: Exact phrase match.
        exclude: Terms to exclude (comma-separated).
        or_terms: Alternative terms (comma-separated, OR operator).
    """
    return _web_advanced_fn(
        query,
        num_results=num_results,
        gl=gl,
        hl=hl,
        location=location,
        tbs=tbs,
        page=page,
        autocorrect=autocorrect,
        site=site,
        filetype=filetype,
        inurl=inurl,
        intitle=intitle,
        related=related,
        cache=cache,
        before=before,
        after=after,
        exact=exact,
        exclude=exclude,
        or_terms=or_terms,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
