"""Incident management tools for ServiceNow."""

from __future__ import annotations

import json
from typing import Any

from servicenow_mcp.client import client


async def create_incident(
    short_description: str,
    description: str = "",
    urgency: int = 3,
    impact: int = 3,
    assignment_group: str = "",
    category: str = "",
    caller_id: str = "",
) -> str:
    """Create a new ServiceNow incident.

    Args:
        short_description: Brief summary of the incident (required).
        description: Detailed description.
        urgency: 1 (High), 2 (Medium), 3 (Low). Default 3.
        impact: 1 (High), 2 (Medium), 3 (Low). Default 3.
        assignment_group: Name or sys_id of the assignment group.
        category: Incident category.
        caller_id: User who reported the incident (sys_id or user_name).

    Returns:
        JSON string with the created incident details.
    """
    fields: dict[str, Any] = {
        "short_description": short_description,
        "urgency": str(urgency),
        "impact": str(impact),
    }
    if description:
        fields["description"] = description
    if assignment_group:
        fields["assignment_group"] = assignment_group
    if category:
        fields["category"] = category
    if caller_id:
        fields["caller_id"] = caller_id

    result = await client.create_incident(fields)
    if "error" in result:
        return json.dumps(result)

    record = result.get("result", {})
    return json.dumps({
        "status": "created",
        "sys_id": record.get("sys_id", ""),
        "number": record.get("number", ""),
        "short_description": record.get("short_description", ""),
        "state": record.get("state", ""),
        "urgency": record.get("urgency", ""),
        "impact": record.get("impact", ""),
    })


async def get_incident(
    sys_id: str = "",
    number: str = "",
) -> str:
    """Get a ServiceNow incident by sys_id or INC number.

    Provide either sys_id or number (e.g. INC0010001). If number is provided,
    a query is performed to find the matching incident.

    Returns:
        JSON string with the incident details.
    """
    if not sys_id and not number:
        return json.dumps({"error": "Provide either sys_id or number."})

    if sys_id:
        result = await client.get_incident(sys_id)
        if "error" in result:
            return json.dumps(result)
        return json.dumps(result.get("result", {}))

    # Lookup by INC number
    result = await client.list_incidents(query=f"number={number}", limit=1)
    if "error" in result:
        return json.dumps(result)
    records = result.get("result", [])
    if not records:
        return json.dumps({"error": f"No incident found with number {number}"})
    return json.dumps(records[0])


async def update_incident(
    sys_id: str,
    short_description: str = "",
    description: str = "",
    urgency: int = 0,
    impact: int = 0,
    state: int = 0,
    assignment_group: str = "",
    assigned_to: str = "",
    work_notes: str = "",
    comments: str = "",
) -> str:
    """Update fields on an existing ServiceNow incident.

    Args:
        sys_id: The sys_id of the incident to update (required).
        short_description: New short description.
        description: New description.
        urgency: New urgency (1-3).
        impact: New impact (1-3).
        state: New state (1=New, 2=In Progress, 3=On Hold, 6=Resolved, 7=Closed).
        assignment_group: New assignment group.
        assigned_to: New assignee (sys_id or user_name).
        work_notes: Internal work notes to add.
        comments: Customer-visible comments to add.

    Returns:
        JSON string with updated incident details.
    """
    fields: dict[str, Any] = {}
    if short_description:
        fields["short_description"] = short_description
    if description:
        fields["description"] = description
    if urgency:
        fields["urgency"] = str(urgency)
    if impact:
        fields["impact"] = str(impact)
    if state:
        fields["state"] = str(state)
    if assignment_group:
        fields["assignment_group"] = assignment_group
    if assigned_to:
        fields["assigned_to"] = assigned_to
    if work_notes:
        fields["work_notes"] = work_notes
    if comments:
        fields["comments"] = comments

    if not fields:
        return json.dumps({"error": "No fields provided to update."})

    result = await client.update_incident(sys_id, fields)
    if "error" in result:
        return json.dumps(result)

    record = result.get("result", {})
    return json.dumps({
        "status": "updated",
        "sys_id": record.get("sys_id", ""),
        "number": record.get("number", ""),
        "state": record.get("state", ""),
        "short_description": record.get("short_description", ""),
    })


async def close_incident(
    sys_id: str,
    close_code: str = "Solved (Permanently)",
    close_notes: str = "",
) -> str:
    """Resolve and close a ServiceNow incident.

    Args:
        sys_id: The sys_id of the incident (required).
        close_code: Resolution code. Common values:
            - "Solved (Permanently)"
            - "Solved (Work Around)"
            - "Not Solved (Not Reproducible)"
            - "Not Solved (Too Costly)"
            - "Closed/Resolved by Caller"
        close_notes: Notes describing the resolution.

    Returns:
        JSON string confirming closure.
    """
    fields: dict[str, Any] = {
        "state": "6",  # Resolved
        "close_code": close_code,
    }
    if close_notes:
        fields["close_notes"] = close_notes

    result = await client.update_incident(sys_id, fields)
    if "error" in result:
        return json.dumps(result)

    record = result.get("result", {})
    return json.dumps({
        "status": "resolved",
        "sys_id": record.get("sys_id", ""),
        "number": record.get("number", ""),
        "state": record.get("state", ""),
        "close_code": record.get("close_code", ""),
    })


async def list_incidents(
    query: str = "",
    limit: int = 20,
    offset: int = 0,
    fields: str = "sys_id,number,short_description,state,urgency,impact,priority,assigned_to,assignment_group,opened_at,caller_id",
) -> str:
    """List or search ServiceNow incidents.

    Args:
        query: ServiceNow encoded query string. Examples:
            - "active=true" — only active incidents
            - "priority=1" — critical priority
            - "assigned_to=john.doe" — assigned to specific user
            - "short_descriptionLIKEnetwork" — description contains 'network'
            - "opened_at>2024-01-01" — opened after date
            Combine with ^: "active=true^priority<=2"
        limit: Max results (default 20, max 100).
        offset: Pagination offset.
        fields: Comma-separated fields to return.

    Returns:
        JSON string with list of incidents.
    """
    limit = min(limit, 100)
    result = await client.list_incidents(query=query, limit=limit, offset=offset, fields=fields)
    if "error" in result:
        return json.dumps(result)

    records = result.get("result", [])
    return json.dumps({
        "count": len(records),
        "incidents": records,
    })
