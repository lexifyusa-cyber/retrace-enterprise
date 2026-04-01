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
    websocket_clients: set = field(default_factory=set)
    screenshot_task: Optional[asyncio.Task] = None
    last_activity: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_url: str = "about:blank"
    current_title: str = ""
    is_navigating: bool = False

    async def get_screenshot_base64(self) -> Optional[str]:
        """Capture current page as base64 JPEG."""
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

    async def navigate(self, url: str) -> dict:
        """Navigate the page to a URL."""
        if not self.page:
            return {"error": "No browser page"}
        self.is_navigating = True
        self.last_activity = time.time()
        try:
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
        if not self.page:
            return {"error": "No browser page"}
        self.last_activity = time.time()
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
        if not self.page:
            return {"error": "No browser page"}
        self.last_activity = time.time()
        try:
            delta = amount if direction == "down" else -amount
            await self.page.mouse.wheel(0, delta)
            return {"scrolled": direction}
        except Exception as exc:
            return {"error": str(exc)}

    async def type_text(self, text: str) -> dict:
        """Type text into the focused element."""
        if not self.page:
            return {"error": "No browser page"}
        self.last_activity = time.time()
        try:
            await self.page.keyboard.type(text)
            return {"typed": len(text)}
        except Exception as exc:
            return {"error": str(exc)}

    async def press_key(self, key: str) -> dict:
        """Press a keyboard key (e.g., 'Enter', 'Tab')."""
        if not self.page:
            return {"error": "No browser page"}
        self.last_activity = time.time()
        try:
            await self.page.keyboard.press(key)
            return {"pressed": key}
        except Exception as exc:
            return {"error": str(exc)}

    async def go_back(self) -> dict:
        """Navigate back in history."""
        if not self.page:
            return {"error": "No browser page"}
        self.last_activity = time.time()
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=15000)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}

    async def go_forward(self) -> dict:
        """Navigate forward in history."""
        if not self.page:
            return {"error": "No browser page"}
        self.last_activity = time.time()
        try:
            await self.page.go_forward(wait_until="domcontentloaded", timeout=15000)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}

    async def refresh(self) -> dict:
        """Reload the current page."""
        if not self.page:
            return {"error": "No browser page"}
        self.last_activity = time.time()
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
        """Get an existing browser session or create a new one."""
        async with self._lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                session.last_activity = time.time()
                return session

            if self.active_count >= MAX_CONCURRENT_SESSIONS:
                # Close the oldest idle session
                oldest_id = min(
                    self._sessions,
                    key=lambda k: self._sessions[k].last_activity,
                )
                await self._close_session_unlocked(oldest_id)

            session = BrowserSession(conversation_id=conversation_id)
            try:
                from playwright.async_api import async_playwright

                session.playwright = await async_playwright().start()
                session.browser = await session.playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                session.context = await session.browser.new_context(
                    viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                session.page = await session.context.new_page()
                session.screenshot_task = asyncio.create_task(
                    self._screenshot_loop(session)
                )
                self._sessions[conversation_id] = session
                logger.info(
                    "browser_session_created",
                    conversation_id=conversation_id,
                    active_sessions=self.active_count,
                )
                return session
            except Exception as exc:
                logger.error("browser_session_create_failed", error=str(exc))
                await self._cleanup_session(session)
                raise

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
        """Background task: capture and broadcast screenshots."""
        last_data = None
        while True:
            try:
                await asyncio.sleep(SCREENSHOT_INTERVAL)

                if not session.websocket_clients:
                    continue  # No one watching, skip capture

                screenshot_b64 = await session.get_screenshot_base64()
                if not screenshot_b64:
                    continue

                # Skip if identical to last frame (no change)
                if screenshot_b64 == last_data:
                    continue
                last_data = screenshot_b64

                # Update page info
                if session.page:
                    try:
                        session.current_url = session.page.url
                        session.current_title = await session.page.title()
                    except Exception:
                        pass

                message = json.dumps({
                    "type": "screenshot",
                    "data": screenshot_b64,
                    "url": session.current_url,
                    "title": session.current_title,
                })

                # Broadcast to all clients
                disconnected = set()
                for ws in session.websocket_clients:
                    try:
                        await ws.send_text(message)
                    except Exception:
                        disconnected.add(ws)
                session.websocket_clients -= disconnected

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("screenshot_loop_error", error=str(exc))
                await asyncio.sleep(1)

    async def close_all(self) -> None:
        """Close all sessions (app shutdown)."""
        async with self._lock:
            for cid in list(self._sessions.keys()):
                await self._close_session_unlocked(cid)


# Singleton instance
browser_manager = BrowserManager()
