"""
ReTrace Browser MCP Server — granular browser automation.

Drives the same Chromium instance the user's BrowserWorkspace WebSocket is
attached to, so agent actions are visible in the right-panel view in real
time. The MCP server itself runs as a separate Python subprocess (spawned by
the OpenCode sidecar), so it can't share Python objects with the FastAPI
backend's BrowserManager. Instead, every tool is a thin HTTP call to the
backend's internal browser-action bridge:

    POST /api/v1/internal/browser/action
      body:   {"action": "...", "user_id": "...", "selector"?: "...", ...}
      header: X-Internal-Token: <RETRACE_INTERNAL_TOKEN>

DESIGN NOTE — no `conversation_id` argument
-------------------------------------------
Pre-OC the auto_browser tool baked `conversation_id` into a Python closure
when the LLM tool object was constructed, so the LLM never saw it. Post-OC
we run as a separate process and FastMCP exposes every function arg to the
LLM. If we exposed `conversation_id` here the LLM would invent values
("browser_session_1", "default", "google_session") and each one would spawn
a fresh BrowserSession that the user's WebSocket isn't attached to — the
agent would navigate an invisible Chromium and the panel would never update.

We solve that by:
  - NOT exposing `conversation_id` as a tool arg.
  - Passing only `user_id` (from env) in the body.
  - The backend uses `_active_browser_conv[user_id]` (set by
    `opencode_send_message` at the start of every turn) to find the SAME
    conversation the user's WS is attached to.

Required env (set by opencode_manager.generate_config in opencode.json):
  RETRACE_USER_ID            — agent's user id; tags every request.
  RETRACE_TEAM_ID            — agent's team id (logged; not auth-relevant).
  RETRACE_BACKEND_URL        — base URL of the ReTrace backend.
  RETRACE_INTERNAL_TOKEN     — shared secret. Backend refuses calls without
                               the matching X-Internal-Token header.

Tools:
  navigate, take_screenshot, click, type_text, read_page, scroll, press_key,
  vision_click (when screenops is configured server-side)
"""

import json
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("retrace-browser")

_USER_ID = os.environ.get("RETRACE_USER_ID", "")
_TEAM_ID = os.environ.get("RETRACE_TEAM_ID", "")
_BACKEND_URL = os.environ.get("RETRACE_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
_INTERNAL_TOKEN = os.environ.get("RETRACE_INTERNAL_TOKEN", "")

# Generous default. Navigate + post-click stabilization can spend a few seconds
# on slow sites; we don't want to dangle forever on a truly bad URL.
_HTTP_TIMEOUT = 30.0


def _post_action(action: str, **kwargs) -> str:
    """POST to the backend's internal browser bridge. Returns the JSON
    response as a string (the agent reads JSON from tool output).

    Failures (network / 4xx-5xx / missing token) are returned as a JSON
    `{"error": "..."}` blob so OpenCode propagates them to the LLM rather
    than crashing the tool call.
    """
    if not _BACKEND_URL:
        return json.dumps({"error": "RETRACE_BACKEND_URL is not set"})
    if not _INTERNAL_TOKEN:
        return json.dumps({"error": "RETRACE_INTERNAL_TOKEN is not set"})
    if not _USER_ID:
        return json.dumps({"error": "RETRACE_USER_ID is not set"})

    body: dict = {"action": action, "user_id": _USER_ID}
    for k, v in kwargs.items():
        if v is not None and v != "":
            body[k] = v

    url = f"{_BACKEND_URL}/api/v1/internal/browser/action"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                url, json=body, headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"Transport error contacting backend: {exc}"})

    if resp.status_code != 200:
        try:
            err_body = resp.json()
        except Exception:
            err_body = {"detail": resp.text[:300]}
        return json.dumps({"error": f"HTTP {resp.status_code}", "detail": err_body})

    try:
        return json.dumps(resp.json())
    except Exception:
        return resp.text


@mcp.tool()
def navigate(url: str) -> str:
    """Navigate the browser to a URL. Waits for the page to reach
    `domcontentloaded` before returning so subsequent reads see the loaded
    DOM. Operates on the user's currently-active conversation's browser.

    Args:
        url: The URL to navigate to (must include scheme: https://...)
    """
    return _post_action("navigate", url=url)


@mcp.tool()
def take_screenshot() -> str:
    """Capture a screenshot of the current page. Returns base64-encoded JPEG
    inside `{screenshot, url, title}`."""
    return _post_action("screenshot")


@mcp.tool()
def click(selector: str) -> str:
    """Click the first element matching a CSS selector. After the click we
    wait for the page to settle (any same-tab navigation triggered by the
    click completes before the screenshot is captured), so the result and
    the panel update reflect the post-click state.

    Args:
        selector: CSS selector of the element to click. Examples:
                  `button.submit`, `a[href*="login"]`, `[aria-label="Sign in"]`,
                  `[data-testid="next"]`
    """
    return _post_action("click", selector=selector)


@mcp.tool()
def vision_click(description: str) -> str:
    """Click an element by visual description. Takes a screenshot and asks
    a vision model (`screenops`) to return pixel coordinates, then clicks
    at those coordinates. Use this when no stable CSS selector exists
    (e.g., shadow DOM, dynamic classnames, image-based buttons).

    Requires server-side `screenops_api_key` configuration. If unset, this
    falls back to returning a helpful error — use `click(selector)` instead.

    Args:
        description: Natural-language description of the element to click.
                    Examples: "blue submit button", "Sign in link in top right",
                    "the second result card"
    """
    return _post_action("vision_click", text=description)


@mcp.tool()
def type_text(selector: str, text: str) -> str:
    """Fill a form field by CSS selector. Uses Playwright's `.fill()`
    (which clears the field first), so repeated calls don't accumulate stale
    input.

    Args:
        selector: CSS selector of the input element
        text: Text to type
    """
    return _post_action("type", selector=selector, text=text)


@mcp.tool()
def read_page(selector: Optional[str] = None) -> str:
    """Extract visible text from the page, optionally scoped to a CSS
    selector. Truncated to 8000 chars.

    Args:
        selector: Optional CSS selector. If omitted, reads the whole `<body>`.
    """
    return _post_action("read_page", selector=selector)


@mcp.tool()
def scroll(direction: str = "down", amount: int = 500) -> str:
    """Scroll the page.

    Args:
        direction: 'up' or 'down'
        amount: Pixels
    """
    return _post_action("scroll", direction=direction, amount=amount)


@mcp.tool()
def press_key(key: str) -> str:
    """Press a keyboard key on the page (Enter, Tab, Escape, ArrowDown, etc).

    Args:
        key: Key name (Playwright keyboard key syntax)
    """
    return _post_action("press_key", key=key)


@mcp.tool()
def evaluate_js(expression: str) -> str:
    """Run a JavaScript expression in the page and return its result.

    The DOM escape hatch — use this when no other tool covers what's needed.
    Examples: probing computed styles, traversing shadow DOM, dispatching
    custom events, scraping non-text attributes. The result is coerced to a
    string and truncated to 8000 chars.

    Args:
        expression: JavaScript expression to evaluate. Example:
            `document.querySelectorAll('.row').length`
            `Array.from(document.links).map(a => a.href)`
    """
    return _post_action("evaluate_js", expression=expression)


@mcp.tool()
def select_option(selector: str, value: str) -> str:
    """Pick an option from a native `<select>` dropdown.

    Native `<select>` elements ignore mouse clicks for dropdown items —
    this is the only correct way. Accepts either the option's `value`
    attribute or its visible text label.

    Args:
        selector: CSS selector of the `<select>` element
        value: Option `value` attribute OR visible label
    """
    return _post_action("select_option", selector=selector, value=value)


@mcp.tool()
def hover(selector: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None) -> str:
    """Hover an element by selector, or move the cursor to coordinates.

    Use for hover-revealed menus, tooltips, or UI that only renders on
    `mouseenter`. Provide either `selector` OR both `x` and `y`.

    Args:
        selector: CSS selector to hover. Mutually exclusive with x/y.
        x: X coordinate (used with y when no selector)
        y: Y coordinate (used with x when no selector)
    """
    return _post_action("hover", selector=selector, x=x, y=y)


@mcp.tool()
def back() -> str:
    """Navigate back in the browser history."""
    return _post_action("back")


@mcp.tool()
def forward() -> str:
    """Navigate forward in the browser history."""
    return _post_action("forward")


@mcp.tool()
def refresh() -> str:
    """Reload the current page."""
    return _post_action("refresh")


@mcp.tool()
def get_url() -> str:
    """Return the current page URL and title without any side effects."""
    return _post_action("get_url")


@mcp.tool()
def wait(seconds: float = 1.0) -> str:
    """Pause for a number of seconds (clamped to [0.1, 10]).

    Useful when a page needs to settle after an async update before the next
    action. Prefer waiting on a specific element (via `evaluate_js`) over a
    blind sleep when possible.

    Args:
        seconds: How long to wait (0.1 to 10.0)
    """
    return _post_action("wait", seconds=seconds)


@mcp.tool()
def screenshot_full_page(max_tiles: int = 10) -> str:
    """Capture viewport-sized JPEG tiles top-to-bottom covering the whole page.

    Returns base64-encoded JPEG tiles in scroll order. Combine with a vision
    model call to "see" a tall page in one reasoning step. Capped at
    `max_tiles` to bound vision token cost.

    Args:
        max_tiles: Maximum number of tiles to capture (default 10)
    """
    return _post_action("screenshot_full_page", max_tiles=max_tiles)


@mcp.tool()
def analyze_page() -> str:
    """Return token-cost analysis for three extraction strategies.

    Compares: (1) `read_page` to extract all text, (2) single screenshot,
    (3) multi-tile full-page screenshot. The LLM picks the cheapest path
    before committing to the full extraction call. Useful on long pages
    where token cost matters.
    """
    return _post_action("analyze_page")


if __name__ == "__main__":
    mcp.run(transport="stdio")
