"""
Remote Trigger — webhook endpoint that wakes the agent on external events.

Each row owns a long random `token` that becomes part of a URL the user
hands to an external system (GitHub Actions, Slack, Linear, cron servers
on other boxes, etc.). When that URL is POSTed to, retrace looks up the
trigger by token, builds a prompt from `prompt_template` + the payload,
and injects it as a synthetic user message into the conversation — same
delivery path the scheduled follow-ups and URL monitors use.

This is the "wake from outside" capability — equivalent to Claude Code's
RemoteTrigger. With it the agent can be plugged into the user's existing
event sources (CI, chat, ticketing) without polling.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class RemoteTrigger(Base):
    """A webhook target tied to one conversation."""

    __tablename__ = "remote_triggers"

    trigger_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    # The url-path secret. 32 random url-safe bytes ≈ 43 chars; effectively
    # un-guessable. Indexed because every webhook hit looks this up.
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # The instruction the agent will see when this trigger fires. The
    # incoming POST body (as JSON-pretty-printed text) is appended after
    # this template so the agent has the raw event details. Phase 1 does
    # NOT do field substitution — keeps the contract simple and resistant
    # to template-injection attacks from untrusted payload sources.
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional human label for list_remote_triggers.
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self, include_token: bool = False) -> dict:
        d = {
            "trigger_id": self.trigger_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "prompt_template": self.prompt_template,
            "description": self.description,
            "is_active": self.is_active,
            "fire_count": self.fire_count,
            "last_fired_at": self.last_fired_at.isoformat() if self.last_fired_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        # Token only included when the caller is the creator (returned from
        # the create endpoint). list_remote_triggers does NOT echo tokens
        # back — once an URL leaks, the agent shouldn't make it easy to
        # re-read it from chat history.
        if include_token:
            d["token"] = self.token
        return d
