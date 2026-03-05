"""
SOP (Standard Operating Procedure) database model.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SOP(Base):
    """A generated Standard Operating Procedure."""

    __tablename__ = "sops"

    sop_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    product_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    sop_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    sop_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft"
    )
    source_conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, onupdate=datetime.utcnow
    )
    # Scheduling fields
    schedule_type: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, default=None
    )  # none, once, interval, daily, weekly, monthly, cron
    schedule_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def to_dict(self) -> dict:
        return {
            "sop_id": self.sop_id,
            "product_id": self.product_id,
            "title": self.title,
            "goal": self.goal,
            "sop_json": self.sop_json,
            "sop_markdown": self.sop_markdown,
            "status": self.status,
            "source_conversation_id": self.source_conversation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "schedule_type": self.schedule_type,
            "schedule_config": self.schedule_config,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "is_active": self.is_active,
        }
