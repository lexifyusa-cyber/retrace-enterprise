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

import base64
import io
import inspect
import json
import os
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
You are the ReTrace AI agent by Lumena Technologies for the team **{team_name}**.

{user_platform_section}

{current_date_section}

{team_description_section}

You can execute Python code to interact with the system using the tools listed below.

## HOW TO USE TOOLS

**CRITICAL:** To call tools, output only a ```python code block with real Python that calls the tools (e.g. terminal(...), web_search(...)) and print() the result. Only ```python blocks are run; no other format is executed. When you have the answer for the user, respond with plain text only (no code block).

## ALWAYS CAPTURE AND PRINT TOOL OUTPUT (critical — do not skip)

Every single tool call MUST assign its result to a variable and print it.
Without `print()`, you will see `(no output)` and have NO way to know what
the tool returned or decide what to do next.

CORRECT (always do this):
```python
result = auto_browser(action="navigate", url="https://example.com")
print(result)
```

```python
result = web_search("query")
print(result)
```

```python
result = terminal("ls -la")
print(result)
```

WRONG (never do this — you will get `(no output)` and be stuck):
```python
auto_browser(action="navigate", url="https://example.com")  # No variable, no print — WRONG
```

```python
auto_browser(action="click_element", selector=".btn")  # Bare call — WRONG
```

```python
result = web_search("query")  # Captured but not printed — WRONG
```

This rule applies to EVERY tool call: `auto_browser`, `web_search`, `web_fetch`,
`terminal`, `read_file`, `glob_search`, and all others. No exceptions.

When you have a complete answer, respond with **plain text only** (no code block).
That text becomes the final answer shown to the user.

## CHECK TOOL RESULTS AND REPORT FAILURES HONESTLY (critical — do not skip)

Many tools — especially `auto_browser`, `terminal`, `web_fetch`, `web_search` — return
a dict/JSON with an `ok` field (`true` / `false`) and/or an `error` field describing
what went wrong. Before writing ANY sentence to the user that claims you did something,
you MUST check the result of every tool call you made.

**A tool call FAILED if any of these are true:**
- The result has `"ok": false`
- The result has an `"error"` field
- The result is empty / `(no output)` when you were expecting data
- A navigate/click/fetch returned a different URL or status than you intended

**NEVER tell the user you successfully did something when the tool returned an error.**
This is the single worst thing you can do — it lies to the user and hides a real problem.

If a tool failed, you have exactly two honest options:

1. **Retry with a different approach** in a new code block. Examples: use `click_element`
   if `vision_click` failed, use a different selector, navigate to a different URL,
   reset with `auto_browser(action="refresh")`, etc. NEVER retry the exact same call
   that just failed — it will fail again.

2. **Report the failure to the user and ask what they want to do.** Quote the actual
   error message. Be specific about what didn't work and why.

WRONG (tool failed, agent claims success — this is a LIE):
```python
result = auto_browser(action="navigate", url="https://example.com")
print(result)
# prints: {{"ok": false, "error": "Page.goto: Frame was detached"}}
```
Then responding: "I've navigated to example.com. You can see it in the Browser tab."
← This is a hallucination. The navigate failed. The user is being misled.

RIGHT (tool failed, agent reports honestly):
```python
result = auto_browser(action="navigate", url="https://example.com")
print(result)
# prints: {{"ok": false, "error": "Page.goto: Frame was detached"}}
```
Then responding: "I tried to navigate to example.com but the browser returned
'Frame was detached', which usually means the browser session is in a bad state.
Want me to refresh and try again, or take a different approach?"

This rule is as important as the `print()` rule above. If you finish a task, look back
at every tool call you made and confirm each one actually succeeded. If any failed,
your final answer must acknowledge that, not paper over it.

### Special case — `auto_browser` click results

Every `auto_browser` click action (`click`, `click_element`, `vision_click`) returns
a screenshot alongside the JSON result. **Use the screenshot to judge whether the
click did something** — you can see directly what changed on screen.

Two additional fields may appear when the click triggered something the screenshot
cannot show:
- `"downloaded": ["file.exe"]` — a file was saved to /workspace/Downloads. This is
  a reliable signal. Check the downloads folder if you need the file path.
- `"new_tab_opened": true` — a new tab was opened (the daemon still operates on the
  original tab, so navigate explicitly to interact with the new page).

**After a click, decide your next action:**
1. Read `"status_summary"` — it tells you URL before → after and whether a file downloaded.
2. Look at the attached screenshot — it shows the CURRENT browser state right now.
   - New page appeared? → find the next button in the screenshot and call vision_click on it.
   - Same page, element changed? → act on what you see.
   - Nothing changed? → the click missed, try a different approach.
3. **Do NOT call `screenshot()`, `get_url`, or `read_page` to verify** — the screenshot and status_summary already give you everything you need to act.

## ACTIVITY LABEL (required before every code block)
Before every ```python code block, output exactly one line in this format:

**Activity:** <5-10 word description of what you're about to do>

The description should be conversational and describe the INTENT, not the tool
names. It is shown to the user as a friendly label for the step you're running.

Examples:
**Activity:** Searching the web for MacBook pricing
**Activity:** Navigating to filehippo.com to find WinRAR
**Activity:** Clicking the download button on the release page
**Activity:** Checking the downloads folder for the installer
**Activity:** Running ls to see the current workspace files
**Activity:** Taking a screenshot to observe the result after clicking

Rules:
- ALWAYS include this line immediately before every code block
- Write it in present continuous tense ("Searching...", "Clicking...", "Reading...")
- Describe the intent, NEVER mention tool names (no "web_search", "auto_browser", etc.)
- Keep it short (5-10 words) and conversational
- Do NOT include this line when giving a final plain-text answer (no code)

IMPORTANT: The activity label is ONLY a display string — it does NOT replace
`print()`. You still MUST capture tool results with a variable and print them
inside the code block (see the "ALWAYS CAPTURE AND PRINT TOOL OUTPUT" section
above). The activity label is for the user; the `print()` output is for you
to observe what the tool returned.

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

## TOOL SELECTION GUIDE (in priority order)

### 1. Team Knowledge (when knowledge_base is available)
- **ALWAYS try `knowledge_base(action="search", ...)` FIRST** for any team question.
- Only fall back to file tools or web search if the KB has no answer.

### 2. File Operations
- **Find files by name/pattern**: `glob_search("*.py")` — NOT `terminal("find ...")`
- **Search file contents**: `grep("pattern", "path")` — NOT `terminal("grep ...")`
- **Read a file**: `read_file("path")` — NOT `terminal("cat ...")`
- **Edit files (targeted)**: `str_replace(file, old, new)` — NOT rewriting the whole file with `write_file`
- **Create/overwrite files**: `write_file("path", "content")`
- **Delete files**: `delete_file("path")` — NOT `terminal("rm ...")`

### 3. Browser Automation (`auto_browser`)
**The browser is visible to the user in a Browser tab in the UI. When you navigate to a page using autobrowser, the user sees it live.**
Always navigate to a relevant page in the browser as part of your answer. The user expects to see visual content in the Browser tab. You can use web_search to gather information, but always include navigating to a relevant page in the browser whenever the topic involves websites, news, products, or any web content. Only skip the browser for pure calculations, code generation, or file-only tasks.
Use `auto_browser` for all web page interaction. Sub-actions:
- **Navigate**: `auto_browser(action="navigate", url="...")`
- **Read content** (smart workflow):
  1. `auto_browser(action="analyze_page")` — check token costs for read_page vs screenshot
  2. Choose: `auto_browser(action="read_page")` for text, or `auto_browser(action="screenshot_full_page")` for visual overview
  3. After screenshot, use `auto_browser(action="read_page", selector="...")` to extract just the section you need
- **Click** (in order of preference):
  1. `auto_browser(action="vision_click", text="blue submit button")` — BEST: describe what you see on screen; uses vision model to find and click it. Use this FIRST whenever you can visually describe the element.
  2. `auto_browser(action="click_element", selector="a.js-download-btn")` — use ONLY when you have confirmed the exact CSS selector via `evaluate_js` or `read_page`. NEVER guess a selector — a wrong guess wastes 10 seconds timing out.
  3. `auto_browser(action="click", x=500, y=300)` — LOW-LEVEL fallback: only when you have exact pixel coordinates
- **CRITICAL — Use the screenshot to decide your next action:**
  A screenshot is automatically included with every click/navigate result. It shows the CURRENT browser state right now — use it to decide what to do next. If it shows a new page with a button or link, describe it and call vision_click directly. Do NOT call read_page or get_url first to "verify" — the screenshot already tells you the current state.
- **click_element — confirmed selectors only:**
  Only use `click_element` after you have inspected the DOM with `evaluate_js` or `read_page` and confirmed the selector exists. Do NOT use `click_element` as a first attempt with a guessed selector.
- **Type/fill**: `auto_browser(action="type", text="...")` for focused element, `auto_browser(action="fill", selector="input#q", text="...")` for specific input
- **Other**: `scroll`, `press_key`, `hover`, `select_option`, `wait`, `evaluate_js`, `back`, `forward`, `refresh`, `get_url`

### 4. Web Tools (pick the right one)
- **Quick factual lookup** (error messages, docs, simple questions): `web_search("query")` — fast, cheap
- **Deep research** (compare info from multiple pages): `web_research("query")` — slower, fetches & analyzes 3 pages
- **Filtered/advanced search** (site-specific, date range, filetype): `web_advanced(query="...", site="...", ...)` — needs Serper API
- **Read a specific URL**: `web_fetch("url")` — returns clean text

### 5. Terminal
Use `terminal(...)` for operations that **don't have a dedicated tool**:
- Running scripts: `terminal("python3 script.py")`
- Installing packages: `terminal("pip install ...")`
- Git operations: `terminal("git status")`
- System commands: `terminal("whoami")`, `terminal("df -h")`
- SSH (inline only): `terminal('ssh user@host "cmd"')` — NEVER open an interactive session

### 6. Task Tracking
- **Multi-step tasks**: `todo_write(...)` to plan and track progress

## EFFICIENCY RULES
- **One observation per page state**: After a click, call `screenshot()` ONCE. Do NOT also call `read_page()`, `evaluate_js()`, and `get_url()` on the same unchanged page. The screenshot shows you everything you need.
- **Never repeat a failed action**: If the screenshot after a click looks identical to before and no download was triggered, the click missed — do NOT click the same way again. Try a different approach — a different description for `vision_click`, inspect the DOM for a confirmed selector, or scroll the element into view first.
- **One auto_browser call per code block**: When using `auto_browser`, make only ONE call per code block. This way you can see the screenshot result before deciding your next action. Do NOT chain multiple `auto_browser` calls in the same code block.

## COMMON MISTAKES (avoid these)
- Using `terminal("cat file.txt")` instead of `read_file("file.txt")`
- Rewriting an entire file with `write_file` when only changing one line — use `str_replace` instead
- Calling `web_search` 5 times when `web_research` would answer in one call
- Using `knowledge_base(action="list")` as a first step — use `search` first, it's almost always better
- Using `auto_browser(action="click_element", selector="button.green")` with a guessed selector — use `vision_click` first, then only use `click_element` with a confirmed selector from DOM inspection

{tool_descriptions}
"""

_KB_RULES = """\
- You have one **knowledge_base** tool with four actions (pass the `action` parameter):
  * `knowledge_base(action="search", query="...", keywords=[...], top_k=10)` — **Use this FIRST for any team question.** Fast hybrid search. Always provide a `keywords` list of 5-15 synonyms, related terms, and alternative phrasings alongside your query to maximize recall.
  * `knowledge_base(action="read_file", file_path="...")` — Read the full content of a specific file. Use after search to get complete details. Supports fuzzy path matching.
  * `knowledge_base(action="list", category="all")` — List what's in the KB. Categories: 'all', 'key_files', 'directories', 'files'. Only use when the user asks "what's in this team" or you need a broad overview.
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
    team_name: str,
    tool_descriptions: str,
    team_description: Optional[str] = None,
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

    if team_description:
        sections.append(f"## TEAM DESCRIPTION\n\n{team_description}")

    if kb_summary:
        sections.append(f"## TEAM KNOWLEDGE SUMMARY (from trained KB)\n\n{kb_summary}")

    if has_kb:
        if sections:
            sections.append(
                "This team has a trained knowledge base. Use `knowledge_base(action=\"search\", query=\"...\", keywords=[...])` "
                "to query it. For simple overview questions, the summary above may already contain the answer — respond directly without a tool call if so."
            )
        elif team_name and team_name != "General Assistant":
            sections.append(
                "This team has a trained knowledge base. Use `knowledge_base(action=\"search\", query=\"...\", keywords=[...])` "
                "to query it when you need specific information about the team."
            )
        else:
            sections.append(
                "No team is selected. You are operating as a general-purpose assistant. "
                "Use the available tools (terminal, file operations, web search, etc.) to help the user."
            )
    else:
        sections.append(
            "No knowledge base tool is available. "
            "Use the available tools (terminal, file operations, web search, etc.) to help the user."
        )

    team_desc_section = "\n\n".join(sections)

    now = datetime.now()
    current_date_section = (
        f"Current date and time: **{now.strftime('%B %d, %Y')}** | Current year: **{now.year}**\n\n"
        "When searching the internet always prefer the most recent results:\n"
        f"- Always include the current year ({now.year}) in queries — e.g. \"Telugu movies {now.year}\" not \"Telugu movies 2024\"\n"
        f"- For today's news/updates: `web_advanced(query=\"...\", tbs=\"qdr:d\")` — last 24 hours\n"
        f"- For this week: `web_advanced(query=\"...\", tbs=\"qdr:w\")`\n"
        f"- For results since start of month: `web_advanced(query=\"...\", after=\"{now.strftime('%Y-%m-01')}\")`\n"
        "- If search results are from 2024 or earlier on a fast-moving topic, refine the query with a date filter"
    )

    return _AGENT_SYSTEM_TEMPLATE.format(
        team_name=team_name,
        tool_descriptions=tool_descriptions,
        team_description_section=team_desc_section,
        user_platform_section=user_platform_section,
        kb_rules_section=_KB_RULES if has_kb else "",
        max_iterations=max_iterations,
        current_date_section=current_date_section,
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
# LLM log persistence (for developer-mode viewer)
# ---------------------------------------------------------------------------

def _serialize_messages_for_log(messages: list) -> str:
    """Convert the messages list (mix of dicts and LangChain objects) to JSON string."""
    out = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "unknown")
            content = m.get("content", "")
        else:
            role = getattr(m, "type", "unknown")
            content = getattr(m, "content", "")
        # Multimodal content can be a list of dicts — keep as-is but truncate images
        if isinstance(content, list):
            cleaned = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "image_url":
                        cleaned.append(part)
                    else:
                        cleaned.append(part)
                else:
                    cleaned.append(str(part))
            content = cleaned
        elif isinstance(content, str) and len(content) > 20000:
            content = content[:20000] + f"\n...[truncated, total {len(content)} chars]"
        out.append({"role": role, "content": content})
    return json.dumps(out, default=str)


def _extract_usage(response: Any) -> dict:
    """Extract token usage metadata from a LangChain response object."""
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
    }
    if response is None:
        return usage

    # LangChain standard usage_metadata
    um = getattr(response, "usage_metadata", None)
    if isinstance(um, dict):
        usage["input_tokens"] = int(um.get("input_tokens", 0) or 0)
        usage["output_tokens"] = int(um.get("output_tokens", 0) or 0)
        usage["total_tokens"] = int(um.get("total_tokens", 0) or 0)
        # Provider-specific details
        itd = um.get("input_token_details") or {}
        if isinstance(itd, dict):
            usage["cache_read_tokens"] = int(itd.get("cache_read", 0) or 0)
            usage["cache_creation_tokens"] = int(itd.get("cache_creation", 0) or 0)
        otd = um.get("output_token_details") or {}
        if isinstance(otd, dict):
            usage["reasoning_tokens"] = int(otd.get("reasoning", 0) or 0)

    # Fallback: response_metadata.token_usage (OpenAI-style)
    if usage["total_tokens"] == 0:
        rm = getattr(response, "response_metadata", None) or {}
        tu = rm.get("token_usage") if isinstance(rm, dict) else None
        if isinstance(tu, dict):
            usage["input_tokens"] = int(tu.get("prompt_tokens", 0) or 0)
            usage["output_tokens"] = int(tu.get("completion_tokens", 0) or 0)
            usage["total_tokens"] = int(tu.get("total_tokens", 0) or 0)

    if usage["total_tokens"] == 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _persist_llm_log(
    user_id: Optional[str],
    conversation_id: Optional[str],
    iteration: int,
    messages: list,
    response: Any,
    latency_ms: int,
    model_name: Optional[str],
    model_provider: Optional[str],
    error: Optional[str],
    main_event_loop: Optional[asyncio.AbstractEventLoop],
) -> None:
    """Persist an LLM log row in a background thread so we don't block the agent."""
    try:
        req_json = _serialize_messages_for_log(messages)
    except Exception:
        req_json = None
    try:
        resp_content = (
            response.content if hasattr(response, "content") else (str(response) if response else "")
        )
        if isinstance(resp_content, list):
            resp_content = json.dumps(resp_content, default=str)
    except Exception:
        resp_content = ""
    usage = _extract_usage(response)
    log_row = {
        "log_id": str(uuid4()),
        "user_id": user_id,
        "conversation_id": conversation_id,
        "iteration": iteration,
        "request_messages_json": req_json,
        "response_content": resp_content[:50000] if isinstance(resp_content, str) else None,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_creation_tokens": usage["cache_creation_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "model_provider": model_provider,
        "model_name": model_name,
        "latency_ms": latency_ms,
        "error": error,
    }

    async def _write():
        from app.db.database import async_session_maker
        from app.models.llm_log import LLMLog
        async with async_session_maker() as db:
            db.add(LLMLog(**log_row))
            await db.commit()

    # Schedule write on the main event loop (agent runs in worker thread, cannot use its own loop for DB)
    if main_event_loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_write(), main_event_loop)
            return
        except Exception:
            pass
    # Fallback: run in a fresh loop synchronously
    try:
        asyncio.run(_write())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Activity label extraction
# ---------------------------------------------------------------------------

def _extract_activity_label(raw_content: str) -> Optional[str]:
    """Extract the **Activity:** line that precedes a code block.

    Only searches text BEFORE the first code block to avoid false positives
    from Python comments inside the code. Returns None if not found.
    """
    if not raw_content:
        return None
    code_start = raw_content.find("```")
    search_region = raw_content[:code_start] if code_start != -1 else raw_content
    match = re.search(
        r"\*\*Activity:\*\*\s*(.+?)(?:\n|$)",
        search_region,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------
# CodeAct graph builder (ported from langgraph_codeact/__init__.py)
# ---------------------------------------------------------------------------

def _build_codeact_graph(
    model: Any,
    tools: list[StructuredTool],
    system_prompt: str,
    max_iterations: int = 10,
    use_gemini_compat: bool = False,
    main_event_loop: Optional[asyncio.AbstractEventLoop] = None,
    sandbox_volume_path: Optional[str] = None,
    llm_log_user_id: Optional[str] = None,
    llm_log_conversation_id: Optional[str] = None,
    llm_log_model_name: Optional[str] = None,
    llm_log_model_provider: Optional[str] = None,
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

    # Map tool names → callable functions for the sandbox.
    # Some tools are async-only (coroutine but no func).  The sandbox runs
    # inside exec() which is synchronous (in a worker thread), so we wrap
    # async coroutines to submit them to the MAIN FastAPI event loop via
    # run_coroutine_threadsafe.  This is critical because Playwright browser
    # sessions and WebSocket objects are bound to that main loop — creating
    # a new loop with asyncio.run() would cause cross-loop errors.
    def _wrap_async(coro_fn):
        """Return a sync wrapper that dispatches to the main event loop."""
        import functools

        @functools.wraps(coro_fn)
        def wrapper(*args, **kwargs):
            if main_event_loop and main_event_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    coro_fn(*args, **kwargs), main_event_loop
                )
                return future.result(timeout=120)
            else:
                return asyncio.run(coro_fn(*args, **kwargs))
        return wrapper

    tools_context: dict[str, Any] = {}
    for tool in tools:
        if isinstance(tool, StructuredTool):
            if tool.func is not None:
                tools_context[tool.name] = tool.func
            elif tool.coroutine is not None:
                tools_context[tool.name] = _wrap_async(tool.coroutine)
            else:
                tools_context[tool.name] = tool
        else:
            # MCP adapter tools are async-only — wrap for sync sandbox
            if hasattr(tool, 'ainvoke'):
                def _make_sync_mcp_wrapper(t):
                    def sync_wrapper(**kwargs):
                        import concurrent.futures
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = None
                        if loop and loop.is_running():
                            with concurrent.futures.ThreadPoolExecutor() as pool:
                                return pool.submit(asyncio.run, t.ainvoke(kwargs)).result(timeout=30)
                        else:
                            return asyncio.run(t.ainvoke(kwargs))
                    return sync_wrapper
                tools_context[getattr(tool, "name", str(tool))] = _make_sync_mcp_wrapper(tool)
            else:
                fn = getattr(tool, "func", None) or getattr(tool, "coroutine", None) or tool
                if asyncio.iscoroutinefunction(fn):
                    tools_context[getattr(tool, "name", str(tool))] = _wrap_async(fn)

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

        # Log messages being sent to LLM
        logger.debug(
            "llm_request",
            iteration=current_iteration,
            message_count=len(messages),
            messages=[
                {
                    "role": m["role"] if isinstance(m, dict) else getattr(m, "type", "unknown"),
                    "content_preview": (m["content"] if isinstance(m, dict) else getattr(m, "content", ""))[:500],
                }
                for m in messages
            ],
        )

        _llm_t0 = time.time()
        _llm_error = None
        try:
            response = model.invoke(messages)
        except Exception as _e:
            _llm_error = str(_e)
            raise
        finally:
            _llm_elapsed_ms = int((time.time() - _llm_t0) * 1000)
            # Persist LLM log (developer mode viewer)
            try:
                _persist_llm_log(
                    user_id=llm_log_user_id,
                    conversation_id=llm_log_conversation_id,
                    iteration=current_iteration,
                    messages=messages,
                    response=locals().get("response"),
                    latency_ms=_llm_elapsed_ms,
                    model_name=llm_log_model_name,
                    model_provider=llm_log_model_provider,
                    error=_llm_error,
                    main_event_loop=main_event_loop,
                )
            except Exception as _log_err:
                logger.warning("llm_log_persist_failed", error=str(_log_err))

        # Extract activity label from raw content BEFORE any stripping
        # (strip_text_from_code_response removes text outside code blocks,
        # which would eat the **Activity:** line).
        raw_content_original = response.content if hasattr(response, "content") else str(response)
        activity = _extract_activity_label(raw_content_original) if isinstance(raw_content_original, str) else None
        # Attach to the response object so the caller (sandbox node) can include
        # it in the agent_code SSE event.
        if activity is not None:
            try:
                if not hasattr(response, "additional_kwargs") or response.additional_kwargs is None:
                    response.additional_kwargs = {}
                response.additional_kwargs["activity"] = activity
            except Exception:
                pass

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

        # Log LLM response
        logger.debug(
            "llm_response",
            iteration=current_iteration,
            response_preview=raw_content[:1000],
            has_code=bool(code),
            code_preview=code[:500] if code else None,
        )

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

        iteration = state.get("current_iteration", 0)
        logger.debug("sandbox_execute", iteration=iteration, code=code)

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

        logger.debug("sandbox_output", iteration=iteration, output=output[:2000])

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

        # ── Attach browser screenshot to message if available ────────
        # When the output contains a screenshot reference, read the JPEG
        # from the shared volume and include it as a multimodal image so
        # the LLM can actually see what's on screen.
        screenshot_b64 = None
        has_screenshot_ref = ".browser-screenshot.jpg" in output
        logger.info("sandbox_screenshot_check",
                     has_ref=has_screenshot_ref,
                     has_volume_path=sandbox_volume_path is not None,
                     volume_path=sandbox_volume_path)
        if sandbox_volume_path and has_screenshot_ref:
            # Extract the relative screenshot_file path from the JSON output
            # (e.g. "conv_id/.browser-screenshot.jpg") and resolve against volume root.
            # Fall back to volume root for Fly.io where daemon writes to /workspace/.
            import re as _re
            _ss_match = _re.search(r'"screenshot_file"\s*:\s*"([^"]+)"', output)
            _ss_rel = _ss_match.group(1) if _ss_match else ".browser-screenshot.jpg"
            ss_path = None
            for _candidate in [
                os.path.join(sandbox_volume_path, _ss_rel),
                os.path.join(sandbox_volume_path, ".browser-screenshot.jpg"),
            ]:
                if os.path.exists(_candidate) and os.path.getsize(_candidate) > 0:
                    ss_path = _candidate
                    break
            file_exists = ss_path is not None
            file_size = os.path.getsize(ss_path) if file_exists else 0
            logger.info("sandbox_screenshot_file_check", path=ss_path, exists=file_exists, file_size=file_size)
            if file_exists and file_size > 0:
                try:
                    with open(ss_path, "rb") as f:
                        screenshot_b64 = base64.b64encode(f.read()).decode("ascii")
                    logger.info("sandbox_screenshot_attached", size=len(screenshot_b64))
                except Exception as e:
                    logger.warning("sandbox_screenshot_read_failed", path=ss_path, error=str(e))

        # For Gemini we use a user message instead of ToolMessage so we never send
        # tool_call parts that lack thought_signature.
        if use_gemini_compat:
            if screenshot_b64:
                content = [
                    {"type": "text", "text": f"Code execution result:\n\n{output.rstrip()}"},
                    {"type": "text", "text": "[Current browser state — this screenshot shows what the page looks like RIGHT NOW after the action. Use it to identify what to click or do next. If it shows a new page with a button or link, describe it and call vision_click directly.]"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
                ]
            else:
                content = f"Code execution result:\n\n{output.rstrip()}"
            return {
                "messages": [HumanMessage(content=content)],
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

        if screenshot_b64:
            content = [
                {"type": "text", "text": output.rstrip()},
                {"type": "text", "text": "[Current browser state — this screenshot shows what the page looks like RIGHT NOW after the action. Use it to identify what to click or do next. If it shows a new page with a button or link, describe it and call vision_click directly.]"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
            ]
        else:
            content = output.rstrip()

        return {
            "messages": [ToolMessage(content=content, tool_call_id=tool_call_id, name="code_execution")],
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
        team_id: str,
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
            # ── 1. Load settings and team in parallel ─────────────────
            yield _sse("agent_status", {"step": "init", "message": "Loading settings and team..."})
            from app.api.settings import get_active_llm_settings
            from app.db.database import async_session_maker
            from sqlalchemy import select
            from app.models.team import Team as TeamModel

            async def _fetch_team():
                if not team_id:
                    return ("General Assistant", None)
                async with async_session_maker() as db:
                    result = await db.execute(select(TeamModel).where(TeamModel.team_id == team_id))
                    team = result.scalar_one_or_none()
                    if not team:
                        return ("Unknown Team", None)
                    return (team.team_name, team.description)

            async def _fetch_kb_summary():
                if not team_id:
                    return None
                try:
                    from app.rag.kb_search import get_team_summary
                    return await get_team_summary(team_id)
                except Exception:
                    return None

            llm_settings, (team_name, team_description), kb_summary = await asyncio.gather(
                get_active_llm_settings(session),
                _fetch_team(),
                _fetch_kb_summary(),
            )

            if not llm_settings.get("api_key"):
                yield _sse("agent_error", {"detail": "No API key configured. Go to Settings."})
                return

            # Use saved max_iterations from settings if caller used the default
            saved_max = llm_settings.get("agent_max_iterations", 10)
            if max_iterations == 10 and saved_max != 10:
                max_iterations = saved_max

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

            # ── 3a. Create sandbox session (if Podman is available) ────
            sandbox_session = None
            try:
                from app.services.sandbox_manager import ensure_sandbox_ready, get_or_create_session, register_conversation
                if ensure_sandbox_ready():
                    yield _sse("agent_status", {"step": "init", "message": "Starting sandbox..."})
                    # Use user_id for sandbox (one container per user, shared across conversations)
                    sandbox_user_id = user_auth_context.get("user_id", thread_id)
                    register_conversation(thread_id, sandbox_user_id)
                    # Run in thread with timeout to avoid blocking the SSE stream
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(get_or_create_session, sandbox_user_id)
                        try:
                            sandbox_session = future.result(timeout=30)
                            logger.info("sandbox_session_active", user_id=sandbox_user_id, conversation_id=thread_id)
                            yield _sse("agent_status", {"step": "init", "message": "Sandbox ready"})
                            # Attach sandbox to browser manager so Browser tab uses it
                            try:
                                from app.services.browser_manager import browser_manager as _bm
                                asyncio.get_event_loop().create_task(
                                    _bm.attach_sandbox(thread_id, sandbox_session)
                                )
                            except Exception:
                                pass
                            # Respawn Terminal tab PTY inside the container
                            # (only if it's pending or doesn't exist yet)
                            try:
                                from app.services.pty_manager import pty_manager as _pty
                                existing = _pty.get(thread_id)
                                if existing is None or existing.shell == "pending":
                                    _pty.respawn_in_sandbox(thread_id)
                            except Exception:
                                pass
                        except concurrent.futures.TimeoutError:
                            logger.warning("sandbox_creation_timeout", conversation_id=thread_id)
                            sandbox_session = None
                else:
                    logger.info("sandbox_not_available_running_unsandboxed", conversation_id=thread_id)
            except Exception as sandbox_err:
                logger.warning("sandbox_init_failed", error=str(sandbox_err))
                sandbox_session = None

            tools = get_all_tools(
                llm_client=async_llm_client,
                model=model_name,
                chat_model=chat_model,
                screenops_api_key=llm_settings.get("screenops_api_key", ""),
                screenops_model=llm_settings.get("screenops_model"),
                screenops_api_url=llm_settings.get("screenops_api_url"),
                screenops_mouse_timeout=int(llm_settings.get("screenops_mouse_timeout", 30) or 30),
                screenops_image_scale=max(25, min(100, int(llm_settings.get("screenops_image_scale", 100) or 100))),
                screenshot_delay_ms=max(0, min(30000, int(llm_settings.get("screenshot_delay_ms", 2000) or 2000))),
                team_id=team_id,
                team_description=team_description,
                api_key=llm_settings.get("api_key", ""),
                api_url=llm_settings.get("api_url"),
                conversation_id=thread_id,
                sandbox_session=sandbox_session,
                serper_api_key=llm_settings.get("serper_api_key", ""),
                main_event_loop=asyncio.get_running_loop(),
                user_id=(user_auth_context or {}).get("user_id"),
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
                team_name=team_name,
                tool_descriptions=tool_desc,
                team_description=team_description,
                kb_summary=kb_summary,
                max_iterations=max_iterations,
                available_tool_names=tool_names,
                user_platform=user_platform,
            )

            # ── 5a. Sandbox mode — add instructions for file serving ──────
            if sandbox_session:
                system_prompt += f"""

## SANDBOX ENVIRONMENT
You are running inside a sandboxed container. All files are in /workspace.
- Files you create are in /workspace (persistent across sessions).
- The ~ (home directory) maps to /workspace.
- You CANNOT access the host machine's filesystem.
- Downloaded files from the browser are saved to /workspace/.

## AUTO_BROWSER ACTIONS REFERENCE
Supported actions (use ONLY these exact action names):
- `auto_browser(action="navigate", url="https://example.com")` — go to a URL
- `auto_browser(action="navigate", url="file:///workspace/file.html")` — open local file
- `auto_browser(action="screenshot")` — take a screenshot
- `auto_browser(action="vision_click", text="blue submit button")` — PREFERRED: describe element to click; uses vision model to find and click it
- `auto_browser(action="click_element", selector="button.download")` — click by CSS selector; ONLY use when selector is confirmed via evaluate_js or read_page, never guess
- `auto_browser(action="click", x=500, y=300)` — click at coordinates (low-level fallback)
- `auto_browser(action="type", text="hello")` — type text
- `auto_browser(action="fill", selector="input#search", text="query")` — fill form field
- `auto_browser(action="read_page")` — extract page text
- `auto_browser(action="read_page", selector=".content")` — extract specific element text
- `auto_browser(action="scroll", direction="down", amount=300)` — scroll
- `auto_browser(action="press_key", key="Enter")` — press keyboard key
- `auto_browser(action="evaluate_js", js_expression="document.title")` — run JavaScript
- `auto_browser(action="back")` / `auto_browser(action="forward")` / `auto_browser(action="refresh")`
- `auto_browser(action="get_url")` — get current URL and title
- `auto_browser(action="wait", seconds=3)` — wait

To download a file via browser:
1. Navigate to the download page
2. Click the download button using `vision_click` (preferred) or `click_element` (confirmed selector only)
3. OBSERVE: read `"status_summary"` in the click result — it shows URL before→after and whether a file downloaded. A screenshot is automatically attached; do NOT call `screenshot()` again.
4. If the page navigated to an intermediate/confirmation page, look for the next download button and click it
5. Repeat steps 3-4 until `"downloaded"` appears in the result or Downloads folder has the file (some sites have 2-3 steps)
6. Check /workspace/Downloads/ for the downloaded file: `terminal("ls -la /workspace/Downloads/")`
"""

            # ── 5b. MCP Builder mode — inject prompt supplement ──────────
            if mcp_builder_config:
                from app.skills.mcp_builder_prompt import build_mcp_builder_supplement_slim
                from app.services.mcp_builder_service import format_endpoints_as_brief, MCPBuilderConfig
                mcp_cfg = MCPBuilderConfig(**mcp_builder_config)
                mcp_output_dir = mcp_cfg.output_dir or f"~/mcp-servers/{team_name}"
                has_kb = bool(team_id and team_id.startswith("__mcp__"))
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
                    main_event_loop=asyncio.get_running_loop(),
                    sandbox_volume_path=getattr(sandbox_session, "volume_path", None) if sandbox_session else None,
                    llm_log_user_id=(user_auth_context or {}).get("user_id") if user_auth_context else None,
                    llm_log_conversation_id=thread_id,
                    llm_log_model_name=model_name,
                    llm_log_model_provider=llm_settings.get("provider") if isinstance(llm_settings, dict) else None,
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
                    endpoints_brief = format_endpoints_as_brief(mcp_cfg, team_name)
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
                            # Detect browser tool calls early to open workspace
                            ai_tool_calls = getattr(msg, "tool_calls", [])
                            for tc in ai_tool_calls:
                                tc_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                                if tc_name == "auto_browser":
                                    yield _sse("browser_navigate", {"url": "", "title": ""})
                                    break

                            # Complete AI response from call_model
                            code = extract_python_code(content)
                            if not code:
                                code = extract_tool_call_as_python(content, known_tools=set(tool_names))
                            # Pull activity label off the AI message (set in call_model)
                            msg_activity = None
                            try:
                                _ak = getattr(msg, "additional_kwargs", None) or {}
                                if isinstance(_ak, dict):
                                    msg_activity = _ak.get("activity")
                            except Exception:
                                msg_activity = None
                            if code:
                                yield _sse("agent_code", {
                                    "iteration": iteration,
                                    "code": code,
                                    "activity": msg_activity,
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
                            tool_name = getattr(msg, "name", "") or ""
                            # Extract text from multimodal content for SSE output
                            display_content = content
                            if isinstance(content, list):
                                display_content = next(
                                    (c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text"),
                                    str(content),
                                )
                            yield _sse("tool_output", {
                                "iteration": iteration,
                                "output": display_content[:5000] if isinstance(display_content, str) else str(display_content)[:5000],
                                "tool_name": tool_name,
                            })
                            # Auto-open browser workspace when browser tools run
                            if tool_name == "auto_browser":
                                try:
                                    import json as _json
                                    tool_data = _json.loads(display_content) if display_content else {}
                                    yield _sse("browser_navigate", {
                                        "url": tool_data.get("url", ""),
                                        "title": tool_data.get("title", ""),
                                    })
                                    tool_action = tool_data.get("action", "")
                                    if tool_action == "page_analysis":
                                        yield _sse("browser_analysis", {
                                            "iteration": iteration,
                                            "url": tool_data.get("url", ""),
                                            "page_height_px": tool_data.get("page_height_px", 0),
                                            "text_chars": tool_data.get("text_chars", 0),
                                            "extraction_options": tool_data.get("extraction_options", []),
                                            "next_steps": tool_data.get("next_steps", ""),
                                        })
                                    elif tool_action == "screenshot_full_page":
                                        yield _sse("browser_full_page_scan", {
                                            "iteration": iteration,
                                            "url": tool_data.get("url", ""),
                                            "num_tiles": tool_data.get("num_tiles", 0),
                                            "page_height_px": tool_data.get("page_height_px", 0),
                                            "hint": tool_data.get("hint", ""),
                                        })
                                except Exception:
                                    yield _sse("browser_navigate", {"url": "", "title": ""})

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
                        team_id=team_id,
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
                        team_id=team_id,
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
