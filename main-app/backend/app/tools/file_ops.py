"""
File operation tools — read and write files.

Ported from IQWorksAtlas langgraph_codeact/community_tools.py
get_file_management_tools(). Simplified to plain functions.
"""

import os
from pathlib import Path

import structlog

logger = structlog.get_logger()

MAX_READ_SIZE = 500_000  # 500 KB


def read_file(file_path: str) -> str:
    """Read and return the contents of a file.

    Supports relative paths (resolved from the terminal's current
    working directory) and absolute paths.  Returns an error string
    if the file doesn't exist or can't be read.
    """
    try:
        # Resolve relative to terminal CWD
        from app.tools.terminal import _cwd
        file_path = os.path.expanduser(file_path)
        p = Path(file_path)
        if not p.is_absolute():
            p = Path(_cwd) / p
        p = p.resolve()

        if not p.exists():
            return f"Error: file not found — {p}"
        if not p.is_file():
            return f"Error: not a file — {p}"

        size = p.stat().st_size
        if size > MAX_READ_SIZE:
            text = p.read_text(encoding="utf-8", errors="replace")[:MAX_READ_SIZE]
            return text + f"\n\n[File truncated — showing first {MAX_READ_SIZE} of {size} bytes]"

        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.error("read_file_error", path=file_path, error=str(exc))
        return f"Error reading file: {exc}"


def write_file(file_path: str, text: str) -> str:
    """Write *text* to a file (creates parent directories as needed).

    Returns a confirmation message or an error string.
    """
    try:
        from app.tools.terminal import _cwd
        file_path = os.path.expanduser(file_path)
        p = Path(file_path)
        if not p.is_absolute():
            p = Path(_cwd) / p
        p = p.resolve()

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return f"Successfully wrote {len(text)} characters to {p}"
    except Exception as exc:
        logger.error("write_file_error", path=file_path, error=str(exc))
        return f"Error writing file: {exc}"
