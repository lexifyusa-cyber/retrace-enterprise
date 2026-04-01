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

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.browser_manager import browser_manager

logger = structlog.get_logger()
router = APIRouter()


@router.websocket("/ws/browser/{conversation_id}")
async def browser_websocket(websocket: WebSocket, conversation_id: str):
    """Bidirectional browser I/O over WebSocket."""
    await websocket.accept()

    try:
        session = await browser_manager.get_or_create(conversation_id)
    except Exception as exc:
        await websocket.send_json({"type": "error", "data": str(exc)})
        await websocket.close()
        return

    # Register this client for screenshot broadcasts
    session.websocket_clients.add(websocket)

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
            else:
                result = {"error": f"Unknown message type: {msg_type}"}

            await websocket.send_json({"type": "action_result", "result": result})

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
