"""
Knowledge base tools — gives the agent multiple ways to query the trained KB.

Four tools for agentic RAG:
  1. search_knowledge_base — hybrid search (multi-query + structural + vector + RRF + LLM re-rank)
  2. list_kb_entries       — list skills, files, components, directories
  3. read_kb_file          — read a specific file's full content from the KB
  4. browse_kb_structure   — browse the KB folder tree (like `ls`)
"""

from typing import Optional
import asyncio
import structlog

logger = structlog.get_logger()


def _run_async(coro):
    """Run an async coroutine from sync context (needed by LangGraph sandbox)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=60)
    else:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tool 1: Hybrid search
# ---------------------------------------------------------------------------

def search_knowledge_base(
    query: str,
    product_id: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
    top_k: int = 10,
) -> str:
    """Search the product's trained knowledge base using enterprise hybrid retrieval.

    Uses multi-query expansion, structural registry, keyword search, vector
    similarity (if embeddings exist), RRF fusion, and LLM re-ranking.
    """
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

    return _run_async(_async_search())


# ---------------------------------------------------------------------------
# Tool 2: List KB entries
# ---------------------------------------------------------------------------

def list_kb_entries_tool(
    product_id: str,
    category: str = "all",
) -> str:
    """List entries in the knowledge base: skills, directories, files, or all.

    Use this to discover what's in the knowledge base before searching.
    Categories: 'all', 'skills', 'directories', 'files'.
    """
    async def _async_list() -> str:
        try:
            from app.rag.kb_search import list_kb_entries
            return await list_kb_entries(product_id=product_id, category=category)
        except Exception as exc:
            logger.error("kb_list_entries_error", product_id=product_id, error=str(exc))
            return f"Error listing KB entries: {str(exc)}"

    return _run_async(_async_list())


# ---------------------------------------------------------------------------
# Tool 3: Read specific KB file
# ---------------------------------------------------------------------------

def read_kb_file_tool(
    product_id: str,
    file_path: str,
) -> str:
    """Read the full content of a specific file from the knowledge base.

    Use after search or list to get the complete text of a file.
    Supports fuzzy path matching (partial paths work).
    """
    async def _async_read() -> str:
        try:
            from app.rag.kb_search import read_kb_file
            return await read_kb_file(product_id=product_id, file_path=file_path)
        except Exception as exc:
            logger.error("kb_read_file_error", product_id=product_id, file_path=file_path, error=str(exc))
            return f"Error reading KB file: {str(exc)}"

    return _run_async(_async_read())


# ---------------------------------------------------------------------------
# Tool 4: Browse KB structure
# ---------------------------------------------------------------------------

def browse_kb_structure_tool(
    product_id: str,
    path: str = "/",
) -> str:
    """Browse the knowledge base folder structure at a given path.

    Like 'ls' for the knowledge base. Shows directories and files
    with descriptions. Start from '/' and drill down.
    """
    async def _async_browse() -> str:
        try:
            from app.rag.kb_search import browse_kb_structure
            return await browse_kb_structure(product_id=product_id, path=path)
        except Exception as exc:
            logger.error("kb_browse_error", product_id=product_id, path=path, error=str(exc))
            return f"Error browsing KB structure: {str(exc)}"

    return _run_async(_async_browse())


# ---------------------------------------------------------------------------
# Tool builders (called by tools/__init__.py)
# ---------------------------------------------------------------------------

def make_knowledge_base_tools(
    product_id: str,
    product_description: Optional[str] = None,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
) -> list:
    """Create all KB StructuredTool instances for a product."""
    from langchain_core.tools import StructuredTool

    # Tool 1: Hybrid search
    search_desc = (
        "Search the trained knowledge base for product information. "
        "Uses enterprise hybrid retrieval: multi-query expansion, structural "
        "registry, keyword + vector search, and LLM re-ranking. "
        "Use for questions about architecture, code, features, skills, "
        "configuration, or any trained content."
    )
    if product_description:
        search_desc += f"\n\nProduct Description: {product_description}"

    def _search(query: str, top_k: int = 10) -> str:
        return search_knowledge_base(query, product_id, api_key, model, base_url, top_k)

    # Tool 2: List entries
    def _list_entries(category: str = "all") -> str:
        return list_kb_entries_tool(product_id, category)

    # Tool 3: Read file
    def _read_file(file_path: str) -> str:
        return read_kb_file_tool(product_id, file_path)

    # Tool 4: Browse structure
    def _browse(path: str = "/") -> str:
        return browse_kb_structure_tool(product_id, path)

    tools = [
        StructuredTool.from_function(
            name="search_knowledge_base",
            description=search_desc,
            func=_search,
        ),
        StructuredTool.from_function(
            name="list_kb_entries",
            description=(
                "List what's in the knowledge base: skills, directories, "
                "files, or all. Use this FIRST when you need to discover "
                "what the product contains — like checking a table of "
                "contents before reading a book. "
                "Categories: 'all', 'skills', 'directories', 'files'."
            ),
            func=_list_entries,
        ),
        StructuredTool.from_function(
            name="read_kb_file",
            description=(
                "Read the full content of a specific file from the knowledge "
                "base by its path. Use after search or list to get complete "
                "file text. Supports fuzzy path matching (partial paths work)."
            ),
            func=_read_file,
        ),
        StructuredTool.from_function(
            name="browse_kb_structure",
            description=(
                "Browse the knowledge base folder structure. Like 'ls' or "
                "'tree' for the KB. Shows directories and files with their "
                "descriptions. Start from '/' and drill down into "
                "subdirectories to explore."
            ),
            func=_browse,
        ),
    ]

    return tools
