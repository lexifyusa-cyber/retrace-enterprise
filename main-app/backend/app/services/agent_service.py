"""
Agent service — LangGraph CodeAct agent powered by the ReTrace knowledge base.

Ported from IQWorksAtlas langgraph_codeact/__init__.py (create_codeact) and
interactive_tool_agent.py (setup_agent_async).

Pipeline:
  1. Load LLM settings → build LangChain ChatModel
  2. Build StructuredTool instances (including knowledge base tool)
  3. Create LangGraph StateGraph (CodeAct pattern)
  4. Stream execution via agent.astream() → SSE events
"""

import io
import inspect
import json
import re
import sys
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
import asyncio
import queue
import threading
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict, Annotated

from app.core.config import settings as app_settings
from app.tools.utils import extract_python_code, extract_tool_call_as_python, strip_text_from_code_response

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    script: Optional[str]
    context: dict
    current_iteration: int
    max_iterations: int
    is_final_answer: bool
    syntax_error_count: int
    has_native_tool_calls: bool


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_AGENT_SYSTEM_TEMPLATE = """\
You are the ReTrace AI agent by Lumena Technologies for the product **{product_name}**.

{user_platform_section}

{product_description_section}

You can execute Python code to interact with the system using the tools listed below.

## HOW TO USE TOOLS

**CRITICAL:** To call tools, output only a ```python code block with real Python that calls the tools (e.g. terminal(...), web_search(...)) and print() the result. Only ```python blocks are run; no other format is executed. When you have the answer for the user, respond with plain text only (no code block).

Generate a Python code block to call tools.  **Always `print()` results** so you
can see the output.

```python
result = terminal("ls -la")
print(result)
```

When you have a complete answer, respond with **plain text only** (no code block).
That text becomes the final answer shown to the user.

## RULES
- Generate Python code in ```python blocks to call tools.
- Always print() results so you can see output.
- When done, respond with plain text (no code block) — that is your final answer.
- Only use the tools listed under AVAILABLE TOOLS below. Do NOT call tools that are not listed.
{kb_rules_section}- You can call tools multiple times with different queries to gather comprehensive information.
- If the evidence is insufficient, say so.
- Do NOT generate code after your final text answer.
- Maximum {max_iterations} tool-use iterations allowed.
- Do NOT use `import os`, `import subprocess`, `import shutil`, `import pathlib`, or other Python standard library modules for system operations. Always use the provided tools instead (e.g. `terminal("ls -la")`, `read_file("path")`, `write_file("path", "content")`).
- Do NOT use `open()`, `os.listdir()`, `Path()`, or any direct Python file I/O. Use the provided `read_file`, `write_file`, `delete_file`, and `terminal` tools for all file and system operations.
- Only use `import` for data processing libraries (e.g. `json`, `re`, `math`) when needed to parse or format tool output.
- When using the `screenops` tool: it returns a JSON object with a "result" field (and optionally "iterations"). Before writing to a file or using the content, parse the return value: if it is a string, use `json.loads(screenops_result)` to get a dict, then use the "result" field only (e.g. `description_text = data["result"]` or `data.get("result", "")`). Pass that string to `write_file`, not the raw return value.

## TOOL SELECTION GUIDE
- **Finding files by name/pattern**: use `glob_search("*.py")` — NOT `terminal("find ...")`
- **Searching file contents**: use `grep("pattern", "path")` — NOT `terminal("grep ...")`
- **Editing files (targeted changes)**: use `str_replace(file, old, new)` — NOT rewriting the whole file
- **Deleting files**: use `delete_file("path")` — NOT `terminal("rm ...")`
- **Reading web pages**: use `web_fetch("url")` — returns clean text
- **Web search**: use `web_search("query")` for quick lookups, `web_research("query")` for comprehensive research, `web_advanced(...)` for advanced searches with operators
- **Multi-step tasks**: use `todo_write(...)` to plan and track progress

- **SSH**: ALWAYS pass the remote command inline: `terminal('ssh user@host "cmd"')`. NEVER open an interactive session (`ssh user@host` alone) — it will timeout.

{tool_descriptions}
"""

_KB_RULES = """\
- You have one **knowledge_base** tool with four actions (pass the `action` parameter):
  * `knowledge_base(action="search", query="...", keywords=[...], top_k=10)` — **Use this FIRST for any product question.** Fast hybrid search. Always provide a `keywords` list of 5-15 synonyms, related terms, and alternative phrasings alongside your query to maximize recall.
  * `knowledge_base(action="read_file", file_path="...")` — Read the full content of a specific file. Use after search to get complete details. Supports fuzzy path matching.
  * `knowledge_base(action="list", category="all")` — List what's in the KB. Categories: 'all', 'key_files', 'directories', 'files'. Only use when the user asks "what's in this product" or you need a broad overview.
  * `knowledge_base(action="browse", path="/some/path")` — Browse the folder tree at a path. Like `ls` for the KB.
- **Strategy**: For most questions, a single `search` call with good keywords is enough. Only use `list` for broad discovery, `read_file` to drill into a specific file from search results, and `browse` to explore a directory.
- When calling search, always include a `keywords` list with synonyms and related terms. Example: query="authentication" → keywords=["auth", "login", "password", "credentials", "oauth", "token", "session", "security"].
"""


def _platform_label(platform: Optional[str]) -> str:
    """Return a human-readable OS label for the system prompt."""
    if not platform or not str(platform).strip():
        return ""
    p = str(platform).strip().lower()
    if p in ("darwin", "mac", "macos", "os x"):
        return "User platform: **macOS (darwin)**. Use Unix paths (e.g. ~/Desktop), .dmg/.zip for downloads, and commands appropriate for macOS."
    if p in ("win32", "windows", "win"):
        return "User platform: **Windows**. Use Windows paths (e.g. %USERPROFILE%\\Desktop), .exe/.msi for downloads, and commands appropriate for Windows."
    if p in ("linux", "linux2"):
        return "User platform: **Linux**. Use Unix paths (e.g. ~/Desktop) and commands appropriate for Linux."
    return f"User platform: **{platform}**. Use paths and commands appropriate for this OS."


def _build_system_prompt(
    product_name: str,
    tool_descriptions: str,
    product_description: Optional[str] = None,
    kb_summary: Optional[str] = None,
    max_iterations: int = 10,
    available_tool_names: Optional[list[str]] = None,
    user_platform: Optional[str] = None,
) -> str:
    sections: list[str] = []
    has_kb = available_tool_names is None or "knowledge_base" in (available_tool_names or [])

    user_platform_section = _platform_label(user_platform)
    if user_platform_section:
        user_platform_section = user_platform_section + "\n\n"

    if product_description:
        sections.append(f"## PRODUCT DESCRIPTION\n\n{product_description}")

    if kb_summary:
        sections.append(f"## PRODUCT KNOWLEDGE SUMMARY (from trained KB)\n\n{kb_summary}")

    if has_kb:
        if sections:
            sections.append(
                "This product has a trained knowledge base. Use `knowledge_base(action=\"search\", query=\"...\", keywords=[...])` "
                "to query it. For simple overview questions, the summary above may already contain the answer — respond directly without a tool call if so."
            )
        elif product_name and product_name != "General Assistant":
            sections.append(
                "This product has a trained knowledge base. Use `knowledge_base(action=\"search\", query=\"...\", keywords=[...])` "
                "to query it when you need specific information about the product."
            )
        else:
            sections.append(
                "No product is selected. You are operating as a general-purpose assistant. "
                "Use the available tools (terminal, file operations, web search, etc.) to help the user."
            )
    else:
        sections.append(
            "No knowledge base tool is available. "
            "Use the available tools (terminal, file operations, web search, etc.) to help the user."
        )

    product_desc_section = "\n\n".join(sections)

    return _AGENT_SYSTEM_TEMPLATE.format(
        product_name=product_name,
        tool_descriptions=tool_descriptions,
        product_description_section=product_desc_section,
        user_platform_section=user_platform_section,
        kb_rules_section=_KB_RULES if has_kb else "",
        max_iterations=max_iterations,
    )




# ---------------------------------------------------------------------------
# Gemini detection (for thought_signature / tool_call compatibility)
# ---------------------------------------------------------------------------

def _is_gemini_model(llm_settings: dict) -> bool:
    """Return True when the chat model is Google Gemini.

    Gemini thinking models require thought_signature on function calls.
    We avoid synthetic tool_calls for Gemini so the API does not reject the conversation.
    """
    api_url = (llm_settings.get("api_url") or "").strip()
    model_name = (llm_settings.get("model_name") or "").strip()
    return (
        "generativelanguage.googleapis.com" in api_url
        or "aiplatform.googleapis.com" in api_url
        or model_name.startswith("gemini")
    )


# ---------------------------------------------------------------------------
# LangChain model builder
# ---------------------------------------------------------------------------

def _build_langchain_model(llm_settings: dict) -> Any:
    """Create a LangChain ChatModel from ReTrace LLM settings."""
    provider = llm_settings.get("provider", "openai")
    api_key = llm_settings.get("api_key", "")
    model_name = llm_settings.get("model_name", app_settings.REASONING_MODEL)
    api_url = llm_settings.get("api_url")

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            api_key=api_key,
            model=model_name,
        )
    else:
        # openai or custom — both use OpenAI-compatible API
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "model": model_name,
        }
        if api_url:
            kwargs["base_url"] = api_url
        return ChatOpenAI(**kwargs)


# ---------------------------------------------------------------------------
# CodeAct graph builder (ported from langgraph_codeact/__init__.py)
# ---------------------------------------------------------------------------

def _build_codeact_graph(
    model: Any,
    tools: list[StructuredTool],
    system_prompt: str,
    max_iterations: int = 10,
    use_gemini_compat: bool = False,
) -> StateGraph:
    """Build the LangGraph CodeAct agent graph.

    When use_gemini_compat is True (Gemini models), we avoid synthetic tool_calls
    and represent code execution as assistant text + user message with result,
    so Gemini never sees function_call parts that require thought_signature.

    Nodes:
      call_model — invoke LLM, extract Python code
      sandbox    — execute code with tools in scope

    Edges:
      START → call_model
      call_model →(code)→ sandbox
      call_model →(text)→ END
      sandbox → call_model
    """

    # Map tool names → callable functions for the sandbox
    tools_context: dict[str, Any] = {}

    def _make_sync_mcp_wrapper(t):
        """Wrap an async MCP tool so it can be called synchronously from exec()."""
        def sync_wrapper(**kwargs):
            import asyncio as _aio
            import concurrent.futures
            try:
                loop = _aio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(_aio.run, t.ainvoke(kwargs)).result(timeout=30)
            else:
                return _aio.run(t.ainvoke(kwargs))
        return sync_wrapper

    for tool in tools:
        if isinstance(tool, StructuredTool) and tool.func is not None:
            tools_context[tool.name] = tool.func
        elif hasattr(tool, 'ainvoke'):
            # MCP adapter tools are async-only — wrap for sync sandbox
            tools_context[getattr(tool, "name", str(tool))] = _make_sync_mcp_wrapper(tool)
        elif hasattr(tool, 'func') and tool.func is not None:
            tools_context[getattr(tool, "name", str(tool))] = tool.func
        else:
            tools_context[getattr(tool, "name", str(tool))] = getattr(tool, "run", tool)

    # ── call_model node ──────────────────────────────────────────────
    def call_model(state: AgentState):
        current_iteration = state.get("current_iteration", 0) + 1
        max_iter = state.get("max_iterations", max_iterations)
        syntax_error_count = state.get("syntax_error_count", 0)

        # Guard: too many syntax errors
        if syntax_error_count >= 3:
            msg = AIMessage(content="I encountered multiple syntax errors. Stopping.")
            return {
                "messages": [msg],
                "current_iteration": current_iteration,
                "is_final_answer": True,
                "script": None,
                "syntax_error_count": 0,
            }

        # Guard: max iterations
        if current_iteration > max_iter:
            msg = AIMessage(content=f"Maximum iterations ({max_iter}) reached. Please simplify your request.")
            return {
                "messages": [msg],
                "current_iteration": current_iteration,
                "is_final_answer": True,
                "script": None,
            }

        messages = [{"role": "system", "content": system_prompt}] + state["messages"]

        response = model.invoke(messages)

        # Strip explanatory text from code responses to prevent echo loops
        if hasattr(response, "content") and isinstance(response.content, str) and "```" in response.content:
            stripped = strip_text_from_code_response(response.content)
            if stripped != response.content:
                response.content = stripped

        # Extract Python code
        raw_content = response.content if hasattr(response, "content") else str(response)
        code = extract_python_code(raw_content)

        # Fallback: model may use its native tool-call format (XML, JSON, bare call)
        if not code:
            tool_names = set(tools_context.keys())
            code = extract_tool_call_as_python(raw_content, known_tools=tool_names)

        if code:
            # Add synthetic tool_call for OpenAI message format compatibility.
            # Skip for Gemini: it requires thought_signature on function_call parts,
            # so we represent code execution as text + user message instead.
            if not use_gemini_compat:
                tool_call_id = f"code_exec_{uuid4().hex[:8]}"
                response.tool_calls = [{
                    "name": "code_execution",
                    "args": {},
                    "id": tool_call_id,
                }]
            return {
                "messages": [response],
                "script": code,
                "is_final_answer": False,
                "current_iteration": current_iteration,
            }
        else:
            return {
                "messages": [response],
                "script": None,
                "is_final_answer": True,
                "current_iteration": current_iteration,
            }

    # ── sandbox node ─────────────────────────────────────────────────
    def sandbox(state: AgentState):
        code = state.get("script", "")
        existing_context = state.get("context", {})
        exec_globals = {**existing_context, **tools_context}

        # Capture stdout
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                exec(code, exec_globals)
            output = buf.getvalue()
        except Exception:
            output = buf.getvalue() + "\n" + traceback.format_exc()

        # Limit output size
        if len(output) > 50_000:
            output = output[:50_000] + "\n[output truncated]"

        if not output.strip():
            output = "(no output)"

        # Track syntax errors
        syntax_error_count = state.get("syntax_error_count", 0)
        if "SyntaxError" in output:
            syntax_error_count += 1
        else:
            syntax_error_count = 0

        # Merge new variables into context (excluding tools, builtins,
        # and non-serializable types like modules, functions, classes)
        import types as _types
        _skip_types = (_types.ModuleType, _types.FunctionType, _types.BuiltinFunctionType, type)
        new_context = {k: v for k, v in exec_globals.items()
                       if k not in tools_context and not k.startswith("__")
                       and not isinstance(v, _skip_types)}

        # For Gemini we use a user message instead of ToolMessage so we never send
        # tool_call parts that lack thought_signature.
        if use_gemini_compat:
            return {
                "messages": [HumanMessage(content=f"Code execution result:\n\n{output.rstrip()}")],
                "context": {**existing_context, **new_context},
                "syntax_error_count": syntax_error_count,
            }

        # Build tool message (OpenAI/Anthropic path)
        tool_call_id = "fallback_code_exec"
        msgs = state.get("messages", [])
        if msgs:
            last = msgs[-1]
            tc = getattr(last, "tool_calls", [])
            if tc:
                tool_call_id = tc[0].get("id", tool_call_id) if isinstance(tc[0], dict) else getattr(tc[0], "id", tool_call_id)

        return {
            "messages": [ToolMessage(content=output.rstrip(), tool_call_id=tool_call_id, name="code_execution")],
            "context": {**existing_context, **new_context},
            "syntax_error_count": syntax_error_count,
        }

    # ── routing function ─────────────────────────────────────────────
    def should_continue(state: AgentState) -> str:
        if state.get("current_iteration", 0) >= state.get("max_iterations", max_iterations):
            return "END"
        if state.get("is_final_answer", False):
            return "END"
        if state.get("script"):
            return "EXECUTE"
        return "END"

    # ── build graph ──────────────────────────────────────────────────
    graph = StateGraph(AgentState)
    graph.add_node("call_model", call_model)
    graph.add_node("sandbox", sandbox)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", should_continue, {
        "END": END,
        "EXECUTE": "sandbox",
    })
    graph.add_edge("sandbox", "call_model")

    return graph


# ---------------------------------------------------------------------------
# MCP Builder graph (fully native tool calls — no CodeAct sandbox)
# ---------------------------------------------------------------------------


def _build_mcp_builder_graph(
    model: Any,
    tools: list[StructuredTool],
    system_prompt: str,
    max_iterations: int = 60,
    use_gemini_compat: bool = False,
) -> StateGraph:
    """Build the native-tool-calling LangGraph agent for MCP Builder.

    ALL tools are bound as native structured tools via model.bind_tools().
    No CodeAct sandbox — the LLM calls tools directly via JSON tool calls.
    This keeps agent chat untouched (uses _build_codeact_graph).
    """

    # ── ALL tools are native in MCP Builder (no CodeAct sandbox) ────
    native_tools: list[StructuredTool] = []
    for tool in tools:
        native_tools.append(tool)

    # Bind ALL tools to the model so the LLM calls them via structured JSON
    model_with_tools = model.bind_tools(native_tools) if native_tools else model

    # Map for executing native tool calls by name
    native_tools_by_name: dict[str, Any] = {}
    for tool in native_tools:
        fn = tool.func if tool.func is not None else tool.coroutine
        native_tools_by_name[tool.name] = fn

    # Empty sandbox context — not used in MCP Builder mode
    tools_context: dict[str, Any] = {}

    # ── call_model node ──────────────────────────────────────────────
    def call_model(state: AgentState):
        current_iteration = state.get("current_iteration", 0) + 1
        max_iter = state.get("max_iterations", max_iterations)
        syntax_error_count = state.get("syntax_error_count", 0)

        if syntax_error_count >= 3:
            msg = AIMessage(content="I encountered multiple syntax errors. Stopping.")
            return {
                "messages": [msg],
                "current_iteration": current_iteration,
                "is_final_answer": True,
                "script": None,
                "has_native_tool_calls": False,
                "syntax_error_count": 0,
            }

        if current_iteration > max_iter:
            msg = AIMessage(content=f"Maximum iterations ({max_iter}) reached.")
            return {
                "messages": [msg],
                "current_iteration": current_iteration,
                "is_final_answer": True,
                "script": None,
                "has_native_tool_calls": False,
            }

        messages = [{"role": "system", "content": system_prompt}] + state["messages"]
        response = model_with_tools.invoke(messages)

        # ── Log LLM response for debugging ─────────────────────────
        response_text = getattr(response, "content", "") or ""
        all_tc = getattr(response, "tool_calls", None) or []
        logger.info(
            "mcp_builder_llm_response",
            iteration=current_iteration,
            response_length=len(response_text),
            response_preview=response_text[:500] if response_text else "(empty)",
            tool_calls_count=len(all_tc),
            tool_calls=[{"name": tc.get("name"), "args_keys": list(tc.get("args", {}).keys())} for tc in all_tc[:10]],
        )

        # ── Check for native structured tool calls ─────────────────
        native_tc = getattr(response, "tool_calls", None) or []
        valid_native_tc = [tc for tc in native_tc if tc.get("name") in native_tools_by_name]

        if valid_native_tc:
            logger.info("mcp_builder_has_tool_calls", iteration=current_iteration, tools=[tc.get("name") for tc in valid_native_tc])
            return {
                "messages": [response],
                "script": None,
                "is_final_answer": False,
                "has_native_tool_calls": True,
                "current_iteration": current_iteration,
            }

        # ── No tool calls — treat as final answer ────────────────
        logger.info("mcp_builder_final_answer", iteration=current_iteration, answer_preview=response_text[:300] if response_text else "(empty)")
        return {
            "messages": [response],
            "script": None,
            "is_final_answer": True,
            "has_native_tool_calls": False,
            "current_iteration": current_iteration,
        }

    # ── execute_native_tools node ─────────────────────────────────────
    def execute_native_tools(state: AgentState):
        """Execute native structured tool calls and return ToolMessages."""
        msgs = state.get("messages", [])
        if not msgs:
            return {"messages": [], "has_native_tool_calls": False}

        last_msg = msgs[-1]
        tool_calls = getattr(last_msg, "tool_calls", []) or []
        result_messages = []

        for tc in tool_calls:
            tc_name = tc.get("name", "")
            tc_args = tc.get("args", {})
            tc_id = tc.get("id", f"native_{uuid4().hex[:8]}")

            logger.info("mcp_builder_tool_exec", tool_name=tc_name, args_keys=list(tc_args.keys()), args_preview={k: str(v)[:100] for k, v in tc_args.items()})

            fn = native_tools_by_name.get(tc_name)
            if fn is None:
                result_messages.append(
                    ToolMessage(content=f"Error: unknown tool '{tc_name}'", tool_call_id=tc_id, name=tc_name)
                )
                continue

            try:
                if asyncio.iscoroutinefunction(fn):
                    # Async tool — run in event loop
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import concurrent.futures
                        future = asyncio.run_coroutine_threadsafe(fn(**tc_args), loop)
                        result = future.result(timeout=120)
                    else:
                        result = asyncio.run(fn(**tc_args))
                else:
                    result = fn(**tc_args)
                output = str(result) if result is not None else "(no output)"
            except Exception:
                output = traceback.format_exc()

            if len(output) > 50_000:
                output = output[:50_000] + "\n[output truncated]"

            result_messages.append(
                ToolMessage(content=output.rstrip(), tool_call_id=tc_id, name=tc_name)
            )

        return {
            "messages": result_messages,
            "has_native_tool_calls": False,
        }

    # ── routing function ─────────────────────────────────────────────
    def should_continue(state: AgentState) -> str:
        if state.get("current_iteration", 0) >= state.get("max_iterations", max_iterations):
            return "END"
        if state.get("is_final_answer", False):
            return "END"
        if state.get("has_native_tool_calls", False):
            return "NATIVE_TOOLS"
        return "END"

    # ── build graph (no sandbox — all tools are native) ──────────────
    graph = StateGraph(AgentState)
    graph.add_node("call_model", call_model)
    graph.add_node("execute_native_tools", execute_native_tools)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", should_continue, {
        "END": END,
        "NATIVE_TOOLS": "execute_native_tools",
    })
    graph.add_edge("execute_native_tools", "call_model")

    return graph


# ---------------------------------------------------------------------------
# MCP extension tool loading
# ---------------------------------------------------------------------------

async def _load_mcp_extension_tools(
    session: AsyncSession,
    user_auth_context: Optional[dict] = None,
) -> list:
    """Load tools from enabled MCP extensions and convert to StructuredTools."""
    from sqlalchemy import select
    from app.models.mcp_extension import McpExtension
    from app.models.user_extension import UserExtension
    from app.services.mcp_client_service import mcp_client_manager
    from app.tools.mcp_bridge import build_mcp_tools

    user_id = (user_auth_context or {}).get("user_id")
    if not user_id:
        return []

    # Auto-provision any auto_install=True extensions this user doesn't have yet
    tenant_id = (user_auth_context or {}).get("tenant_id") or None
    try:
        from app.api.mcp_extensions import _ensure_auto_install_extensions_provisioned
        await _ensure_auto_install_extensions_provisioned(session, user_id, tenant_id)
        await session.commit()  # persist before the main SELECT reads them back
    except Exception as e:
        logger.warning("auto_install_provision_skipped", error=str(e))
        await session.rollback()

    # Load this user's installed + enabled extensions, joined with catalog
    result = await session.execute(
        select(UserExtension, McpExtension)
        .join(McpExtension, UserExtension.extension_id == McpExtension.extension_id)
        .where(UserExtension.user_id == user_id)
        .where(UserExtension.is_enabled == True)
        .where(McpExtension.is_enabled == True)
    )
    rows = result.all()
    logger.info("mcp_user_extensions_query", user_id=user_id, row_count=len(rows))
    if not rows:
        return []

    all_tools = []

    # Fetch user's external auth token if needed (with auto-refresh)
    user_auth_token = None
    user_session = None
    if user_auth_context and user_auth_context.get("user_id"):
        try:
            from app.models.user_session import UserSession
            from datetime import datetime, timedelta
            sess_result = await session.execute(
                select(UserSession)
                .where(UserSession.user_id == user_auth_context["user_id"])
                .where(UserSession.revoked == False)
                .order_by(UserSession.last_active_at.desc())
                .limit(1)
            )
            user_session = sess_result.scalar_one_or_none()
            if user_session and user_session.external_access_token:
                # Auto-refresh if token expires within 5 minutes
                needs_refresh = (
                    user_session.external_token_expires_at
                    and user_session.external_token_expires_at < datetime.utcnow() + timedelta(minutes=5)
                    and user_session.external_refresh_token
                )
                if needs_refresh:
                    try:
                        from app.models.tenant import Tenant
                        from app.services.auth.azure_ad_auth import refresh_azure_ad_token
                        tenant_result = await session.execute(
                            select(Tenant).where(Tenant.tenant_id == user_session.tenant_id)
                        )
                        tenant = tenant_result.scalar_one_or_none()
                        if tenant and tenant.azure_ad_config:
                            refresh_result = await refresh_azure_ad_token(
                                user_session.external_refresh_token, tenant.azure_ad_config
                            )
                            if "access_token" in refresh_result:
                                user_session.external_access_token = refresh_result["access_token"]
                                if refresh_result.get("id_token"):
                                    user_session.external_id_token = refresh_result["id_token"]
                                user_session.external_refresh_token = refresh_result.get(
                                    "refresh_token", user_session.external_refresh_token
                                )
                                user_session.external_token_expires_at = (
                                    datetime.utcnow() + timedelta(seconds=refresh_result.get("expires_in", 3600))
                                )
                                await session.commit()
                                logger.info("azure_ad_token_auto_refreshed", user_id=user_auth_context["user_id"])
                            else:
                                logger.warning("azure_ad_token_auto_refresh_failed", error=refresh_result.get("error"))
                    except Exception as refresh_exc:
                        logger.warning("azure_ad_token_auto_refresh_error", error=str(refresh_exc))
                # Prefer ID token for ServiceNow (aud=client_id) over access token (aud=graph)
                user_auth_token = user_session.external_id_token or user_session.external_access_token
        except Exception:
            pass  # No token available — fall through to env-based auth

    # Load LDAP credentials for ldap_passthrough extensions
    user_auth_credentials = None
    try:
        if user_session and user_session.external_username and user_session.external_password:
            from app.core.encryption import decrypt_value
            user_auth_credentials = {
                "username": user_session.external_username,
                "password": decrypt_value(user_session.external_password),
            }
    except Exception:
        pass  # No LDAP credentials — fall through

    for user_ext, catalog_ext in rows:
        try:
            # Merge catalog defaults with user overrides (user wins)
            merged_env = user_ext.get_merged_env_vars(catalog_ext.get_raw_env_vars())
            merged_auth = {**(catalog_ext.auth_config_json or {}), **(user_ext.auth_config_json or {})}

            tool_infos = await mcp_client_manager.start_extension(
                extension_id=catalog_ext.extension_id,
                extension_name=catalog_ext.name,
                transport=getattr(catalog_ext, "transport", "stdio") or "stdio",
                url=getattr(catalog_ext, "url", None),
                command=catalog_ext.command,
                args=catalog_ext.args_json,
                env_vars=merged_env,
                auth_type=catalog_ext.auth_type,
                auth_config=merged_auth,
                user_auth_token=user_auth_token,
                user_auth_credentials=user_auth_credentials,
                user_id=user_id,
                tenant_id=user_auth_context.get("tenant_id", "") if user_auth_context else "",
            )
            ext_tools = build_mcp_tools(
                mcp_manager=mcp_client_manager,
                extension_id=catalog_ext.extension_id,
                extension_name=catalog_ext.name,
                tool_infos=tool_infos,
                user_id=user_id,
            )
            all_tools.extend(ext_tools)
        except Exception as e:
            logger.warning("mcp_extension_load_failed", extension=catalog_ext.name, error=str(e))

    return all_tools


# ---------------------------------------------------------------------------
# Agent service (public API)
# ---------------------------------------------------------------------------

class AgentService:
    """Orchestrates LangGraph CodeAct agent execution."""

    async def execute_task(
        self,
        product_id: str,
        task: str,
        session: AsyncSession,
        max_iterations: int = 10,
        thread_id: Optional[str] = None,
        user_platform: Optional[str] = None,
        use_reasoning: bool = False,
        mcp_builder_config: Optional[dict] = None,
        user_auth_context: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        """Execute an agent task and yield SSE events.

        Yields lines in SSE format:  ``event: <type>\\ndata: <json>\\n\\n``
        """

        def _sse(event: str, data: Any) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        session_id = str(uuid4())
        if not thread_id:
            thread_id = str(uuid4())
        timings: dict[str, int] = {}
        t0 = time.time()

        try:
            # ── 1. Load settings and product in parallel ─────────────────
            yield _sse("agent_status", {"step": "init", "message": "Loading settings and product..."})
            from app.api.settings import get_active_llm_settings
            from app.db.database import async_session_maker
            from sqlalchemy import select
            from app.models.product import Product as ProductModel

            async def _fetch_product():
                if not product_id:
                    return ("General Assistant", None)
                async with async_session_maker() as db:
                    result = await db.execute(select(ProductModel).where(ProductModel.product_id == product_id))
                    product = result.scalar_one_or_none()
                    if not product:
                        return ("Unknown Product", None)
                    return (product.product_name, product.description)

            async def _fetch_kb_summary():
                if not product_id:
                    return None
                try:
                    from app.rag.kb_search import get_product_summary
                    return await get_product_summary(product_id)
                except Exception:
                    return None

            llm_settings, (product_name, product_description), kb_summary = await asyncio.gather(
                get_active_llm_settings(session),
                _fetch_product(),
                _fetch_kb_summary(),
            )

            if not llm_settings.get("api_key"):
                yield _sse("agent_error", {"detail": "No API key configured. Go to Settings."})
                return

            # ── 2. Build LangChain model ──────────────────────────────
            yield _sse("agent_status", {"step": "init", "message": "Initializing model..."})
            chat_model = _build_langchain_model(llm_settings)

            # Also build an AsyncOpenAI client for web_research (needs async)
            client_kwargs: dict[str, Any] = {"api_key": llm_settings["api_key"]}
            if llm_settings.get("api_url"):
                client_kwargs["base_url"] = llm_settings["api_url"]
            async_llm_client = AsyncOpenAI(**client_kwargs)
            model_name = llm_settings.get("model_name", app_settings.REASONING_MODEL)

            # ── 3. Build tools (including knowledge base tool) ──────────
            yield _sse("agent_status", {"step": "init", "message": "Loading tools..."})
            from app.tools import get_all_tools, get_tool_descriptions

            # Configure web search (Serper API)
            from app.tools.web_search import configure_web_search
            configure_web_search(serper_api_key=llm_settings.get("serper_api_key", ""))

            tools = get_all_tools(
                llm_client=async_llm_client,
                model=model_name,
                chat_model=chat_model,
                screenops_api_key=llm_settings.get("screenops_api_key", ""),
                screenops_model=llm_settings.get("screenops_model"),
                screenops_api_url=llm_settings.get("screenops_api_url"),
                screenops_mouse_timeout=int(llm_settings.get("screenops_mouse_timeout", 30) or 30),
                screenops_image_scale=max(25, min(100, int(llm_settings.get("screenops_image_scale", 100) or 100))),
                product_id=product_id,
                product_description=product_description,
                api_key=llm_settings.get("api_key", ""),
                api_url=llm_settings.get("api_url"),
                conversation_id=thread_id,
            )
            # Filter by user-disabled tools (legacy KB names → disable knowledge_base)
            disabled = set((llm_settings.get("agent_tools_config") or {}).get("disabled") or [])
            if disabled & {"search_knowledge_base", "list_kb_entries", "read_kb_file", "browse_kb_structure"}:
                disabled = set(disabled) | {"knowledge_base"}
            tools = [t for t in tools if t.name not in disabled]

            # ── 3b. Load MCP extension tools (per-user, with auth pass-through) ──
            if not mcp_builder_config:
                try:
                    from app.db.database import async_session_maker
                    async with async_session_maker() as mcp_session:
                        mcp_ext_tools = await _load_mcp_extension_tools(mcp_session, user_auth_context)
                    if mcp_ext_tools:
                        mcp_ext_tools = [t for t in mcp_ext_tools if t.name not in disabled]
                        tools.extend(mcp_ext_tools)
                        logger.info("mcp_extension_tools_loaded", count=len(mcp_ext_tools))
                except Exception as e:
                    logger.warning("mcp_extension_tools_failed", error=str(e))

            tool_desc = get_tool_descriptions(tools)
            tool_names = [t.name for t in tools]

            yield _sse("agent_tools", {"tools": tool_names, "count": len(tools)})
            timings["init_ms"] = int((time.time() - t0) * 1000)

            # ── 5. Build system prompt (includes KB summary and user platform) ─
            system_prompt = _build_system_prompt(
                product_name=product_name,
                tool_descriptions=tool_desc,
                product_description=product_description,
                kb_summary=kb_summary,
                max_iterations=max_iterations,
                available_tool_names=tool_names,
                user_platform=user_platform,
            )

            # ── 5b. MCP Builder mode — inject prompt supplement ──────────
            if mcp_builder_config:
                from app.skills.mcp_builder_prompt import build_mcp_builder_supplement_slim
                from app.services.mcp_builder_service import format_endpoints_as_brief, MCPBuilderConfig
                mcp_cfg = MCPBuilderConfig(**mcp_builder_config)
                mcp_output_dir = mcp_cfg.output_dir or f"~/mcp-servers/{product_name}"
                has_kb = bool(product_id and product_id.startswith("__mcp__"))
                is_external = bool(mcp_cfg.api_docs_url or mcp_cfg.api_docs_text)
                service_name = mcp_output_dir.rstrip("/").split("/")[-1]
                supplement = build_mcp_builder_supplement_slim(
                    output_dir=mcp_output_dir,
                    service_name=service_name,
                    is_external=is_external,
                    has_kb=has_kb,
                    api_docs_text=mcp_cfg.api_docs_text,
                    api_base_url=mcp_cfg.api_base_url,
                    auth_type=mcp_cfg.auth_type,
                    auth_details=mcp_cfg.auth_details,
                    selected_endpoints=mcp_cfg.selected_endpoints,
                )
                system_prompt += "\n\n" + supplement
                yield _sse("agent_status", {"step": "init", "message": "MCP Builder mode activated"})

            # ── 6. Build LangGraph agent (shared checkpointer for conversation memory) ─
            yield _sse("agent_status", {"step": "agent", "message": "Building agent graph..."})
            use_gemini_compat = _is_gemini_model(llm_settings)
            if mcp_builder_config:
                # MCP Builder: use hybrid graph (native tool calls for file ops)
                graph = _build_mcp_builder_graph(
                    chat_model, tools, system_prompt, max_iterations,
                    use_gemini_compat=use_gemini_compat,
                )
                agent = graph.compile()
            else:
                # Agent chat: use CodeAct-only graph
                graph = _build_codeact_graph(
                    chat_model, tools, system_prompt, max_iterations,
                    use_gemini_compat=use_gemini_compat,
                )
                agent = graph.compile(checkpointer=_agent_checkpointer)

            # ── 7. Execute ────────────────────────────────────────────
            yield _sse("agent_status", {"step": "executing", "message": "Agent is working..."})
            t_exec = time.time()

            task_content = task
            if mcp_builder_config:
                from app.services.mcp_builder_service import format_endpoints_as_brief, MCPBuilderConfig
                mcp_cfg = MCPBuilderConfig(**mcp_builder_config)
                if mcp_cfg.selected_endpoints:
                    endpoints_brief = format_endpoints_as_brief(mcp_cfg, product_name)
                    task_content = endpoints_brief + "\n\n" + task_content
            if use_reasoning:
                task_content = "Think through this carefully step by step before giving your answer.\n\n" + task

            initial_state: AgentState = {
                "messages": [HumanMessage(content=task_content)],
                "script": None,
                "context": {},
                "current_iteration": 0,
                "max_iterations": max_iterations,
                "is_final_answer": False,
                "syntax_error_count": 0,
            }

            config = {"configurable": {"thread_id": thread_id}}
            iteration = 0
            final_answer = ""

            # Run blocking agent.stream() in thread; yield chunks via queue
            chunk_queue: queue.Queue = queue.Queue()

            def run_stream():
                try:
                    for chunk in agent.stream(initial_state, config=config, stream_mode="updates"):
                        chunk_queue.put(chunk)
                except Exception as e:
                    chunk_queue.put(("__error__", str(e)))
                chunk_queue.put(None)

            stream_thread = threading.Thread(target=run_stream, daemon=True)
            stream_thread.start()

            while True:
                chunk = await asyncio.get_event_loop().run_in_executor(None, chunk_queue.get)
                if chunk is None:
                    break
                if isinstance(chunk, tuple) and chunk[0] == "__error__":
                    raise RuntimeError(chunk[1])
                if not isinstance(chunk, dict):
                    continue

                for node_name, node_update in chunk.items():
                    if not isinstance(node_update, dict):
                        continue

                    # Track iteration count
                    ci = node_update.get("current_iteration")
                    if ci is not None:
                        iteration = ci

                    # Process complete messages emitted by this node
                    new_messages = node_update.get("messages", [])
                    for msg in new_messages:
                        msg_type = getattr(msg, "type", "unknown")
                        content = msg.content if hasattr(msg, "content") else str(msg)

                        if msg_type == "ai":
                            # Complete AI response from call_model
                            code = extract_python_code(content)
                            if not code:
                                code = extract_tool_call_as_python(content, known_tools=set(tool_names))
                            if code:
                                yield _sse("agent_code", {
                                    "iteration": iteration,
                                    "code": code,
                                })
                            elif content and content.strip():
                                # Final text answer — stream word by word
                                final_answer = content
                                words = content.split(' ')
                                total = len(words)
                                chunk_size = 4
                                for wi in range(0, total, chunk_size):
                                    partial = ' '.join(words[:wi + chunk_size])
                                    yield _sse("agent_answer_chunk", {
                                        "chunk": partial,
                                        "done": False,
                                    })
                                    await asyncio.sleep(0.02)
                                yield _sse("agent_answer_chunk", {
                                    "chunk": content,
                                    "done": True,
                                })

                        elif msg_type == "tool":
                            yield _sse("tool_output", {
                                "iteration": iteration,
                                "output": content[:5000],
                            })

            timings["execution_ms"] = int((time.time() - t_exec) * 1000)
            timings["total_ms"] = int((time.time() - t0) * 1000)

            # ── 8. Cleanup MCP connections ─────────────────────────────
            if 'mcp_manager' in locals() and mcp_manager:
                await mcp_manager.cleanup()

            # ── 9. Done ───────────────────────────────────────────────
            yield _sse("agent_done", {
                "session_id": session_id,
                "thread_id": thread_id,
                "iterations": iteration,
                "timings": timings,
                "tools_used": tool_names,
            })

            # Persist session using a NEW short-lived DB session
            # (the request session may already be closed or locked)
            try:
                from app.db.database import async_session_maker
                from app.models.agent_session import AgentSession
                async with async_session_maker() as save_session:
                    save_session.add(AgentSession(
                        session_id=session_id,
                        product_id=product_id,
                        task=task,
                        status="completed",
                        thread_id=thread_id,
                        iterations=iteration,
                        final_answer=final_answer[:10_000] if final_answer else None,
                        token_usage_json=json.dumps(timings),
                        created_at=datetime.utcnow(),
                        completed_at=datetime.utcnow(),
                    ))
                    await save_session.commit()
            except Exception:
                logger.warning("agent_session_save_failed")

        except Exception as exc:
            logger.error("agent_execution_failed", error=str(exc), exc_info=True)
            yield _sse("agent_error", {"detail": str(exc)})

            # Persist failed session using a NEW short-lived DB session
            try:
                from app.db.database import async_session_maker
                from app.models.agent_session import AgentSession
                async with async_session_maker() as save_session:
                    save_session.add(AgentSession(
                        session_id=session_id,
                        product_id=product_id,
                        task=task,
                        status="failed",
                        thread_id=thread_id,
                        iterations=0,
                        error=str(exc),
                        created_at=datetime.utcnow(),
                        completed_at=datetime.utcnow(),
                    ))
                    await save_session.commit()
            except Exception:
                pass


# Shared checkpointer for conversation memory across requests
_agent_checkpointer = MemorySaver()


# Global instance
agent_service = AgentService()
