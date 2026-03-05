"""
Screen capture and operation execution — ported from IQWorksAtlas screenops/execution.py.

Uses pyautogui for screenshots and mouse/keyboard automation.  If pyautogui is
not installed the module still imports but functions raise RuntimeError.
"""

import base64
import io
import time

try:
    import pyautogui
    from PIL import Image
except ImportError:
    pyautogui = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]


def capture_screenshot_base64() -> tuple[str, int, int, int, int]:
    """Capture screen → (base64_png, screen_w, screen_h, img_w, img_h)."""
    if not pyautogui or not Image:
        raise RuntimeError("pyautogui and Pillow are required for ScreenOps")
    screen_w, screen_h = pyautogui.size()
    img = pyautogui.screenshot()
    img_w, img_h = img.size
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64, screen_w, screen_h, img_w, img_h


def run_operation(op: dict, screen_width: int, screen_height: int) -> None:
    """Execute one operation (click / write / press)."""
    if not pyautogui:
        raise RuntimeError("pyautogui is required for ScreenOps")

    action = (op.get("operation") or "").lower()

    if action == "click":
        x, y = op.get("x"), op.get("y")
        if x is None or y is None:
            return
        click_type = op.get("click_type", "single")
        button = "right" if click_type == "right" else "left"
        clicks = 2 if click_type == "double" else 1
        x = max(0, min(int(x), screen_width - 1))
        y = max(0, min(int(y), screen_height - 1))
        pyautogui.moveTo(x, y, duration=0.15)
        time.sleep(0.05)
        pyautogui.click(x, y, button=button, clicks=clicks)

    elif action == "write":
        content = op.get("content", "").replace("\\n", "\n")
        pyautogui.write(content, interval=0.02)

    elif action == "press":
        keys = op.get("keys") or []
        if not keys:
            return
        if len(keys) >= 2:
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(keys[0])
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Usage helpers
# ---------------------------------------------------------------------------

def _normalize_usage(u: dict) -> dict:
    if not u or not isinstance(u, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    p = u.get("prompt_tokens") or u.get("input_tokens", 0)
    c = u.get("completion_tokens") or u.get("output_tokens", 0)
    t = u.get("total_tokens") or (p + c)
    return {
        "prompt_tokens": p,
        "completion_tokens": c,
        "total_tokens": t,
        "cache_write_tokens": u.get("cache_write_tokens", 0),
        "cache_read_tokens": u.get("cache_read_tokens", 0),
        "cached_tokens": u.get("cached_tokens", 0),
    }


def merge_usage(a: dict, b: dict) -> dict:
    keys = ["prompt_tokens", "completion_tokens", "total_tokens",
            "cache_write_tokens", "cache_read_tokens", "cached_tokens"]
    return {k: (a.get(k) or 0) + (b.get(k) or 0) for k in keys}
