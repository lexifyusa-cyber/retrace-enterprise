"""
Shared coordinate-finder LLM-call logging.

Both the pre-OC LangChain `get_all_tools` path and the post-OC inline
`browser_manager.vision_click` path need to record every screenops VLM
call (the vision model that resolves a description like "green Download
button" to pixel coordinates) into the `llm_logs` table so the LLM Logs
page can show the agent's vision usage alongside its main-LLM calls.

The `_make_coordinate_finder_invoker` factory in `tool.py` accepts an
optional `log_callback`. This module is the single place that builds one
— closure variables (user_id, conversation_id, model, provider, loop)
are passed in as explicit parameters so both call sites can share it.
"""

import asyncio
import json
from typing import Any, Callable, Optional
from uuid import uuid4

import structlog

logger = structlog.get_logger()


def infer_provider_from_url(api_url: Optional[str]) -> str:
    """Return "anthropic" or "openai" based on the configured screenops URL.

    The screenops endpoint can be either an OpenAI-compatible API or
    Anthropic's Messages API; that choice determines what we record in
    `llm_logs.model_provider`. Empty / missing URL defaults to "openai"
    since that's the historical default for the coord finder.
    """
    return "anthropic" if "anthropic" in (api_url or "").lower() else "openai"


def make_coord_log_callback(
    user_id: Optional[str],
    conversation_id: Optional[str],
    provider: str,
    default_model: str,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Callable[..., None]:
    """Build a `log_callback` for `_make_coordinate_finder_invoker`.

    The returned callable receives the kwargs the invoker passes
    (`messages`, `response_text`, `usage`, `latency_ms`, `model_name`,
    `error`) and writes one row to `llm_logs`.

    The coord-finder invoker runs synchronously on a worker thread (see
    `asyncio.to_thread(...)` at the call sites), so the DB write has to
    cross back to the FastAPI event loop via `run_coroutine_threadsafe`.
    If no loop is supplied, falls back to `asyncio.run` which works when
    the callback happens to fire on the main thread.

    Args:
        user_id: Owning user. May be None for standalone sessions.
        conversation_id: ReTrace conversation id this call belongs to.
        provider: "openai" or "anthropic" — written to `model_provider`.
        default_model: Model id to use when the invoker doesn't provide
            one in the callback kwargs.
        loop: FastAPI event loop, captured at startup. Required when the
            callback fires from a worker thread (the usual case).
    """
    def _cb(messages, response_text, usage, latency_ms, model_name, error):
        req_json = json.dumps(messages, default=str)
        u = usage or {}
        log_row = {
            "log_id": str(uuid4()),
            "user_id": user_id,
            "conversation_id": conversation_id,
            "iteration": 0,
            "request_messages_json": req_json,
            "response_content": (response_text or "")[:50000],
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "reasoning_tokens": 0,
            "model_provider": provider,
            "model_name": model_name or default_model,
            "latency_ms": latency_ms,
            "error": error,
        }

        async def _write():
            from app.db.database import async_session_maker
            from app.models.llm_log import LLMLog
            async with async_session_maker() as db:
                db.add(LLMLog(**log_row))
                await db.commit()

        def _on_done(fut):
            try:
                fut.result()
            except Exception as e:
                logger.error("coord_log_callback_write_failed", error=str(e))

        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(_write(), loop)
                future.add_done_callback(_on_done)
                return
            except Exception as e:
                logger.error("coord_log_callback_schedule_failed", error=str(e))
        try:
            asyncio.run(_write())
        except Exception as e:
            logger.error("coord_log_callback_run_failed", error=str(e))

    return _cb
