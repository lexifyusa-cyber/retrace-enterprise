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
from app.core.security import get_current_user, CurrentUser
from app.db.database import get_session, async_session_maker
from app.models.settings import LLMSettingsModel

logger = structlog.get_logger()
router = APIRouter()


def _configured_secret(value: Optional[str]) -> Optional[str]:
    """Return a stripped secret only when it is not an example/placeholder."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    placeholder_markers = (
        "your-api-key",
        "your-openai-api-key",
        "sk-your-",
        "placeholder",
        "changeme",
        "change-me",
        "replace-me",
    )
    if any(marker in lowered for marker in placeholder_markers):
        return None
    return cleaned


def _resolved_openai_api_key(db_settings: LLMSettingsModel) -> Optional[str]:
    return _configured_secret(db_settings.api_key) or _configured_secret(app_settings.OPENAI_API_KEY)


class LLMSettings(BaseModel):
    """Schema for LLM settings."""
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: str = Field(..., min_length=1)
    provider: str = Field(default="openai", pattern="^(openai|anthropic|custom)$")
    # ScreenOps (Computer Use) — URL, model, and API key for coordinate finder (OpenAI-compatible)
    screenops_api_url: Optional[str] = None
    screenops_api_key: Optional[str] = None
    screenops_model: Optional[str] = None
    # ScreenOps image scale 25–100: percentage of screenshot size sent to model (reduces tokens)
    screenops_image_scale: Optional[int] = Field(None, ge=25, le=100)
    # Central delay (ms) before every screenshot capture (0–30000, default 2000)
    screenshot_delay_ms: Optional[int] = Field(None, ge=0, le=30000)
    # Web Search — Serper API key for enhanced web search
    serper_api_key: Optional[str] = None
    # OpenCode agent — small model for title generation, summaries
    small_model_name: Optional[str] = None
    # OpenCode agent — plan agent model (defaults to model_name)
    plan_model_name: Optional[str] = None
    # OpenCode agent — max iterations per turn
    max_steps: Optional[int] = Field(None, ge=1, le=200)
    # OpenCode agent — compaction
    compaction_auto: Optional[bool] = None
    compaction_tail_turns: Optional[int] = Field(None, ge=1, le=50)
    # OpenCode agent — snapshot
    snapshot_enabled: Optional[bool] = None
    # Agent max iterations (1–50, default 10)
    agent_max_iterations: Optional[int] = Field(None, ge=1, le=50)
    # Training: when True, pipeline logs included/excluded folders and files
    debug_logging: Optional[bool] = None
    # Training Phase 3: max files to extract in parallel (1–32, default 1)
    max_parallel_files: Optional[int] = None

    class Config:
        # Allow extra fields to be ignored
        extra = "ignore"


class LLMSettingsResponse(BaseModel):
    """Schema for LLM settings response (hides API key)."""
    api_url: Optional[str] = None
    model_name: str
    provider: str
    api_key_set: bool
    screenops_api_key_set: bool = False
    serper_api_key_set: bool = False
    small_model_name: Optional[str] = None
    plan_model_name: Optional[str] = None
    max_steps: int = 50
    compaction_auto: bool = False
    compaction_tail_turns: int = 4
    snapshot_enabled: bool = False
    screenops_api_url: Optional[str] = None
    screenops_model: Optional[str] = None
    screenops_image_scale: int = 100
    screenshot_delay_ms: int = 2000
    agent_max_iterations: int = 10
    debug_logging: bool = False
    max_parallel_files: int = 1


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
async def update_llm_settings(settings_data: LLMSettings, current_user: CurrentUser = Depends(get_current_user)):
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
                
                # ScreenOps API key and model — only update if provided
                if settings_data.screenops_api_url is not None:
                    db_settings.screenops_api_url = (settings_data.screenops_api_url or "").strip() or None
                if settings_data.screenops_api_key is not None:
                    db_settings.screenops_api_key = settings_data.screenops_api_key if settings_data.screenops_api_key else None
                if settings_data.screenops_model is not None:
                    db_settings.screenops_model = (settings_data.screenops_model or "").strip() or None
                if settings_data.screenops_image_scale is not None:
                    db_settings.screenops_image_scale = max(25, min(100, int(settings_data.screenops_image_scale)))
                if settings_data.screenshot_delay_ms is not None:
                    db_settings.screenshot_delay_ms = max(0, min(30000, int(settings_data.screenshot_delay_ms)))
                # Serper API key for web search
                if settings_data.serper_api_key is not None:
                    db_settings.serper_api_key = settings_data.serper_api_key if settings_data.serper_api_key else None
                if settings_data.small_model_name is not None:
                    db_settings.small_model_name = (settings_data.small_model_name or "").strip() or None
                if settings_data.plan_model_name is not None:
                    db_settings.plan_model_name = (settings_data.plan_model_name or "").strip() or None
                if settings_data.max_steps is not None:
                    db_settings.max_steps = max(1, min(200, int(settings_data.max_steps)))
                if settings_data.compaction_auto is not None:
                    db_settings.compaction_auto = bool(settings_data.compaction_auto)
                if settings_data.compaction_tail_turns is not None:
                    db_settings.compaction_tail_turns = max(1, min(50, int(settings_data.compaction_tail_turns)))
                if settings_data.snapshot_enabled is not None:
                    db_settings.snapshot_enabled = bool(settings_data.snapshot_enabled)
                if settings_data.agent_max_iterations is not None:
                    db_settings.agent_max_iterations = max(1, min(50, int(settings_data.agent_max_iterations)))
                if settings_data.debug_logging is not None:
                    db_settings.debug_logging = bool(settings_data.debug_logging)
                if settings_data.max_parallel_files is not None:
                    n = max(1, min(32, int(settings_data.max_parallel_files)))
                    db_settings.max_parallel_files = n

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
            
            # Apply the new LLM config to any running OpenCode session right
            # away, so a model/provider/key change takes effect WITHOUT a
            # server or agent restart. OpenCode reads its model + api_key from
            # a per-user config file written when its session starts;
            # reload_for_user() regenerates that file from the settings we just
            # saved and restarts the live instance. No-op for users who have no
            # running OpenCode session (their next cold start reads it anyway).
            try:
                from app.services.opencode_manager import opencode_manager
                for _uid in list(opencode_manager._processes.keys()):
                    try:
                        await opencode_manager.reload_for_user(_uid)
                        print(f"[SETTINGS] Reloaded OpenCode for {_uid} to apply new LLM config", flush=True)
                    except Exception as _re:
                        print(f"[SETTINGS] OpenCode reload failed for {_uid} (non-critical): {_re}", flush=True)
            except Exception as _oc_err:
                print(f"[SETTINGS] OpenCode reload skipped (non-critical): {_oc_err}", flush=True)

            # Build response from the refreshed object
            response = LLMSettingsResponse(
                api_url=db_settings.api_url,
                model_name=db_settings.model_name or app_settings.REASONING_MODEL,
                provider=db_settings.provider or "openai",
                api_key_set=bool(_resolved_openai_api_key(db_settings)),
                screenops_api_key_set=bool(db_settings.screenops_api_key),
                serper_api_key_set=bool(getattr(db_settings, "serper_api_key", None)),
                small_model_name=getattr(db_settings, "small_model_name", None),
                plan_model_name=getattr(db_settings, "plan_model_name", None),
                max_steps=int(getattr(db_settings, "max_steps", 50) or 50),
                compaction_auto=bool(getattr(db_settings, "compaction_auto", False)),
                compaction_tail_turns=int(getattr(db_settings, "compaction_tail_turns", 4) or 4),
                snapshot_enabled=bool(getattr(db_settings, "snapshot_enabled", False)),
                screenops_api_url=getattr(db_settings, "screenops_api_url", None),
                screenops_model=db_settings.screenops_model,
                screenops_image_scale=max(25, min(100, int(getattr(db_settings, "screenops_image_scale", 100) or 100))),
                screenshot_delay_ms=max(0, min(30000, int(getattr(db_settings, "screenshot_delay_ms", 2000) or 2000))),
                agent_max_iterations=int(getattr(db_settings, "agent_max_iterations", 10) or 10),
                debug_logging=bool(getattr(db_settings, "debug_logging", False)),
                max_parallel_files=int(getattr(db_settings, "max_parallel_files", 1) or 1),
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
async def get_llm_settings(session: AsyncSession = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    """Get current LLM settings."""
    print("[GET_SETTINGS] Loading settings from database...", flush=True)
    db_settings = await get_or_create_settings(session)
    
    print(f"[GET_SETTINGS] Loaded: provider={db_settings.provider}, model={db_settings.model_name}, api_url={db_settings.api_url}", flush=True)

    response = LLMSettingsResponse(
        api_url=db_settings.api_url,
        model_name=db_settings.model_name or app_settings.REASONING_MODEL,
        provider=db_settings.provider or "openai",
        api_key_set=bool(_resolved_openai_api_key(db_settings)),
        screenops_api_key_set=bool(db_settings.screenops_api_key),
        serper_api_key_set=bool(getattr(db_settings, "serper_api_key", None)),
        small_model_name=getattr(db_settings, "small_model_name", None),
        plan_model_name=getattr(db_settings, "plan_model_name", None),
        max_steps=int(getattr(db_settings, "max_steps", 50) or 50),
        compaction_auto=bool(getattr(db_settings, "compaction_auto", False)),
        compaction_tail_turns=int(getattr(db_settings, "compaction_tail_turns", 4) or 4),
        snapshot_enabled=bool(getattr(db_settings, "snapshot_enabled", False)),
        screenops_api_url=getattr(db_settings, "screenops_api_url", None),
        screenops_model=db_settings.screenops_model,
        screenops_image_scale=max(25, min(100, int(getattr(db_settings, "screenops_image_scale", 100) or 100))),
        screenshot_delay_ms=max(0, min(30000, int(getattr(db_settings, "screenshot_delay_ms", 2000) or 2000))),
        agent_max_iterations=int(getattr(db_settings, "agent_max_iterations", 10) or 10),
        debug_logging=bool(getattr(db_settings, "debug_logging", False)),
        max_parallel_files=int(getattr(db_settings, "max_parallel_files", 1) or 1),
    )

    print(f"[GET_SETTINGS] Returning: provider={response.provider}, model={response.model_name}, api_url={response.api_url}", flush=True)
    return response


@router.post("/llm/test", response_model=TestConnectionResponse)
async def test_llm_connection(settings_data: LLMSettings, session: AsyncSession = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    """Test connectivity to Chat and ScreenOps APIs."""
    import time
    from openai import AsyncOpenAI
    from anthropic import AsyncAnthropic

    # If API key is a placeholder or empty, use saved settings
    db_settings = None
    if not settings_data.api_key or settings_data.api_key == 'saved-key-placeholder':
        db_settings = await get_or_create_settings(session)
        settings_data.api_key = _resolved_openai_api_key(db_settings)
        if not settings_data.screenops_api_key or settings_data.screenops_api_key == "saved-key-placeholder":
            settings_data.screenops_api_key = db_settings.screenops_api_key
    # Resolve ScreenOps placeholder when chat key was provided in form
    if (not settings_data.screenops_api_key or settings_data.screenops_api_key == "saved-key-placeholder") and settings_data.api_key:
        if db_settings is None:
            db_settings = await get_or_create_settings(session)
        settings_data.screenops_api_key = db_settings.screenops_api_key
    # Resolve ScreenOps URL and model from request, then DB
    if db_settings is None:
        db_settings = await get_or_create_settings(session)
    screenops_api_url = (settings_data.screenops_api_url or getattr(db_settings, "screenops_api_url", None) or "").strip()
    screenops_model = (settings_data.screenops_model or db_settings.screenops_model or "").strip()

    results = {"chat": {}, "screenops": {}}
    
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
    
    # Test ScreenOps coordinate-finder API (OpenAI-compatible: URL + model + key)
    if settings_data.screenops_api_key and (screenops_api_url or screenops_model):
        base_url = (screenops_api_url or "https://api.openai.com/v1").rstrip("/")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        test_url = f"{base_url}/chat/completions"
        try:
            import httpx
            start_time = time.time()
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    test_url,
                    headers={
                        "Authorization": f"Bearer {settings_data.screenops_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": screenops_model or "gpt-4o-mini",
                        "messages": [{"role": "user", "content": "Say OK"}],
                        "max_tokens": 5,
                    },
                )
            latency_ms = int((time.time() - start_time) * 1000)
            if r.is_success:
                results["screenops"] = {
                    "success": True,
                    "message": "✅ ScreenOps: Coordinate finder API OK",
                    "latency_ms": latency_ms,
                }
            else:
                results["screenops"] = {
                    "success": False,
                    "message": f"❌ ScreenOps: {r.status_code} {r.text[:80]}",
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
            "message": "⚠️  ScreenOps: Set URL, model, and API key to test",
            "latency_ms": 0,
        }

    messages = [
        results["chat"].get("message", ""),
        results["screenops"].get("message", ""),
    ]
    total_latency = (
        results["chat"].get("latency_ms", 0)
        + results["screenops"].get("latency_ms", 0)
    )

    return TestConnectionResponse(
        success=results["chat"].get("success", False),
        message="\n".join(messages),
        latency_ms=total_latency,
        model_info=results
    )


_LEGACY_KB_TOOL_NAMES = {"search_knowledge_base", "list_kb_entries", "read_kb_file", "browse_kb_structure"}


@router.patch("/agent-tools")
async def update_agent_tools(
    data: AgentToolsUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Update which agent tools are enabled/disabled. Pass disabled_tools list."""
    # Normalize: legacy KB tool names → knowledge_base
    disabled = list(data.disabled_tools) if data.disabled_tools else []
    normalized = []
    has_legacy_kb = False
    for name in disabled:
        if name in _LEGACY_KB_TOOL_NAMES:
            has_legacy_kb = True
        else:
            normalized.append(name)
    if has_legacy_kb and "knowledge_base" not in normalized:
        normalized.append("knowledge_base")
    normalized = list(dict.fromkeys(normalized))  # preserve order, dedupe

    db_settings = await get_or_create_settings(session)
    db_settings.agent_tools_config = json.dumps({"disabled": normalized})
    await session.flush()
    return {"ok": True, "disabled_tools": normalized}


@router.get("/llm/api-key")
async def get_api_key(session: AsyncSession = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    """Get the current API key (for internal use)."""
    db_settings = await get_or_create_settings(session)
    return {
        "api_key": _resolved_openai_api_key(db_settings)
    }


@router.get("/llm/model")
async def get_model_name(session: AsyncSession = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    """Get the current model name (for internal use)."""
    db_settings = await get_or_create_settings(session)
    return {
        "model_name": db_settings.model_name or app_settings.REASONING_MODEL,
        "provider": db_settings.provider or "openai"
    }


async def get_active_llm_settings(session: AsyncSession, consumer_key: str | None = None) -> dict:
    """
    Get active LLM settings for internal use.

    When GATEWAY_PROVIDER is not "direct", api_key and api_url are overridden
    with the gateway's credentials so all LLM traffic flows through the proxy.
    """
    db_settings = await get_or_create_settings(session)
    agent_tools_config = None
    if getattr(db_settings, "agent_tools_config", None):
        try:
            agent_tools_config = json.loads(db_settings.agent_tools_config)
        except (json.JSONDecodeError, TypeError):
            pass

    api_key = _resolved_openai_api_key(db_settings)
    api_url = db_settings.api_url

    # Override with gateway credentials when a gateway provider is configured
    gateway_provider_name = (app_settings.GATEWAY_PROVIDER or "direct").lower().strip()
    if gateway_provider_name != "direct" and app_settings.GATEWAY_BASE_URL:
        api_url = app_settings.GATEWAY_BASE_URL
        if consumer_key:
            api_key = consumer_key

    return {
        "api_key": api_key,
        "api_url": api_url,
        "model_name": db_settings.model_name or app_settings.REASONING_MODEL,
        "provider": db_settings.provider or "openai",
        "screenops_api_key": db_settings.screenops_api_key,
        "screenops_api_url": getattr(db_settings, "screenops_api_url", None),
        "screenops_model": getattr(db_settings, "screenops_model", None),
        "screenops_image_scale": max(25, min(100, int(getattr(db_settings, "screenops_image_scale", 100) or 100))),
        "screenshot_delay_ms": max(0, min(30000, int(getattr(db_settings, "screenshot_delay_ms", 2000) or 2000))),
        "serper_api_key": getattr(db_settings, "serper_api_key", None),
        "small_model_name": getattr(db_settings, "small_model_name", None),
        "plan_model_name": getattr(db_settings, "plan_model_name", None),
        "max_steps": max(1, min(200, int(getattr(db_settings, "max_steps", 50) or 50))),
        "compaction_auto": bool(getattr(db_settings, "compaction_auto", False)),
        "compaction_tail_turns": max(1, min(50, int(getattr(db_settings, "compaction_tail_turns", 4) or 4))),
        "snapshot_enabled": bool(getattr(db_settings, "snapshot_enabled", False)),
        "agent_tools_config": agent_tools_config,
        "agent_max_iterations": max(1, min(50, int(getattr(db_settings, "agent_max_iterations", 10) or 10))),
        "debug_logging": bool(getattr(db_settings, "debug_logging", False)),
        "max_parallel_files": max(1, min(32, int(getattr(db_settings, "max_parallel_files", 1) or 1))),
    }

