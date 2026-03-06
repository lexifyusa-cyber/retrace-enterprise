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
from app.tools.utils import extract_python_code, strip_text_from_code_response

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


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_AGENT_SYSTEM_TEMPLATE = """\
You are the ReTrace AI agent by Lumena Technologies for the product **{product_name}**.

{product_description_section}

You can execute Python code to interact with the system using the tools listed below.

## HOW TO USE TOOLS

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
- You have 4 knowledge base tools. Use them strategically:
  * `list_kb_entries` — Use FIRST to discover what the product contains (skills, files, directories). Like checking a table of contents.
  * `search_knowledge_base` — Hybrid search for specific topics. Uses multi-query expansion, structural matching, vector similarity, and LLM re-ranking.
  * `read_kb_file` — Read the full content of a specific file by path. Use after search or list to get complete details.
  * `browse_kb_structure` — Browse the folder tree. Start from "/" and drill down. Like `ls` for the knowledge base.
- When answering questions about specific features, skills, or components: start with `list_kb_entries` to see what exists, then `search_knowledge_base` for details, then `read_kb_file` for full content.
- You can call tools multiple times with different queries to gather comprehensive information.
- If the evidence is insufficient, say so.
- Do NOT generate code after your final text answer.
- Maximum {max_iterations} tool-use iterations allowed.
- Do NOT use `import os`, `import subprocess`, `import shutil`, `import pathlib`, or other Python standard library modules for system operations. Always use the provided tools instead (e.g. `terminal("ls -la")`, `read_file("path")`, `write_file("path", "content")`).
- Do NOT use `open()`, `os.listdir()`, `Path()`, or any direct Python file I/O. Use the provided `read_file`, `write_file`, and `terminal` tools for all file and system operations.
- Only use `import` for data processing libraries (e.g. `json`, `re`, `math`) when needed to parse or format tool output.
- When using the `screenops` tool: it returns a JSON object with a "result" field (and optionally "iterations"). Before writing to a file or using the content, parse the return value: if it is a string, use `json.loads(screenops_result)` to get a dict, then use the "result" field only (e.g. `description_text = data["result"]` or `data.get("result", "")`). Pass that string to `write_file`, not the raw return value.

{tool_descriptions}
"""


def _build_system_prompt(
    product_name: str,
    tool_descriptions: str,
    product_description: Optional[str] = None,
    max_iterations: int = 10,
) -> str:
    if product_description:
        product_desc_section = f"""## PRODUCT DESCRIPTION

{product_description}

This product has a trained knowledge base. Use the `search_knowledge_base` tool to query it when you need specific information about the product."""
    elif product_name and product_name != "General Assistant":
        product_desc_section = """This product has a trained knowledge base. Use the `search_knowledge_base` tool to query it when you need specific information about the product."""
    else:
        product_desc_section = """No product is selected. You are operating as a general-purpose assistant. Use the available tools (terminal, file operations, web search, etc.) to help the user."""
    
    return _AGENT_SYSTEM_TEMPLATE.format(
        product_name=product_name,
        tool_descriptions=tool_descriptions,
        product_description_section=product_desc_section,
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
    for tool in tools:
        if isinstance(tool, StructuredTool):
            tools_context[tool.name] = tool.func
        else:
            tools_context[getattr(tool, "name", str(tool))] = getattr(tool, "func", tool)

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
        code = extract_python_code(response.content if hasattr(response, "content") else str(response))

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
            # ── 1. Load settings ──────────────────────────────────────
            yield _sse("agent_status", {"step": "init", "message": "Loading settings..."})
            from app.api.settings import get_active_llm_settings
            llm_settings = await get_active_llm_settings(session)

            if not llm_settings.get("api_key"):
                yield _sse("agent_error", {"detail": "No API key configured. Go to Settings."})
                return

            # ── 2. Build LangChain model ──────────────────────────────
            yield _sse("agent_status", {"step": "init", "message": "Initializing model..."})
            chat_model = _build_langchain_model(llm_settings)

            # Also build an AsyncOpenAI client for deep_websearch (needs async)
            client_kwargs: dict[str, Any] = {"api_key": llm_settings["api_key"]}
            if llm_settings.get("api_url"):
                client_kwargs["base_url"] = llm_settings["api_url"]
            async_llm_client = AsyncOpenAI(**client_kwargs)
            model_name = llm_settings.get("model_name", app_settings.REASONING_MODEL)

            # ── 3. Get product info ──────────────────────────────────
            from sqlalchemy import select
            from app.models.product import Product as ProductModel
            product = None
            product_name = "General Assistant"
            product_description = None
            if product_id:
                result = await session.execute(select(ProductModel).where(ProductModel.product_id == product_id))
                product = result.scalar_one_or_none()
                product_name = product.product_name if product else "Unknown Product"
                product_description = product.description if product else None

            # ── 4. Build tools (including knowledge base tool) ──────────
            yield _sse("agent_status", {"step": "init", "message": "Loading tools..."})
            from app.tools import get_all_tools, get_tool_descriptions

            tools = get_all_tools(
                llm_client=async_llm_client,
                model=model_name,
                chat_model=chat_model,
                screenops_api_key=llm_settings.get("screenops_api_key", ""),
                screenops_model=llm_settings.get("screenops_model"),
                product_id=product_id,
                product_description=product_description,
                api_key=llm_settings.get("api_key", ""),
                api_url=llm_settings.get("api_url"),
                conversation_id=thread_id,
            )
            # Filter by user-disabled tools
            disabled = set((llm_settings.get("agent_tools_config") or {}).get("disabled") or [])
            tools = [t for t in tools if t.name not in disabled]
            tool_desc = get_tool_descriptions(tools)
            tool_names = [t.name for t in tools]

            yield _sse("agent_tools", {"tools": tool_names, "count": len(tools)})
            timings["init_ms"] = int((time.time() - t0) * 1000)

            # ── 5. Build system prompt ────────────────────────────────
            system_prompt = _build_system_prompt(
                product_name=product_name,
                tool_descriptions=tool_desc,
                product_description=product_description,
                max_iterations=max_iterations,
            )

            # ── 6. Build LangGraph agent (shared checkpointer for conversation memory) ─
            yield _sse("agent_status", {"step": "agent", "message": "Building agent graph..."})
            use_gemini_compat = _is_gemini_model(llm_settings)
            graph = _build_codeact_graph(
                chat_model, tools, system_prompt, max_iterations,
                use_gemini_compat=use_gemini_compat,
            )
            agent = graph.compile(checkpointer=_agent_checkpointer)

            # ── 7. Execute ────────────────────────────────────────────
            yield _sse("agent_status", {"step": "executing", "message": "Agent is working..."})
            t_exec = time.time()

            initial_state: AgentState = {
                "messages": [HumanMessage(content=task)],
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

            # ── 8. Done ───────────────────────────────────────────────
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
