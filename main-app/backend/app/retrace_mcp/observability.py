"""
ReTrace Observability MCP Server — focused Grafana/Azure Monitor queries.

This wraps Grafana's Azure Monitor datasource for the common enterprise
question: which services have the most errors in Log Analytics-backed logs.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("retrace-observability")

_USER_ID = os.environ.get("RETRACE_USER_ID", "")
_DEFAULT_TABLE = "RetraceDemoAppLog_CL"
_DEFAULT_HOURS = 24 * 30


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            return executor.submit(asyncio.run, coro).result(timeout=90)
    return asyncio.run(coro)


async def _grafana_env() -> dict[str, Any]:
    from sqlalchemy import select
    from app.db.database import async_session_maker
    from app.models.mcp_extension import McpExtension
    from app.models.user_extension import UserExtension

    async with async_session_maker() as session:
        row = (
            await session.execute(
                select(McpExtension.env_vars_json, UserExtension.env_vars_json)
                .select_from(UserExtension)
                .join(McpExtension, UserExtension.extension_id == McpExtension.extension_id)
                .where(UserExtension.user_id == _USER_ID)
                .where(McpExtension.name == "Grafana")
                .where(McpExtension.is_enabled == True)
                .where(UserExtension.is_enabled == True)
                .limit(1)
            )
        ).first()
    if not row:
        raise RuntimeError("Grafana MCP extension is not enabled for this user")
    return {**(row[0] or {}), **(row[1] or {})}


def _rows_from_frame(frame: dict[str, Any]) -> list[dict[str, Any]]:
    fields = frame.get("schema", {}).get("fields", []) or []
    values = frame.get("data", {}).get("values", []) or []
    names = [field.get("name") for field in fields]
    if not names or not values:
        return []
    return [dict(zip(names, row_values)) for row_values in zip(*values)]


async def _query_grafana_azure_logs(kql: str, hours: int = _DEFAULT_HOURS) -> list[dict[str, Any]]:
    env = await _grafana_env()
    grafana_url = str(env.get("GRAFANA_URL") or "").rstrip("/")
    token = str(env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN") or env.get("GRAFANA_API_KEY") or "")
    workspace = str(
        env.get("AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID")
        or env.get("GRAFANA_AZURE_LOG_WORKSPACE_RESOURCE_ID")
        or ""
    )
    subscription = str(env.get("AZURE_SUBSCRIPTION_ID") or "")
    datasource_uid = str(env.get("GRAFANA_AZURE_MONITOR_DATASOURCE_UID") or "azure-monitor-oob")

    missing = [
        name
        for name, value in {
            "GRAFANA_URL": grafana_url,
            "GRAFANA_SERVICE_ACCOUNT_TOKEN/GRAFANA_API_KEY": token,
            "AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": workspace,
            "AZURE_SUBSCRIPTION_ID": subscription,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Missing Grafana/Azure log configuration: " + ", ".join(missing))

    now_ms = int(__import__("time").time() * 1000)
    from_ms = now_ms - max(1, int(hours)) * 60 * 60 * 1000
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": "grafana-azure-monitor-datasource", "uid": datasource_uid},
                "queryType": "Azure Log Analytics",
                "azureLogAnalytics": {
                    "query": kql,
                    "resources": [workspace],
                    "resultFormat": "table",
                    "dashboardTime": False,
                },
                "subscription": subscription,
                "subscriptions": [subscription],
            }
        ],
        "from": str(from_ms),
        "to": str(now_ms),
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{grafana_url}/api/ds/query",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Grafana query failed HTTP {resp.status_code}: {resp.text[:500]}")
    result = (resp.json().get("results") or {}).get("A") or {}
    if result.get("error"):
        raise RuntimeError(str(result.get("error")))
    frames = result.get("frames") or []
    rows: list[dict[str, Any]] = []
    for frame in frames:
        rows.extend(_rows_from_frame(frame))
    return rows


@mcp.tool()
def grafana_datasources() -> str:
    """List configured Grafana datasources, including Azure Monitor and Prometheus.

    Use this before saying whether a Prometheus datasource exists.
    """
    async def _do():
        env = await _grafana_env()
        grafana_url = str(env.get("GRAFANA_URL") or "").rstrip("/")
        token = str(env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN") or env.get("GRAFANA_API_KEY") or "")
        if not grafana_url or not token:
            raise RuntimeError("Missing GRAFANA_URL or Grafana token")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{grafana_url}/api/datasources",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Grafana datasource query failed HTTP {resp.status_code}: {resp.text[:500]}")
        datasources = [
            {
                "uid": ds.get("uid"),
                "name": ds.get("name"),
                "type": ds.get("type"),
                "url": ds.get("url"),
                "isDefault": ds.get("isDefault"),
            }
            for ds in resp.json()
        ]
        return datasources

    try:
        return json.dumps({"datasources": _run_async(_do())}, indent=2)
    except Exception as exc:
        return f"Error querying Grafana datasources: {exc}"


@mcp.tool()
def grafana_azure_log_tables(hours: int = _DEFAULT_HOURS) -> str:
    """List Azure Log Analytics tables visible through Grafana Azure Monitor.

    This is NOT the Grafana datasource list. Use grafana_datasources when the
    user asks which Grafana datasources exist, including Prometheus.

    Args:
        hours: Lookback window in hours. Defaults to 720 hours (30 days).
    """
    kql = "search * | summarize Count=count() by Type | order by Count desc"
    try:
        rows = _run_async(_query_grafana_azure_logs(kql, hours=hours))
        return json.dumps({"hours": hours, "tables": rows}, indent=2)
    except Exception as exc:
        return f"Error querying Grafana Azure logs: {exc}"


@mcp.tool()
def grafana_top_azure_log_errors(
    table: str = _DEFAULT_TABLE,
    hours: int = _DEFAULT_HOURS,
    top: int = 10,
) -> str:
    """Return services with the most errors from Azure logs via Grafana.

    Use this for questions like "which Azure services have most errors" or
    "check Grafana logs for errors". It treats ERROR/CRITICAL severities,
    HTTP 5xx statuses, and messages containing error/exception/failed as errors.

    Args:
        table: Log Analytics table name. Defaults to RetraceDemoAppLog_CL.
        hours: Lookback window in hours. Defaults to 720 hours (30 days).
        top: Number of services to return, 1-25.
    """
    safe_top = min(max(int(top), 1), 25)
    safe_hours = max(int(hours), 1)
    # Table names cannot be parameterized in KQL; keep this conservative.
    if not table.replace("_", "").isalnum():
        return "Invalid table name. Use only letters, numbers, and underscores."
    kql = f"""{table}
| where TimeGenerated >= ago({safe_hours}h)
| extend IsError = toupper(Severity_s) in (\"ERROR\", \"CRITICAL\") or StatusCode_d >= 500 or Message has_any (\"error\", \"exception\", \"failed\")
| where IsError
| summarize ErrorCount=count(), SevErrors=countif(toupper(Severity_s) == \"ERROR\"), FiveXx=countif(StatusCode_d >= 500), SampleMessage=any(Message), Statuses=make_set(tostring(StatusCode_d), 8), ErrorCodes=make_set(ErrorCode_s, 8), Paths=make_set(RequestPath_s, 8) by Service_s
| order by ErrorCount desc
| take {safe_top}"""
    try:
        rows = _run_async(_query_grafana_azure_logs(kql, hours=safe_hours))
        return json.dumps({"table": table, "hours": safe_hours, "top_errors": rows}, indent=2)
    except Exception as exc:
        return f"Error querying Grafana Azure log errors: {exc}"


@mcp.tool()
def grafana_azure_log_query(kql: str, hours: int = _DEFAULT_HOURS) -> str:
    """Run a read-only KQL query through Grafana's Azure Monitor datasource.

    Args:
        kql: Read-only KQL query for the configured Log Analytics workspace.
        hours: Grafana query lookback window in hours. Defaults to 720 hours.
    """
    lowered = kql.strip().lower()
    if any(word in lowered for word in [".set", ".delete", "drop table", "purge"]):
        return "Refusing potentially destructive KQL. Use read-only queries only."
    try:
        rows = _run_async(_query_grafana_azure_logs(kql, hours=max(int(hours), 1)))
        return json.dumps({"hours": hours, "rows": rows[:200]}, indent=2)
    except Exception as exc:
        return f"Error querying Grafana Azure logs: {exc}"


if __name__ == "__main__":
    mcp.run()
