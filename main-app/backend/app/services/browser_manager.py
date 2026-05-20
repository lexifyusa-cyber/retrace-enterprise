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
# Max concurrent BrowserSessions (= Chromium tabs) per user. Beyond this the
# LRU page is evicted to keep memory under control. ~30-80 MB / Page so 5 = ~200-400 MB.
MAX_PAGES_PER_USER = 5


def _resolve_user_id_for_conversation(conversation_id: str) -> Optional[str]:
    """Best-effort lookup of `user_id` for a `conversation_id` from the
    in-memory map. Returns None for standalone (un-authenticated) browsing
    or conversations that haven't been registered yet — in which case the
    caller falls back to using the conversation_id itself as the Chromium
    pool key (= per-conversation isolated Chromium).

    A DB lookup here would deadlock (we're called while holding the manager's
    lock) so we only consult the synchronous in-memory map. Authenticated
    callers (the MCP HTTP bridge) pass `user_id` explicitly via the kwarg
    on `get_or_create`, bypassing this helper.
    """
    try:
        from app.services.sandbox_manager import get_user_for_conversation
        return get_user_for_conversation(conversation_id)
    except Exception:
        return None


@dataclass
class BrowserSession:
    """A single browser instance tied to a conversation."""

    conversation_id: str
    # user_id is set when the session is created so we know which user-level
    # Chromium it belongs to. For standalone (un-authenticated) browsing,
    # the WS handler may pass the conversation_id itself as the "user_id"
    # so it gets its own private Chromium.
    user_id: Optional[str] = None
    browser: Any = None          # Playwright Browser instance (shared from BrowserManager._user_browsers)
    context: Any = None          # Browser context (per-conversation; gives cookie isolation between conversations)
    page: Any = None             # Active page (per-conversation, stays alive across switches so state is preserved)
    playwright: Any = None       # Playwright instance (shared via BrowserManager._user_browsers — DO NOT close from a session)
    sandbox_session: Any = None  # llm-sandbox session — kept for back-compat; never populated in host-Playwright mode
    websocket_clients: set = field(default_factory=set)
    screenshot_task: Optional[asyncio.Task] = None
    last_activity: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_url: str = "about:blank"
    current_title: str = ""
    is_navigating: bool = False

    def _run_browser_agent(self, action_json: str) -> dict:
        """Run a browser action inside the sandbox container via browser_control.py
        (a thin HTTP client that forwards to the persistent browser_daemon.py).
        Injects conversation_id so the daemon uses the correct browser context."""
        # Inject conversation_id into the payload for multi-context isolation
        try:
            payload = json.loads(action_json)
            payload["conversation_id"] = self.conversation_id
            action_json = json.dumps(payload)
        except (json.JSONDecodeError, TypeError):
            pass
        escaped = action_json.replace("'", "'\\''")
        result = self.sandbox_session.execute_command(
            f"python3 /opt/browser_control.py '{escaped}'"
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
        """Read screenshot from per-conversation path on the shared volume."""
        try:
            from app.services.sandbox_manager import SANDBOX_BASE, get_user_for_conversation, _active_sessions
            user_id = get_user_for_conversation(self.conversation_id)
            if not user_id:
                # Fallback 1: try any active session
                for uid in _active_sessions:
                    user_id = uid
                    break
            if not user_id:
                # Fallback 2: look up user_id from conversations table in DB
                try:
                    import asyncio
                    from sqlalchemy import text as sa_text
                    from app.db.database import async_session_maker

                    async def _lookup():
                        async with async_session_maker() as session:
                            result = await session.execute(
                                sa_text("SELECT user_id FROM conversations WHERE conversation_id = :cid"),
                                {"cid": self.conversation_id},
                            )
                            row = result.fetchone()
                            return row[0] if row else None

                    # We're in a thread (called via asyncio.to_thread), so run a new loop
                    user_id = asyncio.run(_lookup())
                except Exception:
                    pass
            if not user_id:
                # Fallback 3: scan SANDBOX_BASE for matching conversation subdirectory
                try:
                    for entry in os.listdir(SANDBOX_BASE):
                        candidate = os.path.join(SANDBOX_BASE, entry, self.conversation_id, ".browser-screenshot.jpg")
                        if os.path.isfile(candidate):
                            user_id = entry
                            break
                except Exception:
                    pass
            if not user_id:
                return None
            # Try per-conversation path first, then volume root (Fly.io writes to root)
            for _ss_candidate in [
                os.path.join(SANDBOX_BASE, user_id, self.conversation_id, ".browser-screenshot.jpg"),
                os.path.join(SANDBOX_BASE, user_id, ".browser-screenshot.jpg"),
            ]:
                if os.path.isfile(_ss_candidate):
                    with open(_ss_candidate, "rb") as f:
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
        """Navigate the page to a URL. Waits for `domcontentloaded` plus a
        short 500 ms paint settle so the screenshot pushed after this call
        captures the new page, not a half-rendered intermediate.
        """
        self.is_navigating = True
        self.last_activity = time.time()
        try:
            if self.sandbox_session:
                return await self._sandbox_action({"action": "navigate", "url": url})
            if not self.page:
                return {"error": "No browser page"}
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Paint settle. Matches the pre-OC auto_browser sleep before the
            # screenshot push so the panel sees the new page, not a frame
            # from while it was still rendering.
            await asyncio.sleep(0.5)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            self.is_navigating = False

    async def click(self, x: int, y: int) -> dict:
        """Click at page coordinates. Waits for any triggered navigation +
        settles for paint, same as `click_selector`."""
        self.last_activity = time.time()
        if self.sandbox_session:
            return await self._sandbox_action({"action": "click", "x": x, "y": y})
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.mouse.click(x, y)
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.5)
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
            await asyncio.sleep(0.5)
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
            await asyncio.sleep(0.5)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}

    # ─── Selector-based actions (used by the agent's MCP browser tools) ───
    # The WebSocket / human-driven path is coordinate-based (`click(x, y)`),
    # but the agent reasons in CSS selectors, so add selector-based variants
    # for the MCP HTTP bridge to dispatch to.

    async def click_selector(self, selector: str) -> dict:
        """Click the first element matching a CSS selector.

        Critical: when the click triggers a same-tab navigation (e.g. clicking
        a `<a href>` or a submit button), `page.click()` returns BEFORE the
        navigation starts loading. We wait for `domcontentloaded` post-click
        with a short tolerance — if no navigation happens, `wait_for_load_state`
        returns immediately. Then a 500 ms settle for paint. This matches what
        pre-OC `auto_browser` did and is what guarantees the screenshot pushed
        after the click captures the post-navigation page.
        """
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.click(selector, timeout=10000)
            # Wait for any navigation the click triggered to finish. No-op
            # when the click didn't trigger one. Short timeout because most
            # SPA-style clicks don't navigate at all and we shouldn't dangle.
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title, "selector": selector}
        except Exception as exc:
            return {"error": str(exc), "selector": selector}

    async def type_in_selector(self, selector: str, text: str) -> dict:
        """Fill a form field by CSS selector. Uses Playwright's `fill` (which
        clears the field first) rather than `type` so the agent doesn't have
        to worry about pre-existing input."""
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.fill(selector, text, timeout=10000)
            return {"selector": selector, "text": text}
        except Exception as exc:
            return {"error": str(exc), "selector": selector}

    async def vision_click(self, description: str) -> dict:
        """Click an element by visual description. Mirrors the pre-OC
        `auto_browser(action='vision_click')` path:

          1. Capture a JPEG screenshot of the page (in-browser, not the host
             desktop).
          2. POST to the configured coordinate-finder API (screenops) with
             the description; receive `{x, y}` in viewport pixels.
          3. `page.mouse.click(x, y)` + wait for any triggered navigation.

        Requires `screenops_api_key` (+ optionally api_url + model) in the
        user's active LLM settings. Without it the call returns a helpful
        error so the LLM falls back to `click(selector)` instead of crashing.
        """
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        # Fetch screenops settings on demand. We don't cache the invoker on
        # `self` because settings can change between turns (user updates the
        # API key in Settings); this is one DB read per vision_click call.
        try:
            from app.api.settings import get_active_llm_settings
            from app.db.database import async_session_maker
            async with async_session_maker() as db:
                llm = await get_active_llm_settings(db)
        except Exception as exc:
            logger.warning("vision_click_settings_fetch_failed", error=str(exc))
            llm = {}
        api_key = (llm.get("screenops_api_key") or "").strip()
        if not api_key:
            return {
                "error": (
                    "vision_click requires a coordinate-finder API. Set "
                    "`screenops_api_key` in Settings, or use click(selector)."
                ),
            }
        api_url = (llm.get("screenops_api_url") or "").strip()
        model = (llm.get("screenops_model") or "gpt-4o-mini").strip()

        try:
            from app.tools.screenops.tool import _make_coordinate_finder_invoker
        except Exception as exc:
            return {"error": f"screenops import failed: {exc}"}

        try:
            b64 = await self.get_screenshot_base64()
            if not b64:
                return {"error": "Screenshot capture failed for vision_click"}
            vp = self.page.viewport_size or {"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT}
            vp_w, vp_h = int(vp["width"]), int(vp["height"])
            invoker = _make_coordinate_finder_invoker(api_url, model, api_key)
            # `invoker` is sync (uses httpx blocking) so push it to a worker
            # thread to avoid stalling the event loop while the VLM thinks.
            coords, _usage = await asyncio.to_thread(
                invoker, b64, description, vp_w, vp_h, vp_w, vp_h,
            )
            if not coords or coords.get("x") is None or coords.get("y") is None:
                return {
                    "error": (
                        f"Could not find element matching '{description}'. "
                        "Try a more specific description, or use click(selector)."
                    ),
                }
            cx, cy = int(coords["x"]), int(coords["y"])
            await self.page.mouse.click(cx, cy)
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {
                "clicked": {"x": cx, "y": cy},
                "target": description,
                "url": self.current_url,
                "title": self.current_title,
            }
        except Exception as exc:
            return {"error": f"vision_click failed: {exc}", "target": description}

    async def evaluate_js(self, expression: str) -> dict:
        """Run a JavaScript expression in the page context, return its result.

        The DOM escape hatch — when no other action covers what's needed
        (shadow DOM traversal, computed style probing, custom-event dispatch,
        scraping a non-text attribute), the agent uses this. Returns the
        value coerced to a string, truncated to 8000 chars to match read_page.
        """
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            result = await self.page.evaluate(expression)
            return {"result": (str(result) if result is not None else "")[:8000]}
        except Exception as exc:
            return {"error": str(exc)}

    async def select_option(self, selector: str, value: str) -> dict:
        """Pick a `<select>` option by value or visible label.

        Native `<select>` elements ignore mouse clicks for the dropdown
        items — Playwright's `select_option` is the only correct way.
        Accepts either `value="opt_id"` or the visible label.
        """
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            await self.page.select_option(selector, value, timeout=10000)
            return {"selector": selector, "value": value}
        except Exception as exc:
            return {"error": str(exc), "selector": selector}

    async def hover(self, selector: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        """Hover an element by selector, or move the cursor to coordinates.

        Needed for hover-revealed menus, tooltips, and any UI that only
        renders on `mouseenter`. Either `selector` or both `x` and `y` must
        be provided.
        """
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            if selector:
                await self.page.hover(selector, timeout=10000)
                await asyncio.sleep(0.3)
                return {"hovered": selector}
            if x is not None and y is not None:
                await self.page.mouse.move(int(x), int(y))
                await asyncio.sleep(0.3)
                return {"hovered": f"({x},{y})"}
            return {"error": "hover requires selector OR (x,y)"}
        except Exception as exc:
            return {"error": str(exc)}

    async def wait(self, seconds: float) -> dict:
        """Pause for a number of seconds (clamped to [0.1, 10])."""
        self.last_activity = time.time()
        t = max(0.1, min(float(seconds), 10.0))
        await asyncio.sleep(t)
        return {"waited": t}

    async def get_url_info(self) -> dict:
        """Return the current URL and page title without any side effects."""
        if not self.page:
            return {"error": "No browser page"}
        try:
            self.current_url = self.page.url
            self.current_title = await self.page.title()
            return {"url": self.current_url, "title": self.current_title}
        except Exception as exc:
            return {"error": str(exc)}

    async def screenshot_full_page(self, max_tiles: int = 10) -> dict:
        """Scroll the page top-to-bottom, capture viewport-sized JPEG tiles,
        return them all base64-encoded. The MCP wrapper bundles them into a
        single vision call so the LLM sees a tall page at once.

        Capped at `max_tiles` to bound vision token cost.
        """
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            from app.tools.token_analyzer import calculate_screenshot_count
            page_height = await self.page.evaluate("document.body.scrollHeight")
            num_tiles = min(calculate_screenshot_count(page_height, SCREENSHOT_HEIGHT), max_tiles)
            await self.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.3)
            tiles: list[str] = []
            for i in range(num_tiles):
                await self.page.evaluate(f"window.scrollTo(0, {i * SCREENSHOT_HEIGHT})")
                await asyncio.sleep(0.25)
                b64 = await self.get_screenshot_base64()
                if b64:
                    tiles.append(b64)
            await self.page.evaluate("window.scrollTo(0, 0)")
            return {
                "tiles": tiles,
                "num_tiles": len(tiles),
                "page_height_px": int(page_height),
                "viewport_height_px": SCREENSHOT_HEIGHT,
                "url": self.page.url,
                "title": await self.page.title(),
            }
        except Exception as exc:
            return {"error": str(exc)}

    async def analyze_page(self) -> dict:
        """Return token-cost analysis for the three extraction strategies
        (read_page / single screenshot / multi-tile). Lets the LLM pick the
        cheapest path before committing to the full extraction call.
        """
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            from app.tools.analysis import analyze_extraction_options
            text = await self.page.eval_on_selector("body", "(el) => el.innerText")
            page_height = await self.page.evaluate("document.body.scrollHeight")
            options = analyze_extraction_options(text or "", int(page_height))
            return {
                "url": self.page.url,
                "page_height_px": int(page_height),
                "text_chars": len(text or ""),
                "options": [
                    {
                        "approach": o.approach,
                        "total_tokens": o.total_tokens,
                        "num_screenshots": o.num_screenshots,
                        "description": o.description,
                    }
                    for o in options
                ],
            }
        except Exception as exc:
            return {"error": str(exc)}

    async def read_page(self, selector: Optional[str] = None) -> dict:
        """Extract visible text from the page, optionally scoped to a selector.
        Truncates to 8000 chars to keep LLM context usage reasonable."""
        self.last_activity = time.time()
        if not self.page:
            return {"error": "No browser page"}
        try:
            if selector:
                # Use innerText to skip hidden elements + collapse whitespace.
                text = await self.page.eval_on_selector(
                    selector,
                    "(el) => el.innerText",
                )
            else:
                text = await self.page.eval_on_selector(
                    "body",
                    "(el) => el.innerText",
                )
            text = (text or "")[:8000]
            return {"text": text, "url": self.page.url, "title": await self.page.title()}
        except Exception as exc:
            return {"error": str(exc), "selector": selector}


class BrowserManager:
    """Manages browser sessions keyed by conversation_id.

    Per-user model: each user has at most ONE Chromium instance, shared across
    all that user's conversations. Each conversation gets its own Playwright
    `Context` (cookie-isolated) + one `Page` inside it. Pages stay alive when
    the user switches conversations, so scroll position / form state /
    in-flight JS are preserved (matches the UX of "switch conversations and
    see what you left behind").

    The per-user Chromium is launched lazily on the first conversation that
    needs it (`_ensure_user_browser`) and torn down when its ref-count hits
    zero (last conversation closed). Each user can have up to
    MAX_PAGES_PER_USER concurrent Pages — LRU-evict beyond that.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        # user_id -> { playwright, browser, ref_count, launched_at }
        # ref_count = number of BrowserSessions whose .browser points at this entry.
        self._user_browsers: dict[str, dict] = {}
        # Locks for per-user launch races: avoids two concurrent get_or_create
        # calls launching two Chromiums for the same user. Lazy keyed.
        self._user_locks: dict[str, asyncio.Lock] = {}

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def _user_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        return lock

    async def _ensure_user_browser(self, user_id: str) -> tuple[Any, Any]:
        """Return (playwright, browser) for `user_id`, launching Chromium on
        first call. Concurrency-safe via per-user lock. Bumps ref_count.

        Resurrects the host-Playwright path that was deleted in commit fa666e7
        ("Remove _ensure_host_browser — guarantees single browser in container")
        — except this time it's keyed per user instead of per conversation,
        so multiple conversations for the same user share one Chromium.
        """
        async with self._user_lock(user_id):
            entry = self._user_browsers.get(user_id)
            if entry and entry.get("browser"):
                entry["ref_count"] = entry.get("ref_count", 0) + 1
                return entry["playwright"], entry["browser"]

            from playwright.async_api import async_playwright
            try:
                pw = await async_playwright().start()
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
            except Exception as exc:
                logger.error("browser_launch_failed", user_id=user_id, error=str(exc))
                raise

            self._user_browsers[user_id] = {
                "playwright": pw,
                "browser": browser,
                "ref_count": 1,
                "launched_at": time.time(),
            }
            logger.info("user_browser_launched", user_id=user_id)
            return pw, browser

    async def _release_user_browser(self, user_id: str) -> None:
        """Decrement ref_count for user_id's Chromium. Closes the browser when
        no more sessions reference it. Called from `_cleanup_session`."""
        async with self._user_lock(user_id):
            entry = self._user_browsers.get(user_id)
            if not entry:
                return
            entry["ref_count"] = max(0, entry.get("ref_count", 1) - 1)
            if entry["ref_count"] > 0:
                return
            # No more sessions for this user — close Chromium to free memory.
            try:
                if entry.get("browser"):
                    await entry["browser"].close()
            except Exception as exc:
                logger.warning("user_browser_close_failed", user_id=user_id, error=str(exc))
            try:
                if entry.get("playwright"):
                    await entry["playwright"].stop()
            except Exception as exc:
                logger.warning("user_playwright_stop_failed", user_id=user_id, error=str(exc))
            self._user_browsers.pop(user_id, None)
            logger.info("user_browser_closed", user_id=user_id)

    async def get_or_create(self, conversation_id: str, user_id: Optional[str] = None) -> BrowserSession:
        """Get an existing browser session or create a new one.

        Launches the user's Chromium on first call (lazy) and creates a new
        Playwright Context+Page off it for this conversation. Each conversation
        gets its own Context so cookies / localStorage are isolated — but all
        Contexts share one Chromium per user.

        `user_id` is required for multi-user isolation. If not provided, we
        try to look it up from the sandbox_manager mapping (set when the
        OC session was created) or from the conversations DB row. As a last
        resort, we use the conversation_id itself as the user_id — that gives
        an isolated per-conversation Chromium (heavier but correct).
        """
        async with self._lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                session.last_activity = time.time()
                return session

            # Resolve user_id if the caller didn't supply one.
            resolved_user_id = user_id or _resolve_user_id_for_conversation(conversation_id)

            # Per-user LRU: enforce MAX_PAGES_PER_USER on user-owned sessions.
            # Standalone (no user_id) sessions skip per-user eviction and fall
            # through to the global cap below.
            if resolved_user_id:
                user_sessions = [
                    sid for sid, s in self._sessions.items()
                    if s.user_id == resolved_user_id
                ]
                if len(user_sessions) >= MAX_PAGES_PER_USER:
                    oldest_id = min(
                        user_sessions,
                        key=lambda k: self._sessions[k].last_activity,
                    )
                    logger.info("evicting_lru_user_page", user_id=resolved_user_id, evicted=oldest_id)
                    await self._close_session_unlocked(oldest_id)

            # Global cap as a safety net (across all users).
            if self.active_count >= MAX_CONCURRENT_SESSIONS * 5:
                oldest_id = min(
                    self._sessions,
                    key=lambda k: self._sessions[k].last_activity,
                )
                await self._close_session_unlocked(oldest_id)

            session = BrowserSession(conversation_id=conversation_id, user_id=resolved_user_id)

            # Launch (or share) the user's Chromium, then mint a per-conversation
            # Context+Page off it. We use a new Context (not just a new Page)
            # because each conversation should have its own cookie jar — logging
            # into Gmail in conv A doesn't pollute conv B.
            browser_key = resolved_user_id or f"standalone:{conversation_id}"
            try:
                pw, browser = await self._ensure_user_browser(browser_key)
                session.playwright = pw
                session.browser = browser
                session.context = await browser.new_context(
                    viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                session.page = await session.context.new_page()
                logger.info(
                    "browser_session_created",
                    conversation_id=conversation_id,
                    user_id=resolved_user_id,
                    browser_key=browser_key,
                )
            except Exception as exc:
                # Browser launch failed — fall back to a sandbox-less,
                # browser-less session. The screenshot loop will be a no-op
                # and any action will return an error. We still register the
                # session so the WS gets a sensible "error" status.
                logger.error("browser_session_create_failed", conversation_id=conversation_id, error=str(exc))

            session.screenshot_task = asyncio.create_task(
                self._screenshot_loop(session)
            )
            self._sessions[conversation_id] = session
            return session

    async def attach_sandbox(self, conversation_id: str, sandbox_session) -> None:
        """Legacy hook from the pre-OC AgentService era. Now a no-op.

        Prior to commit fa666e7, this swapped a host-Playwright BrowserSession
        for a sandbox-backed one. Post-OC migration, all sessions are host-
        Playwright (the sandbox-browser path is dead). Kept as a no-op so any
        leftover caller fails open instead of throwing AttributeError.
        """
        logger.warning(
            "browser_manager.attach_sandbox is deprecated and now a no-op "
            "— host Playwright handles everything; sandbox path is dead",
            conversation_id=conversation_id,
        )


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
        """Cleanup all resources of a session. The Playwright `browser` +
        `playwright` are SHARED via `_user_browsers` — DO NOT close them
        here. We only close the per-conversation Page+Context and decrement
        the ref-count via `_release_user_browser`. Closing the shared Browser
        would tear down every other conversation for that user.
        """
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

        # Close ONLY the per-conversation Page + Context. Leave the shared
        # Browser+Playwright alone — `_release_user_browser` decides if it's
        # safe to fully close them based on ref_count.
        try:
            if session.page:
                await session.page.close()
            if session.context:
                await session.context.close()
        except Exception as exc:
            logger.warning("browser_cleanup_error", error=str(exc))

        # Release the ref on the user-level Chromium. Closes it iff this was
        # the last conversation using it.
        browser_key = session.user_id or f"standalone:{session.conversation_id}"
        try:
            await self._release_user_browser(browser_key)
        except Exception as exc:
            logger.warning("browser_release_error", error=str(exc))

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
