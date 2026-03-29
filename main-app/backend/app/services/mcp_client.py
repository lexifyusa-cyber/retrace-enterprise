"""
MCP Client — connects to MCP servers via stdio transport and
exposes their tools as LangChain StructuredTools.

Supports both JSON-RPC framing styles:
- Bare JSON lines (newline-delimited, used by MCP SDK 1.x)
- Content-Length framing (used by some servers)
"""

import fcntl
import json
import os
import subprocess
import time
from typing import Any, Optional

import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger()


class McpClientError(Exception):
    pass


def _start_mcp_process(
    name: str,
    command: str,
    args: list[str],
    env: dict[str, str],
    cwd: str = None,
) -> subprocess.Popen:
    """Start an MCP server process with stdio transport."""
    full_env = {**os.environ, **env}
    cmd = [command] + args

    # Fix: if command is python and args use -m module.server,
    # the if __name__ == "__main__" guard won't fire.
    # Rewrite to directly import and run.
    if len(args) >= 2 and args[0] == "-m":
        module_name = args[1]
        extra_args = args[2:]
        # Try to infer cwd from command path for proper module resolution
        if not cwd and "/mcp-servers/" in command:
            # e.g. /Users/x/mcp-servers/chrome_1/.venv/bin/python → /Users/x/mcp-servers/chrome_1
            parts = command.split("/mcp-servers/")
            if len(parts) == 2:
                server_dir_name = parts[1].split("/")[0]
                cwd = parts[0] + "/mcp-servers/" + server_dir_name
        # Check for src/ directory and add to PYTHONPATH
        if cwd and os.path.isdir(os.path.join(cwd, "src")):
            existing = full_env.get("PYTHONPATH", "")
            src_path = os.path.join(cwd, "src")
            full_env["PYTHONPATH"] = f"{src_path}:{existing}" if existing else src_path

        # Use -c to bypass __main__ guard
        run_code = f"from {module_name} import mcp; mcp.run()"
        cmd = [command, "-c", run_code] + extra_args

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            cwd=cwd,
        )
        # Make stdout non-blocking for timeout support
        fd = proc.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        logger.info("mcp_server_started", name=name, pid=proc.pid, cmd=cmd)
        return proc
    except FileNotFoundError:
        raise McpClientError(f"Command not found: {command}")
    except Exception as e:
        raise McpClientError(f"Failed to start MCP server '{name}': {e}")


def _send_jsonrpc(proc: subprocess.Popen, method: str, params: dict = None, req_id: int = None) -> dict:
    """Send a JSON-RPC request as a bare JSON line and read the response."""
    if proc.poll() is not None:
        raise McpClientError("MCP server process has exited")

    request: dict = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        request["id"] = req_id
    if params:
        request["params"] = params

    line = json.dumps(request) + "\n"
    try:
        proc.stdin.write(line.encode("utf-8"))
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        raise McpClientError(f"Failed to write to MCP server: {e}")

    # If no id (notification), don't wait for response
    if req_id is None:
        return {}

    return _read_jsonrpc_response(proc, req_id=req_id)


def _read_jsonrpc_response(proc: subprocess.Popen, req_id: int, timeout: float = 10.0) -> dict:
    """Read JSON lines from stdout until we get the response matching req_id."""
    import select as sel

    deadline = time.time() + timeout
    buffer = b""

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise McpClientError(f"MCP server response timed out after {timeout}s")

        ready, _, _ = sel.select([proc.stdout], [], [], min(remaining, 0.5))
        if not ready:
            if proc.poll() is not None:
                stderr = ""
                try:
                    stderr = proc.stderr.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                raise McpClientError(f"MCP server exited (code {proc.returncode}): {stderr}")
            continue

        try:
            chunk = os.read(proc.stdout.fileno(), 8192)
        except BlockingIOError:
            continue
        if not chunk:
            raise McpClientError("MCP server closed stdout")

        buffer += chunk

        # Process complete lines
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue

            # Skip Content-Length headers (some servers use framing)
            if line.lower().startswith(b"content-length"):
                continue

            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            # Skip notifications (no id)
            if "id" not in msg:
                continue

            # Check if this is our response
            if msg.get("id") == req_id:
                return msg

    raise McpClientError("Unexpected end of response stream")


def _initialize_server(proc: subprocess.Popen, name: str) -> dict:
    """Send initialize + initialized to the MCP server."""
    init_resp = _send_jsonrpc(proc, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "retrace", "version": "1.0.0"},
    }, req_id=1)

    if "error" in init_resp:
        raise McpClientError(f"MCP init error: {init_resp['error']}")

    # Send initialized notification (no id)
    _send_jsonrpc(proc, "notifications/initialized", {})

    return init_resp.get("result", {})


def discover_tools(
    name: str,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> list[dict]:
    """Start an MCP server, discover tools, then shut it down."""
    proc = _start_mcp_process(name, command, args, env)
    try:
        # Give server a moment to start
        time.sleep(0.5)
        _initialize_server(proc, name)

        tools_resp = _send_jsonrpc(proc, "tools/list", {}, req_id=2)
        if "error" in tools_resp:
            raise McpClientError(f"tools/list error: {tools_resp['error']}")

        tools = tools_resp.get("result", {}).get("tools", [])
        return [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {}),
            }
            for t in tools
        ]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def call_tool(
    proc: subprocess.Popen,
    tool_name: str,
    arguments: dict,
    req_id: int = 100,
) -> str:
    """Call a tool on a running MCP server and return the result as a string."""
    resp = _send_jsonrpc(proc, "tools/call", {
        "name": tool_name,
        "arguments": arguments,
    }, req_id=req_id)

    if "error" in resp:
        return f"MCP tool error: {resp['error']}"

    result = resp.get("result", {})
    content = result.get("content", [])

    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif isinstance(block, str):
            texts.append(block)

    return "\n".join(texts) if texts else json.dumps(result)


def get_mcp_tools_for_agent(
    configs: list[dict],
) -> tuple[list[StructuredTool], dict[str, subprocess.Popen]]:
    """
    Start enabled MCP servers and return their tools as LangChain StructuredTools.
    """
    tools = []
    processes = {}
    _call_counter = [0]

    for cfg in configs:
        if not cfg.get("enabled", True):
            continue

        name = cfg["name"]
        config_json = cfg.get("config_json", {})

        # Skip non-stdio configs (HTTP/SSE not yet supported at runtime)
        if "url" in config_json and "command" not in config_json:
            logger.info("mcp_server_skipped_http", name=name)
            continue

        command = config_json.get("command", "")
        args = config_json.get("args", [])
        env = config_json.get("env", {})
        cwd = config_json.get("cwd")

        if not command:
            logger.warning("mcp_server_no_command", name=name)
            continue

        try:
            proc = _start_mcp_process(name, command, args, env, cwd=cwd)
            # Give server time to start
            time.sleep(0.5)
            _initialize_server(proc, name)

            # Discover tools
            tools_resp = _send_jsonrpc(proc, "tools/list", {}, req_id=2)
            if "error" in tools_resp:
                logger.error("mcp_tools_list_failed", name=name, error=tools_resp["error"])
                proc.terminate()
                continue

            mcp_tools = tools_resp.get("result", {}).get("tools", [])
            processes[name] = proc

            for t in mcp_tools:
                tool_name = t.get("name", "")
                tool_desc = t.get("description", "")

                # Prefix with server name to avoid collisions
                prefixed_name = f"mcp_{name}_{tool_name}"

                def make_tool_fn(_proc, _tool_name):
                    def tool_fn(**kwargs) -> str:
                        _call_counter[0] += 1
                        return call_tool(_proc, _tool_name, kwargs, req_id=100 + _call_counter[0])
                    return tool_fn

                tools.append(StructuredTool.from_function(
                    name=prefixed_name,
                    description=f"[MCP: {name}] {tool_desc}",
                    func=make_tool_fn(proc, tool_name),
                ))

            logger.info("mcp_tools_registered", name=name, tool_count=len(mcp_tools))

        except McpClientError as e:
            logger.error("mcp_server_connect_failed", name=name, error=str(e))
        except Exception as e:
            logger.error("mcp_server_unexpected_error", name=name, error=str(e))

    return tools, processes


def cleanup_mcp_processes(processes: dict[str, subprocess.Popen]):
    """Terminate all running MCP server processes."""
    for name, proc in processes.items():
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass
    processes.clear()
