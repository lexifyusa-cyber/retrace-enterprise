"""
AutoBrowser — Enterprise browser automation tool for the ReTrace agent.

Provides granular, composable browser actions that the agent calls directly:
  navigate, screenshot, click, type, read_page, scroll, press_key, wait,
  extract_text, fill_form, select_option, hover, evaluate_js

All actions operate on the **shared BrowserManager session** so every action
is streamed live to the user's Browser Workspace panel via WebSocket.

Screenshots are analysed by the main LLM (via managed gateway) so the agent
can reason about what it sees on screen.

Hybrid content analysis strategy
---------------------------------
When the agent needs page content, it calls **analyze_page** first which
returns token costs for read_page vs screenshot approaches.

If the LLM chooses the *screenshot* path:
  1. screenshot_full_page — captures all viewport tiles, sends them bundled
     to the LLM in one vision call so it can see the entire page.
  2. The LLM identifies the section of interest and names a CSS selector.
  3. read_page(selector='#that-section') — targeted text extraction of just
     the relevant section (links, data, etc.).

If the LLM chooses the *read_page* path, it just reads directly — no
screenshot logic involved.
"""

from __future__ import annotations

import asyncio
import base64
import json
import traceback
from typing import Any, Optional

import structlog
from langchain_core.tools import StructuredTool

from app.services.browser_manager import browser_manager, SCREENSHOT_HEIGHT
from app.tools.analysis import analyze_extraction_options
from app.tools.token_analyzer import calculate_screenshot_count

logger = structlog.get_logger()

# Max chars returned from page text extraction
MAX_TEXT_LENGTH = 8000


async def _notify_ws_clients(session: Any, message: str) -> None:
    """Send a status message to all WebSocket clients watching this session."""
    import json as _json
    payload = _json.dumps({"type": "status", "message": message, "url": session.current_url})
    disconnected = set()
    for ws in session.websocket_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.add(ws)
    session.websocket_clients -= disconnected


async def _push_screenshot_to_ws(session: Any) -> None:
    """Force an immediate screenshot broadcast to WebSocket clients."""
    if not session.websocket_clients or not session.page:
        return
    try:
        b64 = await session.get_screenshot_base64()
        if not b64:
            return
        session.current_url = session.page.url
        session.current_title = await session.page.title()
        import json as _json
        payload = _json.dumps({
            "type": "screenshot",
            "data": b64,
            "url": session.current_url,
            "title": session.current_title,
        })
        disconnected = set()
        for ws in session.websocket_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.add(ws)
        session.websocket_clients -= disconnected
    except Exception:
        pass


async def _auto_browser(
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    x: int = 0,
    y: int = 0,
    key: str = "",
    direction: str = "down",
    amount: int = 300,
    js_expression: str = "",
    seconds: float = 1.0,
    describe_screenshot: bool = False,
    chat_model: Any = None,
    conversation_id: Optional[str] = None,
    output_callback: Optional[Any] = None,
) -> str:
    """Execute a browser action and return the result as JSON.

    Actions
    -------
    navigate     : Go to a URL. Params: url
    screenshot   : Take a screenshot. Set describe_screenshot=True to get an
                   LLM description of what's on screen.
    click        : Click at (x, y) coordinates.
    click_element: Click the first element matching a CSS selector.
    type         : Type text into the currently focused element.
    fill         : Fill a form field found by CSS selector with text.
    read_page    : Extract visible text content from the page (or a selector).
    scroll       : Scroll the page. Params: direction (up/down), amount (pixels).
    press_key    : Press a keyboard key (Enter, Tab, Escape, etc.).
    hover        : Hover over (x, y) coordinates or a CSS selector.
    select_option: Select a <select> option by value or label.
    wait         : Wait for a number of seconds (max 10).
    evaluate_js  : Run a JavaScript expression in the page and return the result.
    back         : Go back in history.
    forward      : Go forward in history.
    refresh      : Reload the page.
    get_url      : Return the current page URL and title.
    """
    if not conversation_id:
        return json.dumps({"error": "No conversation_id — cannot create browser session"})

    try:
        session = await browser_manager.get_or_create(conversation_id)
        page = session.page
        if not page:
            return json.dumps({"error": "Browser page not available"})

        # Notify workspace clients that a tool action is happening
        await _notify_ws_clients(session, f"Agent: {action}...")

        result: dict[str, Any] = {"action": action, "ok": True}

        # ── Navigation ─────────────────────────────────────────────
        if action == "navigate":
            if not url:
                return json.dumps({"error": "url is required for navigate"})
            if output_callback:
                output_callback(f"Navigating to {url}...")
            nav = await session.navigate(url)
            if "error" in nav:
                return json.dumps({"error": nav["error"]})
            result["url"] = nav.get("url", url)
            result["title"] = nav.get("title", "")

            # Auto-describe the page after navigation so the LLM has
            # visual context and can decide on next actions.
            if chat_model:
                await asyncio.sleep(1)  # wait for page to render
                b64 = await session.get_screenshot_base64()
                if b64:
                    try:
                        from langchain_core.messages import HumanMessage
                        resp = await chat_model.ainvoke([HumanMessage(content=[
                            {"type": "text", "text": (
                                "Describe this browser screenshot concisely. "
                                "List the main page elements, navigation, headings, "
                                "key content, and any interactive elements (buttons, links, forms). "
                                "Be specific about text you can read."
                            )},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ])])
                        result["page_description"] = resp.content if hasattr(resp, "content") else str(resp)
                    except Exception as exc:
                        result["page_description"] = f"(vision failed: {exc})"
                else:
                    result["page_description"] = "(screenshot not available)"

        # ── Screenshot (+ optional LLM description) ────────────────
        elif action == "screenshot":
            b64 = await session.get_screenshot_base64()
            if not b64:
                return json.dumps({"error": "Screenshot capture failed"})
            result["screenshot_length"] = len(b64)
            result["url"] = session.current_url
            result["title"] = session.current_title

            if describe_screenshot and chat_model:
                try:
                    from langchain_core.messages import HumanMessage
                    prompt_text = (
                        "Describe this browser screenshot in detail. "
                        "List the main UI elements, text content, buttons, "
                        "links, forms, and any notable features. "
                        "If there are error messages, quote them exactly."
                    )
                    resp = await chat_model.ainvoke([HumanMessage(content=[
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ])])
                    result["description"] = resp.content if hasattr(resp, "content") else str(resp)
                except Exception as exc:
                    result["description"] = f"Vision analysis failed: {exc}"
            else:
                result["description"] = (
                    "Screenshot captured. Use describe_screenshot=True to get "
                    "an AI description of the page content."
                )

        # ── Full-page screenshot tiles bundled into one LLM call ──
        elif action == "screenshot_full_page":
            """Scroll through the entire page, capture viewport-sized tiles,
            and send ALL tiles to the LLM in a single vision call.

            The LLM describes each section it sees and suggests CSS selectors
            so the agent can follow up with a targeted read_page(selector=...).
            """
            if not chat_model:
                return json.dumps({"error": "screenshot_full_page requires a chat model for vision analysis"})

            try:
                page_height = await page.evaluate("document.body.scrollHeight")
                num_tiles = calculate_screenshot_count(page_height, SCREENSHOT_HEIGHT)
                # Cap at 10 tiles to avoid excessive token use
                num_tiles = min(num_tiles, 10)

                if output_callback:
                    output_callback(f"Capturing {num_tiles} screenshot tile(s) for full-page analysis...")

                await _notify_ws_clients(session, f"Capturing {num_tiles} tiles...")

                # Scroll to top first
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.3)

                tiles_b64: list[str] = []
                for i in range(num_tiles):
                    scroll_y = i * SCREENSHOT_HEIGHT
                    await page.evaluate(f"window.scrollTo(0, {scroll_y})")
                    await asyncio.sleep(0.25)  # let paint settle
                    b64 = await session.get_screenshot_base64()
                    if b64:
                        tiles_b64.append(b64)

                # Scroll back to top
                await page.evaluate("window.scrollTo(0, 0)")

                if not tiles_b64:
                    return json.dumps({"error": "Failed to capture any screenshot tiles"})

                # Build multi-image message for LLM
                from langchain_core.messages import HumanMessage

                content_parts: list[dict] = [
                    {"type": "text", "text": (
                        f"I captured {len(tiles_b64)} screenshot tiles covering this full web page "
                        f"(each tile is a {SCREENSHOT_HEIGHT}px-tall viewport section, scrolling top to bottom).\n\n"
                        "For each tile, describe the content you see.\n"
                        "Then provide a SUMMARY section at the end with:\n"
                        "1. Overall page structure and purpose\n"
                        "2. Key content sections you identified\n"
                        "3. For each section of interest, suggest a CSS selector that could be used with "
                        "read_page(selector='...') to extract just that section's text. "
                        "Use selectors like '#id', '.class', 'nav', 'article', 'main', 'table', "
                        "'ul.links', 'div.content', 'section:nth-of-type(N)', etc.\n"
                        "4. If you found the specific content the user is looking for, state which "
                        "selector to use for targeted extraction."
                    )},
                ]
                for idx, tile_b64 in enumerate(tiles_b64):
                    content_parts.append(
                        {"type": "text", "text": f"\n--- Tile {idx + 1} of {len(tiles_b64)} (scroll position ~{idx * SCREENSHOT_HEIGHT}px) ---"}
                    )
                    content_parts.append(
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{tile_b64}"}}
                    )

                resp = await chat_model.ainvoke([HumanMessage(content=content_parts)])
                description = resp.content if hasattr(resp, "content") else str(resp)

                result["num_tiles"] = len(tiles_b64)
                result["page_height_px"] = page_height
                result["url"] = session.current_url
                result["title"] = session.current_title
                result["full_page_description"] = description
                result["hint"] = (
                    "Use the CSS selectors from the description above to do a targeted "
                    "read_page(selector='...') to extract just the section you need."
                )

            except Exception as exc:
                return json.dumps({"error": f"screenshot_full_page failed: {exc}"})

        # ── Click at coordinates (DISABLED — use click_element instead) ──
        elif action == "click":
            return json.dumps({
                "error": "click(x, y) is disabled. Use click_element(selector) instead — "
                "it is more reliable. Example: auto_browser(action='click_element', selector='a.headline')"
            })

        # ── Click element by CSS selector ──────────────────────────
        elif action == "click_element":
            if not selector:
                return json.dumps({"error": "selector is required for click_element"})
            try:
                elem = page.locator(selector).first
                await elem.click(timeout=5000)
                await asyncio.sleep(0.5)
                session.current_url = page.url
                session.current_title = await page.title()
                result["url"] = session.current_url
                result["title"] = session.current_title
            except Exception as exc:
                return json.dumps({"error": f"click_element failed: {exc}"})

        # ── Type text into focused element ─────────────────────────
        elif action == "type":
            if not text:
                return json.dumps({"error": "text is required for type"})
            type_result = await session.type_text(text)
            if "error" in type_result:
                return json.dumps({"error": type_result["error"]})
            result["typed"] = len(text)

        # ── Fill a form field by selector ──────────────────────────
        elif action == "fill":
            if not selector or text is None:
                return json.dumps({"error": "selector and text are required for fill"})
            try:
                await page.fill(selector, text, timeout=5000)
                result["filled"] = selector
                result["value"] = text
            except Exception as exc:
                return json.dumps({"error": f"fill failed: {exc}"})

        # ── Read page text ─────────────────────────────────────────
        elif action == "read_page":
            try:
                if selector:
                    content = await page.locator(selector).first.inner_text(timeout=5000)
                else:
                    content = await page.inner_text("body", timeout=5000)
                if len(content) > MAX_TEXT_LENGTH:
                    content = content[:MAX_TEXT_LENGTH] + f"\n... (truncated, {len(content)} total chars)"
                result["text"] = content
                result["url"] = session.current_url
                result["title"] = session.current_title
            except Exception as exc:
                return json.dumps({"error": f"read_page failed: {exc}"})

        # ── Scroll ─────────────────────────────────────────────────
        elif action == "scroll":
            scroll_result = await session.scroll(direction, amount)
            if "error" in scroll_result:
                return json.dumps({"error": scroll_result["error"]})
            result["scrolled"] = direction

        # ── Press key ──────────────────────────────────────────────
        elif action == "press_key":
            if not key:
                return json.dumps({"error": "key is required for press_key"})
            key_result = await session.press_key(key)
            if "error" in key_result:
                return json.dumps({"error": key_result["error"]})
            result["pressed"] = key

        # ── Hover ──────────────────────────────────────────────────
        elif action == "hover":
            try:
                if selector:
                    await page.hover(selector, timeout=5000)
                else:
                    await page.mouse.move(x, y)
                await asyncio.sleep(0.3)
                result["hovered"] = selector or f"({x}, {y})"
            except Exception as exc:
                return json.dumps({"error": f"hover failed: {exc}"})

        # ── Select option ──────────────────────────────────────────
        elif action == "select_option":
            if not selector or not text:
                return json.dumps({"error": "selector and text (value) required for select_option"})
            try:
                await page.select_option(selector, text, timeout=5000)
                result["selected"] = text
            except Exception as exc:
                return json.dumps({"error": f"select_option failed: {exc}"})

        # ── Wait ───────────────────────────────────────────────────
        elif action == "wait":
            wait_time = min(max(seconds, 0.1), 10.0)
            await asyncio.sleep(wait_time)
            result["waited"] = wait_time

        # ── Evaluate JavaScript ────────────────────────────────────
        elif action == "evaluate_js":
            if not js_expression:
                return json.dumps({"error": "js_expression is required for evaluate_js"})
            try:
                js_result = await page.evaluate(js_expression)
                result["js_result"] = str(js_result)[:MAX_TEXT_LENGTH] if js_result is not None else None
            except Exception as exc:
                return json.dumps({"error": f"evaluate_js failed: {exc}"})

        # ── Back / Forward / Refresh ───────────────────────────────
        elif action == "back":
            r = await session.go_back()
            result.update(r)
        elif action == "forward":
            r = await session.go_forward()
            result.update(r)
        elif action == "refresh":
            r = await session.refresh()
            result.update(r)

        # ── Get current URL and title ──────────────────────────────
        elif action == "get_url":
            result["url"] = session.current_url
            result["title"] = session.current_title

        # ── Analyze page content extraction options ─────────────────
        elif action == "analyze_page":
            """Analyze page and return token costs for different extraction approaches."""
            try:
                # Get full page text and height
                text_content = await page.inner_text("body", timeout=5000)
                page_height = await page.evaluate("document.body.scrollHeight")

                # Analyze extraction options
                options = analyze_extraction_options(text_content, page_height)

                # Return analysis as JSON for LLM to review and decide
                result["action"] = "page_analysis"
                result["url"] = session.current_url
                result["page_height_px"] = page_height
                result["text_chars"] = len(text_content)
                result["extraction_options"] = [
                    {
                        "approach": opt.approach,
                        "total_tokens": opt.total_tokens,
                        "num_screenshots": opt.num_screenshots,
                        "description": opt.description,
                    }
                    for opt in options
                ]
                # Guide the LLM on what to do next based on its choice
                result["next_steps"] = (
                    "Review the extraction options above and decide:\n"
                    "• If you choose READ_PAGE: call auto_browser(action='read_page') to get all text.\n"
                    "• If you choose SCREENSHOT path: call auto_browser(action='screenshot_full_page') — "
                    "this captures all page tiles and sends them to you in one go. You will see the "
                    "entire page and can identify specific sections. Then use "
                    "auto_browser(action='read_page', selector='<css-selector>') to extract just "
                    "the text/links from the section you identified."
                )
            except Exception as exc:
                return json.dumps({"error": f"analyze_page failed: {exc}"})

        else:
            return json.dumps({"error": f"Unknown action: {action}. Valid actions: navigate, analyze_page, screenshot, screenshot_full_page, click_element, type, fill, read_page, scroll, press_key, hover, select_option, wait, evaluate_js, back, forward, refresh, get_url"})

        # Force an immediate screenshot push to the workspace so the user
        # sees the result of every tool action in real-time, without waiting
        # for the next screenshot loop tick (up to 0.33s delay).
        await _push_screenshot_to_ws(session)

        return json.dumps(result)

    except Exception as exc:
        logger.error("auto_browser_error", action=action, error=str(exc), tb=traceback.format_exc())
        return json.dumps({"error": str(exc)})


def build_auto_browser_tool(
    chat_model: Any = None,
    conversation_id: Optional[str] = None,
    output_callback: Optional[Any] = None,
) -> Optional[StructuredTool]:
    """Build the AutoBrowser StructuredTool.

    Returns None if Playwright is not installed.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        logger.warning("playwright_not_installed_auto_browser_disabled")
        return None

    async def _tool_fn(
        action: str,
        url: str = "",
        selector: str = "",
        text: str = "",
        x: int = 0,
        y: int = 0,
        key: str = "",
        direction: str = "down",
        amount: int = 300,
        js_expression: str = "",
        seconds: float = 1.0,
        describe_screenshot: bool = False,
    ) -> str:
        return await _auto_browser(
            action=action,
            url=url,
            selector=selector,
            text=text,
            x=x,
            y=y,
            key=key,
            direction=direction,
            amount=amount,
            js_expression=js_expression,
            seconds=seconds,
            describe_screenshot=describe_screenshot,
            chat_model=chat_model,
            conversation_id=conversation_id,
            output_callback=output_callback,
        )

    return StructuredTool.from_function(
        coroutine=_tool_fn,
        name="auto_browser",
        description=(
            "Control a real Chromium browser visible in the user's Workspace panel. "
            "Every action you take is streamed live so the user sees it happen.\n\n"
            "ACTIONS:\n"
            "  navigate(url)            — Go to a URL\n"
            "  analyze_page             — Compare token costs: read_page vs screenshot vs multi-tile\n"
            "  screenshot(describe_screenshot=True) — Capture viewport + AI-describe\n"
            "  screenshot_full_page     — Capture ALL page tiles, bundle them into ONE LLM vision call\n"
            "  click_element(selector)  — Click a CSS-selector element\n"
            "  type(text)               — Type text into the focused element\n"
            "  fill(selector, text)     — Fill a form input by CSS selector\n"
            "  read_page(selector?)     — Extract text from the full page or a specific CSS selector\n"
            "  scroll(direction, amount) — Scroll up/down by pixel amount\n"
            "  press_key(key)           — Press a key: Enter, Tab, Escape, Backspace, etc.\n"
            "  hover(selector or x,y)   — Hover over an element or coordinates\n"
            "  select_option(selector, text) — Select a dropdown option\n"
            "  wait(seconds)            — Wait up to 10 seconds\n"
            "  evaluate_js(js_expression) — Run JavaScript in the page\n"
            "  back / forward / refresh — Browser navigation\n"
            "  get_url                  — Get current URL and page title\n\n"
            "SMART CONTENT EXTRACTION WORKFLOW:\n"
            "  1. navigate(url) → land on the page\n"
            "  2. analyze_page  → see token costs for read_page vs screenshot\n"
            "  3. YOU DECIDE based on costs:\n"
            "     PATH A (read_page cheaper): read_page() → get all text directly\n"
            "     PATH B (screenshot cheaper): screenshot_full_page → see entire page visually,\n"
            "       identify the section you need, then read_page(selector='#that-section') → \n"
            "       extract ONLY the targeted text/links from that section\n\n"
            "EXAMPLE (Path B — hybrid screenshot→targeted read):\n"
            "  auto_browser(action='navigate', url='https://en.wikipedia.org/wiki/Python')\n"
            "  auto_browser(action='analyze_page')  # → read_page=28K tokens, screenshot=500\n"
            "  auto_browser(action='screenshot_full_page')  # → see all tiles, find References\n"
            "  auto_browser(action='read_page', selector='.references')  # → just the links\n\n"
            "For clicking, ALWAYS use click_element with a CSS selector, never click with x,y."
        ),
    )
