"""
Grep tool — search file contents using ripgrep (rg) with Python fallback.

Ripgrep is the gold standard for code search (used by VS Code, Cursor, etc.).
When ``rg`` is not installed, falls back to Python ``re`` + ``os.walk``.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

MAX_RESULTS = 200
MAX_OUTPUT_CHARS = 80_000
_RG_BIN: Optional[str] = shutil.which("rg")

_DEFAULT_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
}


def grep(
    pattern: str,
    path: str = ".",
    glob_filter: Optional[str] = None,
    case_insensitive: bool = False,
    max_results: int = MAX_RESULTS,
    output_mode: str = "content",
    context_lines: int = 0,
) -> str:
    """Search file contents for *pattern* (regex).

    Parameters
    ----------
    pattern : str
        Regular expression to search for.
    path : str
        File or directory to search in.  Defaults to current directory.
    glob_filter : str, optional
        Glob pattern to restrict files (e.g. ``"*.py"``, ``"*.{ts,tsx}"``).
    case_insensitive : bool
        Case-insensitive search.
    max_results : int
        Maximum number of matches to return.
    output_mode : str
        ``"content"`` — matching lines with file:line prefix (default).
        ``"files_with_matches"`` — only file paths.
        ``"count"`` — match counts per file.
    context_lines : int
        Lines of context before and after each match (like ``grep -C``).

    Returns
    -------
    str
        Formatted search results or an error message.
    """
    if not pattern or not pattern.strip():
        return "Error: empty search pattern"

    from app.tools.terminal import _cwd
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(_cwd, p)
    p = os.path.abspath(p)

    if not os.path.exists(p):
        return f"Error: path not found — {p}"

    if _RG_BIN:
        return _rg_search(
            pattern, p, glob_filter, case_insensitive,
            max_results, output_mode, context_lines,
        )
    return _python_search(
        pattern, p, glob_filter, case_insensitive,
        max_results, output_mode,
    )


# ---------------------------------------------------------------------------
# Ripgrep backend
# ---------------------------------------------------------------------------

def _rg_search(
    pattern: str,
    path: str,
    glob_filter: Optional[str],
    case_insensitive: bool,
    max_results: int,
    output_mode: str,
    context_lines: int,
) -> str:
    args = [_RG_BIN, "--no-heading", "--line-number", "--color=never"]

    if case_insensitive:
        args.append("-i")

    if output_mode == "files_with_matches":
        args.append("-l")
    elif output_mode == "count":
        args.append("-c")

    if context_lines > 0 and output_mode == "content":
        args.extend(["-C", str(context_lines)])

    args.extend(["-m", str(max_results)])

    if glob_filter:
        args.extend(["--glob", glob_filter])

    args.extend(["--", pattern, path])

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Error: search timed out after 30 seconds"
    except Exception as exc:
        return f"Error running ripgrep: {exc}"

    output = proc.stdout
    if not output.strip():
        if proc.returncode == 1:
            return f"No matches found for pattern: {pattern}"
        if proc.returncode == 2:
            return f"Error: {proc.stderr.strip()}"
        return f"No matches found for pattern: {pattern}"

    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n[output truncated]"

    lines = output.strip().split("\n")
    header = f"Found matches for '{pattern}'"
    if len(lines) >= max_results:
        header += f" (showing first {max_results})"
    return f"{header}\n\n{output.strip()}"


# ---------------------------------------------------------------------------
# Python fallback
# ---------------------------------------------------------------------------

def _python_search(
    pattern: str,
    path: str,
    glob_filter: Optional[str],
    case_insensitive: bool,
    max_results: int,
    output_mode: str,
) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        return f"Error: invalid regex — {exc}"

    import fnmatch

    results: list[str] = []
    file_counts: dict[str, int] = {}
    match_count = 0

    target = Path(path)
    files = [target] if target.is_file() else sorted(target.rglob("*"))

    for fp in files:
        if not fp.is_file():
            continue
        if any(skip in fp.parts for skip in _DEFAULT_SKIP_DIRS):
            continue
        if glob_filter and not fnmatch.fnmatch(fp.name, glob_filter):
            continue

        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line_num, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = os.path.relpath(fp, path) if target.is_dir() else fp.name

                if output_mode == "files_with_matches":
                    if rel not in file_counts:
                        file_counts[rel] = 0
                        results.append(rel)
                elif output_mode == "count":
                    file_counts[rel] = file_counts.get(rel, 0) + 1
                else:
                    results.append(f"{rel}:{line_num}:{line.rstrip()}")

                match_count += 1
                if match_count >= max_results:
                    break
        if match_count >= max_results:
            break

    if output_mode == "count":
        lines = [f"{fp}:{cnt}" for fp, cnt in file_counts.items()]
        if not lines:
            return f"No matches found for pattern: {pattern}"
        return f"Match counts for '{pattern}':\n\n" + "\n".join(lines)

    if not results:
        return f"No matches found for pattern: {pattern}"

    header = f"Found matches for '{pattern}'"
    if match_count >= max_results:
        header += f" (showing first {max_results})"
    return header + "\n\n" + "\n".join(results)
