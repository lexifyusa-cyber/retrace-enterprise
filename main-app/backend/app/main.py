"""
ReTrace Main Application Entry Point

FastAPI backend with WebSocket support for POD communication.
Lumena Technologies — lumenatech.io
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import structlog
import time

from app.core.config import settings
from app.core.logging import setup_logging
from app.api import pods, groups, products, training, query, health, settings as settings_api, files as files_api
from app.api import agent as agent_api
from app.api import email as email_api
from app.api import channels as channels_api
from app.services.websocket_manager import websocket_manager
from app.db.database import init_db
from app.models.automation_run import AutomationRun  # ensure table creation
# Import models to ensure they're registered with Base
from app.models import pod, folder_group, product, query as query_model, settings as settings_model
from app.models import agent_session as agent_session_model  # noqa: F401  – registers AgentSession table
from app.models import sop as sop_model  # noqa: F401  – registers SOP table
from app.models import documentation as documentation_model  # noqa: F401  – registers Documentation table
from app.rag import models as rag_models  # noqa: F401  – registers ChunkRecord table
from app.models import email_connection as email_connection_model  # noqa: F401
from app.models import email_inbox as email_inbox_model  # noqa: F401
from app.models import email_message as email_message_model  # noqa: F401
from app.models import channel_connection as channel_connection_model  # noqa: F401
from app.models import tenant as tenant_model  # noqa: F401
from app.models import user_session as user_session_model  # noqa: F401
from app.models import product_access as product_access_model  # noqa: F401
from app.models import audit_log as audit_log_model  # noqa: F401
from app.api import auth as auth_api

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Application lifespan manager."""
    # Startup
    setup_logging()
    logger.info("Starting ReTrace", version=settings.APP_VERSION)
    
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
            if "debug_logging" not in cols:
                cursor.execute("ALTER TABLE llm_settings ADD COLUMN debug_logging INTEGER")
                conn.commit()
                print("[STARTUP] Added debug_logging column to llm_settings", flush=True)
            conn.close()
    except Exception as exc:
        print(f"[STARTUP] llm_settings migration skipped: {exc}", flush=True)
    
    # Migrate existing embeddings from SQLite → ChromaDB (one-time, no-op if already done)
    try:
        from app.rag.embedding_index import migrate_sqlite_to_chroma
        migrated = await migrate_sqlite_to_chroma()
        print(f"[STARTUP] ChromaDB migration: {migrated} chunks", flush=True)
    except Exception as exc:
        print(f"[STARTUP] ChromaDB migration skipped: {exc}", flush=True)
    
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

    yield
    
    # Shutdown
    logger.info("Shutting down ReTrace")
    from app.services.scheduler_service import automation_scheduler
    await automation_scheduler.stop()
    from app.services.email_poller import email_poller as _ep
    await _ep.stop()
    await websocket_manager.shutdown()


app = FastAPI(
    title="ReTrace API",
    description="Distributed Enterprise Knowledge Management — Lumena Technologies",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# Request logging middleware (add before CORS) - simplified to avoid blocking
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    # Use print for immediate logging (structlog might be blocking)
    print(f"[REQUEST] {request.method} {request.url.path}")
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        print(f"[REQUEST] {request.method} {request.url.path} - {response.status_code} ({process_time:.3f}s)")
        return response
    except Exception as e:
        process_time = time.time() - start_time
        print(f"[ERROR] {request.method} {request.url.path} - {str(e)} ({process_time:.3f}s)")
        raise

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
app.include_router(products.router, prefix="/api/v1/products", tags=["Products"])
app.include_router(training.router, prefix="/api/v1/training", tags=["Training"])
app.include_router(query.router, prefix="/api/v1/query", tags=["Query"])
app.include_router(settings_api.router, prefix="/api/v1/settings", tags=["Settings"])
app.include_router(agent_api.router, prefix="/api/v1/agent", tags=["Agent"])
app.include_router(files_api.router, prefix="/api/v1/files", tags=["Local Files"])
app.include_router(email_api.router, prefix="/api/v1/email", tags=["Email"])
app.include_router(channels_api.router, prefix="/api/v1/channels", tags=["Channels"])
app.include_router(email_api.product_email_router, prefix="/api/v1/products", tags=["Product Email"])

# RAG product-assistant router
from app.rag.api import router as rag_router
app.include_router(rag_router, prefix="/api/v1/product-assistant", tags=["Product Assistant"])


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
