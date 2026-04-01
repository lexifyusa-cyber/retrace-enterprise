"""
In-memory generation progress store for MCP Builder.

Allows the frontend to poll for generation status after navigating away
(which kills the SSE stream). Entries auto-expire after 10 minutes.
"""

from __future__ import annotations

import time

_generation_progress: dict[str, dict] = {}

TTL_SECONDS = 600  # 10 minutes


def _cleanup_expired() -> None:
    now = time.time()
    expired = [k for k, v in _generation_progress.items() if v.get("expires_at", 0) < now]
    for k in expired:
        _generation_progress.pop(k, None)


def update_progress(server_name: str, **fields) -> None:
    """Upsert progress for a generation. Pass any subset of fields to update."""
    _cleanup_expired()
    now = time.time()

    if server_name not in _generation_progress:
        _generation_progress[server_name] = {
            "status": "in_progress",
            "phase": "",
            "iteration": 0,
            "logs": [],
            "result": None,
            "expires_at": now + TTL_SECONDS,
        }

    entry = _generation_progress[server_name]
    entry["expires_at"] = now + TTL_SECONDS

    # Special handling: "log" appends a single line (capped at 50)
    if "log" in fields:
        entry["logs"].append(fields.pop("log"))
        if len(entry["logs"]) > 50:
            entry["logs"] = entry["logs"][-50:]

    entry.update(fields)


def get_progress(server_name: str) -> dict | None:
    """Read progress without removing. Returns None if missing or expired."""
    entry = _generation_progress.get(server_name)
    if entry is None:
        return None
    if entry.get("expires_at", 0) < time.time():
        _generation_progress.pop(server_name, None)
        return None
    return entry


def clear_progress(server_name: str) -> None:
    """Manually remove a progress entry."""
    _generation_progress.pop(server_name, None)
