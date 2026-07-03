"""
ReTrace Knowledge Base MCP Server — search, list, read, browse, learn.

Tools:
  - search: Fast hybrid search across the team's knowledge base
  - list: List entries in the KB (key_files, directories, files)
  - read_file: Read full content of a KB file
  - browse: Browse the KB folder structure
  - learn: Store new knowledge (text/doc) into the KB for future retrieval
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
except Exception:
    pass

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("retrace-kb")

# This MCP server calls ReTrace's KB modules directly via Python import.
# user_id is genuinely process-global (one OC process per user) — fine to
# freeze. team_id is NOT — the user can switch teams in the UI at any time,
# so we resolve it per tool call via an internal HTTP endpoint (see the
# `retrace-browser` MCP at app/retrace_mcp/browser.py for the same pattern).
_USER_ID = os.environ.get("RETRACE_USER_ID", "")
_BACKEND_URL = (os.environ.get("RETRACE_BACKEND_URL") or "").rstrip("/")
_INTERNAL_TOKEN = os.environ.get("RETRACE_INTERNAL_TOKEN") or ""
_HTTP_TIMEOUT = 2.0  # interactive — fail fast and fall back


def _resolve_team_id() -> str:
    """Ask the backend for the user's currently-active team_id.

    A 200 response is authoritative — including `team_id=null`, which means
    the user explicitly has "No team" selected. Only fall back to the
    `RETRACE_TEAM_ID` env var when we can't reach the backend at all
    (transport error or non-200), because the env var is a snapshot from
    spawn time and may be stale.
    """
    if _BACKEND_URL and _INTERNAL_TOKEN and _USER_ID:
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.post(
                    f"{_BACKEND_URL}/api/v1/agent/internal/team/active",
                    json={"user_id": _USER_ID},
                    headers={"X-Internal-Token": _INTERNAL_TOKEN},
                )
            if resp.status_code == 200:
                # Authoritative — empty/null means "no team", don't fall back.
                return (resp.json() or {}).get("team_id") or ""
            print(
                f"[kb] active-team lookup HTTP {resp.status_code}, falling back to env",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"[kb] active-team lookup failed, falling back to env: {exc}", file=sys.stderr)
    return os.environ.get("RETRACE_TEAM_ID", "")


def _get_team_id() -> str:
    return _resolve_team_id()


def _run_async(coro):
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


@mcp.tool()
def search(query: str, keywords: Optional[str] = None, top_k: int = 10) -> str:
    """Search the team's knowledge base using fast hybrid retrieval.

    Args:
        query: The search query
        keywords: Optional comma-separated list of synonyms/related terms for better recall
        top_k: Max results to return (1-20)
    """
    from app.rag.kb_search import search_kb

    team_id = _get_team_id()
    if not team_id:
        return "No team is selected. The user must pick a team in the picker before the knowledge base can be searched."

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None
    async def _do():
        return await search_kb(
            query=query,
            team_id=team_id,
            top_k=min(max(1, top_k), 20),
            agent_keywords=kw_list,
        )

    try:
        return _run_async(_do())
    except Exception as e:
        return f"Error searching knowledge base: {e}"


@mcp.tool()
def list_entries(category: str = "all") -> str:
    """List entries in the team's knowledge base.

    Args:
        category: What to list - 'all', 'key_files', 'directories', or 'files'
    """
    from app.rag.kb_search import list_kb_entries

    team_id = _get_team_id()
    if not team_id:
        return "No team is selected. The user must pick a team in the picker before the knowledge base can be used."

    async def _do():
        return await list_kb_entries(team_id=team_id, category=category)

    try:
        return _run_async(_do())
    except Exception as e:
        return f"Error listing KB entries: {e}"


@mcp.tool()
def read_file(file_path: str) -> str:
    """Read the full content of a file from the knowledge base.

    Args:
        file_path: Path to the file within the KB (supports fuzzy matching)
    """
    from app.rag.kb_search import read_kb_file

    team_id = _get_team_id()
    if not team_id:
        return "No team is selected. The user must pick a team in the picker before the knowledge base can be used."

    async def _do():
        return await read_kb_file(team_id=team_id, file_path=file_path)

    try:
        return _run_async(_do())
    except Exception as e:
        return f"Error reading KB file: {e}"


@mcp.tool()
def browse(path: str = "/") -> str:
    """Browse the knowledge base folder structure at a given path.

    Args:
        path: Folder path to browse (default '/' for root)
    """
    from app.rag.kb_search import browse_kb_structure

    team_id = _get_team_id()
    if not team_id:
        return "No team is selected. The user must pick a team in the picker before the knowledge base can be used."

    async def _do():
        return await browse_kb_structure(team_id=team_id, path=path)

    try:
        return _run_async(_do())
    except Exception as e:
        return f"Error browsing KB structure: {e}"


@mcp.tool()
def learn(title: str, content: str) -> str:
    """Store new knowledge into the team's knowledge base for future retrieval.

    Use this to save important facts, documentation, or learnings discovered during
    the conversation so they can be found later via search.

    Args:
        title: Short descriptive title for the knowledge entry (e.g. 'How to deploy to staging')
        content: The full text content to store
    """
    return "Learning into the legacy KB blob is disabled. Use TeamKnowParse training instead."


if __name__ == "__main__":
    mcp.run(transport="stdio")
