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
            # Auto-title from the first user message if the conversation still
            # carries the placeholder.
            if convo.title == "New conversation" and first_user_text:
                title = first_user_text[:80]
                if len(first_user_text) > 80:
                    title += "..."
                convo.title = title
            await session.commit()

        logger.info(
            "sync_messages_complete",
            opencode_session_id=opencode_session_id,
            new_count=len(new_rows),
        )


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
