from fastmcp import FastMCP
import httpx
import os
import json
from typing import Optional, Any
from enum import Enum

mcp = FastMCP("tenso_mcp")

API_BASE_URL = "https://dashboard.tensordock.com/api/v2"


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def _get_headers() -> dict:
    """Build auth headers using TENSO_API_TOKEN."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.environ.get("TENSO_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _handle_api_error(status_code: int, response_text: str) -> str:
    error_map = {
        400: "Bad request — check your parameters",
        401: "Authentication failed — verify your API token",
        403: "Forbidden — insufficient permissions for this resource",
        404: "Resource not found — check the ID or path",
        409: "Conflict — resource already exists or state conflict",
        422: "Validation error — check parameter types and values",
        429: "Rate limited — wait and retry in a few seconds",
        500: "Internal server error — the API is experiencing issues",
        502: "Bad gateway — the API is temporarily unavailable",
        503: "Service unavailable — the API is under maintenance",
    }
    msg = error_map.get(status_code, f"HTTP {status_code} error")
    detail = response_text[:500] if response_text else ""
    return f"Error: {msg}. Details: {detail}"


async def _request(method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(
            method,
            f"{API_BASE_URL}{path}",
            params=params,
            json=json_body,
            headers=_get_headers()
        )
        if resp.status_code >= 400:
            return {"error": _handle_api_error(resp.status_code, resp.text)}
        return resp.json() if resp.content else {"status": "ok"}


async def _paginated_request(method: str, path: str, params: Optional[dict] = None, limit: int = 50, offset: int = 0) -> dict:
    params = params or {}
    params["limit"] = limit
    params["offset"] = offset
    result = await _request(method, path, params=params)
    if "error" in result:
        return result
    if isinstance(result, list):
        return {"items": result, "has_more": len(result) >= limit, "next_offset": offset + len(result)}
    return result


def _format_response(data: dict, fmt: ResponseFormat = ResponseFormat.MARKDOWN) -> str:
    if fmt == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)
    if "error" in data:
        return f"**Error:** {data['error']}"
    lines = []
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"**{key}:** {len(value)} items")
            for item in value[:10]:
                if isinstance(item, dict):
                    summary = ", ".join(f"{k}={v}" for k, v in list(item.items())[:4])
                    lines.append(f"  - {summary}")
                else:
                    lines.append(f"  - {item}")
            if len(value) > 10:
                lines.append(f"  - ... and {len(value) - 10} more")
        else:
            lines.append(f"**{key}:** {value}")
    return "\n".join(lines)


@mcp.tool(annotations={"readOnlyHint": True})
async def tenso_list_instances() -> str:
    """Get a list of all virtual machine instances."""
    result = await _paginated_request("GET", "/instances")
    return _format_response(result)


@mcp.tool(annotations={"readOnlyHint": True})
async def tenso_get_instance(id: str) -> str:
    """Get details about a specific instance."""
    result = await _request("GET", f"/instances/{id}")
    return _format_response(result)


@mcp.tool()
async def tenso_create_instance(data: dict) -> str:
    """Create virtual machine instances with GPU acceleration."""
    result = await _request("POST", "/instances", json_body=data)
    return _format_response(result)


@mcp.tool()
async def tenso_start_instance(id: str) -> str:
    """Start a stopped instance."""
    result = await _request("POST", f"/instances/{id}/start")
    return _format_response(result)


@mcp.tool()
async def tenso_stop_instance(id: str) -> str:
    """Stop a running instance."""
    result = await _request("POST", f"/instances/{id}/stop")
    return _format_response(result)


@mcp.tool(annotations={"destructiveHint": True})
async def tenso_delete_instance(id: str) -> str:
    """Delete an instance and its associated resources."""
    result = await _request("DELETE", f"/instances/{id}")
    return _format_response(result)


@mcp.tool(annotations={"idempotentHint": True})
async def tenso_modify_instance(
    id: str,
    cpuCores: Optional[int] = None,
    ramGb: Optional[int] = None,
    diskGb: Optional[int] = None,
    gpus: Optional[dict] = None
) -> str:
    """Modify the resources of a stopped or stopped-disassociated instance."""
    body: dict[str, Any] = {}
    if cpuCores is not None:
        body["cpuCores"] = cpuCores
    if ramGb is not None:
        body["ramGb"] = ramGb
    if diskGb is not None:
        body["diskGb"] = diskGb
    if gpus is not None:
        body["gpus"] = gpus
    result = await _request("PUT", f"/instances/{id}/modify", json_body=body)
    return _format_response(result)


@mcp.tool(annotations={"readOnlyHint": True})
async def tenso_list_secrets() -> str:
    """Retrieve all secrets for your organization."""
    result = await _paginated_request("GET", "/secrets")
    return _format_response(result)


@mcp.tool()
async def tenso_create_secret(data: dict) -> str:
    """Create a new secret in your organization."""
    result = await _request("POST", "/secrets", json_body=data)
    return _format_response(result)


@mcp.tool(annotations={"readOnlyHint": True})
async def tenso_get_secret(id: str) -> str:
    """Retrieve a specific secret by ID."""
    result = await _request("GET", f"/secrets/{id}")
    return _format_response(result)


@mcp.tool(annotations={"destructiveHint": True})
async def tenso_delete_secret(id: str) -> str:
    """Delete a secret from your organization."""
    result = await _request("DELETE", f"/secrets/{id}")
    return _format_response(result)


@mcp.tool(annotations={"readOnlyHint": True})
async def tenso_list_locations() -> str:
    """Fetch all locations with their available GPU configurations and deployable resources."""
    result = await _paginated_request("GET", "/locations")
    return _format_response(result)


@mcp.tool(annotations={"readOnlyHint": True})
async def tenso_list_hostnodes(
    location: Optional[str] = None,
    minRamGb: Optional[int] = None,
    minVcpu: Optional[int] = None,
    gpu: Optional[str] = None
) -> str:
    """Fetch a list of all available hostnodes with their resources, pricing, and location information."""
    params: dict[str, Any] = {}
    if location is not None:
        params["location"] = location
    if minRamGb is not None:
        params["minRamGb"] = minRamGb
    if minVcpu is not None:
        params["minVcpu"] = minVcpu
    if gpu is not None:
        params["gpu"] = gpu
    result = await _paginated_request("GET", "/hostnodes", params=params)
    return _format_response(result)


@mcp.tool(annotations={"readOnlyHint": True})
async def tenso_get_hostnode(uuid: str) -> str:
    """Fetch detailed information about a specific hostnode including available ports and extended resource details."""
    result = await _request("GET", f"/hostnodes/{uuid}")
    return _format_response(result)


if __name__ == "__main__":
    mcp.run()
