"""
Agent API endpoints — OpenCode sidecar integration,
conversation persistence, and Learn This (Q&A training).
"""

from datetime import datetime
from typing import Optional, List
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import asyncio
import json
import os
import structlog

from app.db.database import get_session, async_session_maker
from app.models.agent_session import AgentSession
from app.models.conversation import Conversation, ConversationMessage
from app.models.llm_log import LLMLog
from app.models.sop import SOP
from app.services.opencode_manager import opencode_manager, _opencode_projects_dir
from app.services.session_bridge import get_or_create_conversation, find_opencode_session_id, link_session, sync_messages_from_opencode
from app.core.security import CurrentUser, get_current_user
from app.core.security import get_tenant_team_ids
# AgentToolsUpdate is needed at module-load time for the PATCH /opencode/tools
# route's body annotation. settings.py has no agent-side imports, so the
# module-level import is safe.
from app.api.settings import AgentToolsUpdate

logger = structlog.get_logger()
router = APIRouter()

# Key by (user_id, session_id) so two users can never cross-cancel each other's
# streams even if OpenCode session ids happen to collide (recycled ids after
# sidecar restart, etc.).
_active_streams: dict[tuple[str, str], asyncio.Event] = {}

# Maps `user_id` → ReTrace `conversation_id` currently being processed by the
# agent (set when `opencode_send_message` starts, cleared on done). The
# internal browser bridge reads this so it can ignore the LLM-invented
# `conversation_id` arg and drive the SAME BrowserSession the user's
# BrowserWorkspace WebSocket is attached to. Without this override the agent
# would create its own Chromium (whatever id the LLM made up) and the user
# would never see the navigation in the right panel.
_active_browser_conv: dict[str, str] = {}


def get_active_browser_conv(user_id: str) -> Optional[str]:
    """Public accessor for the internal bridge — returns the ReTrace
    conversation_id currently being processed for `user_id`, or None if no
    agent run is in flight."""
    return _active_browser_conv.get(user_id)


# Maps `user_id` → the team_id the frontend most recently selected. Set in
# `opencode_regenerate_config` (and rehydrated from `user.preferences` at
# backend startup). MCP subprocesses hit /api/v1/internal/team/active to read
# this per call, so a team switch propagates instantly without respawning the
# MCP subprocess. Without this, team_id was frozen into the subprocess env at
# spawn time — see `app/retrace_mcp/kb.py`.
_active_team_id: dict[str, str] = {}


def get_active_team_id(user_id: str) -> Optional[str]:
    """Public accessor — returns the user's currently-selected team, or
    None if they haven't picked one yet."""
    return _active_team_id.get(user_id)


async def _persist_active_team(user_id: str, team_id: Optional[str]) -> None:
    """Save (or clear) the user's active team in `user.preferences`. Pass
    `team_id=None` to clear. Best-effort — a failure here must not fail the
    caller.
    """
    try:
        from app.models.user import User
        async with async_session_maker() as session:
            user = (await session.execute(
                select(User).where(User.user_id == user_id)
            )).scalar_one_or_none()
            if user is None:
                return
            prefs = dict(user.preferences or {})
            current = prefs.get("active_team_id")
            if team_id is None:
                if current is None:
                    return  # already cleared, skip write
                prefs.pop("active_team_id", None)
            else:
                if current == team_id:
                    return  # already correct, skip write
                prefs["active_team_id"] = team_id
            # JSON column isn't using MutableDict; reassign so SQLAlchemy
            # detects the change.
            user.preferences = prefs
            await session.commit()
    except Exception as exc:
        logger.warning("persist_active_team_failed", user_id=user_id, error=str(exc))


# Maps user_id → set of OC session ids that ReTrace has created on the user's
# behalf and that may still be alive. Populated on createSession, drained on
# stop. The live-toggle endpoint (`PATCH /opencode/tools`) fans permission
# patches across this set, so a toggle in one conversation propagates to every
# other open conversation of the user. Stale entries self-heal: a PATCH that
# returns 404 drops the id.
_active_oc_sessions: dict[str, set[str]] = {}


def _register_oc_session(user_id: str, oc_session_id: str) -> None:
    if not user_id or not oc_session_id:
        return
    _active_oc_sessions.setdefault(user_id, set()).add(oc_session_id)


def _drop_oc_session(user_id: str, oc_session_id: str) -> None:
    s = _active_oc_sessions.get(user_id)
    if not s:
        return
    s.discard(oc_session_id)
    if not s:
        _active_oc_sessions.pop(user_id, None)


# UI tool name → OC permission key. Mirrors the same renames generate_config
# uses (write/multiEdit collapse to "edit"; web_fetch → webfetch; todo_write
# → todowrite — these match OC's permission schema in config/permission.ts).
_NATIVE_PERM_KEYS: dict[str, str] = {
    "bash": "bash", "read": "read", "edit": "edit", "write": "edit",
    "multiEdit": "edit", "glob": "glob", "grep": "grep", "task": "task",
    "web_fetch": "webfetch", "todo_write": "todowrite",
}

# UI names that refer to entire MCP servers — toggled via /mcp/<name>/connect|
# disconnect rather than session permission. user-* extensions resolve at
# request time (we don't know the names statically).
_BUILTIN_MCP_NAMES = {"retrace-kb", "retrace-browser", "retrace-screenops", "retrace-search"}


def _build_native_permission_ruleset(disabled_names: set[str]) -> list[dict]:
    """Translate the user's disabled-tools list into OC's Permission.Ruleset
    shape — an array of `{permission, pattern, action}`. Emits an EXPLICIT
    rule for every native tool's OC permission key (deny if the UI name is in
    disabled_names, otherwise allow). MCP-provided tools are excluded
    (handled separately via mcp connect/disconnect).

    Why emit explicit allows: OC's session PATCH MERGES the new ruleset onto
    the existing one (Permission.merge = flat concat; evaluator uses
    findLast for the effective rule). If we only ever sent denies, toggling
    a tool back ON couldn't un-deny it — the prior deny rule would persist
    forever and findLast would still pick it. Sending an explicit allow on
    toggle-ON appends a newer rule that findLast prefers, so the effective
    permission flips back.

    UI names collapsing to the same OC key (write/multiEdit/edit → "edit")
    are folded: the key is denied if *any* of the UI names is disabled,
    otherwise allowed.
    """
    by_key: dict[str, str] = {}
    for ui_name, oc_key in _NATIVE_PERM_KEYS.items():
        action = "deny" if ui_name in disabled_names else "allow"
        # If any UI name disables the key, deny wins regardless of order.
        if by_key.get(oc_key) != "deny":
            by_key[oc_key] = action
    return [
        {"permission": key, "pattern": "*", "action": action}
        for key, action in by_key.items()
    ]


async def _cancel_active_stream(user_id: str, session_id: str) -> None:
    event = _active_streams.get((user_id, session_id))
    if event:
        event.set()


def _register_stream(user_id: str, session_id: str) -> asyncio.Event:
    cancel_event = asyncio.Event()
    _active_streams[(user_id, session_id)] = cancel_event
    return cancel_event


def _unregister_stream(user_id: str, session_id: str) -> None:
    _active_streams.pop((user_id, session_id), None)
    # Drop the active-browser-conv mapping too. New turns will set a fresh
    # one. Leaving a stale mapping would let an idle bridge call target the
    # wrong BrowserSession (e.g. after the user switched conversations).
    _active_browser_conv.pop(user_id, None)


def _read_oc_system_prompt(user_id: str, mode: str) -> Optional[str]:
    """Load the agent's system prompt for `mode` ('build' or 'plan') from the
    user's opencode.json. This is exactly the text OC fed into the LLM as the
    system role for this turn — we generated it ourselves in `opencode_manager`,
    so we can faithfully reconstruct it from disk.
    """
    try:
        from pathlib import Path
        cfg_path = Path.home() / ".config" / "opencode" / "projects" / user_id / "opencode.json"
        if not cfg_path.exists():
            return None
        cfg = json.loads(cfg_path.read_text())
        agent_def = (cfg.get("agent") or {}).get(mode or "build") or {}
        prompt = agent_def.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt
    except Exception:
        pass
    return None


async def _fetch_oc_tool_list(port: int, provider: str, model: str) -> list[dict]:
    """Fetch OC's built-in tool list from `/experimental/tool`. That endpoint
    requires BOTH `provider` and `model` query params (returns 400 otherwise),
    and returns the 12 OC built-ins (bash/read/edit/etc.) — it does NOT include
    MCP tools. MCP tools are listed separately via `_fetch_oc_mcp_servers`.
    """
    import httpx
    if not provider or not model:
        return []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/experimental/tool",
                params={"provider": provider, "model": model},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "tools" in data:
                    return data["tools"] or []
            else:
                logger.warning(
                    "[OC] llm_log: /experimental/tool non-200",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
    except Exception as exc:
        logger.warning("[OC] llm_log: /experimental/tool failed", error=str(exc))
    return []


async def _fetch_oc_mcp_servers(port: int) -> dict:
    """Fetch the live MCP-server status map from OC's `/mcp`. Returns
    `{server_name: {"status": "connected"|"failed"|..., ...}}`. The LLM also
    gets MCP-tool function definitions from these servers — but OC doesn't
    expose them via a simple HTTP endpoint, so we surface the server roster
    instead so the LLM Logs viewer at least shows which MCP servers were
    attached on this turn.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/mcp")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
    except Exception as exc:
        logger.warning("[OC] llm_log: /mcp failed", error=str(exc))
    return {}


def _format_tool_part_for_log(part: dict, *, truncate_output: int = 8000) -> dict:
    """Convert an OC `tool` part into a chat-API-style {type:'tool_use', ...}
    block plus a paired {type:'tool_result', ...} block. Used in the request
    history rendering so the LLM Logs viewer shows tool calls + their results
    inline as the LLM saw them.

    Attachments (typically screenshots from the retrace-browser MCP) live in
    `state.attachments` as `{type, mime, url}` with `url` a `data:image/...`
    URI. OC sends those to the LLM in the tool result, so we include them
    in the LLM Logs view too — otherwise the viewer makes it look like the
    LLM only received the text portion, which can be misleading when
    diagnosing vision-related issues. We pass them through verbatim so the
    frontend can render the data: URI inline (or at minimum show "1
    attachment, image/jpeg, 72 KB").
    """
    state = part.get("state") or {}
    output = state.get("output", "")
    if isinstance(output, (dict, list)):
        output = json.dumps(output, indent=2)
    output_str = str(output)
    if len(output_str) > truncate_output:
        output_str = output_str[:truncate_output] + f"\n…[truncated {len(output_str) - truncate_output} chars]"

    tool_result: dict = {
        "type": "tool_result",
        "tool_use_id": part.get("callID", ""),
        "content": output_str,
        "status": state.get("status", ""),
    }
    attachments = state.get("attachments") or []
    if isinstance(attachments, list) and attachments:
        # Trim each attachment to the fields we actually care about for log
        # display. The `url` is a `data:<mime>;base64,...` URI; the frontend
        # can render images directly from it. We keep the full URL so the
        # viewer can show the image — base64 expands the row but vision
        # debugging is the whole point of having attachments in the log.
        tool_result["attachments"] = [
            {
                "type": a.get("type"),
                "mime": a.get("mime"),
                "filename": a.get("filename"),
                "url": a.get("url"),
            }
            for a in attachments
            if isinstance(a, dict) and a.get("url")
        ]

    return {
        "tool_use": {
            "type": "tool_use",
            "id": part.get("callID", ""),
            "name": part.get("tool", ""),
            "input": state.get("input", {}),
        },
        "tool_result": tool_result,
    }


async def _build_request_messages_for_turn(
    port: int,
    oc_session_id: str,
    user_id: str,
    target_message_id: str,
    mode: str,
    tool_list_cache: list[dict],
    mcp_servers_cache: dict,
) -> list[dict]:
    """Reconstruct the messages list the LLM saw before producing `target_message_id`.

    Returns a list of `{role, content}` items matching the LLM Logs page's
    rendering contract (LlmLogs.tsx's MessageBlock handles string OR array
    content with text/tool_use/tool_result blocks).

    The reconstruction has four sections:
      1. System prompt — pulled from `opencode.json` for this agent mode.
      2. Built-in tools — OC's 12 built-ins (bash/read/edit/etc.) with their
         full JSON schemas, formatted as a text summary so they render inline.
      3. MCP servers — names + connection status of the MCP servers attached
         on this turn. (OC doesn't expose per-tool MCP definitions via HTTP,
         so we surface the server roster; the actual tool calls in the history
         section show what was invoked.)
      4. Conversation history — every message in the session BEFORE the target,
         with parts rendered as text + tool_use + tool_result blocks.

    This is the closest faithful reconstruction of what the LLM saw without
    intercepting OC's outgoing HTTP request. Token-for-token byte equivalence
    isn't guaranteed (OC may inline metadata we don't reproduce), but the
    semantic content matches.
    """
    import httpx
    out: list[dict] = []

    # 1. System prompt (from disk)
    system_prompt = _read_oc_system_prompt(user_id, mode)
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})

    # 2. Built-in tools (from OC's experimental endpoint, cached per stream)
    tools_section_lines: list[str] = []
    if tool_list_cache:
        tools_section_lines.append("## BUILT-IN TOOLS AVAILABLE\n")
        for t in tool_list_cache:
            name = t.get("id") or t.get("name") or "?"
            desc = (t.get("description") or "").strip()
            tools_section_lines.append(f"### {name}\n{desc[:600]}\n")
            params = t.get("parameters") or t.get("inputSchema") or t.get("schema")
            if params:
                try:
                    params_str = json.dumps(params, indent=2)
                    if len(params_str) > 1500:
                        params_str = params_str[:1500] + "\n…[truncated]"
                    tools_section_lines.append(f"Parameters:\n```json\n{params_str}\n```\n")
                except Exception:
                    pass

    # 3. MCP servers (from OC's /mcp endpoint, cached per stream)
    if mcp_servers_cache:
        tools_section_lines.append("\n## MCP SERVERS ATTACHED\n")
        tools_section_lines.append(
            "OpenCode does not expose individual MCP tool schemas over HTTP; "
            "each server contributes its own tool set, which appears inline "
            "as `mcp__<server>__<tool>` calls in the conversation history.\n"
        )
        for server_name, info in sorted(mcp_servers_cache.items()):
            status = info.get("status") if isinstance(info, dict) else str(info)
            tools_section_lines.append(f"- **{server_name}** — status: {status}")
        tools_section_lines.append("")

    if tools_section_lines:
        out.append({"role": "system", "content": "\n".join(tools_section_lines)})

    # 3. Conversation history — every message strictly before the target
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/session/{oc_session_id}/message")
            if resp.status_code != 200:
                return out
            messages = resp.json()
            for msg in messages:
                info = msg.get("info") or {}
                if info.get("id") == target_message_id:
                    break  # we want pre-history only
                role = info.get("role") or ""
                if role not in ("user", "assistant"):
                    continue
                parts = msg.get("parts") or []
                content_blocks: list[dict | str] = []
                for p in parts:
                    pt = p.get("type")
                    if pt == "text":
                        text = p.get("text") or ""
                        if text:
                            content_blocks.append({"type": "text", "text": text})
                    elif pt == "thinking":
                        text = p.get("text") or ""
                        if text:
                            content_blocks.append({"type": "thinking", "text": text})
                    elif pt == "tool":
                        pair = _format_tool_part_for_log(p)
                        content_blocks.append(pair["tool_use"])
                        # The tool_result is what the LLM saw on the next turn
                        # as a user-role message in some providers. We attach it
                        # inline here for display continuity in the log viewer.
                        content_blocks.append(pair["tool_result"])
                if not content_blocks:
                    continue
                # If there's only a single text block, pass content as plain
                # string — matches the simpler shape the viewer prefers.
                if len(content_blocks) == 1 and isinstance(content_blocks[0], dict) and content_blocks[0].get("type") == "text":
                    out.append({"role": role, "content": content_blocks[0]["text"]})
                else:
                    out.append({"role": role, "content": content_blocks})
    except Exception as exc:
        logger.warning("[OC] llm_log: history fetch failed", error=str(exc))

    return out


async def _build_response_content_for_turn(
    port: int,
    oc_session_id: str,
    target_message_id: str,
) -> str:
    """Render this turn's parts as a structured plaintext blob.

    Includes thinking, text, and tool call/result sections so a developer can
    see exactly what the model emitted on this round-trip — not just the final
    text answer.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/session/{oc_session_id}/message/{target_message_id}")
            if resp.status_code != 200:
                return ""
            msg = resp.json()
            parts = msg.get("parts") or []
            sections: list[str] = []
            for p in parts:
                pt = p.get("type")
                if pt == "thinking":
                    text = p.get("text") or ""
                    if text:
                        sections.append(f"━━━ THINKING ━━━\n{text}")
                elif pt == "text":
                    text = p.get("text") or ""
                    if text:
                        sections.append(f"━━━ TEXT ━━━\n{text}")
                elif pt == "tool":
                    state = p.get("state") or {}
                    tool_name = p.get("tool") or "?"
                    call_id = p.get("callID") or "?"
                    status = state.get("status") or "?"
                    try:
                        input_str = json.dumps(state.get("input") or {}, indent=2)
                    except Exception:
                        input_str = str(state.get("input"))
                    output = state.get("output", "")
                    if isinstance(output, (dict, list)):
                        try:
                            output = json.dumps(output, indent=2)
                        except Exception:
                            output = str(output)
                    output = str(output)
                    if len(output) > 8000:
                        output = output[:8000] + f"\n…[truncated {len(output) - 8000} chars]"
                    sections.append(
                        f"━━━ TOOL CALL: {tool_name} (call_id={call_id}, status={status}) ━━━\n"
                        f"input:\n{input_str}\n\n"
                        f"output:\n{output}"
                    )
                elif pt == "step-start":
                    sections.append("━━━ STEP START ━━━")
                elif pt == "step-finish":
                    sections.append("━━━ STEP FINISH ━━━")
            return "\n\n".join(sections).strip()
    except Exception as exc:
        logger.warning("[OC] llm_log: response build failed", error=str(exc))
        return ""


async def _persist_oc_llm_log(
    user_id: str,
    oc_session_id: str,
    port: int,
    info: dict,
    iteration: int,
    tool_list_cache: list[dict],
    mcp_servers_cache: dict,
) -> None:
    """Persist an LLMLog row for one OpenCode assistant-message turn, with the
    closest available reconstruction of what the LLM actually saw + produced.

    OpenCode doesn't store the raw outgoing LLM request body — but it does
    store every input piece we'd need to rebuild it: the system prompt (in
    our generated opencode.json), the tool definitions (via /experimental/tool),
    and the full conversation history (via /session/{id}/message). This
    function pulls all three and assembles them into the same `{role, content}`
    shape the legacy LangGraph CodeAct logs used, so the existing LLM Logs
    viewer renders them faithfully.

    The reconstruction is semantically equivalent to OC's outgoing request,
    not byte-equivalent — OC may inline provider-specific metadata we don't
    reproduce, but the prompt, tools, and history all match.
    """
    try:
        from app.services.session_bridge import find_conversation_by_opencode_session
        convo = await find_conversation_by_opencode_session(oc_session_id)
        conversation_id = convo.conversation_id if convo else None

        info_id = info.get("id") or ""
        mode = info.get("mode") or info.get("agent") or "build"

        tokens = info.get("tokens") or {}
        time_data = info.get("time") or {}
        created = time_data.get("created")
        completed = time_data.get("completed")
        latency_ms = 0
        if isinstance(created, (int, float)) and isinstance(completed, (int, float)) and completed >= created:
            latency_ms = int(completed - created)

        err = info.get("error")
        if isinstance(err, dict):
            error_msg = err.get("message") or json.dumps(err)[:1000]
        elif err:
            error_msg = str(err)[:1000]
        else:
            error_msg = None

        # Reconstruct the request: system prompt + tool list + MCP servers +
        # history-up-to-this-turn.
        request_messages = await _build_request_messages_for_turn(
            port=port,
            oc_session_id=oc_session_id,
            user_id=user_id,
            target_message_id=info_id,
            mode=mode,
            tool_list_cache=tool_list_cache,
            mcp_servers_cache=mcp_servers_cache,
        )

        # Render this turn's parts as a structured blob: thinking + text +
        # tool_call(input, output) sections, in chronological order.
        response_content = await _build_response_content_for_turn(
            port=port,
            oc_session_id=oc_session_id,
            target_message_id=info_id,
        )

        # Token shape: OC uses nested cache.{read,write}, but some shapes
        # still emit flat `cache` numbers — handle both.
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            cache_read = int(cache.get("read", 0) or 0)
            cache_write = int(cache.get("write", 0) or 0)
        else:
            cache_read = int(tokens.get("cache", 0) or 0)
            cache_write = 0

        input_tokens = int(tokens.get("input", 0) or 0)
        output_tokens = int(tokens.get("output", 0) or 0)

        from app.db.database import async_session_maker
        from app.models.llm_log import LLMLog
        async with async_session_maker() as db:
            db.add(LLMLog(
                log_id=str(uuid4()),
                user_id=user_id,
                conversation_id=conversation_id,
                iteration=iteration,
                request_messages_json=json.dumps(request_messages),
                response_content=(response_content or "")[:200000],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_write,
                reasoning_tokens=int(tokens.get("reasoning", 0) or 0),
                model_provider=info.get("providerID"),
                model_name=info.get("modelID"),
                latency_ms=latency_ms,
                error=error_msg,
            ))
            await db.commit()
    except Exception as exc:
        logger.warning("[OC] llm_log persist failed", error=str(exc))


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class AgentSessionResponse(BaseModel):
    session_id: str
    team_id: str
    task: str
    status: str
    iterations: int
    final_answer: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class AgentToolInfo(BaseModel):
    name: str
    description: str
    requires_key: Optional[str] = None
    available: bool = True
    enabled: bool = True
    group: Optional[str] = None


# ── Conversation schemas ─────────────────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    team_id: Optional[str] = Field(default=None)
    title: str = Field(default="New conversation", max_length=255)


class ConversationRenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)


class MessageSaveRequest(BaseModel):
    """Bulk-save messages for a conversation (used after agent execution)."""
    messages: List[dict]


# ── Learn This schemas ───────────────────────────────────────────────────

class LearnThisRequest(BaseModel):
    """Train a Q&A pair from an agent conversation."""
    team_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)


# ── SOP schemas ──────────────────────────────────────────────────────────

class CreateSOPRequest(BaseModel):
    """Create an SOP from current conversation context."""
    team_id: Optional[str] = Field(default=None)
    goal: str = Field(..., min_length=1)
    messages: List[dict] = Field(..., min_length=1)
    conversation_id: Optional[str] = None


class EditSOPRequest(BaseModel):
    """Edit an existing SOP with user-described changes."""
    sop_id: str = Field(..., min_length=1)
    edit_instructions: str = Field(..., min_length=1)


class ApproveAutomationRequest(BaseModel):
    """Approve an automation with optional scheduling."""
    schedule_type: str = Field(default="none")  # none, once, interval, daily, weekly, monthly, cron
    schedule_config: Optional[dict] = None



# ---------------------------------------------------------------------------
# OpenCode proxy endpoints
# ---------------------------------------------------------------------------

class OpenCodeSessionCreateRequest(BaseModel):
    team_id: Optional[str] = None
    conversation_id: Optional[str] = None
    agent: Optional[str] = None


class OpenCodeMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)
    # Optional attachment parts in OC's wire shape: `{type:"file", mime, filename, url}`
    # where `url` is an RFC 2397 data URL. Forwarded verbatim to OC's prompt_async
    # alongside the text part. None / empty = no attachments.
    parts: Optional[list[dict]] = None
    # Per-turn agent override ("build" or "plan"). OC's PromptInput accepts
    # `agent` as an optional string (see prompt.ts:2062) — different turns in
    # the same session can use different agents. None → OC falls back to the
    # session's original agent. The frontend's Build/Plan toggle flows here.
    agent: Optional[str] = None
    # Per-conversation "Thinking" mode: "off" | "think" | "deep". Drives native
    # reasoning by regenerating opencode.json (reasoning + interleaved +
    # enable_thinking) when it changes. None / "off" → no reasoning (default).
    reasoning: Optional[str] = None


@router.post("/opencode/session")
async def opencode_create_session(
    body: OpenCodeSessionCreateRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create an OpenCode session and link it to a ReTrace conversation."""
    import httpx

    logger.info("[OC] create_session", user_id=current_user.user_id, team_id=body.team_id, agent=body.agent, conversation_id=body.conversation_id)

    # If conversation already has an OpenCode session, check if it still exists
    if body.conversation_id:
        existing_session_id = await find_opencode_session_id(body.conversation_id)
        if existing_session_id:
            port = opencode_manager.get_port(current_user.user_id)
            if port:
                async with httpx.AsyncClient(timeout=10) as client:
                    try:
                        check = await client.get(f"http://127.0.0.1:{port}/session/{existing_session_id}")
                        if check.status_code == 200:
                            logger.info("[OC] reusing existing session", oc_session_id=existing_session_id, conversation_id=body.conversation_id)
                            return {"opencode_session_id": existing_session_id, "port": port}
                    except Exception:
                        pass

    port = await opencode_manager.start_for_user(
        user_id=current_user.user_id,
        team_id=body.team_id,
    )

    async with httpx.AsyncClient(timeout=30) as client:
        # External-mode bootstrap: on the very first /agent click after a clean
        # checkout, start_for_user just wrote opencode.json but the OC daemon
        # (launched separately by scripts/start-dev.sh → run-for-retrace.sh) is
        # still in its 5–10 s cold `bun run` warmup before it binds :4096.
        # Wait here until /doc answers so the user gets "first click works"
        # instead of "first click 502s, second click works." Capped at 45 s.
        # No-op when OC is already up (typical case) — the first probe succeeds.
        external_mode = bool(os.environ.get("RETRACE_OPENCODE_EXTERNAL_URL", "").strip())
        if external_mode:
            ready = False
            for _ in range(90):  # 90 × 0.5s = 45s
                try:
                    probe = await client.get(f"http://127.0.0.1:{port}/doc", timeout=1.0)
                    if probe.status_code < 500:
                        ready = True
                        break
                except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(0.5)
            if not ready:
                logger.error("[OC] create_session: OC not reachable on :%d after 45s", port)
                raise HTTPException(
                    status_code=503,
                    detail="OpenCode is still starting. Wait a few seconds and try again.",
                )

        create_payload = {}
        if body.agent:
            create_payload["agent"] = body.agent
        resp = await client.post(f"http://127.0.0.1:{port}/session", json=create_payload)
        logger.info("[OC] create_session upstream", status=resp.status_code, payload=create_payload)
        if resp.status_code != 200:
            logger.error("[OC] create_session failed", status=resp.status_code, body=resp.text[:200])
            raise HTTPException(status_code=502, detail=f"OpenCode session creation failed: {resp.status_code}")

        session_data = resp.json()
        oc_session_id = session_data.get("id")

        # Apply the user's disabled-tools baseline to this fresh OC session so
        # it starts in the same state as the user's other open sessions. The
        # live-toggle endpoint (`PATCH /opencode/tools`) keeps existing sessions
        # in sync; this PATCH-after-create covers new sessions without needing
        # the config file to be rewritten.
        try:
            from app.api.settings import get_active_llm_settings
            from app.db.database import async_session_maker as _settings_session_maker
            async with _settings_session_maker() as _settings_session:
                _llm = await get_active_llm_settings(_settings_session)
            _cfg = _llm.get("agent_tools_config") or {}
            _disabled = set(_cfg.get("disabled") or [])
            _native_rules = _build_native_permission_ruleset(_disabled)
            if _native_rules and oc_session_id:
                patch_resp = await client.patch(
                    f"http://127.0.0.1:{port}/session/{oc_session_id}",
                    json={"permission": _native_rules},
                )
                if patch_resp.status_code not in (200, 204):
                    logger.warning(
                        "session permission seed failed",
                        status=patch_resp.status_code,
                        oc_session_id=oc_session_id,
                    )
        except Exception as exc:
            # Best-effort — the agent baseline at config-file level still
            # applies, so the session is functional even if this PATCH fails.
            logger.warning("session permission seed errored", error=str(exc))

    if oc_session_id:
        _register_oc_session(current_user.user_id, oc_session_id)

    if body.conversation_id:
        await link_session(body.conversation_id, oc_session_id)
    else:
        await get_or_create_conversation(
            opencode_session_id=oc_session_id,
            user_id=current_user.user_id,
            team_id=body.team_id,
        )

    logger.info("[OC] create_session complete", oc_session_id=oc_session_id, port=port)
    return {"opencode_session_id": oc_session_id, "port": port}


@router.post("/opencode/session/{session_id}/message")
async def opencode_send_message(
    session_id: str,
    request: OpenCodeMessageRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Send a message to an OpenCode session. Streams real-time SSE events."""
    import httpx

    # Apply the per-conversation "Thinking" mode BEFORE resolving the port. When
    # the mode changed since the last config, this regenerates opencode.json and
    # reloads the user's OpenCode (so native reasoning takes effect), which may
    # re-allocate the port — hence we read get_port() afterwards. No-op when
    # unchanged, so steady-state sends pay nothing.
    try:
        await opencode_manager.set_reasoning(current_user.user_id, request.reasoning)
    except Exception as exc:
        logger.warning("[OC] set_reasoning failed", user_id=current_user.user_id, error=str(exc))

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        logger.error("[OC] send_message: no OpenCode server", user_id=current_user.user_id)
        raise HTTPException(status_code=502, detail="OpenCode server not running for this user")

    attachment_count = len(request.parts or [])
    logger.info(
        "[OC] send_message",
        session_id=session_id,
        content_len=len(request.content),
        content_preview=request.content[:100],
        attachments=attachment_count,
    )

    user_id = current_user.user_id
    await _cancel_active_stream(user_id, session_id)
    cancel_event = _register_stream(user_id, session_id)

    # Tell the internal browser bridge which ReTrace conversation this user
    # is currently driving the agent in. The bridge uses this to route every
    # `retrace-browser` MCP action onto the SAME BrowserSession the user's
    # BrowserWorkspace WebSocket is attached to — so the agent's navigation
    # actually shows up in the right panel. Cleared in the relay's `finally`
    # so a new turn for a different conversation isn't poisoned.
    try:
        from app.services.session_bridge import find_conversation_by_opencode_session
        convo_for_browser = await find_conversation_by_opencode_session(session_id)
        if convo_for_browser and convo_for_browser.conversation_id:
            real_conv_id = convo_for_browser.conversation_id
            _active_browser_conv[user_id] = real_conv_id
            logger.debug(
                "[OC] browser_bridge: active conv set",
                user_id=user_id,
                retrace_conv=real_conv_id,
                oc_session=session_id,
            )
            # Adopt the user's `standalone_xxx` BrowserSession (the one
            # mounted by the /agent page before any conversation existed)
            # onto the real conv_id. Without this, the agent's first
            # browser tool call would `get_or_create(real_conv_id)` and
            # mint a fresh headless session — the user's panel stays
            # attached to the standalone session and sees nothing.
            try:
                from app.services.browser_manager import browser_manager
                await browser_manager.adopt_into_conversation(user_id, real_conv_id)
            except Exception as exc:
                logger.warning("[OC] browser_bridge: adopt failed", error=str(exc))
    except Exception as exc:
        logger.warning("[OC] browser_bridge: active conv lookup failed", error=str(exc))

    # Assemble the prompt_async body: text part first, then any attachments.
    # OC's `part.data` schema accepts `{type:"file", mime, filename, url}`
    # verbatim (verified via `~/.local/share/opencode/opencode.db`'s `part` rows).
    oc_parts: list[dict] = [{"type": "text", "text": request.content}]
    if request.parts:
        # Light validation: only forward parts whose type we expect. Reject
        # anything else so a malformed frontend can't smuggle arbitrary data
        # into OC's storage.
        for p in request.parts:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "file":
                if not isinstance(p.get("url"), str):
                    continue
                oc_parts.append({
                    "type": "file",
                    "mime": p.get("mime") or "application/octet-stream",
                    "filename": p.get("filename") or "attachment",
                    "url": p["url"],
                })
            elif ptype == "agent":
                # Forwarded as-is for future Phase 4 (@-mention) wiring.
                name = p.get("name")
                if isinstance(name, str) and name:
                    agent_part: dict = {"type": "agent", "name": name}
                    src = p.get("source")
                    if isinstance(src, dict):
                        agent_part["source"] = src
                    oc_parts.append(agent_part)

    payload: dict = {"parts": oc_parts}
    # Forward per-turn agent so OC uses the right system prompt + permissions
    # (e.g. plan denies edit/bash). Only forward known names; unknown values
    # would be silently rejected by OC's schema, so guard locally.
    if request.agent in ("build", "plan", "mcp-builder"):
        payload["agent"] = request.agent

    async def stream_generator():
        event_count = 0
        relayed_count = 0
        # OpenCode's EventMessagePartUpdated carries only {sessionID, part, time}
        # — no role info. To stop user-message text parts from being forwarded
        # (which would otherwise be appended to the assistant message on the
        # frontend, making the chat show the user's own text twice), we track
        # role per messageID from the role-bearing message.updated events.
        # IDs we haven't seen yet default to "skip" (treated as not assistant).
        message_roles: dict[str, str] = {}
        # One LLMLog row per assistant message turn (per OC round-trip).
        # Counter scoped to this stream so multi-step tool-use turns produce
        # iteration=1, 2, 3, … like the legacy LangGraph CodeAct logs.
        iter_counter = 0
        # OC's stream sometimes fires `message.updated` twice for the same
        # assistant message (e.g. once when the tool call hits status=tool-calls
        # and again ~250ms later when the tool's running→completed transition
        # settles). The second emit has identical content, so persisting on
        # both produced duplicate LLMLog rows. Track which msg_id we've already
        # persisted per stream and skip subsequent emits.
        logged_msg_ids: set[str] = set()
        # Tool list + MCP-server roster for this stream — fetched lazily on the
        # first persist and reused. Neither changes mid-stream so one fetch is
        # enough. `/experimental/tool` requires the provider+model that were
        # used on the round-trip (returns 400 otherwise) — those come from
        # `info.providerID` / `info.modelID` on the first `message.updated`.
        tool_list_cache: Optional[list[dict]] = None
        mcp_servers_cache: Optional[dict] = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            try:
                event_stream = client.stream(
                    "GET",
                    f"http://127.0.0.1:{port}/global/event",
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                )
                event_resp = await event_stream.__aenter__()
                logger.info("[OC] subscribed to /global/event", session_id=session_id)
            except Exception as e:
                logger.error("[OC] failed to subscribe to event stream", error=str(e))
                yield f"data: {json.dumps({'type': 'error', 'error': f'Failed to subscribe to event stream: {e}'})}\n\n"
                _unregister_stream(user_id, session_id)
                return

            try:
                async_resp = await client.post(
                    f"http://127.0.0.1:{port}/session/{session_id}/prompt_async",
                    json=payload,
                )
                logger.info("[OC] prompt_async response", status=async_resp.status_code, session_id=session_id)
                if async_resp.status_code not in (200, 204):
                    body_text = async_resp.text[:200] if async_resp.text else ""
                    logger.error("[OC] prompt_async failed", status=async_resp.status_code, body=body_text)
                    yield f"data: {json.dumps({'type': 'error', 'error': f'prompt_async failed: {async_resp.status_code}'})}\n\n"
                    await event_stream.__aexit__(None, None, None)
                    _unregister_stream(user_id, session_id)
                    return
            except Exception as e:
                logger.error("[OC] prompt_async error", error=str(e))
                yield f"data: {json.dumps({'type': 'error', 'error': f'prompt_async error: {e}'})}\n\n"
                await event_stream.__aexit__(None, None, None)
                _unregister_stream(user_id, session_id)
                return

            try:
                async for line in event_resp.aiter_lines():
                    if cancel_event.is_set():
                        logger.info("[OC] stream cancelled by new request", session_id=session_id)
                        break

                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue

                    try:
                        wrapper = json.loads(data_str)
                    except Exception:
                        continue

                    payload_obj = wrapper.get("payload", wrapper)
                    event_type = payload_obj.get("type", "")
                    properties = payload_obj.get("properties", {})

                    event_count += 1

                    evt_session_id = properties.get("sessionID", "")
                    if evt_session_id and evt_session_id != session_id:
                        continue

                    if event_type == "sync":
                        continue

                    relayed_count += 1

                    if event_type == "message.part.delta":
                        # Incremental token-by-token stream from OC. Payload:
                        # {sessionID, messageID, partID, field, delta}. We
                        # forward it untouched so the frontend can append to
                        # the matching part (creating it on the first delta).
                        # Skip deltas belonging to a user message — same logic
                        # as message.part.updated below.
                        msg_id = properties.get("messageID", "")
                        role = message_roles.get(msg_id)
                        if role != "assistant":
                            continue
                        part_id = properties.get("partID", "")
                        field = properties.get("field", "")
                        delta = properties.get("delta", "")
                        if not part_id or not field:
                            continue
                        yield f"data: {json.dumps({'type': 'message.part.delta', 'partID': part_id, 'messageID': msg_id, 'field': field, 'delta': delta})}\n\n"
                        continue

                    if event_type == "message.part.updated":
                        part = properties.get("part", {})
                        msg_id = properties.get("messageID", "") or part.get("messageID", "")
                        part_type = part.get("type")

                        # Skip parts that belong to a user message. Otherwise the
                        # frontend would append the user's own text/tool parts to
                        # the assistant message (resulting in "hello" appearing
                        # twice: once as your message, once as the assistant's).
                        # If we haven't yet seen a message.updated event for this
                        # messageID, default to skipping — the assistant message
                        # role will arrive before its real content does.
                        role = message_roles.get(msg_id)
                        if role != "assistant":
                            logger.debug(
                                "[OC] skip non-assistant part",
                                msg_id=msg_id,
                                known_role=role,
                                part_type=part_type,
                            )
                            continue

                        logger.debug("[OC] EVT message.part.updated", part_type=part_type, part_id=part.get("id"), tool=part.get("tool"), text_len=len(part.get("text", "")) if part_type == "text" else None)

                        if part_type == "text":
                            # Response content is rebuilt at log-persist time
                            # via GET /session/{id}/message/{id} so we always
                            # get the canonical OC state including tool parts;
                            # no need to accumulate streamed text here.
                            yield f"data: {json.dumps({'type': 'message.part.created', 'part': {'type': 'text', 'text': part.get('text', ''), 'id': part.get('id', ''), 'messageID': msg_id}})}\n\n"
                        elif part_type == "tool":
                            tool_state = part.get("state", {})
                            yield f"data: {json.dumps({'type': 'message.part.created', 'part': {'type': 'tool', 'tool': part.get('tool', ''), 'callID': part.get('callID', ''), 'input': tool_state.get('input', {}), 'output': tool_state.get('output', ''), 'title': part.get('title', ''), 'status': tool_state.get('status', ''), 'id': part.get('id', ''), 'messageID': msg_id}})}\n\n"
                        elif part_type == "thinking":
                            yield f"data: {json.dumps({'type': 'message.part.created', 'part': {'type': 'thinking', 'text': part.get('text', ''), 'id': part.get('id', ''), 'messageID': msg_id}})}\n\n"
                        elif part_type == "step-start":
                            yield f"data: {json.dumps({'type': 'message.part.created', 'part': {'type': 'step-start', 'id': part.get('id', ''), 'messageID': msg_id}})}\n\n"
                        elif part_type == "step-finish":
                            yield f"data: {json.dumps({'type': 'message.part.created', 'part': {'type': 'step-finish', 'id': part.get('id', ''), 'messageID': msg_id}})}\n\n"

                    elif event_type == "message.updated":
                        info = properties.get("info", {})
                        info_id = info.get("id", "")
                        info_role = info.get("role", "")
                        # Track every message's role so the part filter above can
                        # tell user parts from assistant parts.
                        if info_id and info_role:
                            message_roles[info_id] = info_role
                        if info_role == "assistant":
                            logger.debug("[OC] EVT message.updated", msg_id=info_id, finish=info.get("finish"), cost=info.get("cost"))
                            yield f"data: {json.dumps({'type': 'message.created', 'message': {'id': info_id, 'role': 'assistant'}})}\n\n"
                            if info.get("finish") and info_id and info_id not in logged_msg_ids:
                                # Persist an LLMLog row for this round-trip.
                                # Pulls the canonical conversation state from
                                # OC (messages + parts + tool definitions +
                                # the system prompt we wrote into opencode.json)
                                # so the row contains the same level of detail
                                # the legacy LangGraph CodeAct logs did.
                                logged_msg_ids.add(info_id)
                                # Legacy reconstruction. The AI-SDK middleware
                                # capture (POST /api/v1/internal/llm/capture) is
                                # the canonical writer now; opt back into the
                                # reconstruction with RETRACE_LLM_LOG_FALLBACK=1.
                                if os.environ.get("RETRACE_LLM_LOG_FALLBACK") == "1":
                                    iter_counter += 1
                                    if tool_list_cache is None:
                                        tool_list_cache = await _fetch_oc_tool_list(
                                            port,
                                            provider=info.get("providerID") or "",
                                            model=info.get("modelID") or "",
                                        )
                                    if mcp_servers_cache is None:
                                        mcp_servers_cache = await _fetch_oc_mcp_servers(port)
                                    try:
                                        await _persist_oc_llm_log(
                                            user_id=user_id,
                                            oc_session_id=session_id,
                                            port=port,
                                            info=info,
                                            iteration=iter_counter,
                                            tool_list_cache=tool_list_cache or [],
                                            mcp_servers_cache=mcp_servers_cache or {},
                                        )
                                    except Exception as _log_exc:
                                        logger.warning("[OC] llm_log inline failed", error=str(_log_exc))
                                yield f"data: {json.dumps({'type': 'message.completed', 'message': {'id': info_id, 'finish': info.get('finish'), 'cost': info.get('cost'), 'tokens': info.get('tokens')}})}\n\n"
                                if info.get("finish") == "stop":
                                    logger.info("[OC] stream complete", session_id=session_id, total_events=event_count, relayed=relayed_count)
                                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                    return

                    elif event_type == "session.status":
                        status = properties.get("status", {})
                        logger.debug("[OC] EVT session.status", status=status)
                        yield f"data: {json.dumps({'type': 'session.status', 'status': status})}\n\n"

                    elif event_type == "question.asked":
                        # OC's `question` tool waits on a structured reply
                        # (POST /question/{id}/reply). Forward the full request
                        # to the frontend so it can render interactive radios/
                        # checkboxes and post answers back via our proxy.
                        req_id = properties.get("id") or properties.get("requestID", "")
                        questions = properties.get("questions") or []
                        tool_info = properties.get("tool") or {}
                        evt_sid = properties.get("sessionID", "")
                        logger.info(
                            "[OC] EVT question.asked",
                            request_id=req_id,
                            question_count=len(questions) if isinstance(questions, list) else 0,
                            session_id=evt_sid,
                        )
                        yield f"data: {json.dumps({'type': 'question.asked', 'request': {'id': req_id, 'sessionID': evt_sid, 'questions': questions, 'tool': tool_info}})}\n\n"

                    elif event_type in ("question.replied", "question.rejected"):
                        req_id = properties.get("requestID") or properties.get("id", "")
                        logger.info(f"[OC] EVT {event_type}", request_id=req_id)
                        yield f"data: {json.dumps({'type': event_type, 'requestID': req_id})}\n\n"

                    elif event_type == "permission.asked":
                        permission_id = properties.get("id", "")
                        tool_info = properties.get("tool", {})
                        permission_type = properties.get("permission", "")
                        metadata = properties.get("metadata", {})
                        patterns = properties.get("patterns", {})

                        if isinstance(tool_info, str):
                            tool_name = tool_info
                        elif isinstance(tool_info, dict):
                            tool_name = tool_info.get("name", "")
                        else:
                            tool_name = ""

                        if not tool_name and isinstance(permission_type, str):
                            tool_name = permission_type

                        call_id = tool_info.get("callID", "") if isinstance(tool_info, dict) else ""
                        input_data = metadata if isinstance(metadata, dict) and metadata else properties.get("input", {})

                        if isinstance(input_data, dict) and not input_data and isinstance(patterns, dict):
                            input_data = patterns

                        logger.info(
                            "[OC] EVT permission.asked",
                            permission_id=permission_id,
                            tool_name=tool_name,
                            call_id=call_id,
                            permission_type=permission_type,
                            metadata_keys=list(metadata.keys()) if isinstance(metadata, dict) else None,
                            patterns_keys=list(patterns.keys()) if isinstance(patterns, dict) else None,
                        )
                        yield f"data: {json.dumps({'type': 'permission.asked', 'permission': {'id': permission_id, 'tool': tool_name or 'unknown', 'callID': call_id, 'input': input_data}})}\n\n"

                    elif event_type == "session.error":
                        # OC fires this when the provider returns an HTTP error,
                        # an MCP server crashes mid-call, or an internal exception
                        # bubbles up. Without forwarding it the user sees silence.
                        err = properties.get("error") or {}
                        if isinstance(err, dict):
                            message = err.get("message") or err.get("name") or "Session error"
                            code = err.get("code") or err.get("name") or ""
                        elif isinstance(err, str):
                            message = err
                            code = ""
                        else:
                            message = "Session error"
                            code = ""
                        logger.warning("[OC] EVT session.error", code=code, message=message[:200])
                        yield f"data: {json.dumps({'type': 'session.error', 'message': message, 'code': code})}\n\n"

                    elif event_type == "message.part.removed":
                        # OC fires this on revert / fork-cleanup. We forward
                        # {messageID, partID} so the frontend can drop the
                        # part from its local message tree (otherwise stale
                        # text or tool blocks linger until refreshMessages).
                        msg_id = properties.get("messageID", "")
                        part_id = properties.get("partID", "")
                        if msg_id and part_id:
                            yield f"data: {json.dumps({'type': 'message.part.removed', 'messageID': msg_id, 'partID': part_id})}\n\n"

                    else:
                        logger.debug("[OC] EVT unknown", event_type=event_type)

            except httpx.ReadTimeout:
                logger.warning("[OC] stream read timeout", session_id=session_id)
            finally:
                try:
                    await event_stream.__aexit__(None, None, None)
                except Exception:
                    pass
                _unregister_stream(user_id, session_id)
                # Persist OpenCode's full message log into ReTrace's DB so the
                # conversation (tool calls, thinking, costs, etc.) survives an
                # OC sidecar restart. Idempotent — only new parts get inserted.
                try:
                    async with httpx.AsyncClient(timeout=15) as sync_client:
                        msgs_resp = await sync_client.get(
                            f"http://127.0.0.1:{port}/session/{session_id}/message"
                        )
                        if msgs_resp.status_code == 200:
                            await sync_messages_from_opencode(session_id, msgs_resp.json())
                except Exception as sync_exc:
                    logger.warning("[OC] post-stream sync failed", error=str(sync_exc))

            logger.info("[OC] stream ended (no stop event)", session_id=session_id, total_events=event_count, relayed=relayed_count)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/opencode/session/{session_id}")
async def opencode_get_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get OpenCode session info (including revert state)."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"http://127.0.0.1:{port}/session/{session_id}")
        logger.info("[OC] get_session", session_id=session_id, status=resp.status_code)
        return resp.json()


@router.get("/opencode/session/{session_id}/message")
async def opencode_get_messages(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get all messages for an OpenCode session."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"http://127.0.0.1:{port}/session/{session_id}/message")
        data = resp.json()
        msg_count = len(data) if isinstance(data, list) else None
        logger.info("[OC] get_messages", session_id=session_id, status=resp.status_code, msg_count=msg_count)
        return data


@router.get("/opencode/session/{session_id}/todo")
async def opencode_get_todos(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return OpenCode's session-level TODO list.

    OpenCode's agents maintain a live TODO list as they plan and execute.
    Surfacing it gives the user visibility into "what is the agent working on /
    about to do next" — distinct from the assistant's text output.
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"http://127.0.0.1:{port}/session/{session_id}/todo")
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return []
        except Exception as exc:
            logger.warning("[OC] get_todos failed", error=str(exc))
    return []


@router.post("/opencode/session/{session_id}/revert")
async def opencode_revert(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    body: dict = None,
):
    """Revert (undo) the last message in an OpenCode session."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    logger.info("[OC] revert", session_id=session_id, has_body=body is not None)
    async with httpx.AsyncClient(timeout=30) as client:
        if not body or not body.get("messageID"):
            msgs_resp = await client.get(f"http://127.0.0.1:{port}/session/{session_id}/message")
            if msgs_resp.status_code == 200:
                messages = msgs_resp.json()
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("info", {}).get("role") == "user":
                        body = {"messageID": msg["info"]["id"]}
                        break
        if not body:
            body = {}
        resp = await client.post(f"http://127.0.0.1:{port}/session/{session_id}/revert", json=body)
        logger.info("[OC] revert response", session_id=session_id, status=resp.status_code)
        return resp.json()


@router.post("/opencode/session/{session_id}/unrevert")
async def opencode_unrevert(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Unrevert (redo) the last reverted message in an OpenCode session."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    logger.info("[OC] unrevert", session_id=session_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"http://127.0.0.1:{port}/session/{session_id}/unrevert", json={})
        return resp.json()


@router.get("/opencode/status")
async def opencode_status(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Check if the OpenCode sidecar is running for the current user."""
    port = opencode_manager.get_port(current_user.user_id)
    proc = opencode_manager.get_process(current_user.user_id)
    running = proc is not None and proc.process is not None and proc.process.returncode is None
    healthy = False
    if running:
        healthy = await proc.is_healthy()
    # External mode: the developer owns the OC process; ReTrace can't stop it.
    # The frontend uses this to hide the "Stop OpenCode server" overflow item,
    # which would otherwise show a misleading "stopped" toast for a no-op.
    external = bool(os.environ.get("RETRACE_OPENCODE_EXTERNAL_URL", "").strip())
    return {
        "running": running,
        "healthy": healthy,
        "port": port,
        "external": external,
    }


@router.post("/opencode/start")
async def opencode_start(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    team_id: Optional[str] = None,
):
    """Start the OpenCode sidecar for the current user."""
    logger.info("[OC] start", user_id=current_user.user_id, team_id=team_id)
    port = await opencode_manager.start_for_user(
        user_id=current_user.user_id,
        team_id=team_id,
    )
    logger.info("[OC] started", user_id=current_user.user_id, port=port)
    return {"status": "started", "port": port}


@router.post("/opencode/stop")
async def opencode_stop(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Stop the OpenCode sidecar for the current user."""
    logger.info("[OC] stop", user_id=current_user.user_id)
    await opencode_manager.stop_for_user(current_user.user_id)
    _active_oc_sessions.pop(current_user.user_id, None)
    return {"status": "stopped"}


@router.patch("/opencode/tools")
async def opencode_set_tools(
    body: AgentToolsUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Apply tool-toggle changes LIVE without rewriting the config file.

    Replaces the path that used to PATCH settings.agent_tools_config then
    trigger `regenerate-config` (which forced a watcher reload + dispose-all).
    The new path:
      1. Persists the new disabled list to settings.agent_tools_config (using
         the same legacy-normalization helper that the Settings page uses).
      2. For OC native tools: PATCHes every open OC session of the user with
         the freshly-built Permission.Ruleset (deny rules for each disabled
         native tool). OC merges session permission with the agent permission
         per-turn; the deny rules win via the evaluator's `findLast()`.
      3. For MCP-provided tools (retrace-* + user-*): POSTs to OC's
         `/mcp/<name>/connect|disconnect`. Diff between previous and new
         disabled sets so we only act on changed MCPs.

    The config file (`opencode.json`) is NOT touched here. The watcher is
    NOT fired. Other open OC sessions for the user are not disrupted beyond
    their own permission update.
    """
    from app.api.settings import update_agent_tools, get_active_llm_settings

    user_id = current_user.user_id

    # 1. Snapshot the previous disabled set (for MCP diff). Must read before
    #    the persist call below mutates settings.agent_tools_config.
    prev_llm = await get_active_llm_settings(session)
    prev_cfg = prev_llm.get("agent_tools_config") or {}
    prev_disabled: set[str] = set(prev_cfg.get("disabled") or [])

    # 2. Persist the new list. Reuses Settings' route handler (it's a plain
    #    async function under the decorator) for legacy KB-name normalization
    #    and the JSON serialization.
    result = await update_agent_tools(body, session, current_user)
    new_disabled: set[str] = set(result.get("disabled_tools") or [])

    # 3. Build the native deny ruleset for ALL currently-disabled native
    #    tools — OC's PATCH appends to existing permissions, so we pass the
    #    full current state every time and let `findLast()` resolve.
    native_rules = _build_native_permission_ruleset(new_disabled)

    # 4. MCP diff — which MCPs newly flipped this call.
    def _is_mcp(name: str) -> bool:
        return name in _BUILTIN_MCP_NAMES or name.startswith("user-")
    newly_disabled_mcps = {n for n in (new_disabled - prev_disabled) if _is_mcp(n)}
    newly_enabled_mcps = {n for n in (prev_disabled - new_disabled) if _is_mcp(n)}

    port = opencode_manager.get_port(user_id)
    if port:
        import httpx
        # Use one client across all the PATCH/POST calls below — fan-out is
        # small (one per open session + one per changed MCP), so a single
        # client with short timeout is plenty.
        async with httpx.AsyncClient(timeout=5) as client:
            # 5. Native permissions — fan out to EVERY OC session the user has
            # right now: parent + any live subagents. We query OC's /session
            # endpoint as the source of truth instead of relying on the
            # `_active_oc_sessions` in-memory cache, because:
            #   (a) the cache clears on backend restart — a post-restart toggle
            #       would otherwise reach zero sessions and leave subagents
            #       running with stale permissions;
            #   (b) subagents are created on-the-fly by OC's `task` tool, not
            #       through our `opencode_create_session` path, so they never
            #       enter the cache anyway. The "subagent escape" we observed
            #       was a direct consequence of (a) — the parent didn't get
            #       the deny, so OC's `deriveSubagentSessionPermission`
            #       (subagent-permissions.ts) had no parent deny to inherit.
            # Now we PATCH all of them, including subagents. OC's permission
            # merge appends our explicit ruleset; findLast resolves correctly.
            session_ids: list[str] = []
            try:
                list_resp = await client.get(f"http://127.0.0.1:{port}/session")
                if list_resp.status_code == 200:
                    all_sessions = list_resp.json() or []
                    session_ids = [s["id"] for s in all_sessions if isinstance(s, dict) and s.get("id")]
            except Exception as exc:
                logger.warning("set_tools session list failed; falling back to cache", error=str(exc))
                session_ids = list(_active_oc_sessions.get(user_id, set()))

            for sid in session_ids:
                try:
                    resp = await client.patch(
                        f"http://127.0.0.1:{port}/session/{sid}",
                        json={"permission": native_rules},
                    )
                    if resp.status_code == 404:
                        # OC doesn't have this session anymore — drop it.
                        _drop_oc_session(user_id, sid)
                    elif resp.status_code not in (200, 204):
                        logger.warning(
                            "set_tools session patch failed",
                            status=resp.status_code, oc_session_id=sid,
                        )
                except Exception as exc:
                    logger.warning("set_tools session patch errored", error=str(exc), oc_session_id=sid)

            # 6. MCP connect/disconnect for the names that changed state.
            for mcp_name in newly_disabled_mcps:
                try:
                    await client.post(f"http://127.0.0.1:{port}/mcp/{mcp_name}/disconnect")
                except Exception as exc:
                    logger.warning("mcp disconnect failed", name=mcp_name, error=str(exc))
            for mcp_name in newly_enabled_mcps:
                try:
                    await client.post(f"http://127.0.0.1:{port}/mcp/{mcp_name}/connect")
                except Exception as exc:
                    logger.warning("mcp connect failed", name=mcp_name, error=str(exc))

    return {"ok": True, "disabled_tools": sorted(new_disabled)}


@router.post("/opencode/regenerate-config")
async def opencode_regenerate_config(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    team_id: Optional[str] = None,
    model: Optional[str] = None,
):
    """Regenerate the OpenCode config and restart the sidecar so MCP env vars
    (RETRACE_TEAM_ID, RETRACE_USER_ID), the top-level model, the system prompt,
    and the team description all reflect the new state. Called by the frontend
    on team change, model picker change, tool toggle, and after LLM settings
    changes.

    `model` is optional — expected as "provider/model" (e.g. "openai/gpt-4o"
    or "anthropic/claude-opus-4"). If absent, the DB default is used.

    Bypasses `proc.restart()`'s 3-restart cap (which is meant for unhealthy
    auto-restarts) by doing a clean stop_for_user + start_for_user.
    """
    logger.info("[OC] regenerate-config", user_id=current_user.user_id, team_id=team_id, model=model)

    # Track active team for MCP subprocesses (via /api/v1/agent/internal/team/active).
    # The frontend is the source of truth — whatever it tells us, we use:
    #   team_id="<id>" → user picked a team
    #   team_id=None   → user has "No team" selected (or no selection yet)
    # Treating None as "no-op" was wrong — that left stale teams active when
    # the picker said "No team", leading to KB returning data the user
    # didn't ask for.
    if team_id:
        _active_team_id[current_user.user_id] = team_id
    else:
        _active_team_id.pop(current_user.user_id, None)
    await _persist_active_team(current_user.user_id, team_id)

    proc = opencode_manager.get_process(current_user.user_id)
    was_running = proc is not None and proc.process is not None and proc.process.returncode is None
    if was_running:
        await opencode_manager.stop_for_user(current_user.user_id)
        # start_for_user calls generate_config internally — no need to write twice.
        await opencode_manager.start_for_user(
            user_id=current_user.user_id,
            team_id=team_id,
            model_override=model,
        )
        config_path = opencode_manager.get_process(current_user.user_id).config_dir / "opencode.json"
    else:
        config_path = await opencode_manager.generate_config(
            user_id=current_user.user_id,
            team_id=team_id,
            model_override=model,
        )
    return {"status": "regenerated", "config_path": str(config_path), "restarted": was_running}


@router.get("/opencode/session/{session_id}/cost")
async def opencode_session_cost(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get cumulative cost and token usage for an OpenCode session."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    async with httpx.AsyncClient(timeout=30) as client:
        msgs_resp = await client.get(f"http://127.0.0.1:{port}/session/{session_id}/message")
        if msgs_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch messages")

        messages = msgs_resp.json()
        total_cost = 0.0
        total_input = 0
        total_output = 0
        total_reasoning = 0
        total_cache = 0

        for msg in messages:
            info = msg.get("info", {})
            cost = info.get("cost")
            tokens = info.get("tokens", {})
            if cost is not None:
                total_cost += float(cost)
            if tokens:
                total_input += tokens.get("input", 0) or 0
                total_output += tokens.get("output", 0) or 0
                total_reasoning += tokens.get("reasoning", 0) or 0
                total_cache += tokens.get("cache", 0) or 0

        return {
            "session_id": session_id,
            "total_cost": total_cost,
            "tokens": {
                "input": total_input,
                "output": total_output,
                "reasoning": total_reasoning,
                "cache": total_cache,
            },
            "message_count": len(messages),
        }


@router.post("/opencode/session/{session_id}/permissions/{permission_id}")
async def opencode_permission_respond(
    session_id: str,
    permission_id: str,
    body: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Respond to a permission request from OpenCode (allow once/always/reject)."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    response_val = body.get("response", "once")
    if response_val not in ("once", "always", "reject"):
        raise HTTPException(status_code=400, detail="response must be 'once', 'always', or 'reject'")

    logger.info("[OC] permission_respond", session_id=session_id, permission_id=permission_id, response=response_val)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/session/{session_id}/permissions/{permission_id}",
            json={"response": response_val},
        )
        if resp.status_code != 200:
            logger.error("[OC] permission_respond failed", status=resp.status_code, body=resp.text[:200])
            raise HTTPException(status_code=502, detail=f"Permission respond failed: {resp.status_code}")
        return {"ok": True}


@router.get("/opencode/questions")
async def opencode_list_questions(
    current_user: CurrentUser = Depends(get_current_user),
):
    """List the user's pending question requests. Used by the frontend to
    rehydrate question state on page reload (the SSE `question.asked` event
    fired before the page loaded won't be re-played).
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        return []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/question")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
    except Exception as exc:
        logger.warning("[OC] list_questions failed", error=str(exc))
    return []


@router.post("/opencode/question/{request_id}/reply")
async def opencode_question_reply(
    request_id: str,
    body: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Submit answers for a pending OpenCode `question` tool request. Body:
    `{answers: [["label", ...], ...]}` — one inner array per question (label
    of the chosen option(s), or the literal text typed into the custom field).
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    answers = body.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=400, detail="answers must be a list")

    logger.info("[OC] question_reply", request_id=request_id, count=len(answers))
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/question/{request_id}/reply",
            json={"answers": answers},
        )
        if resp.status_code != 200:
            logger.error("[OC] question_reply failed", status=resp.status_code, body=resp.text[:200])
            raise HTTPException(status_code=502, detail=f"Question reply failed: {resp.status_code}")
        return {"ok": True}


@router.post("/opencode/question/{request_id}/reject")
async def opencode_question_reject(
    request_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Reject a pending OpenCode question (user dismisses without answering).
    OC's tool side will raise QuestionRejectedError, which surfaces as a tool
    error and the agent will see "The user dismissed this question".
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    logger.info("[OC] question_reject", request_id=request_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"http://127.0.0.1:{port}/question/{request_id}/reject")
        if resp.status_code != 200:
            logger.error("[OC] question_reject failed", status=resp.status_code, body=resp.text[:200])
            raise HTTPException(status_code=502, detail=f"Question reject failed: {resp.status_code}")
        return {"ok": True}


@router.get("/opencode/mcp")
async def opencode_list_mcp(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Snapshot of the user's MCP servers — keyed by server name, value is
    `{status: "connected"|"failed"|"disabled"|"connecting", ...}`. Used by
    the Settings page to show per-server status + offer a Reconnect button
    when a server is in an error state.
    """
    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        return {}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/mcp")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
    except Exception as exc:
        logger.warning("[OC] list_mcp failed", error=str(exc))
    return {}


@router.post("/opencode/mcp/{server_name}/connect")
async def opencode_mcp_connect(
    server_name: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Tell OC to (re)connect a specific MCP server. Equivalent of restarting
    the stdio child process for `local` servers, or re-establishing the HTTP
    handshake for `remote` ones. Returns `{ok: true}` on 200, raises a 502
    on upstream failure so the UI can show an error toast.
    """
    import httpx
    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"http://127.0.0.1:{port}/mcp/{server_name}/connect")
        if resp.status_code != 200:
            logger.error("[OC] mcp_connect failed", server=server_name, status=resp.status_code, body=resp.text[:200])
            raise HTTPException(status_code=502, detail=f"MCP connect failed: {resp.status_code}")
    return {"ok": True}


@router.post("/opencode/mcp/{server_name}/disconnect")
async def opencode_mcp_disconnect(
    server_name: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Disconnect a specific MCP server. Mostly useful for the user to
    temporarily disable an MCP without editing config — the next page
    reload or sidecar restart restores it from `opencode.json`.
    """
    import httpx
    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"http://127.0.0.1:{port}/mcp/{server_name}/disconnect")
        if resp.status_code != 200:
            logger.error("[OC] mcp_disconnect failed", server=server_name, status=resp.status_code, body=resp.text[:200])
            raise HTTPException(status_code=502, detail=f"MCP disconnect failed: {resp.status_code}")
    return {"ok": True}


@router.get("/opencode/config")
async def opencode_get_config(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Read and return the current OpenCode config file for the user."""
    proc = opencode_manager.get_process(current_user.user_id)
    config_dir = proc.config_dir if proc else _opencode_projects_dir() / current_user.user_id
    config_path = config_dir / "opencode.json"
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="No OpenCode config found")
    return json.loads(config_path.read_text())


@router.get("/opencode/session/{session_id}/children")
async def opencode_session_children(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """List sessions forked from this one. Used by the frontend's `Forks`
    panel to surface the fork tree. Falls back to an empty list when OC
    reports no children or the upstream call fails — we treat "no forks"
    and "OC unavailable" the same way from the UI's perspective.

    Each child shape mirrors OC's session info: `{id, slug, title, time,
    parentID, model, agent}`. We also enrich with `conversation_id` where
    we have a ReTrace conversation row linked to the upstream session id,
    so the UI can navigate directly into the forked conversation.

    KNOWN LIMITATION (OC v1.14.x): OC's `fork` endpoint creates a new
    session but does NOT set `parent_id` on the child. So `/children`
    currently returns an empty list for every parent. We keep the proxy
    wired because OC may add parent tracking in a future release, and the
    frontend already handles the empty case gracefully. If you need fork
    discovery today, look at ReTrace's `conversations` table for any rows
    whose `title` matches the `(fork #N)` suffix.
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        return []

    children: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/session/{session_id}/children")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    children = data
    except Exception as exc:
        logger.warning("[OC] session_children failed", error=str(exc), session_id=session_id)
        return []

    # Enrich each child with the ReTrace conversation_id (if linked) so the
    # UI can navigate without a separate roundtrip per child.
    if children:
        try:
            child_ids = [c.get("id") for c in children if isinstance(c, dict) and c.get("id")]
            async with async_session_maker() as db:
                result = await db.execute(
                    select(Conversation).where(Conversation.opencode_session_id.in_(child_ids))
                )
                rows = result.scalars().all()
                cid_by_sid = {r.opencode_session_id: r.conversation_id for r in rows}
            for c in children:
                if isinstance(c, dict):
                    c["conversation_id"] = cid_by_sid.get(c.get("id"))
        except Exception as exc:
            logger.warning("[OC] session_children enrichment failed", error=str(exc))

    return children


@router.post("/opencode/session/{session_id}/fork")
async def opencode_fork_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    body: Optional[dict] = None,
):
    """Fork an OpenCode session and create a corresponding ReTrace conversation.

    Returns the forked OC session info plus the new `conversation_id` and
    `team_id`. The frontend uses these to switch to the forked conversation —
    without them, the new conversation row would be created in the DB but
    invisible (no way for the UI to address it).
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    logger.info(
        "[OC] fork: forwarding to OC",
        session_id=session_id,
        port=port,
        body_keys=list((body or {}).keys()),
        user_id=current_user.user_id,
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/session/{session_id}/fork",
                json=body or {},
            )
    except Exception as exc:
        # ConnectError / ReadTimeout / etc. — surface the real reason instead
        # of silently 502-ing with no log.
        logger.error("[OC] fork: transport failure", error=repr(exc), session_id=session_id, port=port)
        raise HTTPException(status_code=502, detail=f"Fork: transport error: {exc}")

    if resp.status_code not in (200, 201):
        logger.error(
            "[OC] fork: upstream non-2xx",
            status=resp.status_code,
            body=resp.text[:300],
            session_id=session_id,
            port=port,
        )
        raise HTTPException(status_code=502, detail=f"Fork failed: {resp.status_code} {resp.text[:200]}")

    try:
        forked = resp.json()
    except Exception as exc:
        logger.error("[OC] fork: bad JSON response", error=repr(exc), body=resp.text[:200])
        raise HTTPException(status_code=502, detail="Fork: invalid response from OpenCode")

    forked_id = forked.get("id", "")

    # Inherit the parent conversation's team_id and use a recognizable title so
    # the fork is identifiable in the conversation list.
    parent_team_id: Optional[str] = None
    parent_title: str = "conversation"
    try:
        async with async_session_maker() as db:
            result = await db.execute(
                select(Conversation).where(Conversation.opencode_session_id == session_id)
            )
            parent = result.scalar_one_or_none()
            if parent:
                parent_team_id = parent.team_id
                parent_title = parent.title or parent_title
    except Exception as exc:
        logger.warning("[OC] fork: parent lookup failed", error=str(exc))

    new_convo = await get_or_create_conversation(
        opencode_session_id=forked_id,
        user_id=current_user.user_id,
        team_id=parent_team_id,
        title=f"Fork: {parent_title}"[:255],
    )

    return {
        **forked,
        "conversation_id": new_convo.conversation_id,
        "team_id": parent_team_id,
    }


@router.post("/opencode/session/{session_id}/compact")
async def opencode_compact_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Compact (summarize) an OpenCode session's conversation history."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/session/{session_id}/compact",
            json={},
        )
        if resp.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail=f"Compact failed: {resp.status_code}")
        if resp.status_code == 204:
            return {"status": "compacted"}
        return resp.json()


@router.post("/opencode/session/{session_id}/share")
async def opencode_share_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Generate a public share URL for an OpenCode session.

    Used by the `/share` slash command. Returns whatever OC returns — typically
    `{url: "https://opencode.ai/s/..."}` plus session metadata.
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    logger.info("[OC] share", session_id=session_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"http://127.0.0.1:{port}/session/{session_id}/share", json={})
        if resp.status_code not in (200, 201):
            logger.error("[OC] share failed", status=resp.status_code, body=resp.text[:200])
            raise HTTPException(status_code=502, detail=f"Share failed: {resp.status_code}")
        return resp.json()


@router.delete("/opencode/session/{session_id}/share")
async def opencode_unshare_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Revoke a previously-issued share URL."""
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    logger.info("[OC] unshare", session_id=session_id)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.delete(f"http://127.0.0.1:{port}/session/{session_id}/share")
        if resp.status_code in (200, 204, 404):
            return {"unshared": True}
        raise HTTPException(status_code=502, detail=f"Unshare failed: {resp.status_code}")


@router.post("/opencode/session/{session_id}/summarize")
async def opencode_summarize_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Trigger OpenCode's session-level summarize.

    Different from compact (which trims context for ongoing use). Summarize
    produces a digest of the whole conversation suitable for displaying as a
    header banner or sharing.
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    logger.info("[OC] summarize", session_id=session_id)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"http://127.0.0.1:{port}/session/{session_id}/summarize", json={})
        if resp.status_code not in (200, 201, 204):
            raise HTTPException(status_code=502, detail=f"Summarize failed: {resp.status_code}")
        if resp.status_code == 204:
            return {"status": "summarized"}
        return resp.json()


@router.get("/opencode/commands")
async def opencode_list_commands(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return OC's markdown slash-command catalog, lightly enriched.

    Pure proxy of OC's `/command` HTTP endpoint — those are the .md files
    under `.opencode/command/` (e.g. `/commit`, `/changelog`, `/spellcheck`).
    Each becomes a slash command that, when selected, gets dropped into the
    input as `/name args` and submitted as a normal user turn — OC's
    session-prompt handler then expands the markdown template into the
    full prompt.

    ReTrace does NOT contribute its own slash commands. Earlier iterations
    hardcoded "session-action" entries (revert/share/compact/etc.) and
    "retrace-action" entries (learn/automation/todos/help), but every one
    of those duplicated an existing UI button — Undo/Redo are in the chat
    input, share/fork/summarize/compact are in the header overflow menu,
    learn/automation/documentation are inline buttons on each assistant
    message, todos is a workspace tab. The slash variants added noise
    without functionality. The "retrace-action" ones were strictly worse
    UX — they showed a toast saying "click the button instead" rather
    than executing the action.
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    commands: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"http://127.0.0.1:{port}/command")
            if resp.status_code == 200:
                raw = resp.json() or []
                if isinstance(raw, list):
                    for c in raw:
                        if not isinstance(c, dict):
                            continue
                        name = c.get("name")
                        if not isinstance(name, str) or not name:
                            continue
                        commands.append({
                            "name": name,
                            "description": c.get("description") or "",
                        })
        except Exception as exc:
            logger.warning("[OC] list_commands failed", error=str(exc))

    return commands


@router.post("/opencode/session/{session_id}/abort")
async def opencode_abort_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Tell OpenCode to abort any in-flight prompt for this session.

    Stops upstream AI processing immediately so further tokens don't accrue.
    Also signals any in-flight SSE relay we have for this session so it stops
    streaming events to the client.
    """
    import httpx

    port = opencode_manager.get_port(current_user.user_id)
    if not port:
        raise HTTPException(status_code=502, detail="OpenCode server not running")

    # Signal local SSE relay (if any) so it stops re-broadcasting upstream events.
    await _cancel_active_stream(current_user.user_id, session_id)

    logger.info("[OC] abort", session_id=session_id, user_id=current_user.user_id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"http://127.0.0.1:{port}/session/{session_id}/abort")
            logger.info("[OC] abort upstream", status=resp.status_code)
            if resp.status_code == 404:
                # Session no longer exists upstream — treat as already-aborted.
                return {"aborted": False, "reason": "session_not_found"}
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Abort failed: {resp.status_code}")
            return {"aborted": bool(resp.json())}
    except httpx.HTTPError as exc:
        logger.error("[OC] abort transport error", error=str(exc))
        raise HTTPException(status_code=502, detail=f"Abort transport error: {exc}")


# ---------------------------------------------------------------------------
# Agent sessions (execution history)
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_agent_sessions(
    team_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List past agent sessions, newest first."""
    tenant_products = await get_tenant_team_ids(current_user, session)
    q = select(AgentSession).order_by(AgentSession.created_at.desc()).limit(limit)
    q = q.where(AgentSession.team_id.in_(tenant_products))
    if team_id:
        q = q.where(AgentSession.team_id == team_id)

    result = await session.execute(q)
    sessions = result.scalars().all()
    return [s.to_dict() for s in sessions]


@router.get("/sessions/{session_id}")
async def get_agent_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get a specific agent session."""
    result = await session.execute(
        select(AgentSession).where(AgentSession.session_id == session_id)
    )
    agent_session = result.scalar_one_or_none()
    if not agent_session:
        raise HTTPException(status_code=404, detail="Session not found")
    return agent_session.to_dict()


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

@router.get("/tools")
async def list_agent_tools(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List available agent tools and their status (OpenCode tools + MCP servers)."""
    import httpx as _httpx
    from app.api.settings import get_active_llm_settings

    # Toolbar toggles write to `settings.agent_tools_config = {"disabled": [...]}`.
    # Treat that list as authoritative for what the chip color should reflect.
    llm = await get_active_llm_settings(session)
    cfg = llm.get("agent_tools_config")
    disabled_set: set[str] = set()
    if isinstance(cfg, dict):
        raw = cfg.get("disabled") or []
        if isinstance(raw, list):
            disabled_set = {str(n) for n in raw}

    FILE_OPS = "File Operations"
    SEARCH = "Search"
    CORE = "Core"
    MCP = "MCP Extensions"

    tools = [
        # `bash` is OC's forked shell tool — routes through retrace's PTY
        # bridge (see opencode/.../tool/shell.ts). Same shell process as the
        # Terminal tab, env/CWD persist both directions.
        AgentToolInfo(name="bash", description="Execute shell commands in the conversation's terminal (shared with the Terminal tab)", available=True, group=CORE),
        AgentToolInfo(name="read", description="Read file contents from disk", available=True, group=FILE_OPS),
        AgentToolInfo(name="edit", description="Edit files using string replacement", available=True, group=FILE_OPS),
        AgentToolInfo(name="write", description="Create or overwrite files", available=True, group=FILE_OPS),
        AgentToolInfo(name="multiEdit", description="Edit multiple locations in a file at once", available=True, group=FILE_OPS),
        AgentToolInfo(name="glob", description="Find files matching a glob pattern", available=True, group=SEARCH),
        AgentToolInfo(name="grep", description="Search file contents using regex (ripgrep)", available=True, group=SEARCH),
        AgentToolInfo(name="web_fetch", description="Fetch URL content as clean readable text", available=True, group=SEARCH),
        AgentToolInfo(name="todo_write", description="Plan and track multi-step tasks", available=True, group=CORE),
        AgentToolInfo(name="task", description="Launch sub-agents (explore/general) for research", available=True, group=CORE),
    ]

    # Check MCP server status from OpenCode
    port = opencode_manager.get_port(current_user.user_id)
    mcp_connected: set[str] = set()
    if port:
        try:
            async with _httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://127.0.0.1:{port}/mcp")
                if resp.status_code == 200:
                    mcp_data = resp.json()
                    # `disabled` is a user-chosen state from the chip toggle —
                    # treat it as "available to toggle" so the user can flip it
                    # back on. Only genuine failures (failed/error) leave the
                    # chip un-clickable.
                    mcp_connected = {
                        k for k, v in mcp_data.items()
                        if isinstance(v, dict) and v.get("status") in ("connected", "disabled")
                    }
        except Exception:
            pass

    mcp_tools = [
        AgentToolInfo(name="retrace-kb", description="Team knowledge base: search, list, read, browse", available="retrace-kb" in mcp_connected, group=MCP),
        AgentToolInfo(name="retrace-browser", description="Browser automation: navigate, click, type, read pages", available="retrace-browser" in mcp_connected, group=MCP),
        AgentToolInfo(name="retrace-screenops", description="Screen-based computer automation (click, type, press)", available="retrace-screenops" in mcp_connected, group=MCP),
    ]

    # Pre-OC the web tools were three independent buttons in the toolbar
    # (not a single "search" group). Keep that layout — list them flat with
    # their original retrace names and route availability through the
    # retrace-search MCP connection status.
    search_available = "retrace-search" in mcp_connected
    web_tools = [
        AgentToolInfo(name="web_search", description="Web search (Serper / DuckDuckGo)", available=search_available, group=SEARCH),
        AgentToolInfo(name="web_research", description="Multi-source web research with LLM ranking", available=search_available, group=SEARCH),
        AgentToolInfo(name="web_advanced", description="Advanced web search with operators (site:, filetype:, before:, after:, ...)", available=search_available, group=SEARCH),
    ]

    all_tools = tools + web_tools + mcp_tools
    for t in all_tools:
        # `available` reflects whether the tool's MCP is connected (or, for
        # OC native tools, always true). `enabled` is what the chip color
        # binds to — chip is dim when the tool is unreachable OR the user
        # has toggled it off. `disabled_set` comes from the toolbar's PATCH
        # /settings/agent-tools, persisted in settings.agent_tools_config.
        t.enabled = t.available and t.name not in disabled_set

    return [t.model_dump() for t in all_tools]


# ---------------------------------------------------------------------------
# MCP Extension warmup
# ---------------------------------------------------------------------------

@router.post("/warmup-mcp")
async def warmup_mcp(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Pre-start MCP extensions so they're ready when the user sends a task.

    Fires a background task and returns immediately.
    The warm-up is best-effort — failures are logged but don't affect the user.
    """
    user_auth_context = {
        "user_id": current_user.user_id,
        "email": current_user.email,
        "tenant_id": current_user.tenant_id,
        "role": current_user.role,
    }
    asyncio.create_task(_warmup_mcp_background(user_auth_context))
    return {"status": "warming_up"}


async def _warmup_mcp_background(user_auth_context: dict) -> None:
    """Background task to pre-start MCP extensions for a user."""
    user_id = user_auth_context.get("user_id", "")
    try:
        from sqlalchemy import select as sa_select
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension
        from app.models.user_session import UserSession
        from app.services.mcp_client_service import mcp_client_manager

        async with async_session_maker() as session:
            result = await session.execute(
                sa_select(UserExtension, McpExtension)
                .join(McpExtension, UserExtension.extension_id == McpExtension.extension_id)
                .where(UserExtension.user_id == user_id)
                .where(UserExtension.is_enabled == True)
                .where(McpExtension.is_enabled == True)
            )
            rows = result.all()
            if not rows:
                return

            # Get user's external auth token (for oauth_passthrough extensions)
            user_auth_token = None
            sess_result = await session.execute(
                sa_select(UserSession)
                .where(UserSession.user_id == user_id)
                .where(UserSession.revoked == False)
                .order_by(UserSession.last_active_at.desc())
                .limit(1)
            )
            user_session = sess_result.scalar_one_or_none()
            if user_session:
                user_auth_token = user_session.external_id_token or user_session.external_access_token

            # Load LDAP credentials for ldap_passthrough extensions
            user_auth_credentials = None
            if user_session and user_session.external_username and user_session.external_password:
                from app.core.encryption import decrypt_value
                user_auth_credentials = {
                    "username": user_session.external_username,
                    "password": decrypt_value(user_session.external_password),
                }

            for user_ext, catalog_ext in rows:
                try:
                    merged_env = user_ext.get_merged_env_vars(catalog_ext.get_raw_env_vars())
                    merged_auth = {**(catalog_ext.auth_config_json or {}), **(user_ext.auth_config_json or {})}
                    await mcp_client_manager.start_extension(
                        extension_id=catalog_ext.extension_id,
                        extension_name=catalog_ext.name,
                        command=catalog_ext.command,
                        args=catalog_ext.args_json,
                        env_vars=merged_env,
                        auth_type=catalog_ext.auth_type,
                        auth_config=merged_auth,
                        user_auth_token=user_auth_token or "",
                        user_auth_credentials=user_auth_credentials,
                        user_id=user_id,
                        tenant_id=user_auth_context.get("tenant_id", ""),
                    )
                except Exception as e:
                    logger.warning("mcp_warmup_extension_failed", extension=catalog_ext.name, error=str(e))

        logger.info("mcp_warmup_complete", user_id=user_id, extensions=len(rows))
    except Exception as e:
        logger.warning("mcp_warmup_failed", error=str(e), user_id=user_id)


# ---------------------------------------------------------------------------
# Conversations — persistent per-team chat threads
# ---------------------------------------------------------------------------

@router.get("/conversations")
async def list_conversations(
    team_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List conversations for the current user, optionally filtered by team.

    Defense-in-depth: the result set is always scoped by user_id (a user can
    only see their own conversations), but the team_id filter is also validated
    against the user's tenant. A user passing a team_id from a different tenant
    gets an empty result, not a "team not found" error — minimizes information
    leakage.
    """
    q = (
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.user_id == current_user.user_id)
        .order_by(Conversation.updated_at.desc())
    )
    if team_id == "__none__":
        q = q.where(Conversation.team_id.is_(None))
    elif team_id:
        tenant_team_ids = await get_tenant_team_ids(current_user, session)
        if team_id not in tenant_team_ids:
            return []
        q = q.where(Conversation.team_id == team_id)
    result = await session.execute(q)
    convos = result.scalars().all()

    # Opportunistic title refresh: any conversation still on the "New
    # conversation" default that has a linked OC session can have its title
    # upgraded from OpenCode's session.title (set by ensureTitle on the first
    # turn). Fires fire-and-forget tasks so this listing call returns
    # immediately; the next listing call (React Query refetch) picks up the
    # upgraded title. Capped at 5 per call to avoid scheduling bursts.
    try:
        from app.services.session_bridge import schedule_deferred_title_sync
        refreshed = 0
        for convo in convos:
            if refreshed >= 5:
                break
            if convo.title != "New conversation":
                continue
            if not convo.opencode_session_id:
                continue
            schedule_deferred_title_sync(
                opencode_session_id=convo.opencode_session_id,
                user_id=convo.user_id,
                conversation_id=convo.conversation_id,
                expected_auto=None,
            )
            refreshed += 1
    except Exception:
        # Title refresh is best-effort — never disrupt the listing response.
        pass

    return [c.to_dict(include_messages=False) for c in convos]


@router.post("/conversations")
async def create_conversation(
    data: ConversationCreateRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a new conversation for a team, owned by the current user."""
    convo = Conversation(
        conversation_id=str(uuid4()),
        team_id=data.team_id,
        user_id=current_user.user_id,
        title=data.title,
    )
    session.add(convo)
    await session.flush()

    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == convo.conversation_id)
    )
    convo = result.scalar_one()
    return convo.to_dict(include_messages=True)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get a conversation with all its messages."""
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == conversation_id)
        .where(Conversation.user_id == current_user.user_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Opportunistic title refresh — same as list_conversations. When a user
    # opens a conversation stuck on "New conversation", schedule a background
    # check against OpenCode's session.title; the next list refetch picks up
    # the upgraded title.
    if convo.title == "New conversation" and convo.opencode_session_id:
        try:
            from app.services.session_bridge import schedule_deferred_title_sync
            schedule_deferred_title_sync(
                opencode_session_id=convo.opencode_session_id,
                user_id=convo.user_id,
                conversation_id=convo.conversation_id,
                expected_auto=None,
            )
        except Exception:
            pass

    return convo.to_dict(include_messages=True)


@router.put("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: str,
    data: ConversationRenameRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Rename a conversation."""
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == conversation_id)
        .where(Conversation.user_id == current_user.user_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    convo.title = data.title
    convo.updated_at = datetime.utcnow()
    await session.flush()
    return convo.to_dict(include_messages=False)


async def _delete_opencode_session(user_id: str, opencode_session_id: Optional[str]) -> None:
    """Best-effort cleanup of an OpenCode session for a user.

    Used when a ReTrace conversation is deleted — without this, the upstream
    OC sidecar accumulates dead session directories on disk (and in memory)
    forever. Failures are logged but never raised: deleting the DB row is the
    authoritative action, this is just to reclaim sidecar resources.
    """
    if not opencode_session_id:
        return
    port = opencode_manager.get_port(user_id)
    if not port:
        return
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.delete(f"http://127.0.0.1:{port}/session/{opencode_session_id}")
            if resp.status_code not in (200, 204, 404):
                logger.warning("[OC] delete_session non-2xx", status=resp.status_code, oc_session_id=opencode_session_id)
    except Exception as exc:
        logger.warning("[OC] delete_session transport error", error=str(exc), oc_session_id=opencode_session_id)


async def _cleanup_conversation_browser(conversation_id: str) -> None:
    """Close browser context and remove per-conversation workspace directory."""
    import asyncio as _asyncio

    # 1. Close the BrowserManager session (stops screenshot loop, notifies WS clients)
    try:
        from app.services.browser_manager import browser_manager
        await browser_manager.close_session(conversation_id)
    except Exception:
        pass

    # 2. Tell the daemon to close the browser context + remove workspace dir inside container
    try:
        from app.services.sandbox_manager import get_session_for_conversation
        sandbox = get_session_for_conversation(conversation_id)
        if sandbox:
            import json
            # Close browser context
            payload = json.dumps({"action": "close_context", "conversation_id": conversation_id})
            escaped = payload.replace("'", "'\\''")
            await _asyncio.to_thread(
                sandbox.execute_command,
                f"python3 /opt/browser_control.py '{escaped}'"
            )
            # Remove per-conversation workspace directory inside container
            # (also removes from host since it's a bind mount)
            await _asyncio.to_thread(
                sandbox.execute_command,
                f"rm -rf '/workspace/{conversation_id}'"
            )
    except Exception:
        pass


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete a conversation, its messages, and its terminal session."""
    query = select(Conversation).where(Conversation.conversation_id == conversation_id)
    # Users can only delete their own conversations
    if current_user.role != "zero_admin":
        query = query.where(Conversation.user_id == current_user.user_id)

    result = await session.execute(query)
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Capture upstream OC session id BEFORE the row is deleted.
    oc_session_id = convo.opencode_session_id

    # Kill the PTY process if one is running for this conversation
    from app.services.pty_manager import pty_manager
    pty_manager.destroy(conversation_id)

    # Clean up browser context and per-conversation workspace
    await _cleanup_conversation_browser(conversation_id)

    await session.delete(convo)

    # Best-effort: tell OpenCode to drop the session too.
    await _delete_opencode_session(current_user.user_id, oc_session_id)

    return {"status": "deleted", "conversation_id": conversation_id}


@router.delete("/conversations")
async def delete_all_conversations(
    team_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete all conversations for the current user, optionally filtered by team."""
    from app.services.pty_manager import pty_manager

    q = select(Conversation).where(Conversation.user_id == current_user.user_id)
    if team_id == "__none__":
        q = q.where(Conversation.team_id.is_(None))
    elif team_id:
        q = q.where(Conversation.team_id == team_id)

    result = await session.execute(q)
    convos = result.scalars().all()
    count = len(convos)

    # Snapshot OC session ids before the DB rows go away.
    oc_session_ids = [c.opencode_session_id for c in convos]

    for convo in convos:
        pty_manager.destroy(convo.conversation_id)
        await _cleanup_conversation_browser(convo.conversation_id)
        await session.delete(convo)

    # Best-effort: tell OpenCode to drop each session. Done after the DB
    # commit so a transport error here doesn't block the user-visible delete.
    for oc_id in oc_session_ids:
        await _delete_opencode_session(current_user.user_id, oc_id)

    return {"status": "deleted", "count": count}


@router.get("/conversations/{conversation_id}/terminal")
async def get_terminal_status(
    conversation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Check whether a PTY session is active for a conversation."""
    from app.services.pty_manager import pty_manager
    session = pty_manager.get(conversation_id)
    return {
        "active": session is not None,
        "conversation_id": conversation_id,
    }


@router.post("/conversations/{conversation_id}/messages")
async def save_messages(
    conversation_id: str,
    data: MessageSaveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Bulk save messages to a conversation (appends to existing messages)."""
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.conversation_id == conversation_id)
        .where(Conversation.user_id == current_user.user_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")

    max_pos = max((m.position for m in convo.messages), default=-1)

    for i, msg in enumerate(data.messages):
        convo.messages.append(ConversationMessage(
            message_id=str(uuid4()),
            conversation_id=conversation_id,
            msg_type=msg.get("type", "user"),
            content=msg.get("content", ""),
            iteration=msg.get("iteration"),
            meta_json=msg.get("meta"),
            position=max_pos + 1 + i,
        ))

    convo.updated_at = datetime.utcnow()

    if convo.title == "New conversation":
        first_user = next((m for m in data.messages if m.get("type") == "user"), None)
        if first_user:
            title = first_user["content"][:80]
            if len(first_user["content"]) > 80:
                title += "..."
            convo.title = title

    await session.flush()
    return convo.to_dict(include_messages=True)


@router.delete("/conversations/{conversation_id}/messages")
async def clear_messages(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Clear all messages from a conversation (keeps the conversation)."""
    result = await session.execute(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .where(Conversation.user_id == current_user.user_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await session.execute(
        delete(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation_id
        )
    )
    return {"status": "cleared", "conversation_id": conversation_id}


# ---------------------------------------------------------------------------
# Learn This — train Q&A pairs as prioritized knowledge
# ---------------------------------------------------------------------------

@router.post("/learn")
async def learn_qa(
    data: LearnThisRequest,
    session: AsyncSession = Depends(get_session),
):
    """Store a Q&A pair as knowledge in the team's knowledge base."""
    from app.rag.kb_store import KnowledgeBaseStore

    if not data.team_id:
        raise HTTPException(status_code=400, detail="team_id is required")

    kb_store = KnowledgeBaseStore()
    result = await kb_store.store_mcp_docs(
        team_id=data.team_id,
        pages=[(data.question, data.answer)],
        api_name="learn-this",
    )
    return {"status": "learned", "chunk_count": result.get("chunk_count", 0)}


# ---------------------------------------------------------------------------
# SOPs — Standard Operating Procedures
# ---------------------------------------------------------------------------

_SOP_SYSTEM_PROMPT = """\
You are an expert at creating Standard Operating Procedures (SOPs).
You will receive a goal and the previous conversation context (user questions, agent code, tool outputs, agent answers).

Create a detailed, step-by-step SOP that an automated agent can follow to accomplish the goal.

## Available tools

Pick the right tool for each step. Web content goes through the BROWSER tools
(real Chromium + Playwright); plain shell commands and file operations go
through the other tools.

### Browser tools (preferred for ANY web content — use these, not `curl`)
The browser drives the same Chromium the chat agent uses. Real headers, JS
execution, cookies — works on sites that block `curl` (anti-bot, login walls,
JS-rendered SPAs).
- `navigate` — command = URL. Loads a page.
- `vision_click` — command = natural-language description of an element to
  click ("blue Sign in button"). Use this by default for clicking.
- `click` — command = CSS selector. Use only when vision_click would be
  ambiguous or you have a confirmed selector.
- `read_page` — extract the visible text of the page (optionally a CSS scope
  in `command`). Use this to capture article/news/page content as text.
- `take_screenshot` — capture a JPEG of the current page.
- `type_text` — command = text to type into the focused element.
- `type` — command = CSS selector; the text comes from the step's `text`
  field. Fills a form field.
- `press_key` — command = key name ("Enter", "Tab", "Escape").
- `scroll` — command = "up" / "down"; optional `amount` field (px).
- `back`, `forward`, `refresh` — browser navigation buttons.
- `wait` — command = number of seconds to pause.
- `get_url` — return current URL + title (no side effects).
- `evaluate_js` — run a JS expression in the page, return the result.

### File and shell tools
- `terminal` — command = shell command. Use for `mkdir`, `chmod`, `git`, etc.
  Do NOT use `curl`/`wget` for fetching web pages — use `navigate` + `read_page`.
- `read_file` — command = file path. Returns file contents.
- `write_file` — command = file path. The content to write comes from the
  step's `text` or `content` field, OR is auto-injected from previous steps'
  outputs if you omit both.
- `delete_file` — command = file path.
- `download_file` — command = URL; the step's `text` field holds the target
  path. Use ONLY for binary files (zip, PDF, installer) where you don't
  need browser-style HTML rendering.

### Search and knowledge
- `search_knowledge_base` — command = search query. Searches the team's
  ingested documents.
- `web_search` — command = search query. Basic web search (Serper/DuckDuckGo).
- `web_research` — command = research question. Multi-source search with
  LLM-driven result selection.
- `web_advanced` — command = search query. Advanced operators (site:,
  filetype:, before:, after:).
- `web_fetch` — command = URL. Returns clean readable text. ONLY use for
  simple text-only pages without anti-bot protection; otherwise prefer
  `navigate` + `read_page`.

## Rules

- Each step must specify which TOOL to use from the lists above.
- Each step must have an exact command, path, or input (not vague instructions).
- Steps must be in the correct execution order.
- For fetching web content, default to `navigate` + `read_page`. Use
  `terminal: curl` only when the user explicitly asks for it.
- For clicking on a page, default to `vision_click` with a clear visual
  description. Reserve `click` for cases where the selector is known.
- Include prerequisites (what must be true before starting).
- Include expected outcome (what success looks like).

## Response format

You MUST respond with valid JSON in this exact format:
{
  "title": "Short descriptive title",
  "purpose": "1-2 sentence description of what this SOP accomplishes",
  "prerequisites": ["prerequisite 1", "prerequisite 2"],
  "steps": [
    {
      "number": 1,
      "action": "Short description of what this step does",
      "tool": "navigate",
      "command": "https://example.com",
      "expected_result": "Page loaded"
    }
  ],
  "expected_outcome": "What success looks like after all steps are completed"
}

Respond ONLY with the JSON object. No markdown, no explanation, no code fences.
"""


def _build_sop_context(messages: List[dict]) -> str:
    """Format conversation messages into a readable context string for the LLM."""
    parts: list[str] = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")
        if not content:
            continue
        if msg_type == "user":
            parts.append(f"USER: {content}")
        elif msg_type == "code":
            parts.append(f"AGENT CODE:\n```\n{content}\n```")
        elif msg_type == "output":
            parts.append(f"TOOL OUTPUT:\n{content[:2000]}")
        elif msg_type == "answer":
            parts.append(f"AGENT ANSWER:\n{content}")
    return "\n\n".join(parts)


def _sop_json_to_markdown(sop: dict) -> str:
    """Convert structured SOP JSON to a fixed-format Markdown string."""
    lines: list[str] = []
    lines.append(f"# {sop.get('title', 'Untitled SOP')}")
    lines.append("")
    lines.append("## Purpose")
    lines.append(sop.get("purpose", ""))
    lines.append("")

    prereqs = sop.get("prerequisites", [])
    if prereqs:
        lines.append("## Prerequisites")
        for p in prereqs:
            lines.append(f"- {p}")
        lines.append("")

    lines.append("## Steps")
    lines.append("")
    for step in sop.get("steps", []):
        lines.append(f"### Step {step.get('number', '?')}: {step.get('action', '')}")
        lines.append(f"- **Tool:** `{step.get('tool', 'N/A')}`")
        cmd = step.get("command", "")
        if cmd:
            lines.append(f"- **Command/Input:** `{cmd}`")
        expected = step.get("expected_result", "")
        if expected:
            lines.append(f"- **Expected Result:** {expected}")
        lines.append("")

    lines.append("## Expected Outcome")
    lines.append(sop.get("expected_outcome", ""))
    return "\n".join(lines)


@router.post("/create-sop")
async def create_sop(
    data: CreateSOPRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create an SOP from the current conversation context. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from app.models.sop import SOP

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("sop_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        context = _build_sop_context(data.messages)
        if not context.strip():
            yield _sse("sop_error", {"detail": "No usable conversation context found."})
            return

        user_prompt = (
            f"GOAL: {data.goal}\n\n"
            f"CONVERSATION CONTEXT:\n{context}"
        )

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        yield _sse("sop_status", {"message": "Generating Automation..."})

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SOP_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("sop_llm_call_failed", error=str(exc))
            yield _sse("sop_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            sop_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("sop_json_parse_failed", raw=raw[:500])
            yield _sse("sop_error", {"detail": "Failed to parse SOP from LLM response."})
            return

        sop_markdown = _sop_json_to_markdown(sop_data)

        # Stream the markdown word by word
        words = sop_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("sop_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("sop_chunk", {"chunk": sop_markdown, "done": True})

        # Store SOP
        sop_id = str(uuid4())
        sop = SOP(
            sop_id=sop_id,
            team_id=data.team_id,
            title=sop_data.get("title", data.goal[:100]),
            goal=data.goal,
            sop_json=sop_data,
            sop_markdown=sop_markdown,
            source_conversation_id=data.conversation_id,
        )
        session.add(sop)
        await session.flush()

        logger.info("sop_created", sop_id=sop_id, team_id=data.team_id, title=sop.title)
        yield _sse("sop_done", {"sop_id": sop_id, "title": sop_data.get("title", data.goal[:100]), "sop_markdown": sop_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/sops")
async def list_sops(
    team_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List all SOPs for a team, newest first. team_id=__none__ lists team-less (general) automations."""
    tenant_products = await get_tenant_team_ids(current_user, session)
    q = select(SOP)
    if team_id == "__none__":
        q = q.where(SOP.team_id.is_(None))
    elif team_id:
        q = q.where(SOP.team_id.in_(tenant_products))
        q = q.where(SOP.team_id == team_id)
    else:
        raise HTTPException(status_code=422, detail="team_id is required (use a team UUID or __none__ for general)")
    result = await session.execute(q.order_by(SOP.created_at.desc()))
    sops = result.scalars().all()
    return [s.to_dict() for s in sops]


@router.get("/sops/{sop_id}")
async def get_sop(
    sop_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get a specific SOP."""
    result = await session.execute(
        select(SOP).where(SOP.sop_id == sop_id)
    )
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="SOP not found")
    return sop.to_dict()


@router.delete("/sops/{sop_id}")
async def delete_sop(
    sop_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete an SOP."""
    result = await session.execute(
        select(SOP).where(SOP.sop_id == sop_id)
    )
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="SOP not found")
    await session.delete(sop)
    return {"status": "deleted", "sop_id": sop_id}


@router.put("/sops/{sop_id}/approve")
async def approve_sop(
    sop_id: str,
    data: ApproveAutomationRequest = ApproveAutomationRequest(),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Approve an automation with optional scheduling."""
    result = await session.execute(
        select(SOP).where(SOP.sop_id == sop_id)
    )
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="Automation not found")

    from datetime import datetime as dt, timedelta

    sop.status = "approved"
    sop.updated_at = dt.utcnow()
    sop.schedule_type = data.schedule_type if data.schedule_type != "none" else None
    sop.schedule_config = data.schedule_config

    if data.schedule_type != "none" and data.schedule_config:
        sop.is_active = True
        # Calculate next_run_at based on schedule
        sop.next_run_at = _calculate_next_run(data.schedule_type, data.schedule_config)
    else:
        sop.is_active = False
        sop.next_run_at = None

    await session.flush()

    # Add/remove scheduler job
    from app.services.scheduler_service import automation_scheduler
    if data.schedule_type != "none" and data.schedule_config:
        automation_scheduler.add_automation_job(sop_id, sop.team_id, data.schedule_type, data.schedule_config)
    else:
        automation_scheduler.remove_automation_job(sop_id)

    logger.info("sop_approved", sop_id=sop_id, schedule_type=data.schedule_type)
    return sop.to_dict()


def _calculate_next_run(schedule_type: str, config: dict) -> "datetime":
    """Calculate the next run time based on schedule configuration."""
    from datetime import datetime as dt, timedelta

    now = dt.utcnow()

    if schedule_type == "once":
        run_at = config.get("run_at")
        if run_at:
            return dt.fromisoformat(run_at.replace("Z", "+00:00").replace("+00:00", ""))
        return now + timedelta(minutes=5)

    elif schedule_type == "interval":
        every = config.get("every", 60)
        unit = config.get("unit", "minutes")
        if unit == "minutes":
            return now + timedelta(minutes=every)
        elif unit == "hours":
            return now + timedelta(hours=every)
        elif unit == "days":
            return now + timedelta(days=every)

    elif schedule_type == "daily":
        time_str = config.get("time", "09:00")
        h, m = map(int, time_str.split(":"))
        next_run = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run

    elif schedule_type == "weekly":
        day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
        days = config.get("days", ["monday"])
        time_str = config.get("time", "09:00")
        h, m = map(int, time_str.split(":"))
        target_days = sorted([day_map.get(d.lower(), 0) for d in days])
        for offset in range(1, 8):
            candidate = now + timedelta(days=offset)
            if candidate.weekday() in target_days:
                return candidate.replace(hour=h, minute=m, second=0, microsecond=0)

    elif schedule_type == "monthly":
        day_of_month = config.get("day_of_month", 1)
        time_str = config.get("time", "09:00")
        h, m = map(int, time_str.split(":"))
        next_run = now.replace(day=min(day_of_month, 28), hour=h, minute=m, second=0, microsecond=0)
        if next_run <= now:
            if now.month == 12:
                next_run = next_run.replace(year=now.year + 1, month=1)
            else:
                next_run = next_run.replace(month=now.month + 1)
        return next_run

    return now + timedelta(hours=1)



# ---------------------------------------------------------------------------
# Automation execution & scheduling endpoints
# ---------------------------------------------------------------------------

@router.post("/sops/{sop_id}/run")
async def manual_run_automation(
    sop_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Trigger a manual run of an automation."""
    from app.models.sop import SOP
    result = await session.execute(select(SOP).where(SOP.sop_id == sop_id))
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="Automation not found")
    if not sop.sop_json:
        raise HTTPException(status_code=400, detail="Automation has no steps to execute")

    from app.services.scheduler_service import automation_scheduler
    await automation_scheduler.run_now(sop_id, sop.team_id, user_id=current_user.user_id)
    return {"status": "started", "sop_id": sop_id}


@router.get("/sops/{sop_id}/runs")
async def list_automation_runs(
    sop_id: str,
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    """List execution history for an automation."""
    from app.models.automation_run import AutomationRun
    result = await session.execute(
        select(AutomationRun)
        .where(AutomationRun.sop_id == sop_id)
        .order_by(AutomationRun.started_at.desc())
        .limit(limit)
    )
    runs = result.scalars().all()
    return [r.to_dict() for r in runs]


@router.put("/sops/{sop_id}/schedule")
async def update_schedule(
    sop_id: str,
    data: ApproveAutomationRequest,
    session: AsyncSession = Depends(get_session),
):
    """Update the schedule for an approved automation."""
    from app.models.sop import SOP
    result = await session.execute(select(SOP).where(SOP.sop_id == sop_id))
    sop = result.scalar_one_or_none()
    if not sop:
        raise HTTPException(status_code=404, detail="Automation not found")

    from datetime import datetime as dt
    sop.schedule_type = data.schedule_type if data.schedule_type != "none" else None
    sop.schedule_config = data.schedule_config
    sop.updated_at = dt.utcnow()

    if data.schedule_type != "none" and data.schedule_config:
        sop.is_active = True
        sop.next_run_at = _calculate_next_run(data.schedule_type, data.schedule_config)
    else:
        sop.is_active = False
        sop.next_run_at = None

    await session.flush()

    from app.services.scheduler_service import automation_scheduler
    if data.schedule_type != "none" and data.schedule_config:
        automation_scheduler.add_automation_job(sop_id, sop.team_id, data.schedule_type, data.schedule_config)
    else:
        automation_scheduler.remove_automation_job(sop_id)

    return sop.to_dict()


_SOP_EDIT_SYSTEM_PROMPT = """\
You are an expert at editing Standard Operating Procedures (SOPs).
You will receive the current SOP as JSON and the user's requested edits.

Apply the requested changes to the SOP while preserving the existing structure and any steps that are not affected by the edits.

IMPORTANT RULES:
- Maintain the same JSON format as the original SOP.
- Renumber steps if steps are added or removed so they are sequential starting from 1.
- Each step must specify which TOOL to use. Available tools: terminal, read_file, write_file, web_search, web_research, web_advanced, screenops, search_knowledge_base.
- Each step must have an exact command, path, or input (not vague instructions).
- Keep prerequisites and expected_outcome up to date with the changes.

Respond ONLY with the complete updated JSON object. No markdown, no explanation, no code fences.
The JSON format:
{
  "title": "Short descriptive title",
  "purpose": "1-2 sentence description of what this SOP accomplishes",
  "prerequisites": ["prerequisite 1", "prerequisite 2"],
  "steps": [
    {
      "number": 1,
      "action": "Short description of what this step does",
      "tool": "terminal",
      "command": "exact command or path or input to use",
      "expected_result": "What should happen after this step"
    }
  ],
  "expected_outcome": "What success looks like after all steps are completed"
}
"""


@router.post("/edit-sop")
async def edit_sop(
    data: EditSOPRequest,
    session: AsyncSession = Depends(get_session),
):
    """Edit an existing SOP based on user instructions. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from datetime import datetime as dt

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        result = await session.execute(
            select(SOP).where(SOP.sop_id == data.sop_id)
        )
        sop = result.scalar_one_or_none()
        if not sop:
            yield _sse("sop_error", {"detail": "SOP not found"})
            return

        if not sop.sop_json:
            yield _sse("sop_error", {"detail": "SOP has no structured data to edit."})
            return

        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("sop_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        yield _sse("sop_status", {"message": "Editing Automation..."})

        user_prompt = (
            f"CURRENT SOP (JSON):\n{json_mod.dumps(sop.sop_json, indent=2)}\n\n"
            f"REQUESTED EDITS:\n{data.edit_instructions}"
        )

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SOP_EDIT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("sop_edit_llm_failed", error=str(exc))
            yield _sse("sop_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            updated_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("sop_edit_parse_failed", raw=raw[:500])
            yield _sse("sop_error", {"detail": "Failed to parse edited SOP from LLM response."})
            return

        updated_markdown = _sop_json_to_markdown(updated_data)

        # Stream the updated markdown word by word
        words = updated_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("sop_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("sop_chunk", {"chunk": updated_markdown, "done": True})

        sop.sop_json = updated_data
        sop.sop_markdown = updated_markdown
        sop.title = updated_data.get("title", sop.title)
        sop.status = "draft"
        sop.updated_at = dt.utcnow()
        await session.flush()

        logger.info("sop_edited", sop_id=data.sop_id, title=sop.title)
        yield _sse("sop_done", {"sop_id": data.sop_id, "title": sop.title, "sop_markdown": updated_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _extract_key_phrases(text: str) -> str:
    """Extract simple key phrases from a question for metadata tagging."""
    stop_words = {
        "what", "how", "why", "when", "where", "which", "who", "is", "are",
        "was", "were", "do", "does", "did", "can", "could", "would", "should",
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "with", "and",
        "or", "but", "not", "this", "that", "it", "be", "have", "has", "had",
        "will", "shall", "may", "might", "must", "about", "from", "by", "as",
        "if", "then", "than", "so", "also", "just", "only", "very", "too",
        "me", "my", "i", "you", "your", "we", "our", "they", "their",
    }
    words = text.lower().split()
    key_words = [w.strip("?.,!;:\"'()[]{}") for w in words if w.lower().strip("?.,!;:\"'()[]{}") not in stop_words]
    seen = set()
    unique = []
    for w in key_words:
        if w and w not in seen:
            seen.add(w)
            unique.append(w)
    return ", ".join(unique[:15])


# ---------------------------------------------------------------------------
# Documentation — Knowledge Articles from conversations
# ---------------------------------------------------------------------------

class CreateDocRequest(BaseModel):
    """Create a documentation article from current conversation context."""
    team_id: Optional[str] = Field(default=None)
    goal: str = Field(..., min_length=1)
    messages: List[dict] = Field(..., min_length=1)
    conversation_id: Optional[str] = None


class EditDocRequest(BaseModel):
    """Edit an existing documentation article with user-described changes."""
    doc_id: str = Field(..., min_length=1)
    edit_instructions: str = Field(..., min_length=1)


_DOC_SYSTEM_PROMPT = """\
You are an expert technical writer. You will receive:
1. A TOPIC (the user's original question).
2. The AGENT'S ANSWER — the final answer the agent gave to the user.

Your job: turn the agent's answer into a clean, polished Knowledge Article.

CRITICAL RULES:
- Document ONLY the substance of the agent's ANSWER. That is the content the user cares about.
- Do NOT mention tools, search engines, DuckDuckGo, log files, agent internals, or how the answer was produced.
- Do NOT reference the agent, the conversation, or "the system". Write as if this is original documentation.
- Focus purely on the SUBJECT MATTER — the actual information the user asked about.
- Preserve code snippets, commands, and technical details from the answer, but strip out any tool/search metadata.
- If the answer contains file paths or URLs that are relevant to the subject (not to agent logs), include them.

You MUST respond with valid JSON in this exact format:
{
  "title": "Clear descriptive title about the subject matter",
  "summary": "1-2 sentence abstract of the topic",
  "tags": ["tag1", "tag2", "tag3"],
  "sections": [
    {
      "heading": "Overview",
      "content": "Introduction to the topic..."
    },
    {
      "heading": "Details",
      "content": "In-depth explanation..."
    },
    {
      "heading": "Key Takeaways",
      "content": "Summary of important points..."
    }
  ],
  "references": ["only subject-relevant file paths or URLs from the answer"]
}

Guidelines:
- Always start with an "Overview" section.
- Add detail sections as needed based on the answer content.
- End with "Key Takeaways" summarizing the main points.
- Use markdown formatting (code blocks, lists, bold) within section content.
- Include 3-8 relevant tags about the SUBJECT, not about tools or agents.
- references should ONLY contain paths/URLs about the subject matter. Omit agent logs, tool configs, test files, etc.

Respond ONLY with the JSON object. No markdown, no explanation, no code fences.
"""


_DOC_EDIT_SYSTEM_PROMPT = """\
You are an expert technical writer editing a Knowledge Article.
You will receive the current article as JSON and the user's requested edits.

Apply the requested changes while preserving the existing structure and any sections that are not affected by the edits.

IMPORTANT RULES:
- Maintain the same JSON format as the original article.
- Keep all unaffected sections intact.
- Update tags if the content changes significantly.
- Update the summary if the overall topic shifts.
- Use markdown formatting within section content.

Respond ONLY with the complete updated JSON object. No markdown, no explanation, no code fences.
The JSON format:
{
  "title": "Clear descriptive title",
  "summary": "1-2 sentence abstract",
  "tags": ["tag1", "tag2"],
  "sections": [
    {"heading": "Section Title", "content": "Section content with markdown..."}
  ],
  "references": ["file paths or sources"]
}
"""


def _doc_json_to_markdown(doc: dict) -> str:
    """Convert structured documentation JSON to a Markdown string."""
    lines: list[str] = []
    lines.append(f"# {doc.get('title', 'Untitled Article')}")
    lines.append("")

    summary = doc.get("summary", "")
    if summary:
        lines.append(f"*{summary}*")
        lines.append("")

    tags = doc.get("tags", [])
    if tags:
        lines.append(" ".join(f"`{t}`" for t in tags))
        lines.append("")

    for section in doc.get("sections", []):
        lines.append(f"## {section.get('heading', 'Section')}")
        lines.append("")
        lines.append(section.get("content", ""))
        lines.append("")

    refs = doc.get("references", [])
    if refs:
        lines.append("## References")
        lines.append("")
        for r in refs:
            lines.append(f"- `{r}`")
        lines.append("")

    return "\n".join(lines)


def _build_doc_context(messages: list[dict]) -> str:
    """Extract only user questions and agent answers — no code, tool output, or internals."""
    parts: list[str] = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")
        if not content:
            continue
        if msg_type == "user":
            parts.append(f"QUESTION: {content}")
        elif msg_type == "answer":
            parts.append(f"ANSWER:\n{content}")
    return "\n\n".join(parts)


@router.post("/create-doc")
async def create_doc(
    data: CreateDocRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a documentation article from the conversation's Q&A content. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from app.models.documentation import Documentation

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("doc_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        # Only extract user questions + agent answers (no code/tool output/logs)
        doc_context = _build_doc_context(data.messages)
        if not doc_context.strip():
            yield _sse("doc_error", {"detail": "No usable conversation context found."})
            return

        # Guard: require at least one substantive answer
        answer_msgs = [m for m in data.messages if m.get("type") == "answer"]
        has_substantive = any(len(m.get("content", "")) > 80 for m in answer_msgs)
        if not has_substantive:
            yield _sse("doc_error", {
                "detail": "The conversation doesn't have enough substantive content to build documentation. "
                          "Ask the agent a detailed question first, then click Build Documentation on the answer."
            })
            return

        yield _sse("doc_status", {"message": "Generating Documentation..."})

        user_prompt = (
            f"TOPIC: {data.goal}\n\n"
            f"{doc_context}"
        )

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _DOC_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("doc_llm_call_failed", error=str(exc))
            yield _sse("doc_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            doc_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("doc_json_parse_failed", raw=raw[:500])
            yield _sse("doc_error", {"detail": "Failed to parse documentation from LLM response."})
            return

        doc_markdown = _doc_json_to_markdown(doc_data)

        words = doc_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("doc_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("doc_chunk", {"chunk": doc_markdown, "done": True})

        doc_id = str(uuid4())
        doc = Documentation(
            doc_id=doc_id,
            team_id=data.team_id,
            title=doc_data.get("title", data.goal[:100]),
            goal=data.goal,
            doc_json=doc_data,
            doc_markdown=doc_markdown,
            source_conversation_id=data.conversation_id,
        )
        session.add(doc)
        await session.flush()

        logger.info("doc_created", doc_id=doc_id, team_id=data.team_id, title=doc.title)
        yield _sse("doc_done", {"doc_id": doc_id, "title": doc_data.get("title", data.goal[:100]), "doc_markdown": doc_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/docs")
async def list_docs(
    team_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List documentation for a team, newest first. team_id=__none__ lists general (no team) articles."""
    from app.models.documentation import Documentation
    tenant_products = await get_tenant_team_ids(current_user, session)
    q = select(Documentation)
    if team_id == "__none__":
        q = q.where(Documentation.team_id.is_(None))
    elif team_id:
        q = q.where(Documentation.team_id.in_(tenant_products))
        q = q.where(Documentation.team_id == team_id)
    else:
        raise HTTPException(status_code=422, detail="team_id is required (use a team UUID or __none__ for general)")
    result = await session.execute(q.order_by(Documentation.created_at.desc()))
    docs = result.scalars().all()
    return [d.to_dict() for d in docs]


@router.get("/docs/{doc_id}")
async def get_doc(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get a specific documentation article."""
    from app.models.documentation import Documentation
    result = await session.execute(
        select(Documentation).where(Documentation.doc_id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Documentation not found")
    return doc.to_dict()


@router.delete("/docs/{doc_id}")
async def delete_doc(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete a documentation article."""
    from app.models.documentation import Documentation
    result = await session.execute(
        select(Documentation).where(Documentation.doc_id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Documentation not found")
    await session.delete(doc)
    return {"status": "deleted", "doc_id": doc_id}


@router.put("/docs/{doc_id}/approve")
async def approve_doc(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Approve a documentation article (draft → approved)."""
    from app.models.documentation import Documentation
    from datetime import datetime as dt

    result = await session.execute(
        select(Documentation).where(Documentation.doc_id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Documentation not found")

    doc.status = "approved"
    doc.updated_at = dt.utcnow()
    await session.flush()
    return doc.to_dict()


@router.post("/edit-doc")
async def edit_doc(
    data: EditDocRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Edit an existing documentation article based on user instructions. Returns SSE stream."""
    import json as json_mod
    from openai import AsyncOpenAI
    from app.api.settings import get_active_llm_settings
    from app.models.documentation import Documentation
    from datetime import datetime as dt

    def _sse(event: str, data_dict: dict) -> str:
        return f"event: {event}\ndata: {json_mod.dumps(data_dict)}\n\n"

    async def event_generator():
        result = await session.execute(
            select(Documentation).where(Documentation.doc_id == data.doc_id)
        )
        doc = result.scalar_one_or_none()
        if not doc:
            yield _sse("doc_error", {"detail": "Documentation not found"})
            return

        if not doc.doc_json:
            yield _sse("doc_error", {"detail": "Documentation has no structured data to edit."})
            return

        llm_settings = await get_active_llm_settings(session)
        api_key = llm_settings.get("api_key")
        if not api_key:
            yield _sse("doc_error", {"detail": "No LLM API key configured. Set one in Settings."})
            return

        client_kw: dict = {"api_key": api_key}
        api_url = llm_settings.get("api_url")
        if api_url:
            client_kw["base_url"] = api_url
        llm_client = AsyncOpenAI(**client_kw)
        model = llm_settings.get("model_name", "gpt-4o")

        yield _sse("doc_status", {"message": "Editing Documentation..."})

        user_prompt = (
            f"CURRENT ARTICLE (JSON):\n{json_mod.dumps(doc.doc_json, indent=2)}\n\n"
            f"REQUESTED EDITS:\n{data.edit_instructions}"
        )

        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _DOC_EDIT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("doc_edit_llm_failed", error=str(exc))
            yield _sse("doc_error", {"detail": f"LLM call failed: {exc}"})
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            updated_data = json_mod.loads(cleaned)
        except json_mod.JSONDecodeError:
            logger.error("doc_edit_parse_failed", raw=raw[:500])
            yield _sse("doc_error", {"detail": "Failed to parse edited documentation from LLM response."})
            return

        updated_markdown = _doc_json_to_markdown(updated_data)

        words = updated_markdown.split(' ')
        total = len(words)
        chunk_size = 4
        for wi in range(0, total, chunk_size):
            partial = ' '.join(words[:wi + chunk_size])
            yield _sse("doc_chunk", {"chunk": partial, "done": False})
            await asyncio.sleep(0.02)
        yield _sse("doc_chunk", {"chunk": updated_markdown, "done": True})

        doc.doc_json = updated_data
        doc.doc_markdown = updated_markdown
        doc.title = updated_data.get("title", doc.title)
        doc.updated_at = dt.utcnow()
        await session.flush()

        logger.info("doc_edited", doc_id=data.doc_id, title=doc.title)
        yield _sse("doc_done", {"doc_id": data.doc_id, "title": doc.title, "doc_markdown": updated_markdown})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )



# ---------------------------------------------------------------------------
# Dashboard aggregate stats
# ---------------------------------------------------------------------------

@router.get("/dashboard-stats")
async def dashboard_stats(
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Aggregate stats for the dashboard, scoped to the user's tenant."""
    from app.models.sop import SOP
    from app.models.automation_run import AutomationRun
    from app.models.documentation import Documentation
    from app.models.team import Team
    from app.models.team_access import TeamAccess
    from app.models.pod import Pod
    from app.models.conversation import Conversation
    from sqlalchemy import func
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    tenant_id = current_user.tenant_id
    user_id = current_user.user_id

    # Products — scoped by tenant + role:
    # zero_admin sees all; other admins see their tenant; user_admins see own;
    # regular users see via TeamAccess
    from sqlalchemy import or_, text
    _prebuilt_filter = text("json_extract(teams.metadata_json, '$.is_prebuilt') = 1")
    products_stmt = select(Team).options(selectinload(Team.folder_groups))
    if current_user.role == "zero_admin":
        pass  # zero_admin sees all tenants
    elif current_user.role in ("admin", "super_admin", "tenant_admin"):
        products_stmt = products_stmt.where(or_(Team.tenant_id == tenant_id, _prebuilt_filter))
    elif current_user.role == "user_admin":
        products_stmt = products_stmt.where(or_(
            (Team.tenant_id == tenant_id) & (Team.created_by == user_id),
            _prebuilt_filter,
        ))
    else:
        # Regular users — only teams they have access to + prebuilt
        accessible = select(TeamAccess.team_id).where(TeamAccess.user_id == user_id)
        products_stmt = products_stmt.where(or_(
            (Team.tenant_id == tenant_id) & Team.team_id.in_(accessible),
            _prebuilt_filter,
        ))
    products_result = await session.execute(products_stmt)
    teams = products_result.scalars().all()
    team_ids = [p.team_id for p in teams]
    total_groups = sum(len(p.folder_groups) for p in teams)
    trained_groups = sum(
        sum(1 for g in p.folder_groups if g.training_status == "completed")
        for p in teams
    )

    # PODs — scoped to tenant's teams, exclude synthetic __local__
    pods_stmt = select(Pod).where(Pod.pod_id != "__local__")
    if team_ids:
        pods_stmt = pods_stmt.where(Pod.team_id.in_(team_ids))
    else:
        pods_stmt = pods_stmt.where(Pod.team_id.is_(None))
    pods_result = await session.execute(pods_stmt)
    pods = pods_result.scalars().all()
    online_pods = sum(1 for p in pods if p.status == "online")

    # Automations — scoped to tenant's teams only
    if team_ids:
        sop_filter = SOP.team_id.in_(team_ids)
    else:
        # No teams for this tenant — show nothing
        sop_filter = SOP.team_id.is_(None)
    total_sops = (await session.execute(select(func.count(SOP.sop_id)).where(sop_filter))).scalar() or 0
    approved_sops = (await session.execute(
        select(func.count(SOP.sop_id)).where(sop_filter, SOP.status == "approved")
    )).scalar() or 0
    active_sops = (await session.execute(
        select(func.count(SOP.sop_id)).where(sop_filter, SOP.status == "approved", SOP.is_active == True)
    )).scalar() or 0

    # Automation runs — scoped to tenant's SOPs
    tenant_sop_ids_result = await session.execute(select(SOP.sop_id).where(sop_filter))
    tenant_sop_ids = [r[0] for r in tenant_sop_ids_result.all()]
    run_filter = AutomationRun.sop_id.in_(tenant_sop_ids) if tenant_sop_ids else AutomationRun.run_id.is_(None)
    total_runs = (await session.execute(select(func.count(AutomationRun.run_id)).where(run_filter))).scalar() or 0
    completed_runs = (await session.execute(
        select(func.count(AutomationRun.run_id)).where(run_filter, AutomationRun.status == "completed")
    )).scalar() or 0
    failed_runs = (await session.execute(
        select(func.count(AutomationRun.run_id)).where(run_filter, AutomationRun.status == "failed")
    )).scalar() or 0

    # Recent runs (last 10) — scoped
    recent_runs_stmt = select(AutomationRun).where(run_filter).order_by(AutomationRun.started_at.desc()).limit(10)
    recent_runs_result = await session.execute(recent_runs_stmt)
    recent_runs = recent_runs_result.scalars().all()

    # Map run sop_ids to titles
    run_sop_ids = list(set(r.sop_id for r in recent_runs))
    sop_titles = {}
    if run_sop_ids:
        sop_result = await session.execute(
            select(SOP.sop_id, SOP.title).where(SOP.sop_id.in_(run_sop_ids))
        )
        sop_titles = {row[0]: row[1] for row in sop_result.all()}

    # Documentation — scoped to tenant's teams only
    if team_ids:
        doc_filter = Documentation.team_id.in_(team_ids)
    else:
        # No teams for this tenant — show nothing
        doc_filter = Documentation.team_id.is_(None)
    total_docs = (await session.execute(select(func.count(Documentation.doc_id)).where(doc_filter))).scalar() or 0
    approved_docs = (await session.execute(
        select(func.count(Documentation.doc_id)).where(doc_filter, Documentation.status == "approved")
    )).scalar() or 0
    docs_this_week = (await session.execute(
        select(func.count(Documentation.doc_id)).where(doc_filter, Documentation.created_at >= week_start)
    )).scalar() or 0

    # Conversations — user's own conversations + conversations on their teams
    conv_filter = or_(
        Conversation.user_id == user_id,
        Conversation.team_id.in_(team_ids) if team_ids else Conversation.conversation_id.is_(None),
    )
    total_conversations = (await session.execute(
        select(func.count(Conversation.conversation_id)).where(conv_filter)
    )).scalar() or 0
    conversations_today = (await session.execute(
        select(func.count(Conversation.conversation_id)).where(
            conv_filter, Conversation.created_at >= today_start
        )
    )).scalar() or 0

    # Upcoming scheduled runs — scoped
    upcoming_result = await session.execute(
        select(SOP)
        .where(sop_filter, SOP.status == "approved", SOP.is_active == True, SOP.next_run_at.isnot(None))
        .order_by(SOP.next_run_at.asc())
        .limit(5)
    )
    upcoming = upcoming_result.scalars().all()

    # Products with coverage data
    product_coverage = []
    for p in teams:
        total = len(p.folder_groups)
        trained = sum(1 for g in p.folder_groups if g.training_status == "completed")
        in_progress = sum(1 for g in p.folder_groups if g.training_status == "training")
        product_coverage.append({
            "team_id": p.team_id,
            "team_name": p.team_name,
            "total_groups": total,
            "trained_groups": trained,
            "in_progress_groups": in_progress,
            "coverage_pct": round(trained / total * 100) if total > 0 else 0,
        })

    return {
        "stats": {
            "knowledge_coverage_pct": round(trained_groups / total_groups * 100) if total_groups > 0 else 0,
            "trained_groups": trained_groups,
            "total_groups": total_groups,
            "active_automations": active_sops,
            "approved_automations": approved_sops,
            "total_automations": total_sops,
            "total_docs": total_docs,
            "approved_docs": approved_docs,
            "docs_this_week": docs_this_week,
            "execution_success_rate": round(completed_runs / total_runs * 100) if total_runs > 0 else 100,
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "failed_runs": failed_runs,
            "online_pods": online_pods,
            "total_pods": len(pods),
            "total_conversations": total_conversations,
            "conversations_today": conversations_today,
            "total_teams": len(teams),
        },
        "recent_runs": [
            {
                **r.to_dict(),
                "sop_title": sop_titles.get(r.sop_id, "Unknown"),
            }
            for r in recent_runs
        ],
        "upcoming_schedules": [
            {
                "sop_id": s.sop_id,
                "title": s.title,
                "team_id": s.team_id,
                "schedule_type": s.schedule_type,
                "schedule_config": s.schedule_config,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            }
            for s in upcoming
        ],
        "team_coverage": product_coverage,
    }


# ---------------------------------------------------------------------------
# Sandbox file explorer
# ---------------------------------------------------------------------------

async def _oc_working_dir(user_id: str) -> Optional[str]:
    """Ask OC for the directory it's operating in.

    OC's `GET /path` returns `{home, state, config, worktree, directory}`. The
    `directory` field is the instance's actual cwd — what the write/edit tools
    resolve relative paths against. In managed mode this is OC's per-user
    config dir; in external/dev mode it's wherever the developer ran `bun run`.
    """
    import httpx
    port = opencode_manager.get_port(user_id)
    if not port:
        return None
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/path")
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data.get("directory") or data.get("worktree")
    except Exception:
        return None


@router.get("/sandbox/{conversation_id}/files")
async def list_sandbox_files(
    conversation_id: str,
    path: str = Query(default="/", description="Directory path relative to OC working dir"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List files in the agent's actual working directory (OC's cwd)."""
    from app.services.sandbox_manager import list_sandbox_files as _list_files
    root_dir = await _oc_working_dir(current_user.user_id)
    if not root_dir:
        return {"path": path, "entries": [], "root_dir": None}
    entries = _list_files(root_dir, path)
    return {"path": path, "entries": entries, "root_dir": root_dir}


@router.get("/sandbox/{conversation_id}/file")
async def read_sandbox_file(
    conversation_id: str,
    path: str = Query(..., description="File path relative to OC working dir"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Read a text file from the agent's working directory."""
    from app.services.sandbox_manager import read_sandbox_file as _read_file
    root_dir = await _oc_working_dir(current_user.user_id)
    if not root_dir:
        raise HTTPException(status_code=404, detail="OC working directory unavailable")
    content = _read_file(root_dir, path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"path": path, "content": content}


@router.get("/sandbox/{conversation_id}/download")
async def download_sandbox_file(
    conversation_id: str,
    path: str = Query(..., description="File path relative to OC working dir"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Download a file from the agent's working directory."""
    from app.services.sandbox_manager import get_sandbox_file_path
    from fastapi.responses import FileResponse
    import os

    root_dir = await _oc_working_dir(current_user.user_id)
    if not root_dir:
        raise HTTPException(status_code=404, detail="OC working directory unavailable")
    file_path = get_sandbox_file_path(root_dir, path)
    if file_path is None:
        raise HTTPException(status_code=404, detail="File not found")

    filename = os.path.basename(file_path)
    return FileResponse(file_path, filename=filename)


@router.get("/sandbox/{conversation_id}/serve/{path:path}")
async def serve_sandbox_file(
    conversation_id: str,
    path: str,
):
    """Serve a file from the agent's working directory with correct MIME type.

    This endpoint does NOT require auth so the browser workspace iframe can
    load HTML/JS/CSS files directly. Resolves the owning user via the
    conversation registry, then asks OC for its current working directory.
    """
    from app.services.sandbox_manager import get_sandbox_file_path, get_user_for_conversation
    from fastapi.responses import FileResponse
    import mimetypes

    user_id = get_user_for_conversation(conversation_id)
    if not user_id:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    root_dir = await _oc_working_dir(user_id)
    if not root_dir:
        raise HTTPException(status_code=404, detail="OC working directory unavailable")
    file_path = get_sandbox_file_path(root_dir, path)
    if file_path is None:
        raise HTTPException(status_code=404, detail="File not found")

    mime, _ = mimetypes.guess_type(file_path)
    return FileResponse(file_path, media_type=mime or "application/octet-stream")


@router.get("/sandbox/status")
async def sandbox_status(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Check if sandboxing is available (Podman installed + image built)."""
    from app.services.sandbox_manager import is_sandbox_available
    return is_sandbox_available()


# ---------------------------------------------------------------------------
# LLM Logs — developer-mode viewer for every LLM request/response
# ---------------------------------------------------------------------------

@router.get("/llm-logs/conversations")
async def list_llm_log_conversations(
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List conversations (with metadata) that have at least one LLM log entry
    for the current user. Used to populate the conversation dropdown in the
    dev-mode LLM Logs viewer."""
    # Get distinct conversation_ids from llm_logs for this user
    log_result = await session.execute(
        select(LLMLog.conversation_id)
        .where(LLMLog.user_id == current_user.user_id)
        .where(LLMLog.conversation_id.is_not(None))
        .distinct()
    )
    conv_ids = [row[0] for row in log_result.fetchall() if row[0]]

    if not conv_ids:
        return []

    # Fetch conversation metadata (title, timestamps) for those IDs
    conv_result = await session.execute(
        select(Conversation)
        .where(Conversation.conversation_id.in_(conv_ids))
        .where(Conversation.user_id == current_user.user_id)
        .order_by(Conversation.updated_at.desc())
    )
    conversations = conv_result.scalars().all()
    convs_by_id = {c.conversation_id: c for c in conversations}

    # Also count logs per conversation
    from sqlalchemy import func
    count_result = await session.execute(
        select(LLMLog.conversation_id, func.count(LLMLog.log_id))
        .where(LLMLog.user_id == current_user.user_id)
        .where(LLMLog.conversation_id.in_(conv_ids))
        .group_by(LLMLog.conversation_id)
    )
    counts = {row[0]: row[1] for row in count_result.fetchall()}

    out = []
    for cid in conv_ids:
        conv = convs_by_id.get(cid)
        out.append({
            "conversation_id": cid,
            "title": conv.title if conv else "(deleted)",
            "team_id": conv.team_id if conv else None,
            "updated_at": conv.updated_at.isoformat() if conv and conv.updated_at else None,
            "log_count": counts.get(cid, 0),
        })
    # Sort: most recently updated first, then by log count
    out.sort(key=lambda x: (x["updated_at"] or ""), reverse=True)
    return out


@router.get("/llm-logs")
async def get_llm_logs(
    conversation_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get all LLM log entries for a specific conversation (owned by current user).
    Returns entries sorted by creation time (oldest first for timeline view)."""
    result = await session.execute(
        select(LLMLog)
        .where(LLMLog.user_id == current_user.user_id)
        .where(LLMLog.conversation_id == conversation_id)
        .order_by(LLMLog.created_at.asc())
    )
    logs = result.scalars().all()
    return [log.to_dict() for log in logs]


# ────────────────────────────────────────────────────────────────────────────
# Internal endpoint: active team lookup for MCP subprocesses.
# Called per tool call by `app/retrace_mcp/kb.py` (and any other MCP that
# needs team-scoped data). Auth via `X-Internal-Token` — same shared-secret
# the browser/bash bridges use (see `app/api/browser_ws.py:_check_internal_token`).
# ────────────────────────────────────────────────────────────────────────────


class _ActiveTeamBody(BaseModel):
    user_id: str


@router.post("/internal/team/active", include_in_schema=False)
async def internal_get_active_team(
    body: _ActiveTeamBody,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    """Return the user's currently-active team_id for MCP subprocesses.
    Tries the in-memory dict first; falls back to `user.preferences` so a
    fresh backend that hasn't seen a regenerate-config yet can still answer.
    """
    from app.api.browser_ws import _check_internal_token
    _check_internal_token(x_internal_token)

    team_id = _active_team_id.get(body.user_id)
    if team_id is None:
        try:
            from app.models.user import User
            async with async_session_maker() as session:
                user = (await session.execute(
                    select(User).where(User.user_id == body.user_id)
                )).scalar_one_or_none()
                if user is not None:
                    team_id = (user.preferences or {}).get("active_team_id")
                    if team_id:
                        _active_team_id[body.user_id] = team_id
        except Exception as exc:
            logger.warning("active_team_db_lookup_failed", user_id=body.user_id, error=str(exc))

    return {"team_id": team_id}
