"""ServiceNow REST API client — wraps the Table API with Basic and OAuth auth."""

from __future__ import annotations

import json
from typing import Any

import httpx

from servicenow_mcp.config import config


class ServiceNowClient:
    """Thin wrapper around the ServiceNow Table API."""

    def __init__(self) -> None:
        self._base = config.instance_url.rstrip("/")
        self._auth_type = config.auth_type

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._auth_type == "oauth" and config.oauth_token:
            headers["Authorization"] = f"Bearer {config.oauth_token}"
        return headers

    def _auth(self) -> httpx.BasicAuth | None:
        if self._auth_type == "basic" and config.username:
            return httpx.BasicAuth(config.username, config.password)
        return None

    def _url(self, table: str) -> str:
        return f"{self._base}/api/now/table/{table}"

    async def _request(
        self,
        method: str,
        table: str,
        *,
        sys_id: str = "",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(table)
        if sys_id:
            url = f"{url}/{sys_id}"

        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                auth=self._auth(),
            )
            if resp.status_code == 401:
                return {"error": "Authentication failed. Check credentials or token.", "status": 401}
            if resp.status_code == 403:
                return {"error": "Access denied. Your account does not have access to this ServiceNow resource.", "status": 403}
            if resp.status_code >= 400:
                detail = resp.text[:500]
                return {"error": f"ServiceNow API error {resp.status_code}: {detail}", "status": resp.status_code}
            return resp.json()

    # ------------------------------------------------------------------
    # Public API — Incidents
    # ------------------------------------------------------------------

    async def create_incident(self, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "incident", json_body=fields)

    async def get_incident(self, sys_id: str) -> dict[str, Any]:
        return await self._request("GET", "incident", sys_id=sys_id)

    async def update_incident(self, sys_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", "incident", sys_id=sys_id, json_body=fields)

    async def list_incidents(
        self,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
        fields: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sysparm_limit": limit, "sysparm_offset": offset}
        if query:
            params["sysparm_query"] = query
        if fields:
            params["sysparm_fields"] = fields
        return await self._request("GET", "incident", params=params)

    # ------------------------------------------------------------------
    # Public API — Generic Table
    # ------------------------------------------------------------------

    async def query_table(
        self,
        table: str,
        query: str = "",
        fields: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sysparm_limit": limit, "sysparm_offset": offset}
        if query:
            params["sysparm_query"] = query
        if fields:
            params["sysparm_fields"] = fields
        return await self._request("GET", table, params=params)


client = ServiceNowClient()
