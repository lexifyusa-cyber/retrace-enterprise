"""
Sandboxed tool wrappers using llm-sandbox.

Each factory function returns a callable with the same signature as the
original tool, but delegates execution into the llm-sandbox container
session. The agent's exec() on the host calls these wrappers, which
run code inside the Podman container.
"""

import json
import shlex
from typing import Optional

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

def make_sandboxed_terminal(session, conversation_id=None, output_callback=None):
    """Return a sandboxed terminal(command) function.

    Uses podman exec (via session.execute_command) for clean output.
    Forwards the command + output to the Terminal tab's WebSocket so
    it shows there too.
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

            # Truncate large output
            if len(output) > 100_000:
                output = output[:100_000] + "\n[output truncated at 100KB]"

            if output_callback and output:
                output_callback(output)

            # Forward to Terminal tab WebSocket so it shows there
            if conversation_id:
                try:
                    from app.services.pty_manager import pty_manager
                    session_pty = pty_manager._sessions.get(conversation_id)
                    if session_pty and session_pty.websocket_clients:
                        import asyncio
                        # Format: show command and output cleanly
                        display = f"\r\n\033[1;33m$ {command}\033[0m\r\n{output}\r\n"
                        loop = pty_manager._event_loop
                        if loop and loop.is_running():
                            for ws in list(session_pty.websocket_clients):
                                try:
                                    asyncio.run_coroutine_threadsafe(
                                        ws.send_json({"type": "output", "data": display}),
                                        loop,
                                    )
                                except Exception:
                                    pass
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
            result = session.run(code)
            return result.stdout if result.stdout else f"Error: {result.stderr or 'unknown error'}"
        except Exception as exc:
            return f"Error reading file: {exc}"

    return read_file


def make_sandboxed_write_file(session):
    """Return a sandboxed write_file(file_path, text) function."""

    def write_file(file_path: str, text: str) -> str:
        """Write a file in the sandbox filesystem."""
        try:
            code = f"""
import os
path = os.path.expanduser({json.dumps(file_path)})
if not os.path.isabs(path):
    path = os.path.join('/workspace', path)
parent = os.path.dirname(path)
if parent:
    os.makedirs(parent, exist_ok=True)
with open(path, 'w', encoding='utf-8') as f:
    f.write({json.dumps(text)})
size = os.path.getsize(path)
print(f"Wrote {{size}} bytes to {{path}}")
"""
            result = session.run(code)
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
            result = session.run(code)
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
            result = session.run(code)
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
            result = session.run(code)
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
            result = session.run(cmd)
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
            result = session.run(code)
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
from duckduckgo_search import DDGS
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
            result = session.run(code)
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
            result = session.run(code)
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
            result = session.run(code)
            return result.stdout if result.stdout else "Todo list updated"
        except Exception as exc:
            return f"Error in todo_write: {exc}"

    return todo_write


# ---------------------------------------------------------------------------
# Auto Browser (Playwright + Chromium inside container)
# ---------------------------------------------------------------------------

def make_sandboxed_auto_browser(session, output_callback=None):
    """Return a sandboxed auto_browser function.

    Runs browser_agent.py inside the container which manages a persistent
    Chromium instance via Playwright. All actions return JSON with screenshot.
    """

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
        try:
            # Build action payload
            payload = {"action": action}
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

            payload_json = json.dumps(payload)
            # Escape single quotes for shell
            escaped = payload_json.replace("'", "'\\''")

            result = session.execute_command(
                f"python3 /opt/browser_agent.py '{escaped}'"
            )

            stdout = result.stdout or ""
            if not stdout.strip():
                return json.dumps({"error": f"No output from browser_agent: {result.stderr or 'unknown'}"})

            # Parse and return the JSON result
            try:
                data = json.loads(stdout.strip())
            except json.JSONDecodeError:
                return json.dumps({"error": f"Invalid JSON from browser_agent: {stdout[:200]}"})

            # Broadcast screenshot to WebSocket if available
            if output_callback and data.get("screenshot"):
                try:
                    import asyncio
                    ws_data = json.dumps({
                        "type": "screenshot",
                        "data": data["screenshot"],
                        "url": data.get("url", ""),
                        "title": data.get("title", ""),
                    })
                    output_callback(ws_data)
                except Exception:
                    pass

            return json.dumps(data)

        except Exception as exc:
            logger.error("sandboxed_auto_browser_error", action=action, error=str(exc))
            return json.dumps({"error": str(exc)})

    return auto_browser
