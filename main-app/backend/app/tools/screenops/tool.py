"""
ScreenOps tool builder for the ReTrace agent.

Ported from IQWorksAtlas screenops/tool.py.  Creates the vision invoker
(using ReTrace's configured chat model via LangChain) and a configurable
coordinate finder invoker (URL + model + API key, OpenAI-compatible API).
"""

import base64
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

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
# Coordinate finder invoker — OpenAI-compatible API (URL + model + API key)
# ---------------------------------------------------------------------------

_DEFAULT_COORD_API_URL = "https://api.openai.com/v1"
_MAX_IMAGE_LONGEST_SIDE = 1280


def _resize_screenshot_for_coord_finder(screenshot_base64: str) -> tuple[str, int, int]:
    """Resize a screenshot so the longest side is <= _MAX_IMAGE_LONGEST_SIDE.

    Ensures the model sees exactly the dimensions we state in the prompt
    for accurate pixel coordinates.

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


def _make_coordinate_finder_invoker(
    api_url: str, model: str, api_key: str
) -> Callable[..., Tuple[dict, dict]]:
    """Build coordinate_finder_invoker using an OpenAI-compatible API (URL + model + key)."""
    base_url = (api_url or _DEFAULT_COORD_API_URL).strip().rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    coord_model = (model or "gpt-4o-mini").strip()
    endpoint = f"{base_url}/chat/completions"

    def invoker(screenshot_base64, target_description, screen_width, screen_height, image_width, image_height, **kwargs):
        import httpx

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
        system_text = prompt + COORDINATE_FINDER_CACHE_PADDING

        payload = {
            "model": coord_model,
            "messages": [
                {"role": "system", "content": system_text},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{resized_b64}"},
                        },
                        {"type": "text", "text": f"Find: {target_description}"},
                    ],
                },
            ],
            "max_tokens": 150,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                r = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("coord_finder_request_failed", endpoint=endpoint, error=str(e))
            return {}, {}

        text = ""
        usage = {}
        choice = (data.get("choices") or [None])[0]
        if choice and isinstance(choice.get("message"), dict):
            text = (choice["message"].get("content") or "").strip()
        if data.get("usage"):
            u = data["usage"]
            usage = {
                "prompt_tokens": u.get("input_tokens", u.get("prompt_tokens", 0)),
                "completion_tokens": u.get("output_tokens", u.get("completion_tokens", 0)),
                "total_tokens": u.get("total_tokens", 0),
            }
            usage["total_tokens"] = usage["total_tokens"] or usage["prompt_tokens"] + usage["completion_tokens"]

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

        if coords.get("x") is not None and coords.get("y") is not None:
            cx, cy = float(coords["x"]), float(coords["y"])
            if 0.0 < cx <= 1.0 and 0.0 < cy <= 1.0:
                px = int(round(cx * screen_width))
                py = int(round(cy * screen_height))
                _debug_log("coord_finder", target=target_description, normalized_to_screen=(cx, cy, px, py))
                coords = {"x": px, "y": py}
            else:
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

        _debug_log("coord_finder", target=target_description, final_screen_coords=coords)
        return coords, usage

    return invoker


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_screenops_tool(
    chat_model: Any,
    screenops_api_key: str = "",
    screenops_model: Optional[str] = None,
    screenops_api_url: Optional[str] = None,
    screenops_mouse_timeout: int = 30,
) -> Optional[StructuredTool]:
    """Build the ScreenOps StructuredTool.

    *chat_model* — LangChain ChatModel for the vision step (required).
    *screenops_api_key* — API key for the coordinate finder (empty = keyboard-only mode).
    *screenops_model* — Model name for the coordinate finder.
    *screenops_api_url* — Base URL for the coordinate finder API (OpenAI-compatible).
    *screenops_mouse_timeout* — Seconds to wait for manual click in keyboard-only mode.
    """
    if not chat_model:
        return None

    if _is_xai_model(chat_model):
        model_invoker = _make_xai_vision_invoker(chat_model)
        logger.info("Using x.ai Responses API for ScreenOps vision")
    elif _is_google_model(chat_model):
        model_invoker = _make_google_vision_invoker(chat_model)
        logger.info("Using Google Gemini API for ScreenOps vision")
    else:
        model_invoker = _make_vision_invoker(chat_model)

    keyboard_only = not bool(screenops_api_key)
    coordinate_finder_invoker = None
    if not keyboard_only:
        api_url = (screenops_api_url or _DEFAULT_COORD_API_URL).strip()
        model_name = (screenops_model or "gpt-4o-mini").strip()
        coordinate_finder_invoker = _make_coordinate_finder_invoker(api_url, model_name, screenops_api_key)

    if keyboard_only:
        logger.info("ScreenOps: keyboard-only mode (no coordinate finder API configured)")

    mouse_timeout = max(5, min(120, screenops_mouse_timeout))

    def run_screenops(task: str) -> str:
        out = run_screen_workflow(
            task=task,
            model_invoker=model_invoker,
            coordinate_finder_invoker=coordinate_finder_invoker,
            keyboard_only=keyboard_only,
            mouse_timeout=mouse_timeout,
            max_steps=10,
        )
        return json.dumps({
            "result": out.get("result", ""),
            "iterations": out.get("iterations"),
        })

    desc = (
        "Perform screen-based computer operations: open apps, type text, "
        "use keyboard shortcuts. Pass the user's request verbatim as the "
        "task argument. Requires a visible desktop."
    )
    if keyboard_only:
        desc += (
            " Running in keyboard-only mode — prefer keyboard shortcuts "
            "(Tab, Enter, arrow keys, Cmd/Ctrl+...) over mouse clicks."
        )

    return StructuredTool.from_function(
        name="screenops",
        description=desc,
        func=run_screenops,
    )
