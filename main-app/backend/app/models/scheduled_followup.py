"""
Scheduled follow-up — agent-scheduled future turns.

When the agent (via the retrace-scheduler MCP) wants to "wake up later" or
"check every N minutes", a row lands here. The FollowupScheduler service
loads these rows at startup, registers them with APScheduler, and on each
fire POSTs the stored prompt as a user-role message into the user's OC
session — same channel a typed message would take.

This is the persistence layer for Option B (scheduled agent follow-ups),
sibling to SOPs' `schedule_type` / `schedule_config` fields. We don't reuse
SOP rows because the lifecycle, ownership, and visibility are different —
SOPs are admin-approved automations; follow-ups are agent-initiated, scoped
to a single conversation, and the user can cancel them via the agent in
plain English.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ScheduledFollowup(Base):
    """A scheduled future agent turn for a specific conversation."""

    __tablename__ = "scheduled_followups"

    followup_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    # No FK to users — user rows can be deleted; we just skip orphaned schedules
    # at fire time. Indexed for the list-by-user query the MCP tool issues.
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # No FK to conversations either, same reason; null means "any conversation
    # for this user" — fire opens a fresh OC session. Not yet implemented; for
    # now we require a conversation_id.
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    # The exact prompt the agent will see when this fires. Carries everything
    # needed to act — the firing agent re-derives intent from the text alone,
    # so we don't store extra "instruction context" separately (mirrors the
    # Claude Code ScheduleWakeup / CronCreate design).
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # 'once' | 'interval' | 'cron'.
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Shape depends on trigger_type:
    #   once:     {"run_at": "ISO8601"}
    #   interval: {"every": int, "unit": "seconds"|"minutes"|"hours"|"days"}
    #   cron:     {"expression": "* * * * *"}  (standard 5-field cron)
    trigger_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Optional human description (set by the agent so list_followups is
    # readable without unpacking the prompt).
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict:
        return {
            "followup_id": self.followup_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "prompt": self.prompt,
            "trigger_type": self.trigger_type,
            "trigger_config": self.trigger_config,
            "description": self.description,
            "is_active": self.is_active,
            "fire_count": self.fire_count,
            "last_fired_at": self.last_fired_at.isoformat() if self.last_fired_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
