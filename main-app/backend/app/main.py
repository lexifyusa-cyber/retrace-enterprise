"""
ReTrace Main Application Entry Point

FastAPI backend with WebSocket support for POD communication.
Lumena Technologies — lumenatech.io
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import structlog
import time

from app.core.config import settings
from app.core.logging import setup_logging
from app.api import pods, groups, products, teams, training, query, health, settings as settings_api, files as files_api
from app.api import agent as agent_api
from app.api import terminal_ws as terminal_ws_api
from app.api import browser_ws as browser_ws_api
from app.api import email as email_api
from app.api import channels as channels_api
from app.api import connections as connections_api
from app.api import connections_oauth as connections_oauth_api
from app.services.websocket_manager import websocket_manager
from app.db.database import init_db
from app.models.automation_run import AutomationRun  # ensure table creation
# Import models to ensure they're registered with Base
from app.models import pod, folder_group, team, query as query_model, settings as settings_model
from app.models import agent_session as agent_session_model  # noqa: F401  – registers AgentSession table
from app.models import llm_log as llm_log_model  # noqa: F401  – registers LLMLog table
from app.models import terminal_session as terminal_session_model  # noqa: F401  – registers TerminalSession table
from app.models import sop as sop_model  # noqa: F401  – registers SOP table
from app.models import documentation as documentation_model  # noqa: F401  – registers Documentation table
from app.rag import models as rag_models  # noqa: F401  – registers ChunkRecord table
from app.models import email_connection as email_connection_model  # noqa: F401
from app.models import email_inbox as email_inbox_model  # noqa: F401
from app.models import email_message as email_message_model  # noqa: F401
from app.models import channel_connection as channel_connection_model  # noqa: F401
from app.models import mcp_server as mcp_server_model  # noqa: F401  – registers McpServer table
from app.models import mcp_extension as mcp_extension_model  # noqa: F401  – registers McpExtension table
from app.models import user_extension as user_extension_model  # noqa: F401  – registers UserExtension table
from app.models import tenant as tenant_model  # noqa: F401
from app.models import user_session as user_session_model  # noqa: F401
from app.models import team_access as team_access_model  # noqa: F401
from app.models import audit_log as audit_log_model  # noqa: F401
from app.models import scheduled_followup as scheduled_followup_model  # noqa: F401  – registers ScheduledFollowup table
from app.models import tool_monitor as tool_monitor_model  # noqa: F401  – registers ToolMonitor table (unified)
from app.models import remote_trigger as remote_trigger_model  # noqa: F401  – registers RemoteTrigger table
from app.api import auth as auth_api
from app.api import mcp_builder as mcp_builder_api
from app.api import mcp_extensions as mcp_extensions_api

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Application lifespan manager."""
    # Startup
    setup_logging()
    logger.info("Starting ReTrace", version=settings.APP_VERSION)

    # Internal shared-secret for the MCP → backend browser bridge. The token
    # is generated once per host (not per process), persisted to a file, and
    # picked up by every Python process that needs to authenticate to / from
    # the backend's internal endpoints (the FastAPI process itself, the OC
    # sidecar, and the retrace-* MCP subprocesses).
    # File location: ~/.local/share/retrace/internal-token. Mode 0600 so
    # other users on the host can't read it.
    try:
        import os as _os
        from pathlib import Path as _Path
        import secrets as _secrets
        token_dir = _Path.home() / ".local" / "share" / "retrace"
        token_path = token_dir / "internal-token"
        if not _os.environ.get("RETRACE_INTERNAL_TOKEN"):
            token = None
            if token_path.exists():
                token = token_path.read_text().strip() or None
            if not token:
                token_dir.mkdir(parents=True, exist_ok=True)
                token = _secrets.token_urlsafe(32)
                token_path.write_text(token)
                _os.chmod(token_path, 0o600)
                logger.info("internal_token_generated", path=str(token_path))
            _os.environ["RETRACE_INTERNAL_TOKEN"] = token
    except Exception as exc:
        logger.warning("internal_token_init_failed", error=str(exc))

    # Capture the asyncio event loop for PTY manager's background threads
    import asyncio as _asyncio
    from app.services.pty_manager import pty_manager as _pty_mgr
    _pty_mgr.set_event_loop(_asyncio.get_running_loop())

    await init_db()
    logger.info("Database initialized", db_url=settings.DATABASE_URL)
    print(f"[STARTUP] Database URL: {settings.DATABASE_URL}", flush=True)
    
    # Migration: add agent_tools_config column to llm_settings if missing
    try:
        import sqlite3
        from pathlib import Path
        from urllib.parse import unquote
        db_url = settings.DATABASE_URL
        if "sqlite" in db_url:
            # Extract path: sqlite+aiosqlite:///path or sqlite:///path
            parsed = db_url.split("///", 1)[-1].split("?", 1)[0]
            db_path = unquote(parsed)
            if not Path(db_path).is_absolute():
                # Resolve relative to backend/ (parent of app/), same as app CWD
                backend_dir = Path(__file__).parent.parent
                db_path = str((backend_dir / parsed).resolve())
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(llm_settings)")
            cols = [row[1] for row in cursor.fetchall()]
            if "agent_tools_config" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN agent_tools_config TEXT")
                conn.commit()
                print("[STARTUP] Added agent_tools_config column to llm_settings", flush=True)
            if "screenops_model" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN screenops_model VARCHAR(255)")
                conn.commit()
                print("[STARTUP] Added screenops_model column to llm_settings", flush=True)
            if "screenops_api_url" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN screenops_api_url TEXT")
                conn.commit()
                print("[STARTUP] Added screenops_api_url column to llm_settings", flush=True)
            if "debug_logging" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN debug_logging INTEGER")
                conn.commit()
                print("[STARTUP] Added debug_logging column to llm_settings", flush=True)
            if "max_parallel_files" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN max_parallel_files INTEGER DEFAULT 1")
                conn.commit()
                print("[STARTUP] Added max_parallel_files column to llm_settings", flush=True)
            if "screenops_image_scale" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN screenops_image_scale INTEGER DEFAULT 100")
                conn.commit()
                print("[STARTUP] Added screenops_image_scale column to llm_settings", flush=True)
            if "agent_max_iterations" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN agent_max_iterations INTEGER DEFAULT 10")
                conn.commit()
                print("[STARTUP] Added agent_max_iterations column to llm_settings", flush=True)
            if "serper_api_key" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN serper_api_key TEXT")
                conn.commit()
                print("[STARTUP] Added serper_api_key column to llm_settings", flush=True)
            if "small_model_name" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN small_model_name VARCHAR(255)")
                conn.commit()
                print("[STARTUP] Added small_model_name column to llm_settings", flush=True)
            if "plan_model_name" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN plan_model_name VARCHAR(255)")
                conn.commit()
                print("[STARTUP] Added plan_model_name column to llm_settings", flush=True)
            if "max_steps" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN max_steps INTEGER DEFAULT 50")
                conn.commit()
                print("[STARTUP] Added max_steps column to llm_settings", flush=True)
            if "compaction_auto" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN compaction_auto INTEGER DEFAULT 0")
                conn.commit()
                print("[STARTUP] Added compaction_auto column to llm_settings", flush=True)
            if "compaction_tail_turns" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN compaction_tail_turns INTEGER DEFAULT 4")
                conn.commit()
                print("[STARTUP] Added compaction_tail_turns column to llm_settings", flush=True)
            if "snapshot_enabled" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN snapshot_enabled INTEGER DEFAULT 0")
                conn.commit()
                print("[STARTUP] Added snapshot_enabled column to llm_settings", flush=True)
            conn.close()
    except Exception as exc:
        print(f"[STARTUP] llm_settings migration skipped: {exc}", flush=True)

    # Migration: add external auth columns to user_sessions if missing
    try:
        import sqlite3
        from pathlib import Path
        from urllib.parse import unquote
        db_url = settings.DATABASE_URL
        if "sqlite" in db_url:
            parsed = db_url.split("///", 1)[-1].split("?", 1)[0]
            db_path = unquote(parsed)
            if not Path(db_path).is_absolute():
                backend_dir = Path(__file__).parent.parent
                db_path = str((backend_dir / parsed).resolve())
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(user_sessions)")
            cols = [row[1] for row in cursor.fetchall()]
            for col_name, col_type in [
                ("external_access_token", "TEXT"),
                ("external_id_token", "TEXT"),
                ("external_refresh_token", "TEXT"),
                ("external_token_expires_at", "DATETIME"),
                ("external_username", "TEXT"),
                ("external_password", "TEXT"),
            ]:
                if col_name not in cols:
                    cursor.execute(f"ALTER TABLE user_sessions ADD COLUMN {col_name} {col_type}")
                    conn.commit()
                    print(f"[STARTUP] Added {col_name} column to user_sessions", flush=True)
            conn.close()
    except Exception as exc:
        print(f"[STARTUP] user_sessions migration skipped: {exc}", flush=True)

    # Cleanup: remove stale sessions + add partial unique index
    try:
        import sqlite3
        from pathlib import Path
        from urllib.parse import unquote
        db_url = settings.DATABASE_URL
        if "sqlite" in db_url:
            parsed = db_url.split("///", 1)[-1].split("?", 1)[0]
            db_path = unquote(parsed)
            if not Path(db_path).is_absolute():
                backend_dir = Path(__file__).parent.parent
                db_path = str((backend_dir / parsed).resolve())
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Delete expired sessions
            cursor.execute("DELETE FROM user_sessions WHERE expires_at < datetime('now')")
            expired_count = cursor.rowcount

            # Delete revoked sessions older than 30 days
            cursor.execute("DELETE FROM user_sessions WHERE revoked = 1 AND created_at < datetime('now', '-30 days')")
            revoked_count = cursor.rowcount

            # Revoke duplicate active sessions (keep newest per refresh_token_hash)
            cursor.execute("""
                UPDATE user_sessions SET revoked = 1
                WHERE revoked = 0
                AND session_id NOT IN (
                    SELECT session_id FROM (
                        SELECT session_id, ROW_NUMBER() OVER (
                            PARTITION BY refresh_token_hash ORDER BY created_at DESC
                        ) AS rn
                        FROM user_sessions WHERE revoked = 0
                    ) WHERE rn = 1
                )
            """)
            dup_count = cursor.rowcount

            # Add partial unique index on active sessions (prevents future duplicates)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sessions_active_token
                ON user_sessions (refresh_token_hash)
                WHERE revoked = 0
            """)

            conn.commit()
            conn.close()
            if expired_count or revoked_count or dup_count:
                print(f"[STARTUP] Session cleanup: {expired_count} expired, {revoked_count} old revoked, {dup_count} duplicates", flush=True)
    except Exception as exc:
        print(f"[STARTUP] Session cleanup skipped: {exc}", flush=True)

    # Migration: create user_extensions table if missing
    try:
        import sqlite3
        from pathlib import Path
        from urllib.parse import unquote
        db_url = settings.DATABASE_URL
        if "sqlite" in db_url:
            parsed = db_url.split("///", 1)[-1].split("?", 1)[0]
            db_path = unquote(parsed)
            if not Path(db_path).is_absolute():
                backend_dir = Path(__file__).parent.parent
                db_path = str((backend_dir / parsed).resolve())
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_extensions'")
            if not cursor.fetchone():
                cursor.execute("""
                    CREATE TABLE user_extensions (
                        user_extension_id VARCHAR(36) PRIMARY KEY,
                        extension_id VARCHAR(36) NOT NULL,
                        user_id VARCHAR(36) NOT NULL,
                        tenant_id VARCHAR(36),
                        is_enabled INTEGER DEFAULT 1,
                        env_vars_json TEXT,
                        auth_config_json TEXT,
                        installed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME,
                        UNIQUE(extension_id, user_id)
                    )
                """)
                conn.commit()
                print("[STARTUP] Created user_extensions table", flush=True)
            conn.close()
    except Exception as exc:
        print(f"[STARTUP] user_extensions migration skipped: {exc}", flush=True)

    # Migration: add is_builtin/mcp_server_id to mcp_extensions, created_by to mcp_servers
    try:
        import sqlite3
        from pathlib import Path
        from urllib.parse import unquote
        db_url = settings.DATABASE_URL
        if "sqlite" in db_url:
            parsed = db_url.split("///", 1)[-1].split("?", 1)[0]
            db_path = unquote(parsed)
            if not Path(db_path).is_absolute():
                backend_dir = Path(__file__).parent.parent
                db_path = str((backend_dir / parsed).resolve())
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            # mcp_extensions columns
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mcp_extensions'")
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(mcp_extensions)")
                cols = [row[1] for row in cursor.fetchall()]
                if "is_builtin" not in cols:
                    cursor.execute("ALTER TABLE mcp_extensions ADD COLUMN is_builtin INTEGER DEFAULT 0")
                    conn.commit()
                    print("[STARTUP] Added is_builtin column to mcp_extensions", flush=True)
                if "mcp_server_id" not in cols:
                    cursor.execute("ALTER TABLE mcp_extensions ADD COLUMN mcp_server_id VARCHAR(36)")
                    conn.commit()
                    print("[STARTUP] Added mcp_server_id column to mcp_extensions", flush=True)
                if "transport" not in cols:
                    cursor.execute("ALTER TABLE mcp_extensions ADD COLUMN transport VARCHAR(20) DEFAULT 'stdio'")
                    conn.commit()
                    print("[STARTUP] Added transport column to mcp_extensions", flush=True)
                if "url" not in cols:
                    cursor.execute("ALTER TABLE mcp_extensions ADD COLUMN url TEXT")
                    conn.commit()
                    print("[STARTUP] Added url column to mcp_extensions", flush=True)
                # SQLite doesn't support ALTER COLUMN — recreate table to make command nullable
                # Detect by checking if command has a NOT NULL constraint (notnull=1 in PRAGMA)
                cursor.execute("PRAGMA table_info(mcp_extensions)")
                col_info = {row[1]: row for row in cursor.fetchall()}
                command_notnull = col_info.get("command", (None,)*4 + (0,))[3]  # index 3 is notnull
                if command_notnull:
                    print("[STARTUP] Recreating mcp_extensions table to make command nullable...", flush=True)
                    cursor.execute("PRAGMA foreign_keys=OFF")
                    cursor.executescript("""
                        CREATE TABLE mcp_extensions_new (
                            extension_id VARCHAR(36) NOT NULL PRIMARY KEY,
                            tenant_id VARCHAR(36),
                            name VARCHAR(100) NOT NULL,
                            description TEXT,
                            transport VARCHAR(20) DEFAULT 'stdio',
                            url TEXT,
                            command VARCHAR(500),
                            args_json JSON,
                            env_vars_json JSON,
                            auth_type VARCHAR(30) DEFAULT 'none',
                            auth_config_json JSON,
                            is_enabled INTEGER DEFAULT 1,
                            is_builtin INTEGER DEFAULT 0,
                            auto_install INTEGER DEFAULT 0,
                            mcp_server_id VARCHAR(36),
                            created_by VARCHAR(36),
                            created_at DATETIME,
                            updated_at DATETIME
                        );
                        INSERT INTO mcp_extensions_new
                            SELECT extension_id, tenant_id, name, description,
                                   COALESCE(transport, 'stdio'), url, command, args_json, env_vars_json,
                                   auth_type, auth_config_json, is_enabled, is_builtin,
                                   COALESCE(auto_install, 0), mcp_server_id, created_by, created_at, updated_at
                            FROM mcp_extensions;
                        DROP TABLE mcp_extensions;
                        ALTER TABLE mcp_extensions_new RENAME TO mcp_extensions;
                    """)
                    cursor.execute("PRAGMA foreign_keys=ON")
                    conn.commit()
                    print("[STARTUP] mcp_extensions table recreated with nullable command", flush=True)
            # mcp_servers columns
            cursor.execute("PRAGMA table_info(mcp_servers)")
            cols = [row[1] for row in cursor.fetchall()]
            if "created_by" not in cols:
                cursor.execute("ALTER TABLE mcp_servers ADD COLUMN created_by VARCHAR(36)")
                conn.commit()
                print("[STARTUP] Added created_by column to mcp_servers", flush=True)
            conn.close()
    except Exception as exc:
        print(f"[STARTUP] mcp_extensions/mcp_servers migration skipped: {exc}", flush=True)

    # ── Add user_id to conversations table ──
    try:
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        _db_url = settings.DATABASE_URL
        _parsed = _db_url.split("///", 1)[-1].split("?", 1)[0]
        _db_path = _parsed if _Path(_parsed).is_absolute() else str((_Path(__file__).parent.parent / _parsed).resolve())
        with _sqlite3.connect(_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(conversations)")
            cols = [row[1] for row in cursor.fetchall()]
            if cols and "user_id" not in cols:
                cursor.execute("ALTER TABLE conversations ADD COLUMN user_id VARCHAR(36)")
                cursor.execute("CREATE INDEX IF NOT EXISTS ix_conversations_user_id ON conversations(user_id)")
                conn.commit()
                print("[STARTUP] Added user_id column to conversations", flush=True)
            if cols and "opencode_session_id" not in cols:
                cursor.execute("ALTER TABLE conversations ADD COLUMN opencode_session_id VARCHAR(100)")
                cursor.execute("CREATE INDEX IF NOT EXISTS ix_conversations_opencode_session_id ON conversations(opencode_session_id)")
                conn.commit()
                print("[STARTUP] Added opencode_session_id column to conversations", flush=True)
    except Exception as exc:
        print(f"[STARTUP] conversations migration skipped: {exc}", flush=True)

    # Ensure synthetic __local__ pod exists for local filesystem browse
    try:
        from sqlalchemy import select
        from app.db.database import async_session_maker
        from app.models.pod import Pod

        async with async_session_maker() as session:
            result = await session.execute(select(Pod).where(Pod.pod_id == "__local__"))
            if result.scalar_one_or_none() is None:
                local_pod = Pod(
                    pod_id="__local__",
                    pod_name="Local",
                    os_type="local",
                    status="online",
                    metadata_json={"synthetic": True, "description": "Backend server local filesystem"},
                )
                session.add(local_pod)
                await session.commit()
                print("[STARTUP] Created synthetic __local__ pod for local filesystem browse", flush=True)
    except Exception as exc:
        print(f"[STARTUP] __local__ pod creation skipped: {exc}", flush=True)

    # Ensure default organization + administrator both exist AND are linked.
    # Tenant must come first: without `admin.tenant_id` pointing at a real
    # tenant, every team/conversation the admin creates will inherit
    # tenant_id=NULL, which then becomes invisible to any SSO user provisioned
    # into a real tenant. The first-run order was previously reversed and the
    # link was never set — see the tenant_id backfill in the git history.
    _DEFAULT_ADMIN_PASSWORD = "***REMOVED***"
    try:
        from sqlalchemy import select, func
        from app.db.database import async_session_maker
        from app.models.user import User
        from app.models.tenant import Tenant
        from app.core.security import hash_password

        async with async_session_maker() as session:
            # 1) Ensure the default tenant exists, and grab its id.
            result = await session.execute(select(Tenant))
            default_tenant = result.scalars().first()
            if default_tenant is None:
                default_tenant = Tenant(
                    name="Default Organization",
                    domain="local",
                    auth_method="email",
                )
                session.add(default_tenant)
                await session.flush()  # populate default_tenant.tenant_id
                print("[STARTUP] Created default organization (domain: local)", flush=True)

            # 2) Ensure the administrator exists and is linked to that tenant.
            result = await session.execute(
                select(User).where(func.lower(User.email) == "administrator")
            )
            admin = result.scalar_one_or_none()
            if admin is None:
                admin = User(
                    email="administrator",
                    display_name="Administrator",
                    hashed_password=hash_password(_DEFAULT_ADMIN_PASSWORD),
                    role="admin",
                    auth_provider="email",
                    force_password_change=False,
                    tenant_id=default_tenant.tenant_id,
                )
                session.add(admin)
                print("[STARTUP] Created default administrator account (username: administrator, password: ***REMOVED***)", flush=True)
            else:
                # Normalize email, reset password, and self-heal tenant link
                # if a pre-fix install left it NULL.
                admin.email = "administrator"
                admin.hashed_password = hash_password(_DEFAULT_ADMIN_PASSWORD)
                admin.force_password_change = False
                admin.is_active = True
                if admin.tenant_id is None:
                    admin.tenant_id = default_tenant.tenant_id
                    print("[STARTUP] Linked administrator to default tenant (self-heal)", flush=True)
                print("[STARTUP] Reset administrator password to ***REMOVED***", flush=True)

            await session.commit()
    except Exception as exc:
        print(f"[STARTUP] Default admin/tenant setup skipped: {exc}", flush=True)

    # Auto-seed built-in MCP extensions and auto-install them for all users.
    try:
        from sqlalchemy import select
        from app.db.database import async_session_maker
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension
        from app.models.user import User

        async with async_session_maker() as session:
            import sys

            python_path = sys.executable
            builtin_specs = [
                {
                    "name": "ServiceNow",
                    "description": "ServiceNow IT Service Management - create incidents, query tables, and manage tickets.",
                    "command": python_path,
                    "args_json": ["-m", "servicenow_mcp"],
                    "env_vars_json": {"SERVICENOW_INSTANCE_URL": "https://dev221851.service-now.com"},
                    "auth_type": "auto_passthrough",
                    "auth_config_json": {
                        "token_env_var": "SERVICENOW_OAUTH_TOKEN",
                        "username_env_var": "SERVICENOW_USERNAME",
                        "password_env_var": "SERVICENOW_PASSWORD",
                        "auth_type_env_var": "SERVICENOW_AUTH_TYPE",
                        "oauth_auth_type_value": "oauth",
                        "ldap_auth_type_value": "basic",
                    },
                },
                {
                    "name": "Grafana",
                    "description": "Grafana observability MCP - query dashboards, datasources, alerts, and metrics context.",
                    "command": "npx",
                    "args_json": ["-y", "@leval/mcp-grafana"],
                    "env_vars_json": {
                        "GRAFANA_URL": "",
                        "GRAFANA_API_KEY": "",
                    },
                },
                {
                    "name": "Prometheus",
                    "description": "Prometheus monitoring MCP - query PromQL, labels, series, targets, and alert state.",
                    "command": "npx",
                    "args_json": ["-y", "mcp-prometheus"],
                    "env_vars_json": {
                        "PROMETHEUS_URL": "",
                    },
                },
                {
                    "name": "Azure",
                    "description": "Azure MCP - inspect and operate Azure resources through the Azure MCP server.",
                    "command": "npx",
                    "args_json": ["-y", "@azure/mcp", "server", "start"],
                    "env_vars_json": {},
                },
                {
                    "name": "Teams",
                    "description": "Microsoft Teams MCP - access Teams data and actions through Microsoft Graph.",
                    "command": "npx",
                    "args_json": ["-y", "@floriscornel/teams-mcp"],
                    "env_vars_json": {
                        "MICROSOFT_CLIENT_ID": "",
                        "MICROSOFT_CLIENT_SECRET": "",
                        "MICROSOFT_TENANT_ID": "",
                    },
                },
                {
                    "name": "Outlook",
                    "description": "Microsoft Outlook MCP - access mail and calendar through Microsoft Graph.",
                    "command": "npx",
                    "args_json": ["-y", "microsoft-outlook-mcp"],
                    "env_vars_json": {
                        "MICROSOFT_CLIENT_ID": "",
                        "MICROSOFT_CLIENT_SECRET": "",
                        "MICROSOFT_TENANT_ID": "",
                    },
                },
                {
                    "name": "Slack",
                    "description": "Slack MCP - search channels, read messages, and interact with Slack workspace data.",
                    "command": "npx",
                    "args_json": ["-y", "@modelcontextprotocol/server-slack"],
                    "env_vars_json": {
                        "SLACK_BOT_TOKEN": "",
                        "SLACK_TEAM_ID": "",
                    },
                },
            ]

            created_count = 0
            updated_count = 0
            for spec in builtin_specs:
                result = await session.execute(
                    select(McpExtension).where(McpExtension.name == spec["name"])
                )
                ext = result.scalar_one_or_none()
                if ext is None:
                    ext = McpExtension(
                        name=spec["name"],
                        description=spec["description"],
                        transport="stdio",
                        command=spec["command"],
                        args_json=spec["args_json"],
                        env_vars_json=spec.get("env_vars_json") or {},
                        auth_type=spec.get("auth_type", "none"),
                        auth_config_json=spec.get("auth_config_json"),
                        is_builtin=True,
                        auto_install=True,
                        is_enabled=True,
                    )
                    session.add(ext)
                    created_count += 1
                    continue

                changed = False
                for field in (
                    "description",
                    "command",
                    "args_json",
                    "env_vars_json",
                    "auth_type",
                    "auth_config_json",
                ):
                    value = spec.get(field)
                    if field == "auth_type":
                        value = value or "none"
                    if getattr(ext, field) != value:
                        setattr(ext, field, value)
                        changed = True
                if ext.transport != "stdio":
                    ext.transport = "stdio"
                    changed = True
                if not ext.is_builtin:
                    ext.is_builtin = True
                    changed = True
                if not getattr(ext, "auto_install", False):
                    ext.auto_install = True
                    changed = True
                if not ext.is_enabled:
                    ext.is_enabled = True
                    changed = True
                if changed:
                    updated_count += 1

            if created_count or updated_count:
                await session.commit()
                print(
                    f"[STARTUP] Built-in MCP extensions seeded: {created_count} created, {updated_count} updated",
                    flush=True,
                )

            # Auto-install all enabled, auto-install built-in extensions for every active user
            builtin_result = await session.execute(
                select(McpExtension).where(
                    McpExtension.is_builtin == True,
                    McpExtension.auto_install == True,
                    McpExtension.is_enabled == True,
                )
            )
            builtin_exts = builtin_result.scalars().all()

            if builtin_exts:
                users_result = await session.execute(
                    select(User).where(User.is_active == True)
                )
                all_users = users_result.scalars().all()

                installed_count = 0
                for ext in builtin_exts:
                    for user in all_users:
                        existing = await session.execute(
                            select(UserExtension)
                            .where(UserExtension.extension_id == ext.extension_id)
                            .where(UserExtension.user_id == user.user_id)
                        )
                        if existing.scalar_one_or_none() is None:
                            user_ext = UserExtension(
                                extension_id=ext.extension_id,
                                user_id=user.user_id,
                                tenant_id=user.tenant_id,
                                is_enabled=True,
                            )
                            session.add(user_ext)
                            installed_count += 1

                if installed_count > 0:
                    await session.commit()
                    print(f"[STARTUP] Auto-installed built-in extensions: {installed_count} new user-extension records", flush=True)
    except Exception as exc:
        print(f"[STARTUP] Built-in extension seeding skipped: {exc}", flush=True)

    # Rehydrate _active_team_id from user.preferences. Covers the case where
    # the backend restarts and a user with a tab still open asks a KB
    # question before their tab fires the next regenerate-config. The
    # in-memory dict would otherwise be empty until that fires.
    try:
        from app.api.agent import _active_team_id
        from app.models.user import User
        async with async_session_maker() as session:
            users_result = await session.execute(
                select(User).where(User.is_active == True)
            )
            for u in users_result.scalars().all():
                atid = (u.preferences or {}).get("active_team_id")
                if atid:
                    _active_team_id[u.user_id] = atid
            if _active_team_id:
                print(f"[STARTUP] Hydrated active_team_id for {len(_active_team_id)} user(s)", flush=True)
    except Exception as exc:
        print(f"[STARTUP] active_team hydration skipped: {exc}", flush=True)

    # Start automation scheduler
    from app.services.scheduler_service import automation_scheduler
    await automation_scheduler.start()
    logger.info("Automation scheduler started")

    # Start follow-up scheduler (Option B — agent-scheduled future turns)
    from app.services.followup_scheduler import followup_scheduler
    await followup_scheduler.start()
    logger.info("Follow-up scheduler started")

    # Start unified tool monitor service (generic monitor primitive — watches
    # any tool's output, url/command/builtin/mcp, with a fire_when predicate;
    # zero LLM cost during quiet windows).
    from app.services.tool_monitor import tool_monitor_service
    await tool_monitor_service.start()
    logger.info("Tool monitor service started")

    # Start stream monitor service (live process-stream + websocket watches;
    # one persistent asyncio task per monitor). Gated by RETRACE_ENABLE_STREAM_MONITOR.
    from app.services.stream_monitor import stream_monitor_service
    await stream_monitor_service.start()
    logger.info("Stream monitor service started")

    # Start WebSocket server in background
    asyncio.create_task(websocket_manager.start_server())
    logger.info("WebSocket server starting", port=settings.WEBSOCKET_PORT)

    # Start email poller (checks every 60s for new unread emails)
    from app.services.email_poller import email_poller
    await email_poller.start()

    # Start MCP extension idle cleanup loop
    from app.services.mcp_client_service import mcp_client_manager
    await mcp_client_manager.start_idle_cleanup()
    logger.info("MCP client idle cleanup started")

    # Check sandbox availability (non-blocking)
    try:
        from app.services.sandbox_manager import is_sandbox_available, _build_sandbox_image
        status = is_sandbox_available()
        if status.get("available"):
            logger.info("sandbox_ready", status="available")
        elif status.get("needs_build"):
            # Build in background thread so app starts immediately
            import threading
            logger.info("sandbox_building_image_in_background")
            threading.Thread(target=_build_sandbox_image, daemon=True).start()
        else:
            logger.info("sandbox_not_available", reason=status.get("reason", "unknown"))
    except Exception as sandbox_check_err:
        logger.warning("sandbox_startup_check_failed", error=str(sandbox_check_err))

    # Start sandbox idle cleanup background task (every 5 minutes)
    async def _sandbox_idle_cleanup_loop():
        import asyncio as _aio
        while True:
            await _aio.sleep(300)  # 5 minutes
            try:
                from app.services.sandbox_manager import cleanup_idle_sessions
                cleanup_idle_sessions()
            except Exception:
                pass

    _sandbox_cleanup_task = _asyncio.create_task(_sandbox_idle_cleanup_loop())

    yield
    
    # Shutdown
    logger.info("Shutting down ReTrace")
    from app.services.opencode_manager import opencode_manager
    await opencode_manager.stop_all()
    from app.services.pty_manager import pty_manager
    pty_manager.destroy_all()
    from app.services.scheduler_service import automation_scheduler
    await automation_scheduler.stop()
    from app.services.followup_scheduler import followup_scheduler
    await followup_scheduler.stop()
    from app.services.tool_monitor import tool_monitor_service
    await tool_monitor_service.stop()
    from app.services.stream_monitor import stream_monitor_service
    await stream_monitor_service.stop()
    from app.services.email_poller import email_poller as _ep
    await _ep.stop()
    from app.services.mcp_client_service import mcp_client_manager as _mcp_mgr
    await _mcp_mgr.stop_idle_cleanup()
    await _mcp_mgr.stop_all()
    # Close all browser sessions
    from app.services.browser_manager import browser_manager as _bm
    await _bm.close_all()
    await websocket_manager.shutdown()
    # Destroy all sandbox containers
    try:
        from app.services.sandbox_manager import destroy_all_sessions
        destroy_all_sessions()
    except Exception:
        pass


app = FastAPI(
    title="ReTrace API",
    description="Distributed Enterprise Knowledge Management — Lumena Technologies",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# Request logging middleware (add before CORS) - uses stderr to avoid polluting agent tool output
@app.middleware("http")
async def log_requests(request: Request, call_next):
    import sys
    start_time = time.time()
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        print(f"[REQUEST] {request.method} {request.url.path} - {response.status_code} ({process_time:.3f}s)", file=sys.stderr)
        return response
    except Exception as e:
        process_time = time.time() - start_time
        print(f"[ERROR] {request.method} {request.url.path} - {str(e)} ({process_time:.3f}s)", file=sys.stderr)
        raise

# Gzip compression — reduces JS bundle from 1.5MB to ~568KB
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_api.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(health.router, tags=["Health"])
app.include_router(pods.router, prefix="/api/v1/pods", tags=["PODs"])
app.include_router(groups.router, prefix="/api/v1/groups", tags=["Folder Groups"])
app.include_router(teams.router, prefix="/api/v1/teams", tags=["Teams"])
app.include_router(products.router, prefix="/api/v1/products", tags=["Products (deprecated)"])
app.include_router(training.router, prefix="/api/v1/training", tags=["Training"])
app.include_router(query.router, prefix="/api/v1/query", tags=["Query"])
app.include_router(settings_api.router, prefix="/api/v1/settings", tags=["Settings"])
app.include_router(agent_api.router, prefix="/api/v1/agent", tags=["Agent"])
app.include_router(files_api.router, prefix="/api/v1/files", tags=["Local Files"])
app.include_router(email_api.router, prefix="/api/v1/email", tags=["Email"])
app.include_router(channels_api.router, prefix="/api/v1/channels", tags=["Channels"])
app.include_router(connections_api.router, prefix="/api/v1/connections", tags=["Connections"])
app.include_router(connections_oauth_api.router, prefix="/api/v1/connections", tags=["Connections"])
app.include_router(terminal_ws_api.router, tags=["Terminal WebSocket"])
app.include_router(browser_ws_api.router, tags=["Browser WebSocket"])
app.include_router(mcp_builder_api.router, prefix="/api/v1/mcp-builder", tags=["MCP Builder"])
app.include_router(mcp_extensions_api.router, prefix="/api/v1/mcp-extensions", tags=["MCP Extensions"])
app.include_router(email_api.team_email_router, prefix="/api/v1/teams", tags=["Team Email"])
app.include_router(email_api.team_email_router, prefix="/api/v1/products", tags=["Team Email (deprecated)"])

# RAG team-assistant router
from app.rag.api import router as rag_router
app.include_router(rag_router, prefix="/api/v1/team-assistant", tags=["Team Assistant"])

# Remote-trigger webhook router (Tier 4). The /api/v1/triggers/<token> path
# is intentionally unauth — the token in the URL IS the secret. See
# app/api/triggers.py and app/services/remote_trigger.py.
from app.api import triggers as triggers_api
app.include_router(triggers_api.router, tags=["Remote Triggers"])

# ── Serve built React frontend (production only) ────────────────────
_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.is_dir():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=str(_static_dir / "assets")), name="static-assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve the React SPA. API routes are handled by routers above."""
        file_path = _static_dir / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        # Fallback to index.html for client-side routing
        return FileResponse(str(_static_dir / "index.html"))
else:
    @app.get("/")
    async def root():
        """Root endpoint."""
        return {
            "name": "ReTrace API",
            "version": settings.APP_VERSION,
            "status": "running"
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
