"""
WebSocket endpoint for interactive terminal sessions, plus the HTTP bridge
that OC's forked shell.ts uses to drive the same PTY.

WebSocket protocol (JSON messages):
  Client -> Server:
    {"type": "input",  "data": "ls -la\\r\\n"}    -- keystrokes / pasted text
    {"type": "resize", "cols": 120, "rows": 40}   -- terminal resize

  Server -> Client:
    {"type": "scrollback", "data": "..."}          -- buffered history on connect
    {"type": "output",     "data": "..."}          -- live shell output
    {"type": "exited",     "code": <int>}          -- shell process ended

HTTP bridge (called by OC's forked shell.ts):
    POST /api/v1/internal/bash/exec
      body:   {"user_id": "...", "command": "...", "timeout": N}
      header: X-Internal-Token: <RETRACE_INTERNAL_TOKEN>
      result: {"output": "...", "conversation_id": "..."}
"""

import asyncio
from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.services.pty_manager import pty_manager

logger = structlog.get_logger()
router = APIRouter()


@router.websocket("/ws/terminal/{conversation_id}")
async def terminal_websocket(websocket: WebSocket, conversation_id: str):
    """Bidirectional terminal I/O over WebSocket."""
    await websocket.accept()

    # Capture the running asyncio event loop so background reader threads
    # can schedule WebSocket sends on it.
    pty_manager.set_event_loop(asyncio.get_running_loop())

    try:
        session = pty_manager.get_or_create(conversation_id)
    except Exception as exc:
        await websocket.send_json({"type": "error", "data": str(exc)})
        await websocket.close()
        return

    # Register this client for output broadcasts
    session.websocket_clients.add(websocket)

    # If the host shell failed to spawn, surface the error. This is rare —
    # only happens if $SHELL is missing or pty.fork() fails on the OS.
    if session.shell == "pending":
        await websocket.send_json({
            "type": "output",
            "data": "\033[1;31mFailed to spawn shell.\033[0m Check that $SHELL is set and the backend has terminal permissions.\r\n",
        })

    # Send buffered scrollback so the user sees prior output.
    # CRITICAL: filter it the same way live output is filtered. The raw
    # scrollback contains all the marker-wrapping infrastructure from prior
    # agent bash calls (printf wrappers, __RTSTART/__RTEND/__RTPS markers,
    # precmd setup). Live output goes through `_filter_pty_line` in the
    # reader loop — the scrollback dump must too, or a user opening the
    # Terminal tab after the agent has run commands sees raw marker garbage.
    if session.scrollback:
        try:
            from app.services.pty_manager import _filter_pty_line
            raw = bytes(session.scrollback).decode("utf-8", errors="replace")
            kept: list[str] = []
            for line in raw.split("\n"):
                cleaned = _filter_pty_line(line)
                if cleaned is not None:
                    kept.append(cleaned)
            await websocket.send_json({
                "type": "scrollback",
                "data": "\n".join(kept),
            })
        except Exception:
            session.websocket_clients.discard(websocket)
            return

    logger.info(
        "terminal_ws_connected",
        conversation_id=conversation_id,
        clients=len(session.websocket_clients),
    )

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "input":
                data = msg.get("data", "")
                if data:
                    pty_manager.write(conversation_id, data.encode())

            elif msg_type == "resize":
                cols = msg.get("cols", 120)
                rows = msg.get("rows", 30)
                pty_manager.resize(conversation_id, cols, rows)

    except WebSocketDisconnect:
        logger.info("terminal_ws_disconnected", conversation_id=conversation_id)
    except Exception as exc:
        logger.error("terminal_ws_error", conversation_id=conversation_id, error=str(exc))
    finally:
        session.websocket_clients.discard(websocket)


# ─────────────────────────────────────────────────────────────────────────────
# Internal HTTP bridge — drives the PTY for OC's forked shell.ts.
#
# Pattern matches the browser bridge in browser_ws.py — `user_id` (not
# `conversation_id`) is what OC sends. We resolve the real conv_id from
# the `_active_browser_conv[user_id]` map populated by opencode_send_message
# at turn start, so the agent's bash call writes to the same PTY the user's
# Terminal tab is attached to.
# ─────────────────────────────────────────────────────────────────────────────


class _BashExecBody(BaseModel):
    """Body of POST /api/v1/internal/bash/exec.

    No `conversation_id` argument — the LLM would invent values, spawning a
    PTY the user's xterm isn't attached to (same root cause as the browser
    bridge bug). We resolve conv_id server-side from user_id.
    """
    user_id: str
    command: str
    timeout: int = 30


@router.post("/api/v1/internal/bash/exec")
async def internal_bash_exec(
    body: _BashExecBody,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    """Execute a shell command on the PTY for the user's currently-active
    conversation. The PTY is shared with the user's Terminal tab — both
    parties' commands flow through the same shell process.

    Returns `{"output": "<stdout+stderr>", "conversation_id": "..."}`. On
    timeout, output contains the partial result plus a marker note that the
    command may still be running.

    Errors with `409 Conflict` if no active conversation is recorded for the
    user (agent called bash outside an active turn). The MCP propagates this
    back to the LLM as a tool error.
    """
    # Reuse the same shared-secret validator the browser bridge uses.
    from app.api.browser_ws import _check_internal_token
    _check_internal_token(x_internal_token)

    # Resolve the active conversation. Authoritative lookup first; a WS-attached
    # fallback covers uvicorn dev-reloads that wipe the in-memory map mid-turn.
    real_conv_id: Optional[str] = None
    try:
        from app.api.agent import get_active_browser_conv
        real_conv_id = get_active_browser_conv(body.user_id)
    except Exception as exc:
        logger.warning("bash_bridge_active_conv_lookup_failed", error=str(exc))

    if not real_conv_id:
        # Fallback: find a live WS-attached PTY for this user. We don't track
        # user_id on PTYSession directly, so we use the conversation table to
        # map. Best-effort — if it fails the agent gets a 409 and the LLM
        # learns to wait for the next turn.
        try:
            from app.db.database import async_session_maker
            from app.models.conversation import Conversation
            from sqlalchemy import select
            async with async_session_maker() as db:
                result = await db.execute(
                    select(Conversation.conversation_id)
                    .where(Conversation.user_id == body.user_id)
                    .order_by(Conversation.updated_at.desc())
                    .limit(20)
                )
                candidate_ids = [row[0] for row in result.all()]
            for cid in candidate_ids:
                sess = pty_manager.get(cid)
                if sess and sess.websocket_clients:
                    real_conv_id = cid
                    logger.info("bash_bridge_fallback_ws_pty", user_id=body.user_id, resolved_conv=cid)
                    break
        except Exception as exc:
            logger.warning("bash_bridge_fallback_failed", error=str(exc))

    if not real_conv_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "No active conversation for this user. Either the agent is "
                "outside an active turn, or no Terminal WebSocket is attached. "
                "Open /agent and try again."
            ),
        )

    # Get-or-create the PTY. If the user has already opened the Terminal tab,
    # this returns the existing session and the command shows up live in
    # their xterm. If not, the PTY spawns now and the user catches up via
    # scrollback when they open the tab.
    pty_manager.get_or_create(real_conv_id)

    # Some LLMs (notably Grok) HTML-entity-encode shell operators in tool
    # input — sending `&amp;&amp;` instead of `&&`, `&gt;` instead of `>`.
    # That breaks shell parsing. Decode before writing to the PTY. Pre-OC
    # `file_ops.write_file` had the same patch for the same reason.
    import html as _html
    command = _html.unescape(body.command or "")

    # Execute (sync — pty_manager.execute_command blocks on the marker round-
    # trip). Push it to a worker thread so the event loop stays responsive
    # for the screenshot loop, WS broadcasts, and other concurrent agent
    # tool calls.
    timeout = max(1, min(int(body.timeout or 30), 600))
    try:
        output = await asyncio.to_thread(
            pty_manager.execute_command,
            real_conv_id,
            command,
            float(timeout),
        )
    except Exception as exc:
        logger.error("bash_bridge_exec_failed", conv_id=real_conv_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"PTY exec failed: {exc}")

    return {"output": output, "conversation_id": real_conv_id}
