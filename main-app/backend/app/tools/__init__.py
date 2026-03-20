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
  13. screenops       — Screen-based computer automation
  14. todo_write      — In-session task management
  15. knowledge_base  — Product knowledge base (search, read, list, browse)

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


def get_base_tools(
    conversation_id: Optional[str] = None,
    output_callback: Optional[Callable[[str], None]] = None,
) -> list[StructuredTool]:
    """Return the always-available tools (no extra API keys required).

    When *conversation_id* is provided the terminal tool is backed by a
    persistent PTY session.  *output_callback* receives real-time terminal
    output chunks for SSE streaming.
    """
    if conversation_id:
        terminal_fn = make_terminal_for_conversation(conversation_id, output_callback=output_callback)
    else:
        terminal_fn = terminal

    tools = [
        StructuredTool.from_function(
            name="terminal",
            description=(
                "Execute a shell/terminal command on the system. "
                "This is a persistent terminal session — environment variables, "
                "PATH changes, and working directory persist across calls. "
                "Use for running commands, installing packages, checking system info, "
                "executing scripts, managing files via CLI. Returns stdout + stderr. "
                "The command runs in real time and the user can see the output."
            ),
            func=terminal_fn,
        ),
        StructuredTool.from_function(
            name="read_file",
            description=(
                "Read and return the full text contents of a file. "
                "Use to examine source code, config files, logs, data files. "
                "Supports relative and absolute paths."
            ),
            func=read_file,
        ),
        StructuredTool.from_function(
            name="write_file",
            description=(
                "Write text content to a file (creates or overwrites). "
                "Creates parent directories as needed. "
                "Use to save code, configs, reports, or any text data."
            ),
            func=write_file,
        ),
        StructuredTool.from_function(
            name="delete_file",
            description=(
                "Delete a file at the given path. Refuses to delete directories. "
                "Fails safely if the file does not exist or permission is denied."
            ),
            func=delete_file,
        ),
        StructuredTool.from_function(
            name="download_file",
            description=(
                "Download a file from a URL to a local path. Use for saving installers, "
                "archives, or any file from the web. Follows redirects; max 512 MB."
            ),
            func=download_file,
        ),
        StructuredTool.from_function(
            name="str_replace",
            description=(
                "Replace exact text in a file. Performs precise string matching. "
                "Fails if old_string is not found or is ambiguous (multiple matches). "
                "Set replace_all=True to replace all occurrences. "
                "Preferred over write_file for targeted edits."
            ),
            func=str_replace,
        ),
        StructuredTool.from_function(
            name="grep",
            description=(
                "Search file contents using regex. Powered by ripgrep (fast). "
                "Supports glob filters, case-insensitive mode, context lines. "
                "Output modes: 'content' (matching lines), 'files_with_matches', 'count'. "
                "Use instead of terminal('grep ...') for better performance."
            ),
            func=grep,
        ),
        StructuredTool.from_function(
            name="glob_search",
            description=(
                "Find files matching a glob pattern (e.g. '*.py', 'src/**/*.tsx'). "
                "Recursively searches from the given directory. "
                "Returns paths sorted by modification time (newest first) with sizes. "
                "Use for discovering files and understanding project structure."
            ),
            func=glob_search,
        ),
        StructuredTool.from_function(
            name="web_search",
            description=(
                "Search the web using Serper API (if configured) or DuckDuckGo. "
                "Returns titles, URLs, snippets, knowledge graph, 'people also ask', "
                "and related searches. Use for quick fact-checks, finding documentation, "
                "looking up errors."
            ),
            func=web_search,
        ),
        StructuredTool.from_function(
            name="web_fetch",
            description=(
                "Fetch a URL and return its content as clean readable text. "
                "Extracts article content from HTML (removes nav, ads, boilerplate). "
                "Works with HTML pages, JSON APIs, and plain text URLs."
            ),
            func=web_fetch,
        ),
    ]

    if conversation_id:
        todo_fn = make_todo_for_conversation(conversation_id)
        tools.append(StructuredTool.from_function(
            name="todo_write",
            description=(
                "Create or update a structured todo list for the current session. "
                "Pass a JSON array of {id, content, status} objects. "
                "Statuses: pending, in_progress, completed, cancelled. "
                "merge=True updates by id; merge=False replaces all. "
                "Use to plan and track multi-step tasks."
            ),
            func=todo_fn,
        ))

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


def get_screenops_tool(
    chat_model: Any,
    screenops_api_key: str = "",
    screenops_model: Optional[str] = None,
    screenops_api_url: Optional[str] = None,
    screenops_mouse_timeout: int = 30,
    screenops_image_scale: int = 100,
) -> Optional[StructuredTool]:
    """Return the screenops tool if dependencies are available."""
    try:
        from app.tools.screenops.tool import build_screenops_tool
        return build_screenops_tool(
            chat_model, screenops_api_key, screenops_model,
            screenops_api_url, screenops_mouse_timeout, screenops_image_scale,
        )
    except ImportError:
        return None


def get_knowledge_base_tools(
    product_id: str,
    product_description: Optional[str] = None,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
) -> list[StructuredTool]:
    """Return the knowledge base tools for a specific product."""
    from app.tools.knowledge_base import make_knowledge_base_tools
    return make_knowledge_base_tools(
        product_id=product_id,
        product_description=product_description,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


def get_all_tools(
    llm_client: Any = None,
    model: str = "",
    chat_model: Any = None,
    screenops_api_key: str = "",
    screenops_model: Optional[str] = None,
    screenops_api_url: Optional[str] = None,
    screenops_mouse_timeout: int = 30,
    screenops_image_scale: int = 100,
    product_id: Optional[str] = None,
    product_description: Optional[str] = None,
    api_key: str = "",
    api_url: Optional[str] = None,
    conversation_id: Optional[str] = None,
    output_callback: Optional[Callable[[str], None]] = None,
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
        product_id: Product ID for knowledge base tool
        product_description: Optional product description
        api_key: LLM API key (used by knowledge base query expansion)
        api_url: LLM API base URL
        conversation_id: Conversation ID for persistent PTY terminal session
        output_callback: Callback receiving real-time terminal output chunks
    """
    tools = get_base_tools(conversation_id=conversation_id, output_callback=output_callback)

    if product_id:
        tools.extend(get_knowledge_base_tools(
            product_id=product_id,
            product_description=product_description,
            api_key=api_key,
            model=model,
            base_url=api_url,
        ))

    if llm_client and model:
        tools.append(get_web_research_tool(llm_client, model))
    
    # web_advanced is always available (falls back to basic search if Serper not configured)
    tools.append(get_web_advanced_tool())

    if chat_model:
        so = get_screenops_tool(
            chat_model, screenops_api_key, screenops_model,
            screenops_api_url, screenops_mouse_timeout, screenops_image_scale,
        )
        if so:
            tools.append(so)

    return tools


def get_tool_descriptions(tools: list[StructuredTool]) -> str:
    """Format tool list into a prompt-friendly string for the agent system prompt."""
    lines = ["## AVAILABLE TOOLS\n"]
    for t in tools:
        lines.append(f"**{t.name}**")
        lines.append(f"  {t.description}")
        if t.args_schema:
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
