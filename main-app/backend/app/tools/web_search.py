"""
Web search tools — Serper API (primary) with DuckDuckGo fallback.

Provides three tools:
  1. web_search      — Simple web search (replaces basic_search)
  2. web_research     — Multi-source research with LLM selection (replaces deep_websearch)
  3. web_advanced     — Full-featured search with advanced operators

Uses provider abstraction to support multiple search backends:
  - Serper (primary, if API key configured)
  - DuckDuckGo (fallback, always available)
"""

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

import structlog
import httpx
from bs4 import BeautifulSoup

logger = structlog.get_logger()

# Global API key configuration
_serper_api_key: str = ""

# Constants
TIMEOUT = 10  # HTTP request timeout (seconds)
MAX_CONTENT_LENGTH = 5000  # Max chars per scraped page


# ---------------------------------------------------------------------------
# Provider Abstraction
# ---------------------------------------------------------------------------

class SearchResult:
    """Standardized search result format."""
    def __init__(
        self,
        organic: list[dict],
        knowledge_graph: Optional[dict] = None,
        people_also_ask: Optional[list[str]] = None,
        related_searches: Optional[list[str]] = None,
    ):
        self.organic = organic  # [{"title": "...", "link": "...", "snippet": "..."}, ...]
        self.knowledge_graph = knowledge_graph
        self.people_also_ask = people_also_ask
        self.related_searches = related_searches


class SearchProvider(ABC):
    """Abstract base class for search providers."""
    
    @abstractmethod
    def search(
        self,
        query: str,
        num_results: int = 10,
        gl: str = "us",
        hl: str = "en",
        **kwargs
    ) -> SearchResult:
        """Perform a search and return standardized results."""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is available (API key configured, etc.)."""
        pass
    
    @abstractmethod
    def supports_advanced_operators(self) -> bool:
        """Whether this provider supports advanced search operators."""
        pass


class SerperProvider(SearchProvider):
    """Serper API provider."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://google.serper.dev"
        self.scrape_url = "https://scrape.serper.dev"
    
    def is_available(self) -> bool:
        return bool(self.api_key)
    
    def supports_advanced_operators(self) -> bool:
        return True
    
    def _build_advanced_query(self, query: str, **kwargs) -> str:
        """Build query string with advanced operators."""
        import re
        q = re.sub(r'\s+', ' ', query.strip())
        
        if kwargs.get("site"):
            q += f" site:{kwargs['site']}"
        if kwargs.get("filetype"):
            q += f" filetype:{kwargs['filetype']}"
        if kwargs.get("inurl"):
            q += f" inurl:{kwargs['inurl']}"
        if kwargs.get("intitle"):
            q += f" intitle:{kwargs['intitle']}"
        if kwargs.get("related"):
            q += f" related:{kwargs['related']}"
        if kwargs.get("cache"):
            q += f" cache:{kwargs['cache']}"
        if kwargs.get("before"):
            q += f" before:{kwargs['before']}"
        if kwargs.get("after"):
            q += f" after:{kwargs['after']}"
        if kwargs.get("exact"):
            q += f' "{kwargs["exact"]}"'
        if kwargs.get("exclude"):
            exclude_terms = kwargs["exclude"].split(",") if isinstance(kwargs["exclude"], str) else kwargs["exclude"]
            for term in exclude_terms:
                q += f" -{term.strip()}"
        if kwargs.get("or_terms"):
            or_terms = kwargs["or_terms"].split(",") if isinstance(kwargs["or_terms"], str) else kwargs["or_terms"]
            or_str = " OR ".join(t.strip() for t in or_terms)
            q += f" ({or_str})"
        
        return q.strip()
    
    def search(
        self,
        query: str,
        num_results: int = 10,
        gl: str = "us",
        hl: str = "en",
        **kwargs
    ) -> SearchResult:
        """Search using Serper API."""
        try:
            # Ensure num_results is an integer
            try:
                num_results = int(num_results) if isinstance(num_results, str) else int(num_results)
                num_results = max(1, min(num_results, 100))  # Clamp between 1 and 100
            except (ValueError, TypeError):
                num_results = 10
            
            # Build query with advanced operators if provided
            enhanced_query = self._build_advanced_query(query, **kwargs)
            
            payload = {
                "q": enhanced_query,
                "num": num_results,
                "gl": gl,
                "hl": hl,
            }
            
            # Add optional parameters
            if kwargs.get("location"):
                payload["location"] = kwargs["location"]
            if kwargs.get("tbs"):
                payload["tbs"] = kwargs["tbs"]
            if kwargs.get("page"):
                try:
                    page = int(kwargs["page"]) if isinstance(kwargs["page"], str) else int(kwargs["page"])
                    payload["page"] = max(1, page)
                except (ValueError, TypeError):
                    pass  # Skip invalid page
            if "autocorrect" in kwargs:
                try:
                    payload["autocorrect"] = bool(kwargs["autocorrect"]) if isinstance(kwargs["autocorrect"], str) else bool(kwargs["autocorrect"])
                except (ValueError, TypeError):
                    payload["autocorrect"] = True
            
            response = httpx.post(
                f"{self.base_url}/search",
                headers={
                    "Content-Type": "application/json",
                    "X-API-KEY": self.api_key,
                },
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            
            # Extract organic results
            organic = []
            for item in data.get("organic", []):
                organic.append({
                    "title": item.get("title", "N/A"),
                    "link": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                    "position": item.get("position", 0),
                })
            
            # Extract knowledge graph
            kg = data.get("knowledgeGraph")
            
            # Extract "people also ask"
            paa = []
            for item in data.get("peopleAlsoAsk", []):
                question = item.get("question", "")
                if question:
                    paa.append(question)
            
            # Extract related searches
            related = data.get("relatedSearches", [])
            
            return SearchResult(
                organic=organic,
                knowledge_graph=kg,
                people_also_ask=paa if paa else None,
                related_searches=related if related else None,
            )
        except Exception as exc:
            logger.error("serper_search_error", query=query[:80], error=str(exc))
            raise


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo provider (free fallback)."""
    
    def is_available(self) -> bool:
        return True  # Always available
    
    def supports_advanced_operators(self) -> bool:
        return False  # DuckDuckGo doesn't support advanced operators natively
    
    def _build_advanced_query(self, query: str, **kwargs) -> str:
        """Build query string manually (DuckDuckGo doesn't support operators natively)."""
        q = query.strip()
        
        # Try to build operators into query string (may not work perfectly)
        if kwargs.get("site"):
            q += f" site:{kwargs['site']}"
        if kwargs.get("filetype"):
            q += f" filetype:{kwargs['filetype']}"
        if kwargs.get("exact"):
            q += f' "{kwargs["exact"]}"'
        
        return q.strip()
    
    def search(
        self,
        query: str,
        num_results: int = 10,
        gl: str = "us",
        hl: str = "en",
        **kwargs
    ) -> SearchResult:
        """Search using DuckDuckGo."""
        try:
            from ddgs import DDGS
            
            # Ensure num_results is an integer
            try:
                num_results = int(num_results) if isinstance(num_results, str) else int(num_results)
                num_results = max(1, min(num_results, 100))  # Clamp between 1 and 100
            except (ValueError, TypeError):
                num_results = 10
            
            # Build query with basic operators if provided
            enhanced_query = self._build_advanced_query(query, **kwargs)
            
            with DDGS() as ddgs:
                results = list(ddgs.text(enhanced_query, max_results=num_results))
            
            organic = []
            for r in results:
                organic.append({
                    "title": r.get("title", "N/A"),
                    "link": r.get("href") or r.get("link", ""),
                    "snippet": r.get("body") or r.get("snippet", ""),
                    "position": len(organic) + 1,
                })
            
            return SearchResult(organic=organic)
        except ImportError:
            raise Exception("ddgs package not installed. Run: pip install ddgs")
        except Exception as exc:
            logger.error("ddg_search_error", query=query[:80], error=str(exc))
            raise


# ---------------------------------------------------------------------------
# Provider Manager
# ---------------------------------------------------------------------------

def _get_provider() -> SearchProvider:
    """Get the best available search provider."""
    if _serper_api_key:
        try:
            return SerperProvider(_serper_api_key)
        except Exception:
            pass
    
    return DuckDuckGoProvider()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def configure_web_search(serper_api_key: str = ""):
    """Configure web search API keys (called from agent_service)."""
    global _serper_api_key
    _serper_api_key = serper_api_key or ""


# ---------------------------------------------------------------------------
# Tool 1: web_search (replaces basic_search)
# ---------------------------------------------------------------------------

def web_search(
    query: str,
    num_results: int = 10,
    gl: str = "us",
    hl: str = "en",
) -> str:
    """Search the web and return results.
    
    Uses Serper API if configured, otherwise falls back to DuckDuckGo.
    Returns formatted results with titles, URLs, snippets, knowledge graph,
    "people also ask", and related searches.
    
    Parameters
    ----------
    query : str
        Search query.
    num_results : int
        Number of results to return (default 10).
    gl : str
        Region code (ISO 3166-1 alpha-2, default "us").
    hl : str
        Language code (ISO 639-1, default "en").
    
    Returns
    -------
    str
        Formatted search results.
    """
    if not query or not query.strip():
        return "Error: empty search query"
    
    # Convert parameters to correct types (agent may pass strings)
    try:
        num_results = int(num_results) if isinstance(num_results, str) else int(num_results)
        num_results = max(1, min(num_results, 100))  # Clamp between 1 and 100
    except (ValueError, TypeError):
        num_results = 10
    
    try:
        provider = _get_provider()
        result = provider.search(query, num_results=num_results, gl=gl, hl=hl)
        
        lines: list[str] = [f"Search results for: {query}\n"]
        
        # Knowledge graph
        if result.knowledge_graph:
            kg = result.knowledge_graph
            lines.append("Knowledge Graph:")
            if kg.get("title"):
                lines.append(f"  Title: {kg['title']}")
            if kg.get("description"):
                lines.append(f"  Description: {kg['description']}")
            lines.append("")
        
        # Organic results
        if result.organic:
            lines.append("Organic Results:")
            for i, item in enumerate(result.organic, 1):
                lines.append(f"{i}. {item['title']}")
                lines.append(f"   URL: {item['link']}")
                if item['snippet']:
                    lines.append(f"   {item['snippet'][:300]}")
                lines.append("")
        else:
            lines.append("No results found.\n")
        
        # People also ask
        if result.people_also_ask:
            lines.append("People Also Ask:")
            for q in result.people_also_ask[:5]:  # Limit to 5
                lines.append(f"  - {q}")
            lines.append("")
        
        # Related searches
        if result.related_searches:
            lines.append("Related Searches:")
            for s in result.related_searches[:5]:  # Limit to 5
                lines.append(f"  - {s}")
            lines.append("")
        
        return "\n".join(lines)
    except Exception as exc:
        logger.error("web_search_error", query=query[:80], error=str(exc))
        return f"Search error: {exc}"


# ---------------------------------------------------------------------------
# Tool 2: web_research (replaces deep_websearch)
# ---------------------------------------------------------------------------

def _search_and_extract_with_provider(query: str) -> list[dict]:
    """Search using provider and return structured results."""
    try:
        provider = _get_provider()
        result = provider.search(query, num_results=8)
        
        structured = []
        for item in result.organic:
            structured.append({
                "title": item.get("title", "N/A"),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return structured
    except Exception as exc:
        logger.error("web_research_search_error", error=str(exc))
        raise


def _select_top_results(
    query: str,
    results: list[dict],
    llm_client: Any,
    model: str,
) -> list[dict]:
    """Use LLM to pick the top 3 most relevant results (same logic as deep_websearch)."""
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
        
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        
        if loop and loop.is_running():
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
        logger.warning("web_research_selection_fallback", error=str(exc))
        return results[:3]


def _scrape_urls(results: list[dict]) -> dict[str, str]:
    """Scrape content from URLs using web_fetch logic (same as deep_websearch)."""
    from app.tools.web_fetch import web_fetch
    
    scraped: dict[str, str] = {}
    for r in results:
        url = r.get("link", "")
        if not url:
            continue
        try:
            # Use web_fetch for scraping (trafilatura/BeautifulSoup)
            content = web_fetch(url)
            # Extract just the content part (remove "Content from URL:\n\n" prefix)
            if content.startswith(f"Content from {url}:\n\n"):
                content = content[len(f"Content from {url}:\n\n"):]
            elif content.startswith("Content from"):
                # Handle any URL format
                parts = content.split("\n\n", 1)
                if len(parts) > 1:
                    content = parts[1]
            
            # Truncate if too long
            if len(content) > MAX_CONTENT_LENGTH:
                content = content[:MAX_CONTENT_LENGTH] + "... [truncated]"
            scraped[url] = content
        except Exception as exc:
            scraped[url] = f"[Error scraping: {exc}]"
    
    return scraped


def _format_research_content(
    query: str,
    scraped: dict[str, str],
    results: list[dict],
) -> str:
    """Format scraped content for the agent (same as deep_websearch)."""
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


def make_web_research_fn(llm_client: Any, model: str):
    """Return a web_research(query) -> str function bound to the given LLM."""
    
    def web_research(query: str) -> str:
        """Comprehensive web research: search → select top 3 → scrape → format.
        
        Uses Serper API for search if configured, otherwise DuckDuckGo.
        Same pipeline as deep_websearch: search → LLM selects top 3 → scrape → format.
        
        Parameters
        ----------
        query : str
            Research query.
        
        Returns
        -------
        str
            Formatted research results with full content from top sources.
        """
        if not query or not query.strip():
            return "Error: empty query"
        try:
            # Step 1: Search (tries Serper first, falls back to DuckDuckGo)
            results = _search_and_extract_with_provider(query)
            if not results:
                return f"No search results found for: {query}"
            
            # Step 2: LLM selects top 3
            top = _select_top_results(query, results, llm_client, model)
            if not top:
                return "No results selected."
            
            # Step 3: Scrape full content
            scraped = _scrape_urls(top)
            if not scraped:
                return "Failed to scrape any URLs."
            
            # Step 4: Format
            return _format_research_content(query, scraped, top)
        except Exception as exc:
            logger.error("web_research_error", query=query[:80], error=str(exc))
            return f"Web research error: {exc}"
    
    return web_research


# ---------------------------------------------------------------------------
# Tool 3: web_advanced (new tool with full Serper features)
# ---------------------------------------------------------------------------

def web_advanced(
    query: str,
    num_results: int = 10,
    gl: str = "us",
    hl: str = "en",
    location: Optional[str] = None,
    tbs: Optional[str] = None,  # "qdr:h", "qdr:d", "qdr:w", "qdr:m", "qdr:y"
    page: int = 1,
    autocorrect: bool = True,
    # Advanced operators
    site: Optional[str] = None,
    filetype: Optional[str] = None,
    inurl: Optional[str] = None,
    intitle: Optional[str] = None,
    related: Optional[str] = None,
    cache: Optional[str] = None,
    before: Optional[str] = None,  # YYYY-MM-DD
    after: Optional[str] = None,   # YYYY-MM-DD
    exact: Optional[str] = None,
    exclude: Optional[str] = None,  # comma-separated
    or_terms: Optional[str] = None,  # comma-separated
) -> str:
    """Advanced web search with full Serper API features.
    
    Supports advanced search operators, region/language targeting, time filters,
    and pagination. If Serper is not available, falls back to basic search with
    operators built into query string (may not work perfectly with DuckDuckGo).
    
    Parameters
    ----------
    query : str
        Search query.
    num_results : int
        Number of results (default 10).
    gl : str
        Region code (default "us").
    hl : str
        Language code (default "en").
    location : str, optional
        Location string (e.g., "SoHo, New York, United States").
    tbs : str, optional
        Time filter: "qdr:h" (hour), "qdr:d" (day), "qdr:w" (week),
        "qdr:m" (month), "qdr:y" (year).
    page : int
        Page number (default 1).
    autocorrect : bool
        Enable autocorrect (default True).
    site : str, optional
        Limit to domain (e.g., "python.org").
    filetype : str, optional
        File type filter (e.g., "pdf", "doc").
    inurl : str, optional
        Word must appear in URL.
    intitle : str, optional
        Word must appear in title.
    related : str, optional
        Find similar sites (e.g., "github.com").
    cache : str, optional
        View cached version of URL.
    before : str, optional
        Date before (YYYY-MM-DD).
    after : str, optional
        Date after (YYYY-MM-DD).
    exact : str, optional
        Exact phrase match.
    exclude : str, optional
        Terms to exclude (comma-separated).
    or_terms : str, optional
        Alternative terms (comma-separated, OR operator).
    
    Returns
    -------
    str
        Formatted search results.
    """
    if not query or not query.strip():
        return "Error: empty search query"
    
    # Convert parameters to correct types (agent may pass strings)
    try:
        num_results = int(num_results) if isinstance(num_results, str) else int(num_results)
        num_results = max(1, min(num_results, 100))  # Clamp between 1 and 100
    except (ValueError, TypeError):
        num_results = 10
    
    try:
        page = int(page) if isinstance(page, str) else int(page)
        page = max(1, page)  # Ensure at least 1
    except (ValueError, TypeError):
        page = 1
    
    try:
        autocorrect = bool(autocorrect) if isinstance(autocorrect, str) else bool(autocorrect)
    except (ValueError, TypeError):
        autocorrect = True
    
    try:
        provider = _get_provider()
        
        # Build kwargs for advanced operators
        kwargs = {
            "location": location,
            "tbs": tbs,
            "page": page,
            "autocorrect": autocorrect,
            "site": site,
            "filetype": filetype,
            "inurl": inurl,
            "intitle": intitle,
            "related": related,
            "cache": cache,
            "before": before,
            "after": after,
            "exact": exact,
            "exclude": exclude,
            "or_terms": or_terms,
        }
        
        # Remove None values
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        
        # If provider doesn't support advanced operators, build into query
        if not provider.supports_advanced_operators():
            # For DuckDuckGo, we'll try to build operators into query string
            # but it may not work perfectly
            result = provider.search(query, num_results=num_results, gl=gl, hl=hl, **kwargs)
        else:
            result = provider.search(query, num_results=num_results, gl=gl, hl=hl, **kwargs)
        
        # Format results (same as web_search)
        lines: list[str] = [f"Advanced search results for: {query}\n"]
        
        if result.knowledge_graph:
            kg = result.knowledge_graph
            lines.append("Knowledge Graph:")
            if kg.get("title"):
                lines.append(f"  Title: {kg['title']}")
            if kg.get("description"):
                lines.append(f"  Description: {kg['description']}")
            lines.append("")
        
        if result.organic:
            lines.append("Organic Results:")
            for i, item in enumerate(result.organic, 1):
                lines.append(f"{i}. {item['title']}")
                lines.append(f"   URL: {item['link']}")
                if item['snippet']:
                    lines.append(f"   {item['snippet'][:300]}")
                lines.append("")
        else:
            lines.append("No results found.\n")
        
        if result.people_also_ask:
            lines.append("People Also Ask:")
            for q in result.people_also_ask[:5]:
                lines.append(f"  - {q}")
            lines.append("")
        
        if result.related_searches:
            lines.append("Related Searches:")
            for s in result.related_searches[:5]:
                lines.append(f"  - {s}")
            lines.append("")
        
        return "\n".join(lines)
    except Exception as exc:
        logger.error("web_advanced_error", query=query[:80], error=str(exc))
        return f"Advanced search error: {exc}"
