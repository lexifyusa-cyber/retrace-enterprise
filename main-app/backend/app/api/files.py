"""
Local filesystem browse API.

In trial mode, browsing is disabled — trial users cannot see the host filesystem.
They can only use POD agents or pre-seeded data.
"""

import os
import platform
from datetime import datetime
from pathlib import Path
import concurrent.futures

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from app.core.security import get_current_user, CurrentUser

logger = structlog.get_logger()

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
    """Resolve the path for browsing. '/' or '~' or '' means the parent of home."""
    if path in ("/", "~", ""):
        return Path.home().parent
    if path.startswith("~/") or path.startswith("~\\"):
        return Path.home() / path[2:]
    return Path(path)


@router.get("/browse")
async def browse_local_filesystem(
    path: str = "/",
    current_user: CurrentUser = Depends(get_current_user),
):
    """Browse the local filesystem on the backend server.

    Only zero_admin can browse the filesystem. Trial/tenant users cannot
    see the host filesystem — they should use POD agents for remote data.
    """
    # For non-system admins, browse inside their Docker container's filesystem
    if current_user.role != "zero_admin":
        try:
            from app.services.sandbox_manager import get_or_create_session
            import json as _json

            # Create or reuse sandbox session with 30-second timeout (non-blocking via thread pool)
            # This ensures the session exists before attempting to execute commands
            session = None
            try:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(get_or_create_session, current_user.user_id)
                    session = future.result(timeout=30)
                    if session:
                        logger.info("sandbox_session_ready_for_browse", user_id=current_user.user_id)
            except concurrent.futures.TimeoutError:
                logger.warning("sandbox_session_creation_timeout_during_browse", user_id=current_user.user_id)
                session = None

            if session:
                browse_path = path if path not in ("/", "~", "") else "/workspace"
                # Restrict browsing to /workspace only
                if not browse_path.startswith("/workspace"):
                    browse_path = "/workspace"
                result = session.execute_command(f"python3 -c \"\nimport os, json, datetime\ntarget = '{browse_path}'\nif not os.path.isdir(target):\n    print(json.dumps({{'files':[],'total_count':0,'total_size':0}}))\nelse:\n    files = []\n    total_size = 0\n    for e in os.scandir(target):\n        try:\n            s = e.stat()\n            is_dir = e.is_dir()\n            sz = s.st_size if not is_dir else 0\n            if not is_dir: total_size += sz\n            files.append({{'name':e.name,'path':os.path.join(target,e.name),'rel_path':e.name,'type':'directory' if is_dir else 'file','size':sz,'modified':datetime.datetime.utcfromtimestamp(s.st_mtime).isoformat()+'Z'}})\n        except: pass\n    print(json.dumps({{'files':files,'total_count':len(files),'total_size':total_size}}))\n\"")
                stdout = (result.stdout or "").strip()
                if stdout:
                    return _json.loads(stdout)
        except Exception as e:
            logger.warning("sandbox_browse_error", user_id=current_user.user_id, error=str(e))
        return {"files": [], "total_count": 0, "total_size": 0}

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
