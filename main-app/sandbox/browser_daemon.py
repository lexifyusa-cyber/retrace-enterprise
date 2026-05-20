#!/usr/bin/env python3
"""
Browser Daemon — long-lived HTTP server that keeps Playwright connected.

Replaces the per-action subprocess pattern of browser_agent.py. Keeps the
Playwright context and Chromium connection alive across requests so every
browser action skips the ~2-second Python + Playwright cold-start cost.

Listens on http://127.0.0.1:9223 for JSON action requests:
    POST /action        — execute an action (body: JSON)
    GET  /health        — health probe (returns {"ok": true} if ready)

Started automatically by browser_control.py (the thin client that the
host calls per action) or by the container entrypoint at boot time.

Usage:
    python3 /opt/browser_daemon.py              # listen on :9223
    python3 /opt/browser_daemon.py --port 9224  # custom port
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Ad / popup blocking CSS — injected into every page to suppress common
# ad overlays (Google vignette, interstitials, cookie banners, etc.)
# ---------------------------------------------------------------------------
_AD_BLOCK_CSS = """
/* Google Vignette / interstitial ads */
#google_vignette,
.google-auto-placed,
ins.adsbygoogle,
div[id*="google_ads"],
iframe[id*="google_ads"],
div[id*="google_vignette"] {
    display: none !important;
    visibility: hidden !important;
    height: 0 !important;
    width: 0 !important;
    overflow: hidden !important;
}

/* Generic ad overlays / modals */
div[class*="ad-overlay"],
div[class*="ad-modal"],
div[class*="interstitial"],
div[class*="popup-ad"],
div[class*="modal-overlay"][style*="z-index"],
div[class*="vignette"] {
    display: none !important;
}

/* Common cookie consent banners — auto-dismiss */
div[class*="cookie-banner"],
div[class*="cookie-consent"],
div[id*="cookie-banner"],
div[id*="cookie-consent"],
div[id*="CybotCookiebotDialog"] {
    display: none !important;
}
"""

_AD_BLOCK_JS = """
// Dismiss Google vignette by removing the hash fragment
if (window.location.hash === '#google_vignette') {
    history.replaceState(null, '', window.location.pathname + window.location.search);
}

// Remove known ad iframes
document.querySelectorAll('iframe[id*="google_ads"]').forEach(f => f.remove());

// Block future ad insertions via MutationObserver
if (!window.__retraceAdObserver) {
    window.__retraceAdObserver = new MutationObserver(mutations => {
        for (const m of mutations) {
            for (const node of m.addedNodes) {
                if (node.nodeType !== 1) continue;
                const id = (node.id || '').toLowerCase();
                const cls = (node.className || '').toLowerCase();
                if (id.includes('google_vignette') || id.includes('google_ads') ||
                    cls.includes('adsbygoogle') || cls.includes('interstitial') ||
                    cls.includes('ad-overlay') || cls.includes('vignette')) {
                    node.remove();
                }
            }
        }
    });
    window.__retraceAdObserver.observe(document.documentElement, {childList: true, subtree: true});
}
"""

# Max seconds to poll for a new file after a click that might trigger a download
_DOWNLOAD_POLL_TIMEOUT = 20
_DOWNLOAD_POLL_INTERVAL = 1


# ---------------------------------------------------------------------------
# Chromium lifecycle helpers
# ---------------------------------------------------------------------------

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

    os.makedirs("/workspace/__default__/Downloads", exist_ok=True)
    os.makedirs("/workspace/.chrome-profile", exist_ok=True)

    # Remove stale lock files from previous crashed Chromium.
    # Use os.path.islink() because broken symlinks (from previous containers)
    # return False for os.path.exists() but still block Chromium startup.
    for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
        lock_path = f"/workspace/.chrome-profile/{lock}"
        if os.path.exists(lock_path) or os.path.islink(lock_path):
            try:
                os.unlink(lock_path)
            except Exception:
                pass

    subprocess.Popen(
        [
            chrome_path,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--window-size=1280,900",
            "--download-default-directory=/workspace/__default__/Downloads",
            "--disable-popup-blocking",
            "--user-data-dir=/workspace/.chrome-profile",
            # Stealth: hide headless/automation signals
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for CDP to be responsive
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


# ---------------------------------------------------------------------------
# Daemon state — persistent Playwright context held across all requests
# ---------------------------------------------------------------------------

class DaemonState:
    """Holds the persistent Playwright + browser handles and per-conversation contexts.

    Each conversation gets its own browser context (isolated cookies, storage,
    history) and workspace subdirectory. The browser itself is shared across
    all conversations within the container.

    A single lock serializes all action execution because Playwright
    sync_api is not thread-safe.
    """

    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.lock = threading.Lock()
        self.playwright = None
        self.browser = None
        # Per-conversation contexts: {conversation_id: {"context", "page", "cdp_session", "last_used"}}
        self.contexts: dict = {}
        self.max_contexts = 10
        self.ready = False
        self.error = None

    def _ensure_browser_ready(self):
        """Initialize Playwright + Chromium connection (no context/page).

        Must be called with self.lock held.
        """
        if self.ready and self.browser is not None:
            return True
        if not _ensure_chromium():
            self.error = "Failed to start Chromium"
            return False
        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.connect_over_cdp(
                "http://localhost:9222"
            )
            # Don't close pre-existing CDP contexts — closing them kills the
            # browser connection. They'll sit idle; new conversations get
            # their own fresh contexts via get_or_create_context().
            self.ready = True
            self.error = None
            return True
        except Exception as exc:
            self.error = f"Playwright init failed: {exc}"
            self.ready = False
            return False

    def get_or_create_context(self, conversation_id: str):
        """Get or create a browser context for a conversation.

        Returns (context, page) tuple. Must be called with self.lock held.
        """
        if not self._ensure_browser_ready():
            return None, None

        # Return existing context
        if conversation_id in self.contexts:
            entry = self.contexts[conversation_id]
            entry["last_used"] = time.time()
            return entry["context"], entry["page"]

        # Evict LRU context if at capacity
        if len(self.contexts) >= self.max_contexts:
            lru_id = min(self.contexts, key=lambda k: self.contexts[k]["last_used"])
            self._close_context(lru_id)

        # Create per-conversation workspace directory
        conv_workspace = f"/workspace/{conversation_id}"
        os.makedirs(f"{conv_workspace}/Downloads", exist_ok=True)

        # Create isolated context
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=self._USER_AGENT,
        )
        page = context.new_page()

        # Enable downloads to per-conversation directory
        cdp_session = None
        try:
            cdp_session = context.new_cdp_session(page)
            cdp_session.send("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": f"{conv_workspace}/Downloads",
            })
        except Exception:
            try:
                if cdp_session:
                    cdp_session.send("Browser.setDownloadBehavior", {
                        "behavior": "allow",
                        "downloadPath": f"{conv_workspace}/Downloads",
                    })
            except Exception:
                pass

        self.contexts[conversation_id] = {
            "context": context,
            "page": page,
            "cdp_session": cdp_session,
            "last_used": time.time(),
        }
        return context, page

    def _close_context(self, conversation_id: str):
        """Close and remove a single conversation's context. Lock must be held."""
        entry = self.contexts.pop(conversation_id, None)
        if not entry:
            return
        try:
            entry["context"].close()
        except Exception:
            pass

    def reset(self):
        """Tear down all contexts and Playwright handles.

        Called when a Playwright exception suggests the CDP link is dead.
        """
        # Close all per-conversation contexts
        for cid in list(self.contexts):
            try:
                self.contexts[cid]["context"].close()
            except Exception:
                pass
        self.contexts.clear()
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright is not None:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None
        self.browser = None
        self.ready = False


STATE = DaemonState()


# ---------------------------------------------------------------------------
# Action execution helpers
# ---------------------------------------------------------------------------

def _inject_ad_blocker(page):
    """Inject ad-blocking CSS and JS into the current page."""
    try:
        page.add_style_tag(content=_AD_BLOCK_CSS)
    except Exception:
        pass
    try:
        page.evaluate(_AD_BLOCK_JS)
    except Exception:
        pass


def _poll_for_download(before_files, downloads_dir: str, timeout=_DOWNLOAD_POLL_TIMEOUT):
    """Poll downloads directory for new files, waiting up to *timeout* seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(_DOWNLOAD_POLL_INTERVAL)
        try:
            after_files = set(os.listdir(downloads_dir))
        except FileNotFoundError:
            continue
        new_files = after_files - before_files
        new_files = {f for f in new_files if not f.endswith(".crdownload")}
        if new_files:
            time.sleep(1)
            return list(new_files)
    return []


# ---------------------------------------------------------------------------
# Click effectiveness detection
# ---------------------------------------------------------------------------
#
# The raw mouse.click() and locator.click() operations never tell us whether
# the click actually did anything — a click on empty pixels or an ad iframe
# returns with no exception, exactly like a click on a real button. This
# would leave the agent thinking "ok:true == click worked", which is how we
# end up telling the user a download succeeded when the click landed on nothing.
#
# _click_snapshot() captures the page state before the click. _click_verdict()
# compares that state against the page after the click and returns a dict with:
#   click_effective:  bool — True if ANY of URL/title/body-text/tab-count
#                     changed, OR a download was triggered
#   warning:          str — populated only when click_effective is False,
#                     tells the caller (and the agent) what probably went wrong
#   downloaded:       list — filenames of newly downloaded files, if any
#   new_tab_opened:   bool — True if the click opened a new tab/popup
#
# We only inspect cheap things (URL, title, first 4000 chars of body text).
# Pixel-diffing the screenshot would be more robust but much more expensive
# and ad-rotating pages would produce false positives from dynamic banners.
# The body-text hash already catches most meaningful DOM changes while
# tolerating a small amount of ad rotation.


# Max seconds to wait for an element at (x, y) to become actionable
# (pointer-events != 'none', opacity > 0.1) before firing a coordinate click.
# Matches the actionability check that Playwright's locator.click does
# automatically. Without this, pages that render a download button in a
# disabled state (pointer-events: none, opacity: 0.5) and enable it a
# moment later via async JavaScript will silently swallow coordinate clicks
# — the event passes through the "disabled" layer to whatever is beneath.
_CLICK_ACTIONABILITY_TIMEOUT = 5.0
_CLICK_ACTIONABILITY_POLL_INTERVAL = 0.2


def _wait_for_clickable_at(page, x: int, y: int,
                           timeout_s: float = _CLICK_ACTIONABILITY_TIMEOUT) -> dict:
    """Wait for the element at viewport (x, y) to be actionable.

    Polls every 200 ms for up to *timeout_s* seconds, walking the ancestor
    chain of whatever element is at the point. Considers the target NOT
    actionable if any ancestor has:
      - pointer-events: none (click would pass through)
      - opacity < 0.1 (effectively invisible / disabled)

    Returns a dict describing the final state:
      {"waited": <seconds>, "ready": bool, "tag": "...", "blocked_by": "..."}

    The caller should still fire the click even on timeout — the existing
    click_effective verdict will catch the miss and warn the user, rather
    than this helper deciding unilaterally to skip the click.
    """
    deadline = time.time() + timeout_s
    last_state: dict = {}
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            state = page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return {found: false};
                    // Walk ancestors checking pointer-events and opacity.
                    // pointer-events: none on ANY ancestor suppresses clicks
                    // on the descendant, regardless of its own computed style.
                    let cur = el;
                    while (cur && cur !== document.documentElement) {
                        const cs = getComputedStyle(cur);
                        if (cs.pointerEvents === 'none') {
                            return {
                                found: true,
                                clickable: false,
                                blocked_by: cur.tagName + '.' + (cur.className || '').toString().split(' ')[0],
                                reason: 'pointer-events:none',
                            };
                        }
                        if (parseFloat(cs.opacity || '1') < 0.1) {
                            return {
                                found: true,
                                clickable: false,
                                blocked_by: cur.tagName,
                                reason: 'opacity<0.1',
                            };
                        }
                        cur = cur.parentElement;
                    }
                    return {
                        found: true,
                        clickable: true,
                        tag: el.tagName,
                        cls: (el.className || '').toString().slice(0, 60),
                    };
                }""",
                [x, y],
            )
        except Exception as exc:
            # If evaluate itself fails, the page is probably in a bad state
            # — let the click run anyway and let the outer retry/verdict
            # logic handle it.
            return {"waited": time.time() - (deadline - timeout_s),
                    "ready": False, "error": str(exc)[:120]}

        last_state = state or {}
        if last_state.get("clickable"):
            return {
                "waited": round(time.time() - (deadline - timeout_s), 2),
                "ready": True,
                "tag": last_state.get("tag"),
                "attempts": attempts,
            }
        time.sleep(_CLICK_ACTIONABILITY_POLL_INTERVAL)

    # Timed out. Caller will still fire the click; click_effective
    # will detect the miss.
    return {
        "waited": timeout_s,
        "ready": False,
        "blocked_by": last_state.get("blocked_by"),
        "reason": last_state.get("reason"),
        "attempts": attempts,
    }


def _click_snapshot(page, downloads_dir: str) -> dict:
    """Capture page state before a click — used for download, new-tab, and URL-change detection."""
    return {
        "url": page.url,
        "pages_count": len(page.context.pages) if page.context else 1,
        "files": (
            set(os.listdir(downloads_dir))
            if os.path.isdir(downloads_dir) else set()
        ),
    }


def _safe_title(page) -> str:
    """Get page title without raising on dead pages."""
    try:
        return page.title()
    except Exception:
        return ""


def _click_verdict(page, before: dict, action_desc: str, downloads_dir: str = "/workspace/Downloads") -> dict:
    """Detect downloads and new tabs after a click.

    The LLM receives a screenshot after every click and can judge for itself
    whether the click did something visible. This function only reports facts
    the screenshot cannot show: files saved to Downloads and new tabs opened.

    Returns a dict with downloaded (list, optional) and new_tab_opened (bool,
    optional). The caller merges this into the final result dict.

    Performance: if a new tab opened or the URL changed, a navigation clearly
    happened — skip the 20-second download poll entirely and return fast.
    Only run the full poll when nothing obvious changed, which is the only
    case where a silent download could have started without a visible signal.
    """
    verdict: dict = {}

    # New tab detection
    try:
        pages_after = len(page.context.pages) if page.context else 1
    except Exception:
        pages_after = before["pages_count"]
    new_tab = pages_after > before["pages_count"]
    if new_tab:
        verdict["new_tab_opened"] = True

    # Short-circuit: if URL changed or new tab opened, a navigation happened.
    # Downloads don't co-occur with visible navigations — skip the 20s poll.
    url_changed = page.url != before.get("url", page.url)
    if url_changed or new_tab:
        return verdict

    # Nothing obvious changed — poll Downloads to catch silent file saves.
    new_files = _poll_for_download(before["files"], downloads_dir)
    if new_files:
        verdict["downloaded"] = new_files

    return verdict


# Default wait_until policy for goto/back/forward/refresh and post-click
# load-state waits. "domcontentloaded" is 7-12x faster than "load" on
# ad-heavy sites (benchmarked) because it skips lazy images, ad iframes,
# and tracking beacons. Callers can override per-request by passing
# "wait_until" in the action JSON.
DEFAULT_WAIT_UNTIL = "domcontentloaded"

# Substrings that indicate the Playwright state (page/context/browser/frame)
# is dead and must be reset before the next action. If an error message
# matches any of these, the outer wrapper resets STATE and retries once.
# Matching is case-insensitive.
_FATAL_STATE_SUBSTRINGS = (
    "target closed",
    "target page, context or browser has been closed",
    "has been closed",
    "page has been closed",
    "page closed",
    "page crashed",
    "context was destroyed",
    "context has been destroyed",
    "browser closed",
    "browser has been closed",
    "browser has disconnected",
    "frame was detached",
    "frame has been detached",
    "disconnected",
    "connection closed",
    "protocol error",
    "websocket closed",
)


def _is_fatal_state_error(err: str) -> bool:
    """Return True if the error message indicates STATE is poisoned and
    needs to be reset before the next action can succeed."""
    if not err:
        return False
    lower = err.lower()
    return any(s in lower for s in _FATAL_STATE_SUBSTRINGS)


def _execute_action(action_data: dict) -> dict:
    """Run an action with auto-recovery.

    Normal path: delegates to _execute_action_once. If the inner attempt
    fails with a fatal state error (dead page/context/browser/frame), we
    reset STATE (tears down Playwright handles so the next ensure_ready()
    re-initializes them) and retry the same action exactly once. This
    makes the daemon self-heal after Chromium crashes, tab closures, or
    stale references without needing the caller to intervene.

    Assumes STATE.lock is already held by caller.
    """
    result = _execute_action_once(action_data)

    if result.get("ok") is False and _is_fatal_state_error(result.get("error", "")):
        # State is poisoned — tear it down and retry once.
        first_error = result.get("error", "")
        try:
            STATE.reset()
        except Exception:
            pass
        result = _execute_action_once(action_data)
        # Annotate the retry so callers can see what happened
        result["recovered_from"] = first_error[:200]

    return result


def _execute_action_once(action_data: dict) -> dict:
    """Run a single action attempt against the persistent Playwright page.

    Does NOT reset STATE or retry on failure — that's handled by the
    outer wrapper _execute_action(). This function just runs one attempt
    and returns whatever happened.
    """
    # Extract conversation_id for multi-context isolation
    conversation_id = action_data.pop("conversation_id", "__default__")

    # Handle close_context action (no page needed)
    if action_data.get("action") == "close_context":
        STATE._close_context(conversation_id)
        return {"ok": True, "closed": conversation_id}

    context, page = STATE.get_or_create_context(conversation_id)
    if page is None:
        return {"ok": False, "error": STATE.error or "Daemon not ready"}

    # Per-conversation workspace paths
    conv_workspace = f"/workspace/{conversation_id}"
    downloads_dir = f"{conv_workspace}/Downloads"
    screenshot_path = f"{conv_workspace}/.browser-screenshot.jpg"
    os.makedirs(downloads_dir, exist_ok=True)

    result: dict = {"ok": False}
    action = action_data.get("action", "")

    # Per-request wait_until override (used by navigate/back/forward/refresh
    # and as the load-state policy after a click that causes navigation).
    # Defaults to "domcontentloaded" — fastest while still giving us a
    # parsed DOM we can click and read.
    wait_until = action_data.get("wait_until") or DEFAULT_WAIT_UNTIL

    try:
        if action == "navigate":
            url = action_data.get("url", "")
            page.goto(url, wait_until=wait_until, timeout=30000)
            _inject_ad_blocker(page)
            result = {"ok": True, "url": page.url, "title": page.title()}

        elif action == "screenshot":
            result = {"ok": True, "url": page.url, "title": page.title()}

        elif action == "click":
            x = int(action_data.get("x", 0))
            y = int(action_data.get("y", 0))
            before = _click_snapshot(page, downloads_dir)
            # Wait for the element at (x, y) to be actionable before firing
            # a raw mouse.click. Without this, disabled buttons with
            # pointer-events:none silently swallow coordinate clicks.
            wait_info = _wait_for_clickable_at(page, x, y)
            page.mouse.click(x, y)
            time.sleep(1)
            if page.url != before["url"]:
                try:
                    page.wait_for_load_state(wait_until, timeout=5000)
                except Exception:
                    time.sleep(1)
            _inject_ad_blocker(page)
            result = {"ok": True,
                      **_click_verdict(page, before, f"click at ({x}, {y})", downloads_dir)}
            # Surface the actionability wait outcome so the agent can see
            # when a click was dispatched at a non-actionable target.
            if wait_info and not wait_info.get("ready"):
                result["actionability_wait"] = wait_info

        elif action == "click_element":
            selector = action_data.get("selector", "")
            before = _click_snapshot(page, downloads_dir)
            # 10-second timeout (up from 5) to give slow-initializing
            # frameworks like filehippo's more room to enable their buttons.
            page.locator(selector).first.click(timeout=10000)
            time.sleep(1)
            if page.url != before["url"]:
                try:
                    page.wait_for_load_state(wait_until, timeout=5000)
                except Exception:
                    time.sleep(1)
            _inject_ad_blocker(page)
            result = {"ok": True,
                      **_click_verdict(page, before, f"click_element({selector!r})", downloads_dir)}

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
            page.go_back(wait_until=wait_until, timeout=15000)
            _inject_ad_blocker(page)
            result = {"ok": True, "url": page.url, "title": page.title()}

        elif action == "forward":
            page.go_forward(wait_until=wait_until, timeout=15000)
            _inject_ad_blocker(page)
            result = {"ok": True, "url": page.url, "title": page.title()}

        elif action == "refresh":
            page.reload(wait_until=wait_until, timeout=15000)
            _inject_ad_blocker(page)
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
        # Just record the error — the outer _execute_action() wrapper
        # decides whether to reset STATE and retry based on the message.
        result = {"ok": False, "error": str(exc)}

    # Save screenshot to per-conversation path on shared volume.
    # If the page is dead, this will silently fail and the url/title fields
    # won't be set — that's fine, the outer wrapper will see ok=false and
    # trigger a reset+retry if the error is fatal.
    try:
        # Apply configurable delay before capturing screenshot so dynamic
        # content (animations, countdown timers, auto-download triggers)
        # has time to settle before the LLM / agent sees the page.
        delay_ms = int(action_data.get("screenshot_delay_ms", 2000) or 2000)
        delay_ms = max(0, min(30000, delay_ms))
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        screenshot_bytes = page.screenshot(type="jpeg", quality=70, full_page=False)
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)
        result["screenshot_file"] = f"{conversation_id}/.browser-screenshot.jpg"
        result["url"] = page.url
        result["title"] = page.title()
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class DaemonHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default stderr access logs
        pass

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            # Light health check — don't force Playwright init here,
            # just report whether the daemon process is alive.
            self._send_json(200, {
                "ok": True,
                "ready": STATE.ready,
                "error": STATE.error,
            })
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/action":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            action_data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return

        # Serialize all Playwright access — sync_api is not thread-safe
        with STATE.lock:
            result = _execute_action(action_data)

        self._send_json(200, result)


def main():
    port = 9223
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--port" and i + 1 < len(sys.argv) - 1:
            try:
                port = int(sys.argv[i + 2])
            except ValueError:
                pass

    # Start Chromium + Playwright eagerly so the first request is fast.
    # If it fails, the daemon still starts — requests will retry init.
    try:
        with STATE.lock:
            STATE._ensure_browser_ready()
    except Exception as exc:
        print(f"[browser_daemon] Eager init failed: {exc}", file=sys.stderr)

    # Use HTTPServer (single-threaded) — Playwright sync_api is tied to
    # the thread that created it. Since we already serialize all action
    # execution with STATE.lock, there is no benefit to multiple threads.
    server = HTTPServer(("127.0.0.1", port), DaemonHandler)
    print(
        f"[browser_daemon] Listening on http://127.0.0.1:{port} "
        f"(ready={STATE.ready}, error={STATE.error})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.reset()


if __name__ == "__main__":
    main()
