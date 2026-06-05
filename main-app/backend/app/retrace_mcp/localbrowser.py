"""
ReTrace Local Browser MCP — opens a visible Chrome window on the user's
machine and lets the agent drive it.

Unlike `retrace-browser`, this MCP doesn't talk to ReTrace's backend, doesn't
need a conversation_id, and doesn't pipe screenshots to a web panel. The
browser window is a real visible Chrome on the local desktop — the user sees
it directly. Designed for TUI usage where there's no panel to display in.

Persistent user-data dir at ~/.cache/retrace-localbrowser/<user_id>/ so logins
and cookies survive across MCP restarts.

Tools:
  - navigate(url)
  - click(selector_or_text)
  - type_text(selector, text)
  - press_key(key)
  - read_page()
  - screenshot(path?)
  - scroll(direction, amount?)
  - go_back() / go_forward()
  - close_browser()

NOTE: uses Playwright's *async* API because FastMCP runs tools inside an
asyncio event loop — sync_api raises "you can't use sync_api inside an
asyncio loop" if invoked here.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Playwright,
    Error as PWError,
)

mcp = FastMCP("retrace-localbrowser")

_USER_ID = os.environ.get("RETRACE_USER_ID", "default")
_USER_DATA_DIR = Path.home() / ".cache" / "retrace-localbrowser" / _USER_ID
_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Singleton Playwright state. Module-level so it persists across tool calls.
_pw: Optional[Playwright] = None
_ctx: Optional[BrowserContext] = None
_page: Optional[Page] = None
_lock = asyncio.Lock()
_NAV_TIMEOUT_MS = 30_000


def _alive() -> bool:
    """True if browser context + page are still usable."""
    if _ctx is None or _page is None:
        return False
    try:
        return not _page.is_closed()
    except Exception:
        return False


async def _shutdown_quiet() -> None:
    """Best-effort teardown of context + playwright handles."""
    global _pw, _ctx, _page
    try:
        if _ctx is not None:
            await _ctx.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _pw = None
    _ctx = None
    _page = None


async def _ensure() -> Page:
    """Launch (or re-launch) the visible Chrome and return its page."""
    global _pw, _ctx, _page
    async with _lock:
        if _alive():
            return _page  # type: ignore[return-value]
        await _shutdown_quiet()
        _pw = await async_playwright().start()
        # `launch_persistent_context` keeps cookies/login between runs.
        # headless=False so the user actually sees the window.
        _ctx = await _pw.chromium.launch_persistent_context(
            user_data_dir=str(_USER_DATA_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()
        _page.set_default_timeout(_NAV_TIMEOUT_MS)
        return _page


async def _summary(page: Page) -> str:
    """One-line state summary returned with every action."""
    try:
        title = await page.title()
        return f"url={page.url} | title={title[:80]}"
    except Exception:
        return f"url={page.url}"


@mcp.tool()
async def navigate(url: str) -> str:
    """Open the given URL in the visible local Chrome window. Reuses the
    same browser/tab across calls. Returns final URL + page title."""
    page = await _ensure()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except PWError as e:
        return f"Navigation error: {e}\n{await _summary(page)}"
    return f"Navigated. {await _summary(page)}"


@mcp.tool()
async def click(selector_or_text: str) -> str:
    """Click an element. Tries (in order): visible text → role-based → CSS
    selector. Pass natural text like "Sign in" or a CSS selector like "#submit".
    """
    page = await _ensure()
    # 1. Try by visible text — most LLM-friendly.
    try:
        loc = page.get_by_text(selector_or_text, exact=False).first
        await loc.click(timeout=4_000)
        return f"Clicked text='{selector_or_text}'. {await _summary(page)}"
    except PWError:
        pass
    # 2. Try role-based for buttons/links.
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role, name=selector_or_text).first  # type: ignore[arg-type]
            await loc.click(timeout=2_000)
            return f"Clicked role={role} name='{selector_or_text}'. {await _summary(page)}"
        except PWError:
            continue
    # 3. Fall back to raw CSS selector.
    try:
        await page.click(selector_or_text, timeout=4_000)
        return f"Clicked selector='{selector_or_text}'. {await _summary(page)}"
    except PWError as e:
        return f"Click failed for '{selector_or_text}': {e}\n{await _summary(page)}"


@mcp.tool()
async def type_text(selector: str, text: str) -> str:
    """Type into an input field. `selector` can be a CSS selector, an input
    placeholder, or visible label text. Clears any existing value first."""
    page = await _ensure()
    try:
        # Try by placeholder/label first — friendlier for LLMs.
        try:
            await page.get_by_placeholder(selector).fill(text, timeout=3_000)
            return f"Typed into placeholder='{selector}'. {await _summary(page)}"
        except PWError:
            pass
        try:
            await page.get_by_label(selector).fill(text, timeout=3_000)
            return f"Typed into label='{selector}'. {await _summary(page)}"
        except PWError:
            pass
        # CSS fallback.
        await page.fill(selector, text, timeout=4_000)
        return f"Typed into selector='{selector}'. {await _summary(page)}"
    except PWError as e:
        return f"Type failed for '{selector}': {e}\n{await _summary(page)}"


@mcp.tool()
async def press_key(key: str) -> str:
    """Press a keyboard key on the focused element. Common keys: 'Enter',
    'Tab', 'Escape', 'ArrowDown', 'ArrowUp'. Use this to submit forms after
    type_text (e.g., press_key('Enter'))."""
    page = await _ensure()
    try:
        await page.keyboard.press(key)
        return f"Pressed '{key}'. {await _summary(page)}"
    except PWError as e:
        return f"Key press failed: {e}"


@mcp.tool()
async def read_page() -> str:
    """Return the visible text content of the current page (truncated to 8K
    chars), the URL, and the title. Use this to see what's on screen."""
    page = await _ensure()
    try:
        text = await page.inner_text("body")
    except PWError:
        text = ""
    text = (text or "").strip()
    if len(text) > 8_000:
        text = text[:8_000] + "\n…[truncated]"
    title = ""
    try:
        title = await page.title()
    except Exception:
        pass
    return f"URL: {page.url}\nTitle: {title}\n\n{text}"


@mcp.tool()
async def screenshot(path: Optional[str] = None) -> str:
    """Save a PNG screenshot of the visible page. If `path` is omitted,
    saves to /tmp/retrace-localbrowser-<timestamp>.png. Returns the path."""
    page = await _ensure()
    if not path:
        path = f"/tmp/retrace-localbrowser-{int(time.time() * 1000)}.png"
    try:
        await page.screenshot(path=path)
        return f"Screenshot saved to {path}"
    except PWError as e:
        return f"Screenshot failed: {e}"


@mcp.tool()
async def scroll(direction: str = "down", amount: int = 600) -> str:
    """Scroll the page. `direction` is 'down' or 'up'. `amount` is pixels."""
    page = await _ensure()
    delta = amount if direction == "down" else -amount
    try:
        await page.mouse.wheel(0, delta)
        return f"Scrolled {direction} {abs(delta)}px. {await _summary(page)}"
    except PWError as e:
        return f"Scroll failed: {e}"


@mcp.tool()
async def go_back() -> str:
    """Navigate back to the previous page (browser back button)."""
    page = await _ensure()
    try:
        await page.go_back(wait_until="domcontentloaded")
    except PWError as e:
        return f"go_back failed: {e}"
    return f"Went back. {await _summary(page)}"


@mcp.tool()
async def go_forward() -> str:
    """Navigate forward (browser forward button)."""
    page = await _ensure()
    try:
        await page.go_forward(wait_until="domcontentloaded")
    except PWError as e:
        return f"go_forward failed: {e}"
    return f"Went forward. {await _summary(page)}"


@mcp.tool()
async def close_browser() -> str:
    """Close the visible browser window. Next call to any tool will open
    a fresh one."""
    await _shutdown_quiet()
    return "Browser closed."


if __name__ == "__main__":
    mcp.run()
