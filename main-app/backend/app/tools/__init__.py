"""
ReTrace Tools Package.

Provides tools for the LangGraph CodeAct agent:
  1.  terminal        — Execute shell commands (persistent PTY per conversation)
  2.  read_file       — Read file contents
  3.  write_file      — Write text to files
  4.  delete_file     — Delete a file
  5.  download_file   — Download a file from a URL
  6.  str_replace     — Precise text replacement in files
  7.  grep            — Search file contents (ripgrep with Python fallback)
  8.  glob_search     — Find files by glob pattern
  9.  web_search      — Web search (Serper API / DuckDuckGo fallback)
  10. web_fetch       — Fetch URL content as readable text
  11. web_research    — Multi-source web research with LLM selection (requires LLM)
  12. web_advanced    — Advanced web search with operators (Serper API)
  13. todo_write      — In-session task management
  14. knowledge_base  — Team knowledge base (search, read, list, browse)
  15. auto_browser    — Granular browser control with intelligent content analysis
                        (navigate, analyze_page, click, vision_click, type, read, screenshot)

Each tool is exposed both as a plain async/sync function and as a
LangChain StructuredTool for injection into the LangGraph sandbox.
"""

from typing import Any, Callable, Optional

from langchain_core.tools import StructuredTool

from app.tools.terminal import terminal, make_terminal_for_conversation
from app.tools.file_ops import read_file, write_file, delete_file, download_file
from app.tools.web_search import web_search, make_web_research_fn, web_advanced
from app.tools.grep import grep
from app.tools.glob_tool import glob_search
from app.tools.str_replace import str_replace
from app.tools.web_fetch import web_fetch
from app.tools.todo import make_todo_for_conversation


# ---------------------------------------------------------------------------
# Shared tool descriptions — used by BOTH sandbox and host tool construction
# to ensure consistent behaviour regardless of execution mode.
# ---------------------------------------------------------------------------

_DESC_TERMINAL = (
    "Execute a shell/terminal command on the system. "
    "This is a persistent terminal session — environment variables, "
    "PATH changes, and working directory persist across calls. "
    "Use for running commands, installing packages, checking system info, "
    "executing scripts, managing files via CLI. Returns stdout + stderr. "
    "The command runs in real time and the user can see the output."
)

_DESC_READ_FILE = (
    "Read and return the full text contents of a file. "
    "Use to examine source code, config files, logs, data files. "
    "Supports relative and absolute paths."
)

_DESC_WRITE_FILE = (
    "Write text content to a file (creates or overwrites). "
    "Creates parent directories as needed. "
    "Use to save code, configs, reports, or any text data."
)

_DESC_DELETE_FILE = (
    "Delete a file at the given path. Refuses to delete directories. "
    "Fails safely if the file does not exist or permission is denied."
)

_DESC_DOWNLOAD_FILE = (
    "Download a file from a URL to a local path. Use for saving installers, "
    "archives, or any file from the web. Follows redirects; max 512 MB."
)

_DESC_STR_REPLACE = (
    "Replace exact text in a file. Performs precise string matching. "
    "Fails if old_string is not found or is ambiguous (multiple matches). "
    "Set replace_all=True to replace all occurrences. "
    "Preferred over write_file for targeted edits."
)

_DESC_GREP = (
    "Search file contents using regex. Powered by ripgrep (fast). "
    "Supports glob filters, case-insensitive mode, context lines. "
    "Output modes: 'content' (matching lines), 'files_with_matches', 'count'. "
    "Use instead of terminal('grep ...') for better performance."
)

_DESC_GLOB_SEARCH = (
    "Find files matching a glob pattern (e.g. '*.py', 'src/**/*.tsx'). "
    "Recursively searches from the given directory. "
    "Returns paths sorted by modification time (newest first) with sizes. "
    "Use for discovering files and understanding project structure."
)

_DESC_WEB_SEARCH = (
    "Search the web using Serper API (if configured) or DuckDuckGo. "
    "Returns titles, URLs, snippets, knowledge graph, 'people also ask', "
    "and related searches. Use for quick fact-checks, finding documentation, "
    "looking up errors."
)

_DESC_WEB_FETCH = (
    "Fetch a URL and return its content as clean readable text. "
    "Extracts article content from HTML (removes nav, ads, boilerplate). "
    "Works with HTML pages, JSON APIs, and plain text URLs."
)

_DESC_TODO_WRITE = (
    "Create or update a structured todo list for the current session. "
    "Pass a JSON array of {id, content, status} objects. "
    "Statuses: pending, in_progress, completed, cancelled. "
    "merge=True updates by id; merge=False replaces all. "
    "Use to plan and track multi-step tasks."
)

_DESC_WEB_ADVANCED = (
    "Advanced web search with full Serper API features. Supports advanced "
    "operators (site:, filetype:, inurl:, intitle:, before:, after:, etc.), "
    "region/language targeting, time filters, and pagination. Falls back to "
    "basic search if Serper is not available. Use for precise searches requiring "
    "specific filters or operators."
)

_DESC_AUTO_BROWSER = (
    "Control a real Chromium browser visible in the user's Workspace panel. "
    "Use file:///workspace/filename.html to open local files.\n\n"
    "CLICK PRIORITY: vision_click(text) > click_element(selector) > click(x, y).\n"
    "Use vision_click whenever you can see and describe the element on screen. "
    "Do NOT guess CSS selectors — a wrong guess wastes 10 seconds timing out. "
    "Only use click_element when you have confirmed the exact selector via evaluate_js or read_page. "
    "Use click(x, y) only as a last resort."
)


def get_base_tools(
    conversation_id: Optional[str] = None,
    output_callback: Optional[Callable[[str], None]] = None,
    sandbox_session: Optional[Any] = None,
    serper_api_key: str = "",
    coord_finder_invoker: Any = None,
    screenshot_delay_ms: int = 2000,
) -> list[StructuredTool]:
    """Return the always-available tools (no extra API keys required).

    When *conversation_id* is provided the terminal tool is backed by a
    persistent PTY session.  *output_callback* receives real-time terminal
    output chunks for SSE streaming.

    When *sandbox_session* is provided (an llm-sandbox SandboxSession),
    all tools execute inside the Podman container instead of on the host.

    *coord_finder_invoker* — optional callable for vision_click in auto_browser.
    """

    # ── Sandboxed tools (all execute inside Podman container) ──────────
    if sandbox_session:
        from app.tools.sandboxed import (
            make_sandboxed_terminal,
            make_sandboxed_read_file,
            make_sandboxed_write_file,
            make_sandboxed_delete_file,
            make_sandboxed_download_file,
            make_sandboxed_str_replace,
            make_sandboxed_grep,
            make_sandboxed_glob_search,
            make_sandboxed_web_search,
            make_sandboxed_web_fetch,
            make_sandboxed_todo_write,
            make_sandboxed_auto_browser,
        )

        serper_key = serper_api_key

        terminal_fn = make_sandboxed_terminal(sandbox_session, conversation_id, output_callback)
        read_file_fn = make_sandboxed_read_file(sandbox_session)
        write_file_fn = make_sandboxed_write_file(sandbox_session)
        delete_file_fn = make_sandboxed_delete_file(sandbox_session)
        download_file_fn = make_sandboxed_download_file(sandbox_session)
        str_replace_fn = make_sandboxed_str_replace(sandbox_session)
        grep_fn = make_sandboxed_grep(sandbox_session)
        glob_search_fn = make_sandboxed_glob_search(sandbox_session)
        web_search_fn = make_sandboxed_web_search(sandbox_session, serper_key)
        web_fetch_fn = make_sandboxed_web_fetch(sandbox_session)

        tools = [
            StructuredTool.from_function(name="terminal", func=terminal_fn, description=_DESC_TERMINAL),
            StructuredTool.from_function(name="read_file", func=read_file_fn, description=_DESC_READ_FILE),
            StructuredTool.from_function(name="write_file", func=write_file_fn, description=_DESC_WRITE_FILE),
            StructuredTool.from_function(name="delete_file", func=delete_file_fn, description=_DESC_DELETE_FILE),
            StructuredTool.from_function(name="download_file", func=download_file_fn, description=_DESC_DOWNLOAD_FILE),
            StructuredTool.from_function(name="str_replace", func=str_replace_fn, description=_DESC_STR_REPLACE),
            StructuredTool.from_function(name="grep", func=grep_fn, description=_DESC_GREP),
            StructuredTool.from_function(name="glob_search", func=glob_search_fn, description=_DESC_GLOB_SEARCH),
            StructuredTool.from_function(name="web_search", func=web_search_fn, description=_DESC_WEB_SEARCH),
            StructuredTool.from_function(name="web_fetch", func=web_fetch_fn, description=_DESC_WEB_FETCH),
        ]

        if conversation_id:
            todo_fn = make_sandboxed_todo_write(sandbox_session)
            tools.append(StructuredTool.from_function(name="todo_write", func=todo_fn, description=_DESC_TODO_WRITE))

        # auto_browser — Playwright + Chromium inside container
        auto_browser_fn = make_sandboxed_auto_browser(sandbox_session, output_callback, coord_finder_invoker, conversation_id, screenshot_delay_ms)
        tools.append(StructuredTool.from_function(
            name="auto_browser",
            func=auto_browser_fn,
            description=_DESC_AUTO_BROWSER,
        ))

        return tools

    # ── Original tools (direct host execution, no sandbox) ─────────────
    if conversation_id:
        terminal_fn = make_terminal_for_conversation(conversation_id, output_callback=output_callback)
    else:
        terminal_fn = terminal

    tools = [
        StructuredTool.from_function(name="terminal", func=terminal_fn, description=_DESC_TERMINAL),
        StructuredTool.from_function(name="read_file", func=read_file, description=_DESC_READ_FILE),
        StructuredTool.from_function(name="write_file", func=write_file, description=_DESC_WRITE_FILE),
        StructuredTool.from_function(name="delete_file", func=delete_file, description=_DESC_DELETE_FILE),
        StructuredTool.from_function(name="download_file", func=download_file, description=_DESC_DOWNLOAD_FILE),
        StructuredTool.from_function(name="str_replace", func=str_replace, description=_DESC_STR_REPLACE),
        StructuredTool.from_function(name="grep", func=grep, description=_DESC_GREP),
        StructuredTool.from_function(name="glob_search", func=glob_search, description=_DESC_GLOB_SEARCH),
        StructuredTool.from_function(name="web_search", func=web_search, description=_DESC_WEB_SEARCH),
        StructuredTool.from_function(name="web_fetch", func=web_fetch, description=_DESC_WEB_FETCH),
    ]

    if conversation_id:
        todo_fn = make_todo_for_conversation(conversation_id)
        tools.append(StructuredTool.from_function(name="todo_write", func=todo_fn, description=_DESC_TODO_WRITE))

    return tools


def get_web_research_tool(llm_client: Any, model: str) -> StructuredTool:
    """Return the web_research tool (requires an LLM for result selection)."""
    fn = make_web_research_fn(llm_client, model)
    return StructuredTool.from_function(
        name="web_research",
        description=(
            "Comprehensive web research: searches using Serper API (if configured) "
            "or DuckDuckGo, uses LLM to select the top 3 most relevant results, "
            "scrapes their full content, and returns formatted information from "
            "multiple sources. Use for complex research questions that need detailed "
            "analysis from multiple web pages."
        ),
        func=fn,
    )


def get_web_advanced_tool() -> StructuredTool:
    """Return the web_advanced tool (full-featured search with advanced operators)."""
    return StructuredTool.from_function(
        name="web_advanced",
        description=(
            "Advanced web search with full Serper API features. Supports advanced "
            "operators (site:, filetype:, inurl:, intitle:, before:, after:, etc.), "
            "region/language targeting, time filters, and pagination. Falls back to "
            "basic search if Serper is not available. Use for precise searches requiring "
            "specific filters or operators."
        ),
        func=web_advanced,
    )


def get_knowledge_base_tools(
    team_id: str,
    team_description: Optional[str] = None,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
) -> list[StructuredTool]:
    """Return the knowledge base tools for a specific team."""
    from app.tools.knowledge_base import make_knowledge_base_tools
    return make_knowledge_base_tools(
        team_id=team_id,
        team_description=team_description,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


def get_auto_browser_tool(
    chat_model: Any = None,
    conversation_id: Optional[str] = None,
    output_callback: Optional[Callable[[str], None]] = None,
    coord_finder_invoker: Any = None,
) -> Optional[StructuredTool]:
    """Return the auto_browser tool if Playwright is available."""
    try:
        from app.tools.auto_browser import build_auto_browser_tool
        return build_auto_browser_tool(
            chat_model=chat_model,
            conversation_id=conversation_id,
            output_callback=output_callback,
            coord_finder_invoker=coord_finder_invoker,
        )
    except ImportError:
        return None


def get_all_tools(
    llm_client: Any = None,
    model: str = "",
    chat_model: Any = None,
    screenops_api_key: str = "",
    screenops_model: Optional[str] = None,
    screenops_api_url: Optional[str] = None,
    screenops_mouse_timeout: int = 30,
    screenops_image_scale: int = 100,
    screenshot_delay_ms: int = 2000,
    team_id: Optional[str] = None,
    team_description: Optional[str] = None,
    api_key: str = "",
    api_url: Optional[str] = None,
    conversation_id: Optional[str] = None,
    output_callback: Optional[Callable[[str], None]] = None,
    sandbox_session: Optional[Any] = None,
    serper_api_key: str = "",
    main_event_loop: Optional[Any] = None,
    user_id: Optional[str] = None,
) -> list[StructuredTool]:
    """Assemble the full tool list based on available credentials.

    Args:
        llm_client: AsyncOpenAI client for web_research
        model: Model name for web_research
        chat_model: LangChain ChatModel for screenops
        screenops_api_key: API key for ScreenOps coordinate finder
        screenops_model: Model name for ScreenOps coordinate finder
        screenops_api_url: Base URL for ScreenOps coordinate finder API
        screenops_mouse_timeout: Seconds to wait for manual click in keyboard-only mode
        screenops_image_scale: 25-100, percentage of screenshot size sent to model
        team_id: Team ID for knowledge base tool
        team_description: Optional team description
        api_key: LLM API key (used by knowledge base query expansion)
        api_url: LLM API base URL
        conversation_id: Conversation ID for persistent PTY terminal session
        output_callback: Callback receiving real-time terminal output chunks
        sandbox_session: llm-sandbox SandboxSession for sandboxed execution
    """
    # ── Build coordinate finder invoker (if configured) ───────────
    # Built early so both sandbox and host auto_browser can use it.
    coord_finder_invoker = None
    if screenops_api_key:
        try:
            from app.tools.screenops.tool import _make_coordinate_finder_invoker
            from app.services.agent_service import _persist_llm_log
            import asyncio

            # Determine provider from API URL
            _coord_api_url = (screenops_api_url or "").strip()
            _coord_provider = "anthropic" if "anthropic" in _coord_api_url else "openai"
            _coord_model = screenops_model or "gpt-4o-mini"
            _loop = main_event_loop  # FastAPI event loop passed from agent_service

            def _coord_log_callback(messages, response_text, usage, latency_ms, model_name, error):
                """Persist coordinate finder LLM call to llm_logs table."""
                import json
                from uuid import uuid4
                req_json = json.dumps(messages, default=str)
                u = usage or {}
                log_row = {
                    "log_id": str(uuid4()),
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "iteration": 0,
                    "request_messages_json": req_json,
                    "response_content": (response_text or "")[:50000],
                    "input_tokens": u.get("prompt_tokens", 0),
                    "output_tokens": u.get("completion_tokens", 0),
                    "total_tokens": u.get("total_tokens", 0),
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "reasoning_tokens": 0,
                    "model_provider": _coord_provider,
                    "model_name": model_name or _coord_model,
                    "latency_ms": latency_ms,
                    "error": error,
                }

                async def _write():
                    from app.db.database import async_session_maker
                    from app.models.llm_log import LLMLog
                    async with async_session_maker() as db:
                        db.add(LLMLog(**log_row))
                        await db.commit()

                def _on_done(fut):
                    try:
                        fut.result()
                    except Exception as e:
                        logger.error("coord_log_callback_write_failed: %s", str(e))

                if _loop is not None and _loop.is_running():
                    try:
                        future = asyncio.run_coroutine_threadsafe(_write(), _loop)
                        future.add_done_callback(_on_done)
                        return
                    except Exception as e:
                        logger.error("coord_log_callback_schedule_failed: %s", str(e))
                try:
                    asyncio.run(_write())
                except Exception as e:
                    logger.error("coord_log_callback_run_failed: %s", str(e))

            coord_finder_invoker = _make_coordinate_finder_invoker(
                api_url=screenops_api_url or "",
                model=screenops_model or "gpt-4o-mini",
                api_key=screenops_api_key,
                log_callback=_coord_log_callback,
            )
        except ImportError:
            pass

    tools = get_base_tools(
        conversation_id=conversation_id,
        output_callback=output_callback,
        sandbox_session=sandbox_session,
        serper_api_key=serper_api_key,
        coord_finder_invoker=coord_finder_invoker,
        screenshot_delay_ms=screenshot_delay_ms,
    )

    # When sandboxed, web_advanced is already handled by sandboxed web_search.
    # web_research needs LLM client — for now, use host version.
    if sandbox_session:
        from app.tools.sandboxed import make_sandboxed_web_advanced
        serper_key = serper_api_key
        tools.append(StructuredTool.from_function(
            name="web_advanced",
            func=make_sandboxed_web_advanced(sandbox_session, serper_key),
            description=_DESC_WEB_ADVANCED,
        ))

    # ── Tools that stay on host regardless of sandbox ─────────────
    # knowledge_base needs direct DB access
    if team_id:
        tools.extend(get_knowledge_base_tools(
            team_id=team_id,
            team_description=team_description,
            api_key=api_key,
            model=model,
            base_url=api_url,
        ))

    if not sandbox_session:
        # These only run on host (non-sandboxed path)
        if llm_client and model:
            tools.append(get_web_research_tool(llm_client, model))

        # web_advanced is always available (falls back to basic search if Serper not configured)
        tools.append(get_web_advanced_tool())

    if chat_model:
        # AutoBrowser — host-side only when not sandboxed
        # (sandboxed version is added in get_base_tools above)
        if not sandbox_session:
            ab = get_auto_browser_tool(
                chat_model=chat_model,
                conversation_id=conversation_id,
                output_callback=output_callback,
                coord_finder_invoker=coord_finder_invoker,
            )
            if ab:
                tools.append(ab)

    return tools


def get_tool_descriptions(tools: list[StructuredTool]) -> str:
    """Format tool list into a prompt-friendly string for the agent system prompt."""
    lines = ["## AVAILABLE TOOLS\n"]
    for t in tools:
        lines.append(f"**{t.name}**")
        lines.append(f"  {t.description}")
        if t.args_schema and hasattr(t.args_schema, 'model_fields'):
            fields = t.args_schema.model_fields
            params = ", ".join(
                f"{k}: {v.annotation.__name__ if hasattr(v.annotation, '__name__') else str(v.annotation)}"
                for k, v in fields.items()
            )
            lines.append(f"  Signature: `{t.name}({params})`")
        lines.append("")
    return "\n".join(lines)


def get_tool_by_name(name: str):
    """Return a plain callable tool function by name (for automation execution)."""
    _tool_map = {
        "terminal": terminal,
        "read_file": read_file,
        "write_file": write_file,
        "delete_file": delete_file,
        "download_file": download_file,
        "str_replace": str_replace,
        "grep": grep,
        "glob_search": glob_search,
        "web_search": web_search,
        "web_fetch": web_fetch,
        "web_advanced": web_advanced,
    }
    return _tool_map.get(name)
