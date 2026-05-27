"""
Session Bridge — maps OpenCode sessions to ReTrace conversations.

When the frontend creates an OpenCode session, we create or link a ReTrace
conversation. After each message exchange, we sync messages back to the
ReTrace DB for dashboard/search/audit.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import async_session_maker
from app.models.conversation import Conversation, ConversationMessage

logger = structlog.get_logger()


# Strong references to in-flight fire-and-forget background tasks (deferred
# title sync, opportunistic refresh from list/get endpoints, etc.). Python's
# `asyncio.create_task` keeps only a WEAK reference to its task — a task with
# no other reference can be garbage-collected at any time, including mid-
# execution. We add tasks here on creation and remove them when done.
_BACKGROUND_TASKS: set = set()

# Per-OC-session dedup so list/get opportunistic refreshes (which can fire
# many times per minute via TanStack Query refetches) don't stack 4-poll,
# ~7-second tasks against OpenCode's HTTP API. Without this the OC daemon
# on :4096 gets a thundering herd of `/session/<id>` GETs that crowd out
# session-create calls on the same loop.
_INFLIGHT_TITLE_SYNCS: set = set()


def schedule_deferred_title_sync(
    opencode_session_id: str,
    user_id: str,
    conversation_id: str,
    expected_auto: Optional[str],
) -> None:
    """Schedule a fire-and-forget title-refresh task with a strong reference
    so Python's GC won't kill it before it finishes its ~7-second poll.

    Safe to call from request handlers (FastAPI routes) or from inside the
    main sync — the helper handles task lifecycle (creation + reference +
    cleanup) so callers don't have to.
    """
    import asyncio
    if opencode_session_id in _INFLIGHT_TITLE_SYNCS:
        return
    _INFLIGHT_TITLE_SYNCS.add(opencode_session_id)
    task = asyncio.create_task(
        _deferred_title_sync(
            opencode_session_id=opencode_session_id,
            user_id=user_id,
            conversation_id=conversation_id,
            expected_auto=expected_auto,
        )
    )
    _BACKGROUND_TASKS.add(task)

    def _on_done(t):
        _BACKGROUND_TASKS.discard(t)
        _INFLIGHT_TITLE_SYNCS.discard(opencode_session_id)

    task.add_done_callback(_on_done)


async def get_or_create_conversation(
    opencode_session_id: str,
    user_id: str,
    team_id: Optional[str] = None,
    title: str = "New conversation",
) -> Conversation:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Conversation)
            .where(Conversation.opencode_session_id == opencode_session_id)
        )
        convo = result.scalar_one_or_none()
        if convo:
            return convo

        convo = Conversation(
            conversation_id=str(uuid4()),
            opencode_session_id=opencode_session_id,
            team_id=team_id,
            user_id=user_id,
            title=title,
        )
        session.add(convo)
        await session.commit()

        result = await session.execute(
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(Conversation.conversation_id == convo.conversation_id)
        )
        return result.scalar_one()


async def find_conversation_by_opencode_session(opencode_session_id: str) -> Optional[Conversation]:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(Conversation.opencode_session_id == opencode_session_id)
        )
        return result.scalar_one_or_none()


async def find_opencode_session_id(conversation_id: str) -> Optional[str]:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Conversation.opencode_session_id)
            .where(Conversation.conversation_id == conversation_id)
        )
        row = result.scalar_one_or_none()
        return row if isinstance(row, str) else None


async def sync_messages_from_opencode(
    opencode_session_id: str,
    messages: List[Dict[str, Any]],
) -> None:
    """Persist an OpenCode session's full message log into ReTrace's
    ConversationMessage table.

    OpenCode messages have the shape:
        { "info": { "id": "...", "role": "user"|"assistant", ... }, "parts": [...] }

    Each part maps to one or more ConversationMessage rows:
      - user message text part → msg_type='user'
      - assistant text part    → msg_type='answer'
      - assistant tool part    → two rows: msg_type='code' (the call) + msg_type='output' (the result)
      - assistant thinking part → msg_type='thinking'

    OpenCode's part IDs are used as ConversationMessage.message_id so repeated
    syncs are idempotent — only new parts get inserted, fixing the previous
    N²-per-turn growth from the legacy `save_messages` route.
    """
    if not messages:
        return
    async with async_session_maker() as session:
        result = await session.execute(
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(Conversation.opencode_session_id == opencode_session_id)
        )
        convo = result.scalar_one_or_none()
        if not convo:
            logger.warning("sync_conversation_not_found", opencode_session_id=opencode_session_id)
            return

        existing_ids: set[str] = {m.message_id for m in convo.messages}
        max_pos = max((m.position for m in convo.messages), default=-1)
        position = max_pos
        new_rows: List[ConversationMessage] = []
        first_user_text: Optional[str] = None

        for msg in messages:
            info = msg.get("info") or {}
            role = info.get("role", "")
            parts = msg.get("parts") or []

            if role == "user":
                # Combine all text parts into a single user message. There's
                # almost always exactly one text part for user messages.
                user_text = "\n".join(
                    p.get("text", "") for p in parts if p.get("type") == "text"
                ).strip()
                if not user_text:
                    continue
                msg_id = info.get("id") or str(uuid4())
                if msg_id in existing_ids:
                    if first_user_text is None:
                        first_user_text = user_text
                    continue
                position += 1
                new_rows.append(ConversationMessage(
                    message_id=msg_id,
                    conversation_id=convo.conversation_id,
                    msg_type="user",
                    content=user_text,
                    position=position,
                ))
                existing_ids.add(msg_id)
                if first_user_text is None:
                    first_user_text = user_text

            elif role == "assistant":
                meta_common = {
                    "model": info.get("modelID"),
                    "cost": info.get("cost"),
                    "tokens": info.get("tokens"),
                }
                for part in parts:
                    part_id = part.get("id") or str(uuid4())
                    part_type = part.get("type")

                    if part_type == "text":
                        text = part.get("text", "")
                        if not text:
                            continue
                        if part_id in existing_ids:
                            continue
                        position += 1
                        new_rows.append(ConversationMessage(
                            message_id=part_id,
                            conversation_id=convo.conversation_id,
                            msg_type="answer",
                            content=text,
                            position=position,
                            meta_json=meta_common,
                        ))
                        existing_ids.add(part_id)

                    elif part_type == "tool":
                        state = part.get("state") or {}
                        tool_name = part.get("tool", "")
                        input_data = state.get("input") or {}
                        output = state.get("output", "")
                        status = state.get("status", "completed")

                        # Tool call (the invocation)
                        call_id = f"{part_id}::call"
                        if call_id not in existing_ids:
                            position += 1
                            new_rows.append(ConversationMessage(
                                message_id=call_id,
                                conversation_id=convo.conversation_id,
                                msg_type="code",
                                content=json.dumps(input_data) if not isinstance(input_data, str) else input_data,
                                position=position,
                                meta_json={
                                    **meta_common,
                                    "tool": tool_name,
                                    "oc_part_id": part_id,
                                    "status": status,
                                },
                            ))
                            existing_ids.add(call_id)

                        # Tool output — only persist when there's output AND status is terminal.
                        # Don't churn the DB with placeholder rows for in-progress tool calls.
                        if output and status in ("completed", "error"):
                            output_id = f"{part_id}::output"
                            if output_id not in existing_ids:
                                position += 1
                                output_str = output if isinstance(output, str) else json.dumps(output)
                                new_rows.append(ConversationMessage(
                                    message_id=output_id,
                                    conversation_id=convo.conversation_id,
                                    msg_type="output",
                                    content=output_str,
                                    position=position,
                                    meta_json={
                                        "tool": tool_name,
                                        "oc_part_id": part_id,
                                        "status": status,
                                    },
                                ))
                                existing_ids.add(output_id)

                    elif part_type == "thinking":
                        text = part.get("text", "")
                        if not text:
                            continue
                        if part_id in existing_ids:
                            continue
                        position += 1
                        new_rows.append(ConversationMessage(
                            message_id=part_id,
                            conversation_id=convo.conversation_id,
                            msg_type="thinking",
                            content=text,
                            position=position,
                            meta_json=meta_common,
                        ))
                        existing_ids.add(part_id)

        if new_rows:
            for row in new_rows:
                convo.messages.append(row)
            convo.updated_at = datetime.utcnow()

            # Title resolution. Two sources:
            #   1. OpenCode's LLM-generated session.title (via ensureTitle —
            #      fires on the first turn and yields a polished phrase).
            #   2. The truncated first user message (stopgap if OC's title
            #      isn't ready yet on the first sync).
            # OC's title is preferred when available. We only auto-update the
            # title if it's still the placeholder OR exactly equals our own
            # earlier truncation — so a user's manual rename (via the pencil
            # icon in the UI) is never overwritten.
            auto_truncation: Optional[str] = None
            if first_user_text:
                auto_truncation = first_user_text[:80]
                if len(first_user_text) > 80:
                    auto_truncation += "..."

            scheduled_deferred = False
            if convo.title in ("New conversation", auto_truncation):
                oc_title = await _fetch_opencode_session_title(
                    opencode_session_id, convo.user_id
                )
                if oc_title:
                    convo.title = oc_title[:255]
                elif convo.title == "New conversation" and auto_truncation:
                    # OC's forked title-gen hasn't completed yet — use our
                    # stopgap. A later sync will upgrade to OC's title once
                    # ensureTitle finishes (the check above re-runs each turn).
                    convo.title = auto_truncation
                # Race-handler: OC's ensureTitle runs in a forked fiber that
                # may not have completed by the time this sync ran. For one-
                # turn conversations there's no later sync to upgrade the
                # title — so schedule a background poll that catches OC's
                # title once it lands (typically within 1-3s). Use the
                # strong-ref helper so the task isn't GC'd mid-execution.
                if not oc_title and first_user_text:
                    schedule_deferred_title_sync(
                        opencode_session_id=opencode_session_id,
                        user_id=convo.user_id,
                        conversation_id=convo.conversation_id,
                        expected_auto=auto_truncation,
                    )
                    scheduled_deferred = True

            await session.commit()

        logger.info(
            "sync_messages_complete",
            opencode_session_id=opencode_session_id,
            new_count=len(new_rows),
        )


async def _deferred_title_sync(
    opencode_session_id: str,
    user_id: str,
    conversation_id: str,
    expected_auto: Optional[str],
) -> None:
    """Background poll for OpenCode's LLM-generated title.

    `sync_messages_from_opencode` runs immediately after the agent response
    stream ends, which races with OC's forked `ensureTitle` fiber. For
    one-turn conversations there's no later sync to upgrade the title —
    so we poll here in the background and write the title once it appears.

    Stops early once a non-default OC title is captured. Only updates
    `convo.title` if it's still in an auto-set state (placeholder or the
    truncation stopgap set by the original sync); preserves any manual
    rename the user did via the pencil icon in the UI.

    Best-effort — wraps every step in try/except and never raises. A
    failed deferred sync just leaves the truncation stopgap in place.
    """
    import asyncio
    logger.info("deferred_title_sync_start", oc_sid=opencode_session_id, conv_id=conversation_id, expected_auto=expected_auto)
    delays = (1.0, 1.5, 2.0, 2.5)  # ~7s total polling window
    for i, delay in enumerate(delays):
        await asyncio.sleep(delay)
        try:
            oc_title = await _fetch_opencode_session_title(
                opencode_session_id, user_id
            )
        except Exception:
            oc_title = None
        logger.info("deferred_title_sync_poll", oc_sid=opencode_session_id, attempt=i + 1, got=bool(oc_title), title=(oc_title or "")[:80])
        if not oc_title:
            continue
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Conversation).where(
                        Conversation.conversation_id == conversation_id
                    )
                )
                convo = result.scalar_one_or_none()
                if not convo:
                    return
                if convo.title in ("New conversation", expected_auto):
                    new_title = oc_title[:255]
                    if convo.title != new_title:
                        convo.title = new_title
                        await session.commit()
                        logger.info(
                            "deferred_title_synced",
                            opencode_session_id=opencode_session_id,
                            title=new_title,
                        )
            return
        except Exception as exc:
            logger.warning(
                "deferred_title_sync_failed",
                opencode_session_id=opencode_session_id,
                error=str(exc),
            )
            return


async def _fetch_opencode_session_title(
    opencode_session_id: str,
    user_id: str,
) -> Optional[str]:
    """Read OpenCode's `session.title` via the per-user OC sidecar HTTP API.

    DEBUG: temporarily logs every step at INFO level so we can diagnose why
    OC's "Greeting" isn't surfacing to the pill. Strip the extra logging
    once the root cause is identified.
    """
    try:
        from app.services.opencode_manager import opencode_manager
        import httpx

        port = opencode_manager.get_port(user_id)
        if not port:
            logger.info("title_fetch_skip_no_port", oc_sid=opencode_session_id, user_id=user_id)
            return None
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(
                f"http://127.0.0.1:{port}/session/{opencode_session_id}"
            )
            status = r.status_code
            body_preview = r.text[:300] if r.text else ""
            if status != 200:
                logger.info("title_fetch_non_200", oc_sid=opencode_session_id, status=status, body=body_preview)
                return None
            data = r.json()
        if isinstance(data, dict) and "info" in data and isinstance(data["info"], dict):
            data = data["info"]
        raw_title = data.get("title") if isinstance(data, dict) else None
        title = (raw_title or "").strip()
        if not title:
            logger.info("title_fetch_empty", oc_sid=opencode_session_id, raw=str(raw_title)[:120])
            return None
        if title.lower().startswith("new "):
            logger.info("title_fetch_default", oc_sid=opencode_session_id, title=title[:120])
            return None
        logger.info("title_fetch_ok", oc_sid=opencode_session_id, title=title[:120])
        return title
    except Exception as exc:
        logger.warning("title_fetch_exception", oc_sid=opencode_session_id, error=str(exc))
        return None


async def link_session(
    conversation_id: str,
    opencode_session_id: str,
) -> None:
    """Point a ReTrace conversation at an OpenCode session.

    If the conversation already has a different opencode_session_id, the old
    link is REPLACED (not refused). The old link almost always points at a
    dead session — typical cause: the OpenCode sidecar was restarted, so the
    upstream session id is gone but the DB row still has the old pointer. We
    want the next interaction with this conversation to use the new live
    session, not keep redirecting to nowhere.
    """
    async with async_session_maker() as session:
        result = await session.execute(
            select(Conversation)
            .where(Conversation.conversation_id == conversation_id)
        )
        convo = result.scalar_one_or_none()
        if convo:
            if convo.opencode_session_id and convo.opencode_session_id != opencode_session_id:
                logger.warning(
                    "link_session_replacing",
                    conversation_id=conversation_id,
                    existing=convo.opencode_session_id,
                    new=opencode_session_id,
                )
            convo.opencode_session_id = opencode_session_id
            await session.commit()


def _map_role_to_type(role: str) -> str:
    mapping = {
        "user": "user",
        "assistant": "answer",
        "tool": "output",
        "system": "system",
    }
    return mapping.get(role, "answer")


def _extract_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") == "tool_use":
                    parts.append(f"Tool: {part.get('name', '')}\nInput: {json.dumps(part.get('input', {}), indent=2)}")
                elif part.get("type") == "tool_result":
                    parts.append(part.get("content", "") if isinstance(part.get("content"), str) else json.dumps(part.get("content", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content) if content else ""


def _extract_meta(msg: Dict[str, Any]) -> Optional[Dict]:
    meta: Dict[str, Any] = {}
    if msg.get("role") == "assistant":
        tool_calls = []
        content = msg.get("content", [])
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tool_calls.append({"name": part.get("name"), "id": part.get("id")})
        if tool_calls:
            meta["tool_calls"] = tool_calls
    if msg.get("role") == "tool":
        meta["tool_call_id"] = msg.get("tool_call_id")
    return meta if meta else None
