"""
Knowledge base tools — single unified tool with four actions (subtools).

One tool `knowledge_base` with action: search | list | read_file | browse.
The search action accepts optional agent-provided keywords for zero-latency
query expansion (no internal LLM calls).
"""

from typing import Literal, Optional
import asyncio
import structlog
from pydantic import BaseModel, Field

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
# Single-tool schema and dispatcher
# ---------------------------------------------------------------------------

class KnowledgeBaseToolInput(BaseModel):
    """Arguments for the unified knowledge_base tool."""
    action: Literal["search", "list", "read_file", "browse"] = Field(
        description="Action: 'search' = fast hybrid search; 'list' = list key_files/dirs/files; 'read_file' = read one file; 'browse' = browse folder tree."
    )
    query: Optional[str] = Field(default=None, description="Search query (required when action='search').")
    keywords: Optional[list[str]] = Field(
        default=None,
        description="Optional list of search keywords, synonyms, and related terms to improve recall. Provide when action='search'.",
    )
    top_k: Optional[int] = Field(default=10, ge=1, le=20, description="Max results for search.")
    category: Optional[str] = Field(default="all", description="Category for list: 'all', 'key_files', 'directories', 'files'.")
    file_path: Optional[str] = Field(default=None, description="File path in KB (required when action='read_file').")
    path: Optional[str] = Field(default="/", description="Folder path to browse (action='browse').")


def _knowledge_base_dispatcher(
    action: str,
    product_id: str,
    api_key: str,
    model: str,
    base_url: Optional[str],
    query: Optional[str] = None,
    keywords: Optional[list[str]] = None,
    top_k: int = 10,
    category: str = "all",
    file_path: Optional[str] = None,
    path: str = "/",
) -> str:
    """Dispatch to the correct KB operation."""
    if action == "search":
        if not (query or "").strip():
            return "Error: action='search' requires a non-empty 'query'."
        return search_knowledge_base(
            query=query.strip(),
            product_id=product_id,
            api_key=api_key,
            model=model,
            base_url=base_url,
            top_k=min(max(1, top_k or 10), 20),
            agent_keywords=keywords,
        )
    if action == "list":
        return list_kb_entries_tool(product_id=product_id, category=category or "all")
    if action == "read_file":
        if not (file_path or "").strip():
            return "Error: action='read_file' requires a non-empty 'file_path'."
        return read_kb_file_tool(product_id=product_id, file_path=file_path.strip())
    if action == "browse":
        return browse_kb_structure_tool(product_id=product_id, path=(path or "/").strip())
    return f"Error: unknown action '{action}'. Use one of: search, list, read_file, browse."


# ---------------------------------------------------------------------------
# Internal implementations
# ---------------------------------------------------------------------------

def search_knowledge_base(
    query: str,
    product_id: str,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
    top_k: int = 10,
    agent_keywords: Optional[list[str]] = None,
) -> str:
    """Search the product's trained knowledge base using fast hybrid retrieval.

    Uses deterministic keyword expansion (+ optional agent-provided keywords),
    structural registry, keyword search, and RRF fusion.  No internal LLM calls.
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
                agent_keywords=agent_keywords,
            )
        except Exception as exc:
            logger.error("knowledge_base_search_error", query=query, product_id=product_id, error=str(exc))
            return f"Error searching knowledge base: {str(exc)}"

    return _run_async(_async_search())


# ---------------------------------------------------------------------------
# List KB entries
# ---------------------------------------------------------------------------

def list_kb_entries_tool(
    product_id: str,
    category: str = "all",
) -> str:
    """List entries in the knowledge base: key_files, directories, files, or all."""
    async def _async_list() -> str:
        try:
            from app.rag.kb_search import list_kb_entries
            return await list_kb_entries(product_id=product_id, category=category)
        except Exception as exc:
            logger.error("kb_list_entries_error", product_id=product_id, error=str(exc))
            return f"Error listing KB entries: {str(exc)}"

    return _run_async(_async_list())


# ---------------------------------------------------------------------------
# Read specific KB file
# ---------------------------------------------------------------------------

def read_kb_file_tool(
    product_id: str,
    file_path: str,
) -> str:
    """Read the full content of a specific file from the knowledge base.

    Use after search to get the complete text of a file.
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
# Browse KB structure
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
# Tool builder (called by tools/__init__.py)
# ---------------------------------------------------------------------------

def make_knowledge_base_tools(
    product_id: str,
    product_description: Optional[str] = None,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
) -> list:
    """Create the single knowledge_base StructuredTool for a product."""
    from langchain_core.tools import StructuredTool

    description = (
        "Product knowledge base tool with four actions (use the 'action' parameter):\n"
        "- **search**: Fast hybrid search. Requires 'query'; also provide 'keywords' (list of synonyms/related terms) for better recall. Optional 'top_k' (1-20).\n"
        "- **list**: List what's in the KB. Use 'category': 'all' | 'key_files' | 'directories' | 'files'.\n"
        "- **read_file**: Read full content of a file by 'file_path'. Use after search. Supports fuzzy path matching.\n"
        "- **browse**: Browse folder structure. Use 'path' (default '/'). Like 'ls' for the KB."
    )
    if product_description:
        description += f"\n\nProduct: {product_description[:300]}"

    def _kb_tool(
        action: str,
        query: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        top_k: int = 10,
        category: str = "all",
        file_path: Optional[str] = None,
        path: str = "/",
    ) -> str:
        return _knowledge_base_dispatcher(
            action=action,
            product_id=product_id,
            api_key=api_key,
            model=model,
            base_url=base_url,
            query=query,
            keywords=keywords,
            top_k=top_k,
            category=category,
            file_path=file_path,
            path=path,
        )

    tool = StructuredTool.from_function(
        name="knowledge_base",
        description=description,
        func=_kb_tool,
        args_schema=KnowledgeBaseToolInput,
    )
    return [tool]
