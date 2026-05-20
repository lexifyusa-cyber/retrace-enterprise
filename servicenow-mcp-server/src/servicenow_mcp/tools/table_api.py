"""Generic ServiceNow Table API tool."""

from __future__ import annotations

import json
from typing import Any

from servicenow_mcp.client import client


async def search_table(
    table: str,
    query: str = "",
    fields: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Query any ServiceNow table using the Table API.

    This is a generic tool for accessing tables beyond incidents, such as:
    - change_request — Change requests
    - sc_request — Service catalog requests
    - sc_req_item — Requested items
    - sys_user — Users
    - sys_user_group — Groups
    - cmdb_ci — Configuration items
    - kb_knowledge — Knowledge articles
    - problem — Problems

    Args:
        table: ServiceNow table name (required).
        query: Encoded query string. Examples:
            - "active=true"
            - "nameLIKEnetwork"
            - "sys_created_on>2024-01-01"
        fields: Comma-separated fields to return. Leave empty for all fields.
        limit: Max results (default 20, max 100).
        offset: Pagination offset.

    Returns:
        JSON string with query results.
    """
    limit = min(limit, 100)
    result = await client.query_table(
        table=table,
        query=query,
        fields=fields,
        limit=limit,
        offset=offset,
    )
    if "error" in result:
        return json.dumps(result)

    records = result.get("result", [])
    return json.dumps({
        "table": table,
        "count": len(records),
        "records": records,
    })
