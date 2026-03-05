"""
ReTrace Tools Package.

Provides 6 tools for the LangGraph CodeAct agent:
  1. terminal       — Execute shell commands
  2. read_file      — Read file contents
  3. write_file     — Write text to files
  4. basic_search   — DuckDuckGo web search
  5. deep_websearch — Multi-source web research
  6. screenops      — Screen-based computer automation

Each tool is exposed both as a plain async/sync function and as a
LangChain StructuredTool for injection into the LangGraph sandbox.
"""

from typing import Any, Optional

from langchain_core.tools import StructuredTool

from app.tools.terminal import terminal
from app.tools.file_ops import read_file, write_file
from app.tools.basic_search import basic_search


def get_base_tools() -> list[StructuredTool]:
    """Return the always-available tools (no extra API keys required)."""
    return [
        StructuredTool.from_function(
            name="terminal",
            description=(
                "Execute a shell/terminal command on the system. "
                "Use for running commands, checking system info, executing scripts, "
                "managing files via CLI. Returns stdout + stderr."
            ),
            func=terminal,
        ),
        StructuredTool.from_function(
            name="read_file",
            description=(
                "Read and return the full text contents of a file. "
                "Use to examine source code, config files, logs, data files."
            ),
            func=read_file,
        ),
        StructuredTool.from_function(
            name="write_file",
            description=(
                "Write text content to a file (creates or overwrites). "
                "Use to save code, configs, reports, or any text data."
            ),
            func=write_file,
        ),
        StructuredTool.from_function(
            name="basic_search",
            description=(
                "Search the web via DuckDuckGo. Returns titles, URLs, and snippets. "
                "Use for quick fact-checks, finding documentation, looking up errors."
            ),
            func=basic_search,
        ),
    ]


def get_deep_websearch_tool(llm_client: Any, model: str) -> StructuredTool:
    """Return the deep_websearch tool (requires an LLM for result selection)."""
    from app.tools.deep_websearch import make_deep_websearch_fn

    fn = make_deep_websearch_fn(llm_client, model)
    return StructuredTool.from_function(
        name="deep_websearch",
        description=(
            "Comprehensive web research: searches the web, selects the top 3 most "
            "relevant results, scrapes their full content, and returns formatted "
            "information from multiple sources. Use for complex research questions "
            "that need detailed analysis from multiple web pages."
        ),
        func=fn,
    )


def get_screenops_tool(
    chat_model: Any,
    screenops_api_key: str,
    screenops_model: Optional[str] = None,
) -> Optional[StructuredTool]:
    """Return the screenops tool if dependencies are available.

    *chat_model* — LangChain ChatModel used for the vision step.
    *screenops_api_key* — Anthropic API key for the coordinate finder.
    *screenops_model* — Optional Anthropic model for the coordinate finder.
    """
    try:
        from app.tools.screenops.tool import build_screenops_tool
        return build_screenops_tool(chat_model, screenops_api_key, screenops_model)
    except ImportError:
        return None


def get_knowledge_base_tool(
    product_id: str,
    product_description: Optional[str] = None,
    api_key: str = "",
    model: str = "",
    base_url: Optional[str] = None,
) -> StructuredTool:
    """Return the knowledge base search tool for a specific product."""
    from app.tools.knowledge_base import make_knowledge_base_tool
    return make_knowledge_base_tool(
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
    product_id: Optional[str] = None,
    product_description: Optional[str] = None,
    api_key: str = "",
    api_url: Optional[str] = None,
) -> list[StructuredTool]:
    """Assemble the full tool list based on available credentials.

    Args:
        llm_client: AsyncOpenAI client for deep_websearch
        model: Model name for deep_websearch
        chat_model: LangChain ChatModel for screenops
        screenops_api_key: Anthropic API key for screenops
        screenops_model: Optional Anthropic model for ScreenOps coordinate finder
        product_id: Product ID for knowledge base tool (required for knowledge base tool)
        product_description: Optional product description to include in knowledge base tool
        api_key: LLM API key (used by knowledge base query expansion)
        api_url: LLM API base URL (used by knowledge base query expansion)
    """
    tools = get_base_tools()

    if product_id:
        tools.append(get_knowledge_base_tool(
            product_id=product_id,
            product_description=product_description,
            api_key=api_key,
            model=model,
            base_url=api_url,
        ))

    if llm_client and model:
        tools.append(get_deep_websearch_tool(llm_client, model))

    if chat_model and screenops_api_key:
        so = get_screenops_tool(chat_model, screenops_api_key, screenops_model)
        if so:
            tools.append(so)

    return tools


def get_tool_descriptions(tools: list[StructuredTool]) -> str:
    """Format tool list into a prompt-friendly string for the agent system prompt."""
    lines = ["## AVAILABLE TOOLS\n"]
    for t in tools:
        lines.append(f"**{t.name}**")
        lines.append(f"  {t.description}")
        # Include parameter info from schema if available
        if t.args_schema:
            fields = t.args_schema.model_fields
            params = ", ".join(f"{k}: {v.annotation.__name__ if hasattr(v.annotation, '__name__') else str(v.annotation)}" for k, v in fields.items())
            lines.append(f"  Signature: `{t.name}({params})`")
        lines.append("")
    return "\n".join(lines)


def get_tool_by_name(name: str):
    """Return a plain callable tool function by name (for automation execution)."""
    _tool_map = {
        "terminal": terminal,
        "read_file": read_file,
        "write_file": write_file,
        "basic_search": basic_search,
    }
    return _tool_map.get(name)
