"""
MCP Client Manager — spawns and manages external MCP server connections.

Uses the official MCP Python SDK client to:
  1. Spawn MCP servers as subprocesses via stdio transport
  2. Connect to remote MCP servers via HTTP/SSE transport (Streamable HTTP or SSE)
  3. Discover available tools via tools/list
  4. Route tool calls via tools/call
  5. Manage lifecycle (start, health check, shutdown)
  6. Inject user auth tokens for OAuth pass-through
  7. Idle timeout — auto-cleanup after inactivity
  8. Connection limits — per-tenant and global caps with LRU eviction
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
from mcp.types import Tool as McpTool

logger = structlog.get_logger()

# ── Connection lifecycle constants ────────────────────────────────
IDLE_TIMEOUT_SECONDS = 900          # 15 minutes — kill idle MCP subprocesses
IDLE_CHECK_INTERVAL_SECONDS = 60    # sweep every 60 seconds
MAX_CONNECTIONS_PER_TENANT = 50     # max concurrent MCP subprocesses per tenant
MAX_CONNECTIONS_GLOBAL = 200        # max concurrent MCP subprocesses total


@dataclass
class McpToolInfo:
    """Cached info about a tool discovered from an MCP server."""
    name: str
    description: str
    input_schema: dict


@dataclass
class McpConnection:
    """A live connection to an MCP server (stdio subprocess or HTTP endpoint)."""
    extension_id: str
    extension_name: str
    session: ClientSession
    tools: list[McpToolInfo] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    tenant_id: str = ""
    _context_manager: Any = None  # holds the transport context manager
    _session_context: Any = None  # holds the session context
    # Reconnection config — stored so we can restart without caller re-supplying
    transport: str = "stdio"       # "stdio" or "http"
    url: str = ""                  # for HTTP transport
    command: str = ""              # for stdio transport
    args: list[str] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    auth_type: str = "none"
    auth_config: dict = field(default_factory=dict)
    user_auth_token: str = ""
    user_auth_credentials: dict = field(default_factory=dict)  # {username, password} for LDAP


class McpClientManager:
    """Manages connections to external MCP servers.

    Singleton-ish — one instance shared across the application.
    Connections are keyed by (extension_id, user_id) for per-user auth isolation.
    """

    def __init__(self) -> None:
        self._connections: dict[str, McpConnection] = {}  # key: "{ext_id}:{user_id}"
        self._lock = asyncio.Lock()
        self._idle_task: Optional[asyncio.Task] = None
        self._idle_running = False

    def _key(self, extension_id: str, user_id: str = "__shared__") -> str:
        return f"{extension_id}:{user_id}"

    # ── Connection lifecycle ──────────────────────────────────────

    async def start_extension(
        self,
        extension_id: str,
        extension_name: str,
        command: str | None = None,
        args: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
        auth_type: str = "none",
        auth_config: dict | None = None,
        user_auth_token: str | None = None,
        user_auth_credentials: dict | None = None,
        user_id: str = "__shared__",
        tenant_id: str = "",
        transport: str = "stdio",
        url: str | None = None,
    ) -> list[McpToolInfo]:
        """Start an MCP server connection and discover its tools.

        Supports stdio (subprocess) and http (Streamable HTTP / SSE) transports.
        If a connection already exists for this extension+user, return cached tools.
        Enforces global and per-tenant connection limits with LRU eviction.
        """
        key = self._key(extension_id, user_id)

        async with self._lock:
            if key in self._connections:
                conn = self._connections[key]
                # Verify connection is still alive by checking if process is running
                try:
                    if hasattr(conn, 'session') and conn.session:
                        # Quick ping — list tools to verify connection
                        await conn.session.list_tools()
                        conn.last_activity = time.time()
                        return conn.tools
                except Exception:
                    logger.warning("mcp_stale_connection_detected", extension=extension_id, user_id=user_id)
                    # Connection is dead — remove and fall through to restart
                    await self._stop_connection_unlocked(key)

            # ── Enforce connection limits before spawning ──
            if len(self._connections) >= MAX_CONNECTIONS_GLOBAL:
                logger.warning("mcp_global_limit_reached", current=len(self._connections), max=MAX_CONNECTIONS_GLOBAL)
                evicted = await self._evict_lru_unlocked()
                if not evicted:
                    raise RuntimeError("MCP connection limit reached and no evictable connections")

            if tenant_id:
                tenant_count = self._count_tenant_connections(tenant_id)
                if tenant_count >= MAX_CONNECTIONS_PER_TENANT:
                    logger.warning("mcp_tenant_limit_reached", tenant_id=tenant_id, current=tenant_count, max=MAX_CONNECTIONS_PER_TENANT)
                    evicted = await self._evict_lru_unlocked(tenant_id)
                    if not evicted:
                        raise RuntimeError(f"MCP connection limit reached for tenant {tenant_id}")

        try:
            if transport == "http":
                # ── HTTP / SSE transport ──────────────────────────────
                if not url:
                    raise ValueError("url is required for HTTP transport")

                # For HTTP MCPs, env_vars_json entries are passed as HTTP headers
                # (e.g. { "Authorization": "Bearer token" })
                headers: dict[str, str] = {}
                if env_vars:
                    headers.update(env_vars)

                # Inject stored OAuth token (from user_extension.auth_config_json)
                if auth_config:
                    stored_token = auth_config.get("oauth_access_token")
                    if stored_token:
                        token_type = auth_config.get("oauth_token_type", "Bearer")
                        headers["Authorization"] = f"{token_type} {stored_token}"

                # Auto-inject OAuth token if passthrough auth configured
                if auth_type in ("oauth_passthrough", "auto_passthrough") and user_auth_token and auth_config:
                    header_name = auth_config.get("header", "Authorization")
                    prefix = auth_config.get("prefix", "Bearer")
                    headers[header_name] = f"{prefix} {user_auth_token}".strip()

                # Use stored OAuth token if available (from user_ext.auth_config_json)
                stored_oauth_token = (auth_config or {}).get("oauth_access_token")
                if stored_oauth_token and "Authorization" not in headers:
                    token_type = (auth_config or {}).get("oauth_token_type", "Bearer")
                    headers["Authorization"] = f"{token_type} {stored_oauth_token}"

                # Choose transport: SSE for legacy /sse endpoints, Streamable HTTP for everything else
                if url.rstrip("/").endswith("/sse"):
                    ctx = sse_client(url, headers=headers or None)
                else:
                    ctx = streamablehttp_client(url, headers=headers or None)

                raw = await ctx.__aenter__()
                # streamablehttp_client returns (read, write, get_session_id) — 3-tuple
                # sse_client returns (read, write) — 2-tuple
                read_stream, write_stream = raw[0], raw[1]

                logger.info("mcp_http_transport_connecting", extension=extension_name, url=url)

            else:
                # ── stdio transport (subprocess) ──────────────────────
                if not command:
                    raise ValueError("command is required for stdio transport")

                # Build subprocess environment
                env = dict(os.environ)
                if env_vars:
                    env.update(env_vars)

                # Inject OAuth token if pass-through
                if auth_type == "oauth_passthrough" and auth_config:
                    if not user_auth_token:
                        raise RuntimeError(
                            f"Your Azure AD session has expired or is missing. "
                            f"Please sign out and sign in again with Azure AD to use {extension_name}."
                        )
                    token_env_var = auth_config.get("token_env_var", "OAUTH_TOKEN")
                    env[token_env_var] = user_auth_token

                # Inject LDAP credentials if ldap_passthrough
                if auth_type == "ldap_passthrough" and auth_config:
                    if not user_auth_credentials:
                        raise RuntimeError(
                            f"Your LDAP session has expired or is missing. "
                            f"Please sign out and sign in again to use {extension_name}."
                        )
                    username_env_var = auth_config.get("username_env_var", "AUTH_USERNAME")
                    password_env_var = auth_config.get("password_env_var", "AUTH_PASSWORD")
                    env[username_env_var] = user_auth_credentials["username"]
                    env[password_env_var] = user_auth_credentials["password"]

                # Auto pass-through: detect user's auth method
                if auth_type == "auto_passthrough" and auth_config:
                    auth_type_env_var = auth_config.get("auth_type_env_var", "")
                    if user_auth_token:
                        token_env_var = auth_config.get("token_env_var", "OAUTH_TOKEN")
                        env[token_env_var] = user_auth_token
                        if auth_type_env_var:
                            env[auth_type_env_var] = auth_config.get("oauth_auth_type_value", "bearer")
                        logger.info("auto_passthrough_oauth", extension=extension_name)
                    elif user_auth_credentials:
                        username_env_var = auth_config.get("username_env_var", "AUTH_USERNAME")
                        password_env_var = auth_config.get("password_env_var", "AUTH_PASSWORD")
                        env[username_env_var] = user_auth_credentials["username"]
                        env[password_env_var] = user_auth_credentials["password"]
                        if auth_type_env_var:
                            env[auth_type_env_var] = auth_config.get("ldap_auth_type_value", "basic")
                        logger.info("auto_passthrough_ldap", extension=extension_name)
                    else:
                        raise RuntimeError(
                            f"No authentication credentials found. "
                            f"Please sign out and sign in again (via Azure AD or LDAP) to use {extension_name}."
                        )

                server_params = StdioServerParameters(command=command, args=args or [], env=env)
                ctx = stdio_client(server_params)
                read_stream, write_stream = await ctx.__aenter__()

            session = ClientSession(read_stream, write_stream)
            session_ctx = session
            await session.__aenter__()
            await session.initialize()

            # Discover tools
            tools_result = await session.list_tools()
            tool_infos = [
                McpToolInfo(
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
                )
                for t in tools_result.tools
            ]

            conn = McpConnection(
                extension_id=extension_id,
                extension_name=extension_name,
                session=session,
                tools=tool_infos,
                tenant_id=tenant_id,
                _context_manager=ctx,
                _session_context=session_ctx,
                transport=transport,
                url=url or "",
                command=command or "",
                args=args or [],
                env_vars=env_vars or {},
                auth_type=auth_type,
                auth_config=auth_config or {},
                user_auth_token=user_auth_token or "",
                user_auth_credentials=user_auth_credentials or {},
            )

            async with self._lock:
                self._connections[key] = conn

            logger.info(
                "mcp_extension_started",
                extension=extension_name,
                transport=transport,
                tools=[t.name for t in tool_infos],
            )
            return tool_infos

        except Exception as e:
            logger.error("mcp_extension_start_failed", extension=extension_name, transport=transport, error=str(e))
            raise

    async def reconnect_extension(
        self,
        extension_id: str,
        user_id: str = "__shared__",
    ) -> list[McpToolInfo]:
        """Reconnect a dead MCP server using its saved startup config."""
        key = self._key(extension_id, user_id)
        conn = self._connections.get(key)
        if not conn:
            raise RuntimeError(f"No connection to reconnect for {extension_id}:{user_id}")

        # Save config before stopping
        saved = {
            "extension_name": conn.extension_name,
            "transport": conn.transport,
            "url": conn.url,
            "command": conn.command,
            "args": conn.args,
            "env_vars": conn.env_vars,
            "auth_type": conn.auth_type,
            "auth_config": conn.auth_config,
            "user_auth_token": conn.user_auth_token,
            "user_auth_credentials": conn.user_auth_credentials,
            "tenant_id": conn.tenant_id,
        }
        logger.info("mcp_reconnecting", extension=conn.extension_name, user_id=user_id)

        await self.stop_extension(extension_id, user_id)
        return await self.start_extension(
            extension_id=extension_id,
            extension_name=saved["extension_name"],
            transport=saved["transport"],
            url=saved["url"],
            command=saved["command"],
            args=saved["args"],
            env_vars=saved["env_vars"],
            auth_type=saved["auth_type"],
            auth_config=saved["auth_config"],
            user_auth_token=saved["user_auth_token"],
            user_auth_credentials=saved["user_auth_credentials"],
            user_id=user_id,
            tenant_id=saved["tenant_id"],
        )

    async def call_tool(
        self,
        extension_id: str,
        tool_name: str,
        arguments: dict,
        user_id: str = "__shared__",
    ) -> str:
        """Call a tool on a running MCP server. Returns the text result."""
        key = self._key(extension_id, user_id)
        conn = self._connections.get(key)
        if not conn:
            return json.dumps({"error": f"MCP extension {extension_id} is not running."})

        # Track activity for idle timeout
        conn.last_activity = time.time()

        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(tool_name, arguments),
                timeout=30,
            )
            # Extract text from result content
            texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    texts.append(content.text)
            return "\n".join(texts) if texts else json.dumps({"result": "ok"})
        except asyncio.TimeoutError:
            error_msg = f"Tool call timed out after 30s: {tool_name}"
            logger.error("mcp_tool_call_timeout", tool=tool_name, extension=extension_id)
            # Connection is likely stale — remove it
            if key in self._connections:
                try:
                    await self.stop_extension(extension_id, user_id)
                except Exception:
                    self._connections.pop(key, None)
            return json.dumps({"error": f"{error_msg}. The MCP connection may have dropped — please try again."})
        except Exception as e:
            import traceback
            error_msg = str(e) or repr(e) or type(e).__name__
            tb = traceback.format_exc()
            logger.error("mcp_tool_call_failed", tool=tool_name, error=error_msg, traceback=tb)
            # Connection may be dead — remove it so it gets restarted next time
            if key in self._connections:
                try:
                    await self.stop_extension(extension_id, user_id)
                except Exception:
                    self._connections.pop(key, None)
            return json.dumps({"error": f"Tool call failed: {error_msg}. The MCP connection may have dropped — please try again."})

    # ── Stop / cleanup ────────────────────────────────────────────

    async def _stop_connection_unlocked(self, key: str) -> None:
        """Stop a connection WITHOUT acquiring the lock. Caller must hold _lock."""
        conn = self._connections.pop(key, None)
        if conn:
            try:
                if conn._session_context:
                    await conn._session_context.__aexit__(None, None, None)
                if conn._context_manager:
                    await conn._context_manager.__aexit__(None, None, None)
                logger.info("mcp_extension_stopped", extension=conn.extension_name)
            except Exception as e:
                logger.warning("mcp_extension_stop_error", extension=conn.extension_name, error=str(e))

    async def stop_extension(
        self,
        extension_id: str,
        user_id: str = "__shared__",
    ) -> None:
        """Stop an MCP server subprocess."""
        key = self._key(extension_id, user_id)
        async with self._lock:
            await self._stop_connection_unlocked(key)

    async def stop_all(self) -> None:
        """Stop all running MCP server subprocesses."""
        async with self._lock:
            keys = list(self._connections.keys())
            for key in keys:
                await self._stop_connection_unlocked(key)

    async def stop_user_connections(self, user_id: str) -> int:
        """Stop all MCP connections for a specific user (e.g., on logout).

        Returns the number of connections stopped.
        """
        async with self._lock:
            matching_keys = [
                k for k in self._connections
                if k.endswith(f":{user_id}")
            ]
            for key in matching_keys:
                await self._stop_connection_unlocked(key)

        if matching_keys:
            logger.info("mcp_user_connections_stopped", user_id=user_id, count=len(matching_keys))
        return len(matching_keys)

    # ── Connection limits (LRU eviction) ──────────────────────────

    def _count_tenant_connections(self, tenant_id: str) -> int:
        """Count active connections for a tenant. Caller should hold _lock."""
        return sum(1 for c in self._connections.values() if c.tenant_id == tenant_id)

    async def _evict_lru_unlocked(self, tenant_id: str | None = None) -> bool:
        """Evict the least-recently-used connection. Caller must hold _lock.

        If tenant_id is given, only consider connections for that tenant.
        Returns True if a connection was evicted.
        """
        candidates = [
            (k, c) for k, c in self._connections.items()
            if tenant_id is None or c.tenant_id == tenant_id
        ]
        if not candidates:
            return False

        # Find the connection with the oldest last_activity
        lru_key, lru_conn = min(candidates, key=lambda x: x[1].last_activity)
        idle_seconds = int(time.time() - lru_conn.last_activity)
        logger.warning(
            "mcp_lru_eviction",
            extension=lru_conn.extension_name,
            tenant_id=lru_conn.tenant_id,
            idle_seconds=idle_seconds,
        )
        await self._stop_connection_unlocked(lru_key)
        return True

    # ── Idle timeout background loop ──────────────────────────────

    async def start_idle_cleanup(self) -> None:
        """Start the background idle-connection cleanup loop."""
        self._idle_running = True
        self._idle_task = asyncio.create_task(self._idle_cleanup_loop())
        logger.info("mcp_idle_cleanup_started", timeout_seconds=IDLE_TIMEOUT_SECONDS, interval_seconds=IDLE_CHECK_INTERVAL_SECONDS)

    async def stop_idle_cleanup(self) -> None:
        """Stop the background idle-connection cleanup loop."""
        self._idle_running = False
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        logger.info("mcp_idle_cleanup_stopped")

    async def _idle_cleanup_loop(self) -> None:
        """Periodically check for and stop idle MCP connections."""
        while self._idle_running:
            try:
                await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
                now = time.time()
                async with self._lock:
                    # Snapshot keys to avoid mutation during iteration
                    idle_keys = [
                        key for key, conn in self._connections.items()
                        if (now - conn.last_activity) > IDLE_TIMEOUT_SECONDS
                    ]
                    for key in idle_keys:
                        conn = self._connections.get(key)
                        if conn:
                            idle_secs = int(now - conn.last_activity)
                            logger.info(
                                "mcp_idle_cleanup",
                                extension=conn.extension_name,
                                idle_seconds=idle_secs,
                                key=key,
                            )
                            await self._stop_connection_unlocked(key)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("mcp_idle_cleanup_error", error=str(e))

    # ── Query helpers ─────────────────────────────────────────────

    async def list_tools(
        self,
        extension_id: str,
        user_id: str = "__shared__",
    ) -> list[McpToolInfo]:
        """Return cached tools for a running extension."""
        key = self._key(extension_id, user_id)
        conn = self._connections.get(key)
        return conn.tools if conn else []

    def is_running(self, extension_id: str, user_id: str = "__shared__") -> bool:
        """Check if an extension is currently running."""
        return self._key(extension_id, user_id) in self._connections

    def active_connection_count(self) -> int:
        """Return the total number of active MCP connections."""
        return len(self._connections)

    async def test_connection(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
        transport: str = "stdio",
        url: str | None = None,
        auth_config: dict | None = None,
    ) -> dict[str, Any]:
        """Test an MCP server connection — connect, list tools, then shut down.

        Supports both stdio and HTTP transports.
        Returns a dict with status and discovered tools (or error).
        """
        try:
            if transport == "http":
                if not url:
                    return {"status": "error", "error": "url is required for HTTP transport"}
                headers: dict[str, str] = dict(env_vars or {})
                # Inject stored OAuth token if available
                stored_oauth_token = (auth_config or {}).get("oauth_access_token")
                if stored_oauth_token and "Authorization" not in headers:
                    token_type = (auth_config or {}).get("oauth_token_type", "Bearer")
                    headers["Authorization"] = f"{token_type} {stored_oauth_token}"
                if url.rstrip("/").endswith("/sse"):
                    ctx = sse_client(url, headers=headers or None)
                else:
                    ctx = streamablehttp_client(url, headers=headers or None)
                async with ctx as raw:
                    read_stream, write_stream = raw[0], raw[1]
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools_result = await session.list_tools()
                        tool_infos = [{"name": t.name, "description": t.description or ""} for t in tools_result.tools]
                        return {"status": "ok", "tools": tool_infos, "tool_count": len(tool_infos)}
            else:
                if not command:
                    return {"status": "error", "error": "command is required for stdio transport"}
                env = dict(os.environ)
                if env_vars:
                    env.update(env_vars)
                server_params = StdioServerParameters(command=command, args=args or [], env=env)
                async with stdio_client(server_params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools_result = await session.list_tools()
                        tool_infos = [{"name": t.name, "description": t.description or ""} for t in tools_result.tools]
                        return {"status": "ok", "tools": tool_infos, "tool_count": len(tool_infos)}
        except Exception as e:
            if transport == "http" and url:
                try:
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=8) as probe:
                        r = await probe.get(url)
                        if r.status_code in (401, 403) and ("Bearer" in r.headers.get("WWW-Authenticate", "") or r.status_code == 401):
                            return {"status": "needs_oauth", "needs_oauth": True, "error": "Authentication required"}
                except Exception:
                    pass
            return {"status": "error", "error": str(e)}


# Singleton instance
mcp_client_manager = McpClientManager()
