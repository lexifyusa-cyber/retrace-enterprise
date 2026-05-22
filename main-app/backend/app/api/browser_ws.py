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
import json
import os

import structlog
from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Optional

from app.services.browser_manager import browser_manager

logger = structlog.get_logger()
router = APIRouter()


async def _broadcast_click_indicator(session, x: int, y: int) -> None:
    """Push a `click_indicator` to every WS client on `session`.

    Matches what the manual-click path in `browser_websocket()` does, so the
    workspace's dot+ripple animation fires for agent clicks too. Failures
    are best-effort — a dead client just gets dropped from the set.
    """
    if not session:
        logger.info("click_indicator_skip_no_session", x=x, y=y)
        return
    if not session.websocket_clients:
        logger.info(
            "click_indicator_skip_no_ws_clients",
            x=x, y=y,
            conv_id=getattr(session, "conversation_id", None),
        )
        return
    msg = {"type": "click_indicator", "x": int(x), "y": int(y)}
    disconnected = set()
    sent = 0
    for ws in list(session.websocket_clients):
        try:
            await ws.send_json(msg)
            sent += 1
        except Exception as exc:
            disconnected.add(ws)
            logger.warning("click_indicator_send_failed", error=str(exc))
    session.websocket_clients -= disconnected
    logger.info(
        "click_indicator_broadcast",
        x=x, y=y, sent=sent,
        conv_id=getattr(session, "conversation_id", None),
        total_ws=len(session.websocket_clients),
    )


@router.websocket("/ws/browser/{conversation_id}")
async def browser_websocket(websocket: WebSocket, conversation_id: str):
    """Bidirectional browser I/O over WebSocket."""
    await websocket.accept()

    # Resolve the user_id so the BrowserSession gets tagged with it. The
    # internal-browser-action bridge uses this tag to find the right session
    # as a fallback when the active-conv map is empty (e.g. after a dev-server
    # reload mid-turn) and — crucially — to match the user's standalone
    # session before any ReTrace conversation has been created.
    #
    # Two sources, in order:
    #   1. `?token=<jwt>` query param from the WS handshake. Browsers can't
    #      send Authorization headers on `new WebSocket(...)`, so the frontend
    #      appends the access token here. This is the only way to identify a
    #      `standalone_xxx` session (no DB row to look up).
    #   2. Fall back to deriving user_id from the conversation row in DB, for
    #      real (non-standalone) conversation IDs. Covers browser-tab reloads
    #      where the JWT might not be on the query string yet.
    resolved_user_id: Optional[str] = None

    token = websocket.query_params.get("token")
    if token:
        try:
            from app.core.security import verify_token
            payload = verify_token(token, expected_type="access")
            if payload and isinstance(payload.get("sub"), str):
                resolved_user_id = payload["sub"]
        except Exception as exc:
            logger.warning("browser_ws_token_verify_failed", error=str(exc))

    if not resolved_user_id:
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

    # Apply the user's screenshot tunables to the session before dispatch:
    #   - `screenshot_delay_ms` → post-action settle wait before capture
    #     (replaces the pre-OC sandbox `browser_daemon.py`'s screenshot_delay
    #     in the host-Playwright path)
    #   - `screenops_image_scale` → JPEG resize percentage applied right
    #     before base64-encoding the screenshot, cutting vision tokens for
    #     the auto-attach path on every browser action
    # Lookup is cheap (the settings row is singleton id=1, often cached
    # in SQLAlchemy's identity map for the request).
    try:
        from app.api.settings import get_active_llm_settings
        from app.db.database import async_session_maker
        async with async_session_maker() as _llm_session:
            _llm = await get_active_llm_settings(_llm_session)
        session.settle_delay_ms = int(_llm.get("screenshot_delay_ms", 500) or 500)
        session.image_scale_pct = int(_llm.get("screenops_image_scale", 100) or 100)
    except Exception as exc:
        logger.warning("browser_action_settings_lookup_failed", error=str(exc))

    action = body.action.lower()
    result: dict = {}
    try:
        if action == "navigate":
            if not body.url:
                raise HTTPException(status_code=400, detail="`url` required for navigate")
            result = await session.navigate(body.url)
        elif action == "click_xy":
            # Coords known up-front: broadcast before the click so the dot+
            # ripple shows on the pre-click screenshot the user is still seeing.
            cx, cy = int(body.x or 0), int(body.y or 0)
            await _broadcast_click_indicator(session, cx, cy)
            result = await session.click(cx, cy)
        elif action == "click":
            # Selector-based click — what the agent sends most of the time.
            if not body.selector:
                raise HTTPException(status_code=400, detail="`selector` required for click")
            result = await session.click_selector(body.selector)
            # click_selector now resolves the element's center and returns it
            # as `clicked: {x, y}` so the workspace can render the indicator.
            clicked = result.get("clicked") if isinstance(result, dict) else None
            if clicked and clicked.get("x") is not None and clicked.get("y") is not None:
                await _broadcast_click_indicator(session, clicked["x"], clicked["y"])
            else:
                logger.info(
                    "click_indicator_no_coords",
                    action="click", selector=body.selector,
                    has_clicked_field=bool(clicked),
                )
        elif action == "vision_click":
            if not body.text:
                raise HTTPException(status_code=400, detail="`text` (description) required for vision_click")
            result = await session.vision_click(body.text)
            # vision_click returns `clicked: {x, y}` after the screenops model
            # resolves the description to coords. Broadcast post-click so the
            # ripple lands on whichever screenshot is on screen.
            clicked = result.get("clicked") if isinstance(result, dict) else None
            if clicked and clicked.get("x") is not None and clicked.get("y") is not None:
                await _broadcast_click_indicator(session, clicked["x"], clicked["y"])
            else:
                logger.info(
                    "click_indicator_no_coords",
                    action="vision_click", target=body.text,
                    has_clicked_field=bool(clicked),
                )
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


# ─────────────────────────────────────────────────────────────────────────────
# Faithful LLM capture bridge
#
# OpenCode's AI-SDK middleware (`retrace-capture.ts`) POSTs here once per LLM
# round-trip with the EXACT post-transform request params (system, messages,
# tools with full MCP JSON schemas, sampling params) and the accumulated
# response. We render that into the `{role, content}` shape the LLM Logs page
# already knows how to draw and write one `llm_logs` row. This is the
# canonical writer; `agent.py`'s reconstruction only runs under
# `RETRACE_LLM_LOG_FALLBACK=1`. Same `X-Internal-Token` auth as above.
# ─────────────────────────────────────────────────────────────────────────────


class _LLMCaptureBody(BaseModel):
    """Body posted by OpenCode's faithful-LLM-capture middleware."""

    session_id: str
    log_id: str
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    agent_name: Optional[str] = None
    user_id: Optional[str] = None
    started_at_ms: int = 0
    completed_at_ms: int = 0
    request: dict = {}
    response: Optional[dict] = None
    error: Optional[str] = None


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except Exception:
        return str(value)


# AI SDK / MCP content parts that represent images use a few different
# shapes depending on SDK version. We accept all three so the LLM Logs
# viewer can render the screenshot regardless of which one OC emits.
_IMAGE_PART_TYPES = {"image", "image-data"}


def _is_image_part(part) -> bool:
    """True if `part` is a content block that carries image data."""
    if not isinstance(part, dict):
        return False
    t = part.get("type")
    if t in _IMAGE_PART_TYPES:
        return bool(part.get("data"))
    if t == "image_url":
        return bool((part.get("image_url") or {}).get("url"))
    if t == "file":
        media = (part.get("mediaType") or part.get("mimeType") or "").lower()
        return media.startswith("image/") and bool(part.get("data"))
    return False


def _extract_image_url(part: dict) -> Optional[str]:
    """Return a `data:` URI (or http URL) for an image content part, or None."""
    t = part.get("type")
    media = part.get("mediaType") or part.get("mimeType") or "image/png"
    if t in _IMAGE_PART_TYPES or t == "file":
        data = part.get("data")
        if not isinstance(data, str) or not data:
            return None
        if data.startswith(("data:", "http://", "https://")):
            return data
        return f"data:{media};base64,{data}"
    if t == "image_url":
        return (part.get("image_url") or {}).get("url")
    return None


def _split_tool_result_content(value) -> tuple[str, list[dict]]:
    """Split an AI SDK ToolResultPart.output.value (which can be a list of
    mixed text / image / unknown content parts) into a flat text string
    plus a list of `{url, mime}` attachment dicts. Images go to
    attachments so the viewer's existing `tool_result.attachments[]`
    branch (LlmLogs.tsx:384-405) renders them as `<img>` under the
    tool result block.
    """
    if isinstance(value, str):
        return value, []
    if not isinstance(value, list):
        return _stringify(value), []

    text_parts: list[str] = []
    attachments: list[dict] = []
    for item in value:
        if _is_image_part(item):
            url = _extract_image_url(item)
            if url:
                mime = item.get("mediaType") or item.get("mimeType") or "image/png"
                attachments.append({"url": url, "mime": mime})
                continue
        if isinstance(item, dict) and item.get("type") == "text":
            text_parts.append(item.get("text") or "")
        elif isinstance(item, str):
            text_parts.append(item)
        else:
            # Unknown content part — preserve as JSON so we don't lose info.
            text_parts.append(_stringify(item))
    return "\n\n".join(t for t in text_parts if t), attachments


def _render_capture_request(request: dict) -> list[dict]:
    """Turn a post-transform AI SDK `LanguageModelV3CallOptions` dict into the
    `{role, content}` message list the LLM Logs viewer renders.

    The first synthesized `system` block carries the sampling params and the
    full tool definitions (with MCP JSON schemas) so they show inline.
    """
    out: list[dict] = []

    head: list[str] = ["## REQUEST PARAMS"]
    for key in ("temperature", "topP", "topK", "maxOutputTokens", "toolChoice"):
        if request.get(key) is not None:
            head.append(f"- {key}: {_stringify(request[key])}")
    provider_options = request.get("providerOptions")
    if provider_options:
        head.append("\nproviderOptions:\n```json\n" + _stringify(provider_options) + "\n```")

    tools = request.get("tools")
    if isinstance(tools, list) and tools:
        head.append(f"\n## TOOLS ({len(tools)})\n")
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name") or tool.get("type") or "?"
            desc = (tool.get("description") or "").strip()
            head.append(f"### {name}\n{desc}")
            schema = tool.get("inputSchema")
            if schema is not None:
                head.append("```json\n" + _stringify(schema) + "\n```")
    out.append({"role": "system", "content": "\n".join(head)})

    for msg in request.get("prompt") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role, "content": _stringify(content)})
            continue
        blocks: list = []
        for part in content:
            if not isinstance(part, dict):
                blocks.append({"type": "text", "text": str(part)})
                continue
            part_type = part.get("type")
            if part_type == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif part_type == "reasoning":
                blocks.append({"type": "text", "text": "[reasoning] " + (part.get("text") or "")})
            elif part_type == "tool-call":
                blocks.append({
                    "type": "tool_use",
                    "id": part.get("toolCallId", ""),
                    "name": part.get("toolName", ""),
                    "input": part.get("input"),
                })
            elif part_type == "tool-result":
                output = part.get("output")
                value = output.get("value") if isinstance(output, dict) else output
                text, attachments = _split_tool_result_content(value)
                tr_block = {
                    "type": "tool_result",
                    "tool_use_id": part.get("toolCallId", ""),
                    "content": text,
                }
                if attachments:
                    tr_block["attachments"] = attachments
                blocks.append(tr_block)
            elif part_type == "file":
                media = part.get("mediaType") or ""
                data = part.get("data")
                if isinstance(data, str) and media.startswith("image/"):
                    url = data if data.startswith(("data:", "http")) else f"data:{media};base64,{data}"
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
                else:
                    blocks.append({"type": "text", "text": f"[file: {media or 'unknown type'}]"})
            else:
                blocks.append(part)
        out.append({"role": role, "content": blocks})

    return out


def _render_capture_response(response: Optional[dict], error: Optional[str]) -> str:
    """Render the AI SDK `onFinish` event into the ━━━-delimited plaintext
    blob the LLM Logs viewer shows for `response_content`.
    """
    if not response:
        return error or ""
    sections: list[str] = []
    reasoning = response.get("reasoningText")
    if reasoning:
        sections.append(f"━━━ THINKING ━━━\n{reasoning}")
    text = response.get("text")
    if text:
        sections.append(f"━━━ TEXT ━━━\n{text}")
    for call in response.get("toolCalls") or []:
        if not isinstance(call, dict):
            continue
        name = call.get("toolName") or "?"
        call_id = call.get("toolCallId") or "?"
        sections.append(
            f"━━━ TOOL CALL: {name} (call_id={call_id}) ━━━\n"
            f"input:\n{_stringify(call.get('input'))}"
        )
    footer: list[str] = []
    if response.get("finishReason"):
        footer.append(f"finish_reason: {response['finishReason']}")
    if response.get("usage"):
        footer.append(f"usage: {_stringify(response['usage'])}")
    if response.get("warnings"):
        footer.append(f"warnings: {_stringify(response['warnings'])}")
    if footer:
        sections.append("━━━ META ━━━\n" + "\n".join(footer))
    if error:
        sections.append(f"━━━ ERROR ━━━\n{error}")
    return "\n\n".join(sections).strip()


@router.post("/api/v1/internal/llm/capture", include_in_schema=False)
async def internal_llm_capture(
    body: _LLMCaptureBody,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    """Persist one faithful LLM round-trip captured by OpenCode's AI-SDK
    middleware — the canonical writer for the `llm_logs` table.
    """
    _check_internal_token(x_internal_token)

    from app.db.database import async_session_maker
    from app.models.llm_log import LLMLog
    from app.services.session_bridge import find_conversation_by_opencode_session
    from sqlalchemy import func, select

    convo = None
    try:
        convo = await find_conversation_by_opencode_session(body.session_id)
    except Exception as exc:
        logger.warning("llm_capture_conv_lookup_failed", session_id=body.session_id, error=str(exc))
    conversation_id = convo.conversation_id if convo else None
    user_id = (convo.user_id if convo else None) or body.user_id

    try:
        request_messages = _render_capture_request(body.request or {})
    except Exception as exc:
        logger.warning("llm_capture_render_request_failed", error=str(exc))
        request_messages = [{"role": "system", "content": f"[capture render failed: {exc}]"}]
    request_json = json.dumps(request_messages, ensure_ascii=False)
    if len(request_json) > 8_000_000:
        request_json = json.dumps(
            [{"role": "system", "content": f"[capture too large: {len(request_json)} bytes — not stored]"}]
        )

    try:
        response_content = _render_capture_response(body.response, body.error)
    except Exception as exc:
        logger.warning("llm_capture_render_response_failed", error=str(exc))
        response_content = body.error or ""

    usage = (body.response or {}).get("usage") or {}
    input_tokens = _safe_int(usage.get("inputTokens"))
    output_tokens = _safe_int(usage.get("outputTokens"))
    total_tokens = _safe_int(usage.get("totalTokens")) or (input_tokens + output_tokens)
    reasoning_tokens = _safe_int(usage.get("reasoningTokens"))
    cache_read = _safe_int(usage.get("cachedInputTokens"))
    cache_creation = 0
    provider_meta = (body.response or {}).get("providerMetadata")
    if isinstance(provider_meta, dict):
        anthropic_meta = provider_meta.get("anthropic")
        if isinstance(anthropic_meta, dict):
            cache_creation = _safe_int(anthropic_meta.get("cacheCreationInputTokens"))

    latency_ms = max(0, _safe_int(body.completed_at_ms) - _safe_int(body.started_at_ms))

    async with async_session_maker() as db:
        existing = await db.get(LLMLog, body.log_id)
        if existing is not None:
            # Idempotent retry — keep the original row untouched.
            return {
                "log_id": existing.log_id,
                "iteration": existing.iteration,
                "conversation_id": existing.conversation_id,
                "idempotent": True,
            }

        if conversation_id is not None:
            max_iter = (await db.execute(
                select(func.max(LLMLog.iteration)).where(LLMLog.conversation_id == conversation_id)
            )).scalar()
            iteration = (max_iter + 1) if max_iter is not None else 0
        else:
            iteration = 0

        db.add(LLMLog(
            log_id=body.log_id,
            user_id=user_id,
            conversation_id=conversation_id,
            iteration=iteration,
            request_messages_json=request_json,
            response_content=(response_content or "")[:200000],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            reasoning_tokens=reasoning_tokens,
            model_provider=body.model_provider,
            model_name=body.model_name,
            latency_ms=latency_ms,
            error=body.error,
        ))
        await db.commit()

    logger.info(
        "llm_capture_persisted",
        log_id=body.log_id,
        conversation_id=conversation_id,
        iteration=iteration,
        model=body.model_name,
    )
    return {"log_id": body.log_id, "iteration": iteration, "conversation_id": conversation_id}
