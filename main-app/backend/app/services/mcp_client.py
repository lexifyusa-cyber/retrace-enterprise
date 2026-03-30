"""
MCP Client — connects to MCP servers via langchain-mcp-adapters.

Supports both stdio and HTTP/SSE transport. Returns LangChain
StructuredTools that can be used directly by the agent.
"""

import asyncio
from typing import Any

import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger()


class McpClientError(Exception):
    pass


def _ensure_pydantic_schemas(tools: list) -> list:
    """
    Ensure all tools have proper Pydantic model args_schema.
    langchain-mcp-adapters may return tools with dict schemas
    which break model.bind_tools() (needs .model_fields).
    """
    from pydantic import create_model
    from typing import Any, Optional

    fixed = []
    for tool in tools:
        # Check if args_schema is a dict instead of a Pydantic model class
        schema = getattr(tool, 'args_schema', None)
        if schema is not None and not isinstance(schema, type):
            # It's an instance or dict — tool is fine if schema is a class
            fixed.append(tool)
            continue

        if schema is not None and isinstance(schema, type):
            # Already a proper Pydantic model class
            fixed.append(tool)
            continue

        # No args_schema — create one from the tool's args if possible
        try:
            if hasattr(tool, 'args') and isinstance(tool.args, dict):
                fields = {}
                for field_name, field_info in tool.args.items():
                    field_type = Any
                    if isinstance(field_info, dict):
                        ft = field_info.get('type', 'string')
                        if ft == 'string':
                            field_type = str
                        elif ft == 'integer':
                            field_type = int
                        elif ft == 'number':
                            field_type = float
                        elif ft == 'boolean':
                            field_type = bool
                        elif ft == 'array':
                            field_type = list
                        elif ft == 'object':
                            field_type = dict
                    default = field_info.get('default', ...) if isinstance(field_info, dict) else ...
                    if default is ...:
                        fields[field_name] = (Optional[field_type], None)
                    else:
                        fields[field_name] = (field_type, default)

                model_cls = create_model(f"{tool.name}_Schema", **fields)
                tool.args_schema = model_cls
        except Exception as e:
            logger.warning("mcp_tool_schema_fix_failed", tool=tool.name, error=str(e))

        fixed.append(tool)
    return fixed


class McpToolManager:
    """
    Manages MCP server connections and tools using langchain-mcp-adapters.
    Must be used as an async context manager.
    """

    def __init__(self, configs: list[dict]):
        self._configs = configs
        self._client = None
        self._tools = []

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

    async def connect(self):
        """Connect to all enabled MCP servers and discover tools."""
        from langchain_mcp_adapters.client import MultiServerMCPClient

        server_configs = {}
        for cfg in self._configs:
            if not cfg.get("enabled", True):
                continue

            name = cfg["name"]
            config_json = cfg.get("config_json", {})
            command = config_json.get("command", "")
            url = config_json.get("url", "")

            if command:
                # Stdio transport
                server_configs[name] = {
                    "command": command,
                    "args": config_json.get("args", []),
                    "transport": "stdio",
                    "env": config_json.get("env"),
                }
                # Add cwd if specified
                if config_json.get("cwd"):
                    server_configs[name]["cwd"] = config_json["cwd"]

                # If command is a venv python and uses -m, set PYTHONPATH to src/
                args = config_json.get("args", [])
                if len(args) >= 2 and args[0] == "-m" and "/mcp-servers/" in command:
                    import os
                    parts = command.split("/mcp-servers/")
                    if len(parts) == 2:
                        server_dir_name = parts[1].split("/")[0]
                        cwd = parts[0] + "/mcp-servers/" + server_dir_name
                        src_path = os.path.join(cwd, "src")
                        if os.path.isdir(src_path):
                            env = server_configs[name].get("env") or {}
                            existing = env.get("PYTHONPATH", "")
                            env["PYTHONPATH"] = f"{src_path}:{existing}" if existing else src_path
                            server_configs[name]["env"] = env

            elif url:
                # HTTP/SSE transport
                transport = config_json.get("transportType", "streamable-http")
                if transport == "sse":
                    transport = "sse"
                else:
                    transport = "streamable-http"

                server_configs[name] = {
                    "url": url,
                    "transport": transport,
                }
                if config_json.get("headers"):
                    server_configs[name]["headers"] = config_json["headers"]
            else:
                logger.warning("mcp_server_no_command_or_url", name=name)
                continue

        if not server_configs:
            return

        try:
            self._client = MultiServerMCPClient(server_configs)
            raw_tools = await self._client.get_tools()

            # Wrap MCP tools to ensure args_schema is a Pydantic model
            # (bind_tools requires .model_fields which dicts don't have)
            self._tools = _ensure_pydantic_schemas(raw_tools)
            logger.info("mcp_tools_loaded", count=len(self._tools),
                        servers=list(server_configs.keys()))
        except Exception as e:
            logger.error("mcp_client_connect_failed", error=str(e))
            self._client = None
            self._tools = []

    async def cleanup(self):
        """Disconnect from all MCP servers."""
        if self._client:
            try:
                # Try close() if available, otherwise just clear references
                if hasattr(self._client, 'close'):
                    await self._client.close()
                elif hasattr(self._client, '__aexit__'):
                    await self._client.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("mcp_client_cleanup_error", error=str(e))
            self._client = None
            self._tools = []

    @property
    def tools(self) -> list:
        """Return discovered MCP tools as LangChain tools."""
        return self._tools


def get_mcp_tools_sync(configs: list[dict]) -> tuple[list, "McpToolManager"]:
    """
    Synchronous wrapper to connect to MCP servers and get tools.
    Returns (tools, manager). Caller must eventually call manager.cleanup().
    """
    manager = McpToolManager(configs)

    # Run connect in event loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an async context — run in a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, manager.connect())
            future.result(timeout=30)
    else:
        asyncio.run(manager.connect())

    return manager.tools, manager


async def get_mcp_tools_async(configs: list[dict]) -> tuple[list, "McpToolManager"]:
    """
    Async version — connect to MCP servers and get tools.
    Returns (tools, manager). Caller must eventually await manager.cleanup().
    """
    manager = McpToolManager(configs)
    await manager.connect()
    return manager.tools, manager


# Keep backward compatibility for cleanup
def cleanup_mcp_processes(processes_or_manager):
    """Clean up MCP connections. Accepts either old dict or new McpToolManager."""
    if isinstance(processes_or_manager, McpToolManager):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(asyncio.run, processes_or_manager.cleanup()).result(timeout=10)
        else:
            asyncio.run(processes_or_manager.cleanup())
    elif isinstance(processes_or_manager, dict):
        # Old-style process dict — terminate all
        for name, proc in processes_or_manager.items():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        processes_or_manager.clear()
