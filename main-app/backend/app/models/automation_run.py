"""
Automation run / execution log model.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AutomationRun(Base):
    """Log of a single automation execution."""

    __tablename__ = "automation_runs"

    run_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    sop_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sops.sop_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    trigger: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled"
    )  # scheduled, manual
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running"
    )  # running, completed, failed
    steps_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_log: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "sop_id": self.sop_id,
            "product_id": self.product_id,
            "trigger": self.trigger,
            "status": self.status,
            "steps_total": self.steps_total,
            "steps_completed": self.steps_completed,
            "output_log": self.output_log,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
        }
