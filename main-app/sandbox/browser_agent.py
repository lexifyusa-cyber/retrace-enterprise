#!/usr/bin/env python3
"""
Browser Agent — runs inside the sandbox container.

Manages a persistent headless Chromium instance via Playwright.
Accepts JSON action as CLI argument, executes it, returns JSON result
with screenshot via stdout.

Chromium persistence: On first invocation, launches Chromium with
--remote-debugging-port=9222. Subsequent invocations connect via CDP
to the already-running instance (all inside the container, no port
mapping needed).

Usage:
    python3 /opt/browser_agent.py '{"action":"navigate","url":"https://google.com"}'
"""

import base64
import json
import os
import subprocess
import sys
import time


def _get_chromium_path():
    """Find the Playwright Chromium binary."""
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright")
    for root, dirs, files in os.walk(browsers_path):
        if "chrome" in files:
            return os.path.join(root, "chrome")
    return None


def _is_chromium_running():
    """Check if Chromium is already running on port 9222."""
    try:
        import urllib.request
        req = urllib.request.urlopen("http://localhost:9222/json/version", timeout=2)
        return req.status == 200
    except Exception:
        return False


def _start_chromium():
    """Start Chromium in the background with remote debugging."""
    chrome_path = _get_chromium_path()
    if not chrome_path:
        return False

    # Create download directory
    os.makedirs("/workspace/Downloads", exist_ok=True)

    subprocess.Popen(
        [
            chrome_path,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--window-size=1280,900",
            "--download-default-directory=/workspace/Downloads",
            "--disable-popup-blocking",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for it to be ready
    for _ in range(20):
        if _is_chromium_running():
            return True
        time.sleep(0.5)
    return False


def _ensure_chromium():
    """Ensure Chromium is running, start if not."""
    if not _is_chromium_running():
        return _start_chromium()
    return True


def _connect_and_execute(action_data):
    """Connect to Chromium via CDP and execute the action."""
    from playwright.sync_api import sync_playwright

    result = {"ok": False}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")

        # Get or create a context/page
        contexts = browser.contexts
        if contexts:
            context = contexts[0]
            pages = context.pages
            if pages:
                page = pages[0]
            else:
                page = context.new_page()
        else:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

        # Enable file downloads to /workspace/Downloads/
        try:
            cdp_session = context.new_cdp_session(page)
            cdp_session.send("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": "/workspace/Downloads"
            })
        except Exception:
            try:
                cdp_session.send("Browser.setDownloadBehavior", {
                    "behavior": "allow",
                    "downloadPath": "/workspace/Downloads"
                })
            except Exception:
                pass

        action = action_data.get("action", "")

        try:
            if action == "navigate":
                url = action_data.get("url", "")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                result = {"ok": True, "url": page.url, "title": page.title()}

            elif action == "screenshot":
                result = {"ok": True, "url": page.url, "title": page.title()}

            elif action == "click":
                x = int(action_data.get("x", 0))
                y = int(action_data.get("y", 0))
                # Snapshot files before click to detect new downloads
                before_files = set(os.listdir("/workspace/Downloads/")) if os.path.isdir("/workspace/Downloads/") else set()
                page.mouse.click(x, y)
                time.sleep(3)  # Wait for download to start
                # Check for new downloaded files
                after_files = set(os.listdir("/workspace/Downloads/")) if os.path.isdir("/workspace/Downloads/") else set()
                new_files = after_files - before_files
                result = {"ok": True}
                if new_files:
                    result["downloaded"] = list(new_files)

            elif action == "click_element":
                selector = action_data.get("selector", "")
                # Snapshot files before click to detect new downloads
                before_files = set(os.listdir("/workspace/Downloads/")) if os.path.isdir("/workspace/Downloads/") else set()
                page.locator(selector).first.click(timeout=5000)
                time.sleep(3)  # Wait for download to start
                # Check for new downloaded files
                after_files = set(os.listdir("/workspace/Downloads/")) if os.path.isdir("/workspace/Downloads/") else set()
                new_files = after_files - before_files
                result = {"ok": True}
                if new_files:
                    result["downloaded"] = list(new_files)
                result = {"ok": True}

            elif action == "type":
                text = action_data.get("text", "")
                page.keyboard.type(text)
                result = {"ok": True}

            elif action == "fill":
                selector = action_data.get("selector", "")
                text = action_data.get("text", "")
                page.fill(selector, text, timeout=5000)
                result = {"ok": True}

            elif action == "read_page":
                selector = action_data.get("selector", "")
                if selector:
                    text = page.locator(selector).first.inner_text(timeout=5000)
                else:
                    text = page.inner_text("body", timeout=5000)
                result = {"ok": True, "text": text[:8000]}

            elif action == "scroll":
                direction = action_data.get("direction", "down")
                amount = int(action_data.get("amount", 300))
                delta = amount if direction == "down" else -amount
                if direction in ("left", "right"):
                    page.mouse.wheel(delta if direction == "right" else -delta, 0)
                else:
                    page.mouse.wheel(0, delta)
                result = {"ok": True}

            elif action == "press_key":
                key = action_data.get("key", "")
                page.keyboard.press(key)
                result = {"ok": True}

            elif action == "hover":
                selector = action_data.get("selector", "")
                x = action_data.get("x")
                y = action_data.get("y")
                if selector:
                    page.hover(selector, timeout=5000)
                elif x is not None and y is not None:
                    page.mouse.move(int(x), int(y))
                result = {"ok": True}

            elif action == "hover_element":
                selector = action_data.get("selector", "")
                page.hover(selector, timeout=5000)
                result = {"ok": True}

            elif action == "select_option":
                selector = action_data.get("selector", "")
                value = action_data.get("value", "")
                page.select_option(selector, value, timeout=5000)
                result = {"ok": True}

            elif action == "wait":
                seconds = min(float(action_data.get("seconds", 1)), 10)
                time.sleep(seconds)
                result = {"ok": True}

            elif action == "evaluate_js":
                expression = action_data.get("js_expression", "")
                js_result = page.evaluate(expression)
                result = {"ok": True, "result": str(js_result)[:8000]}

            elif action == "back":
                page.go_back(wait_until="domcontentloaded", timeout=15000)
                result = {"ok": True, "url": page.url, "title": page.title()}

            elif action == "forward":
                page.go_forward(wait_until="domcontentloaded", timeout=15000)
                result = {"ok": True, "url": page.url, "title": page.title()}

            elif action == "refresh":
                page.reload(wait_until="domcontentloaded", timeout=15000)
                result = {"ok": True, "url": page.url, "title": page.title()}

            elif action == "get_url":
                result = {"ok": True, "url": page.url, "title": page.title()}

            elif action == "analyze_page":
                body_text = page.inner_text("body", timeout=5000)
                scroll_height = page.evaluate("document.body.scrollHeight")
                result = {
                    "ok": True,
                    "text_length": len(body_text),
                    "scroll_height": scroll_height,
                }

            else:
                result = {"ok": False, "error": f"Unknown action: {action}"}

        except Exception as exc:
            result = {"ok": False, "error": str(exc)}

        # Always include screenshot and page info
        try:
            screenshot_bytes = page.screenshot(type="jpeg", quality=70, full_page=False)
            result["screenshot"] = base64.b64encode(screenshot_bytes).decode()
            result["url"] = page.url
            result["title"] = page.title()
        except Exception:
            pass

        # Don't close browser — keep it running for next invocation
        browser.close()  # Disconnects CDP, doesn't kill Chromium

    return result


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "No action provided"}))
        sys.exit(1)

    try:
        action_data = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}))
        sys.exit(1)

    # Ensure Chromium is running
    if not _ensure_chromium():
        print(json.dumps({"ok": False, "error": "Failed to start Chromium"}))
        sys.exit(1)

    # Execute action
    result = _connect_and_execute(action_data)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
