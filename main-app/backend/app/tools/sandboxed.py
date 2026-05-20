"""
Sandboxed tool wrappers using llm-sandbox.

Each factory function returns a callable with the same signature as the
original tool, but delegates execution into the llm-sandbox container
session. The agent's exec() on the host calls these wrappers, which
run code inside the Podman container.
"""

import json
import os
import shlex
from typing import Optional

import base64
import io

import structlog
from PIL import Image

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Screenshot Normalization for Screenops UI-TARS Model
# ---------------------------------------------------------------------------

def _normalize_screenshot_dimensions(width: int, height: int) -> tuple[int, int]:
    """Calculate normalized dimensions divisible by 28 while respecting UI-TARS constraints.

    UI-TARS requirements:
    - Both dimensions must be divisible by 28
    - Total pixels must be between 78,400 and 12,845,056
    - Aspect ratio must be <= 200:1

    Returns (new_width, new_height).
    """
    # Round down to nearest multiple of 28
    new_w = (width // 28) * 28
    new_h = (height // 28) * 28

    # Ensure at least 280x280 (10x10 in 28-pixel blocks, 78,400 pixels)
    if new_w < 280:
        new_w = 280
    if new_h < 280:
        new_h = 280

    # Check aspect ratio constraint (<=200:1)
    aspect = max(new_w, new_h) / min(new_w, new_h)
    if aspect > 200:
        # Clamp to 200:1 by expanding the shorter dimension
        longer = max(new_w, new_h)
        shorter = longer // 200
        if shorter < 280:
            shorter = 280
        shorter = (shorter // 28) * 28
        if new_w > new_h:
            new_h = shorter
        else:
            new_w = shorter

    # Check total pixels constraint (max 12,845,056)
    max_pixels = 12_845_056
    if new_w * new_h > max_pixels:
        # Scale down preserving aspect ratio
        scale = (max_pixels / (new_w * new_h)) ** 0.5
        new_w = int(new_w * scale)
        new_h = int(new_h * scale)
        new_w = (new_w // 28) * 28
        new_h = (new_h // 28) * 28
        if new_w < 280:
            new_w = 280
        if new_h < 280:
            new_h = 280

    return new_w, new_h


def _normalize_screenshot_image(b64_jpeg: str) -> tuple[str, int, int, int, int]:
    """Normalize screenshot for UI-TARS model by resizing to valid dimensions.

    Args:
        b64_jpeg: Base64-encoded JPEG screenshot

    Returns:
        (normalized_b64, orig_width, orig_height, norm_width, norm_height)
    """
    try:
        # Decode the image
        img_bytes = base64.b64decode(b64_jpeg)
        img = Image.open(io.BytesIO(img_bytes))

        orig_w, orig_h = img.size
        norm_w, norm_h = _normalize_screenshot_dimensions(orig_w, orig_h)

        # Resize if needed
        if (norm_w, norm_h) != (orig_w, orig_h):
            img = img.resize((norm_w, norm_h), Image.Resampling.LANCZOS)

        # Re-encode to JPEG
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=70)
        normalized_b64 = base64.b64encode(output.getvalue()).decode('ascii')

        return normalized_b64, orig_w, orig_h, norm_w, norm_h
    except Exception as e:
        logger.warning("screenshot_normalization_failed", error=str(e))
        # Return original if normalization fails
        return b64_jpeg, 0, 0, 0, 0


def _denormalize_coordinates(norm_x: float, norm_y: float, orig_width: int, orig_height: int) -> tuple[int, int]:
    """Convert normalized 0-1000 coordinates back to pixel coordinates.

    The vision model returns coordinates on a 0-1000 normalized scale.
    This converts them back to the original image dimensions.

    Args:
        norm_x: Normalized X coordinate (0-1000)
        norm_y: Normalized Y coordinate (0-1000)
        orig_width: Original screenshot width in pixels
        orig_height: Original screenshot height in pixels

    Returns:
        (pixel_x, pixel_y)
    """
    pixel_x = int((norm_x / 1000.0) * orig_width)
    pixel_y = int((norm_y / 1000.0) * orig_height)
    return pixel_x, pixel_y


def _run_python(session, code: str):
    """Run Python code inside the container using session.execute_command.

    Uses podman exec (CLI) instead of session.run() which copies files
    via the Podman Python library's SSH connection. The SSH connection
    gets overwhelmed in long chats with many tool calls and drops.

    This sends the code as base64-encoded string to avoid shell escaping
    issues with quotes, newlines, etc.
    """
    encoded = base64.b64encode(code.encode()).decode()
    return session.execute_command(
        f"python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode())\""
    )


def _run_shell(session, cmd: str):
    """Run a shell command inside the container using session.execute_command."""
    return session.execute_command(cmd)


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

def make_sandboxed_terminal(session, conversation_id=None, output_callback=None):
    """Return a sandboxed terminal(command) function.

    Uses podman exec for clean output to the LLM. Forwards the command
    and output to the Terminal tab WebSocket with proper formatting.
    """

    def terminal(command: str) -> str:
        """Execute a shell command inside the sandbox container."""
        command = command.strip()
        if not command:
            return "Error: empty command"

        try:
            # Run via podman exec — clean output, no markers
            result = session.execute_command(f"cd /workspace && {command}")
            output = result.stdout or ""
            if result.stderr:
                output += f"\n{result.stderr}" if output else result.stderr

            if len(output) > 100_000:
                output = output[:100_000] + "\n[output truncated at 100KB]"

            if output_callback and output:
                output_callback(output)

            # Write command into PTY shell so it appears in Terminal tab
            # scrollback (visible when user opens Terminal tab later).
            if conversation_id:
                try:
                    from app.services.pty_manager import pty_manager
                    pty_session = pty_manager.get(conversation_id)
                    if pty_session:
                        if pty_session.fd >= 0 and not pty_session._closed:
                            # PTY ready — write command directly
                            try:
                                import os as _os
                                _os.write(pty_session.fd, f"{command}\n".encode())
                            except OSError:
                                pass
                        else:
                            # PTY pending — queue command for replay when ready
                            if not hasattr(pty_session, '_queued_commands'):
                                pty_session._queued_commands = []
                            pty_session._queued_commands.append(command)
                except Exception:
                    pass

            return output if output.strip() else "(no output)"
        except Exception as exc:
            logger.error("sandboxed_terminal_error", command=command[:80], error=str(exc))
            return f"Error: {exc}"

    return terminal


# ---------------------------------------------------------------------------
# File Operations
# ---------------------------------------------------------------------------

def make_sandboxed_read_file(session):
    """Return a sandboxed read_file(file_path) function."""

    def read_file(file_path: str) -> str:
        """Read a file from the sandbox filesystem."""
        try:
            code = f"""
import os
path = {json.dumps(file_path)}
path = os.path.expanduser(path)
if not os.path.isabs(path):
    path = os.path.join('/workspace', path)
if not os.path.isfile(path):
    print(f"Error: file not found: {{path}}")
elif os.path.getsize(path) > 500000:
    print(f"Error: file too large ({{os.path.getsize(path)}} bytes, max 500KB)")
else:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        print(f.read())
"""
            result = _run_python(session, code)
            return result.stdout if result.stdout else f"Error: {result.stderr or 'unknown error'}"
        except Exception as exc:
            return f"Error reading file: {exc}"

    return read_file


def make_sandboxed_write_file(session):
    """Return a sandboxed write_file(file_path, text) function.

    Content is piped via stdin to avoid ARG_MAX limits on `docker exec`.
    Embedding large content in the command string would fail with
    `[Errno 7] Argument list too long`.
    """

    def write_file(file_path: str, text: str) -> str:
        """Write a file in the sandbox filesystem."""
        try:
            # Only the small `path` is encoded in the script body. The
            # potentially-large file content arrives via stdin.
            code = f"""
import os, sys
path = os.path.expanduser({json.dumps(file_path)})
if not os.path.isabs(path):
    path = os.path.join('/workspace', path)
parent = os.path.dirname(path)
if parent:
    os.makedirs(parent, exist_ok=True)
data = sys.stdin.read()
with open(path, 'w', encoding='utf-8') as f:
    f.write(data)
size = os.path.getsize(path)
print(f"Wrote {{size}} bytes to {{path}}")
"""
            encoded = base64.b64encode(code.encode()).decode()
            result = session.execute_command(
                f"python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode())\"",
                stdin_data=text,
            )
            return result.stdout if result.stdout else f"Error: {result.stderr or 'unknown error'}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    return write_file


def make_sandboxed_delete_file(session):
    """Return a sandboxed delete_file(file_path) function."""

    def delete_file(file_path: str) -> str:
        """Delete a file from the sandbox filesystem."""
        try:
            code = f"""
import os
path = os.path.expanduser({json.dumps(file_path)})
if not os.path.isabs(path):
    path = os.path.join('/workspace', path)
if not os.path.exists(path):
    print(f"Error: path not found: {{path}}")
elif os.path.isdir(path):
    print(f"Error: {{path}} is a directory, not a file")
else:
    os.remove(path)
    print(f"Deleted {{path}}")
"""
            result = _run_python(session, code)
            return result.stdout if result.stdout else f"Error: {result.stderr or 'unknown error'}"
        except Exception as exc:
            return f"Error deleting file: {exc}"

    return delete_file


def make_sandboxed_download_file(session):
    """Return a sandboxed download_file(url, file_path) function."""

    def download_file(url: str, file_path: str) -> str:
        """Download a file from a URL into the sandbox filesystem."""
        try:
            code = f"""
import httpx, os
path = os.path.expanduser({json.dumps(file_path)})
if not os.path.isabs(path):
    path = os.path.join('/workspace', path)
parent = os.path.dirname(path)
if parent:
    os.makedirs(parent, exist_ok=True)
try:
    with httpx.stream("GET", {json.dumps(url)}, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        total = 0
        with open(path, 'wb') as f:
            for chunk in r.iter_bytes(8192):
                total += len(chunk)
                if total > 512_000_000:
                    print("Error: file exceeds 512MB limit")
                    break
                f.write(chunk)
        if total <= 512_000_000:
            print(f"Downloaded {{total}} bytes to {{path}}")
except httpx.HTTPError as e:
    print(f"Error downloading: {{e}}")
"""
            result = _run_python(session, code)
            return result.stdout if result.stdout else f"Error: {result.stderr or 'unknown error'}"
        except Exception as exc:
            return f"Error downloading file: {exc}"

    return download_file


def make_sandboxed_str_replace(session):
    """Return a sandboxed str_replace(file_path, old_string, new_string, replace_all) function."""

    def str_replace(
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """Replace text in a file in the sandbox filesystem."""
        try:
            code = f"""
import os
path = os.path.expanduser({json.dumps(file_path)})
if not os.path.isabs(path):
    path = os.path.join('/workspace', path)
if not os.path.isfile(path):
    print(f"Error: file not found: {{path}}")
else:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    old = {json.dumps(old_string)}
    new = {json.dumps(new_string)}
    count = content.count(old)
    if count == 0:
        print("Error: old_string not found in file")
    elif count > 1 and not {replace_all}:
        print(f"Error: {{count}} matches found. Set replace_all=True to replace all.")
    else:
        new_content = content.replace(old, new) if {replace_all} else content.replace(old, new, 1)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Replaced {{count if {replace_all} else 1}} occurrence(s) in {{path}}")
"""
            result = _run_python(session, code)
            return result.stdout if result.stdout else f"Error: {result.stderr or 'unknown error'}"
        except Exception as exc:
            return f"Error in str_replace: {exc}"

    return str_replace


# ---------------------------------------------------------------------------
# Search Tools
# ---------------------------------------------------------------------------

def make_sandboxed_grep(session):
    """Return a sandboxed grep(pattern, path, ...) function."""

    def grep(
        pattern: str,
        path: str = ".",
        glob_filter: Optional[str] = None,
        case_insensitive: bool = False,
        max_results: int = 200,
        output_mode: str = "content",
        context_lines: int = 0,
    ) -> str:
        """Search for a regex pattern in files inside the sandbox."""
        try:
            cmd_parts = ["rg"]
            if case_insensitive:
                cmd_parts.append("-i")
            if glob_filter:
                cmd_parts.extend(["--glob", shlex.quote(glob_filter)])
            if context_lines > 0:
                cmd_parts.extend(["-C", str(context_lines)])
            if output_mode == "files_with_matches":
                cmd_parts.append("-l")
            elif output_mode == "count":
                cmd_parts.append("-c")
            cmd_parts.extend(["--max-count", str(max_results)])
            cmd_parts.append(shlex.quote(pattern))
            cmd_parts.append(shlex.quote(path))

            cmd = " ".join(cmd_parts)
            result = _run_shell(session, cmd)
            output = result.stdout or ""
            if len(output) > 80_000:
                output = output[:80_000] + "\n[output truncated]"
            return output if output.strip() else "No matches found"
        except Exception as exc:
            return f"Error in grep: {exc}"

    return grep


def make_sandboxed_glob_search(session):
    """Return a sandboxed glob_search(pattern, directory, ...) function."""

    def glob_search(
        pattern: str,
        directory: str = ".",
        max_results: int = 500,
    ) -> str:
        """Find files by glob pattern inside the sandbox."""
        try:
            code = f"""
import glob, os
base = os.path.expanduser({json.dumps(directory)})
pat = {json.dumps(pattern)}
if not pat.startswith('**/'):
    pat = '**/' + pat
matches = sorted(glob.glob(os.path.join(base, pat), recursive=True))
for m in matches[:{max_results}]:
    try:
        size = os.path.getsize(m)
        print(f"{{m}} ({{size}} bytes)")
    except OSError:
        print(m)
if not matches:
    print("No matches found")
elif len(matches) > {max_results}:
    print(f"[{{len(matches) - {max_results}}} more results truncated]")
"""
            result = _run_python(session, code)
            return result.stdout if result.stdout else "No matches found"
        except Exception as exc:
            return f"Error in glob_search: {exc}"

    return glob_search


# ---------------------------------------------------------------------------
# Web Tools
# ---------------------------------------------------------------------------

def make_sandboxed_web_search(session, serper_api_key: str = ""):
    """Return a sandboxed web_search(query, ...) function."""

    def web_search(
        query: str,
        num_results: int = 10,
        gl: str = "us",
        hl: str = "en",
    ) -> str:
        """Search the web from inside the sandbox."""
        try:
            if serper_api_key:
                code = f"""
import httpx, json
try:
    r = httpx.post(
        "https://google.serper.dev/search",
        headers={{"X-API-KEY": {json.dumps(serper_api_key)}, "Content-Type": "application/json"}},
        json={{"q": {json.dumps(query)}, "num": {num_results}, "gl": {json.dumps(gl)}, "hl": {json.dumps(hl)}}},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    # Knowledge graph
    kg = data.get("knowledgeGraph")
    if kg:
        print(f"## {{kg.get('title', '')}}\\n{{kg.get('description', '')}}\\n")
    # Organic results
    for item in data.get("organic", []):
        print(f"**{{item.get('title', '')}}**\\n{{item.get('link', '')}}\\n{{item.get('snippet', '')}}\\n")
    # People also ask
    for q in data.get("peopleAlsoAsk", [])[:3]:
        print(f"Q: {{q.get('question', '')}}\\nA: {{q.get('snippet', '')}}\\n")
except httpx.HTTPError as e:
    print(f"Search error: {{e}}")
"""
            else:
                code = f"""
from ddgs import DDGS
try:
    with DDGS() as ddgs:
        results = list(ddgs.text({json.dumps(query)}, max_results={num_results}))
        for r in results:
            print(f"**{{r.get('title', '')}}**\\n{{r.get('href', '')}}\\n{{r.get('body', '')}}\\n")
        if not results:
            print("No results found")
except Exception as e:
    print(f"Search error: {{e}}")
"""
            result = _run_python(session, code)
            return result.stdout if result.stdout else f"Error: {result.stderr or 'no results'}"
        except Exception as exc:
            return f"Error in web_search: {exc}"

    return web_search


def make_sandboxed_web_fetch(session):
    """Return a sandboxed web_fetch(url) function."""

    def web_fetch(url: str) -> str:
        """Fetch and extract content from a URL inside the sandbox."""
        try:
            code = f"""
import httpx
try:
    r = httpx.get({json.dumps(url)}, follow_redirects=True, timeout=30,
                  headers={{"User-Agent": "Mozilla/5.0"}})
    r.raise_for_status()
    try:
        from trafilatura import extract
        text = extract(r.text)
        if text:
            print(text[:200000])
        else:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()
            print(soup.get_text(separator='\\n', strip=True)[:200000])
    except ImportError:
        print(r.text[:200000])
except httpx.HTTPError as e:
    print(f"Fetch error: {{e}}")
"""
            result = _run_python(session, code)
            output = result.stdout or ""
            if len(output) > 200_000:
                output = output[:200_000] + "\n[content truncated]"
            return output if output.strip() else f"Error: {result.stderr or 'empty response'}"
        except Exception as exc:
            return f"Error in web_fetch: {exc}"

    return web_fetch


def make_sandboxed_web_advanced(session, serper_api_key: str = ""):
    """Return a sandboxed web_advanced(...) function."""

    def web_advanced(
        query: str,
        num_results: int = 10,
        site: str = "",
        filetype: str = "",
        inurl: str = "",
        intitle: str = "",
        before: str = "",
        after: str = "",
        exclude: list = None,
        exact: str = "",
        or_terms: list = None,
    ) -> str:
        """Advanced web search with operators inside the sandbox."""
        # Build the advanced query string
        parts = [query]
        if site:
            parts.append(f"site:{site}")
        if filetype:
            parts.append(f"filetype:{filetype}")
        if inurl:
            parts.append(f"inurl:{inurl}")
        if intitle:
            parts.append(f"intitle:{intitle}")
        if before:
            parts.append(f"before:{before}")
        if after:
            parts.append(f"after:{after}")
        if exact:
            parts.append(f'"{exact}"')
        for term in (exclude or []):
            parts.append(f"-{term}")
        if or_terms:
            parts.append("(" + " OR ".join(or_terms) + ")")

        full_query = " ".join(parts)
        # Delegate to regular web_search
        search_fn = make_sandboxed_web_search(session, serper_api_key)
        return search_fn(query=full_query, num_results=num_results)

    return web_advanced


def make_sandboxed_todo_write(session):
    """Return a sandboxed todo_write(todos_json, merge) function.

    Note: todo state lives in the container's memory for the session.
    """

    def todo_write(todos_json: str, merge: bool = True) -> str:
        """Manage in-session task list inside the sandbox."""
        try:
            code = f"""
import json

# Simple in-memory todo storage
if not hasattr(__builtins__, '_todo_store'):
    if isinstance(__builtins__, dict):
        __builtins__['_todo_store'] = []
    else:
        __builtins__._todo_store = []

store = __builtins__._todo_store if isinstance(__builtins__._todo_store, list) else getattr(__builtins__, '_todo_store', [])

try:
    new_todos = json.loads({json.dumps(todos_json)})
except json.JSONDecodeError as e:
    print(f"Error: invalid JSON: {{e}}")
else:
    if {merge}:
        existing = {{t.get('id'): t for t in store}}
        for t in new_todos:
            tid = t.get('id', str(len(existing)))
            existing[tid] = t
        store.clear()
        store.extend(existing.values())
    else:
        store.clear()
        store.extend(new_todos)

    # Format output
    for t in store:
        status = t.get('status', 'pending')
        icon = {{'pending': '[ ]', 'in_progress': '[~]', 'completed': '[x]', 'cancelled': '[-]'}}.get(status, '[ ]')
        print(f"{{icon}} {{t.get('content', '')}}")
"""
            result = _run_python(session, code)
            return result.stdout if result.stdout else "Todo list updated"
        except Exception as exc:
            return f"Error in todo_write: {exc}"

    return todo_write


# ---------------------------------------------------------------------------
# Auto Browser (Playwright + Chromium inside container)
# ---------------------------------------------------------------------------

def make_sandboxed_auto_browser(session, output_callback=None, coord_finder_invoker=None, conversation_id=None, screenshot_delay_ms=2000):
    """Return a sandboxed auto_browser function.

    Forwards each action via browser_control.py to the long-lived
    browser_daemon.py inside the container, which keeps Playwright + Chromium
    connected across requests. All actions return JSON with screenshot.

    *coord_finder_invoker* — optional callable (runs on host) for vision_click.
    *conversation_id* — used to find the BrowserWorkspace WebSocket clients
    so screenshots + URL updates can be pushed to the Browser tab UI.
    """

    def _broadcast_to_browser_tab(data: dict) -> None:
        """Push the latest screenshot, URL and title to the BrowserWorkspace
        WebSocket clients so the UI address bar stays in sync with whatever
        the agent is doing. Safe to call from a worker thread."""
        if not conversation_id:
            return
        try:
            from app.services.browser_manager import browser_manager as _bm
            bsession = _bm._sessions.get(conversation_id)
            if not bsession or not bsession.websocket_clients:
                return
            # Keep BrowserSession state in sync with agent actions
            if data.get("url"):
                bsession.current_url = data.get("url")
            if data.get("title"):
                bsession.current_title = data.get("title")
            # Read screenshot from the shared volume root
            # (browser_daemon.py writes JPEG to /workspace/.browser-screenshot.jpg).
            ss_path = os.path.join(session.volume_path, ".browser-screenshot.jpg")
            try:
                with open(ss_path, "rb") as f:
                    ss_b64 = base64.b64encode(f.read()).decode("ascii")
            except Exception:
                return
            message = json.dumps({
                "type": "screenshot",
                "data": ss_b64,
                "url": data.get("url") or bsession.current_url or "",
                "title": data.get("title") or bsession.current_title or "",
            })
            # Broadcast to every connected client using the main event loop
            # (we're in a worker thread; `asyncio.run(...)` would fail because
            # the WebSocket objects are bound to the FastAPI loop).
            try:
                import asyncio as _asyncio
                loop = _bm._main_loop if hasattr(_bm, "_main_loop") else None
                for ws in list(bsession.websocket_clients):
                    try:
                        if loop is not None:
                            _asyncio.run_coroutine_threadsafe(ws.send_text(message), loop)
                        else:
                            _asyncio.run(ws.send_text(message))
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as _bcast_err:
            logger.debug("sandboxed_browser_ws_broadcast_failed", error=str(_bcast_err))

    def _broadcast_click_indicator(x: int, y: int) -> None:
        """Send a click_indicator message so the frontend shows a dot+ripple."""
        if not conversation_id:
            return
        try:
            from app.services.browser_manager import browser_manager as _bm
            bsession = _bm._sessions.get(conversation_id)
            if not bsession or not bsession.websocket_clients:
                return
            message = json.dumps({"type": "click_indicator", "x": x, "y": y})
            try:
                import asyncio as _asyncio
                loop = _bm._main_loop if hasattr(_bm, "_main_loop") else None
                for ws in list(bsession.websocket_clients):
                    try:
                        if loop is not None:
                            _asyncio.run_coroutine_threadsafe(ws.send_text(message), loop)
                        else:
                            _asyncio.run(ws.send_text(message))
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    _ACTION_LABELS = {
        "navigate": "Navigating…",
        "click": "Clicking…",
        "click_element": "Clicking element…",
        "vision_click": "Finding & clicking…",
        "type": "Typing…",
        "fill": "Filling field…",
        "scroll": "Scrolling…",
        "screenshot": "Taking screenshot…",
        "press_key": "Pressing key…",
        "back": "Going back…",
        "forward": "Going forward…",
        "refresh": "Refreshing…",
        "wait": "Waiting…",
        "run_js": "Running script…",
        "get_page_info": "Reading page…",
    }

    def _broadcast_browser_control(active: bool, action: str = "") -> None:
        """Send browser_control message so the frontend shows/hides the AI overlay."""
        if not conversation_id:
            return
        try:
            from app.services.browser_manager import browser_manager as _bm
            bsession = _bm._sessions.get(conversation_id)
            if not bsession or not bsession.websocket_clients:
                return
            label = _ACTION_LABELS.get(action, action) if active else ""
            message = json.dumps({
                "type": "browser_control",
                "active": active,
                "action": label,
            })
            try:
                import asyncio as _asyncio
                loop = _bm._main_loop if hasattr(_bm, "_main_loop") else None
                for ws in list(bsession.websocket_clients):
                    try:
                        if loop is not None:
                            _asyncio.run_coroutine_threadsafe(ws.send_text(message), loop)
                        else:
                            _asyncio.run(ws.send_text(message))
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    def _run_in_sandbox(payload: dict) -> dict:
        """Send an action payload to browser_control.py (thin HTTP client that
        forwards to the long-lived browser_daemon.py) and return parsed JSON."""
        payload_json = json.dumps(payload)
        escaped = payload_json.replace("'", "'\\''")
        result = session.execute_command(
            f"python3 /opt/browser_control.py '{escaped}'"
        )
        stdout = result.stdout or ""
        if not stdout.strip():
            return {"error": f"No output from browser_control: {result.stderr or 'unknown'}"}
        try:
            return json.loads(stdout.strip())
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON from browser_control: {stdout[:200]}"}

    def auto_browser(
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
        value: str = "",
    ) -> str:
        """Control a browser inside the sandbox container."""
        logger.debug("sandboxed_auto_browser_call", action=action, url=url)
        _broadcast_browser_control(True, action)
        try:
            # ── vision_click: screenshot from sandbox → host coord finder → click in sandbox ──
            if action == "vision_click":
                if not text:
                    return json.dumps({"error": "text is required for vision_click — describe the element to click"})
                if not coord_finder_invoker:
                    return json.dumps({
                        "error": "vision_click requires a coordinate finder API. "
                        "Configure screenops_api_key in Settings, or use click_element(selector) instead."
                    })
                # 0. Get URL before click so we can report before→after
                _before_url_data = _run_in_sandbox({"action": "get_url", "conversation_id": conversation_id or "__default__", "screenshot_delay_ms": screenshot_delay_ms})
                _before_url = _before_url_data.get("url") or "unknown"
                # 1. Take screenshot inside sandbox (writes to per-conversation path)
                ss_data = _run_in_sandbox({"action": "screenshot", "conversation_id": conversation_id or "__default__", "screenshot_delay_ms": screenshot_delay_ms})
                if "error" in ss_data:
                    return json.dumps({"error": f"vision_click screenshot failed: {ss_data['error']}"})
                # 2. Read the screenshot from the shared volume on the host
                b64 = ""
                conv_id = conversation_id or "__default__"
                screenshot_path = os.path.join(session.volume_path, conv_id, ".browser-screenshot.jpg")
                try:
                    with open(screenshot_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                except Exception as e:
                    logger.warning("vision_click_screenshot_read_failed", path=screenshot_path, error=str(e))
                if not b64:
                    return json.dumps({"error": "No screenshot data returned for vision_click"})
                # 3. Get actual image dimensions from the screenshot file
                try:
                    img = Image.open(io.BytesIO(base64.b64decode(b64)))
                    vp_w, vp_h = img.size
                except Exception:
                    vp_w = 1280
                    vp_h = 900
                # 4. Invoke coordinate finder (handles normalization + denormalization internally)
                coords, usage = coord_finder_invoker(b64, text, vp_w, vp_h, vp_w, vp_h)
                if not coords or coords.get("x") is None or coords.get("y") is None:
                    return json.dumps({"error": f"Could not find element: '{text}'. Try a more specific description or use click_element(selector)."})
                # 5. Click at coordinates inside sandbox (already denormalized to screen pixels)
                click_data = _run_in_sandbox({"action": "click", "x": int(coords["x"]), "y": int(coords["y"]), "conversation_id": conversation_id or "__default__", "screenshot_delay_ms": screenshot_delay_ms})
                click_data["vision_click"] = True
                click_data["target"] = text
                click_data["clicked"] = {"x": int(coords["x"]), "y": int(coords["y"])}
                if usage:
                    click_data["coord_finder_tokens"] = usage
                # Build status summary (URL before → after, downloads)
                _after_url = click_data.get("url") or _before_url
                _url_changed = _after_url != _before_url
                _downloaded = click_data.get("downloaded", [])
                _nav_note = f"navigated → {_after_url}" if _url_changed else "URL unchanged (no navigation)"
                _dl_note = f"downloaded={_downloaded}" if _downloaded else "downloads=none"
                click_data["status_summary"] = (
                    f"vision_click '{text}' at ({coords['x']},{coords['y']}) | "
                    f"URL before: {_before_url} | URL after: {_after_url} | {_nav_note} | {_dl_note}. "
                    f"Screenshot attached — no need to call screenshot() again."
                )
                # Send click indicator before screenshot so ripple appears first
                _broadcast_click_indicator(int(coords["x"]), int(coords["y"]))
                # Broadcast to Browser tab (screenshot + URL + title)
                if click_data.get("screenshot_file"):
                    _broadcast_to_browser_tab(click_data)
                _broadcast_browser_control(False)
                return json.dumps(click_data)

            # ── All other actions: forward to browser_control.py (which
            #    calls the long-lived browser_daemon.py inside the container) ──
            # For click actions capture URL before so we can report before→after
            _pre_url = None
            if action in ("click", "click_element"):
                _pre_url_data = _run_in_sandbox({"action": "get_url", "conversation_id": conversation_id or "__default__", "screenshot_delay_ms": screenshot_delay_ms})
                _pre_url = _pre_url_data.get("url") or "unknown"

            payload = {"action": action, "conversation_id": conversation_id or "__default__", "screenshot_delay_ms": screenshot_delay_ms}
            if url:
                payload["url"] = url
            if selector:
                payload["selector"] = selector
            if text:
                payload["text"] = text
            if x or y:
                payload["x"] = x
                payload["y"] = y
            if key:
                payload["key"] = key
            if action == "scroll":
                payload["direction"] = direction
                payload["amount"] = amount
            if js_expression:
                payload["js_expression"] = js_expression
            if action == "wait":
                payload["seconds"] = seconds
            if value:
                payload["value"] = value

            data = _run_in_sandbox(payload)

            # Add status_summary for navigate and click actions
            if action == "navigate":
                _nav_url = data.get("url") or url or "unknown"
                _nav_title = data.get("title") or "unknown"
                data["status_summary"] = (
                    f"Navigated to: {_nav_url} | Title: {_nav_title}. "
                    f"Screenshot attached — no need to call screenshot() again."
                )
            elif action in ("click", "click_element"):
                _post_url = data.get("url") or _pre_url or "unknown"
                _url_changed = _pre_url and _post_url != _pre_url
                _downloaded = data.get("downloaded", [])
                _nav_note = f"navigated → {_post_url}" if _url_changed else "URL unchanged (no navigation)"
                _dl_note = f"downloaded={_downloaded}" if _downloaded else "downloads=none"
                data["status_summary"] = (
                    f"Click performed | URL before: {_pre_url} | URL after: {_post_url} | "
                    f"{_nav_note} | {_dl_note}. "
                    f"Screenshot attached — no need to call screenshot() again."
                )

            # Send click indicator for click actions
            if action in ("click", "click_element") and data.get("ok"):
                click_x = data.get("clicked_x") or x
                click_y = data.get("clicked_y") or y
                if click_x and click_y:
                    _broadcast_click_indicator(int(click_x), int(click_y))

            # Broadcast to Browser tab (screenshot + URL + title)
            if data.get("screenshot_file"):
                _broadcast_to_browser_tab(data)

            _broadcast_browser_control(False)
            return json.dumps(data)

        except Exception as exc:
            _broadcast_browser_control(False)
            logger.error("sandboxed_auto_browser_error", action=action, error=str(exc))
            return json.dumps({"error": str(exc)})

    return auto_browser
