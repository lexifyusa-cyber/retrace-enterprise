"""
Local filesystem browse API.

Browse the backend server's local filesystem for adding training data.
"""

import os
import platform
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

router = APIRouter()

# Allowed roots for browsing - prevent directory traversal outside these
def _allowed_roots() -> list[Path]:
    roots: list[Path] = []
    if platform.system() == "Windows":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{letter}:\\")
            if p.exists():
                roots.append(p.resolve())
    else:
        roots.append(Path("/").resolve())
    return roots


def is_path_allowed(path_str: str) -> bool:
    """Check if path is under an allowed root. Use for validating local folder paths."""
    ok, _ = _is_path_allowed(path_str)
    return ok


def _is_path_allowed(path_str: str) -> tuple[bool, Path | None]:
    """Check if path is under an allowed root. Returns (allowed, resolved_path)."""
    if not path_str:
        return False, None
    try:
        if path_str.startswith("~/") or path_str.startswith("~\\"):
            path = Path.home() / path_str[2:]
        else:
            path = Path(path_str)
        if not path.is_absolute():
            # Resolve relative to cwd
            path = path.resolve()
        resolved = path.resolve()
        for root in _allowed_roots():
            try:
                resolved.relative_to(root)
                return True, resolved
            except ValueError:
                continue
        return False, None
    except (OSError, RuntimeError):
        return False, None


def _resolve_browse_path(path: str) -> Path:
    """Resolve the path for browsing. '/' or '~' or '' means the parent of home (e.g. /Users, C:\\Users)."""
    if path in ("/", "~", ""):
        return Path.home().parent  # e.g. /Users (macOS) or /home (Linux) or C:\Users (Windows)
    if path.startswith("~/") or path.startswith("~\\"):
        return Path.home() / path[2:]
    return Path(path)


@router.get("/browse")
async def browse_local_filesystem(path: str = "/"):
    """Browse the local filesystem on the backend server.

    Returns a DirectoryListing structure compatible with pod browse.
    Path '/' or '~' means the directory containing home (e.g. /Users, /home, C:\\Users).
    Other paths must be under allowed roots.
    """
    resolved = _resolve_browse_path(path)
    if path not in ("/", "~", ""):
        allowed, resolved_check = _is_path_allowed(path)
        if not allowed or resolved_check is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Path is not under allowed browse roots",
            )
        resolved = resolved_check

    if not resolved.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory",
        )

    # Ensure path is under an allowed root (home always is)
    allowed, _ = _is_path_allowed(str(resolved))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Path is not under allowed browse roots",
        )

    files: list[dict] = []
    total_size = 0
    try:
        with os.scandir(resolved) as it:
            for entry in it:
                try:
                    stat = entry.stat()
                    name = entry.name
                    entry_path = str(resolved / name)
                    rel_path = name
                    is_dir = entry.is_dir()
                    size = stat.st_size if not is_dir else 0
                    if not is_dir:
                        total_size += size
                    modified = stat.st_mtime
                    modified_iso = datetime.utcfromtimestamp(modified).isoformat() + "Z"
                    files.append({
                        "name": name,
                        "path": entry_path,
                        "rel_path": rel_path,
                        "type": "directory" if is_dir else "file",
                        "size": size,
                        "modified": modified_iso,
                    })
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError) as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot read directory: {e!s}",
        )

    return {
        "files": files,
        "total_count": len(files),
        "total_size": total_size,
    }
