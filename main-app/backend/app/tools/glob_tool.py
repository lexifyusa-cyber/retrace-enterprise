"""
Glob tool — find files whose paths match a glob pattern.

Uses ``pathlib.Path.glob`` for fast recursive file discovery.  Patterns
not starting with ``**/`` are automatically prepended for recursive
searching.  Common noise directories are excluded by default.
"""

import os
import time
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

MAX_RESULTS = 500

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", ".cache", ".parcel-cache",
}


def glob_search(
    pattern: str,
    directory: str = ".",
    max_results: int = MAX_RESULTS,
) -> str:
    """Find files matching a glob pattern.

    Parameters
    ----------
    pattern : str
        Glob pattern (e.g. ``"*.py"``, ``"src/**/*.tsx"``).  If the pattern
        does not start with ``**/`` it is automatically prepended for
        recursive searching.
    directory : str
        Root directory to search from.  Defaults to current working directory.
    max_results : int
        Maximum number of results to return.

    Returns
    -------
    str
        Matching file paths sorted by modification time (newest first),
        with file sizes.
    """
    if not pattern or not pattern.strip():
        return "Error: empty glob pattern"

    from app.tools.terminal import _cwd
    d = os.path.expanduser(directory)
    if not os.path.isabs(d):
        d = os.path.join(_cwd, d)
    d = os.path.abspath(d)

    if not os.path.isdir(d):
        return f"Error: directory not found — {d}"

    if not pattern.startswith("**/") and not pattern.startswith("/"):
        pattern = "**/" + pattern

    root = Path(d)
    matches: list[tuple[Path, float, int]] = []

    try:
        for fp in root.glob(pattern):
            if not fp.is_file():
                continue
            if any(skip in fp.parts for skip in _SKIP_DIRS):
                continue
            try:
                stat = fp.stat()
                matches.append((fp, stat.st_mtime, stat.st_size))
            except OSError:
                matches.append((fp, 0.0, 0))

            if len(matches) >= max_results * 2:
                break
    except Exception as exc:
        return f"Error during glob search: {exc}"

    matches.sort(key=lambda x: x[1], reverse=True)
    matches = matches[:max_results]

    if not matches:
        return f"No files found matching: {pattern} in {d}"

    lines = [f"Found {len(matches)} file(s) matching '{pattern}':\n"]
    for fp, mtime, size in matches:
        rel = os.path.relpath(fp, d)
        size_str = _fmt_size(size)
        mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "unknown"
        lines.append(f"  {rel}  ({size_str}, {mtime_str})")

    return "\n".join(lines)


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
