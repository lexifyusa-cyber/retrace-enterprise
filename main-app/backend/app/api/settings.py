"""
Settings and configuration API endpoints.
"""

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import structlog

from app.core.config import settings as app_settings
from app.db.database import get_session, async_session_maker
from app.models.settings import LLMSettingsModel

logger = structlog.get_logger()
router = APIRouter()


class LLMSettings(BaseModel):
    """Schema for LLM settings. api_key is optional to allow partial saves (e.g. only embeddings)."""
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: str = Field(..., min_length=1)
    provider: str = Field(default="openai", pattern="^(openai|anthropic|custom)$")
    # Embeddings configuration (all optional)
    embeddings_api_url: Optional[str] = None
    embeddings_api_key: Optional[str] = None
    embeddings_model: Optional[str] = None
    # ScreenOps (Computer Use) — Anthropic API key and model for Claude coordinate finder
    screenops_api_key: Optional[str] = None
    screenops_model: Optional[str] = None
    # Training: when True, pipeline logs included/excluded folders and files
    debug_logging: Optional[bool] = None

    class Config:
        # Allow extra fields to be ignored
        extra = "ignore"


class LLMSettingsResponse(BaseModel):
    """Schema for LLM settings response (hides API key)."""
    api_url: Optional[str] = None
    model_name: str
    provider: str
    api_key_set: bool
    embeddings_api_url: Optional[str] = None
    embeddings_model: Optional[str] = None
    embeddings_api_key_set: bool
    screenops_api_key_set: bool = False
    screenops_model: Optional[str] = None
    debug_logging: bool = False


class TestConnectionResponse(BaseModel):
    """Schema for connection test response."""
    success: bool
    message: str
    latency_ms: Optional[int] = None
    model_info: Optional[dict] = None


class AgentToolsUpdate(BaseModel):
    """Schema for updating agent tools (enabled/disabled)."""
    disabled_tools: list[str] = Field(default_factory=list, description="Tool names to disable")


async def get_or_create_settings(session: AsyncSession) -> LLMSettingsModel:
    """Get or create settings record (singleton - always id=1)."""
    import sys
    try:
        print("[DB] Executing SELECT query...", flush=True)
        result = await session.execute(
            select(LLMSettingsModel).where(LLMSettingsModel.id == 1)
        )
        print("[DB] Query executed, getting result...", flush=True)
        settings = result.scalar_one_or_none()
        print(f"[DB] Result: {'found' if settings else 'not found'}", flush=True)
        
        if not settings:
            print("[DB] Creating new settings record...", flush=True)
            # Create default settings
            settings = LLMSettingsModel(
                id=1,
                provider="openai",
                model_name=app_settings.REASONING_MODEL,
            )
            session.add(settings)
            # Flush to get the ID, but don't commit yet
            # Note: This is called from get_llm_settings which uses get_session dependency
            # The dependency will auto-commit, so we don't need to commit here
            print("[DB] Flushing new record...", flush=True)
            await session.flush()
            print("[DB] Refreshing record...", flush=True)
            await session.refresh(settings)
            print("[DB] New settings created (will be committed by get_session)", flush=True)
        
        return settings
    except Exception as e:
        print(f"[DB] ERROR in get_or_create_settings: {str(e)}", flush=True)
        import traceback
        traceback.print_exc()
        logger.error("Error in get_or_create_settings", error=str(e), exc_info=True)
        raise


@router.get("/test")
async def test_settings_endpoint():
    """Test endpoint to verify settings API is reachable."""
    logger.info("=== TEST ENDPOINT CALLED ===")
    return {"status": "ok", "message": "Settings API is reachable"}


@router.post("/llm", response_model=LLMSettingsResponse)
async def update_llm_settings(settings_data: LLMSettings):
    """Update LLM settings."""
    import asyncio
    import sys
    
    # Use print for immediate logging
    print(f"[SETTINGS] POST /llm - Provider: {settings_data.provider}, Model: {settings_data.model_name}", flush=True)
    sys.stdout.flush()
    
    logger.info("=== SETTINGS UPDATE REQUEST RECEIVED ===")
    logger.info(f"Provider: {settings_data.provider}, Model: {settings_data.model_name}")
    
    # Use manual session management with explicit transaction
    try:
        print("[SETTINGS] Creating database session...", flush=True)
        async with async_session_maker() as session:
            print("[SETTINGS] Session created, starting transaction...", flush=True)
            # Use explicit transaction context to ensure proper commit
            async with session.begin():
                print("[SETTINGS] Transaction started, getting settings...", flush=True)
                db_settings = await get_or_create_settings(session)
                print(f"[SETTINGS] Got settings from DB, ID: {db_settings.id}", flush=True)
                
                # Update chat settings - only update api_key when provided (partial save)
                if settings_data.api_key is not None and settings_data.api_key.strip():
                    db_settings.api_key = settings_data.api_key.strip()
                db_settings.model_name = settings_data.model_name
                db_settings.provider = settings_data.provider
                
                # For optional api_url: update if provided
                if settings_data.api_url is not None:
                    db_settings.api_url = settings_data.api_url if settings_data.api_url else None
                
                # Update embeddings settings - only if provided
                if settings_data.embeddings_api_url is not None:
                    db_settings.embeddings_api_url = settings_data.embeddings_api_url if settings_data.embeddings_api_url else None
                
                if settings_data.embeddings_api_key is not None:
                    db_settings.embeddings_api_key = settings_data.embeddings_api_key if settings_data.embeddings_api_key else None
                
                if settings_data.embeddings_model is not None:
                    db_settings.embeddings_model = settings_data.embeddings_model if settings_data.embeddings_model else None
                
                # ScreenOps API key and model — only update if provided
                if settings_data.screenops_api_key is not None:
                    db_settings.screenops_api_key = settings_data.screenops_api_key if settings_data.screenops_api_key else None
                if settings_data.screenops_model is not None:
                    db_settings.screenops_model = (settings_data.screenops_model or "").strip() or None
                if settings_data.debug_logging is not None:
                    db_settings.debug_logging = bool(settings_data.debug_logging)

                print("[SETTINGS] Changes staged, flushing before commit...", flush=True)
                # Flush to ensure all changes are applied
                await session.flush()
                
                # Refresh to get updated values (before commit, while still in transaction)
                await session.refresh(db_settings)
                print("[SETTINGS] Object refreshed", flush=True)
                
                print("[SETTINGS] Transaction will commit on exit...", flush=True)
                # Transaction will auto-commit when exiting the 'begin()' context
                
            # Transaction committed here
            print("[SETTINGS] Transaction committed successfully", flush=True)
            
            # Close any remaining implicit transactions to prevent rollback
            if session.in_transaction():
                await session.commit()
                print("[SETTINGS] Committed any remaining implicit transaction", flush=True)
            
            # Verify the commit worked by checking the database directly
            import sqlite3 as sqlite_sync
            from pathlib import Path
            
            # Get database path from config
            db_url = app_settings.DATABASE_URL
            if db_url.startswith("sqlite+aiosqlite:///"):
                db_path = db_url.replace("sqlite+aiosqlite:///", "")
                if not db_path.startswith("/"):
                    # Relative path - resolve from backend directory
                    backend_dir = Path(__file__).parent.parent.parent
                    db_path = str(backend_dir / db_path)
            else:
                db_path = None
            
            # Quick sync check to verify data was written
            if db_path:
                try:
                    conn = sqlite_sync.connect(db_path, timeout=1.0)
                    cursor = conn.cursor()
                    cursor.execute("SELECT model_name, provider, api_key IS NOT NULL as has_key FROM llm_settings WHERE id = 1")
                    result = cursor.fetchone()
                    conn.close()
                    if result:
                        print(f"[SETTINGS] ✅ Verified in DB: model={result[0]}, provider={result[1]}, has_key={result[2]}", flush=True)
                    else:
                        print("[SETTINGS] ⚠️  WARNING: Settings not found in DB after commit!", flush=True)
                except Exception as verify_error:
                    print(f"[SETTINGS] Could not verify (non-critical): {verify_error}", flush=True)
            
            # Build response from the refreshed object
            response = LLMSettingsResponse(
                api_url=db_settings.api_url,
                model_name=db_settings.model_name or app_settings.REASONING_MODEL,
                provider=db_settings.provider or "openai",
                api_key_set=bool(db_settings.api_key),
                embeddings_api_url=db_settings.embeddings_api_url,
                embeddings_model=db_settings.embeddings_model,
                embeddings_api_key_set=bool(db_settings.embeddings_api_key),
                screenops_api_key_set=bool(db_settings.screenops_api_key),
                screenops_model=db_settings.screenops_model,
                debug_logging=bool(getattr(db_settings, "debug_logging", False)),
            )
            print("[SETTINGS] Returning response", flush=True)
            return response
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"[SETTINGS] ERROR: {str(e)}", flush=True)
        import traceback
        traceback.print_exc()
        logger.error("Failed to update LLM settings", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {str(e)}")


@router.get("/llm", response_model=LLMSettingsResponse)
async def get_llm_settings(session: AsyncSession = Depends(get_session)):
    """Get current LLM settings."""
    print("[GET_SETTINGS] Loading settings from database...", flush=True)
    db_settings = await get_or_create_settings(session)
    
    print(f"[GET_SETTINGS] Loaded: provider={db_settings.provider}, model={db_settings.model_name}, api_url={db_settings.api_url}", flush=True)
    print(f"[GET_SETTINGS] Embeddings: url={db_settings.embeddings_api_url}, model={db_settings.embeddings_model}, has_key={bool(db_settings.embeddings_api_key)}", flush=True)

    response = LLMSettingsResponse(
        api_url=db_settings.api_url,
        model_name=db_settings.model_name or app_settings.REASONING_MODEL,
        provider=db_settings.provider or "openai",
        api_key_set=bool(db_settings.api_key or app_settings.OPENAI_API_KEY),
        embeddings_api_url=db_settings.embeddings_api_url,
        embeddings_model=db_settings.embeddings_model or app_settings.EMBEDDING_MODEL,
        embeddings_api_key_set=bool(db_settings.embeddings_api_key),
        screenops_api_key_set=bool(db_settings.screenops_api_key),
        screenops_model=db_settings.screenops_model,
        debug_logging=bool(getattr(db_settings, "debug_logging", False)),
    )
    
    print(f"[GET_SETTINGS] Returning: provider={response.provider}, model={response.model_name}, api_url={response.api_url}", flush=True)
    return response


@router.post("/llm/test", response_model=TestConnectionResponse)
async def test_llm_connection(settings_data: LLMSettings, session: AsyncSession = Depends(get_session)):
    """Test connectivity to both Chat and Embeddings APIs."""
    import time
    from openai import AsyncOpenAI
    from anthropic import AsyncAnthropic

    # If API key is a placeholder or empty, use saved settings
    db_settings = None
    if not settings_data.api_key or settings_data.api_key == 'saved-key-placeholder':
        db_settings = await get_or_create_settings(session)
        settings_data.api_key = db_settings.api_key or app_settings.OPENAI_API_KEY
        if not settings_data.embeddings_api_key:
            settings_data.embeddings_api_key = db_settings.embeddings_api_key
        if not settings_data.screenops_api_key or settings_data.screenops_api_key == "saved-key-placeholder":
            settings_data.screenops_api_key = db_settings.screenops_api_key
    # Resolve ScreenOps placeholder when chat key was provided in form
    if (not settings_data.screenops_api_key or settings_data.screenops_api_key == "saved-key-placeholder") and settings_data.api_key:
        if db_settings is None:
            db_settings = await get_or_create_settings(session)
        settings_data.screenops_api_key = db_settings.screenops_api_key
    # Resolve ScreenOps model from request, then DB, then default
    if db_settings is None:
        db_settings = await get_or_create_settings(session)
    screenops_model = (settings_data.screenops_model or db_settings.screenops_model or "claude-3-haiku-20240307").strip()

    results = {"chat": {}, "embeddings": {}, "screenops": {}}
    
    # Test Chat API
    try:
        start_time = time.time()
        client_kwargs = {"api_key": settings_data.api_key}
        if settings_data.api_url:
            client_kwargs["base_url"] = settings_data.api_url

        if settings_data.provider == "openai" or settings_data.provider == "custom":
            client = AsyncOpenAI(**client_kwargs)
            response = await client.chat.completions.create(
                model=settings_data.model_name,
                messages=[{"role": "user", "content": "Say OK"}],
                max_tokens=5
            )
            response_model = response.model
            response_content = response.choices[0].message.content
        elif settings_data.provider == "anthropic":
            client = AsyncAnthropic(api_key=settings_data.api_key)
            response = await client.messages.create(
                model=settings_data.model_name,
                max_tokens=5,
                messages=[{"role": "user", "content": "Say OK"}]
            )
            response_model = response.model
            response_content = response.content[0].text if response.content else ""
        else:
            raise ValueError("Invalid provider specified")

        latency_ms = int((time.time() - start_time) * 1000)
        results["chat"] = {
            "success": True,
            "message": f"✅ Chat: {response_model}",
            "latency_ms": latency_ms,
            "response": response_content
        }
    except Exception as e:
        results["chat"] = {
            "success": False,
            "message": f"❌ Chat: {str(e)[:100]}",
            "latency_ms": 0
        }
    
    # Test Embeddings API
    # If a dedicated embeddings key is configured, it's a separate provider (e.g. OpenAI for embeddings)
    # so do NOT fall back to the chat provider's URL — let it use the embeddings provider's default.
    if settings_data.embeddings_api_key:
        emb_key = settings_data.embeddings_api_key
        emb_url = settings_data.embeddings_api_url or None  # Use embeddings provider default
    else:
        emb_key = settings_data.api_key if settings_data.provider == "openai" else None
        emb_url = settings_data.embeddings_api_url or (settings_data.api_url if settings_data.provider == "openai" else None)
    emb_model = settings_data.embeddings_model or app_settings.EMBEDDING_MODEL
    
    if emb_key:
        try:
            start_time = time.time()
            emb_kwargs = {"api_key": emb_key}
            if emb_url:
                emb_kwargs["base_url"] = emb_url
            emb_client = AsyncOpenAI(**emb_kwargs)
            emb_response = await emb_client.embeddings.create(
                model=emb_model,
                input="test"
            )
            latency_ms = int((time.time() - start_time) * 1000)
            results["embeddings"] = {
                "success": True,
                "message": f"✅ Embeddings: {emb_response.model} (dim: {len(emb_response.data[0].embedding)})",
                "latency_ms": latency_ms
            }
        except Exception as e:
            results["embeddings"] = {
                "success": False,
                "message": f"❌ Embeddings: {str(e)[:100]}",
                "latency_ms": 0
            }
    else:
        results["embeddings"] = {
            "success": False,
            "message": "⚠️  Embeddings: Not configured",
            "latency_ms": 0
        }

    # Test ScreenOps (Anthropic) API
    if settings_data.screenops_api_key:
        try:
            start_time = time.time()
            screenops_client = AsyncAnthropic(api_key=settings_data.screenops_api_key)
            _ = await screenops_client.messages.create(
                model=screenops_model,
                max_tokens=5,
                messages=[{"role": "user", "content": "Say OK"}],
            )
            latency_ms = int((time.time() - start_time) * 1000)
            results["screenops"] = {
                "success": True,
                "message": "✅ ScreenOps: Anthropic key valid",
                "latency_ms": latency_ms,
            }
        except Exception as e:
            results["screenops"] = {
                "success": False,
                "message": f"❌ ScreenOps: {str(e)[:100]}",
                "latency_ms": 0,
            }
    else:
        results["screenops"] = {
            "success": False,
            "message": "⚠️  ScreenOps: Not configured",
            "latency_ms": 0,
        }

    messages = [
        results["chat"].get("message", ""),
        results["embeddings"].get("message", ""),
        results["screenops"].get("message", ""),
    ]
    total_latency = (
        results["chat"].get("latency_ms", 0)
        + results["embeddings"].get("latency_ms", 0)
        + results["screenops"].get("latency_ms", 0)
    )

    return TestConnectionResponse(
        success=results["chat"].get("success", False),
        message="\n".join(messages),
        latency_ms=total_latency,
        model_info=results
    )


@router.patch("/agent-tools")
async def update_agent_tools(
    data: AgentToolsUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Update which agent tools are enabled/disabled. Pass disabled_tools list."""
    db_settings = await get_or_create_settings(session)
    db_settings.agent_tools_config = json.dumps({"disabled": data.disabled_tools})
    await session.flush()
    return {"ok": True, "disabled_tools": data.disabled_tools}


@router.get("/llm/api-key")
async def get_api_key(session: AsyncSession = Depends(get_session)):
    """Get the current API key (for internal use)."""
    db_settings = await get_or_create_settings(session)
    return {
        "api_key": db_settings.api_key or app_settings.OPENAI_API_KEY
    }


@router.get("/llm/model")
async def get_model_name(session: AsyncSession = Depends(get_session)):
    """Get the current model name (for internal use)."""
    db_settings = await get_or_create_settings(session)
    return {
        "model_name": db_settings.model_name or app_settings.REASONING_MODEL,
        "provider": db_settings.provider or "openai"
    }


async def get_active_llm_settings(session: AsyncSession) -> dict:
    """Get active LLM settings for internal use (requires session)."""
    db_settings = await get_or_create_settings(session)
    agent_tools_config = None
    if getattr(db_settings, "agent_tools_config", None):
        try:
            agent_tools_config = json.loads(db_settings.agent_tools_config)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "api_key": db_settings.api_key or app_settings.OPENAI_API_KEY,
        "api_url": db_settings.api_url,
        "model_name": db_settings.model_name or app_settings.REASONING_MODEL,
        "provider": db_settings.provider or "openai",
        "embeddings_api_url": db_settings.embeddings_api_url,
        "embeddings_api_key": db_settings.embeddings_api_key,
        "embeddings_model": db_settings.embeddings_model or app_settings.EMBEDDING_MODEL,
        "screenops_api_key": db_settings.screenops_api_key,
        "screenops_model": getattr(db_settings, "screenops_model", None),
        "agent_tools_config": agent_tools_config,
        "debug_logging": bool(getattr(db_settings, "debug_logging", False)),
    }
