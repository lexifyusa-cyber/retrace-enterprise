"""
Deep web search tool — multi-source web research.

Ported from IQWorksAtlas External Tools/deep_websearch_tool.py.
Pipeline:  DuckDuckGo search → LLM selects top 3 → scrape full pages → format.

The LLM client is injected at construction time via ``make_deep_websearch_fn``
so the tool can use the chat model configured in ReTrace settings.
"""

import json
import re
from typing import Any

import structlog
import httpx
from bs4 import BeautifulSoup

logger = structlog.get_logger()

TIMEOUT = 10  # HTTP request timeout (seconds)
MAX_CONTENT_LENGTH = 5000  # Max chars per scraped page


# ---------------------------------------------------------------------------
# Pipeline steps (ported from DeepWebSearchTool)
# ---------------------------------------------------------------------------

def _search_and_extract(query: str) -> list[dict]:
    """Step 1 — DuckDuckGo search returning structured results."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=8))

        structured = []
        for r in results:
            structured.append({
                "title": r.get("title", "N/A"),
                "link": r.get("href") or r.get("link", ""),
                "snippet": r.get("body") or r.get("snippet", ""),
            })
        return structured
    except Exception as exc:
        logger.error("deep_websearch_search_error", error=str(exc))
        raise


def _select_top_results(
    query: str,
    results: list[dict],
    llm_client: Any,
    model: str,
) -> list[dict]:
    """Step 2 — Use LLM to pick the top 3 most relevant results."""
    if len(results) <= 3:
        return results

    formatted = []
    for i, r in enumerate(results, 1):
        formatted.append(
            f"{i}. Title: {r.get('title', 'N/A')}\n"
            f"   URL: {r.get('link', 'N/A')}\n"
            f"   Snippet: {r.get('snippet', '')[:200]}"
        )
    results_text = "\n\n".join(formatted)

    system = (
        "You are a search result ranking expert.\n"
        "Select the top 3 most relevant results for the query.\n"
        "Return ONLY a JSON array of numbers (1-indexed), e.g. [1, 3, 5]."
    )
    user = f"Query: {query}\n\nSearch Results:\n{results_text}\n\nSelect top 3:"

    try:
        # Use the OpenAI-compatible client already available in ReTrace
        import asyncio

        async def _call():
            resp = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
                max_tokens=50,
            )
            return resp.choices[0].message.content or "[]"

        # Run the async call from sync context
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an event loop already — use a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                raw = pool.submit(asyncio.run, _call()).result()
        else:
            raw = asyncio.run(_call())

        m = re.search(r"\[[\d\s,]+\]", raw)
        indices = json.loads(m.group(0)) if m else json.loads(raw)

        selected = []
        for idx in indices[:3]:
            if 1 <= idx <= len(results):
                selected.append(results[idx - 1])
        return selected if selected else results[:3]
    except Exception as exc:
        logger.warning("deep_websearch_selection_fallback", error=str(exc))
        return results[:3]


def _scrape_urls(results: list[dict]) -> dict[str, str]:
    """Step 3 — Scrape content from selected URLs."""
    scraped: dict[str, str] = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ReTrace/1.0)"}

    for r in results:
        url = r.get("link", "")
        if not url:
            continue
        try:
            resp = httpx.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.content, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()

            main = None
            for sel in ["main", "article", '[role="main"]', ".content", "#content"]:
                main = soup.select_one(sel)
                if main:
                    break
            if not main:
                main = soup.find("body") or soup

            text = main.get_text(separator="\n", strip=True)
            if len(text) > MAX_CONTENT_LENGTH:
                text = text[:MAX_CONTENT_LENGTH] + "... [truncated]"
            scraped[url] = text
        except Exception as exc:
            scraped[url] = f"[Error scraping: {exc}]"

    return scraped


def _format_content(
    query: str,
    scraped: dict[str, str],
    results: list[dict],
) -> str:
    """Step 4 — Format scraped content for the agent."""
    sections = []
    for url, content in scraped.items():
        info = next((r for r in results if r.get("link") == url), {})
        title = info.get("title", "Unknown")
        sections.append(
            f"Source: {title}\n"
            f"URL: {url}\n"
            f"Content:\n{content}\n"
            f"{'-' * 50}"
        )
    return "\n\n".join(sections) if sections else "No content scraped."


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def make_deep_websearch_fn(llm_client: Any, model: str):
    """Return a ``deep_websearch(query) -> str`` function bound to the given LLM."""

    def deep_websearch(query: str) -> str:
        """Comprehensive web research: search → select top 3 → scrape → format."""
        if not query or not query.strip():
            return "Error: empty query"
        try:
            results = _search_and_extract(query)
            if not results:
                return f"No search results found for: {query}"

            top = _select_top_results(query, results, llm_client, model)
            if not top:
                return "No results selected."

            scraped = _scrape_urls(top)
            if not scraped:
                return "Failed to scrape any URLs."

            return _format_content(query, scraped, top)
        except Exception as exc:
            logger.error("deep_websearch_error", query=query[:80], error=str(exc))
            return f"Deep web search error: {exc}"

    return deep_websearch
