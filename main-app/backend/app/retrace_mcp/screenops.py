"""
ReTrace ScreenOps MCP Server — screen-based computer automation.

Tools:
  - screenshot: Capture a screenshot of the desktop
  - click: Click at screen coordinates
  - type_text: Type text on the screen
  - press_key: Press a keyboard key or key combination
  - mouse_move: Move the mouse to coordinates
"""

import asyncio
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("retrace-screenops")

_SCREENOPS_API_KEY = os.environ.get("RETRACE_SCREENOPS_API_KEY", "")
_SCREENOPS_API_URL = os.environ.get("RETRACE_SCREENOPS_API_URL", "")
_SCREENOPS_MODEL = os.environ.get("RETRACE_SCREENOPS_MODEL", "")


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=60)
    else:
        return asyncio.run(coro)


@mcp.tool()
def screenshot() -> str:
    """Take a screenshot of the current desktop. Returns base64-encoded PNG."""
    from app.tools.screenops.execution import take_screenshot_base64

    try:
        result = take_screenshot_base64()
        if result:
            return result
        return "Error: Screenshot capture failed. Check if screenops dependencies are installed."
    except Exception as e:
        return f"Error taking screenshot: {e}"


@mcp.tool()
def click(x: int, y: int, button: str = "left") -> str:
    """Click at the specified screen coordinates.

    Args:
        x: X coordinate on screen
        y: Y coordinate on screen
        button: Mouse button - 'left', 'right', or 'middle'
    """
    try:
        import pyautogui
        pyautogui.click(x=x, y=y, button=button)
        return f"Clicked at ({x}, {y}) with {button} button"
    except Exception as e:
        return f"Error clicking: {e}"


@mcp.tool()
def type_text(text: str, interval: float = 0.02) -> str:
    """Type text on the screen at the current cursor position.

    Args:
        text: Text to type
        interval: Delay between keystrokes in seconds
    """
    try:
        import pyautogui
        pyautogui.typewrite(text, interval=interval)
        return f"Typed: {text[:50]}{'...' if len(text) > 50 else ''}"
    except Exception as e:
        return f"Error typing: {e}"


@mcp.tool()
def press_key(key: str) -> str:
    """Press a keyboard key or key combination.

    Args:
        key: Key or combination (e.g. 'enter', 'ctrl+c', 'alt+tab', 'command+s')
    """
    try:
        import pyautogui
        keys = key.split("+")
        if len(keys) > 1:
            for k in keys[:-1]:
                pyautogui.keyDown(k.strip())
            pyautogui.press(keys[-1].strip())
            for k in reversed(keys[:-1]):
                pyautogui.keyUp(k.strip())
        else:
            pyautogui.press(key)
        return f"Pressed: {key}"
    except Exception as e:
        return f"Error pressing key: {e}"


@mcp.tool()
def mouse_move(x: int, y: int) -> str:
    """Move the mouse to the specified screen coordinates.

    Args:
        x: X coordinate
        y: Y coordinate
    """
    try:
        import pyautogui
        pyautogui.moveTo(x=x, y=y)
        return f"Moved mouse to ({x}, {y})"
    except Exception as e:
        return f"Error moving mouse: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
