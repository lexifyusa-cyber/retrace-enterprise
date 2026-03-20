"""
TodoWrite tool — in-memory task management for agent sessions.

Provides structured todo lists that the agent can use to plan and track
multi-step tasks within a conversation.  State is kept in a module-level
dict keyed by conversation_id (ephemeral — lost on server restart).
"""

import json
from typing import Optional

import structlog

logger = structlog.get_logger()

_VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

_todo_stores: dict[str, list[dict]] = {}


def make_todo_for_conversation(conversation_id: str):
    """Return a ``todo_write(todos_json, merge)`` function bound to a conversation."""

    def todo_write(todos_json: str, merge: bool = True) -> str:
        """Create or update a structured todo list for the current session.

        Parameters
        ----------
        todos_json : str
            JSON array of todo objects, each with keys:
              ``id`` (str), ``content`` (str), ``status`` (str).
            Valid statuses: pending, in_progress, completed, cancelled.
        merge : bool
            If True (default), merge into existing todos by ``id``.
            If False, replace the entire list.

        Returns
        -------
        str
            Current state of the todo list after the update.
        """
        try:
            todos = json.loads(todos_json) if isinstance(todos_json, str) else todos_json
        except json.JSONDecodeError as exc:
            return f"Error: invalid JSON — {exc}"

        if not isinstance(todos, list):
            return "Error: todos must be a JSON array"

        for item in todos:
            if not isinstance(item, dict):
                return "Error: each todo must be an object with id, content, status"
            if "id" not in item or "content" not in item or "status" not in item:
                return "Error: each todo must have id, content, and status fields"
            if item["status"] not in _VALID_STATUSES:
                return f"Error: invalid status '{item['status']}'. Use: {', '.join(sorted(_VALID_STATUSES))}"

        if merge and conversation_id in _todo_stores:
            existing = {t["id"]: t for t in _todo_stores[conversation_id]}
            for item in todos:
                existing[item["id"]] = item
            _todo_stores[conversation_id] = list(existing.values())
        else:
            _todo_stores[conversation_id] = list(todos)

        return _format_todos(conversation_id)

    return todo_write


def get_todos(conversation_id: str) -> list[dict]:
    """Return the current todo list for a conversation (for external use)."""
    return list(_todo_stores.get(conversation_id, []))


def clear_todos(conversation_id: str):
    """Remove all todos for a conversation."""
    _todo_stores.pop(conversation_id, None)


def _format_todos(conversation_id: str) -> str:
    """Format the current todo list as a readable string."""
    todos = _todo_stores.get(conversation_id, [])
    if not todos:
        return "Todo list is empty."

    status_icons = {
        "pending": "[ ]",
        "in_progress": "[*]",
        "completed": "[x]",
        "cancelled": "[-]",
    }

    lines = [f"Todo list ({len(todos)} items):\n"]
    for t in todos:
        icon = status_icons.get(t["status"], "[ ]")
        lines.append(f"  {icon} {t['id']}: {t['content']} ({t['status']})")
    return "\n".join(lines)
