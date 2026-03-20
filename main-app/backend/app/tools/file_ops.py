"""
File operation tools — read, write, and download files.

Ported from IQWorksAtlas langgraph_codeact/community_tools.py
get_file_management_tools(). Simplified to plain functions.
"""

import os
from pathlib import Path

import structlog

logger = structlog.get_logger()

MAX_READ_SIZE = 500_000  # 500 KB
MAX_DOWNLOAD_SIZE = 512 * 1024 * 1024  # 512 MB


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


def delete_file(file_path: str) -> str:
    """Delete a file at the given path.

    Refuses to delete directories.  Returns a confirmation message or
    an error string.
    """
    try:
        from app.tools.terminal import _cwd
        file_path = os.path.expanduser(file_path)
        p = Path(file_path)
        if not p.is_absolute():
            p = Path(_cwd) / p
        p = p.resolve()

        if not p.exists():
            return f"Error: file not found — {p}"
        if p.is_dir():
            return f"Error: cannot delete a directory — {p}. Use terminal('rm -r ...') instead."
        if not p.is_file():
            return f"Error: not a regular file — {p}"

        size = p.stat().st_size
        p.unlink()
        return f"Deleted {p} ({size} bytes)"
    except PermissionError:
        return f"Error: permission denied — {p}"
    except Exception as exc:
        logger.error("delete_file_error", path=file_path, error=str(exc))
        return f"Error deleting file: {exc}"


def download_file(url: str, file_path: str) -> str:
    """Download a file from a URL to the local filesystem.

    Supports relative paths (resolved from the terminal's current working
    directory) and absolute paths. Follows redirects. Limits download size
    to avoid exhausting memory (default 512 MB). Returns a confirmation
    message or an error string.
    """
    try:
        import httpx
        from app.tools.terminal import _cwd

        file_path = os.path.expanduser(file_path)
        p = Path(file_path)
        if not p.is_absolute():
            p = Path(_cwd) / p
        p = p.resolve()

        p.parent.mkdir(parents=True, exist_ok=True)

        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=60.0,
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_DOWNLOAD_SIZE:
                return f"Error: file too large ({int(content_length)} bytes). Max allowed is {MAX_DOWNLOAD_SIZE} bytes."
            total = 0
            with open(p, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_SIZE:
                        f.close()
                        p.unlink(missing_ok=True)
                        return f"Error: download exceeded max size ({MAX_DOWNLOAD_SIZE} bytes)."
                    f.write(chunk)

        size = p.stat().st_size
        return f"Successfully downloaded {size} bytes to {p}"
    except httpx.HTTPStatusError as exc:
        logger.error("download_file_http_error", url=url, status=exc.response.status_code)
        return f"Error: HTTP {exc.response.status_code} — {url}"
    except Exception as exc:
        logger.error("download_file_error", url=url, path=file_path, error=str(exc))
        return f"Error downloading file: {exc}"
