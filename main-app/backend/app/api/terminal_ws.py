"""
WebSocket endpoint for interactive terminal sessions.

Protocol (JSON messages):
  Client -> Server:
    {"type": "input",  "data": "ls -la\\r\\n"}    -- keystrokes / pasted text
    {"type": "resize", "cols": 120, "rows": 40}   -- terminal resize

  Server -> Client:
    {"type": "scrollback", "data": "..."}          -- buffered history on connect
    {"type": "output",     "data": "..."}          -- live shell output
    {"type": "exited",     "code": <int>}          -- shell process ended
"""

import asyncio

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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

    # Send buffered scrollback so the user sees prior output
    if session.scrollback:
        try:
            await websocket.send_json({
                "type": "scrollback",
                "data": bytes(session.scrollback).decode("utf-8", errors="replace"),
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
