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
from app.models import mcp_tool_config as mcp_tool_config_model  # noqa: F401  – registers McpToolConfig table
from app.models import mcp_extension as mcp_extension_model  # noqa: F401  – registers McpExtension table
from app.models import user_extension as user_extension_model  # noqa: F401  – registers UserExtension table
from app.models import tenant as tenant_model  # noqa: F401
from app.models import user_session as user_session_model  # noqa: F401
from app.models import team_access as team_access_model  # noqa: F401
from app.models import audit_log as audit_log_model  # noqa: F401
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
            if "screenops_mouse_timeout" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN screenops_mouse_timeout INTEGER DEFAULT 30")
                conn.commit()
                print("[STARTUP] Added screenops_mouse_timeout column to llm_settings", flush=True)
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

    # Ensure default administrator account exists (login: administrator, password: ***REMOVED***)
    _DEFAULT_ADMIN_PASSWORD = "***REMOVED***"
    try:
        from sqlalchemy import select, func
        from app.db.database import async_session_maker
        from app.models.user import User
        from app.core.security import hash_password

        async with async_session_maker() as session:
            # Case-insensitive lookup (DB may have "Administrator" or "administrator")
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
                )
                session.add(admin)
                await session.commit()
                print("[STARTUP] Created default administrator account (username: administrator, password: ***REMOVED***)", flush=True)
            else:
                # Normalize email to lowercase and reset password
                admin.email = "administrator"
                admin.hashed_password = hash_password(_DEFAULT_ADMIN_PASSWORD)
                admin.force_password_change = False
                admin.is_active = True
                await session.commit()
                print("[STARTUP] Reset administrator password to ***REMOVED***", flush=True)
    except Exception as exc:
        print(f"[STARTUP] Default admin creation skipped: {exc}", flush=True)

    # Ensure default organization (single-tenant) exists
    try:
        from app.models.tenant import Tenant
        async with async_session_maker() as session:
            result = await session.execute(select(Tenant))
            if result.scalar_one_or_none() is None:
                default_tenant = Tenant(
                    name="Default Organization",
                    domain="local",
                    auth_method="email",
                )
                session.add(default_tenant)
                await session.commit()
                print("[STARTUP] Created default organization (domain: local)", flush=True)
    except Exception as exc:
        print(f"[STARTUP] Default organization creation skipped: {exc}", flush=True)

    # Auto-seed ServiceNow as built-in extension and auto-install for all users
    try:
        from sqlalchemy import select
        from app.db.database import async_session_maker
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension
        from app.models.user import User
        import json

        async with async_session_maker() as session:
            # Check if ServiceNow extension exists
            result = await session.execute(
                select(McpExtension).where(McpExtension.name == "ServiceNow")
            )
            sn_ext = result.scalar_one_or_none()

            if sn_ext is None:
                import sys
                python_path = sys.executable

                sn_ext = McpExtension(
                    name="ServiceNow",
                    description="ServiceNow IT Service Management — create incidents, query tables, and manage tickets.",
                    command=python_path,
                    args_json=["-m", "servicenow_mcp"],
                    env_vars_json={"SERVICENOW_INSTANCE_URL": "https://dev221851.service-now.com"},
                    auth_type="auto_passthrough",
                    auth_config_json={
                        "token_env_var": "SERVICENOW_OAUTH_TOKEN",
                        "username_env_var": "SERVICENOW_USERNAME",
                        "password_env_var": "SERVICENOW_PASSWORD",
                        "auth_type_env_var": "SERVICENOW_AUTH_TYPE",
                        "oauth_auth_type_value": "oauth",
                        "ldap_auth_type_value": "basic",
                    },
                    is_builtin=True,
                    is_enabled=True,
                )
                session.add(sn_ext)
                await session.commit()
                await session.refresh(sn_ext)
                print("[STARTUP] Created ServiceNow built-in extension", flush=True)
            elif not sn_ext.is_builtin:
                sn_ext.is_builtin = True
                await session.commit()
                print("[STARTUP] Marked existing ServiceNow extension as built-in", flush=True)

            # Auto-install all built-in extensions for every active user
            builtin_result = await session.execute(
                select(McpExtension).where(McpExtension.is_builtin == True)
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

    # Start automation scheduler
    from app.services.scheduler_service import automation_scheduler
    await automation_scheduler.start()
    logger.info("Automation scheduler started")

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
    from app.services.pty_manager import pty_manager
    pty_manager.destroy_all()
    from app.services.scheduler_service import automation_scheduler
    await automation_scheduler.stop()
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
app.include_router(terminal_ws_api.router, tags=["Terminal WebSocket"])
app.include_router(browser_ws_api.router, tags=["Browser WebSocket"])
app.include_router(mcp_builder_api.router, prefix="/api/v1/mcp-builder", tags=["MCP Builder"])
app.include_router(mcp_extensions_api.router, prefix="/api/v1/mcp-extensions", tags=["MCP Extensions"])
app.include_router(email_api.team_email_router, prefix="/api/v1/teams", tags=["Team Email"])
app.include_router(email_api.team_email_router, prefix="/api/v1/products", tags=["Team Email (deprecated)"])

# RAG team-assistant router
from app.rag.api import router as rag_router
app.include_router(rag_router, prefix="/api/v1/team-assistant", tags=["Team Assistant"])

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
