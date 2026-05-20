"""
WebSocket endpoint for browser automation screenshot streaming.

Protocol (JSON messages):
  Client -> Server:
    {"type": "click",     "x": 100, "y": 200}       -- click at coordinates
    {"type": "navigate",  "url": "https://..."}      -- navigate to URL
    {"type": "scroll",    "direction": "down"}        -- scroll page
    {"type": "type",      "text": "hello"}            -- type text
    {"type": "key",       "key": "Enter"}             -- press key
    {"type": "back"}                                   -- go back
    {"type": "forward"}                                -- go forward
    {"type": "refresh"}                                -- reload page

  Server -> Client:
    {"type": "screenshot", "data": "<base64>", "url": "...", "title": "..."}
    {"type": "status",     "message": "..."}
    {"type": "action_result", "result": {...}}
    {"type": "closed",     "message": "..."}
    {"type": "error",      "data": "..."}
"""

import asyncio
import os

import structlog
from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Optional

from app.services.browser_manager import browser_manager

logger = structlog.get_logger()
router = APIRouter()


@router.websocket("/ws/browser/{conversation_id}")
async def browser_websocket(websocket: WebSocket, conversation_id: str):
    """Bidirectional browser I/O over WebSocket."""
    await websocket.accept()

    # Resolve the user_id for this conversation_id so the BrowserSession
    # gets tagged with it. The internal-browser-action bridge uses this
    # tag to find the right session as a fallback when the active-conv
    # map is empty (e.g. after a dev-server reload mid-turn). Standalone
    # conversation IDs (the React composer's `standalone_xxx` ones) have
    # no DB row — those get user_id=None and a per-conversation Chromium.
    resolved_user_id: Optional[str] = None
    try:
        from app.services.sandbox_manager import get_user_for_conversation
        resolved_user_id = get_user_for_conversation(conversation_id)
        if not resolved_user_id and not conversation_id.startswith("standalone_"):
            # DB lookup for real ReTrace conversations not yet in the
            # in-memory map (e.g. browser opens before the agent fires).
            from sqlalchemy import text as sa_text
            from app.db.database import async_session_maker
            async with async_session_maker() as db:
                result = await db.execute(
                    sa_text("SELECT user_id FROM conversations WHERE conversation_id = :cid"),
                    {"cid": conversation_id},
                )
                row = result.fetchone()
                if row and row[0]:
                    resolved_user_id = row[0]
                    # Register so subsequent lookups skip the DB.
                    try:
                        from app.services.sandbox_manager import register_conversation
                        register_conversation(conversation_id, resolved_user_id)
                    except Exception:
                        pass
    except Exception as exc:
        logger.warning("browser_ws_user_lookup_failed", conversation_id=conversation_id, error=str(exc))

    try:
        session = await browser_manager.get_or_create(conversation_id, user_id=resolved_user_id)
    except Exception as exc:
        await websocket.send_json({"type": "error", "data": str(exc)})
        await websocket.close()
        return

    # Register this client for screenshot broadcasts
    session.websocket_clients.add(websocket)
    # Capture the main event loop so worker threads (agent sandbox) can
    # schedule ws.send_text() calls on the correct loop.
    try:
        import asyncio as _asyncio
        browser_manager._main_loop = _asyncio.get_running_loop()
    except Exception:
        pass

    # Send initial status
    await websocket.send_json({
        "type": "status",
        "message": "connected",
        "url": session.current_url,
        "title": session.current_title,
    })

    # Send an initial screenshot immediately
    screenshot = await session.get_screenshot_base64()
    if screenshot:
        await websocket.send_json({
            "type": "screenshot",
            "data": screenshot,
            "url": session.current_url,
            "title": session.current_title,
        })

    try:
        while True:
            raw = await websocket.receive_json()
            msg_type = raw.get("type", "")
            result = {}

            if msg_type == "click":
                x, y = raw.get("x", 0), raw.get("y", 0)
                # Broadcast click indicator to all clients (including sender)
                click_msg = {"type": "click_indicator", "x": int(x), "y": int(y)}
                for client in list(session.websocket_clients):
                    try:
                        await client.send_json(click_msg)
                    except Exception:
                        pass
                result = await session.click(int(x), int(y))
            elif msg_type == "navigate":
                url = raw.get("url", "")
                if url:
                    await websocket.send_json({"type": "status", "message": f"Navigating to {url}..."})
                    result = await session.navigate(url)
            elif msg_type == "scroll":
                direction = raw.get("direction", "down")
                amount = raw.get("amount", 300)
                result = await session.scroll(direction, int(amount))
            elif msg_type == "type":
                text = raw.get("text", "")
                result = await session.type_text(text)
            elif msg_type == "key":
                key = raw.get("key", "")
                result = await session.press_key(key)
            elif msg_type == "back":
                result = await session.go_back()
            elif msg_type == "forward":
                result = await session.go_forward()
            elif msg_type == "refresh":
                result = await session.refresh()
            elif msg_type == "screenshot":
                # Manual screenshot request from user
                pass
            else:
                result = {"error": f"Unknown message type: {msg_type}"}

            await websocket.send_json({"type": "action_result", "result": result})

            # Push screenshot after every action
            screenshot = await session.get_screenshot_base64()
            if screenshot:
                await websocket.send_json({
                    "type": "screenshot",
                    "data": screenshot,
                    "url": session.current_url,
                    "title": session.current_title,
                })

    except WebSocketDisconnect:
        logger.info("browser_ws_disconnected", conversation_id=conversation_id)
    except Exception as exc:
        logger.error("browser_ws_error", error=str(exc))
        try:
            await websocket.send_json({"type": "error", "data": str(exc)})
        except Exception:
            pass
    finally:
        session.websocket_clients.discard(websocket)


# ─────────────────────────────────────────────────────────────────────────────
# Internal HTTP bridge for the `retrace-browser` MCP server.
#
# The MCP server runs in a separate Python process (spawned by the OpenCode
# sidecar) so it can't directly import `browser_manager` and share state with
# the FastAPI backend's in-memory BrowserSession instances. Instead, the MCP
# server posts here, and we drive the same BrowserSession the WebSocket is
# attached to — so the agent and the user share one Chromium, one screen.
#
# Authentication: shared-secret header `X-Internal-Token` matching the
# `RETRACE_INTERNAL_TOKEN` env var (set in opencode.json's MCP `environment`
# section so the MCP subprocess inherits it). If the env var is unset we
# refuse the call — never allow anonymous internal access even on localhost.
# ─────────────────────────────────────────────────────────────────────────────


class _BrowserActionBody(BaseModel):
    """Body of the internal browser-action bridge.

    `user_id` is REQUIRED — we use it to resolve which conversation the
    agent is currently operating in (via `_active_browser_conv[user_id]`)
    so the action runs against the same BrowserSession the user's WS is
    attached to. `conversation_id` is intentionally not accepted; the LLM
    making one up was the root cause of the "agent click doesn't show in
    panel" class of bugs.
    """
    action: str
    user_id: str
    url: Optional[str] = None
    selector: Optional[str] = None
    text: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    key: Optional[str] = None
    direction: Optional[str] = None
    amount: Optional[int] = None
    expression: Optional[str] = None
    value: Optional[str] = None
    seconds: Optional[float] = None
    max_tiles: Optional[int] = None


def _check_internal_token(provided: Optional[str]) -> None:
    """Validate the internal-bridge shared secret. The backend's lifespan
    handler generates the token and writes it to a file + os.environ so any
    process started by us (OC sidecar, MCP subprocesses) can read the same
    value. We accept either the env var or the file as the source of truth
    in case the env var got dropped during a partial reload.
    """
    expected = os.environ.get("RETRACE_INTERNAL_TOKEN")
    if not expected:
        try:
            from pathlib import Path
            token_path = Path.home() / ".local" / "share" / "retrace" / "internal-token"
            if token_path.exists():
                expected = token_path.read_text().strip() or None
                if expected:
                    # Cache for the next call.
                    os.environ["RETRACE_INTERNAL_TOKEN"] = expected
        except Exception:
            pass
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Internal browser bridge is disabled: RETRACE_INTERNAL_TOKEN not set",
        )
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid internal token")


@router.post("/api/v1/internal/browser/action")
async def internal_browser_action(
    body: _BrowserActionBody,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    """Execute a browser action on the BrowserSession for the user's
    currently-active conversation.

    The conversation_id is NEVER taken from the request — we look it up
    server-side from `_active_browser_conv[user_id]`, which is set when
    `opencode_send_message` starts processing a turn. That guarantees the
    agent's action runs against the same BrowserSession the user's
    BrowserWorkspace WS is attached to, so the agent's navigation /click
    / typing all show up in the right panel.

    Errors with `409 Conflict` if no active conversation is recorded for
    the user (i.e. the agent called a browser tool outside an active
    turn). The MCP propagates this back to the LLM as an error tool result.
    """
    _check_internal_token(x_internal_token)

    # Resolve the REAL conversation_id. Two-tier lookup:
    #
    # 1. The `_active_browser_conv[user_id]` map populated by
    #    `opencode_send_message` at turn start. Authoritative when present
    #    — guarantees we drive the conversation the agent is actively
    #    processing.
    #
    # 2. Fallback: scan BrowserManager for any BrowserSession that belongs
    #    to this user_id AND has live WebSocket clients attached. This
    #    survives uvicorn dev-reloads (which wipe the module-level
    #    `_active_browser_conv` dict mid-turn) and covers the case where
    #    the agent calls a browser tool slightly outside the turn boundary.
    #    The "has WS clients" check tells us the user is actually looking
    #    at this BrowserSession's panel — so it's the right target.
    real_conv_id: Optional[str] = None
    try:
        from app.api.agent import get_active_browser_conv
        real_conv_id = get_active_browser_conv(body.user_id)
    except Exception as exc:
        logger.warning("browser_bridge_active_conv_lookup_failed", error=str(exc))

    if not real_conv_id:
        # Fallback: find a live WS-attached BrowserSession for this user.
        # Sort by `last_activity` descending so we pick the most-recently-
        # active one if there happen to be multiple.
        candidates = []
        for cid, sess in browser_manager._sessions.items():
            if sess.user_id == body.user_id and sess.websocket_clients:
                candidates.append((sess.last_activity, cid))
        if candidates:
            candidates.sort(reverse=True)
            real_conv_id = candidates[0][1]
            logger.info(
                "browser_bridge_fallback_ws_session",
                user_id=body.user_id,
                resolved_conv=real_conv_id,
                candidate_count=len(candidates),
            )

    if not real_conv_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "No active conversation for this user. Either the agent is "
                "outside an active turn, or no BrowserWorkspace WebSocket "
                "is attached. Open /agent and try again."
            ),
        )

    # Get-or-create the session. user_id is passed so the per-user Chromium
    # is shared across this user's conversations (and so memory/page caps
    # are enforced per user).
    session = await browser_manager.get_or_create(real_conv_id, user_id=body.user_id)

    action = body.action.lower()
    result: dict = {}
    try:
        if action == "navigate":
            if not body.url:
                raise HTTPException(status_code=400, detail="`url` required for navigate")
            result = await session.navigate(body.url)
        elif action == "click_xy":
            result = await session.click(int(body.x or 0), int(body.y or 0))
        elif action == "click":
            # Selector-based click — what the agent sends most of the time.
            if not body.selector:
                raise HTTPException(status_code=400, detail="`selector` required for click")
            result = await session.click_selector(body.selector)
        elif action == "vision_click":
            if not body.text:
                raise HTTPException(status_code=400, detail="`text` (description) required for vision_click")
            result = await session.vision_click(body.text)
        elif action == "type":
            if not body.selector:
                raise HTTPException(status_code=400, detail="`selector` required for type")
            result = await session.type_in_selector(body.selector, body.text or "")
        elif action == "type_text":
            result = await session.type_text(body.text or "")
        elif action == "press_key":
            result = await session.press_key(body.key or "")
        elif action == "scroll":
            result = await session.scroll(body.direction or "down", int(body.amount or 300))
        elif action == "back":
            result = await session.go_back()
        elif action == "forward":
            result = await session.go_forward()
        elif action == "refresh":
            result = await session.refresh()
        elif action == "read_page":
            result = await session.read_page(body.selector)
        elif action == "screenshot":
            ss = await session.get_screenshot_base64()
            result = {"screenshot": ss, "url": session.current_url, "title": session.current_title}
        elif action == "evaluate_js":
            if not body.expression:
                raise HTTPException(status_code=400, detail="`expression` required for evaluate_js")
            result = await session.evaluate_js(body.expression)
        elif action == "select_option":
            if not body.selector or body.value is None:
                raise HTTPException(status_code=400, detail="`selector` and `value` required for select_option")
            result = await session.select_option(body.selector, body.value)
        elif action == "hover":
            result = await session.hover(body.selector, body.x, body.y)
        elif action == "wait":
            result = await session.wait(body.seconds if body.seconds is not None else 1.0)
        elif action == "get_url":
            result = await session.get_url_info()
        elif action == "screenshot_full_page":
            result = await session.screenshot_full_page(int(body.max_tiles or 10))
        elif action == "analyze_page":
            result = await session.analyze_page()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("internal_browser_action_failed", action=action, error=str(exc))
        result = {"error": str(exc)}

    # Push a fresh screenshot to all WS clients so the user sees the agent's
    # action immediately (no waiting for the 333ms screenshot loop). The
    # action methods on BrowserSession already wait for the page to settle
    # (see navigate/click_selector implementations) so this captures the
    # post-action frame, not a half-rendered intermediate.
    try:
        await browser_manager.push_screenshot(real_conv_id)
    except Exception:
        pass

    # Attach a fresh screenshot to the action response so the LLM-facing MCP
    # tool can return it as a vision attachment alongside the JSON metadata.
    # Pre-OC's `auto_browser` did the same — every browser action returned a
    # screenshot to the agent so it could see the post-action page state.
    # Skip when:
    #   - the action errored (page may be in a weird state, don't waste tokens)
    #   - the action's own response already includes a `screenshot` field
    #     (the dedicated `screenshot` action above)
    #   - the action is text-only and a screenshot adds nothing useful
    #     (`read_page`, `evaluate_js`, `get_url`, `analyze_page`,
    #     `screenshot_full_page` which returns its own tiles, `wait`)
    _NO_AUTO_SCREENSHOT = {
        "read_page", "evaluate_js", "get_url", "analyze_page",
        "screenshot_full_page", "wait", "screenshot",
    }
    if (
        isinstance(result, dict)
        and "error" not in result
        and "screenshot" not in result
        and action not in _NO_AUTO_SCREENSHOT
    ):
        try:
            ss = await session.get_screenshot_base64()
            if ss:
                result["screenshot"] = ss
        except Exception as exc:
            logger.warning("auto_screenshot_attach_failed", action=action, error=str(exc))

    return result
