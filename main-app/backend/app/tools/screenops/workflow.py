"""
ScreenOps agentic workflow — ported from IQWorksAtlas screenops/workflow.py.

Loop: screenshot → vision model → parse operations → coordinate finder → execute.
"""

import base64
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_SCREENOPS_SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "screenops_screenshots"
_DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "screenops_debug.log"

from app.tools.screenops.prompts import (
    get_system_prompt_default_v2,
    get_user_prompt_first,
    get_user_prompt_next,
)
from app.tools.screenops.execution import (
    capture_screenshot_base64,
    run_operation,
    _normalize_usage,
    merge_usage,
)

logger = logging.getLogger("screenops.workflow")


def _debug_log(tag: str, **kwargs):
    """Append a timestamped line to screenops_debug.log."""
    try:
        ts = datetime.utcnow().isoformat()
        parts = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
        line = f"[{ts}] [{tag}] {parts}\n"
        with open(_DEBUG_LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass


def _empty_usage():
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "cached_tokens": 0}


# ---------------------------------------------------------------------------
# JSON parsing helpers (ported from IQWorksAtlas)
# ---------------------------------------------------------------------------

def _clean_json(content: str) -> str:
    if not content:
        return ""
    content = content.strip()
    if content.startswith("```json"):
        content = content[len("```json"):].strip()
    elif content.startswith("```"):
        content = content[len("```"):].strip()
    if content.endswith("```"):
        content = content[:-3].strip()
    return content


def _normalize_ops_list(candidates: Any) -> list[dict]:
    if not candidates:
        return []
    if isinstance(candidates, dict):
        if candidates.get("operation"):
            return [candidates]
        if "actions" in candidates and isinstance(candidates["actions"], list):
            return _normalize_ops_list(candidates["actions"])
        return []
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict) and item.get("operation")]
    return []


def _parse_operations(text: str) -> list[dict]:
    text = _clean_json(text or "")
    # Try JSON array
    m = re.search(r"\[\s*\{[\s\S]*\}\s*\]", text)
    if m:
        try:
            return _normalize_ops_list(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    # Try full text
    try:
        return _normalize_ops_list(json.loads(text))
    except json.JSONDecodeError:
        pass
    # Single object
    s = re.search(r"\{\s*[\s\S]*\}", text)
    if s:
        try:
            return _normalize_ops_list(json.loads(s.group(0)))
        except json.JSONDecodeError:
            pass
    return []


def _extract_fallback(text: str) -> list[dict]:
    """Extract operation from malformed prose (last resort)."""
    if not text:
        return []
    op: dict[str, Any] = {}
    mo = re.search(r'"operation"\s*:\s*["\']?(\w+)["\']?', text, re.IGNORECASE)
    if not mo:
        return []
    op["operation"] = mo.group(1).lower()
    mt = re.search(r'"target_description"\s*:\s*["\']([^"\']*)["\']', text, re.IGNORECASE)
    if mt:
        op["target_description"] = mt.group(1)
    mc = re.search(r'"content"\s*:\s*["\']([^"\']*)["\']', text, re.IGNORECASE)
    if mc:
        op["content"] = mc.group(1)
    md = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.IGNORECASE | re.DOTALL)
    if md:
        op["description"] = md.group(1).replace('\\"', '"').replace('\\n', '\n')
    mk = re.search(r'"keys"\s*:\s*\[\s*([^\]]+)\s*\]', text, re.IGNORECASE)
    if mk:
        op["keys"] = re.findall(r'["\']([^"\']+)["\']', mk.group(1))
    mx = re.search(r'"x"\s*:\s*(\d+)', text)
    my = re.search(r'"y"\s*:\s*(\d+)', text)
    if mx:
        op["x"] = int(mx.group(1))
    if my:
        op["y"] = int(my.group(1))
    ms = re.search(r'"summary"\s*:\s*["\']([^"\']*)["\']', text, re.IGNORECASE)
    if ms:
        op["summary"] = ms.group(1)
    mcr = re.search(r'"chat_response"\s*:\s*["\']([^"\']*)["\']', text, re.IGNORECASE)
    if mcr:
        op["chat_response"] = mcr.group(1)
    return [op]


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run_screen_workflow(
    task: str,
    model_invoker: Callable[..., tuple[str, dict]],
    coordinate_finder_invoker: Callable[..., tuple[dict, dict]],
    max_steps: int = 10,
) -> dict[str, Any]:
    """Run the ScreenOps agentic loop.

    Returns ``{"result": str, "token_usage": {"main": {}, "sub_sonnet": {}}, "iterations": int}``.
    """
    main_usage = _empty_usage()
    sub_usage = _empty_usage()
    result_text = ""
    observed_description = ""
    step = 0

    try:
        system_prompt = get_system_prompt_default_v2(task)
        messages: list[dict] = []
        _debug_log("workflow", event="start", task=task, max_steps=max_steps)

        while step < max_steps:
            step += 1

            # Screenshot
            try:
                b64, scr_w, scr_h, img_w, img_h = capture_screenshot_base64()
                _debug_log("workflow", event="screenshot", step=step, screen=f"{scr_w}x{scr_h}", image=f"{img_w}x{img_h}")
            except Exception as e:
                _debug_log("workflow", event="screenshot_error", step=step, error=str(e))
                result_text = f"ScreenOps error: {e}"
                break

            # Save screenshot to project folder (same image sent to the vision model)
            try:
                _SCREENOPS_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
                path = _SCREENOPS_SCREENSHOTS_DIR / f"screenops_{ts}_step{step}.png"
                path.write_bytes(base64.b64decode(b64))
                logger.info("Saved screenshot to %s", path)
            except Exception as e:
                logger.warning("Could not save screenshot: %s", e)

            prompt_text = get_user_prompt_first(task) if step == 1 else get_user_prompt_next()
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            })

            # Vision call
            try:
                _debug_log("workflow", event="vision_call_start", step=step)
                content, raw_usage = model_invoker(messages=messages, system_prompt=system_prompt)
            except Exception as e:
                _debug_log("workflow", event="vision_call_error", step=step, error=str(e))
                logger.error("ScreenOps vision call failed: %s", e, exc_info=True)
                result_text = f"ScreenOps vision call failed: {e}"
                break

            main_usage = merge_usage(main_usage, _normalize_usage(raw_usage))
            content_str = content if isinstance(content, str) else str(content)
            messages.append({"role": "assistant", "content": content_str})
            _debug_log("workflow", event="vision_response", step=step, response=content_str[:500])

            # Parse operations
            ops = _parse_operations(content_str) or _extract_fallback(content_str)
            logger.info("Step %d model raw response: %s", step, content_str[:500])
            logger.info("Step %d parsed ops: %s", step, ops)
            _debug_log("workflow", event="parsed_ops", step=step, ops=ops)
            if not ops:
                _debug_log("workflow", event="no_ops_parsed", step=step)
                result_text = "ScreenOps could not parse a valid action from the model."
                break

            for op in ops:
                action = (op.get("operation") or "").lower()
                logger.info("Executing op: %s (click_type=%s, target=%s)", action, op.get("click_type"), op.get("target_description"))

                if action == "observe":
                    desc = op.get("description") or op.get("content") or ""
                    if desc:
                        observed_description += ("\n\n" if observed_description else "") + desc
                    logger.info("Observe captured %d chars (total %d)", len(desc), len(observed_description))
                    _debug_log("workflow", event="observe", step=step, desc_len=len(desc))
                    continue

                if action == "done":
                    chat_resp = op.get("chat_response") or op.get("summary") or ""
                    if observed_description:
                        result_text = observed_description
                        if chat_resp:
                            result_text += "\n\n" + chat_resp
                    else:
                        result_text = chat_resp or "Task completed."
                    _debug_log("workflow", event="done", step=step, result_len=len(result_text), had_observe=bool(observed_description))
                    return {"result": result_text, "token_usage": {"main": main_usage, "sub_sonnet": sub_usage}, "iterations": step}

                # Click — resolve coordinates via coordinate finder when we have a target description.
                # Always use the coordinate finder for target_description; ignore any x,y from the vision model.
                if action == "click" and op.get("target_description"):
                    target = op["target_description"]
                    op.pop("x", None)
                    op.pop("y", None)
                    fallback_x = max(1, min(scr_w - 2, scr_w // 2))
                    fallback_y = max(1, min(scr_h - 2, scr_h // 2))

                    try:
                        coords, coord_usage = coordinate_finder_invoker(
                            screenshot_base64=b64,
                            target_description=target,
                            screen_width=scr_w,
                            screen_height=scr_h,
                            image_width=img_w,
                            image_height=img_h,
                        )
                        sub_usage = merge_usage(sub_usage, _normalize_usage(coord_usage))
                        logger.info("Coord finder returned screen-space coords: %s for screen %dx%d", coords, scr_w, scr_h)

                        if isinstance(coords, dict) and coords.get("x") is not None and coords.get("y") is not None:
                            xi, yi = int(coords["x"]), int(coords["y"])
                            if xi == 0 and yi == 0:
                                logger.warning("Coord finder returned (0,0), using fallback center")
                                op["x"], op["y"] = fallback_x, fallback_y
                            else:
                                op["x"] = max(0, min(xi, scr_w - 1))
                                op["y"] = max(0, min(yi, scr_h - 1))
                        else:
                            logger.warning("Coord finder returned no valid coords: %s, using fallback", coords)
                            op["x"], op["y"] = fallback_x, fallback_y
                    except Exception as e:
                        logger.error("Coord finder exception: %s", e, exc_info=True)
                        op["x"], op["y"] = fallback_x, fallback_y

                exec_summary = {k: v for k, v in op.items() if k != "target_description" or len(str(v)) < 200}
                _debug_log("workflow", event="execute_op", step=step, op=exec_summary)
                logger.info("Final op before execution: %s", exec_summary)
                run_operation(op, scr_w, scr_h)

            time.sleep(3)  # Allow UI to settle

        if not result_text:
            if observed_description:
                result_text = observed_description
            else:
                result_text = f"ScreenOps completed {max_steps} steps without 'done'."
            _debug_log("workflow", event="max_steps_reached", steps=step, result=result_text[:300])

    except Exception as e:
        logger.error("ScreenOps workflow error: %s", e, exc_info=True)
        _debug_log("workflow", event="error", error=str(e))
        result_text = f"ScreenOps error: {e}"

    _debug_log("workflow", event="finished", steps=step, result=result_text[:300])
    return {"result": result_text, "token_usage": {"main": main_usage, "sub_sonnet": sub_usage}, "iterations": step}
