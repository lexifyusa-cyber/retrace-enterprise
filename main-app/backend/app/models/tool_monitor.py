"""
Tool Monitor — the unified change-monitor model.

Supersedes the kind-specific UrlMonitor / CommandMonitor / BrowserMonitor
tables. One row watches the output of ANY tool the backend can invoke,
identified by (target_kind, target_json):

  target_kind = "url"      target_json = {"url": ..., "selector": ...}
  target_kind = "command"  target_json = {"command": ...}
  target_kind = "builtin"  target_json = {"tool": "read_page", "args": {...}}  (Phase 3)
  target_kind = "mcp"      target_json = {"extension_id": ..., "tool": ..., "args": {...}}  (Phase 4)

`fire_when` is the predicate that decides whether a tick wakes the agent
(see app/services/monitor_predicate.py). Default "changed" reproduces the
legacy diff-only behaviour. `last_predicate_state` persists the prior
tick's condition result so edge-triggered predicates (thresholds, staleness)
fire on the transition, not on every tick the condition stays true.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, Boolean, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ToolMonitor(Base):
    """A user-installed tool-output change watcher tied to one conversation."""

    __tablename__ = "tool_monitors"

    monitor_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    # url | command | builtin | mcp
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # Kind-specific invocation parameters (see module docstring).
    target_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    every_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Predicate spec; "changed" (default) reproduces diff-only behaviour.
    fire_when: Mapped[str] = mapped_column(String(200), nullable=False, default="changed")
    prompt_on_change: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # sha256 of the (capped) tool output. Empty until first tick baselines.
    last_content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # Prior tick's predicate result — for edge-triggered firing.
    last_predicate_state: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    check_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict:
        return {
            "monitor_id": self.monitor_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "target_kind": self.target_kind,
            "target_json": self.target_json,
            "every_seconds": self.every_seconds,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "fire_when": self.fire_when,
            "prompt_on_change": self.prompt_on_change,
            "description": self.description,
            "last_content_hash": self.last_content_hash,
            "last_predicate_state": self.last_predicate_state,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
            "last_changed_at": self.last_changed_at.isoformat() if self.last_changed_at else None,
            "last_url": self.last_url,
            "check_count": self.check_count,
            "change_count": self.change_count,
            "fire_count": self.fire_count,
            "last_error": self.last_error,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
