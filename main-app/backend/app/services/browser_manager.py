"""
Browser Session Manager — persistent browser instances per conversation.

Each conversation gets at most one browser session (Playwright Chromium).
The auto_browser agent tool and the WebSocket endpoint share the
same browser, so navigation state persists across interactions.

Screenshot streaming:
  A background task captures JPEG screenshots at ~3 FPS and broadcasts
  them to all connected WebSocket clients as base64-encoded data.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

MAX_CONCURRENT_SESSIONS = 3
SCREENSHOT_INTERVAL = 0.33  # ~3 FPS
SCREENSHOT_QUALITY = 70
SCREENSHOT_WIDTH = 1280
SCREENSHOT_HEIGHT = 900
IDLE_TIMEOUT_SECONDS = 10 * 60  # 10 minutes


@dataclass
class BrowserSession:
    """A single browser instance tied to a conversation."""

    conversation_id: str
    browser: Any = None          # Playwright Browser instance
    context: Any = None          # Browser context
    page: Any = None             # Active page
    playwright: Any = None       # Playwright instance
    sandbox_session: Any = None  # llm-sandbox session (if sandboxed)
    websocket_clients: set = field(default_factory=set)
    screenshot_task: Optional[asyncio.Task] = None
    last_activity: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_url: str = "about:blank"
    current_title: str = ""
    is_navigating: bool = False

    def _run_browser_agent(self, action_json: str) -> dict:
        """Run browser_agent.py inside the sandbox container."""
        escaped = action_json.replace("'", "'\\''")
        result = self.sandbox_session.execute_command(
            f"python3 /opt/browser_agent.py '{escaped}'"
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            return {"ok": False, "error": result.stderr or "no output"}
        try:
            data = json.loads(stdout)
            if data.get("url"):
                self.current_url = data["url"]
            if data.get("title"):
                self.current_title = data["title"]
            return data
        except json.JSONDecodeError:
            return {"ok": False, "error": f"Invalid JSON: {stdout[:200]}"}

    def _read_screenshot_from_volume(self) -> Optional[str]:
        """Read screenshot directly from the shared volume (no SSH)."""
        try:
            from app.services.sandbox_manager import SANDBOX_BASE, get_user_for_conversation
            user_id = get_user_for_conversation(self.conversation_id)
            if not user_id:
                # Try any active session
                from app.services.sandbox_manager import _active_sessions
                for uid in _active_sessions:
                    user_id = uid
                    break
            if not user_id:
                return None
            screenshot_path = os.path.join(SANDBOX_BASE, user_id, ".browser-screenshot.jpg")
            if os.path.isfile(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    return base64.b64encode(f.read()).decode()
        except Exception:
            pass
        return None

    async def get_screenshot_base64(self) -> Optional[str]:
        """Read screenshot from shared volume (no SSH needed)."""
        if self.sandbox_session:
            # Read directly from disk — no podman exec, no SSH
            return await asyncio.to_thread(self._read_screenshot_from_volume)
        if not self.page:
            return None
        try:
            data = await self.page.screenshot(
                type="jpeg",
                quality=SCREENSHOT_QUALITY,
                full_page=False,
            )
            return base64.b64encode(data).decode("ascii")
        except Exception as exc:
            logger.warning("screenshot_failed", error=str(exc))
            return None

    async def _sandbox_action(self, action_data: dict) -> dict:
        """Run a browser action via the sandbox container."""
        result = await asyncio.to_thread(
            self._run_browser_agent, json.dumps(action_data)
        )
        # Read screenshot from shared volume (no SSH) and push to Browser tab
        if result.get("screenshot_file") and self.websocket_clients:
            try:
                screenshot_b64 = await asyncio.to_thread(self._read_screenshot_from_volume)
                if screenshot_b64:
                    message = json.dumps({
                        "type": "screenshot",
                        "data": screenshot_b64,
                        "url": result.get("url", self.current_url),
                        "title": result.get("title", self.current_title),
                    })
                    disconnected = set()
                    for ws in self.websocket_clients:
                        try:
                            await ws.send_text(message)
                        except Exception:
                            disconnected.add(ws)
                    self.websocket_clients -= disconnected
            except Exception:
                pass
        return result

    async def navigate(self, url: str) -> dict:
        """Navigate the page to a URL."""
        self.is_navigating = True
        self.last_activity = time.time()
        try:
            if self.sandbox_session:
                return await self._sandbox_action({"action": "navigate", "url": url})
            if not self.page:
                return {"error": "No browser page"}
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            self.is_navigating = False

    async def click(self, x: int, y: int) -> dict:
        """Click at page coordinates."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "click", "x": x, "y": y})
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.mouse.click(x, y)
            await asyncio.sleep(0.3)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}

    async def scroll(self, direction: str = "down", amount: int = 300) -> dict:
        """Scroll the page."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "scroll", "direction": direction, "amount": amount})
        if not self.page:
            return {"error": "No browser page"}
        try:
            delta = amount if direction == "down" else -amount
            await self.page.mouse.wheel(0, delta)
            return {"scrolled": direction}
        except Exception as exc:
            return {"error": str(exc)}

    async def type_text(self, text: str) -> dict:
        """Type text into the focused element."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "type", "text": text})
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.keyboard.type(text)
            return {"typed": len(text)}
        except Exception as exc:
            return {"error": str(exc)}

    async def press_key(self, key: str) -> dict:
        """Press a keyboard key (e.g., 'Enter', 'Tab')."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "press_key", "key": key})
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.keyboard.press(key)
            return {"pressed": key}
        except Exception as exc:
            return {"error": str(exc)}

    async def go_back(self) -> dict:
        """Navigate back in history."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "back"})
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=15000)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}

    async def go_forward(self) -> dict:
        """Navigate forward in history."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "forward"})
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.go_forward(wait_until="domcontentloaded", timeout=15000)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}

    async def refresh(self) -> dict:
        """Reload the current page."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "refresh"})
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=15000)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}


class BrowserManager:
    """Manages browser sessions keyed by conversation_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    async def get_or_create(self, conversation_id: str) -> BrowserSession:
        """Get an existing browser session or create a new one.

        Creates a lazy session — no host browser is launched until needed.
        If a sandbox session exists, uses that for all browser operations.
        If no sandbox and no browser, the screenshot loop waits gracefully.
        """
        async with self._lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                session.last_activity = time.time()
                return session

            if self.active_count >= MAX_CONCURRENT_SESSIONS:
                oldest_id = min(
                    self._sessions,
                    key=lambda k: self._sessions[k].last_activity,
                )
                await self._close_session_unlocked(oldest_id)

            # Create session WITHOUT launching a browser.
            # Browser will be attached later via attach_sandbox() when
            # the agent creates the sandbox container.
            session = BrowserSession(conversation_id=conversation_id)

            # Check if sandbox already exists (agent may have started first)
            try:
                from app.services.sandbox_manager import get_session_for_conversation
                sandbox = get_session_for_conversation(conversation_id)
                if sandbox:
                    session.sandbox_session = sandbox
                    logger.info("browser_using_sandbox", conversation_id=conversation_id)
            except Exception:
                pass

            session.screenshot_task = asyncio.create_task(
                self._screenshot_loop(session)
            )
            self._sessions[conversation_id] = session
            logger.info(
                "browser_session_created",
                conversation_id=conversation_id,
                sandboxed=session.sandbox_session is not None,
            )
            return session

    async def attach_sandbox(self, conversation_id: str, sandbox_session) -> None:
        """Attach a sandbox session to an existing browser session.

        Called by agent_service when the sandbox is created, so the
        Browser tab starts using the container's Chromium.
        """
        async with self._lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                # Close any existing host browser (shouldn't exist with lazy creation)
                if session.playwright:
                    try:
                        if session.page:
                            await session.page.close()
                        if session.context:
                            await session.context.close()
                        if session.browser:
                            await session.browser.close()
                        await session.playwright.stop()
                    except Exception:
                        pass
                    session.playwright = None
                    session.browser = None
                    session.context = None
                    session.page = None
                session.sandbox_session = sandbox_session
                logger.info("browser_sandbox_attached", conversation_id=conversation_id)
            else:
                # No browser session yet — create one with sandbox
                session = BrowserSession(
                    conversation_id=conversation_id,
                    sandbox_session=sandbox_session,
                )
                session.screenshot_task = asyncio.create_task(
                    self._screenshot_loop(session)
                )
                self._sessions[conversation_id] = session
                logger.info("browser_session_created_with_sandbox", conversation_id=conversation_id)


    async def get(self, conversation_id: str) -> Optional[BrowserSession]:
        """Get an existing session or None."""
        return self._sessions.get(conversation_id)

    async def close_session(self, conversation_id: str) -> None:
        """Close and cleanup a browser session."""
        async with self._lock:
            await self._close_session_unlocked(conversation_id)

    async def _close_session_unlocked(self, conversation_id: str) -> None:
        """Close session without acquiring the lock (caller holds it)."""
        session = self._sessions.pop(conversation_id, None)
        if session:
            await self._cleanup_session(session)
            logger.info(
                "browser_session_closed",
                conversation_id=conversation_id,
                active_sessions=self.active_count,
            )

    async def _cleanup_session(self, session: BrowserSession) -> None:
        """Cleanup all resources of a session."""
        if session.screenshot_task:
            session.screenshot_task.cancel()
            try:
                await session.screenshot_task
            except (asyncio.CancelledError, Exception):
                pass

        # Notify connected clients
        for ws in list(session.websocket_clients):
            try:
                await ws.send_json({"type": "closed", "message": "Browser session ended"})
            except Exception:
                pass
        session.websocket_clients.clear()

        try:
            if session.context:
                await session.context.close()
            if session.browser:
                await session.browser.close()
            if session.playwright:
                await session.playwright.stop()
        except Exception as exc:
            logger.warning("browser_cleanup_error", error=str(exc))

    async def _screenshot_loop(self, session: BrowserSession) -> None:
        """Background task: poll screenshot from shared volume and broadcast.

        Screenshots are read from disk (no SSH) — the container writes
        .browser-screenshot.jpg to the shared volume, and we read it
        directly from the host filesystem. Safe to poll frequently.
        """
        last_data = None
        while True:
            try:
                await asyncio.sleep(SCREENSHOT_INTERVAL)

                if not session.websocket_clients:
                    continue

                screenshot_b64 = await session.get_screenshot_base64()
                if not screenshot_b64:
                    continue

                # Skip if identical to last frame
                if screenshot_b64 == last_data:
                    continue
                last_data = screenshot_b64

                message = json.dumps({
                    "type": "screenshot",
                    "data": screenshot_b64,
                    "url": session.current_url,
                    "title": session.current_title,
                })

                disconnected = set()
                for ws in session.websocket_clients:
                    try:
                        await ws.send_text(message)
                    except Exception:
                        disconnected.add(ws)
                session.websocket_clients -= disconnected

            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

    async def push_screenshot(self, conversation_id: str) -> None:
        """Take a screenshot and broadcast to all WebSocket clients.

        Called after browser actions (navigate, click, etc.) instead
        of constant polling. Prevents SSH overload.
        """
        session = self._sessions.get(conversation_id)
        if not session or not session.websocket_clients:
            return

        try:
            screenshot_b64 = await session.get_screenshot_base64()
            if not screenshot_b64:
                return

            message = json.dumps({
                "type": "screenshot",
                "data": screenshot_b64,
                "url": session.current_url,
                "title": session.current_title,
            })

            disconnected = set()
            for ws in session.websocket_clients:
                try:
                    await ws.send_text(message)
                except Exception:
                    disconnected.add(ws)
            session.websocket_clients -= disconnected
        except Exception as exc:
            logger.warning("push_screenshot_error", error=str(exc))

    async def close_all(self) -> None:
        """Close all sessions (app shutdown)."""
        async with self._lock:
            for cid in list(self._sessions.keys()):
                await self._close_session_unlocked(cid)


# Singleton instance
browser_manager = BrowserManager()
