"""
Knowledge base search tool — allows agent to query the trained knowledge base.

Uses LLM-expanded semantic search (no embeddings required):
  1. LLM expands the user query into keywords, components, and file patterns
  2. Three parallel searches run against the in-memory KB blob
  3. Results are scored, ranked, and returned with metadata + content
"""

from typing import Optional
import structlog

logger = structlog.get_logger()


def search_knowledge_base(
    query: str,
    product_id: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
    top_k: int = 10,
) -> str:
    """Search the product's trained knowledge base for relevant information.

    Uses LLM-powered query expansion + parallel keyword search over the
    encrypted KB blob (project analysis, file descriptions, folder purposes,
    and full extracted text).

    Args:
        query: The question or search query to find relevant information
        product_id: The ID of the product whose knowledge base to search
        api_key: LLM API key for query expansion
        model: LLM model name for query expansion
        base_url: Optional LLM API base URL
        top_k: Number of results to return (default: 10, max: 20)

    Returns:
        Formatted string with relevant knowledge from the knowledge base.
    """
    import asyncio

    limited_top_k = min(max(1, top_k), 20)

    async def _async_search() -> str:
        try:
            from app.rag.kb_search import search_kb
            return await search_kb(
                query=query,
                product_id=product_id,
                api_key=api_key,
                model=model,
                base_url=base_url,
                top_k=limited_top_k,
            )
        except Exception as exc:
            logger.error("knowledge_base_search_error", query=query, product_id=product_id, error=str(exc))
            return f"Error searching knowledge base: {str(exc)}"

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, _async_search())
            return future.result(timeout=30)
    else:
        return asyncio.run(_async_search())


def make_knowledge_base_tool(
    product_id: str,
    product_description: Optional[str] = None,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
) -> "StructuredTool":
    """Create a StructuredTool instance for knowledge base search.

    Args:
        product_id: The product ID to search
        product_description: Optional product description for tool description
        api_key: LLM API key (for query expansion)
        model: LLM model name (for query expansion)
        base_url: Optional LLM API base URL
    """
    from langchain_core.tools import StructuredTool

    base_desc = (
        "Search the trained knowledge base for product information. "
        "Use this when you need details about the product's architecture, "
        "code, documentation, configuration, or any trained content. "
        "The search uses intelligent keyword expansion to find semantically "
        "related results — e.g. searching 'credentials' will also find "
        "content about passwords, authentication, tokens, etc."
    )

    if product_description:
        base_desc += f"\n\nProduct Description: {product_description}"

    def _search(query: str, top_k: int = 10) -> str:
        return search_knowledge_base(query, product_id, api_key, model, base_url, top_k)

    return StructuredTool.from_function(
        name="search_knowledge_base",
        description=base_desc,
        func=_search,
    )
