"""ServiceNow MCP Server — stdio transport entry point.

Run standalone:
    SERVICENOW_INSTANCE_URL=https://dev12345.service-now.com \
    SERVICENOW_AUTH_TYPE=basic \
    SERVICENOW_USERNAME=admin \
    SERVICENOW_PASSWORD=secret \
    python -m servicenow_mcp
"""

from __future__ import annotations

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import json

from servicenow_mcp.tools.incidents import (
    create_incident,
    get_incident,
    update_incident,
    close_incident,
    list_incidents,
)
from servicenow_mcp.tools.table_api import search_table

server = Server("servicenow-mcp-server")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="create_incident",
        description="Create a new ServiceNow incident. Provide short_description (required), and optionally description, urgency (1-3), impact (1-3), assignment_group, category, caller_id.",
        inputSchema={
            "type": "object",
            "properties": {
                "short_description": {"type": "string", "description": "Brief summary of the incident"},
                "description": {"type": "string", "description": "Detailed description", "default": ""},
                "urgency": {"type": "integer", "description": "1=High, 2=Medium, 3=Low", "default": 3},
                "impact": {"type": "integer", "description": "1=High, 2=Medium, 3=Low", "default": 3},
                "assignment_group": {"type": "string", "description": "Assignment group name or sys_id", "default": ""},
                "category": {"type": "string", "description": "Incident category", "default": ""},
                "caller_id": {"type": "string", "description": "Caller user_name or sys_id", "default": ""},
            },
            "required": ["short_description"],
        },
    ),
    Tool(
        name="get_incident",
        description="Get a ServiceNow incident by sys_id or INC number.",
        inputSchema={
            "type": "object",
            "properties": {
                "sys_id": {"type": "string", "description": "sys_id of the incident", "default": ""},
                "number": {"type": "string", "description": "INC number (e.g. INC0010001)", "default": ""},
            },
        },
    ),
    Tool(
        name="update_incident",
        description="Update fields on an existing ServiceNow incident. Provide sys_id and any fields to change.",
        inputSchema={
            "type": "object",
            "properties": {
                "sys_id": {"type": "string", "description": "sys_id of the incident to update"},
                "short_description": {"type": "string", "default": ""},
                "description": {"type": "string", "default": ""},
                "urgency": {"type": "integer", "description": "1-3", "default": 0},
                "impact": {"type": "integer", "description": "1-3", "default": 0},
                "state": {"type": "integer", "description": "1=New, 2=InProgress, 3=OnHold, 6=Resolved, 7=Closed", "default": 0},
                "assignment_group": {"type": "string", "default": ""},
                "assigned_to": {"type": "string", "default": ""},
                "work_notes": {"type": "string", "description": "Internal work notes", "default": ""},
                "comments": {"type": "string", "description": "Customer-visible comments", "default": ""},
            },
            "required": ["sys_id"],
        },
    ),
    Tool(
        name="close_incident",
        description="Resolve and close a ServiceNow incident.",
        inputSchema={
            "type": "object",
            "properties": {
                "sys_id": {"type": "string", "description": "sys_id of the incident"},
                "close_code": {"type": "string", "description": "Resolution code", "default": "Solved (Permanently)"},
                "close_notes": {"type": "string", "description": "Resolution notes", "default": ""},
            },
            "required": ["sys_id"],
        },
    ),
    Tool(
        name="list_incidents",
        description="List or search ServiceNow incidents. Use query parameter with ServiceNow encoded query syntax (e.g. 'active=true^priority<=2').",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "ServiceNow encoded query", "default": ""},
                "limit": {"type": "integer", "description": "Max results (max 100)", "default": 20},
                "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                "fields": {"type": "string", "description": "Comma-separated fields to return", "default": "sys_id,number,short_description,state,urgency,impact,priority,assigned_to,assignment_group,opened_at,caller_id"},
            },
        },
    ),
    Tool(
        name="search_table",
        description="Query any ServiceNow table (change_request, sc_request, sys_user, cmdb_ci, kb_knowledge, problem, etc.) using the Table API.",
        inputSchema={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "ServiceNow table name"},
                "query": {"type": "string", "description": "Encoded query string", "default": ""},
                "fields": {"type": "string", "description": "Comma-separated fields", "default": ""},
                "limit": {"type": "integer", "description": "Max results", "default": 20},
                "offset": {"type": "integer", "description": "Offset", "default": 0},
            },
            "required": ["table"],
        },
    ),
]

# Tool handler dispatch
TOOL_HANDLERS = {
    "create_incident": create_incident,
    "get_incident": get_incident,
    "update_incident": update_incident,
    "close_incident": close_incident,
    "list_incidents": list_incidents,
    "search_table": search_table,
}


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    try:
        result = await handler(**arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def run_server():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    import asyncio
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
