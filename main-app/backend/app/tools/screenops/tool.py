"""
ScreenOps tool builder for the ReTrace agent.

Ported from IQWorksAtlas screenops/tool.py.  Creates the vision invoker
(using ReTrace's configured chat model via LangChain) and the
Anthropic coordinate finder invoker (using the screenops_api_key from settings).
"""

import base64
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from langchain_core.tools import StructuredTool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.tools.screenops.workflow import run_screen_workflow
from app.tools.screenops.prompts import (
    get_coordinate_finder_prompt,
    COORDINATE_FINDER_CACHE_PADDING,
    VISION_CACHE_PADDING,
)

logger = logging.getLogger("screenops.tool")

_DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "screenops_debug.log"


def _debug_log(tag: str, **kwargs):
    """Append a timestamped line to screenops_debug.log for post-mortem analysis."""
    try:
        ts = datetime.utcnow().isoformat()
        parts = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
        line = f"[{ts}] [{tag}] {parts}\n"
        with open(_DEBUG_LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Vision invoker — uses the ReTrace chat LangChain model
# ---------------------------------------------------------------------------

def _make_vision_invoker(model: Any):
    """Build model_invoker(messages, system_prompt) → (content_str, usage_dict)."""

    def invoker(messages, system_prompt, **kwargs):
        lc_messages = [SystemMessage(content=system_prompt)]
        for m in messages:
            role = m.get("role", "user") if isinstance(m, dict) else getattr(m, "type", "human")
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", m)

            if role == "assistant" or (hasattr(m, "type") and getattr(m, "type", None) == "ai"):
                lc_messages.append(AIMessage(content=content if isinstance(content, str) else str(content)))
                continue

            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            parts.append({"type": "text", "text": part.get("text", "")})
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {})
                            if isinstance(url, str):
                                url = {"url": url}
                            parts.append({"type": "image_url", "image_url": url})
                    elif isinstance(part, str):
                        parts.append({"type": "text", "text": part})
                    else:
                        parts.append(part)
                lc_messages.append(HumanMessage(content=parts))
            else:
                lc_messages.append(HumanMessage(content=content))

        resp = model.invoke(lc_messages, **kwargs)
        text = resp.content if hasattr(resp, "content") else str(resp)

        usage = {}
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            u = resp.usage_metadata
            if isinstance(u, dict):
                usage = {
                    "prompt_tokens": u.get("input_tokens") or u.get("prompt_tokens", 0),
                    "completion_tokens": u.get("output_tokens") or u.get("completion_tokens", 0),
                    "total_tokens": u.get("total_tokens", 0),
                }
            else:
                usage = {
                    "prompt_tokens": getattr(u, "input_tokens", None) or getattr(u, "prompt_tokens", 0),
                    "completion_tokens": getattr(u, "output_tokens", None) or getattr(u, "completion_tokens", 0),
                    "total_tokens": getattr(u, "total_tokens", 0),
                }
        return text, usage

    return invoker


# ---------------------------------------------------------------------------
# Provider detection helpers
# ---------------------------------------------------------------------------

def _get_model_base_url(model: Any) -> str:
    return str(
        getattr(model, "openai_api_base", None)
        or getattr(model, "base_url", "")
        or ""
    )


def _get_model_name(model: Any) -> str:
    return str(
        getattr(model, "model_name", None)
        or getattr(model, "model", "")
        or ""
    )


def _get_model_api_key(model: Any) -> str:
    api_key_raw = (
        getattr(model, "openai_api_key", None)
        or getattr(model, "api_key", None)
    )
    if hasattr(api_key_raw, "get_secret_value"):
        return api_key_raw.get_secret_value()
    return str(api_key_raw or "")


# ---------------------------------------------------------------------------
# x.ai / Grok vision invoker — uses the Responses API (/v1/responses)
# ---------------------------------------------------------------------------

def _is_xai_model(model: Any) -> bool:
    """Return True when *model* targets the x.ai API."""
    return "x.ai" in _get_model_base_url(model)


# ---------------------------------------------------------------------------
# Google Gemini vision invoker — uses Chat Completions via OpenAI SDK
# ---------------------------------------------------------------------------

def _is_google_model(model: Any) -> bool:
    """Return True when *model* targets the Google Gemini API."""
    base_url = _get_model_base_url(model)
    model_name = _get_model_name(model)
    return (
        "generativelanguage.googleapis.com" in base_url
        or "aiplatform.googleapis.com" in base_url
        or model_name.startswith("gemini")
    )


def _make_google_vision_invoker(model: Any):
    """Build a vision invoker for Google Gemini via its OpenAI-compatible endpoint.

    Gemini's ``/v1beta/openai/`` compatibility layer requires every element
    in a multimodal content array to be a typed struct (``{"type": "text", …}``
    or ``{"type": "image_url", …}``).  Bare strings are rejected with
    ``INVALID_ARGUMENT: Value is not a struct``.

    This invoker bypasses LangChain and calls the endpoint directly through
    the OpenAI SDK so we have full control over the wire format.
    """
    import httpx
    from openai import OpenAI

    api_key = _get_model_api_key(model)
    base_url = _get_model_base_url(model) or "https://generativelanguage.googleapis.com/v1beta/openai/"
    model_name = _get_model_name(model)

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=httpx.Timeout(120.0),
    )

    def invoker(messages, system_prompt, **kwargs):
        api_messages: list[dict] = [{"role": "system", "content": system_prompt}]

        for m in messages:
            role = m.get("role", "user") if isinstance(m, dict) else "user"
            content = m.get("content") if isinstance(m, dict) else m

            if role == "assistant":
                api_messages.append({
                    "role": "assistant",
                    "content": content if isinstance(content, str) else str(content),
                })
                continue

            if isinstance(content, list):
                parts: list[dict] = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            parts.append({"type": "text", "text": part.get("text", "")})
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {})
                            if isinstance(url, str):
                                url = {"url": url}
                            parts.append({"type": "image_url", "image_url": url})
                        else:
                            parts.append(part)
                    elif isinstance(part, str):
                        parts.append({"type": "text", "text": part})
                api_messages.append({"role": "user", "content": parts})
            else:
                api_messages.append({
                    "role": "user",
                    "content": content if isinstance(content, str) else str(content),
                })

        response = client.chat.completions.create(
            model=model_name,
            messages=api_messages,
        )

        text = ""
        if response.choices:
            text = response.choices[0].message.content or ""

        usage: dict = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }

        return text, usage

    return invoker


def _make_xai_vision_invoker(model: Any):
    """Build a vision invoker for x.ai models.

    x.ai's grok-4 models require the **Responses API** (``/v1/responses``)
    with ``input_image`` / ``input_text`` content-block types rather than
    the Chat Completions API with ``image_url`` / ``text``.
    """
    import httpx
    from openai import OpenAI

    api_key = _get_model_api_key(model)
    base_url = _get_model_base_url(model) or "https://api.x.ai/v1"
    model_name = _get_model_name(model)

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=httpx.Timeout(300.0),
    )

    def invoker(messages, system_prompt, **kwargs):
        input_messages: list[dict] = []
        for m in messages:
            role = m.get("role", "user") if isinstance(m, dict) else "user"
            content = m.get("content") if isinstance(m, dict) else m

            if role == "assistant":
                input_messages.append({
                    "role": "assistant",
                    "content": content if isinstance(content, str) else str(content),
                })
                continue

            if isinstance(content, list):
                parts: list[dict] = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            parts.append({
                                "type": "input_text",
                                "text": part.get("text", ""),
                            })
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {})
                            if isinstance(url, dict):
                                url = url.get("url", "")
                            parts.append({
                                "type": "input_image",
                                "image_url": url,
                                "detail": "high",
                            })
                    elif isinstance(part, str):
                        parts.append({"type": "input_text", "text": part})
                input_messages.append({"role": "user", "content": parts})
            else:
                input_messages.append({
                    "role": "user",
                    "content": content if isinstance(content, str) else str(content),
                })

        response = client.responses.create(
            model=model_name,
            instructions=system_prompt,
            input=input_messages,
        )

        text = getattr(response, "output_text", "") or ""

        usage: dict = {}
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            inp = getattr(u, "input_tokens", 0) or 0
            out = getattr(u, "output_tokens", 0) or 0
            usage = {
                "prompt_tokens": inp,
                "completion_tokens": out,
                "total_tokens": inp + out,
            }

        return text, usage

    return invoker


# ---------------------------------------------------------------------------
# Coordinate finder invoker — always uses Anthropic Claude via direct API
# ---------------------------------------------------------------------------

ANTHROPIC_COORD_MODEL = "claude-sonnet-4-5-20250929"

_MAX_IMAGE_LONGEST_SIDE = 1280


def _resize_screenshot_for_coord_finder(screenshot_base64: str) -> tuple[str, int, int]:
    """Resize a screenshot so the longest side is <= _MAX_IMAGE_LONGEST_SIDE.

    Anthropic silently resizes images to ~1568px max before Claude sees them,
    but the model then can't accurately place pixel coords in the original
    (much larger) coordinate space.  By resizing ourselves we guarantee the
    model sees exactly the dimensions we state in the prompt.

    Returns (resized_b64, resized_w, resized_h).
    """
    from PIL import Image as PILImage

    raw = base64.b64decode(screenshot_base64)
    img = PILImage.open(io.BytesIO(raw))
    w, h = img.size

    longest = max(w, h)
    if longest <= _MAX_IMAGE_LONGEST_SIDE:
        return screenshot_base64, w, h

    scale = _MAX_IMAGE_LONGEST_SIDE / longest
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    img = img.resize((new_w, new_h), PILImage.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    resized_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return resized_b64, new_w, new_h


def _make_anthropic_coordinate_finder_invoker(api_key: str, model: Optional[str] = None):
    """Build coordinate_finder_invoker using Anthropic API with native image format."""
    coord_model = (model or "").strip() or ANTHROPIC_COORD_MODEL

    def invoker(screenshot_base64, target_description, screen_width, screen_height, image_width, image_height, **kwargs):
        import anthropic

        # Resize the screenshot so Claude sees the exact dimensions we state
        resized_b64, resized_w, resized_h = _resize_screenshot_for_coord_finder(screenshot_base64)

        _debug_log("coord_finder",
                   target=target_description,
                   event="resize",
                   original_image=f"{image_width}x{image_height}",
                   resized_image=f"{resized_w}x{resized_h}",
                   screen=f"{screen_width}x{screen_height}")

        prompt = get_coordinate_finder_prompt(
            target_description=target_description,
            screen_width=resized_w,
            screen_height=resized_h,
            image_width=resized_w,
            image_height=resized_h,
            match_agent_prompt=True,
        )

        client = anthropic.Anthropic(api_key=api_key)

        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": resized_b64},
            },
            {"type": "text", "text": f"Find: {target_description}"},
        ]

        system_text = prompt + COORDINATE_FINDER_CACHE_PADDING
        system_blocks = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

        response = client.messages.create(
            model=coord_model,
            system=system_blocks,
            messages=[{"role": "user", "content": content}],
            max_tokens=150,
        )

        text = ""
        if response.content:
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "")
                    break

        usage = {}
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            inp = getattr(u, "input_tokens", 0) or 0
            out = getattr(u, "output_tokens", 0) or 0
            usage = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}

        # Parse coordinates (in resized-image space)
        coords = {}
        clean_text = text.strip()
        if clean_text.startswith("```"):
            clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text)
            clean_text = re.sub(r"\s*```\s*$", "", clean_text)
            clean_text = clean_text.strip()
        json_match = re.search(r'\{[^}]+\}', clean_text)
        if json_match:
            try:
                coords = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass
        if not coords:
            m = re.search(r'\{\s*["\']x["\']\s*:\s*(\d+)\s*,\s*["\']y["\']\s*:\s*(\d+)\s*\}', text)
            if m:
                coords = {"x": int(m.group(1)), "y": int(m.group(2))}

        _debug_log("coord_finder",
                   target=target_description,
                   raw_text=text,
                   resized_image=f"{resized_w}x{resized_h}",
                   screen=f"{screen_width}x{screen_height}",
                   raw_parsed=coords)

        # Scale coordinates from resized-image space → screen space
        if coords.get("x") is not None and coords.get("y") is not None:
            cx, cy = float(coords["x"]), float(coords["y"])

            # Safeguard: if model returned normalized floats (0.0–1.0), convert
            if 0.0 < cx <= 1.0 and 0.0 < cy <= 1.0:
                px = int(round(cx * screen_width))
                py = int(round(cy * screen_height))
                _debug_log("coord_finder",
                           target=target_description,
                           normalized_to_screen=(cx, cy, px, py))
                coords = {"x": px, "y": py}
            else:
                # Coords are in resized-image pixel space → scale to screen
                scale_x = screen_width / resized_w
                scale_y = screen_height / resized_h
                px = int(round(cx * scale_x))
                py = int(round(cy * scale_y))
                _debug_log("coord_finder",
                           target=target_description,
                           resized_to_screen=(cx, cy, resized_w, resized_h, scale_x, scale_y, px, py))
                coords = {"x": px, "y": py}

            coords["x"] = max(0, min(coords["x"], screen_width - 1))
            coords["y"] = max(0, min(coords["y"], screen_height - 1))

        _debug_log("coord_finder",
                   target=target_description,
                   final_screen_coords=coords)

        return coords, usage

    return invoker


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_screenops_tool(
    chat_model: Any,
    screenops_api_key: str,
    screenops_model: Optional[str] = None,
) -> Optional[StructuredTool]:
    """Build the ScreenOps StructuredTool.

    *chat_model* — LangChain ChatModel for the vision step.
    *screenops_api_key* — Anthropic API key for the coordinate finder.
    *screenops_model* — Optional Anthropic model for the coordinate finder (default from ANTHROPIC_COORD_MODEL).
    """
    if not chat_model or not screenops_api_key:
        return None

    if _is_xai_model(chat_model):
        model_invoker = _make_xai_vision_invoker(chat_model)
        logger.info("Using x.ai Responses API for ScreenOps vision")
    elif _is_google_model(chat_model):
        model_invoker = _make_google_vision_invoker(chat_model)
        logger.info("Using Google Gemini API for ScreenOps vision")
    else:
        model_invoker = _make_vision_invoker(chat_model)

    coordinate_finder_invoker = _make_anthropic_coordinate_finder_invoker(screenops_api_key, screenops_model)

    def run_screenops(task: str) -> str:
        out = run_screen_workflow(
            task=task,
            model_invoker=model_invoker,
            coordinate_finder_invoker=coordinate_finder_invoker,
            max_steps=10,
        )
        return json.dumps({
            "result": out.get("result", ""),
            "iterations": out.get("iterations"),
        })

    return StructuredTool.from_function(
        name="screenops",
        description=(
            "Perform screen-based computer operations: open apps, click UI elements, "
            "type text, use keyboard shortcuts. Pass the user's request verbatim as "
            "the task argument. Requires a visible desktop."
        ),
        func=run_screenops,
    )
