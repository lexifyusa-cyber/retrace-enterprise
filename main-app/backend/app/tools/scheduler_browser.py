"""
Scheduler dispatch for browser / screenops tools.

The post-OC interactive agent reaches `retrace-browser_*` and
`retrace-screenops_*` capabilities via OpenCode → MCP server → backend
internal endpoint → `BrowserSession`. The scheduler can't go through
OC (no LLM in the loop, deterministic step execution), so this module
gives it direct access to the SAME backend code OC's MCP servers
eventually call. No HTTP round-trip — we're in the same Python
process — but we follow the same `BrowserSession` contract so behavior
matches the chat agent step-for-step.

Each `dispatch_*` function:
  1. Get-or-creates a per-automation `BrowserSession` keyed by the run's
     synthetic `conversation_id` (`automation-{run_id}`). One Chromium
     context per run; fresh cookies each run for reproducibility.
  2. Registers it in `_active_browser_conv` so any in-process bridge
     callers (e.g., MCP servers spawned by OC for parallel chat
     sessions) don't get cross-talk during the run.
  3. Calls the corresponding `BrowserSession` method.
  4. Formats the dict result back into the plain string the scheduler's
     step output log expects.

For Phase 1 the conversation_id is run-scoped — each automation run
gets its own browser session, no state shared across runs. If a future
SOP wants persistent login state, change `make_conv_id` to be SOP-
scoped (`automation-{sop_id}`).
"""
from __future__ import annotations

import json
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


def make_conv_id(run_id: str) -> str:
    """Synthetic conversation id used for the automation's browser session."""
    return f"automation-{run_id}"


async def _ensure_session(user_id: Optional[str], conv_id: str):
    """Get-or-create a BrowserSession for the automation run.

    Sets `_active_browser_conv[user_id]` so the internal browser bridge
    (used by parallel MCP servers, if any) routes to the same session.
    Returns the BrowserSession or None if user_id is missing.
    """
    if not user_id:
        return None
    from app.services.browser_manager import browser_manager
    from app.api.agent import _active_browser_conv

    _active_browser_conv[user_id] = conv_id
    session = await browser_manager.get_or_create(conv_id, user_id=user_id)
    return session


def _format(result: Any) -> str:
    """Render a BrowserSession method's return value into the step log
    text the scheduler's existing output formatting expects."""
    if isinstance(result, dict):
        if "error" in result:
            return f"Error: {result['error']}"
        # Strip the auto-attached `screenshot` field (huge base64 PNG) from
        # the log line — it bloats the row and we already store it on the
        # BrowserSession side for the UI. Keep the metadata.
        clean = {k: v for k, v in result.items() if k != "screenshot"}
        try:
            return json.dumps(clean, ensure_ascii=False, indent=2)
        except Exception:
            return str(clean)
    return str(result) if result is not None else "(no output)"


# ─────────────────────────────────────────────────────────────────────
# Per-action dispatchers
# Each takes the SOP step's `command` (primary arg) + `step` dict
# (extras) + the resolved user_id/conv_id. Returns the formatted output
# string the scheduler logs.
# ─────────────────────────────────────────────────────────────────────


async def navigate(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Navigate the browser to a URL (`command` is the URL)."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    url = command or step.get("url", "")
    if not url:
        return "Error: navigate requires a url"
    return _format(await session.navigate(url))


async def click(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Click an element by CSS selector (`command` is the selector)."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    selector = command or step.get("selector", "")
    if not selector:
        return "Error: click requires a selector"
    return _format(await session.click_selector(selector))


async def click_xy(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Click at viewport coordinates from `step['x']` and `step['y']`."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    try:
        x = int(step.get("x", 0))
        y = int(step.get("y", 0))
    except (TypeError, ValueError):
        return "Error: click_xy requires integer x and y in the step"
    return _format(await session.click(x, y))


async def vision_click(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Click an element by visual description via the screenops VLM
    (`command` is the description)."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    description = command or step.get("description") or step.get("text", "")
    if not description:
        return "Error: vision_click requires a description"
    return _format(await session.vision_click(description))


async def type_in_selector(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Fill a form field by CSS selector (`command` is the selector,
    text is in `step['text']` or `step['content']`)."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    selector = command or step.get("selector", "")
    text = step.get("text") or step.get("content") or ""
    if not selector:
        return "Error: type_in_selector requires a selector"
    return _format(await session.type_in_selector(selector, text))


async def type_text(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Type text into the focused element (`command` is the text)."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    text = command or step.get("text", "")
    return _format(await session.type_text(text))


async def press_key(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Press a keyboard key (`command` is the key name, e.g. 'Enter')."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    key = command or step.get("key", "")
    if not key:
        return "Error: press_key requires a key"
    return _format(await session.press_key(key))


async def scroll(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Scroll the page (`command` is 'up'/'down', amount from
    `step['amount']`, default 300)."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    direction = (command or step.get("direction") or "down").strip().lower()
    try:
        amount = int(step.get("amount", 300))
    except (TypeError, ValueError):
        amount = 300
    return _format(await session.scroll(direction, amount))


async def back(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Browser back."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    return _format(await session.go_back())


async def forward(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Browser forward."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    return _format(await session.go_forward())


async def refresh(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Browser refresh."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    return _format(await session.refresh())


async def read_page(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Read text content from the page. `command` is an optional CSS
    selector; empty means full page."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    selector = command or step.get("selector") or ""
    return _format(await session.read_page(selector or None))


async def take_screenshot(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Capture a screenshot of the current page. Returns URL+title; the
    JPEG itself is held on the session and pushed to any attached WS."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    ss = await session.get_screenshot_base64()
    info = {"url": session.current_url, "title": session.current_title,
            "screenshot_bytes": len(ss) if ss else 0}
    return _format(info)


async def get_url(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Return the current URL + title without any side effects."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    return _format(await session.get_url_info())


async def evaluate_js(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Run a JavaScript expression in the page and return the result.
    `command` is the JS expression."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    expression = command or step.get("expression", "")
    if not expression:
        return "Error: evaluate_js requires an expression"
    return _format(await session.evaluate_js(expression))


async def wait(command: str, step: dict, user_id: str, conv_id: str) -> str:
    """Pause for N seconds (`command` is the number)."""
    session = await _ensure_session(user_id, conv_id)
    if not session:
        return "Error: no user_id for browser session"
    try:
        seconds = float(command or step.get("seconds", 1.0))
    except (TypeError, ValueError):
        seconds = 1.0
    return _format(await session.wait(seconds))


# Map of SOP step tool name → dispatcher. Used by scheduler_service.
DISPATCHERS = {
    "navigate": navigate,
    "click": click,
    "click_element": click,            # alias used by some SOPs
    "click_selector": click,
    "click_xy": click_xy,
    "vision_click": vision_click,
    "type": type_in_selector,
    "type_text": type_text,
    "press_key": press_key,
    "scroll": scroll,
    "back": back,
    "forward": forward,
    "refresh": refresh,
    "read_page": read_page,
    "screenshot": take_screenshot,
    "take_screenshot": take_screenshot,
    "get_url": get_url,
    "evaluate_js": evaluate_js,
    "wait": wait,
}
