"""
Basic web search tool (DuckDuckGo).

Renamed from ``duckduckgo_search`` in IQWorksAtlas.  Uses the ``ddgs``
library directly — no LangChain community wrapper needed.
"""

import json
import structlog

logger = structlog.get_logger()


def basic_search(query: str) -> str:
    """Search the web via DuckDuckGo and return results.

    Returns a formatted string with titles, URLs, and snippets for up
    to 8 results.  No API key required.
    """
    if not query or not query.strip():
        return "Error: empty search query"

    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=8))

        if not results:
            return f"No results found for: {query}"

        lines: list[str] = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "N/A")
            href = r.get("href") or r.get("link", "N/A")
            body = r.get("body") or r.get("snippet", "")
            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {href}")
            if body:
                lines.append(f"   {body[:200]}")
            lines.append("")

        return "\n".join(lines)

    except ImportError:
        return "Error: ddgs package not installed. Run: pip install ddgs"
    except Exception as exc:
        logger.error("basic_search_error", query=query[:80], error=str(exc))
        return f"Search error: {exc}"
