"""
MCP-to-LangChain Bridge — converts MCP server tools into LangChain StructuredTools.

This allows the LangGraph CodeAct agent to use tools from external MCP servers
exactly like native tools (terminal, knowledge_base, etc.).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from langchain_core.tools import StructuredTool

from app.services.mcp_client_service import McpClientManager, McpToolInfo


def build_mcp_tools(
    mcp_manager: McpClientManager,
    extension_id: str,
    extension_name: str,
    tool_infos: list[McpToolInfo],
    user_id: str = "__shared__",
) -> list[StructuredTool]:
    """Convert MCP tool definitions into LangChain StructuredTools.

    Tool names are prefixed with the extension name to avoid collisions,
    e.g. servicenow__create_incident.
    """
    # Capture the event loop NOW — we're called from async context during
    # tool loading. This loop reference is passed into each tool's closure
    # so the sync wrapper can schedule coroutines on it later.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    tools: list[StructuredTool] = []
    # Sanitize extension name for use as prefix
    prefix = extension_name.lower().replace(" ", "_").replace("-", "_")

    for info in tool_infos:
        prefixed_name = f"{prefix}__{info.name}"

        # Build a closure to capture the current tool info
        tool = _make_structured_tool(
            mcp_manager=mcp_manager,
            extension_id=extension_id,
            tool_name=info.name,
            prefixed_name=prefixed_name,
            description=f"[{extension_name}] {info.description}",
            input_schema=info.input_schema,
            user_id=user_id,
            event_loop=loop,
        )
        tools.append(tool)

    return tools


def _make_structured_tool(
    mcp_manager: McpClientManager,
    extension_id: str,
    tool_name: str,
    prefixed_name: str,
    description: str,
    input_schema: dict,
    user_id: str,
    event_loop: asyncio.AbstractEventLoop = None,
) -> StructuredTool:
    """Create a StructuredTool that proxies calls to an MCP server.

    Uses **kwargs to accept any arguments — the MCP server validates them.
    """

    async def _call_mcp_tool(**kwargs: Any) -> str:
        return await mcp_manager.call_tool(
            extension_id=extension_id,
            tool_name=tool_name,
            arguments=kwargs,
            user_id=user_id,
        )

    # Synchronous wrapper — called from the sandbox's exec() which runs in a
    # worker thread. We schedule the async MCP call on the captured event loop
    # using run_coroutine_threadsafe() and block until it completes.
    # On failure, automatically reconnects the MCP subprocess and retries once.
    def _call_mcp_tool_sync(**kwargs: Any) -> str:
        import structlog
        _logger = structlog.get_logger()
        loop = event_loop
        if loop is None or loop.is_closed():
            err = f"No event loop available for MCP tool call: {prefixed_name}"
            _logger.error("mcp_tool_sync_no_loop", tool=prefixed_name)
            return f"ERROR: {err}"

        for attempt in range(2):
            try:
                _logger.info("mcp_tool_sync_call", tool=prefixed_name, kwargs=kwargs, attempt=attempt)
                future = asyncio.run_coroutine_threadsafe(_call_mcp_tool(**kwargs), loop)
                result = future.result(timeout=45)
                # Check if result is a JSON error from call_tool()
                if isinstance(result, str) and '"error"' in result and attempt == 0:
                    try:
                        parsed = json.loads(result)
                        if "error" in parsed and "dropped" in parsed["error"].lower():
                            _logger.warning("mcp_tool_sync_dropped", tool=prefixed_name, error=parsed["error"])
                            # Connection dropped — reconnect and retry
                            reconnect_future = asyncio.run_coroutine_threadsafe(
                                mcp_manager.reconnect_extension(extension_id, user_id), loop
                            )
                            reconnect_future.result(timeout=30)
                            continue  # Retry the tool call
                    except (json.JSONDecodeError, Exception):
                        pass
                _logger.info("mcp_tool_sync_result", tool=prefixed_name, result_len=len(str(result)))
                return result
            except Exception as e:
                _logger.error("mcp_tool_sync_error", tool=prefixed_name, error=str(e), attempt=attempt)
                if attempt == 0:
                    # First failure — try reconnecting MCP subprocess
                    try:
                        _logger.info("mcp_tool_sync_reconnecting", tool=prefixed_name)
                        reconnect_future = asyncio.run_coroutine_threadsafe(
                            mcp_manager.reconnect_extension(extension_id, user_id), loop
                        )
                        reconnect_future.result(timeout=30)
                        continue  # Retry the tool call
                    except Exception as reconn_err:
                        _logger.error("mcp_tool_sync_reconnect_failed", tool=prefixed_name, error=str(reconn_err))
                return f"ERROR calling {prefixed_name}: {e}"

        return f"ERROR calling {prefixed_name}: all retry attempts failed"

    return StructuredTool(
        name=prefixed_name,
        description=description,
        func=_call_mcp_tool_sync,
        coroutine=_call_mcp_tool,
        args_schema=_build_pydantic_model(prefixed_name, input_schema),
    )


def _build_pydantic_model(tool_name: str, input_schema: dict):
    """Dynamically build a Pydantic model from the MCP tool's input schema.

    This allows LangChain to validate arguments before calling the tool.
    Returns None if schema is empty (tool takes no args).
    """
    if not input_schema or not input_schema.get("properties"):
        return None

    from pydantic import create_model, Field
    from typing import Optional

    fields = {}
    properties = input_schema.get("properties", {})
    required_fields = set(input_schema.get("required", []))

    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }

    for prop_name, prop_def in properties.items():
        prop_type = type_map.get(prop_def.get("type", "string"), str)
        prop_desc = prop_def.get("description", "")
        default = prop_def.get("default")

        if prop_name in required_fields:
            fields[prop_name] = (prop_type, Field(description=prop_desc))
        else:
            fields[prop_name] = (
                Optional[prop_type],
                Field(default=default, description=prop_desc),
            )

    model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Args"
    return create_model(model_name, **fields)
